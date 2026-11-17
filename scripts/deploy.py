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
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from uuid import UUID, uuid4

MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 4096
REQUIRED_FILES = {'backend/coconut/__init__.py', 'backend/coconut/__main__.py', 'scripts/host.py'}
MARKER_PATH = Path('.coconut-install.json')
# "app": the login item of Coconut.app runs the service (host.inspect_for_update).
SERVICE_MODES = ('managed', 'manual', 'stopped', 'app')
# Coconut.app sets this variable when it runs the update. A first install then starts the app's login item and writes no
# LaunchAgent.
APP_MANAGED = 'COCONUT_APP_MANAGED'
APP_SERVICE_LABEL = 'com.gumroad.coconut.service'
# Coconut.app sets COCONUT_MODE=team for a Mac that runs Coconut for a team only (backend/coconut/team_only.py). Such an
# install and each of its updates run no step that finds, checks or changes Hermes, build no notification helper and
# start no relay sidecar. Its service has no tunnel listener, so the health check asks the main listener.
MODE_ENVIRONMENT = 'COCONUT_MODE'
TEAM_MODE = 'team'
# The service answers its health check after startup finishes.
HEALTH_WAIT_SECONDS = 300
# A probe whose failure is expected, such as the check for an already restored service, waits only this long.
# This is the earlier wait. With the faster start migrations, a restored service answers well within it.
PROBE_WAIT_SECONDS = 60
# The update waits this long for running turns to finish (host.DRAIN_SECONDS) and a little more for the helper.
DRAIN_WAIT_SECONDS = 600 + 90
# The person's Hermes loads the skills in skills/ (see scripts/hermes_skills.py). Hermes writes a skill file as a new file
# in the same folder, which it then renames, so only read-only folders keep `skill_manage` from changing them. An update
# opens the folders for its own changes, then closes them again.
SKILL_FILE_MODE = 0o400
SKILL_FOLDER_MODE = 0o500
# host.stop_for_update waits up to host.STOP_GRACE_SECONDS for a clean exit of the service. Then it sends SIGKILL and
# stops the child processes. The helper gets this long for all of it.
STOP_HELPER_SECONDS = 300
# deploy.py adds the full output of each run to this file in the data folder, with the traceback of a failure. A file
# that grows past LOG_LIMIT_BYTES moves to LOG_PATH.1, so at most two files remain.
LOG_PATH = Path('.runtime/host/logs/install-source.log')
LOG_LIMIT_BYTES = 1024 * 1024
# The log keeps at most this many characters of each output of one command.
LOG_OUTPUT_CHARACTERS = 64 * 1024
# The lines of the current run. main() adds them to LOG_PATH.
TRANSCRIPT = []


class DeployError(Exception):
    pass


@dataclass
class Package:
    files: dict[str, bytes]
    hashes: dict[str, str]
    package_id: str


def log(text):
    TRANSCRIPT.append(f'{datetime.now(timezone.utc).isoformat(timespec="seconds")} {text}')


def output_tail(data):
    text = data.decode('utf-8', errors='replace')
    return text if len(text) <= LOG_OUTPUT_CHARACTERS else '[...]' + text[-LOG_OUTPUT_CHARACTERS:]


def log_output(phase, output, errors):
    for name, data in (('output', output), ('error output', errors)):
        if data:
            log(f'{phase} {name}:\n{output_tail(data)}')


def failure_reason(errors):
    """Return the last line of an error output: the message of a SystemExit, or the exception line of a traceback."""
    lines = [line.strip() for line in (errors or b'').decode('utf-8', errors='replace').splitlines() if line.strip()]
    return lines[-1][:500] if lines else ''


