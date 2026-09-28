"""Keep one native Hermes worktree per outcome and repository."""
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
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
# Coconut deletes an archive of a nested worktree after this many days, and the oldest ones when all of them
# together are larger than NESTED_ARCHIVE_LIMIT_BYTES.
NESTED_ARCHIVE_DAYS = 30
NESTED_ARCHIVE_LIMIT_BYTES = 2 * 1024 ** 3
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


def ignored_files(path, inside=()):
    """Return the ignored files and links to archive. Skip rebuildable folders and the worktrees in `inside`."""
    listing = git_output(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z', strict=True)
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


def busy_paths(paths):
    """Return the paths in which a process that is not a shared service has its cwd or an open file. Use one lsof scan."""
    held = holders_by_path(paths, detail=True)
    busy = {path for path in paths if held[str(path)]}
    if not busy:
        return busy
    table = process_table()
    return {path for path in busy if blocking(held[str(path)], path, table)}


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


def stop_orphans(found, outcome, age):
    """Stop the old orphaned processes of the outcome in `found`, and their child processes. Return their IDs.

    An orphan is a process that launchd (PID 1) adopted after its parent ended. launchd also starts apps and agents.
    So Coconut stops only an orphan whose environment names the outcome in COCONUT_WORKSPACE_OUTCOME, with its child processes.
    The worker of each turn sets this variable, and the processes of the turn inherit it.
    When the system does not show the environment of a process, for example a macOS platform binary, Coconut does not stop it.
    It never stops a shared app or system daemon that `shared` names, such as OrbStack.
    A process that does not end after SIGTERM gets SIGKILL.
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
    stopped = {pid for pid in found if pid in table and pid != os.getpid() and table[pid][0] == 1
               and table[pid][1] >= age and not is_shared(pid) and environment_names(pid, mark)}
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


def archive_entries(path, inside=()):
    """Return the changes of the worktree and the files that its archive holds. Refuse what an archive cannot hold.

    `inside` names the worktrees inside this one. Coconut archives and removes each of them on its own.
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
    return current, sorted([(UNTRACKED + name, name) for name in names]
                           + [(IGNORED + name, name) for name in ignored_files(path, inside)])


def check_worktree(path, binding, repository, *, processes=True):
    """Refuse a worktree that Git or a process still uses."""
    check_git(path, binding, repository)
    if processes and in_use(path):
        raise ValueError('A process uses the worktree.')


def remove_nested(root, path, repository, *, check_only=False, finished=None):
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
                        git(repository, 'worktree', 'remove', *force, str(record['path']), timeout=CHECKOUT_SECONDS)
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


def release(path, outcome, orphan_age):
    """Stop the old orphaned processes of the outcome in the path. Refuse the path if another process uses it."""
    held = holders(path, detail=True)
    if held and orphan_age is not None:
        stopped = stop_orphans(set(held), outcome, orphan_age)
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
        archive_entries(path, {r['path'] for r in nested(path, registered(repository))})
        if not finished(True):
            raise ValueError('The outcome resumed during retirement.')
        release(path, before['outcome'], orphan_age)
        remove_nested(root, path, repository, finished=finished)
        check_git(path, before, repository)
    head = git(path, 'rev-parse', 'HEAD')
    current, entries = archive_entries(path)
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
    """Delete archive files that an interrupted retirement did not finish, and old nested archives.

    Run before any retirement starts. No binding names a nested archive, and Coconut never restores one.
    It is kept NESTED_ARCHIVE_DAYS, within NESTED_ARCHIVE_LIMIT_BYTES, for a person to recover a subagent's work.
    """
    for folder in (Path(root) / 'archive', Path(root) / 'archive' / 'nested'):
        for temporary in folder.glob('.*.tar.gz.tmp'):
            temporary.unlink(missing_ok=True)
    trim_nested_archives(root)


def trim_nested_archives(root, keep=()):
    """Delete nested archives older than NESTED_ARCHIVE_DAYS, then the oldest over NESTED_ARCHIVE_LIMIT_BYTES.

    The archives in `keep` and those younger than NESTED_ARCHIVE_KEEP_SECONDS stay, and count toward the limit.
    """
    oldest, kept, total = time.time() - NESTED_ARCHIVE_DAYS * 86400, [], 0
    for archive in (Path(root) / 'archive' / 'nested').glob('*.tar.gz'):
        if archive in keep:
            with contextlib.suppress(FileNotFoundError):
                total += archive.stat().st_size
            continue
        with contextlib.suppress(FileNotFoundError):
            status = archive.stat()
            if status.st_mtime < oldest:
                archive.unlink()
                logging.getLogger(__name__).warning('Coconut deleted nested archive %s after %s days.', archive,
                                                    NESTED_ARCHIVE_DAYS)
            else:
                kept.append((status.st_mtime, status.st_size, archive))
    total += sum(size for _, size, _ in kept)
    young = time.time() - NESTED_ARCHIVE_KEEP_SECONDS
    for moment, size, archive in sorted(kept):
        if total <= NESTED_ARCHIVE_LIMIT_BYTES or moment >= young:
            break
        archive.unlink(missing_ok=True)
        logging.getLogger(__name__).warning('Coconut deleted nested archive %s to keep the folder within %s bytes.',
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
            if retired.get('restore_failed') or not occupied(path):
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


def unpack(binding, checked, *, retry_failure=None):
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
            intact = (marks is not None
                      and git(path, 'rev-parse', 'HEAD') == retired['head']
                      and changes_digest(*changes(path)) == retired['changes']
                      and checkout_marks(path, [name for name, *_ in marks]) == marks)
        except GIT_ERRORS:
            intact = False
        if not intact:
            raise ValueError(f'The path {path} is not empty and does not match the archive.')
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
            if occupied(path) or path.is_symlink() or retry_failure and path.resolve() != path:
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
        if git(path, 'rev-parse', 'HEAD') != retired['head'] or changes_digest(*changes(path)) != retired['changes']:
            raise ValueError('The restored files do not match the archive.')
    except BaseException as error:
        if created:
            # Remove only the partial checkout. The archive stays.
            discard_checkout(repository, path)
            if retry_failure and isinstance(error, RESTORE_ERRORS):
                raise DuplicateRestoreFailed(str(error)) from error
        raise


def restore(binding, checked, *, retry_duplicates=False):
    """Recreate a retired worktree at its saved path. Hold the registry lock.

    `checked` comes from compare_checkouts before the lock. Without an entry for the binding,
    unpack compares a checkout that already exists at the path while it holds the lock.

    After a successful restore, the worktree holds every archived change, so Coconut deletes the archive.
    A failed restore keeps the archive and marks the binding `restore_failed`. Only an
    explicit same-owner acquire can retry a proven legacy identical-duplicate collision
    or its proven post-PR92 intent-to-add mismatch into a missing or empty path.
    All other failures retain the operator fence.
    """
    retired = binding['retired']
    archive = retired['archive']
    failure = retired.get('restore_failed')
    retry = (retry_duplicates and isinstance(failure, dict) and not failure.get('intent_retry_failed')
             and (intent_failure(failure) or (not failure.get('duplicate_retry_failed')
             and isinstance(failure.get('error'), str)
             and failure['error'].startswith('The checkout already contains '))))
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
                    restore(binding, checked, retry_duplicates=True)
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
    'To name a PR branch, rename the worktree branch with git branch -m <name>. Do not create or switch branches '
    'in the worktree with git checkout -b, git checkout <branch>, or git switch: Coconut then stops this work item '
    'until a person recovers the checkout. '
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


def retired_path(root, outcome, path):
    """Return True when `path` is the saved path of a retired binding of `outcome`. Read without the lock."""
    try:
        saved = json.loads((Path(root) / 'worktrees.json').read_text())
    except (OSError, ValueError):
        return False
    return any(isinstance(b, dict) and b.get('outcome') == outcome and b.get('retired')
               and b.get('path') == str(path) for b in saved.values())
