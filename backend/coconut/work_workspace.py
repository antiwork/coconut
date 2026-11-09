"""Keep one native Hermes worktree per outcome and repository.

The worker and the coconut plugin of the Hermes gateway call acquire for coconut_workspace (its schema is
card_tools.WORKSPACE_SCHEMA). The plugin loads this file by its path, so it imports only the standard library, and
Hermes inside acquire.
"""
import contextlib
import errno
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import itertools
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
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


# `git reflog expire` writes every entry again, with a tab before an empty message. `git gc` runs it, and Git starts
# that gc in the background after an ordinary fetch or commit. The first entry of `git worktree add` has an empty
# message, so the rewrite changes the bytes of a saved checkpoint but not its entries.
REFLOG_ENTRY = re.compile(rb'([0-9a-f]{40}|[0-9a-f]{64}) ([0-9a-f]{40}|[0-9a-f]{64}) [^\t\n]* (\d+) [+-]\d{4}(\t[^\n]*)?\n')
# With the default settings, expiry removes no entry that is younger than gc.reflogExpireUnreachable, 30 days.
REFLOG_KEEP_SECONDS = 30 * 24 * 60 * 60
# Each rewritten entry may or may not have had its tab before. Coconut tries at most this many saved versions.
REWRITE_CANDIDATES = 256


def checkpoint_end(path, log, checkpoint):
    """Return the length of the saved checkpoint in `log`, or None when the log does not continue it."""
    size, digest = checkpoint.get('size', 0), checkpoint.get('digest')
    if size <= 0 or len(log) < size:
        return None
    if hashlib.sha256(log[:size]).hexdigest() == digest:
        return size
    lines = log.splitlines(keepends=True)
    entries = [REFLOG_ENTRY.fullmatch(line) for line in lines]
    if not all(entries):
        return None
    end = 0
    for count, line in enumerate(lines, 1):
        end += len(line)
        added = end - size
        if added < 0:
            continue
        tabbed = [index for index in range(count) if entries[index].group(4) == b'\t']
        if added > len(tabbed) or math.comb(len(tabbed), added) > REWRITE_CANDIDATES:
            return None
        for untabbed in itertools.combinations(tabbed, added):
            saved = b''.join(lines[index][:-2] + b'\n' if index in untabbed else lines[index] for index in range(count))
            if hashlib.sha256(saved).hexdigest() == digest:
                if not complete_after_rewrite(path, entries, count):
                    return None
                logging.getLogger(__name__).warning(
                    'Coconut matched the saved Git history of worktree %s after Git rewrote its reflog.', path)
                return end
    return None


def complete_after_rewrite(path, entries, count):
    # The saved bytes prove the first `count` entries. Expiry can also remove later entries, so check that it did not.
    settings = subprocess.run(['git', '-C', str(path), 'config', '--get-regexp', r'^gc\..*reflogexpire'],
                              capture_output=True, timeout=60)
    if settings.returncode != 1 or settings.stdout:
        return False
    oldest = time.time() - REFLOG_KEEP_SECONDS
    previous = entries[count - 1].group(2)
    for entry in entries[count:]:
        if entry.group(1) != previous or int(entry.group(3)) < oldest:
            return False
        previous = entry.group(2)
    return previous.decode() == git(path, 'rev-parse', 'HEAD')


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
        size = checkpoint_end(path, log, binding.get('_head_log', {}))
        continuous = size is not None
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
# free_rebuildable moves build folders into this folder of the registry, and then deletes them.
TRASH = 'build-trash'
# Coconut does not archive these ignored folders. An install or a build creates them again. A name counts anywhere in
# the worktree. A path counts only from the top of the worktree. Only a folder that Git ignores counts, so a tracked
# folder with one of these names is never build output.
REBUILDABLE_NAMES = ('node_modules', '.cache', '.next', '.turbo', '.parcel-cache', '.vite', '.nuxt', '.svelte-kit',
                     '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.gradle', 'DerivedData')
# A build or dist folder stays out of this list: its name alone does not prove that a build made all of it.
REBUILDABLE_PATHS = ('tmp', 'log', 'public/packs', 'public/packs-test', 'coverage',
                     'public/vite', 'public/vite-dev', 'public/vite-test', 'public/vite-ssr', 'public/assets')
# Coconut keeps a worktree when its other ignored files are larger than this limit.
IGNORED_LIMIT_BYTES = 200 * 1024 * 1024
# Coconut keeps a worktree when its untracked files are larger than this limit.
UNTRACKED_LIMIT_BYTES = 200 * 1024 * 1024
# Coconut keeps a worktree when its staged and unstaged patches together are larger than this limit.
PATCH_LIMIT_BYTES = 200 * 1024 * 1024
# Coconut deletes an archive of a nested worktree after this many days, and the oldest ones when all of them
# together are larger than NESTED_ARCHIVE_LIMIT_BYTES.
NESTED_ARCHIVE_DAYS = 30
NESTED_ARCHIVE_LIMIT_BYTES = 2 * 1024 ** 3
# The discard archives that a binding still names, together, stay within this limit.
DISCARD_ARCHIVE_LIMIT_BYTES = 4 * 1024 ** 3
# The size limit never deletes an archive younger than this, so a person has time to recover the work.
NESTED_ARCHIVE_KEEP_SECONDS = 24 * 60 * 60
# After Git refuses to remove a worktree, Coconut waits this long before the next attempt.
RETRY_AFTER = timedelta(days=1)
# Git commands that write or delete a whole checkout can take minutes for a large repository.
CHECKOUT_SECONDS = 600
GIT_ERRORS = (ValueError, OSError, subprocess.TimeoutExpired)
# EOFError and zlib.error come from a damaged or truncated archive.
RESTORE_ERRORS = (*GIT_ERRORS, KeyError, TypeError, AttributeError, tarfile.TarError, EOFError, zlib.error)
DIFF = ('--binary', '--no-color', '--no-ext-diff', '--no-textconv', '--no-renames', '--no-relative',
        '--src-prefix=a/', '--dst-prefix=b/')


def git_output(path, *arguments, data=None, strict=False, env=None):
    # A patch must keep its exact bytes, so this helper does not decode or strip the output.
    result = subprocess.run(['git', '-C', str(path), *arguments], input=data, capture_output=True, timeout=CHECKOUT_SECONDS,
                            env=None if env is None else {**os.environ, **env})
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


# The header line of a file in a patch that names its old and new blob, and its mode when that stays.
INDEX_LINE = re.compile(rb'index ([0-9a-f]+)\.\.([0-9a-f]+)( [0-7]{6})?')


def same_patch(archived, full):
    """Return True when `full`, a patch with full object names, holds the changes of the `archived` patch.

    Git abbreviates the blob names in the index lines of a text patch by the number of objects in the repository.
    --binary writes full names only for binary files. A fetch after retirement can make the names longer, so the same
    changes give other bytes. Every other line must be the same, and each archived name must start the full name.
    """
    old, new = archived.split(b'\n'), full.split(b'\n')
    if len(old) != len(new):
        return False
    for line, other in zip(old, new):
        if line == other:
            continue
        short, long = INDEX_LINE.fullmatch(line), INDEX_LINE.fullmatch(other)
        if not (short and long and short.group(3) == long.group(3)
                and all(len(short.group(i)) >= 4 and long.group(i).startswith(short.group(i)) for i in (1, 2))):
            return False
    return True


def matches_archive(path, staged, unstaged, expected):
    """Return True when the checkout at path holds the archived changes: their digest `expected` (changes_digest).

    `staged` and `unstaged` are the archived patches. When the digest of the checkout differs only because Git now
    abbreviates object names with more characters (same_patch), the checkout still holds the archived changes. The
    archived patches and the untracked names of the checkout must then give the saved digest themselves.
    """
    current = changes(path)
    if changes_digest(*current) == expected:
        return True
    if changes_digest(staged, unstaged, current[2]) != expected:
        return False
    return (same_patch(staged, git_output(path, 'diff', '--cached', *DIFF, '--full-index', 'HEAD'))
            and same_patch(unstaged, git_output(path, 'diff', *DIFF, '--full-index')))


def rebuildable(name):
    parts = name.rstrip('/').split('/')
    return (any(part in REBUILDABLE_NAMES for part in parts)
            or any('/'.join(parts[:end]) in REBUILDABLE_PATHS for end in range(1, len(parts) + 1)))


def ignored_files(path, inside=(), env=None, sizes=None):
    """Return the ignored files and links to archive. Skip rebuildable folders and the worktrees in `inside`.

    `env` names the Git folders of an orphaned worktree (see orphan_git). With `sizes`, a dict, Coconut adds the bytes
    of each entry that Git lists to it, and it does not apply IGNORED_LIMIT_BYTES (see held_folders)."""
    listing = git_output(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z', strict=True,
                         env=env)
    names, seen, size = [], set(), 0

    def unreadable(error):
        # Removal would delete the files that Coconut cannot read. Keep the checkout.
        raise ValueError('Coconut cannot read all ignored files in the worktree: ' + str(error)) from error
    for entry in (os.fsdecode(name) for name in listing.split(b'\0') if name):
        # A link to a nested worktree is a file of this worktree. Keep it; resolve() would follow it.
        if rebuildable(entry) or (inside and not (path / entry.rstrip('/')).is_symlink()
                                  and (path / entry.rstrip('/')).resolve() in inside):
            continue
        candidates = [entry]
        if sizes is not None:
            sizes.setdefault(entry, 0)
        if entry.endswith('/'):
            candidates = []
            for directory, folders, files in os.walk(path / entry, onerror=unreadable):
                relative = Path(directory).relative_to(path).as_posix()
                # os.walk lists a link to a folder with the folders. Keep the link and do not follow it.
                links = [folder for folder in folders if (Path(directory) / folder).is_symlink()]
                folders[:] = [f for f in folders if f not in links and not rebuildable(relative + '/' + f)
                              and not (inside and (Path(directory) / f).resolve() in inside)]
                candidates += [relative + '/' + name for name in files + links]
        for name in candidates:
            # Git can list both a directory and its children when its own .gitignore
            # ignores everything. The directory walk already included those children.
            if name in seen or rebuildable(name):
                continue
            seen.add(name)
            status = os.lstat(path / name)
            if stat.S_ISREG(status.st_mode):
                size += status.st_size
                if sizes is not None:
                    sizes[entry] += status.st_size
            elif not stat.S_ISLNK(status.st_mode):
                # Removal would delete a socket or a pipe that the archive cannot hold.
                raise ValueError('The worktree contains an ignored special file: ' + name)
            names.append(name)
    if size > IGNORED_LIMIT_BYTES and sizes is None:
        raise ValueError('The ignored files in the worktree are larger than the archive limit.')
    return names


# Retirement moves the ignored folders that do not fit in the archive to archive/held/<identity>-<time>/, and the restore
# moves them back. A held folder goes after NESTED_ARCHIVE_DAYS, as an archive does.
HELD = 'held'


def held_folders(path, inside=()):
    """Return the ignored folders that retirement moves aside, so that the other ignored files fit in the archive.

    Bots keep evidence in ignored folders, for example screenshots and recordings of a QA run. It is not build output,
    so the archive must keep it, but it can be larger than IGNORED_LIMIT_BYTES. A rename keeps it whole and costs no copy.
    Coconut takes the largest ignored folders first. A folder that holds a Git repository stays, because the move would
    hide the repository from its main checkout. Return [] when the ignored files fit, or when moving folders cannot make
    them fit, for example when one large ignored file is not in a folder. Retirement then refuses as before.
    """
    sizes = {}
    ignored_files(path, inside, sizes=sizes)
    total = sum(sizes.values())
    held = []
    for entry, size in sorted(sizes.items(), key=lambda item: (-item[1], item[0])):
        if total <= IGNORED_LIMIT_BYTES:
            break
        folder = path / entry.rstrip('/')
        if entry.endswith('/') and folder.is_dir() and not folder.is_symlink() and not holds_repository(folder):
            held.append(entry.rstrip('/'))
            total -= size
    return held if total <= IGNORED_LIMIT_BYTES else []


class HeldMoveFailed(OSError):
    """A move of held folders failed, and its undo could not put every folder back. `stranded` names those folders."""

    def __init__(self, message, stranded):
        super().__init__(message)
        self.stranded = stranded


def hold(path, place, names):
    """Move the held folders of the worktree at path to place. On a failure, move back what moved, and raise."""
    moved = []
    try:
        for name in names:
            target = place / name
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.rename(path / name, target)
            moved.append(name)
    except OSError:
        return_held(dict(folder=str(place), names=moved), path)
        raise


def return_held(held, path):
    """Move the held folders back into the worktree at path. Return the names that are gone. Never replace a file.

    A name that is still in the worktree was never moved, for example after a stop between the record and the move.
    The move is all or nothing: when one rename fails, Coconut moves the folders that it already moved back to the held
    place, because a failed restore deletes the new checkout. It then raises HeldMoveFailed. Its `stranded` names the
    folders that the undo could not move back, and the caller must keep the checkout that holds them.
    """
    place, missing, returned = Path(held['folder']), [], []
    try:
        for name in held['names']:
            source, target = place / name, path / name
            if os.path.lexists(target):
                continue
            if not os.path.lexists(source):
                missing.append(name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, target)
            returned.append(name)
    except OSError as error:
        stranded = []
        for name in reversed(returned):
            try:
                os.rename(path / name, place / name)
            except OSError:
                stranded.append(name)
        raise HeldMoveFailed(f'Coconut could not move the held folders between {place} and {path}: {error}',
                             stranded) from error
    if os.path.isdir(place) and not os.path.islink(place):
        # Remove the empty folders that the move left. A folder that still holds files stays.
        for directory, _, _ in sorted(os.walk(place), key=lambda item: -len(item[0])):
            with contextlib.suppress(OSError):
                os.rmdir(directory)
    if missing:
        logging.getLogger(__name__).warning('Coconut could not put back the held folders %s of %s. They are gone from %s.',
                                            missing, path, place)
    return missing


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


def holders(path, *, detail=False):
    """Return the IDs of the processes that have their cwd or an open file in the path. See holders_by_path for detail."""
    return holders_by_path({path}, detail=detail)[str(path)]


# Coconut waits this long for orphaned processes to end after SIGTERM.
STOP_SECONDS = 5