def write_log(root):
    """Add the lines of this run to LOG_PATH in the install. Return the path, or None when there is no data folder.

    A file larger than LOG_LIMIT_BYTES first moves to LOG_PATH.1. A failed first install has no data folder and gets no log.
    """
    lines = TRANSCRIPT[:]
    TRANSCRIPT.clear()
    if not lines:
        return None
    try:
        if not safe_path(root / '.runtime/host', root).is_dir():
            return None
        path = safe_path(root / LOG_PATH, root)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        safe_path(path, root)
        if path.is_file() and path.stat().st_size > LOG_LIMIT_BYTES:
            os.replace(path, path.with_name(path.name + '.1'))
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a', encoding='utf-8') as handle:
            handle.write('\n'.join(lines) + '\n\n')
        return path
    except (OSError, DeployError):
        return None


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
        or re.fullmatch(r'skills/(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_.-]+\.md', name)
        # The `coconut` gateway platform that the person's Hermes loads. See backend/coconut/gateway_plugin.py.
        or re.fullmatch(r'hermes_plugin/coconut/(?:[A-Za-z_][A-Za-z0-9_]*\.py|plugin\.yaml)', name)
    )


# Paths that earlier packages shipped and current packages no longer do. A new package may not contain them, but the
# marker of an earlier install still lists them, so an update reads that marker and removes the files.
RETIRED_PATHS = frozenset({
    # The sprite sheet of the fruit faces of agents, which later had letters and now have no face.
    'backend/coconut/assets/agent-sprites.png',
})


def owned_path(name):
    """Whether a marker or journal may list this path: a current package path or a retired one."""
    return allowed_path(name) or (isinstance(name, str) and name in RETIRED_PATHS)


def file_mode(name):
    if name.endswith('.command'):
        return 0o700
    return SKILL_FILE_MODE if name.startswith('skills/') else 0o600


def lock_skills(root, locked=True):
    """Make the folders in skills/ read-only (locked) or writable again. See SKILL_FOLDER_MODE."""
    top = root / 'skills'
    if top.is_symlink() or not top.is_dir():
        return
    # os.walk does not follow symbolic links. A read-only folder can still be listed.
    for folder, _, _ in os.walk(top):
        if not Path(folder).is_symlink():
            os.chmod(folder, SKILL_FOLDER_MODE if locked else 0o700)


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
        if any(not owned_path(name) or not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for name, value in marker['files'].items()):
            raise ValueError()
        if marker['state'] not in ('installed', 'pending', 'failed'):
            raise ValueError()
        if marker['files'] and marker.get('package_id') != sha256(canonical(marker['files'])):
            raise ValueError()
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


def team_folder_refusal(root, package):
    """The refusal when the data folder of the install belongs to a team-only service, else None. A full install or
    update must not touch the Hermes of that Mac.

    The check is service_base.team_folder of the package, the one helper of this check. deploy.py runs from the app
    bundle without the source next to it, so it loads that module from the checked package. A folder without a store
    needs no module: a first install has no data yet.
    """
    if not safe_path(root / '.runtime/host/coconut.sqlite3', root).is_file():
        return None
    source = package.files.get('backend/coconut/service_base.py')
    if source is None:
        # Each package of scripts/remote.py has the module. Only the small packages of the tests lack it.
        return None
    module = type(sys)('coconut_deploy_service_base')
    exec(compile(source, 'backend/coconut/service_base.py', 'exec'), module.__dict__)
    try:
        return module.HERMES_REFUSAL if module.team_folder(root / '.runtime/host') else None
    except Exception as error:
        # A store that cannot be read cannot show that this Mac is a full install, so no Hermes step runs.
        raise DeployError(f'Coconut could not read the data folder, so it changed nothing: {error}') from None


