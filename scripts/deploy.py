#!/usr/bin/env python3
"""Receive a verified Coconut source package on its execution Mac."""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import http.client
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 4096
REQUIRED_FILES = {'backend/coconut/__init__.py', 'backend/coconut/__main__.py', 'scripts/host.py'}
MARKER_PATH = Path('.coconut-install.json')
# The service answers its health check after startup finishes.
HEALTH_WAIT_SECONDS = 300
# A probe whose failure is expected, such as the check for an already restored service, waits only this long.
# This is the earlier wait. With the faster start migrations, a restored service answers well within it.
PROBE_WAIT_SECONDS = 60
# The update waits this long for running turns to finish (host.DRAIN_SECONDS) and a little more for the helper.
DRAIN_WAIT_SECONDS = 600 + 90



class DeployError(Exception):
    pass


@dataclass
class Package:
    files: dict[str, bytes]
    hashes: dict[str, str]
    package_id: str


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def allowed_path(name):
    if not isinstance(name, str) or '\\' in name or any(ord(char) < 32 for char in name):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or any(part in ('', '.', '..') for part in path.parts):
        return False
    return bool(
        name in {'README.md', 'backend/pyproject.toml', 'backend/coconut/hermes_versions.json', 'Start Coconut.command',
                 'Uninstall Coconut.command'}
        or re.fullmatch(r'backend/coconut/(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*\.py', name)
        or re.fullmatch(r'scripts/[A-Za-z0-9_-]+\.(?:py|swift)', name)
        or re.fullmatch(r'ios/Tunnel/[A-Za-z0-9_-]+\.swift', name)
        or re.fullmatch(r'docs/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.md', name)
    )


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DeployError('The package contains a duplicate JSON field.')
        result[key] = value
    return result


def validate_package(stream):
    compressed = stream.read(MAX_COMPRESSED_BYTES + 1)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise DeployError('The compressed package exceeds its size limit.')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
            data = archive.read(MAX_UNCOMPRESSED_BYTES + 1)
        if len(data) > MAX_UNCOMPRESSED_BYTES:
            raise DeployError('The expanded package exceeds its size limit.')
        files = {}
        folded = set()
        last_end = 0
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
            for entry in archive:
                if len(files) >= MAX_FILES or entry.name in files or entry.name.casefold() in folded:
                    raise DeployError('The package contains too many files or a duplicate path.')
                if entry.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or entry.issparse():
                    raise DeployError('The package must contain regular files only.')
                if entry.name != 'deployment.json' and not allowed_path(entry.name):
                    raise DeployError('The package contains a path outside the source allowlist.')
                if not 0 <= entry.size <= MAX_FILE_BYTES:
                    raise DeployError('A package file exceeds its size limit.')
                handle = archive.extractfile(entry)
                content = handle.read(MAX_FILE_BYTES + 1) if handle else b''
                if len(content) != entry.size:
                    raise DeployError('A package file is incomplete.')
                files[entry.name] = content
                folded.add(entry.name.casefold())
                last_end = entry.offset_data + ((entry.size + 511) // 512) * 512
        if any(data[last_end:]):
            raise DeployError('The package contains data after the archive ends.')
        manifest_data = files.pop('deployment.json', None)
        if manifest_data is None:
            raise DeployError('The package has no deployment manifest.')
        manifest = json.loads(manifest_data, object_pairs_hook=unique_object)
        if not isinstance(manifest, dict) or set(manifest) != {'version', 'files'} or type(manifest['version']) is not int or manifest['version'] != 1:
            raise DeployError('The deployment manifest version is unsupported.')
        hashes = manifest['files']
        if not isinstance(hashes, dict) or set(hashes) != set(files) or not REQUIRED_FILES <= set(files):
            raise DeployError('The deployment manifest does not match the complete source package.')
        for name, expected in hashes.items():
            if not allowed_path(name) or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected) or sha256(files[name]) != expected:
                raise DeployError('A source file does not match its manifest hash.')
            if name.endswith('.py'):
                try:
                    compile(files[name], name, 'exec')
                except (SyntaxError, ValueError):
                    raise DeployError('The package contains invalid Python: ' + name) from None
        return Package(files, hashes, sha256(canonical(hashes)))
    except DeployError:
        raise
    except (OSError, EOFError, tarfile.TarError, ValueError, TypeError, KeyError):
        raise DeployError('The source package or its manifest is invalid.') from None


