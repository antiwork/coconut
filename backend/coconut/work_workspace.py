"""Keep one native Hermes worktree per outcome and repository."""
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
from types import SimpleNamespace
import zlib


def git(path, *arguments, timeout=60):
    result = subprocess.run(['git', '-C', str(path), *arguments], capture_output=True, text=True, timeout=timeout)
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
# Coconut keeps a worktree when its untracked files are larger than this limit.
UNTRACKED_LIMIT_BYTES = 200 * 1024 * 1024
# Coconut keeps a worktree when its staged and unstaged patches together are larger than this limit.
PATCH_LIMIT_BYTES = 200 * 1024 * 1024
# After Git refuses to remove a worktree, Coconut waits this long before the next attempt.
RETRY_AFTER = timedelta(days=1)
# Git commands that write or delete a whole checkout can take minutes for a large repository.
CHECKOUT_SECONDS = 600
GIT_ERRORS = (ValueError, OSError, subprocess.TimeoutExpired)
DIFF = ('--binary', '--no-color', '--no-ext-diff', '--no-textconv', '--no-renames', '--no-relative',
        '--src-prefix=a/', '--dst-prefix=b/')


def git_output(path, *arguments, data=None, strict=False):
    # A patch must keep its exact bytes, so this helper does not decode or strip the output.
    result = subprocess.run(['git', '-C', str(path), *arguments], input=data, capture_output=True, timeout=CHECKOUT_SECONDS)
    # With strict=True, a warning also fails. Git warns and continues when it cannot read a directory.
    if result.returncode or strict and result.stderr.strip():
        raise ValueError(result.stderr.decode(errors='replace').strip() or 'Git could not inspect the workspace.')
    return result.stdout


def changes(path):
    """Return the staged patch, the unstaged patch, and the untracked files that Git does not ignore."""
    staged = git_output(path, 'diff', '--cached', *DIFF, 'HEAD')
    unstaged = git_output(path, 'diff', *DIFF)
    listing = git_output(path, 'ls-files', '--others', '--exclude-standard', '-z', strict=True)
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
    listing = git_output(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z', strict=True)
    names, size = [], 0

    def unreadable(error):
        # Removal would delete the files that Coconut cannot read. Keep the checkout.
        raise ValueError('Coconut cannot read all ignored files in the worktree: ' + str(error)) from error
    for entry in (os.fsdecode(name) for name in listing.split(b'\0') if name):
        if rebuildable(entry):
            continue
        candidates = [entry]
        if entry.endswith('/'):
            candidates = []
            for directory, folders, files in os.walk(path / entry, onerror=unreadable):
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
                # Removal would delete a socket or a pipe that the archive cannot hold.
                raise ValueError('The worktree contains an ignored special file: ' + name)
            names.append(name)
    if size > IGNORED_LIMIT_BYTES:
        raise ValueError('The ignored files in the worktree are larger than the archive limit.')
    return names


def fingerprint(path, names):
    """Return the inode, size, times, and type of each archived file.

    The change time moves on every write and a program cannot set it back, so
    a write shows a difference even when the size and modification time match.
    """
    result = []
    for name in sorted(names):
        status = os.lstat(path / name)
        result.append((name, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns,
                       stat.S_IFMT(status.st_mode)))
    return result


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


def check_worktree(path, binding, repository, *, processes=True):
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
    if processes and in_use(path):
        raise ValueError('A process uses the worktree.')


def file_digest(stream):
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(1 << 20), b''):
        value.update(block)
    return value.hexdigest()


def path_digest(path):
    with open(path, 'rb') as stream:
        return file_digest(stream)


def check_archive(archive, patches, path, entries):
    with tarfile.open(archive) as tar:
        members = {member.name: member for member in tar.getmembers()}
        if set(members) != {'retirement.json', *patches, *(arcname for arcname, _ in entries)}:
            raise ValueError('The archive does not list the expected files.')
        if any(file_digest(tar.extractfile(members[name])) != hashlib.sha256(data).hexdigest()
               for name, data in patches.items()):
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
                # Store each hard link as a regular file. A link member would point to another archive name.
                tar.inodes.clear()
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


