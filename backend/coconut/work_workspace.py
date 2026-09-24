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
UNTRACKED, IGNORED = 'untracked/', 'ignored/'
PATCHES = ('staged.patch', 'unstaged.patch')
IN_PROGRESS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG', 'rebase-merge', 'rebase-apply')
# Coconut does not archive these ignored folders. An install or a build creates them again.
REBUILDABLE_NAMES = ('node_modules', '.cache')
REBUILDABLE_PATHS = ('tmp', 'log', 'public/packs', 'public/packs-test', 'coverage')
# Coconut keeps a worktree when its other ignored files are larger than this limit.
IGNORED_LIMIT_BYTES = 200 * 1024 * 1024
DIFF = ('--binary', '--no-color', '--no-ext-diff', '--no-textconv', '--no-renames', '--no-relative',
        '--src-prefix=a/', '--dst-prefix=b/')


def git_output(path, *arguments, data=None):
    # A patch must keep its exact bytes, so this helper does not decode or strip the output.
    result = subprocess.run(['git', '-C', str(path), *arguments], input=data, capture_output=True, timeout=600)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace').strip() or 'Git could not inspect the workspace.')
    return result.stdout


def changes(path):
    """Return the staged patch, the unstaged patch, and the untracked files that Git does not ignore."""
    staged = git_output(path, 'diff', '--cached', *DIFF, 'HEAD')
    unstaged = git_output(path, 'diff', *DIFF)
    listing = git_output(path, 'ls-files', '--others', '--exclude-standard', '-z')
    return staged, unstaged, [os.fsdecode(name) for name in listing.split(b'\0') if name]


def changes_digest(staged, unstaged, names):
    value = hashlib.sha256()
    for part in (staged, unstaged, b'\0'.join(os.fsencode(name) for name in names)):
        value.update(hashlib.sha256(part).digest())
    return value.hexdigest()


def rebuildable(name):
    parts = name.rstrip('/').split('/')
    return (any(part in REBUILDABLE_NAMES for part in parts)
            or any('/'.join(parts[:end]) in REBUILDABLE_PATHS for end in range(1, len(parts) + 1)))


def ignored_files(path):
    """Return the ignored files and links to archive. Skip rebuildable folders, sockets, and pipes."""
    listing = git_output(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z')
    names, size = [], 0
    for entry in (os.fsdecode(name) for name in listing.split(b'\0') if name):
        if rebuildable(entry):
            continue
        candidates = [entry]
        if entry.endswith('/'):
            candidates = []
            for directory, folders, files in os.walk(path / entry):
                relative = Path(directory).relative_to(path).as_posix()
                # os.walk lists a link to a folder with the folders. Keep the link and do not follow it.
                links = [folder for folder in folders if (Path(directory) / folder).is_symlink()]
                folders[:] = [f for f in folders if f not in links and not rebuildable(relative + '/' + f)]
                candidates += [relative + '/' + name for name in files + links]
        for name in candidates:
            if rebuildable(name):
                continue
            status = os.lstat(path / name)
            if stat.S_ISREG(status.st_mode):
                size += status.st_size
            elif not stat.S_ISLNK(status.st_mode):
                continue
            names.append(name)
    if size > IGNORED_LIMIT_BYTES:
        raise ValueError('The ignored files in the worktree are larger than the archive limit.')
    return names


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


def check_worktree(path, binding, repository):
    """Refuse a worktree that Git or a process still uses."""
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


def file_digest(stream):
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(1 << 20), b''):
        value.update(block)
    return value.hexdigest()