def safe_path(path, boundary):
    if path != boundary and boundary not in path.parents:
        raise DeployError('The installation path leaves its home directory.')
    for component in (path, *path.parents):
        if component.is_symlink():
            raise DeployError('The installation contains a symbolic link.')
        if component == boundary:
            break
    return path


def destination(directory, home):
    if not isinstance(directory, str) or not directory or any(ord(char) < 32 for char in directory) or '\\' in directory:
        raise DeployError('Use a relative installation directory inside your home folder.')
    relative = PurePosixPath(directory)
    if not relative.parts or relative.is_absolute() or str(relative) != directory or any(part in ('.', '..') for part in relative.parts):
        raise DeployError('Use a relative installation directory inside your home folder.')
    return safe_path(home.joinpath(*relative.parts), home)


def normalized_address(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port if parsed.port is not None else 443
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment or '?' in value or '#' in value
                or not 1 <= port <= 65535 or any(char.isspace() or ord(char) < 32 for char in value)):
            raise ValueError()
        host = parsed.hostname.lower().rstrip('.')
        if not re.fullmatch(r'(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]+)?', parsed.netloc):
            raise ValueError()
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            if len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.')):
                raise ValueError()
        display = '[' + host + ']' if ':' in host else host
        return f'https://{display}:{port}'
    except (ValueError, TypeError, AttributeError):
        raise DeployError('Use a valid HTTPS address without credentials or a path.') from None


