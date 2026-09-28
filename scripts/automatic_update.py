"""Apply reviewed main commits with the installed deployment and TestFlight tools."""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit


# After a failed install of a commit, the updater waits this long before it tries the same commit again.
FAILED_DEPLOY_SECONDS = 10 * 60
FAILED_DEPLOY_MAX_SECONDS = 60 * 60
# scripts/check_hermes_candidate.py bounds each of its programs. This budget covers all of them.
HERMES_CHECKS_SECONDS = 80 * 60
# The candidate install peaks at about 2.6 GB, and the live install then adds a runtime of about 1.3 GB.
# A full disk stops the service's database writes, so the gate installs nothing below this free space.
HERMES_CANDIDATE_FREE_BYTES = 8 * 1024 ** 3
# A failed check run costs up to 80 minutes and model calls, and a repeat usually fails the same way, because a
# merge on main rarely fixes a pin. The gate repeats the checks for a failed pin only after this wait. A new pin,
# for example a revert, runs them at once.
HERMES_FAILED_RETRY_SECONDS = 24 * 60 * 60
HERMES_INSTALL_SECONDS = 60 * 60
# scripts/hermes.py install writes these folders of .runtime, and the checks write inside hermes. The gate removes only these.
HERMES_GATE_FOLDERS = ('hermes', 'cache', 'tmp', 'tools')


def run(args, *, cwd=None, stdin=None, pass_fds=()):
    result = subprocess.run(args, cwd=cwd, stdin=stdin, capture_output=True, text=True, timeout=3600, pass_fds=pass_fds)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:] or result.stdout[-2000:] or 'Command failed')
    return result.stdout.strip()


def save(path, value):
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    os.replace(temporary, path)


