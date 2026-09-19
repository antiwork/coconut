"""Apply reviewed main commits with the installed deployment and TestFlight tools."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


def run(args, *, cwd=None, stdin=None):
    result = subprocess.run(args, cwd=cwd, stdin=stdin, capture_output=True, text=True, timeout=3600)
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
                        '--json', 'state,mergeCommit,statusCheckRollup']))
    if pr['state'] != 'MERGED' or pr['mergeCommit']['oid'] != sha:
        raise ValueError('The merge changed during verification.')
    result = json.loads(run(['gh', 'api', f'repos/{repository}/commits/{sha}/check-runs?per_page=100']))
    checks = result['check_runs']
    statuses = json.loads(run(['gh', 'api', f'repos/{repository}/commits/{sha}/status?per_page=100']))
    if (result.get('total_count', len(checks)) > len(checks) or not checks
            or any(check.get('status') != 'completed' or check.get('conclusion') not in
                   ('success', 'neutral', 'skipped') for check in checks)
            or (statuses.get('statuses') and statuses.get('state') != 'success')):
        raise ValueError('The merged commit still has failed or incomplete checks.')


def release_ios(config, state, state_path):
    pending = state['pending_ios']
    helper = Path(config['release_helper'])
    source = Path(config['release_source'])
    command = [sys.executable, str(helper)]
    if 'build' not in pending:
        if run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=source):
            raise ValueError('The release checkout has local changes.')
        run(['git', 'fetch', 'origin', 'main'], cwd=source)
        run(['git', 'checkout', '--detach', pending['source_sha']], cwd=source)
        tracked = set(run(['git', 'ls-files'], cwd=source).splitlines())
        for directory in ('ios/Coconut', 'ios/CoconutNotificationService', 'ios/PushShared'):
            if any(path.is_file() and path.relative_to(source).as_posix() not in tracked
                   for path in (source / directory).rglob('*')):
                raise ValueError('The release includes files outside the merged commit.')
        if ios_hash(source) != pending['hash']:
            raise ValueError('The release inputs changed.')
        if not pending.get('receipt'):
            pending['receipt'] = str(state_path.parent / ('archive-' + uuid.uuid4().hex + '.json'))
            save(state_path, state)
        receipt = Path(pending['receipt'])
        if not receipt.is_file():
            run(command + ['archive', '--source', str(source), '--receipt', str(receipt)])
        archive = json.loads(receipt.read_text())
        if archive['source'] != str(source) or archive['source_hash'] != pending['hash']:
            raise ValueError('The archive belongs to another checkout.')
        pending['build'] = int(archive['build'])
        save(state_path, state)
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
    runtime = json.loads(runtime_path.read_text()) if runtime_path.is_file() else {}
    matches = matches and runtime.get('commit') == validated.revision
    if not matches:
        address = json.loads((live / '.runtime/host/connection.json').read_text())['address']
        arguments = [sys.executable, str(source / 'scripts/deploy.py'), 'receive',
                     '--directory', str(live.relative_to(Path.home())), '--address', address]
        if runtime.get('commit') != validated.revision:
            arguments.append('--update-runtime')
        with package.open('rb') as stream:
            output = run(arguments, stdin=stream)
        save(state_path.parent / 'deployment-result.json', dict(source_sha=sha, output=output))
        result = [json.loads(line.split('=', 1)[1]) for line in output.splitlines() if line.startswith('COCONUT_DEPLOY_RESULT=')]
        if len(result) != 1 or not result[0].get('ok'):
            raise RuntimeError('The installer did not confirm a successful deployment.')
    address = json.loads((live / '.runtime/host/connection.json').read_text())['address']
    expected.Operations().health(live, address)
    marker = json.loads(marker_path.read_text())
    if marker.get('files') != validated.hashes or any(
            hashlib.sha256((live / name).read_bytes()).hexdigest() != digest
            for name, digest in validated.hashes.items()):
        raise RuntimeError('The installed files do not match the merged commit.')
    state.update(source_sha=sha, package_id=metadata['package_id'])
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