def read_marker(root):
    path = safe_path(root / MARKER_PATH, root)
    if not path.exists():
        if root.exists() and any(root.iterdir()):
            raise DeployError('This directory is not an owned Coconut deployment. Choose an empty directory.')
        return None
    try:
        marker = json.loads(path.read_text(), object_pairs_hook=unique_object)
        if type(marker['version']) is not int or marker['version'] != 1 or str(UUID(marker['installation_id'])) != marker['installation_id'] or not isinstance(marker['files'], dict) or len(marker['files']) > MAX_FILES:
            raise ValueError()
        if any(not allowed_path(name) or not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for name, value in marker['files'].items()):
            raise ValueError()
        if marker['state'] not in ('installed', 'pending', 'failed'):
            raise ValueError()
        if marker['files'] and marker.get('package_id') != sha256(canonical(marker['files'])):
            raise ValueError()
        normalized_address(marker['address'])
        return marker
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        raise DeployError('The Coconut deployment marker is invalid.') from None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.coconut-', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_host(root):
    source = safe_path(root / 'scripts/host.py', root)
    spec = importlib.util.spec_from_file_location('coconut_deploy_host_' + uuid4().hex, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Operations:
    @staticmethod
    def terminate_group(process):
        # macOS answers "not permitted" for a group whose last process has just exited and waits to be reaped. The group
        # is gone for our purpose, exactly as when it is not found.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def command(self, command, root, phase, *, input_data=None, timeout=1200):
        try:
            process = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError:
            raise DeployError(phase + ' could not start. No command output was exposed.') from None
        try:
            output, _ = process.communicate(input=input_data, timeout=timeout)
        except BaseException as error:
            self.terminate_group(process)
            if isinstance(error, subprocess.TimeoutExpired):
                raise DeployError(phase + ' timed out. Its process group was stopped.') from None
            raise
        if process.returncode:
            self.terminate_group(process)
            raise DeployError(phase + f' failed (exit {process.returncode}). No command output was exposed.')
        return output

    def preflight(self, root, address, package):
        if platform.system() != 'Darwin':
            raise DeployError('The execution host must run macOS.')
        self.command(['/usr/bin/xcode-select', '-p'], Path.home(), 'Command Line Tools check', timeout=15)
        self.command(['/usr/bin/xcrun', '--find', 'clang'], Path.home(), 'Compiler check', timeout=15)
        self.command(['/usr/bin/git', '--version'], Path.home(), 'Git check', timeout=15)

    def helper(self, root, operation, state=None, source=None, timeout=60):
        program = '''import importlib.util,json,sys
from pathlib import Path
root=Path(sys.argv[1])
sys.path.insert(0,str(root/'scripts'))
spec=importlib.util.spec_from_file_location('coconut_deploy_host',root/'scripts/host.py')
module=importlib.util.module_from_spec(spec)
if len(sys.argv)>3:
    exec(compile(Path(sys.argv[3]).read_bytes(),str(root/'scripts/host.py'),'exec'),module.__dict__)
else:
    spec.loader.exec_module(module)
state=json.load(sys.stdin)
if sys.argv[2]=='inspect':
    _,loaded=module.owned_agent()
    process=module.owned_process()
    result={'mode':'managed' if loaded else 'manual' if process else 'stopped','project':str(root),'data_dir':str(root/'.runtime/host')}
    if result['mode']=='manual': result['options']=process[1]
elif sys.argv[2]=='stop': result=module.stop_for_update()
elif sys.argv[2]=='drain':
    # An installed host.py from before the drain has no such function. Then the service is stopped at once, as it was.
    drain=getattr(module,'drain_for_update',None)
    result={'drained': bool(drain()) if drain else False}
else: result=module.restore_after_update(state)
print('COCONUT_HOST_STATE='+json.dumps(result))
'''
        command = [sys.executable, '-c', program, str(root), operation]
        if source is not None:
            command.append(str(source))
        output = self.command(command, root, 'Service ' + operation, input_data=canonical(state), timeout=timeout)
        lines = [line for line in output.decode('utf-8', errors='replace').splitlines() if line.startswith('COCONUT_HOST_STATE=')]
        if len(lines) != 1:
            raise DeployError('The service helper returned an invalid state.')
        try:
            return json.loads(lines[0].split('=', 1)[1])
        except ValueError:
            raise DeployError('The service helper returned an invalid state.') from None

    def assert_stopped(self, root):
        data = safe_path(root / '.runtime/host', root)
        if (data / 'process.json').exists():
            raise DeployError('A process record prevents recovery without a host helper.')
        path = safe_path(data / 'service.lock', root)
        if path.exists():
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'a') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise DeployError('Coconut is still running.') from None

    def inspect(self, root):
        return self.helper(root, 'inspect')

    def drain(self, root):
        """Let the running turns of the service finish before the update stops it. See host.drain_for_update."""
        return self.helper(root, 'drain', timeout=DRAIN_WAIT_SECONDS)

    def stop(self, root):
        return self.helper(root, 'stop')

    def stop_recovery(self, root, source):
        return self.helper(root, 'stop', source=source)

    def restore(self, root, state):
        self.helper(root, 'restore', state)

    def prepare(self, root, address, package):
        self.command([sys.executable, str(root / 'scripts/push.py'), '--rebuild'], root, 'Notification encryption build')
        # An installed tunnel sidecar gets the new code. A Mac without it stays without it.
        self.command([sys.executable, str(root / 'scripts/tunnel.py'), 'rebuild'], root, 'Tunnel sidecar build')
        cert, key = root / '.runtime/host/server.crt', root / '.runtime/host/server.key'
        if not cert.exists() and not key.exists():
            self.command([sys.executable, str(root / 'scripts/host.py'), 'setup', '--address', address], root, 'Secure host setup')
        elif not cert.is_file() or not key.is_file():
            raise DeployError('The existing certificate pair is incomplete. Coconut kept it unchanged.')

    def hermes(self, root):
        """Find the person's Hermes and run the contract check of the new code against it. See hermes_runtime.status."""
        output = self.command([sys.executable, str(root / 'scripts/host.py'), 'hermes-status'], root, 'Hermes check',
                              timeout=600)
        lines = [line for line in output.decode('utf-8', errors='replace').splitlines() if line.startswith('COCONUT_HERMES_STATUS=')]
        try:
            result = json.loads(lines[-1].split('=', 1)[1])
            if result.get('status') not in ('ok', 'missing', 'incompatible'):
                raise ValueError()
        except (IndexError, ValueError, AttributeError):
            raise DeployError('The Hermes check returned an invalid result.') from None
        return result

    def install(self, root, address):
        plist = Path.home() / 'Library/LaunchAgents/local.coconut.agent.plist'
        if plist.exists() or plist.is_symlink():
            self.restore(root, {'mode': 'managed', 'project': str(root), 'data_dir': str(root / '.runtime/host')})
            return
        self.command([sys.executable, str(root / 'scripts/host.py'), 'install-service', '--address', address], root, 'Login service installation', timeout=60)

    def health(self, root, address):
        return self._health(root, address, HEALTH_WAIT_SECONDS)

    def probe(self, root, address):
        """Check health briefly, for a check whose failure is expected. This keeps the earlier 60-second wait."""
        return self._health(root, address, PROBE_WAIT_SECONDS)

    def _health(self, root, address, seconds):
        cert = safe_path(root / '.runtime/host/server.crt', root)
        try:
            context = load_host(root).trusted_certificate_context(cafile=str(cert))
            expected = sha256(ssl.PEM_cert_to_DER_cert(cert.read_text()))
        except (OSError, ValueError, ssl.SSLError):
            raise DeployError('The saved certificate cannot validate the service.') from None
        parsed = urlsplit(address)
        # Startup migrations can take minutes on an active host with a large database. A deadline that is
        # too short fails a healthy update, rolls it back, and restarts the service again.
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 443, timeout=2)
            raw = None
            try:
                raw = socket.create_connection(('127.0.0.1', parsed.port or 443), timeout=2)
                connection.sock = context.wrap_socket(raw, server_hostname=parsed.hostname)
                raw = None
                if sha256(connection.sock.getpeercert(binary_form=True)) != expected:
                    raise DeployError('The running service certificate does not match its saved certificate.')
                connection.request('GET', '/health')
                response = connection.getresponse()
                data = response.read(65537)
                if len(data) > 65536:
                    raise DeployError('The service returned an oversized health response.')
                result = json.loads(data)
                if response.status == 200 and result == {'ok': True, 'api_version': 1}:
                    return {'ok': True, 'api_version': 1, 'scope': 'local_tls', 'certificate_sha256': expected.upper()}
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                if raw is not None:
                    raw.close()
                connection.close()
            time.sleep(.25)
        raise DeployError('The service did not pass its local TLS health check.')