def holders_by_path(paths, *, detail=False):
    """Return, for each path, the IDs of the processes that have their cwd or an open file in it. Use one lsof scan.

    With detail, each path maps each ID to True when the process holds only its cwd there.
    """
    try:
        result = subprocess.run(['lsof', '-n', '-P', '-w', '-F', 'pn'], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Coconut cannot check open files without lsof.') from None
    # A failed lsof can print part of the list. A missing holder would release a busy path, so require success.
    if result.returncode or not result.stdout:
        raise ValueError('lsof did not list all open files.')
    # Two paths can share one resolved form, such as /tmp and /private/tmp on macOS. Keep every path for each form.
    prefixes = {}
    for path in paths:
        for form in {str(path), str(Path(path).resolve())}:
            prefixes.setdefault(os.fsencode(form), set()).add(str(path))
    held, pid, descriptor = {str(path): {} for path in paths}, None, None
    for line in result.stdout.splitlines():
        if line.startswith(b'p'):
            pid = int(line[1:])
        elif line.startswith(b'f'):
            descriptor = line[1:]
        elif line.startswith(b'n'):
            # Look up the name and each of its parent folders, so the cost does not grow with the number of paths.
            name = line[1:]
            while name:
                for owner in prefixes.get(name, ()):
                    held[owner][pid] = held[owner].get(pid, True) and descriptor == b'cwd'
                cut = name.rfind(b'/')
                if cut <= 0:
                    break
                name = name[:cut]
    return held if detail else {path: set(pids) for path, pids in held.items()}


def in_use(path):
    """Return True when a process has an open file in the path, or has its cwd there and is not a shared service."""
    return bool(blocking(holders(path, detail=True), path))


def blocking(held, path, table=None):
    """Return the holders that keep a path. `held` maps each process ID to True when it holds only its cwd there.

    A shared service, such as the adb server, can keep the cwd of the turn that started it. It serves every
    outcome and outlives the worktree, so its cwd does not keep the worktree. Coconut never stops it.
    An open file still keeps the path, because the removal would break the service.
    """
    if not held:
        return set()
    table = process_table() if table is None else table
    exempt = {pid for pid, cwd_only in held.items() if cwd_only and pid in table and shared(table[pid][2], pid)}
    if exempt:
        logging.getLogger(__name__).warning('Shared services %s have their cwd in %s. They do not keep it.', sorted(exempt), path)
    return set(held) - exempt


def elapsed_seconds(value):
    """Convert a `ps` etime value, [[dd-]hh:]mm:ss, to seconds."""
    days, _, clock = value.rpartition('-')
    seconds = 0
    for part in clock.split(':'):
        seconds = seconds * 60 + int(part)
    return (int(days) if days else 0) * 86400 + seconds


# A shared service can inherit a turn's environment when a turn starts it, for example OrbStack for Docker.
# Stopping it would stop it for every outcome, so Coconut never stops a process under these folders.
# The list names each shared service. A wider folder, such as /Library/ or /Applications/, also holds the JDK,
# Xcode, and the Command Line Tools, whose orphans Coconut must stop. macOS hides the environment of its system
# programs, so the orphan rules already never stop a system daemon.
SHARED_EXECUTABLE_PREFIXES = ('/Library/Developer/PrivateFrameworks/CoreSimulator.framework/',
                              '/Library/Developer/CoreSimulator/')
# These app bundles run shared services, such as the OrbStack VM manager. Their client folders hold programs such
# as docker, orb, and kubectl, which belong to their turn.
SHARED_APP_BUNDLES = {'/Applications/OrbStack.app/': ('Contents/MacOS/bin/', 'Contents/MacOS/xbin/'),
                      '/Applications/Docker.app/': ('Contents/Resources/bin/', 'Contents/Resources/cli-plugins/')}
# launchd_sim runs a booted iOS simulator.
SHARED_EXECUTABLE_NAMES = ('launchd_sim',)
# The adb server serves the emulators of every outcome. An adb client, such as adb logcat, belongs to its turn.
# The server runs as `adb ... fork-server server` or `adb nodaemon server`.
ADB_SERVER_ARGUMENTS = ('fork-server', 'nodaemon')


def process_table():
    """Return each process ID with its parent ID, its age in seconds, and its executable path."""
    try:
        result = subprocess.run(['ps', '-A', '-o', 'pid=,ppid=,etime=,comm='], capture_output=True, text=True,
                                errors='replace', timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Coconut cannot list processes without ps.') from None
    table = {}
    for line in result.stdout.splitlines():
        with contextlib.suppress(ValueError):
            pid, parent, elapsed, executable = line.split(None, 3)
            table[int(pid)] = (int(parent), elapsed_seconds(elapsed), executable.strip())
    return table


def arguments(pid):
    """Return the command line of the process, or None when ps cannot read it."""
    try:
        # -ww keeps the whole command line. A cut line could hide the adb server's argument.
        result = subprocess.run(['ps', '-ww', '-o', 'args=', '-p', str(pid)], capture_output=True, text=True,
                                errors='replace', timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def shared(executable, pid=None, *, unknown=False):
    """Return True for a shared service. An adb process counts only when `pid` shows the adb server.

    `unknown` is the answer when ps cannot read the adb command line. A stop passes True, so Coconut never stops
    the server by mistake. A holder check passes False, so an adb client still keeps its worktree.
    """
    name = executable.rsplit('/', 1)[-1]
    if name == 'adb':
        if pid is None:
            return False
        line = arguments(pid)
        if line is None:
            return unknown
        words = line.split()
        return 'server' in words and any(a in words for a in ADB_SERVER_ARGUMENTS)
    if any(executable.startswith(bundle) and not executable[len(bundle):].startswith(clients)
           for bundle, clients in SHARED_APP_BUNDLES.items()):
        return True
    return executable.startswith(SHARED_EXECUTABLE_PREFIXES) or name in SHARED_EXECUTABLE_NAMES


# Without the outcome marker, Coconut still keeps these programs: a person starts them, and they hold a session.
UNMARKED_KEEP_NAMES = ('tmux', 'screen', 'zellij', 'mosh-server', 'ssh-agent', 'gpg-agent', 'sshd', 'login', 'emacs', 'vim', 'nvim',
                       # Machine-wide daemons and language servers watch or index folders. Other projects use them too.
                       'watchman', 'fseventsd', 'mds', 'mds_stores', 'mdworker', 'bird', 'cloudd', 'Dropbox', 'tsserver',
                       'gopls', 'rust-analyzer', 'sourcekit-lsp', 'pyright', 'pylsp', 'ruby-lsp', 'solargraph')


def working_folder_inside(pid, folder):
    """Return True when the working folder of the process is `folder` or inside it."""
    try:
        result = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    names = [line[1:] for line in result.stdout.splitlines() if line.startswith('n')]
    root = os.path.realpath(folder)
    return any(os.path.realpath(name) == root or os.path.realpath(name).startswith(root + os.sep) for name in names)


def environment_names(pid, mark):
    """Return True when the environment of the process contains the `NAME=value` item `mark`."""
    with contextlib.suppress(OSError):
        return mark.encode() in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    try:
        # On macOS, `ps -E -ww` prints the whole environment after the command for a process of the same user.
        result = subprocess.run(['ps', '-E', '-ww', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return mark in result.stdout.split()


def process_outcomes(probe=None):
    """Return the COCONUT_WORKSPACE_OUTCOME values of the running processes.

    Return None when this system hides process environments. Then no caller can prove that a process ended.
    `probe` is the interpreter that runs workers. macOS shows the environment of some programs only, so on macOS
    Coconut starts that interpreter with a marker and trusts the scan only when the marker shows.
    """
    mark = 'COCONUT_WORKSPACE_OUTCOME='
    proc = Path('/proc')
    if (proc / 'self' / 'environ').exists():
        found = set()
        for environ in proc.glob('[0-9]*/environ'):
            try:
                data = environ.read_bytes()
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                # Workers run as the service user, so another user's process cannot hide a Coconut outcome.
                continue
            except OSError:
                # Another unreadable environment can hide a live outcome.
                return None
            found.update(item[len(mark):].decode(errors='replace') for item in data.split(b'\0')
                         if item.startswith(mark.encode()))
        return found
    marker = mark + 'coconut-probe-' + os.urandom(8).hex()
    try:
        witness = subprocess.Popen([probe or sys.executable, '-c', 'import time; time.sleep(30)'],
                                   env=dict(os.environ, COCONUT_WORKSPACE_OUTCOME=marker[len(mark):]),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    try:
        # On macOS, `ps -E` prints each environment after its command, for processes of the same user.
        # One process with a non-UTF-8 argument must not stop the whole scan.
        result = subprocess.run(['ps', '-E', '-A', '-ww', '-o', 'pid=,command='], capture_output=True, text=True,
                                errors='replace', timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        witness.kill()
        witness.wait()
    found = set()
    for line in result.stdout.splitlines():
        found.update(token[len(mark):] for token in line.split() if token.startswith(mark))
    if marker[len(mark):] not in found:
        return None
    found.discard(marker[len(mark):])
    return found


def running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop(pids, sig, seconds):
    """Send the signal and wait at most `seconds` for the processes to end. Return the processes that remain."""
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)
    deadline = time.monotonic() + seconds
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = {pid for pid in remaining if running(pid)}
    return remaining


def stop_orphans(found, outcome, age, *, marked=True, within=None):
    """Stop the old orphaned processes of the outcome in `found`, and their child processes. Return their IDs.

    An orphan is a process that launchd (PID 1) adopted after its parent ended. launchd also starts apps and agents.
    So Coconut stops only an orphan whose environment names the outcome in COCONUT_WORKSPACE_OUTCOME, with its child processes.
    The worker of each turn sets this variable, and the processes of the turn inherit it.
    When the system does not show the environment of a process, for example a macOS platform binary, Coconut does not stop it.
    It never stops a shared app or system daemon that `shared` names, such as OrbStack.
    A process that does not end after SIGTERM gets SIGKILL.
    With marked=False, the environment check does not apply. Coconut then stops an orphan only when its command line
    names `within`, the path of the worktree, as a dev server that a bot started there does, and never a program in
    UNMARKED_KEEP_NAMES. A daemon that merely holds a file in the worktree does not name it. `found` must hold only
    processes of a worktree of an outcome that is done or idle for long.
    """
    if not found:
        return set()
    table = process_table()
    mark = 'COCONUT_WORKSPACE_OUTCOME=' + outcome
    answers = {}

    def is_shared(pid):
        # shared() can run ps for an adb process. Ask once for each process.
        if pid not in answers:
            answers[pid] = shared(table[pid][2], pid, unknown=True)
        return answers[pid]
    def owned(pid):
        if marked:
            return environment_names(pid, mark)
        if table[pid][2].rsplit('/', 1)[-1] in UNMARKED_KEEP_NAMES or not within:
            return False
        line = arguments(pid)
        # A dev server that a bot started with a relative command, such as `node server.js`, works in the worktree
        # without naming it. A daemon that holds a file there works from another folder, usually /.
        return (line is not None and str(within) in line) or working_folder_inside(pid, within)
    stopped = {pid for pid in found if pid in table and pid != os.getpid() and table[pid][0] == 1
               and table[pid][1] >= age and not is_shared(pid) and owned(pid)}
    while stopped:
        # The parent proves who owns a child process. macOS also hides the environment of a child shell.
        # A shared service stays even as a child. A system program that the orphan started, such as a WebKit
        # process of safaridriver, belongs to the orphan and stops with it.
        children = {pid for pid, (parent, _, _) in table.items()
                    if parent in stopped and pid not in stopped and not is_shared(pid)}
        if not children:
            break
        stopped |= children
    if not stopped:
        return set()
    # A process that survives SIGKILL, or that Coconut cannot signal, still uses the worktree.
    return stopped - stop(stop(stopped, signal.SIGTERM, STOP_SECONDS), signal.SIGKILL, 1)


def archive_entries(path, inside=(), held=()):
    """Return the changes of the worktree and the files that its archive holds. Refuse what an archive cannot hold.

    `inside` names the worktrees inside this one. Coconut archives and removes each of them on its own. `held` names
    the ignored folders that retirement moves aside (see held_folders). The archive does not hold them.
    """
    current = changes(path)
    staged, unstaged, names = current
    if len(staged) + len(unstaged) > PATCH_LIMIT_BYTES:
        raise ValueError('The changes in the worktree are larger than the archive limit.')
    # Git lists a worktree inside this one as an untracked folder.
    names = [name for name in names if not (name.endswith('/') and (path / name.rstrip('/')).resolve() in inside)]
    untracked = 0
    for name in names:
        # Git lists a nested repository as a directory. Removal would delete it without an archive.
        if name.endswith('/') or not ((path / name).is_symlink() or stat.S_ISREG(os.lstat(path / name).st_mode)):
            raise ValueError('The worktree contains a nested repository or a special file.')
        untracked += os.lstat(path / name).st_size
    if untracked > UNTRACKED_LIMIT_BYTES:
        raise ValueError('The untracked files in the worktree are larger than the archive limit.')
    current = (staged, unstaged, names)
    # Sort the entries, so two reads of the same files compare equal. os.walk returns directory order.
    skipped = set(inside) | {(path / name).resolve() for name in held}
    return current, sorted([(UNTRACKED + name, name) for name in names]
                           + [(IGNORED + name, name) for name in ignored_files(path, skipped)])


def check_worktree(path, binding, repository, *, processes=True):
    """Refuse a worktree that Git or a process still uses."""
    check_git(path, binding, repository)
    if processes and in_use(path):
        raise ValueError('A process uses the worktree.')


def remove_nested(root, path, repository, *, check_only=False, finished=None, parent=True):
    """Remove the worktrees that a bot created inside the path. Archive the changes of each one first. Return the archives.

    Hermes subagents and bots create these worktrees, for example to work in parallel or to test a base commit.
    One of them with uncommitted changes would otherwise keep the whole idle parent, and its build folders, on disk.
    Coconut archives the staged, unstaged, untracked, and ignored files of such a worktree in archive/nested/.
    Its branch keeps its commits. Coconut does not restore a nested worktree when the outcome resumes.
    Coconut refuses a nested worktree that it tracks for an outcome, a locked one, one with a Git operation in progress,
    one with a commit that no ref holds, and one whose files an archive cannot hold.
    Coconut checks every nested worktree before it archives or removes any. Under the registry lock, it checks again
    that nothing changed. With check_only, Coconut only checks. `finished(wait)` returns False when the outcome resumed.
    Call this with check_only=False only after the process check of the parent path, which covers the nested folders.
    With parent=False, Coconut does not read the files of the parent again: Git cannot read an orphaned parent
    (see discard_orphan), and that caller archives the files of the parent itself.
    """
    def inspect(record, group):
        """Return None for a clean nested worktree, or the changes to archive."""
        inner = record['path']
        if record['locked']:
            raise ValueError('The worktree contains a locked worktree.')
        git_paths = git(inner, 'rev-parse', '--path-format=absolute', *(a for n in IN_PROGRESS for a in ('--git-path', n)))
        if any(Path(p).exists() for p in git_paths.splitlines()):
            raise ValueError('The worktree contains another worktree with a Git operation in progress.')
        # A patch cannot hold a conflicted file, and git apply cannot restore one.
        if git_output(inner, 'ls-files', '--unmerged', '-z'):
            raise ValueError('The worktree contains another worktree with unmerged files.')
        head = git(inner, 'rev-parse', 'HEAD')
        if not record['branch'] and not git(repository, 'for-each-ref', '--contains', head, '--count=1', '--format=%(refname)',
                                            timeout=CHECKOUT_SECONDS):
            raise ValueError('The worktree contains another worktree with a commit that no ref holds.')
        try:
            current, entries = archive_entries(inner, {r['path'] for r in group if r is not record})
        except ValueError as error:
            # Name the nested checkout, so an operator can find the file.
            raise ValueError(f'The nested worktree {inner} cannot be archived: {error}') from None
        if not (current[0] or current[1] or entries):
            return None
        # git worktree remove --force also deletes a submodule, and an archive cannot hold its files.
        listing = git_output(inner, 'ls-files', '--stage', '-z')
        for entry in (e for e in listing.split(b'\0') if e):
            mode, _, name = entry.partition(b'\t')
            target = inner / os.fsdecode(name)
            # This matches Git's own submodule check, which --force skips: any non-empty gitlink folder.
            if mode.startswith(b'160000') and target.is_dir() and any(target.iterdir()):
                raise ValueError(f'The nested worktree {inner} contains a submodule.')
        # A plan keeps a digest of the patches, not the patches, so many nested worktrees do not fill the memory.
        # The branch is part of the plan, so a branch switch at the same commit also counts as a change.
        return dict(branch=record['branch'], head=head, changes=changes_digest(*current), entries=entries,
                    marks=fingerprint(inner, [name for _, name in entries]))

    def tracked():
        # A failed read must not hide another outcome's worktree, so this read is strict.
        try:
            saved = json.loads((Path(root) / 'worktrees.json').read_text())
        except (OSError, ValueError) as error:
            raise ValueError('Coconut cannot read the worktree registry: ' + str(error)) from None
        if not isinstance(saved, dict):
            raise ValueError('The worktree registry is not valid.')
        return {Path(b['path']).resolve() for b in saved.values() if isinstance(b, dict) and isinstance(b.get('path'), str)}

    def plans():
        records = nested(path, registered(repository))
        owned = tracked()
        for record in records:
            if record['path'] in owned:
                raise ValueError('The worktree contains another worktree.')
        return records, {record['path']: inspect(record, records) for record in records}

    records, planned = plans()
    if check_only or not records:
        return []
    directory = Path(root) / 'archive' / 'nested'
    written = {}
    try:
        for record in records:
            plan = planned[record['path']]
            if plan is None:
                continue
            # mkdir applies the mode only to the last folder, so create each folder.
            directory.parent.mkdir(mode=0o700, exist_ok=True)
            directory.mkdir(mode=0o700, exist_ok=True)
            # The name comes from the content. When a removal fails, the next pass finds the same archive
            # and writes no second copy.
            # The branch is in the key, so a reused archive always names the current branch.
            key = hashlib.sha256(json.dumps([str(record['path']), plan['branch'], plan['head'], plan['changes'],
                                             plan['marks']]).encode()).hexdigest()[:16]
            archive = directory / (record['path'].name + '-' + key + '.tar.gz')
            temporary = directory / ('.' + archive.name + '.tmp')
            written[record['path']] = (temporary, archive)
            if archive.exists():
                continue
            # Read the patches again for this worktree only. They must match the plan.
            try:
                current, entries = archive_entries(record['path'], {r['path'] for r in records if r is not record})
            except ValueError as error:
                # Name the nested checkout, so an operator can find the file.
                raise ValueError(f"The nested worktree {record['path']} cannot be archived: {error}") from None
            if changes_digest(*current) != plan['changes'] or entries != plan['entries']:
                raise ValueError('A worktree inside the worktree changed during retirement.')
            staged, unstaged, _ = current
            write_archive(temporary, dict(branch=record['branch'], head=plan['head'], parent=str(path),
                                          repository=str(repository), path=str(record['path']),
                                          at=stamp(datetime.now(timezone.utc))),
                          record['path'], dict(zip(PATCHES, (staged, unstaged))), plan['entries'])
        if written:
            if finished and not finished(True):
                raise ValueError('The outcome resumed during retirement.')
            if in_use(path):
                raise ValueError('A process uses the worktree.')
        # acquire() registers a new binding under this lock, so no outcome can claim a nested worktree meanwhile.
        with (Path(root) / '.workspace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # This read repeats the diffs under the lock, as retire() does for the parent. An edit to a tracked
            # file shows only in the diff, not in the file marks.
            again, checked = plans()
            # The plans hold the head, the changes, the archived names, and their inode marks.
            if checked != planned:
                raise ValueError('A worktree inside the worktree changed during retirement.')
            # The parent can change while Coconut writes the archives. Check its own files again, so a parent that
            # cannot retire keeps its nested worktrees.
            if parent:
                archive_entries(path, {r['path'] for r in again})
            if written and finished and not finished(False):
                raise ValueError('The outcome resumed during retirement.')
            for inner, (temporary, archive) in written.items():
                if temporary.exists():
                    temporary.replace(archive)
                # --force deletes the changes, so the archive must exist.
                if not archive.is_file():
                    raise ValueError(f'The archive of nested worktree {inner} is missing.')
                logging.getLogger(__name__).warning('Coconut archived the changes of nested worktree %s in %s.',
                                                    inner, archive)
            if written:
                sync_directory(directory)
            try:
                # Remove the deepest first. Removing a parent first would delete a worktree inside it without Git.
                for record in sorted(again, key=lambda r: len(r['path'].parts), reverse=True):
                    force = ('--force',) if record['path'] in written else ()
                    try:
                        remove_worktree(repository, record['path'], *force)
                    except GIT_ERRORS as error:
                        # A forced removal can stop part way and still drop the Git record. Name what is left.
                        saved = f" Its changes are in {written[record['path']][1]}." if record['path'] in written else ''
                        left = f" Files remain in {record['path']}." if record['path'].exists() else ''
                        raise ValueError(f"Git did not remove nested worktree {record['path']}: {error}.{left}{saved}") from None
            finally:
                if written:
                    # One pass can write several archives. Keep the folder within its limit, but keep this pass's
                    # archives: they hold the only copy of the changes.
                    trim_nested_archives(root, keep={archive for _, archive in written.values()})
            return [archive for _, archive in written.values()]
    finally:
        for temporary, _ in written.values():
            temporary.unlink(missing_ok=True)


def release(path, outcome, orphan_age, *, marked=True):
    """Stop the old orphaned processes of the outcome in the path. Refuse the path if another process uses it."""
    held = holders(path, detail=True)
    if held and orphan_age is not None:
        stopped = stop_orphans(set(held), outcome, orphan_age, marked=marked, within=path)
        if stopped:
            logging.getLogger(__name__).warning('Coconut stopped orphaned processes %s in worktree %s.', sorted(stopped), path)
        held = {pid: cwd_only for pid, cwd_only in held.items() if pid not in stopped}
    if blocking(held, path):
        raise ValueError('A process uses the worktree.')


def nested(path, records):
    """Return the Git records of the worktrees inside the path whose folders still exist."""
    # A record without its folder holds no files, so removal of the parent cannot lose any.
    return [r for r in records if path in r['path'].parents and r['path'].exists()]


def check_git(path, binding, repository, *, allow_nested=False):
    """Refuse a worktree that Git still uses."""
    records = registered(repository)
    record = next((r for r in records if r['path'] == path), None)
    if not record or record['branch'] != 'refs/heads/' + binding['branch']:
        raise ValueError('Git does not register this worktree on its saved branch.')
    if record['locked']:
        raise ValueError('The worktree is locked.')
    if nested(path, records) and not allow_nested:
        raise ValueError('The worktree contains another worktree.')
    git_paths = git(path, 'rev-parse', '--path-format=absolute', *(a for n in IN_PROGRESS for a in ('--git-path', n)))
    if any(Path(p).exists() for p in git_paths.splitlines()):
        raise ValueError('A Git operation is in progress in the worktree.')


def open_folders(path):
    """Give the owner full access to each real folder in path that lacks it. Never follow a link.

    Some tools make their folders read-only: Go writes its module cache with folders 0555, and a bot can make its
    evidence read-only. Git cannot delete the entries of such a folder. `git worktree remove --force` then stops part
    way, but it still deletes the Git record of the worktree, and an orphaned half checkout stays on the disk.
    """
    def give(folder):
        with contextlib.suppress(OSError, NotImplementedError):
            mode = os.lstat(folder).st_mode
            if stat.S_ISDIR(mode) and mode & stat.S_IRWXU != stat.S_IRWXU:
                if os.chmod in os.supports_follow_symlinks:
                    os.chmod(folder, stat.S_IMODE(mode) | stat.S_IRWXU, follow_symlinks=False)
                else:
                    os.chmod(folder, stat.S_IMODE(mode) | stat.S_IRWXU)
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        return
    give(path)
    # os.walk lists a link to a folder with the folders, but it does not go into it. give() skips a link.
    for directory, folders, _ in os.walk(path):
        for name in folders:
            give(os.path.join(directory, name))


def remove_worktree(repository, path, *options):
    """Remove a linked worktree with Git. Open its read-only folders first, so that Git can delete their files."""
    open_folders(path)
    git(repository, 'worktree', 'remove', *options, str(path), timeout=CHECKOUT_SECONDS)


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
        if (len(members) != len(tar.getmembers())
                or set(members) != {'retirement.json', *patches, *(arcname for arcname, _ in entries)}):
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
    if len({arcname for arcname, _ in entries}) != len(entries):
        raise ValueError('The archive input lists a file more than once.')
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


def retire(root, identity, finished, *, orphan_age=None):
    """Archive and remove the worktree of one finished outcome. Return the archive path.

    The caller supplies `finished(wait)`. It returns True when the outcome is still finished.
    With wait=False it waits only briefly for the service lock, and it raises ValueError when that lock stays busy.
    Coconut calls it with wait=True before it takes the registry lock, and with wait=False under that lock.
    With `orphan_age`, Coconut first stops the orphaned processes of the outcome that are older than that many seconds.
    The Git, lsof, and archive work runs before the registry lock. Under the lock, Coconut only
    repeats the Git checks that do not wait for other processes. When a bot renamed the branch, Coconut first takes
    the registry lock alone to save the new name. It calls no `finished` check in that block.
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
    checked = dict(before)
    path = validate_binding(checked, allow_rename=True).resolve()
    if checked['branch'] != before['branch']:
        # A bot renamed the branch in its last turn, as coconut_workspace asks. Save the new name first,
        # so the checks below and the archive use it.
        with (root / '.workspace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            saved = json.loads(manifest.read_text())
            if saved.get(identity) != before:
                raise ValueError('The workspace binding changed during retirement.')
            saved[identity] = before = dict(before, branch=checked['branch'], _head_log=checked['_head_log'])
            save_bindings(manifest, saved)
    repository = Path(before['repository'])
    check_git(path, before, repository, allow_nested=True)
    if nested(path, registered(repository)):
        # A bot can create worktrees inside its own worktree, for example to test a base commit.
        # Their files would count as ignored files of this worktree, so archive and remove them first.
        # Check first, so a refusal does not follow an orphan stop for nothing.
        # The parent's own size and content checks must run after the removal, because the nested files would count
        # toward them. If one of those refuses, only the nested checkouts are gone. Their changes are in archives,
        # and the next hourly pass retries without them.
        remove_nested(root, path, repository, check_only=True)
        # Check the parent's own files first, without the nested worktrees. A refusal then removes nothing.
        # The parent is read again after the removal, so the archive reflects its files at that time.
        inside = {r['path'] for r in nested(path, registered(repository))}
        archive_entries(path, inside, held_folders(path, inside))
        if not finished(True):
            raise ValueError('The outcome resumed during retirement.')
        release(path, before['outcome'], orphan_age)
        remove_nested(root, path, repository, finished=finished)
        check_git(path, before, repository)
    head = git(path, 'rev-parse', 'HEAD')
    held = held_folders(path)
    current, entries = archive_entries(path, held=held)
    staged, unstaged, _ = current
    # Stop orphans only after every check that does not need them stopped. The hourly pass already stopped the old
    # orphans of every idle outcome, so this stop covers only the processes that became old since.
    if not finished(True):
        raise ValueError('The outcome resumed during retirement.')
    release(path, before['outcome'], orphan_age)
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
                    or any((path / name).is_symlink() or not (path / name).is_dir() for name in held)
                    or sorted(ignored_files(path, {(path / name).resolve() for name in held}))
                    != sorted(name for arcname, name in entries if arcname.startswith(IGNORED))
                    or fingerprint(path, archived) != marks):
                raise ValueError('The worktree changed during retirement.')
            # A dispatch marks its task active before its worker takes this lock. Check the tasks with a short wait.
            if not finished(False):
                raise ValueError('The outcome resumed during retirement.')
            temporary.replace(archive)
            sync_directory(directory)
            binding.pop('retire_failed', None)
            binding['retired'] = dict(at=at, archive=str(archive), head=head, changes=changes_digest(*current))
            if held:
                # The binding names the held folders before they move, so a stop cannot lose them. The restore
                # finds a folder that never moved still in the worktree.
                place = directory / HELD / f'{identity}-{time.time_ns()}'
                binding['retired']['held'] = dict(folder=str(place), names=held)
            save_bindings(manifest, saved)
            if held:
                try:
                    hold(path, place, held)
                except HeldMoveFailed as error:
                    # The undo left some folders in the held place. The record names them, and the checkout stays.
                    # The restore of the next turn adopts the checkout and moves them back.
                    raise ValueError(f'Coconut could not move the held folders {error.stranded} of {path} back: {error}') from None
                except OSError as error:
                    # hold() moved back what it moved. The worktree stays, with all its files.
                    archive.unlink(missing_ok=True)
                    binding.pop('retired')
                    binding['retire_failed'] = dict(at=stamp(datetime.now(timezone.utc)), error=str(error)[:1000], archive=None)
                    save_bindings(manifest, saved)
                    raise ValueError(f'Coconut could not move the large ignored folders of {path}: {error}') from None
                logging.getLogger(__name__).warning('Coconut moved the ignored folders %s of worktree %s to %s. They do not '
                                                    'fit in the archive. The restore moves them back.', held, identity, place)
            try:
                remove_worktree(repository, path, '--force')
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


def rebuildable_folders(path, keep, env=None):
    """Return the ignored folders of the worktree that a build creates again. Skip a folder that holds a path in `keep`."""
    listing = git_output(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z', env=env)
    names = []
    for entry in (os.fsdecode(name) for name in listing.split(b'\0') if name):
        # Git lists an ignored folder with a slash, and a link to a folder without one. Never follow a link.
        folder = path / entry.rstrip('/')
        if (entry.endswith('/') and rebuildable(entry) and not folder.is_symlink()
                and folder.resolve().parent.is_relative_to(path)
                and not any(inner == folder or folder in inner.parents for inner in keep)):
            names.append(entry.rstrip('/'))
    # Git can list a folder and its children when the folder's own .gitignore ignores everything. Keep the top one.
    return sorted(name for name in names if not any(name.startswith(other + '/') for other in names))


def holds_repository(folder):
    """Return True when the folder holds a Git repository or worktree, for example a clone that a bot made in tmp.

    A folder that Coconut cannot read counts as one, because a repository can be inside it.
    """
    def unreadable(error):
        raise error
    try:
        for directory, folders, files in os.walk(folder, onerror=unreadable):
            # A bare clone or a mirror has no .git entry. It holds HEAD, objects, and refs at its top.
            if '.git' in folders or '.git' in files or ('HEAD' in files and {'objects', 'refs'} <= set(folders)):
                return True
    except OSError:
        return True
    return False


def free_rebuildable(root, identity, finished):
    """Delete the rebuildable folders of an idle worktree that retirement refused. Return the deleted names.

    retire() does not archive these folders, because an install or a build creates them again. A worktree that
    retirement refuses, for example for a nested repository or a branch switch, can keep gigabytes of them for days.
    Coconut deletes only the folders that Git ignores and that `rebuildable` names. It keeps a folder that holds a
    worktree, because retirement archives the changes of a nested worktree first. `finished` is the check of retire().
    """
    root = Path(root)
    manifest = root / 'worktrees.json'
    before = json.loads(manifest.read_text()).get(identity)
    if not before or before.get('retired') or not isinstance(before.get('path'), str):
        return []
    path = Path(before['path'])
    if path.is_symlink() or not (path / '.git').is_file():
        return []
    path = path.resolve()
    repository = Path(before['repository']).resolve()
    # validate_binding makes the same check. Another repository can own a worktree at this path now.
    if (Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path or path == repository
            or Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != repository / '.git'):
        return []

    def keep(saved):
        # A folder can hold a worktree of the repository, or the saved path of another outcome.
        records = registered(repository)
        record = next((r for r in records if r['path'] == path), None)
        if not record or record['locked']:
            # check_git refuses these too. A person locks a worktree so that nothing changes it.
            return None
        inner = {r['path'] for r in records}
        return inner | {Path(binding['path']).resolve() for other, binding in saved.items()
                        if other != identity and isinstance(binding, dict) and isinstance(binding.get('path'), str)}
    protected = keep(json.loads(manifest.read_text()))
    if protected is None:
        return []
    # A nested clone or a worktree of another repository can hold uncommitted work, and no archive keeps it.
    # The walk runs before the lock, because a large folder takes a while. The outcome is idle, so no turn adds one.
    names = [name for name in rebuildable_folders(path, protected) if not holds_repository(path / name)]
    if not names or not finished(True) or in_use(path):
        return []
    with (root / '.workspace.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # acquire() and restore() take this lock, so no outcome can claim the worktree during the deletion.
        saved = json.loads(manifest.read_text())
        protected = keep(saved)
        if saved.get(identity) != before or protected is None or not set(names) <= set(rebuildable_folders(path, protected)):
            raise ValueError('The worktree changed while Coconut freed its rebuildable folders.')
        if not finished(False):
            return []
        # A large folder takes minutes to delete, and acquire() waits for this lock. Under the lock, only move each
        # folder out of the worktree. Delete it after the lock. sweep_temporary deletes what a stop left.
        trash = root / TRASH
        if trash.is_symlink():
            # sweep_temporary does not follow a link, so it would never empty the target.
            raise ValueError(f'{trash} is a link. Coconut does not move build folders through it.')
        trash.mkdir(mode=0o700, exist_ok=True)
        moved, deleted = [], []
        for index, name in enumerate(names):
            target = trash / f'{identity}-{time.time_ns()}-{index}'
            try:
                os.rename(path / name, target)
            except OSError as error:
                # One folder that Coconut cannot move must not keep the others. A folder on another disk than the
                # trash stays, because a deletion in place would hold the lock for minutes.
                reason = 'It is on another disk than ' + str(trash) if error.errno == errno.EXDEV else str(error)
                logging.getLogger(__name__).warning('Coconut could not delete build folder %s of %s: %s', name, path, reason)
            else:
                moved.append((name, target))
    for name, target in moved:
        try:
            delete_tree(target)
        except OSError as error:
            # The folder is out of the worktree now. The next sweep tries again.
            logging.getLogger(__name__).warning('Coconut could not delete build folder %s of %s from %s: %s', name, path, target, error)
            continue
        deleted.append(name)
    return sorted(deleted)


def orphan_record(binding):
    """Return the missing Git record of an orphaned worktree, or None when the worktree is not orphaned.

    A linked worktree holds a .git file that names <repository>/.git/worktrees/<name>. Git can delete that record and
    keep the folder, for example when `git worktree remove` stops part way. Every Git command in the folder then fails
    with "not a git repository", so retire, discard and free_rebuildable refuse the folder on every pass. A record that
    exists but names another folder also makes the worktree orphaned: Git gave the name to a new worktree.
    """
    path, repository = binding.get('path'), binding.get('repository')
    if not isinstance(path, str) or not isinstance(repository, str):
        return None
    path, common = Path(path), Path(repository).resolve() / '.git'
    marker = path / '.git'
    try:
        if path.is_symlink() or not path.is_dir() or marker.is_symlink() or not marker.is_file() or not common.is_dir():
            return None
        line = marker.read_text().strip()
        if not line.startswith('gitdir: '):
            return None
        record = path / line[len('gitdir: '):]
        # Resolve only the parent. The record itself is usually gone.
        record = record.parent.resolve() / record.name
        if record.parent != common / 'worktrees':
            return None
        if not os.path.lexists(record):
            return record
        named = Path((record / 'gitdir').read_text().strip())
    except (OSError, UnicodeDecodeError):
        # Coconut cannot tell. A worktree that it cannot read is not orphaned.
        return None
    return None if named.parent.resolve() == path.resolve() else record


def orphan_snapshot(path, head, env, inside=()):
    """Return the unstaged patch and the archive entries of an orphaned worktree, measured against the commit head.

    `env` names a temporary index. Git has no index for the folder anymore, so the snapshot reads the tree of head into
    the temporary index first. The folder can be the remains of a removal that stopped part way, so a tracked file that
    is missing is not a change of the bot. The patch holds only the added and changed lines of the files that exist.
    The archive limits of retire apply, and a nested repository refuses the snapshot, as in retire. Build folders go
    without an archive, so a clone in one of them refuses the snapshot too. `inside` names the nested worktrees that
    Git still knows. Coconut archives them on their own.
    """
    git_output(path, 'read-tree', head, env=env)
    # The patch of the parent cannot hold the files of a submodule, and the removal deletes them. This matches the
    # submodule check of remove_nested: any non-empty gitlink folder.
    listing = git_output(path, 'ls-files', '--stage', '-z', env=env)
    for entry in (e for e in listing.split(b'\0') if e):
        mode, _, name = entry.partition(b'\t')
        target = path / os.fsdecode(name)
        if mode.startswith(b'160000') and target.is_dir() and not target.is_symlink() and any(target.iterdir()):
            raise ValueError('The worktree contains a submodule.')
    unstaged = git_output(path, 'diff', *DIFF, '--diff-filter=d', env=env)
    if len(unstaged) > PATCH_LIMIT_BYTES:
        raise ValueError('The changes in the worktree are larger than the archive limit.')
    listing = git_output(path, 'ls-files', '--others', '--exclude-standard', '-z', strict=True, env=env)
    names = [os.fsdecode(name) for name in listing.split(b'\0') if name]
    names = [name for name in names if not (name.endswith('/') and (path / name.rstrip('/')).resolve() in inside)]
    untracked = 0
    for name in names:
        if name.endswith('/') or not ((path / name).is_symlink() or stat.S_ISREG(os.lstat(path / name).st_mode)):
            raise ValueError('The worktree contains a nested repository or a special file.')
        untracked += os.lstat(path / name).st_size
    if untracked > UNTRACKED_LIMIT_BYTES:
        raise ValueError('The untracked files in the worktree are larger than the archive limit.')
    if any(holds_repository(path / name) for name in rebuildable_folders(path, inside, env)):
        raise ValueError('A build folder of the worktree holds a Git repository.')
    entries = sorted([(UNTRACKED + name, name) for name in names]
                     + [(IGNORED + name, name) for name in ignored_files(path, inside, env)])
    return unstaged, entries


# discard_orphan saves this key in the binding just before it moves the folder away. It names the archive and the commit.
ORPHAN_REMOVAL = 'orphan_removal'


def discard_orphan(root, identity, finished, *, orphan_age=None, urgent=False):
    """Remove an orphaned worktree (see orphan_record) of an idle outcome. Return True when removed.

    The checks of retire apply: the outcome is idle (`finished`), no process uses the folder, the worktree holds no
    nested repository, and its files fit the archive limits. Coconut archives the worktrees that a bot created inside
    it first (remove_nested). The archive in archive/done/ holds the changed tracked files as a patch against the tip of
    the saved branch, the untracked files, and the ignored files that a build does not make again. Build folders go
    without an archive. The binding then counts as discarded: a later turn gets a new checkout of the saved branch, and
    the archived files go back into it. Under the registry lock, Coconut moves the folder to build-trash/, and it
    deletes the folder after the lock. The prune then drops the Git records of the folder and its nested worktrees.
    """
    root = Path(root)
    manifest = root / 'worktrees.json'
    before = json.loads(manifest.read_text()).get(identity)
    if not before or before.get('retired') or not orphan_record(before):
        return False
    failed = (before.get('retire_failed') or {}).get('at')
    if failed and (datetime.now(timezone.utc) - datetime.fromisoformat(failed.replace('Z', '+00:00'))
                   < (timedelta(hours=1) if urgent else RETRY_AFTER)):
        # As in discard: the retry writes the same archive again.
        return False
    path, repository = Path(before['path']).resolve(), Path(before['repository']).resolve()
    tracked = {Path(other['path']).resolve() for name, other in json.loads(manifest.read_text()).items()
               if name != identity and isinstance(other, dict) and isinstance(other.get('path'), str)}
    if any(path in other.parents and other.exists() for other in tracked):
        raise ValueError('The worktree contains a worktree of another outcome.')
    head = git(repository, 'rev-parse', '--verify', 'refs/heads/' + before['branch'] + '^{commit}')
    if not finished(True):
        raise ValueError('The outcome resumed during retirement.')
    release(path, before['outcome'], orphan_age)
    directory = root / 'archive' / 'done'
    archive = temporary = None
    with tempfile.TemporaryDirectory(dir=root, prefix='.orphan-') as scratch:
        def snapshot(name, inside=()):
            env = dict(GIT_DIR=str(repository / '.git'), GIT_WORK_TREE=str(path), GIT_INDEX_FILE=str(Path(scratch) / name))
            unstaged, entries = orphan_snapshot(path, head, env, inside)
            return unstaged, entries, fingerprint(path, [name for _, name in entries])
        inside = {r['path'] for r in nested(path, registered(repository))}
        if inside:
            # A bot can create worktrees inside its own worktree. Git still knows them, so archive them as retire does.
            # Check the files of the parent first, so that a refusal removes nothing.
            remove_nested(root, path, repository, check_only=True)
            snapshot('parent', inside)
            remove_nested(root, path, repository, finished=finished, parent=False)
        first = snapshot('first')
        unstaged, entries, _ = first
        try:
            if unstaged or entries:
                directory.mkdir(parents=True, mode=0o700, exist_ok=True)
                archive = directory / (identity + '.tar.gz')
                if archive.exists():
                    archive = directory / (identity + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.tar.gz')
                temporary = directory / ('.' + archive.name + '.tmp')
                write_archive(temporary, dict(branch=before['branch'], head=head, outcome=before['outcome'],
                    repository=before['repository'], path=before['path'], at=stamp(datetime.now(timezone.utc)), orphaned=True),
                    path, dict(zip(PATCHES, (b'', unstaged))), entries)
            if not finished(True):
                raise ValueError('The outcome resumed during retirement.')
            if in_use(path):
                raise ValueError('A process uses the worktree.')
            with (root / '.workspace.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                saved = json.loads(manifest.read_text())
                binding = saved.get(identity)
                if binding != before or not orphan_record(binding):
                    raise ValueError('The workspace binding changed during retirement.')
                if not finished(False):
                    raise ValueError('The outcome resumed during retirement.')
                if snapshot('again') != first:
                    raise ValueError('The worktree changed during retirement.')
                trash = root / TRASH
                if trash.is_symlink():
                    raise ValueError(f'{trash} is a link. Coconut does not move worktrees through it.')
                trash.mkdir(mode=0o700, exist_ok=True)
                target = trash / f'{identity}-{time.time_ns()}-orphan'
                if temporary:
                    temporary.replace(archive)
                    sync_directory(directory)
                # The binding names the archive before the move, so a stop cannot leave an archive that no binding
                # names. It is not done yet: a stop before the move leaves the folder, and the next pass removes it
                # again. A stop after the move leaves no folder, and the discard of a gone checkout finishes the
                # binding with this archive (see discard_run).
                earlier = (before.get(ORPHAN_REMOVAL) or {}).get('archive')
                binding[ORPHAN_REMOVAL] = dict(at=stamp(datetime.now(timezone.utc)), head=head,
                                               archive=str(archive) if archive else None)
                save_bindings(manifest, saved)
                try:
                    os.rename(path, target)
                except OSError as error:
                    # The folder still holds the files. Keep no archive, and wait as after a refused removal.
                    if archive:
                        archive.unlink(missing_ok=True)
                    binding.pop(ORPHAN_REMOVAL)
                    binding['retire_failed'] = dict(at=stamp(datetime.now(timezone.utc)), error=str(error)[:1000], archive=None)
                    save_bindings(manifest, saved)
                    raise ValueError(f'Coconut could not move the orphaned worktree {path}: {error}') from None
                binding.pop('retire_failed', None)
                binding.pop(ORPHAN_REMOVAL)
                binding['retired'] = dict(at=stamp(datetime.now(timezone.utc)), done=True, head=head,
                                          archive=str(archive) if archive else None, changes=None, orphaned=True)
                save_bindings(manifest, saved)
                if earlier and earlier != str(archive):
                    # A stop before the move left the archive of an earlier attempt. This attempt wrote its own.
                    Path(earlier).unlink(missing_ok=True)
                with contextlib.suppress(*GIT_ERRORS):
                    git(repository, 'worktree', 'prune', timeout=CHECKOUT_SECONDS)
                drop_other_archives(root / 'archive', identity, None, saved)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    try:
        # A large folder takes minutes to delete, so the deletion runs after the lock. sweep_temporary retries it.
        delete_tree(target)
    except OSError as error:
        logging.getLogger(__name__).warning('Coconut could not delete %s: %s', target, error)
    logging.getLogger(__name__).warning('Coconut removed orphaned worktree %s of outcome %s. Git had no record of it. '
                                        'The branch %s keeps the commits. The archive %s keeps the changes.',
                                        identity, before['outcome'], before['branch'], archive)
    return True


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
        if binding['retired'].get('held'):
            return_held(binding['retired']['held'], path)
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


def discard(root, identity, finished, **options):
    """Remove the worktree of an idle outcome. Return True when removed. See discard_run for the rules.

    Any failure removes what the attempt made before it failed: the branch for a detached HEAD and the temporary archive.
    A retry starts from the registry, and it makes its own.
    """
    state = {}
    try:
        removed = discard_run(root, identity, finished, state, **options)
        if removed and state.get('dropped'):
            logging.getLogger(__name__).warning('Coconut discarded the nested repositories %s of worktree %s without an archive.',
                                                state['dropped'], identity)
        return removed
    except BaseException:
        if state.get('made_branch'):
            saved = json.loads((Path(root) / 'worktrees.json').read_text()).get(identity) or {}
            if saved.get('branch') != state['made_branch'] or not saved.get('retired'):
                with contextlib.suppress(*GIT_ERRORS):
                    git(state['repository'], 'branch', '-D', state['made_branch'])
        raise
    finally:
        if state.get('temporary'):
            state['temporary'].unlink(missing_ok=True)


def discard_run(root, identity, finished, state, *, orphan_age=None, merged=False, urgent=False):
    """Remove the worktree of an idle outcome. Return True when removed.

    Coconut calls this when the pull requests of the outcome all merged or closed, when the checkout is gone, and when
    retirement cannot archive an idle worktree exactly (see worktree_retirement.DISCARD_IDLE_SECONDS). The branch keeps
    the commits, and refs/coconut/discarded/<identity>/<time>-<commit> keeps each HEAD, also a detached one. The archive in archive/done/ keeps the staged and unstaged patches, and the untracked files when they
    fit in UNTRACKED_LIMIT_BYTES. Ignored files, such as build output and logs, and nested worktrees go without an
    archive. A branch switch does not keep the worktree. An old orphaned process in the worktree stops, even when its
    environment does not name the outcome. A locked worktree and another process keep the worktree.
    A missing checkout only loses its Git record. A later turn of the outcome gets a new checkout of the saved branch.
    `finished(wait)` is the check of retire.
    """
    root = Path(root)
    manifest = root / 'worktrees.json'
    before = json.loads(manifest.read_text()).get(identity)
    if not before or before.get('retired'):
        return False
    failed = (before.get('retire_failed') or {}).get('at')
    if failed and (datetime.now(timezone.utc) - datetime.fromisoformat(failed.replace('Z', '+00:00'))
                   < (timedelta(hours=1) if urgent else RETRY_AFTER)):
        # A refused removal waits a day, as in retire, because the retry writes the same archive again. A low disk
        # waits an hour: the disk stops every turn.
        return False
    path, repository = Path(before['path']), Path(before['repository']).resolve()
    present = os.path.lexists(path)
    head, branch, archive, temporary = None, before['branch'], None, None
    state['repository'] = repository
    if not present and (before.get(ORPHAN_REMOVAL) or {}).get('head'):
        # A stop interrupted discard_orphan after it moved the folder away. Its record names the archive and the commit.
        head = before[ORPHAN_REMOVAL]['head']
        archive = Path(before[ORPHAN_REMOVAL]['archive']) if before[ORPHAN_REMOVAL].get('archive') else None
    elif not present and (before.get('retire_failed') or {}).get('archive'):
        # The checkout is gone, and an earlier retirement archived its files. That archive is the only copy.
        # Its own record names the commit, so a resume can put the files back.
        archive = Path(before['retire_failed']['archive'])
        with contextlib.suppress(OSError, KeyError, ValueError, tarfile.TarError):
            with tarfile.open(archive) as tar:
                head = json.loads(tar.extractfile('retirement.json').read()).get('head')
    if present:
        if path.is_symlink() or not (path / '.git').is_file():
            raise ValueError('The saved workspace is no longer a linked worktree.')
        if Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != repository / '.git':
            raise ValueError('The saved worktree belongs to another repository.')
        records = registered(repository)
        if any(r['locked'] for r in records if r['path'] == path.resolve() or path.resolve() in r['path'].parents):
            raise ValueError('The worktree is locked.')
        # A worktree that Coconut tracks for another outcome is not a leftover of this one, and no archive keeps its files.
        tracked = {Path(other['path']).resolve() for name, other in json.loads(manifest.read_text()).items()
                   if name != identity and isinstance(other, dict) and isinstance(other.get('path'), str)}
        if any(path.resolve() in other.parents and other.exists() for other in tracked):
            raise ValueError('The worktree contains a worktree of another outcome.')
        if not finished(True):
            raise ValueError('The outcome resumed during retirement.')
        # Only merged or closed work proves that an unmarked old orphan in the worktree is a leftover.
        release(path, before['outcome'], orphan_age, marked=not merged)
        head = git(path, 'rev-parse', 'HEAD')
        branch = git(path, 'branch', '--show-current')
        in_progress = any(Path(p).exists() for p in git(
            path, 'rev-parse', '--path-format=absolute', *(a for n in IN_PROGRESS for a in ('--git-path', n))).splitlines())
        if not branch and in_progress:
            # A rebase or a bisect detaches the HEAD, and the saved branch is the one that it works on. Keep that
            # branch. The ref for the HEAD keeps the commit that the operation had reached.
            branch = before['branch']
        elif not branch:
            # A detached HEAD names no branch. A branch at that commit keeps the work reachable, and the resume checks it out.
            base = 'wt/' + path.name + '-detached'
            branch, number = base, 0
            while subprocess.run(['git', '-C', str(repository), 'rev-parse', '--verify', '--quiet', 'refs/heads/' + branch],
                                 capture_output=True, timeout=60).returncode == 0:
                number += 1
                branch = f'{base}-{number}'
            git(repository, 'branch', branch, head)
            state['made_branch'] = branch
        dropped = state.setdefault('dropped', [])
        head, staged, unstaged, entries = discard_snapshot(path, dropped=dropped)
        # The record names what the archive holds. A leftover checkout that no longer matches it is not removed.
        state['changes'] = snapshot_digest(path, head, staged, unstaged, entries)
        if staged or unstaged or entries:
            directory = root / 'archive' / 'done'
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            archive = directory / (identity + '.tar.gz')
            if archive.exists():
                # An earlier discard of the same worktree kept other work. Do not write over it.
                archive = directory / (identity + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.tar.gz')
            temporary = directory / ('.' + archive.name + '.tmp')
            state['temporary'] = temporary
            write_archive(temporary, dict(branch=branch, head=head, outcome=before['outcome'],
                repository=before['repository'], path=before['path'], at=stamp(datetime.now(timezone.utc))),
                path, dict(zip(PATCHES, (staged, unstaged))), entries)
    # The scan of the open files runs before the registry lock, as in retire. A turn of an idle outcome does not start.
    if present and in_use(path):
        raise ValueError('A process uses the worktree.')
    return finish_discard(root, identity, before, finished, present, path, repository, branch, head, temporary, archive,
                          state.get('changes'))


def finish_discard(root, identity, before, finished, present, path, repository, branch, head, temporary, archive, digest=None):
    """Remove the checkout of discard under the registry lock, and keep its archive only after the removal checks."""
    manifest = root / 'worktrees.json'
    with (root / '.workspace.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(manifest.read_text())
        binding = saved.get(identity)
        if binding != before:
            raise ValueError('The workspace binding changed during retirement.')
        # A dispatch marks its task active before its worker takes this lock. Check the tasks with a short wait.
        if not finished(False):
            raise ValueError('The outcome resumed during retirement.')
        # The binding names the archive before the removal, as in retire, so a stop cannot leave an archive that no
        # binding names.
        retired = dict(at=stamp(datetime.now(timezone.utc)), done=True, head=head, archive=str(archive) if archive else None,
                       changes=digest)
        binding.pop('retire_failed', None)
        binding.pop(ORPHAN_REMOVAL, None)
        binding['branch'], binding['retired'] = branch, retired
        if present:
            # A detached HEAD, a rebase in progress, or a nested worktree can hold commits that no branch holds.
            # A ref keeps each HEAD, so the removal loses no commit.
            keep_heads(repository, identity, [path, *(r['path'] for r in nested(path.resolve(), registered(repository)))])
            if temporary:
                temporary.replace(archive)
                sync_directory(archive.parent)
            save_bindings(manifest, saved)
            try:
                # The removal deletes the files of nested worktrees too. The prune then drops their Git records.
                remove_worktree(repository, path, '--force')
            except GIT_ERRORS as error:
                if os.path.lexists(path) and (path / '.git').is_file():
                    # The checkout still holds the work. Keep no archive, and wait a day before the next attempt.
                    if archive:
                        archive.unlink(missing_ok=True)
                    binding.pop('retired')
                    binding['branch'] = before['branch']
                    binding['retire_failed'] = dict(at=stamp(datetime.now(timezone.utc)), error=str(error)[:1000], archive=None)
                    save_bindings(manifest, saved)
                    raise
                if os.path.lexists(path):
                    # Git deleted part of the checkout. Name the archive, and send turns to the scratch workspace.
                    message = (f'Git stopped while it removed {path}, and part of the checkout remains. '
                               f'The archive {archive} holds its changes. Inspect the path.')
                    binding['retired'] = dict(retired, restore_failed=dict(at=stamp(datetime.now(timezone.utc)), error=message))
                    save_bindings(manifest, saved)
                    raise ValueError(message) from error
                # Git deleted the whole checkout before it stopped. The prune below drops its record.
        with contextlib.suppress(*GIT_ERRORS):
            git(repository, 'worktree', 'prune', timeout=CHECKOUT_SECONDS)
        # An earlier failed removal kept a retire archive for this binding. Keep the one that this binding names now.
        drop_other_archives(root / 'archive', identity, archive if archive and archive.parent == root / 'archive' else None, saved)
        save_bindings(manifest, saved)
    logging.getLogger(__name__).warning('Coconut discarded worktree %s of outcome %s. The branch %s keeps the commits. '
                                        'The archive %s keeps the changes.', identity, before['outcome'], branch, archive)
    return True


def prune_discarded_refs(repository):
    """Delete the refs of HEADs discarded more than NESTED_ARCHIVE_DAYS ago, as for the archives."""
    oldest = time.time() - NESTED_ARCHIVE_DAYS * 86400
    for name in git(repository, 'for-each-ref', '--format=%(refname)', 'refs/coconut/discarded/').splitlines():
        moment = name.rsplit('/', 1)[-1].split('-', 1)[0]
        if moment.isdigit() and int(moment) < oldest:
            git(repository, 'update-ref', '-d', name)


def discard_snapshot(path, *, quiet=False, dropped=None):
    """Return HEAD, the staged and unstaged patches, and the (name in the archive, path) entries that a discard archives.

    The limits apply here: untracked files and ignored files that are too large, and patches that are too large, stay out.
    """
    head = git(path, 'rev-parse', 'HEAD')
    staged, unstaged, names = changes(path)
    log = logging.getLogger(__name__)
    # Git lists a nested repository as a folder, and it goes without an archive.
    files = [name for name in names if not name.endswith('/')
             and ((path / name).is_symlink() or stat.S_ISREG(os.lstat(path / name).st_mode))]
    if dropped is not None:
        # A folder that Git lists here is a nested repository or a clone. No archive keeps it. The caller says so in
        # the log after the removal, so that no deletion is silent, and no entry names a deletion that did not happen.
        dropped.extend(name for name in names if name.endswith('/'))
    if sum(os.lstat(path / name).st_size for name in files) > UNTRACKED_LIMIT_BYTES:
        if not quiet:
            log.warning('Coconut discards the untracked files of %s. They are larger than the archive limit.', path)
        files = []
    entries = sorted((UNTRACKED + name, name) for name in files)
    try:
        # Ignored files that are not build output can matter, for example .env. ignored_files skips the rebuildable
        # folders and refuses more than IGNORED_LIMIT_BYTES. Then the ignored files go without an archive.
        entries += sorted((IGNORED + name, name) for name in ignored_files(path))
    except (ValueError, OSError) as error:
        if not quiet:
            log.warning('Coconut discards the ignored files of %s: %s', path, error)
    if len(staged) + len(unstaged) > PATCH_LIMIT_BYTES:
        # base85 patches of large binaries can be larger than the files, and the archive is written before the removal.
        if not quiet:
            log.warning('Coconut discards the staged and unstaged changes of %s. They are larger than the archive limit.', path)
        staged = unstaged = b''
    return head, staged, unstaged, entries


def snapshot_digest(path, head, staged, unstaged, entries):
    """Return a digest of HEAD, the patches, and the names and contents of the archived files."""
    value = hashlib.sha256()
    for part in (head.encode(), staged, unstaged):
        value.update(hashlib.sha256(part).digest())
    for arcname, name in entries:
        # Each value gets its own digest, so that the end of one file cannot merge with the start of the next entry.
        target = path / name
        if target.is_symlink():
            content = hashlib.sha256(os.readlink(target).encode())
        else:
            content = hashlib.sha256()
            with target.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b''):
                    content.update(chunk)
        value.update(hashlib.sha256(arcname.encode()).digest() + content.digest())
    return value.hexdigest()


def keep_heads(repository, identity, paths):
    """Point refs/coconut/discarded/<identity>/<time>-<commit> at the HEAD of each checkout, before Coconut removes them.

    The time and the commit name the ref, so a later discard adds a ref and replaces none, and the prune can
    count the retention from the discard, not from the commit.
    """
    moment = int(time.time())
    for path in paths:
        with contextlib.suppress(*GIT_ERRORS):
            head = git(path, 'rev-parse', '--verify', 'HEAD')
            git(repository, 'update-ref', f'refs/coconut/discarded/{identity}/{moment}-{head}', head)


SCRATCH_TRASH = '.scratch-trash'


def move_scratch(folder, *, keep=True):
    """Move a scratch workspace to workspaces/.scratch-trash and return its new path. With keep, leave an empty folder.

    A rename is fast, so the caller can hold the service lock, and no turn can start in the folder meanwhile.
    empty_scratch_trash deletes the files later. Coconut never follows a link.
    """
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        return None
    trash = folder.parent / SCRATCH_TRASH
    if trash.is_symlink():
        # empty_scratch_trash does not follow a link, so it would never delete what moved there.
        raise OSError(f'{trash} is a link. Coconut does not move scratch folders through it.')
    trash.mkdir(mode=0o700, exist_ok=True)
    target = trash / f'{folder.name}-{time.time_ns()}'
    os.rename(folder, target)
    if keep:
        folder.mkdir(mode=0o700)
    return target


def delete_tree(path):
    """Delete a file, a link or a folder that Coconut moved to a trash folder. Never follow a link.

    Some tools make their files read-only: Go writes its module cache with folders 0555 and files 0444. No one can
    delete an entry of a folder without owner write, so on a refusal give the owner full access to the folder that
    holds the entry, and try again. Only a real folder inside path changes its mode, never a link or what a link names.
    """
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    # Resolve only the parent. A link that replaces the entry after the check above stays a link, and rmtree refuses it.
    top = os.path.join(os.path.realpath(path.parent), path.name)

    def opened(folder):
        # True when folder is a real folder inside top that lacked owner access and has it now. realpath finds a link
        # anywhere in the path, so the change never reaches a place outside top.
        if (folder != top and not folder.startswith(top + os.sep)) or os.path.realpath(folder) != folder:
            return False
        try:
            mode = os.lstat(folder).st_mode
        except OSError:
            return False
        if not stat.S_ISDIR(mode) or mode & stat.S_IRWXU == stat.S_IRWXU:
            return False
        mode = stat.S_IMODE(mode) | stat.S_IRWXU
        if os.chmod in os.supports_follow_symlinks:
            os.chmod(folder, mode, follow_symlinks=False)
        elif hasattr(os, 'O_PATH') and os.path.isdir('/proc/self/fd'):
            # Linux has no lchmod. Open the folder itself without following a link, and change that exact folder.
            # O_PATH needs no read access, so it also opens a folder that the owner cannot read.
            descriptor = os.open(folder, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.chmod(f'/proc/self/fd/{descriptor}', mode)
            finally:
                os.close(descriptor)
        else:
            return False
        return True

    def retry(function, name, error):
        if isinstance(error, FileNotFoundError):
            # Something else deleted it first, or a retry below deleted the whole folder.
            return
        if isinstance(error, PermissionError):
            if function in (os.unlink, os.remove, os.rmdir) and opened(os.path.dirname(name)):
                function(name)
                return
            # A folder that the owner cannot read or enter. rmtree does not go into it again, so delete it here.
            if function in (os.open, os.scandir, os.lstat) and (opened(os.path.dirname(name)) | opened(name)):
                delete_tree(name)
                return
        raise error

    # Python 3.12 renamed onerror to onexc, and passes the exception instead of sys.exc_info().
    if sys.version_info >= (3, 12):
        shutil.rmtree(top, onexc=retry)
    else:
        shutil.rmtree(top, onerror=lambda function, name, info: retry(function, name, info[1]))


def empty_scratch_trash(root):
    """Delete the scratch workspaces that move_scratch moved, also those that a stop left."""
    trash = Path(root) / SCRATCH_TRASH
    if trash.is_dir() and not trash.is_symlink():
        for item in trash.iterdir():
            try:
                delete_tree(item)
            except OSError as error:
                logging.getLogger(__name__).warning('Coconut could not delete %s: %s', item, error)


def drop_other_archives(directory, identity, keep, saved):
    """Keep one archive for each worktree. Never delete an archive that a binding names."""
    named = {(b.get(key) or {}).get('archive') for b in saved.values()
             for key in ('retired', 'retire_failed', 'residue_recovery', 'digest_recovery')}
    for old in [directory / (identity + '.tar.gz'), *directory.glob(identity + '-*.tar.gz')]:
        if old != keep and str(old) not in named:
            old.unlink(missing_ok=True)


def sweep_temporary(root):
    """Delete archive files that an interrupted retirement did not finish, and old nested and discard archives.

    Run before any retirement starts. Coconut never restores a nested or a discard archive: a discarded worktree
    comes back as a new checkout of its branch. Each is kept NESTED_ARCHIVE_DAYS, within NESTED_ARCHIVE_LIMIT_BYTES
    for each folder, for a person to recover work. A retirement archive in archive/ that no binding names follows the
    same rules. A retirement archive that a binding names stays, because a restore needs it.
    """
    for folder in (Path(root) / 'archive', Path(root) / 'archive' / 'nested', Path(root) / 'archive' / 'done'):
        for temporary in folder.glob('.*.tar.gz.tmp'):
            temporary.unlink(missing_ok=True)
    # free_rebuildable moves build folders here before it deletes them. A stop can leave some.
    empty_scratch_trash(root)
    trash = Path(root) / TRASH
    if trash.is_dir() and not trash.is_symlink():
        try:
            delete_tree(trash)
        except OSError as error:
            logging.getLogger(__name__).warning('Coconut could not empty %s: %s', trash, error)
    trim_nested_archives(root)
    named = named_retirement_archives(root)
    # Without the registry, Coconut cannot tell which archive a restore needs. Then all of them stay.
    if named is not None:
        trim_nested_archives(root, folder='.', keep=named)
    held = Path(root) / 'archive' / HELD
    if held.is_dir() and not held.is_symlink():
        for folder in held.iterdir():
            with contextlib.suppress(FileNotFoundError):
                if folder.lstat().st_mtime >= time.time() - NESTED_ARCHIVE_DAYS * 86400:
                    continue
                try:
                    delete_tree(folder)
                except OSError as error:
                    logging.getLogger(__name__).warning('Coconut could not delete %s: %s', folder, error)
                else:
                    logging.getLogger(__name__).warning('Coconut deleted held folder %s after %s days.', folder, NESTED_ARCHIVE_DAYS)
    # A binding names its discard archive, and a person needs it to recover the work. Only the age rule removes it.
    named = named_discard_archives(root)
    trim_nested_archives(root, folder='done', keep=named)
    kept = []
    for archive in named:
        with contextlib.suppress(FileNotFoundError):
            status = archive.stat()
            if status.st_mtime < time.time() - NESTED_ARCHIVE_DAYS * 86400:
                archive.unlink()
            else:
                kept.append((status.st_mtime, status.st_size, archive))
    # A binding names a discard archive, and a person needs it to recover the work, so it gets a larger limit than an
    # unnamed archive. It is still a limit: the oldest go first when the named archives together exceed it.
    total = sum(size for _, size, _ in kept)
    for moment, size, archive in sorted(kept):
        if total <= DISCARD_ARCHIVE_LIMIT_BYTES:
            break
        archive.unlink(missing_ok=True)
        logging.getLogger(__name__).warning('Coconut deleted archive %s to keep the named archives within %s bytes.',
                                            archive, DISCARD_ARCHIVE_LIMIT_BYTES)
        total -= size


def named_discard_archives(root):
    """Return the discard archives that a binding still names, for a worktree that has not resumed.

    An orphan removal that a stop interrupted names its archive in ORPHAN_REMOVAL. That archive can be the only copy of
    the changes, so it counts as named too.
    """
    try:
        saved = json.loads((Path(root) / 'worktrees.json').read_text())
    except (OSError, ValueError):
        return set()
    named = set()
    for binding in saved.values():
        if not isinstance(binding, dict):
            continue
        retired, pending = binding.get('retired'), binding.get(ORPHAN_REMOVAL)
        if isinstance(retired, dict) and retired.get('done') and retired.get('archive'):
            named.add(Path(retired['archive']))
        if isinstance(pending, dict) and pending.get('archive'):
            named.add(Path(pending['archive']))
    return named


def named_retirement_archives(root):
    """Return the resolved paths of the archives that a binding names, or None when the registry cannot be read.

    A retired, a failed, an interrupted, a handed-off, and a recovered worktree each name their archive.
    """
    try:
        saved = json.loads((Path(root) / 'worktrees.json').read_text())
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict):
        return None
    named = set()
    for binding in saved.values():
        if not isinstance(binding, dict):
            continue
        for key in ('retired', 'retire_failed', ORPHAN_REMOVAL, 'handoff', 'residue_recovery', 'digest_recovery'):
            record = binding.get(key)
            if isinstance(record, dict) and isinstance(record.get('archive'), str) and record['archive']:
                named.add(Path(record['archive']).resolve())
    return named


def trim_nested_archives(root, keep=(), *, folder='nested'):
    """Delete the archives in archive/<folder> older than NESTED_ARCHIVE_DAYS, then the oldest over NESTED_ARCHIVE_LIMIT_BYTES.

    The archives in `keep` and those younger than NESTED_ARCHIVE_KEEP_SECONDS stay, and count toward the limit.
    `keep` can name an archive by its resolved path. The folder '.' is archive/ itself.
    """
    oldest, kept, total = time.time() - NESTED_ARCHIVE_DAYS * 86400, [], 0
    for archive in (Path(root) / 'archive' / folder).glob('*.tar.gz'):
        if archive in keep or archive.resolve() in keep:
            with contextlib.suppress(FileNotFoundError):
                total += archive.stat().st_size
            continue
        with contextlib.suppress(FileNotFoundError):
            status = archive.stat()
            if status.st_mtime < oldest:
                archive.unlink()
                logging.getLogger(__name__).warning('Coconut deleted archive %s after %s days.', archive,
                                                    NESTED_ARCHIVE_DAYS)
            else:
                kept.append((status.st_mtime, status.st_size, archive))
    total += sum(size for _, size, _ in kept)
    young = time.time() - NESTED_ARCHIVE_KEEP_SECONDS
    for moment, size, archive in sorted(kept):
        if total <= NESTED_ARCHIVE_LIMIT_BYTES or moment >= young:
            break
        archive.unlink(missing_ok=True)
        logging.getLogger(__name__).warning('Coconut deleted archive %s to keep the folder within %s bytes.',
                                            archive, NESTED_ARCHIVE_LIMIT_BYTES)
        total -= size


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


def archive_inventory(tar):
    """Validate the entire archive before writing; coalesce only identical legacy entries.

    Compare headers as well as bytes. Cross-prefix collisions are never duplicates:
    they disagree about whether Git ignored a path. No file or link can be an ancestor
    of another entry, and control members must occur exactly once.
    """
    controls, entries, duplicates = {}, {}, []
    for member in tar.getmembers():
        if member.name in ('retirement.json', *PATCHES):
            if member.name in controls or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.sparse is not None:
                raise ValueError('The archive has an invalid control entry: ' + member.name)
            controls[member.name] = member
            continue
        prefix = next((p for p in (UNTRACKED, IGNORED) if member.name.startswith(p)), None)
        if not prefix:
            raise ValueError('The archive contains an unexpected entry: ' + member.name)
        name = member.name[len(prefix):]
        if any(part in ('', '.', '..') or part.casefold() == '.git' for part in name.split('/')):
            raise ValueError('The archive contains an unsafe path: ' + name)
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.SYMTYPE) or member.sparse is not None:
            raise ValueError('The archive contains an unexpected entry for ' + name + '.')
        previous = entries.get(name)
        if previous is not None:
            fields = ('name', 'type', 'mode', 'size', 'linkname', 'uid', 'gid', 'uname', 'gname', 'mtime', 'pax_headers')
            same = all(getattr(previous, field) == getattr(member, field) for field in fields)
            if same and member.isfile():
                same = file_digest(tar.extractfile(previous)) == file_digest(tar.extractfile(member))
            if not same:
                raise ValueError('The archive contains conflicting entries for ' + name + '.')
            duplicates.append((member, name))
        else:
            entries[name] = member
    if set(controls) != {'retirement.json', *PATCHES}:
        raise ValueError('The archive is missing its control entries.')
    for name in entries:
        if any(parent.as_posix() in entries for parent in Path(name).parents):
            raise ValueError('The archive contains overlapping paths: ' + name)
    return [(member, name) for name, member in entries.items()], duplicates


def archived_members(tar):
    return iter(archive_inventory(tar)[0])


def occupied(path):
    # Hermes can create an empty directory at the path for a native claim. Git checks out into it.
    return path.exists() and (not path.is_dir() or any(path.iterdir()))


def checkout_marks(path, names):
    """Fingerprint archived paths, retaining missing entries so a finishing restore is visible."""
    result = []
    for name in sorted(names):
        try:
            status = os.lstat(path / name)
        except (FileNotFoundError, NotADirectoryError):
            result.append((name, None))
        else:
            result.append((name, status.st_ino, status.st_size, status.st_mtime_ns,
                           status.st_ctime_ns, stat.S_IFMT(status.st_mode)))
    return result


def archived_marks(path, archive):
    """Return (matching fingerprint or None, starting fingerprint) for archived files."""
    with tarfile.open(archive) as tar:
        members = list(archived_members(tar))
        names = [name for _, name in members]
        marks = checkout_marks(path, names)
        if any(len(mark) == 2 for mark in marks):
            # A file appearing during comparison must not make a missing starting file look intact.
            return None, marks
        for member, name in members:
            target = path / name
            if member.issym():
                if not target.is_symlink() or os.readlink(target) != member.linkname:
                    return None, marks
            elif (not target.is_file() or target.is_symlink() or target.stat().st_size != member.size
                    or path_digest(target) != file_digest(tar.extractfile(member))):
                # Same size is not enough. Compare the contents with the archived copy.
                return None, marks
    return marks, marks


def compare_checkouts(bindings):
    """Compare each checkout at the path of a retired binding with its archive. Run without the registry lock.

    The archived files can total hundreds of megabytes, so Coconut reads them before it takes the lock.
    Return a map from each binding, as JSON, to the fingerprint of its archived files, or to None when
    the checkout does not match. Under the lock, unpack accepts a checkout only when its binding and its
    fingerprint did not change.
    """
    checked = {}
    for binding in bindings:
        with contextlib.suppress(*RESTORE_ERRORS):
            retired = binding['retired']
            path = Path(binding['path'])
            if retired.get('restore_failed') or retired.get('done') or not occupied(path):
                continue
            key = json.dumps(binding, sort_keys=True)
            # An error in the comparison refuses the checkout.
            checked[key] = None
            validate_binding(dict(binding))
            checked[key] = archived_marks(path, Path(retired['archive']))
    return checked


def unlocked_registry(root):
    # save_bindings replaces the registry by an atomic rename, so this read needs no lock.
    try:
        saved = json.loads((Path(root) / 'worktrees.json').read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


class DuplicateRestoreFailed(ValueError):
    """A proven duplicate recovery started, but encountered a different restore failure."""


# A recovery moves the files that it finds at the saved path of a retired worktree to archive/residue/ (see recover_residue).
RESIDUE = 'residue'


def occupied_failure(failure, path):
    # The first restore found files at the path and changed nothing. No later recovery has run since.
    return (isinstance(failure, dict) and set(failure) == {'at', 'error'}
            and failure['error'] == f'The path {path} is not empty and does not match the archive.')


def residue_entries(folder, members, held=()):
    """Return the type, size and times of each entry in folder. Refuse what is not evidence of the archived worktree.

    A process of the outcome can write evidence again at the saved path after retirement removed the checkout. Only
    real folders and regular files count, with no Git record and no link. Each file must be in a folder that the
    archive also names at the top of the worktree, such as a folder of QA evidence, or in a folder that retirement
    held (`held`, see held_folders). A folder can be anywhere: it holds no data of its own. A Hermes subagent of the
    outcome can recreate its nested worktree in .worktrees/ after retirement, and Hermes removes it when the subagent
    ends. Only the empty .worktrees/ stays, and the recovery moves it with the rest.
    """
    tops = {name.split('/')[0] for _, name in members if '/' in name} | {name.split('/')[0] for name in held}
    result = []

    def unreadable(error):
        raise ValueError('Coconut cannot read all files at the saved path: ' + str(error)) from error
    for directory, folders, files in os.walk(folder, onerror=unreadable):
        for name in sorted(folders + files):
            relative = (Path(directory) / name).relative_to(folder).as_posix()
            status = os.lstat(Path(directory) / name)
            if name.casefold() == '.git':
                raise ValueError('The saved path holds a Git record.')
            if not (stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode)):
                raise ValueError('The saved path holds a link or a special file: ' + relative)
            if not stat.S_ISDIR(status.st_mode) and ('/' not in relative or relative.split('/')[0] not in tops):
                raise ValueError('The saved path holds files that the archive does not name: ' + relative)
            result.append((relative, stat.S_IFMT(status.st_mode), status.st_ino, status.st_size,
                           status.st_mtime_ns, status.st_ctime_ns))
    if not result:
        raise ValueError('The saved path holds no files.')
    return sorted(result)


def recover_residue(binding, root, persist):
    """Restore a retired worktree whose first restore found newer evidence at its path. Hold the registry lock.

    Only an explicit acquire of the owner calls this, for a binding whose only failure is that occupied path
    (occupied_failure). Coconut checks the archive, the branch, the files at the path (residue_entries) and their
    writers first. It then records the move, moves the folder at the path whole to archive/residue/, and restores
    the archive into the empty path. The archive stays, and the binding names it and the moved folder in
    `residue_recovery`. A refusal before the move changes nothing. A failure after the move takes the folder back
    when it can, keeps a partial checkout whole next to it instead of deleting it, and marks the failure, so Coconut
    never tries again without a person.
    """
    retired, failure = binding['retired'], binding['retired']['restore_failed']
    path, archive, repository = Path(binding['path']), Path(retired['archive']), Path(binding['repository'])
    if path.is_symlink() or not path.is_dir() or path.resolve() != path or (path / '.git').exists():
        raise ValueError('The saved path is not a plain folder.')
    if archive.is_symlink() or not archive.is_file():
        raise ValueError(f'The archive {archive} is missing.')
    with tarfile.open(archive) as tar:
        members, _ = archive_inventory(tar)
        metadata = json.loads(tar.extractfile('retirement.json').read())
    if (any(metadata.get(key) != binding[key] for key in ('outcome', 'repository', 'path', 'branch'))
            or metadata.get('head') != retired['head'] or metadata.get('at') != retired['at']):
        raise ValueError('The archive belongs to another owner, path, branch, or commit.')
    if git(repository, 'rev-parse', '--verify', 'refs/heads/' + binding['branch']) != retired['head']:
        raise ValueError('The saved branch moved after retirement.')
    # Inside the main checkout, `git -C path` reports the main checkout, so Git's own record of the path decides.
    if any(r['path'] == path or r['branch'] == 'refs/heads/' + binding['branch'] for r in registered(repository)):
        raise ValueError('Git registers a worktree at the saved path or on the saved branch.')
    held = [name for name in (retired.get('held') or {}).get('names') or () if isinstance(name, str) and name]
    before = residue_entries(path, members, held)
    if in_use(path):
        raise ValueError('A process uses the saved path.')
    directory = Path(root) / 'archive' / RESIDUE
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    place = directory / f'{path.name}-{time.time_ns()}'
    checkout = place.with_name(place.name + '-checkout')
    if directory.is_symlink() or os.path.lexists(place) or os.path.lexists(checkout):
        raise ValueError('The residue folder is not safe.')
    # The binding names the folder before the move, so a stop cannot lose it. A later acquire then refuses.
    binding['retired'] = dict(retired, residue=dict(folder=str(place), at=stamp(datetime.now(timezone.utc))))
    try:
        persist()
    except BaseException:
        binding['retired'] = retired
        raise
    try:
        # The checks above take time. A parent folder that became a link since redirects the path.
        if path.is_symlink() or path.resolve() != path:
            raise OSError('the saved path is redirected')
        os.rename(path, place)
    except OSError as error:
        binding['retired'] = retired
        persist()
        raise ValueError(f'Coconut could not move the files at the saved path: {error}') from None

    def give_back():
        # Put the folder back only into a missing or empty path. Never replace other files, and never follow a link.
        with contextlib.suppress(OSError):
            if path.is_symlink() or path.resolve() != path:
                return False
            if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                path.rmdir()
            if not os.path.lexists(path):
                os.rename(place, path)
                return True
        return False
    # A writer can act between the check and the move. Its files moved too, so compare them again.
    try:
        changed = residue_entries(place, members, held) != before or in_use(place)
    except (ValueError, OSError):
        changed = True
    changed_error = 'The files at the saved path changed during the recovery.'
    if changed and give_back():
        # Nothing else happened, so the first failure stays the proof.
        binding['retired'] = retired
        persist()
        raise ValueError(changed_error)
    try:
        if changed:
            raise ValueError(changed_error)
        unpack(dict(binding, retired=dict(retired, restore_failed=None)), {}, aside=checkout)
    except BaseException as error:
        returned = give_back()
        message = str(error)[:1000] + ('' if returned else f' The files of the saved path are in {place}.')
        if os.path.lexists(checkout):
            message += (f' The partial checkout, with any file written during the restore, is in {checkout}. '
                        'Its .git file is renamed to .git-detached.')
        binding['retired'] = dict(retired, restore_failed=dict(
            at=stamp(datetime.now(timezone.utc)), error=message, residue_retry_failed=True,
            duplicate_retry_failed=True, intent_retry_failed=True))
        # Name only what is still outside the path, so a person finds every file.
        kept = {**({} if returned else {'folder': str(place)}),
                **({'checkout': str(checkout)} if os.path.lexists(checkout) else {})}
        if kept:
            binding['retired']['residue'] = kept
        logging.getLogger(__name__).warning('Coconut could not restore %s over its newer files: %s', path, message)
        raise
    binding.pop('retired')
    binding['residue_recovery'] = dict(archive=str(archive), residue=str(place), failure=failure,
                                       at=stamp(datetime.now(timezone.utc)))
    logging.getLogger(__name__).warning('Coconut restored %s from %s. The newer files of the path are in %s.',
                                        path, archive, place)
    validate_binding(binding)


def digest_failure(failure):
    # A first restore put the archive back, and only its last comparison failed. No later recovery has run since.
    return (isinstance(failure, dict) and set(failure) == {'at', 'error'}
            and failure['error'] == 'The restored files do not match the archive.')


def abbreviation_drift(repository, *patches):
    """Return the archived and current lengths of the object names in the index lines of `patches`, or raise.

    Prove from Git's own objects that the repository now abbreviates the archived names with more characters, which
    changes the digest of the same changes (same_patch). The new side of a patch often names a blob that only the
    worktree held, so Git cannot resolve it. Such a name proves nothing, but the old side names blobs of the commit.
    """
    names = sorted({name.decode() for patch in patches for line in patch.split(b'\n')
                    if (match := INDEX_LINE.fullmatch(line)) for name in match.group(1, 2) if name.strip(b'0')})
    found = git_output(repository, 'cat-file', '--batch-check=%(objectname)',
                       data=''.join(name + '\n' for name in names).encode()).decode().splitlines()
    if len(found) != len(names):
        raise ValueError('Git did not look up every object that the archived patches name.')
    # Git prints the full name of a found object, and the name with `missing` or `ambiguous` otherwise.
    full = [(name, line) for name, line in zip(names, found)
            if len(line) in (40, 64) and line.startswith(name) and not line.strip('0123456789abcdef')]
    # --short takes one name. It prints the name with Git's current abbreviation.
    current = [git(repository, 'rev-parse', '--short', line) for _, line in full]
    drift = sorted({(len(name), len(short)) for (name, _), short in zip(full, current) if len(short) > len(name)})
    if not drift:
        raise ValueError('Git abbreviates the archived object names as before.')
    return drift


def recover_digest(binding, root):
    """Restore a retired worktree again whose first restore failed only its last comparison. Hold the registry lock.

    Only an explicit acquire of the owner calls this, for a binding whose only failure is that comparison
    (digest_failure). Before a version that compares with full object names (same_patch), a fetch after retirement
    that made Git abbreviate object names with more characters failed every restore, and the rollback left the path
    empty. Coconut checks that the path is still missing or empty and not redirected, that the archive is the saved
    one, that the branch did not move and no worktree holds it or the path, and that Git now abbreviates the
    archived object names with more characters (abbreviation_drift). A refusal changes nothing. It then restores
    as usual. The archive stays, and the binding names it in `digest_recovery`. Other records of the binding, such
    as `residue_recovery`, stay. A failure keeps a partial checkout whole in archive/residue/, and marks the
    failure, so Coconut never tries again without a person.
    """
    retired, failure = binding['retired'], binding['retired']['restore_failed']
    path, archive, repository = Path(binding['path']), Path(retired['archive']), Path(binding['repository'])
    if path.is_symlink() or path.resolve() != path or occupied(path):
        raise ValueError('The saved path is not missing or empty.')
    if archive.is_symlink() or not archive.is_file():
        raise ValueError(f'The archive {archive} is missing.')
    with tarfile.open(archive) as tar:
        members, _ = archive_inventory(tar)
        metadata = json.loads(tar.extractfile('retirement.json').read())
        staged, unstaged = (tar.extractfile(name).read() for name in PATCHES)
    if (any(metadata.get(key) != binding[key] for key in ('outcome', 'repository', 'path', 'branch'))
            or metadata.get('head') != retired['head'] or metadata.get('at') != retired['at']):
        raise ValueError('The archive belongs to another owner, path, branch, or commit.')
    if changes_digest(staged, unstaged, [name for member, name in members if member.name.startswith(UNTRACKED)]) != retired['changes']:
        raise ValueError('The archive does not hold the saved changes.')
    if git(repository, 'rev-parse', '--verify', 'refs/heads/' + binding['branch']) != retired['head']:
        raise ValueError('The saved branch moved after retirement.')
    if any(r['path'] == path or r['branch'] == 'refs/heads/' + binding['branch'] for r in registered(repository)):
        raise ValueError('Git registers a worktree at the saved path or on the saved branch.')
    drift = abbreviation_drift(repository, staged, unstaged)
    directory = Path(root) / 'archive' / RESIDUE
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    checkout = directory / f'{path.name}-{time.time_ns()}-checkout'
    if directory.is_symlink() or os.path.lexists(checkout):
        raise ValueError('The residue folder is not safe.')
    try:
        unpack(dict(binding, retired=dict(retired, restore_failed=None)), {}, aside=checkout)
    except BaseException as error:
        message = str(error)[:1000]
        if os.path.lexists(checkout):
            message += (f' The partial checkout, with any file written during the restore, is in {checkout}. '
                        'Its .git file is renamed to .git-detached.')
        binding['retired'] = dict(retired, restore_failed=dict(
            at=stamp(datetime.now(timezone.utc)), error=message, digest_retry_failed=True,
            residue_retry_failed=True, duplicate_retry_failed=True, intent_retry_failed=True))
        if os.path.lexists(checkout):
            binding['retired']['residue'] = dict(checkout=str(checkout))
        logging.getLogger(__name__).warning('Coconut could not restore %s again: %s', path, message)
        raise
    binding.pop('retired')
    binding['digest_recovery'] = dict(archive=str(archive), failure=failure, abbreviation=drift,
                                      at=stamp(datetime.now(timezone.utc)))
    logging.getLogger(__name__).warning('Coconut restored %s from %s again. Git abbreviates its object names with '
                                        'more characters since retirement.', path, archive)
    validate_binding(binding)


def intent_failure(failure):
    # PR92's duplicate retry reached the final digest check, then rolled back.
    # Do not admit generic digest failures or another failure of this recovery.
    return (isinstance(failure, dict) and set(failure) == {'at', 'error', 'duplicate_retry_failed'}
            and failure['duplicate_retry_failed'] is True
            and failure['error'] == 'The restored files do not match the archive.')


def apply_unstaged(path, unstaged):
    # Only intent-to-add files can be additions in a working-tree diff. Restore
    # their index intent before extracting the separately archived untracked files.
    # Do not use `apply --intent-to-add`: Git 2.50 can replace the existing index
    # with just those additions. `add -N` preserves staged content and other entries.
    before = set(git_output(path, 'ls-files', '--others', '-z').split(b'\0'))
    git_output(path, 'apply', '--binary', '--whitespace=nowarn', '-', data=unstaged)
    added = [name for name in git_output(path, 'ls-files', '--others', '-z').split(b'\0')
             if name and name not in before]
    if added:
        # Include ignored additions and literal names (including whitespace and
        # pathspec magic), without staging any content or touching other paths.
        git_output(path, '--literal-pathspecs', 'add', '--intent-to-add', '--force',
                   '--pathspec-from-file=-', '--pathspec-file-nul', data=b'\0'.join(added) + b'\0')


def leftover_checkout(binding, path, repository):
    """Say what the restore of a discarded binding does with a linked worktree that it finds at the path.

    'adopt': a restore failed to put the archived changes back, and left a clean checkout on the saved branch.
    'remove': a stop between the record and the removal of the discard left the checkout, and it still holds what the
    archive holds: the same HEAD, patches, and archived files, by content (the digest in the record). The restore finishes the removal, and then it restores as usual: a new
    checkout of the saved branch, and the archived changes on it. A checkout that changed since, by a new edit or by
    a removal that stopped half way, is not removed: the path is not empty, and a person looks at it.
    None: no linked worktree of this repository is there, or it needs a person.
    """
    if path.is_symlink() or not path.is_dir() or not (path / '.git').is_file():
        return None
    try:
        if Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != repository.resolve() / '.git':
            return None
        if (binding['retired'].get('restore_failed') and git(path, 'branch', '--show-current') == binding['branch']
                and git(path, 'status', '--porcelain') == ''):
            return 'adopt'
        if binding['retired'].get('restore_failed'):
            return None
        wanted = binding['retired'].get('changes')
        return 'remove' if wanted and snapshot_digest(path, *discard_snapshot(path, quiet=True)) == wanted else None
    except GIT_ERRORS:
        return None


class ReapplyFailed(ValueError):
    """The archived changes of a discard did not go back into the new checkout."""


def reapply_discard(path, archive, head):
    """Put the changes of a discard archive back into a new checkout of its branch. Keep the archive.

    The patches fit only the commit that they were made on. When the branch moved, or a patch does not apply, the
    checkout stays clean, and a person can recover the work from the archive.
    """
    try:
        if git(path, 'rev-parse', 'HEAD') != head:
            raise ValueError('the branch moved after the discard')
        with tarfile.open(archive) as tar:
            members = {member.name: member for member in tar.getmembers()}
            staged, unstaged = (tar.extractfile(members[name]).read() if name in members else b'' for name in PATCHES)
            if staged:
                git_output(path, 'apply', '--index', '--binary', '--whitespace=nowarn', '-', data=staged)
            if unstaged:
                apply_unstaged(path, unstaged)
            links = []
            for name, member in members.items():
                prefix = next((p for p in (UNTRACKED, IGNORED) if name.startswith(p)), None)
                target = name[len(prefix):] if prefix else name
                if not prefix or os.path.lexists(path / target):
                    continue
                if member.issym():
                    links.append((target, member.linkname))
                elif member.isfile():
                    tar.extract(member.replace(name=target, deep=False), path, filter='data')
            # Create links last, so that no archived file is written through a link.
            for target, linkname in links:
                place_link(path, target, linkname)
    except RESTORE_ERRORS as error:
        with contextlib.suppress(*GIT_ERRORS):
            git(path, 'reset', '-q', '--hard', 'HEAD')
            git(path, 'clean', '-q', '-fd')
        logging.getLogger(__name__).warning('Coconut did not put the changes of %s back into %s: %s. The archive keeps '
                                            'them.', archive, path, error)
        raise ReapplyFailed(f'Coconut could not put the archived changes back: {error}. The archive {archive} keeps them, '
                            'and the checkout is clean.') from None


def unpack(binding, checked, *, retry_failure=None, aside=None):
    retired = binding['retired']
    path, archive, repository = Path(binding['path']), Path(retired['archive']), Path(binding['repository'])
    if path.is_symlink():
        raise ValueError('The saved worktree path is a symbolic link.')
    if retry_failure and occupied(path):
        raise ValueError('A failed restore cannot be retried over an occupied path.')
    if occupied(path):
        # Retirement stopped before Git removed the checkout, or a restore stopped before the registry changed.
        # compare_checkouts read the files without the lock. Here Coconut compares only their fingerprint.
        key = json.dumps(binding, sort_keys=True)
        if key not in checked:
            # A checkout appeared or the binding changed since the unlocked read.
            checked = compare_checkouts([binding])
        elif checked[key] is not None:
            match, starting = checked[key]
            if match is None and checkout_marks(path, [name for name, *_ in starting]) != starting:
                # A partial checkout finished after comparison; retry only when its files changed.
                checked = compare_checkouts([binding])
        record = checked.get(key)
        marks = record[0] if record is not None else None
        try:
            validate_binding(dict(binding))
            if marks is not None:
                with tarfile.open(archive) as tar:
                    staged, unstaged = (tar.extractfile(name).read() for name in PATCHES)
            intact = (marks is not None
                      and git(path, 'rev-parse', 'HEAD') == retired['head']
                      and matches_archive(path, staged, unstaged, retired['changes'])
                      and checkout_marks(path, [name for name, *_ in marks]) == marks)
        except RESTORE_ERRORS:
            intact = False
        if not intact:
            raise ValueError(f'The path {path} is not empty and does not match the archive.')
        if retired.get('held'):
            # A stop after the record left the checkout. Some held folders may have moved already.
            return_held(retired['held'], path)
        return
    created = False
    try:
        with tarfile.open(archive) as tar:
            members, duplicates = archive_inventory(tar)
            metadata = json.loads(tar.extractfile('retirement.json').read())
            staged, unstaged = (tar.extractfile(name).read() for name in PATCHES)
            if (metadata['branch'], metadata['head']) != (binding['branch'], retired['head']):
                raise ValueError('The archive belongs to another branch or commit.')
            if git(repository, 'rev-parse', '--verify', 'refs/heads/' + binding['branch']) != retired['head']:
                raise ValueError('The saved branch moved after retirement.')
            if retry_failure:
                # Old unpack extracted the first regular member, then mistook an
                # identical second member for a pre-existing checkout conflict.
                # Links were deferred and failed differently, so they do not prove this case.
                collision = next((name for member, name in duplicates if member.isfile()), None)
                legacy_intent = intent_failure(retry_failure)
                if (collision is None
                        or not legacy_intent and retry_failure.get('error') != 'The checkout already contains ' + collision + '.'
                        or any(metadata.get(key) != binding[key] for key in ('outcome', 'repository', 'path'))
                        or metadata.get('at') != retired.get('at')):
                    raise ValueError('The saved failure is not an identical legacy archive collision.')
                if legacy_intent:
                    # Independently bind the archived patches to the original digest.
                    # Scope this second recovery to PR92's observed empty staged patch
                    # and no untracked members, with a real Git-parsed unstaged addition.
                    # A failure message alone never proves lost index intent.
                    if (staged or not unstaged or any(member.name.startswith(UNTRACKED) for member, _ in members)
                            or changes_digest(staged, unstaged, []) != retired['changes']
                            or not any(line.startswith(b' create mode ') for line in
                                       git_output(repository, 'apply', '--summary', '-', data=unstaged).splitlines())):
                        raise ValueError('The archive does not prove the legacy intent-to-add mismatch.')
            # Archive validation can take time. Do not roll back someone else's files
            # if the previously empty path became occupied before Git starts.
            if occupied(path) or path.is_symlink() or (retry_failure or aside) and path.resolve() != path:
                raise ValueError('The saved worktree path is no longer empty or has been redirected.')
            # Mark the checkout before Git starts. An interrupted add can leave a registered partial checkout.
            created = True
            git(repository, 'worktree', 'add', str(path), binding['branch'], timeout=CHECKOUT_SECONDS)
            if staged:
                git_output(path, 'apply', '--index', '--binary', '--whitespace=nowarn', '-', data=staged)
            if unstaged:
                apply_unstaged(path, unstaged)
            links = []
            for member, name in members:
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
        if git(path, 'rev-parse', 'HEAD') != retired['head'] or not matches_archive(path, staged, unstaged, retired['changes']):
            raise ValueError('The restored files do not match the archive.')
        if retired.get('held'):
            # Git ignores these folders, so they do not change the digest above. One that aged out is gone, and the
            # log names it.
            return_held(retired['held'], path)
            held = Path(retired['held']['folder'])
            if aside and any(os.path.lexists(held / name) for name in retired['held']['names']):
                # A writer of the path made a held folder in the new checkout first, so return_held kept the held
                # copy. The recovery keeps both instead of reporting success (recover_residue).
                raise ValueError(f'A held folder appeared in the checkout during the restore. Its held copy stays in {held}.')
    except BaseException as error:
        if isinstance(error, HeldMoveFailed) and error.stranded:
            # Held evidence that could not go back to its held place is in the checkout now. It is the only copy, so
            # the checkout stays. A later restore adopts it and moves the other held folders back.
            logging.getLogger(__name__).warning('Coconut kept the checkout %s, because it holds the held folders %s.',
                                                path, error.stranded)
        elif created and aside:
            # A writer of the path can write into the new checkout. Keep the partial checkout whole at `aside`, and
            # drop only Git's record of it (recover_residue).
            with contextlib.suppress(OSError):
                if os.path.lexists(path):
                    os.rename(path, aside)
                # A later checkout of the path reuses the name of Git's record. Detach the kept copy from it, so a Git
                # command in that copy cannot change the live worktree.
                if (aside / '.git').is_file() and not (aside / '.git').is_symlink():
                    os.rename(aside / '.git', aside / '.git-detached')
            with contextlib.suppress(*GIT_ERRORS):
                git(repository, 'worktree', 'prune', timeout=CHECKOUT_SECONDS)
        elif created:
            # Remove only the partial checkout. The archive stays.
            discard_checkout(repository, path)
            if retry_failure and isinstance(error, RESTORE_ERRORS):
                raise DuplicateRestoreFailed(str(error)) from error
        raise


def restore(binding, checked, *, retry_duplicates=False, keep_archive=False, residue_root=None, persist=None):
    """Recreate a retired worktree at its saved path. Hold the registry lock.

    `checked` comes from compare_checkouts before the lock. Without an entry for the binding,
    unpack compares a checkout that already exists at the path while it holds the lock.

    After a successful restore, the worktree holds every archived change, so Coconut deletes the archive,
    unless keep_archive is set.
    A failed restore keeps the archive and marks the binding `restore_failed`. Only an
    explicit same-owner acquire can retry a proven legacy identical-duplicate collision
    or its proven post-PR92 intent-to-add mismatch into a missing or empty path.
    With `residue_root`, that acquire can also restore over the newer evidence that a first restore found at the path
    (recover_residue), and restore again after a last comparison that failed only on longer object names
    (recover_digest). `persist()` saves the registry. All other failures retain the operator fence.
    """
    retired = binding['retired']
    if retired.get('done'):
        # discard kept only the branch. The merged work gets a new checkout of it. archive/done/ keeps the patches.
        path, repository = Path(binding['path']), Path(binding['repository'])
        try:
            leftover = leftover_checkout(binding, path, repository)
            if leftover == 'adopt':
                binding.pop('retired')
                validate_binding(binding)
                return
            if leftover == 'remove':
                # The archive holds what the checkout held. Finish the removal that the stop interrupted.
                git(repository, 'worktree', 'remove', '--force', str(path), timeout=CHECKOUT_SECONDS)
            if path.is_symlink() or occupied(path):
                raise ValueError(f'The path {path} of the discarded work is not empty. Ask the operator to inspect it.')
            git(repository, 'rev-parse', '--verify', 'refs/heads/' + binding['branch'])
            with contextlib.suppress(*GIT_ERRORS):
                git(repository, 'worktree', 'prune', timeout=CHECKOUT_SECONDS)
            holder = next((r['path'] for r in registered(repository) if r['branch'] == 'refs/heads/' + binding['branch']), None)
            if holder:
                # Git allows a branch in one worktree only, and another worktree, often the main one, holds it.
                # A new branch at the saved commit keeps the outcome working, and the old branch stays where it is.
                start = retired.get('head') or binding['branch']
                base, number = 'wt/' + Path(binding['path']).name, 0
                # Never move a branch that exists: it can hold commits that no other ref holds.
                while subprocess.run(['git', '-C', str(repository), 'rev-parse', '--verify', '--quiet',
                                      'refs/heads/' + (base if not number else f'{base}-{number}')],
                                     capture_output=True, timeout=60).returncode == 0:
                    number += 1
                name = base if not number else f'{base}-{number}'
                git(repository, 'branch', name, start)
                # The binding names the new branch only after Git created it, so a failure keeps the saved name.
                binding['branch'] = name
            with contextlib.suppress(*GIT_ERRORS):
                git(repository, 'worktree', 'prune', timeout=CHECKOUT_SECONDS)
            with contextlib.suppress(FileNotFoundError, OSError):
                path.rmdir()
            # The Git calls above take time. Do not remove someone else's files if the path filled meanwhile.
            if path.is_symlink() or occupied(path):
                raise ValueError(f'The path {path} of the discarded work is not empty. Ask the operator to inspect it.')
            try:
                git(repository, 'worktree', 'add', '-q', str(path), binding['branch'], timeout=CHECKOUT_SECONDS)
            except BaseException:
                # An interrupted add can leave a registered partial checkout. Remove it, so the next turn tries again.
                discard_checkout(repository, path)
                raise
            if retired.get('archive'):
                if not Path(retired['archive']).is_file():
                    raise ReapplyFailed(f'The archive {retired["archive"]} of the discarded changes is gone. The checkout is clean.')
                reapply_discard(path, Path(retired['archive']), retired.get('head'))
        except RESTORE_ERRORS as error:
            # The mark sends the turn to the scratch workspace (see chosen), and the bot reads the message from
            # coconut_workspace. A clean checkout stays, and the next restore adopts it.
            message = str(error) if isinstance(error, ReapplyFailed) else f'Coconut could not check out {binding["branch"]} again: {error}'
            binding['retired'] = dict(retired, restore_failed=dict(at=stamp(datetime.now(timezone.utc)), error=message[:1000]))
            raise ValueError(message) from None
        binding.pop('retired')
        validate_binding(binding)
        return
    archive = retired['archive']
    failure = retired.get('restore_failed')
    retry = (retry_duplicates and isinstance(failure, dict) and not failure.get('intent_retry_failed')
             and (intent_failure(failure) or (not failure.get('duplicate_retry_failed')
             and isinstance(failure.get('error'), str)
             and failure['error'].startswith('The checkout already contains '))))
    # One recovery for each binding, so its record keeps naming the archive and the moved folder.
    if (residue_root and retry_duplicates and occupied_failure(failure, Path(binding['path'])) and 'residue' not in retired
            and 'residue_recovery' not in binding):
        try:
            recover_residue(binding, residue_root, persist)
            return
        except RESTORE_ERRORS as error:
            if 'retired' not in binding:
                # The checkout is back. Only its last check failed, as after an ordinary restore below.
                raise
            # A refusal keeps the first failure as it is. A failure after the move marks it (recover_residue).
            failure = binding['retired']['restore_failed']
            if not failure.get('residue_retry_failed'):
                raise ValueError(f'Coconut retired this worktree and could not restore it: {failure["error"]} '
                                 f'Coconut did not restore it over the newer files: {str(error)[:1000]} '
                                 f'The archive {archive} holds its uncommitted changes. The branch {binding["branch"]} '
                                 'holds its commits. Ask the operator to restore it.') from None
    # One recovery for each binding, so its record keeps naming the archive.
    if residue_root and retry_duplicates and digest_failure(failure) and 'digest_recovery' not in binding:
        try:
            recover_digest(binding, residue_root)
            return
        except RESTORE_ERRORS as error:
            if 'retired' not in binding:
                # The checkout is back. Only its last check failed, as after an ordinary restore below.
                raise
            # A refusal keeps the first failure as it is. A failure of the restore marks it (recover_digest).
            failure = binding['retired']['restore_failed']
            if not failure.get('digest_retry_failed'):
                raise ValueError(f'Coconut retired this worktree and could not restore it: {failure["error"]} '
                                 f'Coconut did not restore it again: {str(error)[:1000]} '
                                 f'The archive {archive} holds its uncommitted changes. The branch {binding["branch"]} '
                                 'holds its commits. Ask the operator to restore it.') from None
    if not failure or retry:
        try:
            unpack(binding, checked, retry_failure=failure)
        except RESTORE_ERRORS as error:
            # A rejected recovery must not replace or clear the original failure proof.
            if isinstance(error, DuplicateRestoreFailed) or not failure:
                failure = dict(at=stamp(datetime.now(timezone.utc)), error=str(error)[:1000])
                if isinstance(error, DuplicateRestoreFailed):
                    # Every recovery now preserves intent. Even an identical new
                    # digest failure must not masquerade as the old PR92 failure.
                    failure = dict(failure, duplicate_retry_failed=True, intent_retry_failed=True)
            binding['retired'] = dict(retired, restore_failed=failure)
        else:
            binding.pop('retired')
            validate_binding(binding)
            if not keep_archive:
                Path(archive).unlink(missing_ok=True)
            return
    raise ValueError(f'Coconut retired this worktree and could not restore it: {failure["error"]} '
                     f'The archive {archive} holds its uncommitted changes. The branch {binding["branch"]} '
                     'holds its commits. Ask the operator to restore it.')


def hand_off(saved, identity, outcome, repository, requested, target, successor):
    """Return the retired binding of another outcome at `target`, moved to `outcome` and restored. Hold the registry lock.

    Return None when no retired binding of another outcome names the path. Then the usual one-owner rules apply.
    `successor(binding)` proves from native records that this turn continues that outcome, or raises ValueError.
    A failure changes no binding. The archive always stays.
    """
    owners = [(name, b) for name, b in saved.items()
              if isinstance(b, dict) and isinstance(b.get('path'), str) and Path(b['path']).resolve() == target]
    if len(owners) != 1 or owners[0][1].get('outcome') == outcome or not owners[0][1].get('retired'):
        return None
    name, old = owners[0]
    refused = 'Another outcome owns this worktree, and Coconut cannot hand it to this work item: '
    retired = old['retired']
    if old.get('repository') != str(repository):
        raise ValueError(refused + 'it belongs to another repository.')
    # Name the saved path exactly. A link or another spelling of the path does not count.
    if str(requested) != old['path'] or str(target) != old['path'] or name != old['outcome'] + '-' + identity[len(outcome) + 1:]:
        raise ValueError(refused + 'the path does not match its saved binding.')
    if retired.get('done') or retired.get('restore_failed'):
        raise ValueError(refused + 'its earlier restore failed or it was discarded. Ask the operator to inspect it.')
    archive = Path(retired.get('archive') or '')
    if archive.is_symlink() or not archive.is_file():
        raise ValueError(refused + f'its archive {archive} is missing.')
    try:
        with tarfile.open(archive) as tar:
            metadata = json.loads(tar.extractfile('retirement.json').read())
    except RESTORE_ERRORS as error:
        raise ValueError(refused + f'its archive {archive} cannot be read: {error}') from None
    if (any(metadata.get(key) != old[key] for key in ('outcome', 'repository', 'path', 'branch'))
            or metadata.get('head') != retired.get('head')):
        raise ValueError(refused + 'its archive belongs to another owner, branch, or commit.')
    if git(repository, 'rev-parse', '--verify', 'refs/heads/' + old['branch']) != retired['head']:
        raise ValueError(refused + 'its branch moved after retirement.')
    proof = successor(dict(old))
    # A process of the old outcome can still write. Without a full process scan, nothing proves that it ended.
    found = process_outcomes()
    if found is None or old['outcome'] in found:
        raise ValueError(refused + 'a process of its outcome may still run.')
    moved = dict(old, outcome=outcome)
    try:
        restore(moved, {}, keep_archive=True)
    except ValueError as error:
        raise ValueError(refused + str(error)) from None
    moved['handoff'] = dict(proof, outcome=old['outcome'], archive=str(archive), head=retired['head'],
                            at=stamp(datetime.now(timezone.utc)))
    del saved[name]
    saved[identity] = moved
    return moved


def acquire(root, outcome, repository, existing_worktree=None, *, successor=None):
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
    checked = compare_checkouts([unlocked_registry(root).get(identity)])
    with (root / '.workspace.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = root / 'worktrees.json'
        saved = json.loads(manifest.read_text()) if manifest.exists() else {}
        binding = saved.get(identity)
        if binding:
            if binding.get('outcome') != outcome or binding.get('repository') != str(repository):
                raise ValueError('The saved workspace belongs to another owner or repository.')
            previous = dict(binding)
            try:
                if binding.get('retired'):
                    restore(binding, checked, retry_duplicates=True, residue_root=root,
                            persist=lambda: save_bindings(manifest, saved))
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
            moved = successor and hand_off(saved, identity, outcome, repository, Path(existing_worktree).expanduser(),
                                           target, successor)
            if moved:
                save_bindings(manifest, saved)
                return moved
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
        checked = compare_checkouts(v for v in unlocked_registry(root).values()
                                    if isinstance(v, dict) and v.get('outcome') == outcome)
        with (root / '.workspace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            saved = json.loads(manifest.read_text())
            bindings = [v for v in saved.values() if v['outcome'] == outcome]
            before = json.dumps(saved, sort_keys=True)
            for binding in bindings:
                if binding.get('retired'):
                    # A failed restore does not stop new work. coconut_workspace reports the archive to the bot.
                    with contextlib.suppress(ValueError):
                        restore(binding, checked)
                else:
                    validate_binding(binding, allow_rename=True)
            if json.dumps(saved, sort_keys=True) != before:
                save_bindings(manifest, saved)
            return chosen(root, outcome, bindings)
    return root / outcome
