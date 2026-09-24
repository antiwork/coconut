"""Keep one native Hermes worktree per outcome and repository."""
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
from types import SimpleNamespace


def git(path, *arguments):
    result = subprocess.run(['git', '-C', str(path), *arguments], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError((result.stderr or 'Git could not inspect the workspace.').strip())
    return result.stdout.strip()


def head_log(path):
    try:
        return Path(git(path, 'rev-parse', '--git-path', 'logs/HEAD')).read_bytes()
    except FileNotFoundError:
        return b''


def stable_head(path):
    for _ in range(3):
        before = head_log(path)
        branch = git(path, 'branch', '--show-current')
        after = head_log(path)
        if before == after and git(path, 'branch', '--show-current') == branch:
            return branch, after
    raise ValueError('The worktree changed during inspection. Try again after Git finishes.')


def remember_head(binding, log):
    binding['_head_log'] = dict(size=len(log), digest=hashlib.sha256(log).hexdigest())


def validate_binding(binding, *, allow_rename=False):
    path = Path(binding['path'])
    repository = Path(binding['repository']).resolve()
    if not path.is_dir():
        raise ValueError('The saved worktree is missing. Inspect its Git record before creating another checkout.')
    if path.resolve() == repository or not (path / '.git').is_file():
        raise ValueError('The saved workspace is no longer a linked worktree.')
    if Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != repository / '.git':
        raise ValueError('The saved worktree belongs to another repository.')
    current, log = stable_head(path)
    if current != binding['branch']:
        checkpoint = binding.get('_head_log', {})
        size = checkpoint.get('size', 0)
        continuous = (size > 0 and len(log) >= size
            and hashlib.sha256(log[:size]).hexdigest() == checkpoint.get('digest'))
        branch = binding['branch']
        for line in log[size:].splitlines() if continuous else []:
            message = line.partition(b'\t')[2].decode('utf-8', errors='replace')
            if message.startswith('checkout:'):
                continuous = False
                break
            if message.startswith('Branch: renamed '):
                if not line.split(b' ', 2)[1].strip(b'0'):
                    continue
                prefix = 'Branch: renamed refs/heads/' + branch + ' to refs/heads/'
                if not message.startswith(prefix):
                    continuous = False
                    break
                branch = message[len(prefix):]
        renamed = allow_rename and current and continuous and branch == current
        if not renamed:
            raise ValueError('The saved worktree changed branches. Inspect it before continuing.')
        binding['branch'] = current
    remember_head(binding, log)
    return path


def save_bindings(manifest, saved):
    temporary = manifest.with_suffix('.tmp')
    with temporary.open('w') as output:
        temporary.chmod(0o600)
        output.write(json.dumps(saved, sort_keys=True))
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(manifest)
    directory = os.open(manifest.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


# A retired worktree keeps its binding. Its archive holds the changes that are not in a commit.
UNTRACKED = 'untracked/'
IN_PROGRESS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG', 'rebase-merge', 'rebase-apply')


def git_output(path, *arguments, data=None):
    # A patch must keep its exact bytes, so this helper does not decode or strip the output.
    result = subprocess.run(['git', '-C', str(path), *arguments], input=data, capture_output=True, timeout=600)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace').strip() or 'Git could not inspect the workspace.')
    return result.stdout


def changes(path):
    """Return the tracked changes against HEAD and the untracked files that Git does not ignore."""
    patch = git_output(path, 'diff', '--binary', '--no-color', '--no-ext-diff', '--no-textconv',
                       '--no-renames', '--no-relative', '--src-prefix=a/', '--dst-prefix=b/', 'HEAD')
    listing = git_output(path, 'ls-files', '--others', '--exclude-standard', '-z')
    return patch, [os.fsdecode(name) for name in listing.split(b'\0') if name]


def changes_digest(patch, names):
    return hashlib.sha256(patch + b'\0\0' + b'\0'.join(os.fsencode(name) for name in names)).hexdigest()


def registered(repository):
    records = []
    for block in git(repository, 'worktree', 'list', '--porcelain').split('\n\n'):
        lines = block.splitlines()
        if lines and lines[0].startswith('worktree '):
            records.append(dict(path=Path(lines[0][len('worktree '):]).resolve(),
                branch=next((line[len('branch '):] for line in lines if line.startswith('branch ')), None),
                locked=any(line == 'locked' or line.startswith('locked ') for line in lines)))
    return records


def in_use(path):
    """Return True when a process has its cwd or an open file in the path."""
    try:
        result = subprocess.run(['lsof', '-n', '-P', '-w', '-F', 'n'], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Coconut cannot check open files without lsof.') from None
    if not result.stdout:
        raise ValueError('lsof did not list open files.')
    prefixes = {os.fsencode(str(path)), os.fsencode(str(Path(path).resolve()))}
    return any(line.startswith(b'n') and any(line[1:] == prefix or line[1:].startswith(prefix + b'/') for prefix in prefixes)
               for line in result.stdout.splitlines())


def file_digest(stream):
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(1 << 20), b''):
        value.update(block)
    return value.hexdigest()


def check_archive(archive, patch, path, names):
    with tarfile.open(archive) as tar:
        members = {member.name: member for member in tar.getmembers()}
        if set(members) != {'retirement.json', 'changes.patch', *(UNTRACKED + name for name in names)}:
            raise ValueError('The archive does not list the expected files.')
        if tar.extractfile(members['changes.patch']).read() != patch:
            raise ValueError('The archive does not contain the expected changes.')
        for name in names:
            member, source = members[UNTRACKED + name], path / name
            if source.is_symlink():
                same = member.issym() and member.linkname == os.readlink(source)
            else:
                with source.open('rb') as original:
                    same = member.isfile() and file_digest(tar.extractfile(member)) == file_digest(original)
            if not same:
                raise ValueError('The archive does not match ' + name + '.')


def write_archive(temporary, metadata, path, patch, names):
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        with tarfile.open(fileobj=output, mode='w:gz') as tar:
            for name, data in (('retirement.json', json.dumps(metadata, sort_keys=True).encode()), ('changes.patch', patch)):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o600, int(datetime.now().timestamp())
                tar.addfile(info, io.BytesIO(data))
            for name in names:
                tar.add(path / name, arcname=UNTRACKED + name, recursive=False)
        output.flush()
        os.fsync(output.fileno())
    check_archive(temporary, patch, path, names)


def retire(root, identity, finished):
    """Archive and remove the worktree of one finished outcome. Return the archive path.

    The caller supplies `finished`. Coconut calls it again under the registry lock
    immediately before it removes the worktree.
    """
    root = Path(root)
    manifest = root / 'worktrees.json'
    with (root / '.workspace.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(manifest.read_text())
        binding = saved.get(identity)
        if not binding or binding.get('retired'):
            return None
        previous = dict(binding)
        path = validate_binding(binding, allow_rename=True).resolve()
        if binding != previous:
            save_bindings(manifest, saved)
        repository = Path(binding['repository'])
        records = registered(repository)
        record = next((r for r in records if r['path'] == path), None)
        if not record or record['branch'] != 'refs/heads/' + binding['branch']:
            raise ValueError('Git does not register this worktree on its saved branch.')
        if record['locked']:
            raise ValueError('The worktree is locked.')
        if any(path in r['path'].parents for r in records):
            raise ValueError('The worktree contains another worktree.')
        git_paths = git(path, 'rev-parse', '--path-format=absolute', *(a for n in IN_PROGRESS for a in ('--git-path', n)))
        if any(Path(p).exists() for p in git_paths.splitlines()):
            raise ValueError('A Git operation is in progress in the worktree.')
        if in_use(path):
            raise ValueError('A process uses the worktree.')
        head = git(path, 'rev-parse', 'HEAD')
        patch, names = changes(path)
        for name in names:
            # Git lists a nested repository as a directory. Removal would delete it without an archive.
            if name.endswith('/') or not ((path / name).is_symlink() or stat.S_ISREG(os.lstat(path / name).st_mode)):
                raise ValueError('The worktree contains a nested repository or a special file.')
        directory = root / 'archive'
        directory.mkdir(mode=0o700, exist_ok=True)
        moment = datetime.now(timezone.utc)
        archive = directory / (identity + '.tar.gz')
        if archive.exists():
            # Keep every earlier archive. A later retirement of the same worktree gets a new name.
            archive = directory / (identity + '-' + moment.strftime('%Y%m%dT%H%M%S%fZ') + '.tar.gz')
        temporary = directory / ('.' + archive.name + '.tmp')
        at = moment.isoformat().replace('+00:00', 'Z')
        try:
            write_archive(temporary, dict(branch=binding['branch'], head=head, outcome=binding['outcome'],
                repository=binding['repository'], path=binding['path'], at=at), path, patch, names)
            if (git(path, 'rev-parse', 'HEAD') != head or changes(path) != (patch, names)
                    or in_use(path) or not finished()):
                raise ValueError('The worktree or its outcome changed during retirement.')
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        binding['retired'] = dict(at=at, archive=str(archive), head=head, changes=changes_digest(patch, names))
        save_bindings(manifest, saved)
        try:
            git(repository, 'worktree', 'remove', '--force', str(path))
        except ValueError:
            # Git refuses some removals before it deletes files, for example with a submodule.
            with contextlib.suppress(ValueError, OSError):
                if (path / '.git').is_file() and changes(path) == (patch, names):
                    binding.pop('retired')
                    save_bindings(manifest, saved)
            raise
        return archive


def restore(binding):
    """Recreate a retired worktree at its saved path. Keep the archive."""
    retired = binding['retired']
    path, archive, repository = Path(binding['path']), Path(retired['archive']), Path(binding['repository'])
    # Hermes can create an empty directory at the path for a native claim. Git checks out into it.
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        # Retirement stopped before Git removed the checkout, or a restore stopped before the registry changed.
        try:
            validate_binding(dict(binding))
            intact = (git(path, 'rev-parse', 'HEAD') == retired['head']
                      and changes_digest(*changes(path)) == retired['changes'])
        except ValueError:
            intact = False
        if not intact:
            raise ValueError(f'The retired worktree path {path} is not empty and does not match {archive}. '
                             'Inspect both before continuing.')
        binding.pop('retired')
        validate_binding(binding)
        return
    created = False
    try:
        with tarfile.open(archive) as tar:
            metadata = json.loads(tar.extractfile('retirement.json').read())
            patch = tar.extractfile('changes.patch').read()
            if (metadata['branch'], metadata['head']) != (binding['branch'], retired['head']):
                raise ValueError('The archive belongs to another branch or commit.')
            if git(repository, 'rev-parse', '--verify', 'refs/heads/' + binding['branch']) != retired['head']:
                raise ValueError('The saved branch moved after retirement.')
            git(repository, 'worktree', 'add', str(path), binding['branch'])
            created = True
            if patch:
                git_output(path, 'apply', '--binary', '--whitespace=nowarn', '-', data=patch)
            for member in tar.getmembers():
                if member.name.startswith(UNTRACKED):
                    name = member.name[len(UNTRACKED):]
                    if os.path.lexists(path / name):
                        raise ValueError('The checkout already contains ' + name + '.')
                    tar.extract(member.replace(name=name, deep=False), path, filter='data')
        if git(path, 'rev-parse', 'HEAD') != retired['head'] or changes_digest(*changes(path)) != retired['changes']:
            raise ValueError('The restored files do not match the archive.')
    except (ValueError, OSError, KeyError, TypeError, AttributeError, tarfile.TarError, subprocess.TimeoutExpired) as error:
        if created:
            # Remove only the partial checkout. The archive stays for the next attempt.
            with contextlib.suppress(ValueError, OSError, subprocess.TimeoutExpired):
                git(repository, 'worktree', 'remove', '--force', str(path))
        raise ValueError(f'Coconut could not restore the retired worktree at {path} from {archive}: {error}') from error
    binding.pop('retired')
    validate_binding(binding)


def acquire(root, outcome, repository, existing_worktree=None):
    root = Path(root)
    repository = Path(repository).expanduser()
    if not repository.is_absolute():
        raise ValueError('Use an absolute repository path.')
    common = Path(git(repository, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    if common.name != '.git':
        raise ValueError('Use a checkout of a non-bare Git repository.')
    repository = common.parent
    key = hashlib.sha256(str(repository).encode()).hexdigest()[:16]
    identity = outcome + '-' + key
    if '/' in outcome or outcome in ('', '.', '..'):
        raise ValueError('Invalid work identity.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.workspace.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = root / 'worktrees.json'
        saved = json.loads(manifest.read_text()) if manifest.exists() else {}
        binding = saved.get(identity)
        if binding:
            previous = dict(binding)
            if binding.get('retired'):
                restore(binding)
            else:
                validate_binding(binding, allow_rename=True)
            if binding != previous:
                save_bindings(manifest, saved)
            return binding
        branch = 'wt/' + identity
        target = repository
        if existing_worktree:
            target = Path(existing_worktree).expanduser().resolve()
            if target == repository or not (target / '.git').is_file():
                raise ValueError('Only a linked worktree can be adopted.')
            if Path(git(target, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != common:
                raise ValueError('The worktree belongs to another repository.')
            branch = git(target, 'branch', '--show-current')
            if not branch:
                raise ValueError('The worktree must have a branch.')
        if any(Path(item['path']).resolve() == target.resolve() for item in saved.values()):
            raise ValueError('Another outcome owns this worktree. Use its existing work item to continue it.')
        from hermes_cli.kanban_db_workspace import resolve_workspace
        task = SimpleNamespace(id=identity, workspace_kind='worktree', workspace_path=str(target), branch_name=branch)
        path = resolve_workspace(task).resolve()
        if path == repository or git(path, 'branch', '--show-current') != branch:
            raise ValueError('Hermes did not resolve the expected isolated branch.')
        if any(Path(item['path']).resolve() == path for item in saved.values()):
            raise ValueError('Another outcome owns this worktree. Use its existing work item to continue it.')
        binding = dict(path=str(path), branch=branch, repository=str(repository), outcome=outcome)
        validate_binding(binding)
        saved[identity] = binding
        save_bindings(manifest, saved)
        return binding


SCHEMA = {'name': 'coconut_workspace', 'description':
    'Get this work item\'s persistent Git worktree before editing code or running repository tests. '
    'Hermes creates it and reuses it for replies and recovery. Supply the local repository path. '
    'When continuing earlier changes, pass their existing_worktree to retain their branch and files. '
    'Use the returned path as cwd for all commands. Use OrbStack for isolated test services when needed. '
    'This routine setup does not require a decision card. It does not push, merge, or deploy.',
    'parameters': {'type': 'object', 'properties': {
        'repository': {'type': 'string'}, 'existing_worktree': {'type': 'string'}}, 'required': ['repository']}}


def preferred(root, outcome, *, verify=True):
    root = Path(root)
    manifest = root / 'worktrees.json'
    if manifest.exists():
        with (root / '.workspace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if verify else fcntl.LOCK_SH)
            saved = json.loads(manifest.read_text())
            bindings = [v for v in saved.values() if v['outcome'] == outcome]
            before = json.dumps(saved, sort_keys=True)
            if verify:
                for binding in bindings:
                    if binding.get('retired'):
                        restore(binding)
                    else:
                        validate_binding(binding, allow_rename=True)
                if json.dumps(saved, sort_keys=True) != before:
                    save_bindings(manifest, saved)
            # A path hint does not restore a retired worktree. It names the path that a restore uses.
            if len(bindings) == 1 and (Path(bindings[0]['path']).is_dir() or bindings[0].get('retired')):
                return Path(bindings[0]['path'])
    return root / outcome