def journal_paths(root, record):
    deployment = root / '.runtime/deploy'
    result = []
    for field, prefix in (('staging', 'stage-'), ('backup', 'rollback-')):
        name = record.get(field)
        if not isinstance(name, str) or not re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9_-]+', name):
            raise DeployError('The deployment journal contains an invalid recovery path.')
        result.append(safe_path(deployment / name, root))
    return result


def remove_journal(root, record):
    for path in journal_paths(root, record):
        if path.exists():
            shutil.rmtree(path)
    (root / '.runtime/deploy/update.json').unlink(missing_ok=True)


def copy_source(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.coconut-source-', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            with source.open('rb') as content:
                shutil.copyfileobj(content, handle)
            handle.flush()
            os.fsync(handle.fileno())
            temporary.chmod(source.stat().st_mode & 0o777)
            os.replace(temporary, target)
            sync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)


# Older installers updated a private Hermes runtime with the code. Their journal has a runtime_change. Such an update can
# stop after it moved .runtime/hermes to hermes-rollback-<id> and before it moved the shared settings back
# (.runtime/hermes/terminal). Recovery of such a journal restores that runtime with its settings, as those installers did.
def runtime_paths(root, record):
    change = record.get('runtime_change')
    if change is None:
        return None
    if (not isinstance(change, dict) or set(change) != {'previous', 'candidate'}
            or any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{40}', v) for v in change.values())
            or change['previous'] == change['candidate']):
        raise DeployError('The runtime update journal is invalid.')
    identity = str(UUID(record['deployment_id']))
    return tuple(safe_path(root / '.runtime' / name, root) for name in (
        'hermes', 'hermes-rollback-' + identity, 'hermes-failed-' + identity))