def stamp(moment):
    return moment.isoformat().replace('+00:00', 'Z')


def retire(root, identity, finished):
    """Archive and remove the worktree of one finished outcome. Return the archive path.

    The caller supplies `finished(wait)`. It returns True when the outcome is still finished.
    With wait=False it waits only briefly for the service lock, and it raises ValueError when that lock stays busy.
    Coconut calls it with wait=True before it takes the registry lock, and with wait=False under that lock.
    The Git, lsof, and archive work runs before the registry lock. Under the lock, Coconut only
    repeats the Git checks that do not wait for other processes.
    """
    root = Path(root)
    manifest = root / 'worktrees.json'
    # save_bindings replaces the registry by an atomic rename, so this read needs no lock.
    before = json.loads(manifest.read_text()).get(identity)
    if not before or before.get('retired'):
        return None
    failed = (before.get('retire_failed') or {}).get('at')
    if failed and datetime.now(timezone.utc) - datetime.fromisoformat(failed.replace('Z', '+00:00')) < RETRY_AFTER:
        return None
    path = validate_binding(dict(before)).resolve()
    repository = Path(before['repository'])
    check_worktree(path, before, repository)
    head = git(path, 'rev-parse', 'HEAD')
    current = changes(path)
    staged, unstaged, names = current
    if len(staged) + len(unstaged) > PATCH_LIMIT_BYTES:
        raise ValueError('The changes in the worktree are larger than the archive limit.')
    untracked = 0
    for name in names:
        # Git lists a nested repository as a directory. Removal would delete it without an archive.
        if name.endswith('/') or not ((path / name).is_symlink() or stat.S_ISREG(os.lstat(path / name).st_mode)):
            raise ValueError('The worktree contains a nested repository or a special file.')
        untracked += os.lstat(path / name).st_size
    if untracked > UNTRACKED_LIMIT_BYTES:
        raise ValueError('The untracked files in the worktree are larger than the archive limit.')
    entries = [(UNTRACKED + name, name) for name in names] + [(IGNORED + name, name) for name in ignored_files(path)]
    directory = root / 'archive'
    directory.mkdir(mode=0o700, exist_ok=True)
    moment = datetime.now(timezone.utc)
    archive = directory / (identity + '.tar.gz')
    if archive.exists():
        # Do not write over an earlier archive. After the removal, drop_other_archives deletes it if no binding names it.
        archive = directory / (identity + '-' + moment.strftime('%Y%m%dT%H%M%S%fZ') + '.tar.gz')
    temporary = directory / ('.' + archive.name + '.tmp')
    at = stamp(moment)
    # changes() lists untracked names only. Compare file contents and ignored files again before removal.
    archived = [name for _, name in entries]
    marks = fingerprint(path, archived)
    try:
        write_archive(temporary, dict(branch=before['branch'], head=head, outcome=before['outcome'],
            repository=before['repository'], path=before['path'], at=at), path,
            dict(zip(PATCHES, (staged, unstaged))), entries)
        if not finished(True):
            raise ValueError('The outcome resumed during retirement.')
        if in_use(path):
            raise ValueError('A process uses the worktree.')
        with (root / '.workspace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            saved = json.loads(manifest.read_text())
            binding = saved.get(identity)
            if binding != before or archive.exists():
                raise ValueError('The workspace binding changed during retirement.')
            validate_binding(dict(binding))
            check_worktree(path, binding, repository, processes=False)
            if (git(path, 'rev-parse', 'HEAD') != head or changes(path) != current
                    or sorted(ignored_files(path)) != sorted(name for arcname, name in entries if arcname.startswith(IGNORED))
                    or fingerprint(path, archived) != marks):
                raise ValueError('The worktree changed during retirement.')
            # A dispatch marks its task active before its worker takes this lock. Check the tasks with a short wait.
            if not finished(False):
                raise ValueError('The outcome resumed during retirement.')
            temporary.replace(archive)
            sync_directory(directory)
            binding.pop('retire_failed', None)
            binding['retired'] = dict(at=at, archive=str(archive), head=head, changes=changes_digest(*current))
            save_bindings(manifest, saved)
            try:
                git(repository, 'worktree', 'remove', '--force', str(path), timeout=CHECKOUT_SECONDS)
            except GIT_ERRORS as error:
                try:
                    removal_stopped(manifest, saved, binding, path, repository, current, archive, error)
                finally:
                    # A worktree that Git keeps refusing to remove keeps only its latest archive.
                    drop_other_archives(directory, identity, archive, saved)
            drop_other_archives(directory, identity, archive, saved)
            return archive
    finally:
        temporary.unlink(missing_ok=True)


def removal_stopped(manifest, saved, binding, path, repository, current, archive, error):
    """Decide from the path what a failed or interrupted `git worktree remove` did. Hold the registry lock."""
    if not os.path.lexists(path):
        # Git deleted the checkout before it stopped. Remove its stale record. The retirement is complete.
        with contextlib.suppress(*GIT_ERRORS):
            git(repository, 'worktree', 'prune', timeout=CHECKOUT_SECONDS)
        return
    try:
        intact = (path / '.git').is_file() and changes(path) == current
    except GIT_ERRORS:
        intact = False
    now = stamp(datetime.now(timezone.utc))
    if intact:
        # Git refuses some removals before it deletes files, for example with a submodule.
        # Git can also delete ignored files before it stops, and changes() does not see them.
        # Keep the checkout and the archive, and wait a day before the next attempt.
        binding.pop('retired')
        binding['retire_failed'] = dict(at=now, error=str(error)[:1000], archive=str(archive))
        save_bindings(manifest, saved)
        raise ValueError('Git did not remove the worktree: ' + str(error)) from error
    # Git deleted part of the checkout. The archive holds the changes. Do not restore over the remains.
    message = (f'Git stopped while it removed {path}, and part of the checkout remains. '
               f'The archive {archive} holds its changes. Inspect the path.')
    binding['retired'] = dict(binding['retired'], restore_failed=dict(at=now, error=message))
    save_bindings(manifest, saved)
    raise ValueError(message) from error


def drop_other_archives(directory, identity, keep, saved):
    """Keep one archive for each worktree. Never delete an archive that a binding names."""
    named = {(b.get(key) or {}).get('archive') for b in saved.values() for key in ('retired', 'retire_failed')}
    for old in [directory / (identity + '.tar.gz'), *directory.glob(identity + '-*.tar.gz')]:
        if old != keep and str(old) not in named:
            old.unlink(missing_ok=True)


def sweep_temporary(root):
    """Delete archive files that an interrupted retirement did not finish. Run before any retirement starts."""
    for temporary in (Path(root) / 'archive').glob('.*.tar.gz.tmp'):
        temporary.unlink(missing_ok=True)


def discard_checkout(repository, path):
    """Remove a partial restore. The path was missing or empty before the restore."""
    with contextlib.suppress(*GIT_ERRORS):
        git(repository, 'worktree', 'remove', '--force', str(path), timeout=CHECKOUT_SECONDS)
    with contextlib.suppress(OSError):
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
    with contextlib.suppress(*GIT_ERRORS):
        git(repository, 'worktree', 'prune', timeout=CHECKOUT_SECONDS)


def place_link(path, name, target):
    """Recreate one archived link. Do not follow it, and do not write through another link."""
    link = path / name
    top = os.path.realpath(path)
    if os.path.commonpath([top, os.path.realpath(link.parent)]) != top:
        raise ValueError('The archived link ' + name + ' is not inside the worktree.')
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)