def module(source, name):
    sys.path.insert(0, str(source / 'scripts'))
    spec = importlib.util.spec_from_file_location('automatic_' + name, source / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


def ios_hash(source):
    names = run(['git', 'ls-files', 'ios/project.yml', 'scripts/iphone.py',
                 'scripts/push-envelope.swift', 'ios/Coconut', 'ios/CoconutNotificationService',
                 'ios/PushShared', 'ios/Coconut.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved'], cwd=source).splitlines()
    if not names:
        raise ValueError('The checkout has no iOS release inputs.')
    paths = [source / name for name in names]
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        if path.is_symlink():
            raise ValueError('Release inputs must not be symbolic links.')
        digest.update(str(path.relative_to(source)).encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def reviewed_merge(repository, sha):
    pulls = json.loads(run(['gh', 'api', f'repos/{repository}/commits/{sha}/pulls']))
    candidates = [pr for pr in pulls if pr.get('merged_at') and pr.get('merge_commit_sha') == sha
                  and pr.get('base', {}).get('ref') == 'main']
    if len(candidates) != 1:
        raise ValueError('The main commit must belong to one merged pull request.')
    pr = json.loads(run(['gh', 'pr', 'view', str(candidates[0]['number']), '--repo', repository,
                        '--json', 'state,mergeCommit']))
    if pr['state'] != 'MERGED' or pr['mergeCommit']['oid'] != sha:
        raise ValueError('The merge changed during verification.')
    result = json.loads(run(['gh', 'api', f'repos/{repository}/commits/{sha}/check-runs?per_page=100']))
    statuses = json.loads(run(['gh', 'api', f'repos/{repository}/commits/{sha}/status?per_page=100']))
    if (not isinstance(result, dict) or not isinstance(result.get('check_runs'), list)
            or any(not isinstance(check, dict) for check in result['check_runs'])
            or type(result.get('total_count')) is not int
            or not isinstance(statuses, dict) or not isinstance(statuses.get('statuses'), list)
            or any(not isinstance(status, dict) for status in statuses['statuses'])
            or type(statuses.get('total_count', 0)) is not int):
        raise ValueError('The merged commit has malformed check evidence.')
    checks = result['check_runs']
    required = {'contracts', 'native-kanban'}
    trusted = {check.get('name') for check in checks
               if check.get('head_sha') == sha and check.get('conclusion') == 'success'
               and isinstance(check.get('name'), str)
               and isinstance(check.get('app'), dict)
               and check['app'].get('id') == 15368 and check['app'].get('slug') == 'github-actions'}
    if (result.get('total_count', len(checks)) > len(checks) or not required <= trusted
            or statuses.get('total_count', len(statuses.get('statuses', []))) > len(statuses.get('statuses', []))
            or any(check.get('head_sha') != sha for check in checks)
            or any(check.get('status') != 'completed' or check.get('conclusion') not in
                   ('success', 'neutral', 'skipped') for check in checks)
            or (statuses['statuses'] and (statuses.get('state') != 'success'
                or any(status.get('state') != 'success' for status in statuses['statuses'])))):
        raise ValueError('The merged commit still has failed or incomplete checks.')


def archive_record(path, source, fingerprint):
    """Validate the helper's receipt schema before using its build identity."""
    return validate_archive(json.loads(path.read_text()), source, fingerprint)


def validate_archive(record, source, fingerprint):
    if (not isinstance(record, dict) or record.get('source') != str(source)
            or record.get('source_hash') != fingerprint
            or type(record.get('build')) not in (str, int)
            or not re.fullmatch(r'[1-9][0-9]*', str(record['build']))
            or not isinstance(record.get('version'), str)
            or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,2}', record['version'])):
        raise ValueError('Invalid or mismatched archive record.')
    return record


def receipt_prefix(text):
    """Keep complete JSON fields preceding an interrupted receipt write."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    remaining = text.lstrip()
    if not remaining:
        return {}
    if not remaining.startswith('{'):
        raise ValueError('The damaged receipt is not a JSON object.')
    remaining = remaining[1:].lstrip()
    decoder, record = json.JSONDecoder(), {}
    while remaining:
        try:
            key, end = decoder.raw_decode(remaining)
        except ValueError:
            break
        if not isinstance(key, str) or key in record:
            raise ValueError('The damaged receipt has invalid or duplicate fields.')
        remaining = remaining[end:].lstrip()
        if not remaining:
            break
        if not remaining.startswith(':'):
            raise ValueError('The damaged receipt has an invalid field separator.')
        remaining = remaining[1:].lstrip()
        try:
            value, end = decoder.raw_decode(remaining)
        except ValueError:
            break
        record[key] = value
        remaining = remaining[end:].lstrip()
        if not remaining:
            break
        if not remaining.startswith(','):
            raise ValueError('The damaged receipt has an invalid record separator.')
        remaining = remaining[1:].lstrip()
    return record


def archive_identity(path):
    """Distinguish a prior same-source record from a new helper publication."""
    try:
        with path.open('rb') as stream:
            contents = stream.read()
            stat = os.fstat(stream.fileno())
        return dict(device=stat.st_dev, inode=stat.st_ino, modified=stat.st_mtime_ns,
                    changed=stat.st_ctime_ns, sha256=hashlib.sha256(contents).hexdigest())
    except FileNotFoundError:
        return None


def recover_archive(pending, helper, source, state, state_path):
    receipt = Path(pending['receipt'])
    try:
        if pending.get('recovered_receipt'):
            # Persist the selected identity before publishing the snapshot. An
            # interrupted save must not select a later helper build on retry.
            recovered = Path(pending['recovered_receipt'])
            if 'archive_recovery' in pending:
                record = validate_archive(pending['archive_recovery'], source, pending['hash'])
                if not recovered.exists():
                    save(recovered, record)
                if archive_record(recovered, source, pending['hash']) != record:
                    raise ValueError('The saved recovery snapshot conflicts with its selected identity.')
                return record
            return archive_record(recovered, source, pending['hash'])
        try:
            return archive_record(receipt, source, pending['hash'])
        except (ValueError, OSError):
            pass
        attempt = pending.get('archive_attempt')
        if (not isinstance(attempt, dict) or 'previous' not in attempt
                or attempt.get('helper') != str(helper.resolve())):
            raise ValueError('No archive attempt provenance; reconcile this legacy receipt explicitly.')
        current = archive_identity(helper.parent / 'archive.json')
        if current is None or current == attempt['previous']:
            if not receipt.exists():
                # The helper unlinks its old record before building, and only
                # creates the receipt after publishing the new archive record.
                # No new completed record exists. Retrying archive cannot upload.
                return None
            raise ValueError('The receipt has no new helper archive record.')
        authoritative = archive_record(helper.parent / 'archive.json', source, pending['hash'])
        try:
            partial = receipt_prefix(receipt.read_text())
        except FileNotFoundError:
            partial = {}
        if not isinstance(partial, dict) or any(
                key in partial and str(partial[key]) != str(authoritative[key])
                for key in ('source', 'source_hash', 'build', 'version')):
            raise ValueError('The receipt conflicts with the helper archive record.')
        recovered = state_path.parent / ('archive-recovered-' + uuid.uuid4().hex + '.json')
        pending['archive_recovery'] = authoritative
        pending['recovered_receipt'] = str(recovered)
        save(state_path, state)
        save(recovered, authoritative)
        return authoritative
    except (ValueError, OSError) as error:
        raise ValueError(f'Cannot recover archive receipt {receipt}: {error} '
                         'Preserve the receipts and reconcile the helper archive.json before retrying; '
                         'no archive was rebuilt or uploaded.') from error


def prepare_archive(pending, helper, source, state, state_path):
    # Share the private helper's exact lock, including with an orphaned child.
    # Its callable main runs under this inherited lock instead of attempting a
    # second flock through the script guard. No helper implementation is changed.
    with (helper.parent / 'release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=source):
            raise ValueError('The release checkout has local changes.')
        run(['git', 'fetch', 'origin', 'main'], cwd=source)
        run(['git', 'checkout', '--detach', pending['source_sha']], cwd=source)
        if run(['git', 'rev-parse', 'HEAD'], cwd=source) != pending['source_sha']:
            raise ValueError('The release checkout does not match the requested source commit.')
        tracked = set(run(['git', 'ls-files'], cwd=source).splitlines())
        for directory in ('ios/Coconut', 'ios/CoconutNotificationService', 'ios/PushShared'):
            if any(path.is_file() and path.relative_to(source).as_posix() not in tracked
                   for path in (source / directory).rglob('*')):
                raise ValueError('The release includes files outside the merged commit.')
        if ios_hash(source) != pending['hash']:
            raise ValueError('The release inputs changed.')
        if not pending.get('receipt'):
            pending['receipt'] = str(state_path.parent / ('archive-' + uuid.uuid4().hex + '.json'))
            pending['archive_attempt'] = dict(helper=str(helper.resolve()),
                                             previous=archive_identity(helper.parent / 'archive.json'))
            save(state_path, state)
        archive = recover_archive(pending, helper, source, state, state_path)
        if archive is None:
            bootstrap = ("import os, runpy, sys; os.umask(0o077); sys.argv = sys.argv[1:]; "
                         "runpy.run_path(sys.argv[0])['main']()")
            run([sys.executable, '-c', bootstrap, str(helper), 'archive', '--source', str(source),
                 '--receipt', pending['receipt']], pass_fds=(lock.fileno(),))
            archive = recover_archive(pending, helper, source, state, state_path)
            if archive is None:
                raise ValueError('The archive command did not publish a new archive record.')
        pending['build'] = int(archive['build'])
        pending['version'] = archive['version']
        save(state_path, state)


def release_ios(config, state, state_path):
    pending = state['pending_ios']
    helper = Path(config['release_helper']).resolve()
    source = Path(config['release_source']).resolve()
    command = [sys.executable, str(helper)]
    if 'build' not in pending:
        prepare_archive(pending, helper, source, state, state_path)
    # The helper refuses duplicate uploads and checks Apple before any retry.
    status = subprocess.run(command + ['status', '--build', str(pending['build'])], capture_output=True, text=True, timeout=120)
    if status.returncode:
        if status.stdout.lstrip().startswith('{'):
            report = json.loads(status.stdout)
            if report.get('processing') in ('FAILED', 'INVALID'):
                state['rejected_ios_hash'] = pending['hash']
                state['rejected_ios_build'] = pending['build']
                del state['pending_ios']
                save(state_path, state)
                raise RuntimeError('Apple rejected this build. A corrected iOS commit is required.')
            return
        if 'Build is not listed yet.' not in status.stdout:
            raise RuntimeError(status.stderr[-2000:] or 'Cannot read the build status.')
        run(command + ['upload', '--source', str(source), '--expected-build', str(pending['build'])])
        return
    state.update(ios_hash=pending['hash'], ios_build=pending['build'])
    del state['pending_ios']
    save(state_path, state)


# The launch agent that runs the installed service. scripts/host.py installs it.
SERVICE_LABEL = 'local.coconut.agent'


def service_supervised(live):
    """Return True when the service keeps running without the updater.

    launchd restarts it when its launch agent is loaded in the user's GUI domain, as scripts/host.py installs it.
    A service in manual mode runs without the agent, so an answering port also counts.
    """
    try:
        if subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{SERVICE_LABEL}'],
                          capture_output=True, timeout=10).returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        return True
    try:
        port = urlsplit(json.loads((live / '.runtime/host/connection.json').read_text())['address']).port or 443
        with socket.create_connection(('127.0.0.1', port), timeout=3):
            return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def check_hermes_candidate(source, live, revision, sha, state, state_path):
    """Run the native upgrade checks on a new Hermes pin before the live install. Raise when a check fails.

    The checks need model access, so CI cannot run them, and a bot cannot run them inside its Hermes worker.
    This updater runs outside Hermes. It installs the pin in its own checkout, runs
    scripts/check_hermes_candidate.py, and keeps its result in hermes-checks.json for Coconut Care.
    The checks also run the Coconut code of the checkout, so a pass counts only for its revision and source commit.
    A failure stops the checks for that revision for HERMES_FAILED_RETRY_SECONDS, at any commit.
    """
    receipt = state_path.parent / 'hermes-checks.json'
    saved = state.get('hermes_checks') if isinstance(state.get('hermes_checks'), dict) else {}
    if saved.get('revision') == revision:
        if saved.get('passed') is True and saved.get('sha') == sha:
            return
        checked_at = saved.get('checked_at')
        if (saved.get('passed') is False and isinstance(checked_at, (int, float))
                and time.time() - checked_at < HERMES_FAILED_RETRY_SECONDS):
            raise RuntimeError(f'The native upgrade checks failed for Hermes {revision[:12]}. Read {receipt}. '
                               'They run again after 24 hours, or at once for a new pin.')
    # Each run removes its folders at the end. A run that was stopped part way left them. Free that space before
    # the space check. hermes.py also refuses a runtime folder with another commit.
    clear_gate_folders(source)
    free = shutil.disk_usage(source).free
    if free < HERMES_CANDIDATE_FREE_BYTES:
        output = (f'The candidate install needs {HERMES_CANDIDATE_FREE_BYTES // 1024 ** 3} GiB of free disk space, and '
                  f'{free / 1024 ** 3:.1f} GiB is free. Nothing was installed. The next pass after the backoff checks again.')
        save(receipt, dict(revision=revision, passed=False, checked_at=time.time(), output=output))
        raise RuntimeError(output)
    passed, output, recorded = False, '', False
    # scripts/check_hermes_candidate.py writes its own result here. The gate writes one when the script wrote none.
    receipt.unlink(missing_ok=True)
    try:
        code, text = run_bounded([sys.executable, str(source / 'scripts/hermes.py'), 'install', '--revision', revision],
                                 cwd=source, timeout=HERMES_INSTALL_SECONDS)
        if code != 0:
            output = 'The candidate install failed before any check ran: ' + (
                text if code is not None else f'It ran longer than {HERMES_INSTALL_SECONDS} seconds.')
            raise RuntimeError(output)
        code, output = run_bounded([sys.executable, str(source / 'scripts/check_hermes_candidate.py'),
                                    '--runtime', str(source / '.runtime/hermes'), '--host-data', str(live / '.runtime/host'),
                                    '--output', str(receipt)], cwd=source, timeout=HERMES_CHECKS_SECONDS)
        if code is None:
            output += f'\nThe checks ran longer than {HERMES_CHECKS_SECONDS} seconds.'
        with contextlib.suppress(OSError, ValueError, AttributeError):
            result = json.loads(receipt.read_text())
            passed = code == 0 and result.get('passed') is True and result.get('hermes_sha') == revision
            recorded = True
    except BaseException as error:
        # The receipt is what Coconut Care reads. Name an unexpected error there too.
        output = output or f'The gate stopped: {type(error).__name__}: {str(error)[-5000:]}'
        raise
    finally:
        if not recorded:
            save(receipt, dict(revision=revision, passed=False, checked_at=time.time(), output=output[-20000:]))
        # The live install builds its own runtime. Free the candidate and the install caches.
        clear_gate_folders(source)
    state['hermes_checks'] = dict(revision=revision, sha=sha, passed=passed, checked_at=time.time())
    save(state_path, state)
    if not passed:
        raise RuntimeError(f'The native upgrade checks failed for Hermes {revision[:12]}. Read {receipt}.')


def clear_gate_folders(source):
    """Remove what the Hermes gate wrote in .runtime of the updater checkout. Other data there stays."""
    for name in HERMES_GATE_FOLDERS:
        shutil.rmtree(source / '.runtime' / name, ignore_errors=True)


def run_bounded(command, *, cwd, timeout):
    """Run a command in its own session. Return its exit code, or None after a timeout, and its output.

    The output goes to a file, not a pipe, so a process that outlives the command cannot block the updater.
    After the command, Coconut stops its whole process group, for example uv and git below an install.
    """
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code = None
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        output.seek(0)
        return code, output.read()[-20000:].decode(errors='replace')


def recover_journal(source, live):
    """Let the installer finish or roll back a pending update. It installs no package.

    A failed update can leave the service stopped. deploy.py receive recovers the journal first, but the Hermes
    candidate checks run before it and can take more than an hour, so this recovery runs before them.
    """
    if (live / '.runtime/deploy/update.json').exists():
        run([sys.executable, str(source / 'scripts/deploy.py'), 'recover', '--directory', str(live.relative_to(Path.home()))])


def update(config, state_path):
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    processed_build = state.get('pending_ios', {}).get('build')
    if processed_build:
        try:
            release_ios(config, state, state_path)
        except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
    source = Path(config['source']).resolve()
    live = Path(config['live']).resolve()
    if source == live or live in source.parents or source in live.parents:
        raise ValueError('Use a separate checkout outside the running installation.')
    origin = run(['git', 'remote', 'get-url', 'origin'], cwd=source)
    repository = config['repository']
    if origin not in (f'https://github.com/{repository}.git', f'https://github.com/{repository}', f'git@github.com:{repository}.git'):
        raise ValueError('The checkout remote differs from the configured repository.')
    if run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=source):
        raise ValueError('The automatic checkout has local changes.')
    run(['git', 'fetch', 'origin', 'main'], cwd=source)
    sha = run(['git', 'rev-parse', 'origin/main'], cwd=source)
    failed = state.get('failed_deploy') or {}
    if failed.get('source_sha') == sha and time.time() < failed.get('retry_at', 0) and service_supervised(live):
        # Each failed attempt stops and restarts the service. Wait before the next install of the same commit.
        # launchd keeps the restored service running meanwhile. When a failed rollback left the launch agent
        # unloaded, nothing restarts the service, so the install runs now: it recovers from the journal first.
        # A TestFlight archive uses its own checkout, so it does not wait for the backoff.
        if state.get('pending_ios') and not processed_build and config.get('release_helper'):
            try:
                release_ios(config, state, state_path)
            except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as error:
                print(str(error), file=sys.stderr)
        return
    reviewed_merge(repository, sha)
    run(['git', 'checkout', '--detach', sha], cwd=source)
    packaging = module(source, 'remote')
    package = state_path.parent / 'automatic-package.tar.gz'
    tracked = set(run(['git', 'ls-files'], cwd=source).splitlines())
    if set(packaging.source_files()) - tracked:
        raise ValueError('The deployment includes files outside the merged commit.')
    metadata = packaging.package(package)
    marker_path = live / '.coconut-install.json'
    marker = json.loads(marker_path.read_text())
    expected = module(source, 'deploy')
    with package.open('rb') as stream:
        validated = expected.validate_package(stream)
    matches = marker.get('state') == 'installed' and marker.get('files') == validated.hashes
    matches = matches and all((live / name).is_file() and hashlib.sha256((live / name).read_bytes()).hexdigest() == digest
                              for name, digest in validated.hashes.items())
    runtime_path = live / '.runtime/hermes/install.json'
    try:
        runtime = json.loads(runtime_path.read_text())
    except (ValueError, OSError):
        runtime = {}
    if not isinstance(runtime, dict):
        runtime = {}
    matches = matches and runtime.get('commit') == validated.revision
    if not matches:
        address = json.loads((live / '.runtime/host/connection.json').read_text())['address']
        arguments = [sys.executable, str(source / 'scripts/deploy.py'), 'receive',
                     '--directory', str(live.relative_to(Path.home())), '--address', address]
        if runtime.get('commit') != validated.revision:
            arguments.append('--update-runtime')
    try:
        if not matches:
            if '--update-runtime' in arguments:
                recover_journal(source, live)
                check_hermes_candidate(source, live, validated.revision, sha, state, state_path)
            with package.open('rb') as stream:
                output = run(arguments, stdin=stream)
            save(state_path.parent / 'deployment-result.json', dict(source_sha=sha, output=output))
            result = [json.loads(line.split('=', 1)[1]) for line in output.splitlines() if line.startswith('COCONUT_DEPLOY_RESULT=')]
            if len(result) != 1 or not result[0].get('ok'):
                raise RuntimeError('The installer did not confirm a successful deployment.')
        address = json.loads((live / '.runtime/host/connection.json').read_text())['address']
        expected.Operations().health(live, address)
        marker = json.loads(marker_path.read_text())
        runtime = json.loads(runtime_path.read_text())
        if not isinstance(runtime, dict) or runtime.get('commit') != validated.revision:
            raise RuntimeError('The installed runtime does not match the merged commit.')
        if marker.get('state') != 'installed' or marker.get('files') != validated.hashes or any(
                hashlib.sha256((live / name).read_bytes()).hexdigest() != digest
                for name, digest in validated.hashes.items()):
            raise RuntimeError('The installed files do not match the merged commit.')
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError, getattr(expected, 'DeployError', RuntimeError)):
        # A failed install or check restores the previous source or leaves the next pass to install again. Each
        # install stops and restarts the service, so back off for this commit: 10 minutes, then double up to
        # 1 hour. A new merged commit deploys at once.
        attempts = failed.get('attempts', 0) + 1 if failed.get('source_sha') == sha else 1
        state['failed_deploy'] = dict(source_sha=sha, attempts=attempts,
                                      retry_at=time.time() + min(FAILED_DEPLOY_MAX_SECONDS, FAILED_DEPLOY_SECONDS * 2 ** (attempts - 1)))
        save(state_path, state)
        raise
    state.pop('failed_deploy', None)
    state.update(source_sha=sha, package_id=metadata['package_id'])
    # Save the install now, so a later iOS step that fails cannot leave an old backoff in place.
    save(state_path, state)
    fingerprint = ios_hash(source)
    if config.get('release_helper') and not state.get('pending_ios', {}).get('build'):
        if fingerprint in (state.get('ios_hash'), state.get('rejected_ios_hash')):
            state.pop('pending_ios', None)
        elif state.get('pending_ios', {}).get('hash') != fingerprint:
            state['pending_ios'] = dict(hash=fingerprint, source_sha=sha)
    save(state_path, state)
    if state.get('pending_ios') and (not processed_build or state['pending_ios'].get('build') != processed_build):
        release_ios(config, state, state_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    os.environ['PATH'] = '/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    if config.get('gh_config_dir'):
        os.environ['GH_CONFIG_DIR'] = config['gh_config_dir']
    state = args.config.parent / 'automatic-state.json'
    with (args.config.parent / 'automatic.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        update(config, state)


if __name__ == '__main__':
    os.umask(0o077)
    main()