def move_terminal_state(source, target, root):
    # Keep the authoritative native terminal registry and its lock together.
    for name in ('terminal', 'terminal.lock'):
        old, new = safe_path(source / name, root), safe_path(target / name, root)
        if old.exists():
            if new.exists():
                raise DeployError('Two copies of native terminal state prevent the runtime update.')
            old.rename(new)
            sync_directory(source)
            sync_directory(target)


def restore_runtime(root, record):
    paths = runtime_paths(root, record)
    if paths is None:
        return
    runtime, backup, failed = paths
    if backup.exists():
        receipt = safe_path(backup / 'install.json', root)
        if json.loads(receipt.read_text()).get('commit') != record['runtime_change']['previous']:
            raise DeployError('The saved Hermes runtime does not match the rollback revision.')
        if runtime.exists():
            move_terminal_state(runtime, backup, root)
            if failed.exists():
                raise DeployError('A failed runtime already exists. Preserve it before retrying recovery.')
            runtime.rename(failed)
            sync_directory(runtime.parent)
        backup.rename(runtime)
        sync_directory(runtime.parent)
    receipt = safe_path(runtime / 'install.json', root)
    if not receipt.is_file() or json.loads(receipt.read_text()).get('commit') != record['runtime_change']['previous']:
        raise DeployError('The previous Hermes runtime is unavailable. Recovery stopped.')


def prune_runtime_copies(root):
    """Remove the copies of the private Hermes runtime that older versions kept for a rollback.

    Older versions kept the previous runtime as hermes-rollback-<id> and a failed one as hermes-failed-<id>, each about a
    gigabyte. Coconut now runs on the person's Hermes, so no copy serves a rollback. A manual copy with another name stays.
    """
    # Cleanup only reclaims space. It must not turn an update into a failed one.
    with contextlib.suppress(OSError, DeployError):
        for old in (root / '.runtime').glob('hermes-*'):
            # A copy that holds the shared settings of the bots is their only copy. It stays.
            if (runtime_copy_name(old.name) and old.is_dir() and not old.is_symlink()
                    and not any((old / name).exists() for name in ('terminal', 'terminal.lock'))):
                shutil.rmtree(safe_path(old, root), ignore_errors=True)


def runtime_copy_name(name):
    """Return True for the name of a runtime copy that an older update made. Another name is a manual copy."""
    for prefix in ('hermes-rollback-', 'hermes-failed-'):
        if name.startswith(prefix):
            with contextlib.suppress(ValueError):
                return name == prefix + str(UUID(name.removeprefix(prefix)))
    return False