def archived_files_present(path, archive):
    """Return True when every archived untracked and ignored file exists with the same size or link."""
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            prefix = next((p for p in (UNTRACKED, IGNORED) if member.name.startswith(p)), None)
            if not prefix:
                continue
            target = path / member.name[len(prefix):]
            if member.issym():
                if not target.is_symlink() or os.readlink(target) != member.linkname:
                    return False
            elif (not target.is_file() or target.is_symlink() or target.stat().st_size != member.size
                    or path_digest(target) != file_digest(tar.extractfile(member))):
                # Same size is not enough. Compare the contents with the archived copy.
                return False
    return True


def unpack(binding):
    retired = binding['retired']
    path, archive, repository = Path(binding['path']), Path(retired['archive']), Path(binding['repository'])
    # Hermes can create an empty directory at the path for a native claim. Git checks out into it.
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        # Retirement stopped before Git removed the checkout, or a restore stopped before the registry changed.
        try:
            validate_binding(dict(binding))
            intact = (git(path, 'rev-parse', 'HEAD') == retired['head']
                      and changes_digest(*changes(path)) == retired['changes']
                      and archived_files_present(path, Path(retired['archive'])))
        except (*GIT_ERRORS, tarfile.TarError, EOFError, zlib.error):
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
            # Mark the checkout before Git starts. An interrupted add can leave a registered partial checkout.
            created = True
            git(repository, 'worktree', 'add', str(path), binding['branch'], timeout=CHECKOUT_SECONDS)
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
            discard_checkout(repository, path)
        raise


