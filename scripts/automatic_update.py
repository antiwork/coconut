"""Apply reviewed main commits with the installed deployment and TestFlight tools."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
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