def reconcile(root, operations):
    journal = safe_path(root / '.runtime/deploy/update.json', root)
    if not journal.exists():
        return
    try:
        record = json.loads(journal.read_text(), object_pairs_hook=unique_object)
        marker = read_marker(root)
        old = record['original']
        if (type(record['version']) is not int or record['version'] != 1 or marker is None
                or old['installation_id'] != marker['installation_id']
                or not isinstance(old['files'], dict) or not isinstance(record['new_files'], dict)
                or max(len(old['files']), len(record['new_files'])) > MAX_FILES
                or str(UUID(record['deployment_id'])) != record['deployment_id']
                or record['new_package_id'] != sha256(canonical(record['new_files']))
                or record['phase'] not in ('prepared', 'stopped', 'applying', 'starting', 'rollback_stopped', 'code_restored', 'rolled_back')
                or not isinstance(record['service_state'], dict)
                or record['service_state'].get('mode') not in ('managed', 'manual', 'stopped')):
            raise ValueError()
        for names in (old['files'], record['new_files']):
            if any(not allowed_path(name) or not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for name, value in names.items()):
                raise ValueError()
        staging, backup = journal_paths(root, record)
        runtime_paths(root, record)
    except (ValueError, KeyError, TypeError, OSError, AttributeError):
        raise DeployError('The incomplete deployment has an invalid recovery journal. No source or data was changed.') from None
    if (marker.get('state') == 'installed' and marker.get('deployment_id') == record.get('deployment_id') and record.get('deployment_id') and marker.get('files') == record['new_files']) or record['phase'] == 'rolled_back':
        remove_journal(root, record)
        return
    for name, expected in old['files'].items():
        source = safe_path(backup / name, root)
        if not source.is_file() or sha256(source.read_bytes()) != expected:
            raise DeployError('The source backup is incomplete. Recovery stopped without changing current source or data.')
    for name in set(old['files']) | set(record['new_files']):
        path = safe_path(root / name, root)
        if path.exists() and (not path.is_file() or sha256(path.read_bytes()) not in {old['files'].get(name), record['new_files'].get(name)}):
            raise DeployError('Source changed outside the interrupted update. Recovery stopped to preserve those changes.')
    if record['phase'] == 'code_restored' and old['files'] and marker == old and all(
            (root / name).is_file() and sha256((root / name).read_bytes()) == expected
            for name, expected in old['files'].items()) and not any(
                (root / name).exists() for name in set(record['new_files']) - set(old['files'])) and operations.inspect(root) == record['service_state']:
        try:
            if record['service_state']['mode'] != 'stopped':
                # This probe usually fails on a stopped service, so it must not wait the full startup deadline.
                operations.probe(root, old['address'])
            else:
                operations.assert_stopped(root)
        except DeployError:
            pass
        else:
            remove_journal(root, record)
            return
    try:
        if (root / 'scripts/host.py').is_file():
            operations.stop(root)
        elif not old['files'] and record['phase'] in ('starting', 'rollback_stopped', 'code_restored'):
            source = safe_path(backup / 'scripts/host.py', root)
            if not source.is_file() or sha256(source.read_bytes()) != record['new_files'].get('scripts/host.py'):
                raise DeployError('The saved service helper is incomplete.')
            operations.stop_recovery(root, source)
        elif not old['files'] and record['phase'] in ('prepared', 'stopped', 'applying'):
            operations.assert_stopped(root)
        else:
            raise DeployError('The service cannot prove that execution stopped.')
    except BaseException as error:
        raise DeployError('The incomplete update could not stop its service. Its journal, source, and runtime remain intact.') from error
    record['phase'] = 'rollback_stopped'
    write_json(journal, record)
    restore_runtime(root, record)
    for name in set(record['new_files']) - set(old['files']):
        path = safe_path(root / name, root)
        if path.exists():
            path.unlink()
            sync_directory(path.parent)
    for name in old['files']:
        copy_source(backup / name, safe_path(root / name, root))
    write_json(root / MARKER_PATH, old)
    record['phase'] = 'code_restored'
    write_json(journal, record)
    try:
        if old['files']:
            operations.restore(root, record['service_state'])
            if record['service_state']['mode'] != 'stopped':
                operations.health(root, old['address'])
    except BaseException as error:
        raise DeployError('Previous source was restored, but its service could not restart. Runtime data was not rewound.') from error
    record['phase'] = 'rolled_back'
    write_json(journal, record)
    remove_journal(root, record)


def read_hermes_status(root):
    """Return the Hermes status that the service saved at its last start, or None. See hermes_runtime.status."""
    path = root / '.runtime/host/hermes.json'
    try:
        safe_path(path, root)
        value = json.loads(path.read_text())
    except (OSError, ValueError, DeployError):
        return None
    return value if isinstance(value, dict) else None


def check_hermes(operations, root, previous, *, updating):
    """Stop an update that breaks Coconut on a Hermes that worked before it. receive() then restores the earlier code.

    The update changes Coconut only. When Hermes was already missing or incompatible, the new code starts in refuse
    mode, and the phone shows the reason. An installation from before this check ran on its own private Hermes, which
    worked, so it saved no status.
    """
    current = operations.hermes(root)
    worked = previous is None or previous.get('status') == 'ok'
    if updating and worked and current.get('status') != 'ok':
        raise DeployError('The new version does not work with the installed Hermes, which worked before: '
                          + str(current.get('reason') or current.get('status')))
    # The new code starts with this result, so it does not run the same check again while the service is down.
    # It reuses the result only for the same Hermes and code. See hermes_runtime.saved_still_valid.
    write_json(safe_path(root / '.runtime/host/hermes.json', root), current)