def restore(binding):
    """Recreate a retired worktree at its saved path.

    After a successful restore, the worktree holds every archived change, so Coconut deletes the archive.
    A failed restore keeps the archive and marks the binding `restore_failed`. Coconut does not try again
    until an operator removes that mark. The caller saves the registry.
    """
    retired = binding['retired']
    archive = retired['archive']
    failure = retired.get('restore_failed')
    if not failure:
        try:
            unpack(binding)
        except (ValueError, OSError, KeyError, TypeError, AttributeError, tarfile.TarError,
                subprocess.TimeoutExpired, EOFError, zlib.error) as error:
            # EOFError and zlib.error come from a damaged or truncated archive.
            failure = dict(at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), error=str(error)[:1000])
            binding['retired'] = dict(retired, restore_failed=failure)
        else:
            binding.pop('retired')
            validate_binding(binding)
            Path(archive).unlink(missing_ok=True)
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


def chosen(root, outcome, bindings):
    # A path hint does not restore a retired worktree. It names the path that a restore uses.
    retired = len(bindings) == 1 and bindings[0].get('retired') or {}
    if len(bindings) == 1 and not retired.get('restore_failed') and (Path(bindings[0]['path']).is_dir() or retired):
        return Path(bindings[0]['path'])
    return root / outcome


def preferred(root, outcome, *, verify=True):
    root = Path(root)
    manifest = root / 'worktrees.json'
    if not verify:
        # Native dispatch reads this path hint while it holds the service lock, so the hint does not take
        # the registry lock. save_bindings replaces the file by an atomic rename. A read sees one complete version.
        try:
            return chosen(root, outcome, [v for v in json.loads(manifest.read_text()).values() if v['outcome'] == outcome])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return root / outcome
    # The provider calls this before a turn without the service lock, so a restore can hold the registry lock.
    if manifest.exists():
        with (root / '.workspace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            saved = json.loads(manifest.read_text())
            bindings = [v for v in saved.values() if v['outcome'] == outcome]
            before = json.dumps(saved, sort_keys=True)
            for binding in bindings:
                if binding.get('retired'):
                    # A failed restore does not stop new work. coconut_workspace reports the archive to the bot.
                    with contextlib.suppress(ValueError):
                        restore(binding)
                else:
                    validate_binding(binding, allow_rename=True)
            if json.dumps(saved, sort_keys=True) != before:
                save_bindings(manifest, saved)
            return chosen(root, outcome, bindings)
    return root / outcome


def retired_path(root, outcome, path):
    """Return True when `path` is the saved path of a retired binding of `outcome`. Read without the lock."""
    try:
        saved = json.loads((Path(root) / 'worktrees.json').read_text())
    except (OSError, ValueError):
        return False
    return any(isinstance(b, dict) and b.get('outcome') == outcome and b.get('retired')
               and b.get('path') == str(path) for b in saved.values())