def check_archive(archive, patches, path, entries):
    with tarfile.open(archive) as tar:
        members = {member.name: member for member in tar.getmembers()}
        if set(members) != {'retirement.json', *patches, *(arcname for arcname, _ in entries)}:
            raise ValueError('The archive does not list the expected files.')
        if any(tar.extractfile(members[name]).read() != data for name, data in patches.items()):
            raise ValueError('The archive does not contain the expected changes.')
        for arcname, name in entries:
            member, source = members[arcname], path / name
            if source.is_symlink():
                same = member.issym() and member.linkname == os.readlink(source)
            else:
                with source.open('rb') as original:
                    same = member.isfile() and file_digest(tar.extractfile(member)) == file_digest(original)
            if not same:
                raise ValueError('The archive does not match ' + name + '.')


def write_archive(temporary, metadata, path, patches, entries):
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        with tarfile.open(fileobj=output, mode='w:gz') as tar:
            for name, data in (('retirement.json', json.dumps(metadata, sort_keys=True).encode()), *patches.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o600, int(datetime.now().timestamp())
                tar.addfile(info, io.BytesIO(data))
            for arcname, name in entries:
                tar.add(path / name, arcname=arcname, recursive=False)
        output.flush()
        os.fsync(output.fileno())
    check_archive(temporary, patches, path, entries)


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def retire(root, identity, finished):
    """Archive and remove the worktree of one finished outcome. Return the archive path.

    The slow Git, lsof, and archive work runs without the registry lock, so a path hint
    does not wait for it. Under the lock, Coconut checks the worktree and the outcome again.
    The caller supplies `finished` for that check.
    """
    root = Path(root)
    manifest = root / 'worktrees.json'
    # save_bindings replaces the registry by an atomic rename, so this read needs no lock.
    before = json.loads(manifest.read_text()).get(identity)
    if not before or before.get('retired'):
        return None
    path = validate_binding(dict(before)).resolve()
    repository = Path(before['repository'])
    check_worktree(path, before, repository)
    head = git(path, 'rev-parse', 'HEAD')
    current = changes(path)
    staged, unstaged, names = current
    for name in names:
        # Git lists a nested repository as a directory. Removal would delete it without an archive.
        if name.endswith('/') or not ((path / name).is_symlink() or stat.S_ISREG(os.lstat(path / name).st_mode)):
            raise ValueError('The worktree contains a nested repository or a special file.')
    entries = [(UNTRACKED + name, name) for name in names] + [(IGNORED + name, name) for name in ignored_files(path)]
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
        write_archive(temporary, dict(branch=before['branch'], head=head, outcome=before['outcome'],
            repository=before['repository'], path=before['path'], at=at), path,
            dict(zip(PATCHES, (staged, unstaged))), entries)
        with (root / '.workspace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            saved = json.loads(manifest.read_text())
            binding = saved.get(identity)
            if binding != before or archive.exists():
                raise ValueError('The workspace binding changed during retirement.')
            validate_binding(dict(binding))
            check_worktree(path, binding, repository)
            if git(path, 'rev-parse', 'HEAD') != head or changes(path) != current or not finished():
                raise ValueError('The worktree or its outcome changed during retirement.')
            temporary.replace(archive)
            sync_directory(directory)
            binding['retired'] = dict(at=at, archive=str(archive), head=head, changes=changes_digest(*current))
            save_bindings(manifest, saved)
            try:
                git(repository, 'worktree', 'remove', '--force', str(path))
            except ValueError:
                # Git refuses some removals before it deletes files, for example with a submodule.
                with contextlib.suppress(ValueError, OSError):
                    if (path / '.git').is_file() and changes(path) == current:
                        binding.pop('retired')
                        save_bindings(manifest, saved)
                raise
            return archive
    finally:
        temporary.unlink(missing_ok=True)


def place_link(path, name, target):
    """Recreate one archived link. Do not follow it, and do not write through another link."""
    link = path / name
    top = os.path.realpath(path)
    if os.path.commonpath([top, os.path.realpath(link.parent)]) != top:
        raise ValueError('The archived link ' + name + ' is not inside the worktree.')
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)


def unpack(binding):
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
            raise ValueError(f'The path {path} is not empty and does not match the archive.')
        return
    created = False
    try:
        with tarfile.open(archive) as tar:
            metadata = json.loads(tar.extractfile('retirement.json').read())
            staged, unstaged = (tar.extractfile(name).read() for name in PATCHES)
            if (metadata['branch'], metadata['head']) != (binding['branch'], retired['head']):
                raise ValueError('The archive belongs to another branch or commit.')
            if git(repository, 'rev-parse', '--verify', 'refs/heads/' + binding['branch']) != retired['head']:
                raise ValueError('The saved branch moved after retirement.')
            git(repository, 'worktree', 'add', str(path), binding['branch'])
            created = True
            if staged:
                git_output(path, 'apply', '--index', '--binary', '--whitespace=nowarn', '-', data=staged)
            if unstaged:
                git_output(path, 'apply', '--binary', '--whitespace=nowarn', '-', data=unstaged)
            links = []
            for member in tar.getmembers():
                prefix = next((p for p in (UNTRACKED, IGNORED) if member.name.startswith(p)), None)
                if not prefix:
                    continue
                name = member.name[len(prefix):]
                if os.path.lexists(path / name):
                    raise ValueError('The checkout already contains ' + name + '.')
                if member.issym():
                    # The data filter refuses a link that points outside the worktree. Coconut recreates links itself.
                    links.append((name, member.linkname))
                elif member.isfile():
                    tar.extract(member.replace(name=name, deep=False), path, filter='data')
                else:
                    raise ValueError('The archive contains an unexpected entry for ' + name + '.')
            # Create links last, so that no archived file is written through a link.
            for name, target in links:
                place_link(path, name, target)
        if git(path, 'rev-parse', 'HEAD') != retired['head'] or changes_digest(*changes(path)) != retired['changes']:
            raise ValueError('The restored files do not match the archive.')
    except BaseException:
        if created:
            # Remove only the partial checkout. The archive stays.
            with contextlib.suppress(ValueError, OSError, subprocess.TimeoutExpired):
                git(repository, 'worktree', 'remove', '--force', str(path))
        raise


def restore(binding):
    """Recreate a retired worktree at its saved path. Keep the archive.

    A failed restore marks the binding `restore_failed`. Coconut does not try again
    until an operator removes that mark. The caller saves the registry.
    """
    retired = binding['retired']
    archive = retired['archive']
    failure = retired.get('restore_failed')
    if not failure:
        try:
            unpack(binding)
        except (ValueError, OSError, KeyError, TypeError, AttributeError, tarfile.TarError,
                subprocess.TimeoutExpired) as error:
            failure = dict(at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), error=str(error)[:1000])
            binding['retired'] = dict(retired, restore_failed=failure)
        else:
            binding.pop('retired')
            validate_binding(binding)
            return
    raise ValueError(f'Coconut retired this worktree and could not restore it: {failure["error"]} '
                     f'The archive {archive} holds its uncommitted changes. The branch {binding["branch"]} '
                     'holds its commits. Ask the operator to restore it.')


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
            try:
                if binding.get('retired'):
                    restore(binding)
                else:
                    validate_binding(binding, allow_rename=True)
            finally:
                # Save a restore or its failure mark. The bot then gets the archive path in the error.
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
                        # A failed restore does not stop new work. coconut_workspace reports the archive to the bot.
                        with contextlib.suppress(ValueError):
                            restore(binding)
                    else:
                        validate_binding(binding, allow_rename=True)
                if json.dumps(saved, sort_keys=True) != before:
                    save_bindings(manifest, saved)
            # A path hint does not restore a retired worktree. It names the path that a restore uses.
            retired = len(bindings) == 1 and bindings[0].get('retired') or {}
            if (len(bindings) == 1 and not retired.get('restore_failed')
                    and (Path(bindings[0]['path']).is_dir() or retired)):
                return Path(bindings[0]['path'])
    return root / outcome