def check_runtime_receipt(root, marker):
    """Refuse runtime data that another installation left. Return the receipt path and whether it exists."""
    runtime_receipt = safe_path(root / '.runtime/deploy/runtime.json', root)
    runtime_present = runtime_receipt.is_file()
    if runtime_present:
        try:
            value = json.loads(runtime_receipt.read_text(), object_pairs_hook=unique_object)
            if value != {'version': 1, 'installation_id': marker['installation_id']} or type(value['version']) is not int:
                raise ValueError()
        except (ValueError, TypeError, KeyError, OSError):
            raise DeployError('The installed runtime receipt is invalid. Coconut kept its files unchanged.') from None
    return runtime_receipt, runtime_present


def recover(directory, *, home=None, operations=None):
    """Finish or roll back a pending update from its journal. Install no package. Return True when a journal was pending.

    A failed update can leave the service stopped, and receive() recovers only as its first step.
    """
    os.umask(0o077)
    root = destination(directory, Path(home or Path.home()).resolve())
    deployment = safe_path(root / '.runtime/deploy', root)
    journal = safe_path(deployment / 'update.json', root)
    marker = read_marker(root)
    if marker is None or not journal.exists():
        return False
    # receive() makes this check before it recovers a journal.
    check_runtime_receipt(root, marker)
    descriptor = os.open(deployment / 'deploy.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError('Another Coconut deployment is already running.') from None
        reconcile(root, operations or Operations())
    return True


def receive(directory, address, stream, *, home=None, operations=None):
    os.umask(0o077)
    home = Path(home or Path.home()).resolve()
    root = destination(directory, home)
    address = normalized_address(address)
    package = validate_package(stream)
    marker = read_marker(root)
    runtime_receipt, runtime_present = check_runtime_receipt(root, marker)
    operations = operations or Operations()
    operations.preflight(root, address, package)
    if marker and normalized_address(marker['address']) != address:
        raise DeployError('The saved address differs. Change the host address separately before updating.')
    if marker is None and root.exists() and any(root.iterdir()):
        raise DeployError('This destination is no longer empty.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    deployment = safe_path(root / '.runtime/deploy', root)
    deployment.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(deployment / 'deploy.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError('Another Coconut deployment is already running.') from None
        if marker is not None:
            if read_marker(root) != marker:
                raise DeployError('The installation changed while the package was being checked. Retry the update.')
        elif (root / MARKER_PATH).exists() or any(path.name != '.runtime' for path in root.iterdir()):
            raise DeployError('Another deployment claimed this directory. Retry the update.')
        original = marker
        marker = marker or {'version': 1, 'installation_id': str(uuid4()), 'files': {}, 'package_id': '', 'state': 'pending', 'address': address}
        write_json(root / MARKER_PATH, marker)
        reconcile(root, operations)
        marker = read_marker(root)
        initial_install = not marker['files'] or not runtime_present or bool(marker.get('initial_install_pending'))
        if initial_install and not marker.get('initial_install_pending'):
            marker = dict(marker, initial_install_pending=True)
            write_json(root / MARKER_PATH, marker)
        for name in set(package.files) | set(marker['files']):
            path = safe_path(root / name, root)
            if name in marker['files']:
                if not path.is_file() or sha256(path.read_bytes()) != marker['files'][name]:
                    raise DeployError('A managed source file changed. Coconut preserved the local edit; restore it before updating.')
            elif path.exists():
                raise DeployError('A package path would replace an unmanaged file or directory.')
        staging = Path(tempfile.mkdtemp(prefix='stage-', dir=deployment))
        backup = Path(tempfile.mkdtemp(prefix='rollback-', dir=deployment))
        old_names = set(marker['files'])
        for name, data in package.files.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            target.chmod(0o700 if name.endswith('.command') else 0o600)
        for name in old_names:
            copy_source(safe_path(root / name, root), backup / name)
        if not old_names:
            copy_source(staging / 'scripts/host.py', backup / 'scripts/host.py')
        # The service saved its Hermes status at its last start. Read it before the new code checks Hermes again.
        previous_hermes = read_hermes_status(root)
        state = operations.inspect(root) if old_names else {'mode': 'stopped', 'project': str(root), 'data_dir': str(root / '.runtime/host')}
        if not isinstance(state, dict) or state.get('mode') not in ('managed', 'manual', 'stopped'):
            raise DeployError('The service returned an invalid execution mode.')
        record = {'version': 1, 'deployment_id': str(uuid4()), 'original': marker, 'new_files': package.hashes, 'new_package_id': package.package_id,
                  'staging': staging.name, 'backup': backup.name, 'service_state': state, 'phase': 'prepared'}
        journal = deployment / 'update.json'
        write_json(journal, record)
        applied = False
        try:
            if old_names:
                # Turns that a stop interrupts lose their tool results, and their tasks fail at the next start.
                if hasattr(operations, 'drain'):
                    operations.drain(root)
                stopped = operations.stop(root)
                if stopped != state:
                    record['service_state'] = stopped
                    write_json(journal, record)
                    raise DeployError('The service mode changed during the update. Previous source was kept.')
            record['phase'] = 'stopped'
            write_json(journal, record)
            record['phase'] = 'applying'
            write_json(journal, record)
            applied = True
            for name in package.files:
                target = safe_path(root / name, root)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging / name, target)
                sync_directory(target.parent)
            for name in old_names - set(package.files):
                safe_path(root / name, root).unlink()
                sync_directory((root / name).parent)
            operations.prepare(root, address, package)
            check_hermes(operations, root, previous_hermes, updating=bool(old_names))
            record['phase'] = 'starting'
            write_json(journal, record)
            if initial_install:
                operations.install(root, address)
                mode = 'managed'
            else:
                operations.restore(root, state)
                mode = state['mode']
            health = operations.health(root, address) if mode != 'stopped' else {'ok': False, 'status': 'not_running'}
            updated = dict(marker, deployment_id=record['deployment_id'], files=package.hashes, package_id=package.package_id, state='installed', updated_at=datetime.now(timezone.utc).isoformat())
            updated.pop('initial_install_pending', None)
            updated.pop('recovery_state', None)
            write_json(runtime_receipt, {'version': 1, 'installation_id': marker['installation_id']})
            write_json(root / MARKER_PATH, updated)
            remove_journal(root, record)
            prune_runtime_copies(root)
            return {'version': 1, 'installation_id': marker['installation_id'], 'package_id': package.package_id, 'mode': mode, 'address': address, 'health': health, 'updated': original is not None}
        except BaseException as error:
            if not applied:
                # A failed stop can have unloaded the job. Keep its recovery state.
                raise DeployError('Source was not replaced. The recovery journal remains; check service status before retrying.') from error
            try:
                reconcile(root, operations)
            except DeployError as recovery_error:
                raise DeployError(str(recovery_error)) from error
            detail = str(error) if isinstance(error, DeployError) else type(error).__name__
            raise DeployError('The update failed. Previous source and mode were restored; runtime data was not rewound. ' + detail) from error


def main():
    def interrupt(signum, frame):
        raise KeyboardInterrupt()
    for name in ('SIGTERM', 'SIGHUP'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['receive', 'recover'], help='recover replays a pending update journal and installs no package.')
    parser.add_argument('--directory', default='Coconut')
    parser.add_argument('--address')
    args = parser.parse_args()
    if args.command == 'receive' and not args.address:
        parser.error('receive needs --address.')
    try:
        if args.command == 'recover':
            result = {'recovered': recover(args.directory)}
        else:
            result = receive(args.directory, args.address, sys.stdin.buffer)
    except (DeployError, KeyboardInterrupt) as error:
        if isinstance(error, KeyboardInterrupt):
            error = DeployError('The deployment was interrupted. Check host status before retrying.')
        print('COCONUT_DEPLOY_RESULT=' + json.dumps({'ok': False, 'error': str(error)}))
        raise SystemExit(1)
    print('COCONUT_DEPLOY_RESULT=' + json.dumps({'ok': True, **result}))


if __name__ == '__main__':
    main()