def load_host(root):
    source = safe_path(root / 'scripts/host.py', root)
    spec = importlib.util.spec_from_file_location('coconut_deploy_host_' + uuid4().hex, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def team_only_mode(environ=None):
    """True when Coconut.app installs or updates a Mac that runs Coconut for a team only."""
    return (os.environ if environ is None else environ).get(MODE_ENVIRONMENT) == TEAM_MODE


class Operations:
    # A team-only install (see MODE_ENVIRONMENT). receive() gives it to the Operations that it makes.
    team_only = False

    def __init__(self, team_only=False):
        self.team_only = team_only

    @staticmethod
    def terminate_group(process):
        """Stop the process group of the command. Return the output that the command wrote and nobody read yet."""
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
            return process.communicate(timeout=3)
        except (subprocess.TimeoutExpired, ValueError):
            # ValueError: the pipes were closed already.
            process.kill()
            process.wait()
        return None, None

    def command(self, command, root, phase, *, input_data=None, timeout=1200, explain=False):
        """Run a command and return its standard output. LOG_PATH gets both outputs.

        A failure names the phase and the exit code. With explain, it also gives the last line of the error output. Only
        Coconut's own host helper uses explain: its error output is a message of host.py.
        """
        started = time.monotonic()
        log(f'{phase}: start')
        try:
            process = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as error:
            log(f'{phase}: could not start: {error}')
            raise DeployError(phase + ' could not start. No command output was exposed.') from None
        try:
            output, errors = process.communicate(input=input_data, timeout=timeout)
        except BaseException as error:
            # communicate keeps what it read before the timeout. The next communicate returns it with the rest.
            output, errors = self.terminate_group(process)
            log(f'{phase}: {type(error).__name__} after {time.monotonic() - started:.1f}s. Its process group was stopped.')
            log_output(phase, output, errors)
            if isinstance(error, subprocess.TimeoutExpired):
                reason = failure_reason(errors) if explain else ''
                raise DeployError(phase + f' timed out after {timeout} seconds. Its process group was stopped.'
                                  + (f' Its last message: {reason}' if reason else '')) from None
            raise
        log(f'{phase}: exit {process.returncode} after {time.monotonic() - started:.1f}s')
        log_output(phase, output, errors)
        if process.returncode:
            self.terminate_group(process)
            reason = failure_reason(errors) if explain else ''
            if reason:
                raise DeployError(phase + f' failed (exit {process.returncode}): {reason}')
            raise DeployError(phase + f' failed (exit {process.returncode}). Its output is in {LOG_PATH}.')
        return output

    def preflight(self, root, package):
        if platform.system() != 'Darwin':
            raise DeployError('The execution host must run macOS.')
        if self.team_only:
            # Nothing compiles here: the app carries its helpers. Without the Command Line Tools, the Git command opens
            # an installation dialog that nobody sees on a Mac without a person.
            return
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
if sys.argv[2]=='inspect': result=module.inspect_for_update()
elif sys.argv[2]=='stop': result=module.stop_for_update()
elif sys.argv[2]=='drain': result={'drained': bool(module.drain_for_update())}
else: result=module.restore_after_update(state)
print('COCONUT_HOST_STATE='+json.dumps(result))
'''
        command = [sys.executable, '-c', program, str(root), operation]
        if source is not None:
            command.append(str(source))
        output = self.command(command, root, 'Service ' + operation, input_data=canonical(state), timeout=timeout,
                              explain=True)
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

    # With `source`, the helper runs that host.py in place of the installed one. An update gives it the host.py of the
    # new package, so the stop of the new code stops the old service. The installed host.py can have an older stop.
    def inspect(self, root, source=None):
        return self.helper(root, 'inspect', source=source)

    def drain(self, root, source=None):
        """Let the running turns of the service finish before the update stops it. See host.drain_for_update."""
        return self.helper(root, 'drain', source=source, timeout=DRAIN_WAIT_SECONDS)

    def stop(self, root, source=None):
        return self.helper(root, 'stop', source=source, timeout=STOP_HELPER_SECONDS)

    def stop_recovery(self, root, source):
        return self.stop(root, source)

    def restore(self, root, state):
        self.helper(root, 'restore', state)

    def prepare(self, root, package):
        if not self.team_only:
            # A team-only Mac has no phone, so it sends no notification.
            self.command([sys.executable, str(root / 'scripts/push.py'), '--rebuild'], root, 'Notification encryption build')
        # The helper of the team log (docs/team-log.md). Under Coconut.app nothing compiles: the app carries it.
        self.command([sys.executable, str(root / 'scripts/team.py'), 'build'], root, 'Team helper build')

    def tunnel(self, root):
        """Give the tunnel sidecar the new code, and install it on a Mac that never had it. See tunnel.rebuild.

        This runs after the update is complete, so a rollback never leaves a newer sidecar running. A failure here
        keeps the update and goes into the result.
        """
        try:
            self.command([sys.executable, str(root / 'scripts/tunnel.py'), 'rebuild'], root, 'Tunnel sidecar build')
        except DeployError as error:
            return {'ok': False, 'error': str(error)}
        return {'ok': True}

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

    def install(self, root):
        """Start the service of a first install. Return its mode."""
        if os.environ.get(APP_MANAGED) == '1':
            # The app registered its login item before the install. Its tool waits for the source, so start it again now.
            self.command(['/bin/launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/{APP_SERVICE_LABEL}'], root,
                         'Login item start', timeout=30)
            return 'app'
        plist = Path.home() / 'Library/LaunchAgents/local.coconut.agent.plist'
        if plist.exists() or plist.is_symlink():
            self.restore(root, {'mode': 'managed', 'project': str(root), 'data_dir': str(root / '.runtime/host')})
            return 'managed'
        self.command([sys.executable, str(root / 'scripts/host.py'), 'install-service'], root, 'Login service installation', timeout=60)
        return 'managed'

    def skills(self, root):
        """Set Coconut's own entries in the person's Hermes profiles again: the skills folder and the `coconut` gateway
        platform. See scripts/hermes_skills.py.

        Each install does this, a first install and an update. Only Coconut's entries change. A failure keeps the install
        and goes into the result, as for the tunnel.
        """
        try:
            self.command([sys.executable, str(root / 'scripts/hermes_skills.py'), 'add'], root, 'Hermes skill setup',
                         timeout=300)
        except DeployError as error:
            return {'ok': False, 'error': str(error)}
        return {'ok': True}

    def health(self, root):
        """Wait until the service answers its health check."""
        return self._health(root, HEALTH_WAIT_SECONDS)

    def probe(self, root):
        """Check health briefly, for a check whose failure is expected. This keeps the earlier 60-second wait."""
        return self._health(root, PROBE_WAIT_SECONDS)

    def _health(self, root, seconds):
        """Check /health on the tunnel listener, which carries the phone's requests.

        The service answers there with plain HTTP on loopback. A team-only service has no tunnel listener, so its check
        asks the main listener.
        """
        try:
            host = load_host(root)
            port = host.PORT if self.team_only else host.TUNNEL_PORT
        except (OSError, AttributeError, SyntaxError, DeployError):
            raise DeployError('The installed host helper does not name the tunnel listener.') from None
        # Startup migrations can take minutes on an active host with a large database. A deadline that is
        # too short fails a healthy update, rolls it back, and restarts the service again.
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            try:
                connection.request('GET', '/health')
                response = connection.getresponse()
                data = response.read(65537)
                if len(data) > 65536:
                    raise DeployError('The service returned an oversized health response.')
                result = json.loads(data)
                if response.status == 200 and result == {'ok': True, 'api_version': 1}:
                    return {'ok': True, 'api_version': 1, 'scope': 'loopback'}
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(.25)
        raise DeployError('The service did not pass its loopback health check.')


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


def reason(error):
    """The text of an error and of the errors that caused it, for a result that names the actual reason."""
    parts = []
    while error is not None and len(parts) < 4:
        text = str(error) if isinstance(error, (DeployError, SystemExit)) else f'{type(error).__name__}: {error}'
        if text and text not in parts:
            parts.append(text)
        error = error.__cause__
    return ' Reason: '.join(parts)


def reconcile(root, operations, helper=None):
    """Finish or roll back the update of the journal. `helper` is a host.py that stops the service in place of the
    installed one (Operations.stop)."""
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
                or record['service_state'].get('mode') not in SERVICE_MODES):
            raise ValueError()
        for names in (old['files'], record['new_files']):
            if any(not owned_path(name) or not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for name, value in names.items()):
                raise ValueError()
        staging, backup = journal_paths(root, record)
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
                operations.probe(root)
            else:
                operations.assert_stopped(root)
        except DeployError:
            pass
        else:
            remove_journal(root, record)
            return
    try:
        if (root / 'scripts/host.py').is_file():
            if helper is not None:
                operations.stop(root, helper)
            else:
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
        raise DeployError('The incomplete update could not stop its service. Its journal, source, and runtime remain '
                          'intact. Reason: ' + reason(error)) from error
    record['phase'] = 'rollback_stopped'
    write_json(journal, record)
    lock_skills(root, locked=False)
    for name in set(record['new_files']) - set(old['files']):
        path = safe_path(root / name, root)
        if path.exists():
            path.unlink()
            sync_directory(path.parent)
    for name in old['files']:
        copy_source(backup / name, safe_path(root / name, root))
    with contextlib.suppress(OSError):
        lock_skills(root)
    write_json(root / MARKER_PATH, old)
    record['phase'] = 'code_restored'
    write_json(journal, record)
    try:
        if old['files']:
            operations.restore(root, record['service_state'])
            if record['service_state']['mode'] != 'stopped':
                operations.health(root)
    except BaseException as error:
        raise DeployError('Previous source was restored, but its service could not restart. Runtime data was not '
                          'rewound. Reason: ' + reason(error)) from error
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
    mode, and the phone shows the reason. A service that saved no status counts as one that worked.
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


def write_helper(deployment, package):
    """Write the host.py of the package for Operations.stop, and return its path. See reconcile and receive."""
    path = deployment / 'host-helper.py'
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(package.files['scripts/host.py'])
    return path


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
        reconcile(root, operations or Operations(team_only=team_only_mode()))
    return True


def receive(directory, stream, *, home=None, operations=None, team_only=None):
    """Install or update the package. `team_only` defaults to the mode that Coconut.app gives (team_only_mode)."""
    os.umask(0o077)
    team_only = team_only_mode() if team_only is None else team_only
    home = Path(home or Path.home()).resolve()
    root = destination(directory, home)
    package = validate_package(stream)
    refusal = None if team_only else team_folder_refusal(root, package)
    if refusal:
        raise DeployError(refusal)
    marker = read_marker(root)
    runtime_receipt, runtime_present = check_runtime_receipt(root, marker)
    operations = operations or Operations(team_only=team_only)
    operations.preflight(root, package)
    if marker is None and root.exists() and any(root.iterdir()):
        raise DeployError('This destination is no longer empty.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    deployment = safe_path(root / '.runtime/deploy', root)
    deployment.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(deployment / 'deploy.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock, contextlib.ExitStack() as cleanup:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError('Another Coconut deployment is already running.') from None
        helper = write_helper(deployment, package)
        cleanup.callback(helper.unlink, missing_ok=True)
        if marker is not None:
            if read_marker(root) != marker:
                raise DeployError('The installation changed while the package was being checked. Retry the update.')
        elif (root / MARKER_PATH).exists() or any(path.name != '.runtime' for path in root.iterdir()):
            raise DeployError('Another deployment claimed this directory. Retry the update.')
        original = marker
        marker = marker or {'version': 1, 'installation_id': str(uuid4()), 'files': {}, 'package_id': '', 'state': 'pending'}
        write_json(root / MARKER_PATH, marker)
        reconcile(root, operations, helper)
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
            target.chmod(file_mode(name))
        for name in old_names:
            copy_source(safe_path(root / name, root), backup / name)
        if not old_names:
            copy_source(staging / 'scripts/host.py', backup / 'scripts/host.py')
        # The service saved its Hermes status at its last start. Read it before the new code checks Hermes again.
        previous_hermes = None if team_only else read_hermes_status(root)
        state = operations.inspect(root, helper) if old_names else {'mode': 'stopped', 'project': str(root), 'data_dir': str(root / '.runtime/host')}
        if not isinstance(state, dict) or state.get('mode') not in SERVICE_MODES:
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
                    operations.drain(root, helper)
                stopped = operations.stop(root, helper)
                if stopped != state:
                    record['service_state'] = stopped
                    write_json(journal, record)
                    raise DeployError('The service mode changed during the update. Previous source was kept.')
            record['phase'] = 'stopped'
            write_json(journal, record)
            record['phase'] = 'applying'
            write_json(journal, record)
            applied = True
            lock_skills(root, locked=False)
            for name in package.files:
                target = safe_path(root / name, root)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging / name, target)
                sync_directory(target.parent)
            for name in old_names - set(package.files):
                safe_path(root / name, root).unlink()
                sync_directory((root / name).parent)
            operations.prepare(root, package)
            if not team_only:
                check_hermes(operations, root, previous_hermes, updating=bool(old_names))
            record['phase'] = 'starting'
            write_json(journal, record)
            if initial_install:
                mode = operations.install(root) or 'managed'
            else:
                operations.restore(root, state)
                mode = state['mode']
            health = operations.health(root) if mode != 'stopped' else {'ok': False, 'status': 'not_running'}
            updated = dict(marker, deployment_id=record['deployment_id'], files=package.hashes, package_id=package.package_id, state='installed', updated_at=datetime.now(timezone.utc).isoformat())
            updated.pop('initial_install_pending', None)
            updated.pop('recovery_state', None)
            write_json(runtime_receipt, {'version': 1, 'installation_id': marker['installation_id']})
            write_json(root / MARKER_PATH, updated)
            remove_journal(root, record)
            # A folder that stays writable only loses its protection.
            with contextlib.suppress(OSError):
                lock_skills(root)
            result = {'version': 1, 'installation_id': marker['installation_id'], 'package_id': package.package_id, 'mode': mode, 'health': health, 'updated': original is not None}
        except BaseException as error:
            if not applied:
                # A failed stop can have unloaded the job. Keep its recovery state.
                raise DeployError('Source was not replaced. The recovery journal remains; check service status before '
                                  'retrying. Reason: ' + reason(error)) from error
            try:
                reconcile(root, operations, helper)
            except DeployError as recovery_error:
                raise DeployError(str(recovery_error)) from error
            detail = str(error) if isinstance(error, DeployError) else type(error).__name__
            raise DeployError('The update failed. Previous source and mode were restored; runtime data was not rewound. ' + detail) from error
        if team_only:
            # No relay sidecar and no entry in Hermes: a team-only Mac has no phone, and Coconut never touches its Hermes.
            result['team_only'] = True
            return result
        result['tunnel'] = operations.tunnel(root)
        # Each install sets Coconut's own entries in the Hermes profiles again, so an update brings them up to date.
        result['skills'] = operations.skills(root)
        return result


def main():
    def interrupt(signum, frame):
        raise KeyboardInterrupt()
    for name in ('SIGTERM', 'SIGHUP'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['receive', 'recover'], help='recover replays a pending update journal and installs no package.')
    parser.add_argument('--directory', default='Coconut')
    args = parser.parse_args()
    log(f'deploy.py {args.command} --directory {args.directory}')
    try:
        if args.command == 'recover':
            result = {'recovered': recover(args.directory)}
        else:
            result = receive(args.directory, sys.stdin.buffer)
    except (DeployError, KeyboardInterrupt) as error:
        # The full chain of causes, with each traceback, goes only to the log.
        log('Traceback of the failure:\n' + ''.join(traceback.format_exception(error)).rstrip())
        if isinstance(error, KeyboardInterrupt):
            error = DeployError('The deployment was interrupted. Check host status before retrying.')
        line = 'COCONUT_DEPLOY_RESULT=' + json.dumps({'ok': False, 'error': str(error)})
        finish(args.directory, line)
        raise SystemExit(1)
    except BaseException as error:
        log('Traceback of the failure:\n' + ''.join(traceback.format_exception(error)).rstrip())
        finish(args.directory, None)
        raise
    finish(args.directory, 'COCONUT_DEPLOY_RESULT=' + json.dumps({'ok': True, **result}))


def finish(directory, line):
    """Print the result line, and add the run to LOG_PATH of the install."""
    if line is not None:
        log(line)
        print(line, flush=True)
    try:
        root = destination(directory, Path.home().resolve())
    except DeployError:
        TRANSCRIPT.clear()
        return
    write_log(root)


if __name__ == '__main__':
    main()
