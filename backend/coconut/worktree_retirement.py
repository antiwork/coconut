"""Retire the worktrees of idle outcomes. An archive keeps their uncommitted changes.

A turn restores a retired worktree before it starts, so an idle outcome that resumes gets its files back.
An idle outcome whose pull requests all merged or closed loses its worktree instead (work_workspace.discard), but only
when its native cards are all done or archived too."""
import contextlib
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import time

from .work_capacity import execution_key
from . import work_workspace

WORKTREE_RETIRE_SECONDS = 6 * 60 * 60
# A process that launchd adopted and that is older than this does not belong to a turn.
ORPHAN_AGE_SECONDS = WORKTREE_RETIRE_SECONDS
# These Coconut task states mean that a turn runs or starts soon. They are Service.ACTIVE without the states
# that wait for a person. A test keeps the two lists in step.
ACTIVE_STATUSES = ('queued', 'working', 'scheduled', 'waiting')
# These native states mean that no worker runs or claims the outcome soon. Another state keeps the worktree.
NATIVE_IDLE_STATUSES = ('done', 'archived', 'blocked', 'triage', 'review', 'todo', 'scheduled')
# Only these native states end the work of an outcome. A scratch workspace holds files that no archive keeps, such as
# notes and raw evidence, so it keeps them while a native card of the outcome is in another state. A discard of a
# worktree also deletes its ignored files and nested worktrees without an archive, so the same rule applies to it.
NATIVE_DONE_STATUSES = ('done', 'archived')
RETIREMENT_INTERVAL_SECONDS = 60 * 60
# Below LOW_DISK_BYTES an outcome counts as idle after this time without a task update, not after
# WORKTREE_RETIRE_SECONDS. A worktree comes back from its branch and its archive when the outcome resumes.
URGENT_IDLE_SECONDS = 15 * 60
# Below LOW_DISK_BYTES a queued task that did not start for this long does not start soon, so it does not keep the
# worktree of its outcome. While a claim is in progress, it does.
URGENT_QUEUED_SECONDS = 12 * 60 * 60
# Below LOW_DISK_BYTES, a worktree that retirement refuses (a branch switch, a nested repository, large ignored files)
# waits this long before the discard. Retirement itself, which archives exactly, has no such wait.
URGENT_DISCARD_IDLE_SECONDS = 2 * 60 * 60
# An idle outcome keeps a worktree that retirement cannot archive exactly for at most this long. Then Coconut discards it:
# the branch keeps the commits, and an archive keeps the patches and the small untracked files.
DISCARD_IDLE_SECONDS = 2 * 24 * 60 * 60
# Below this free disk space, the pass runs every LOW_DISK_INTERVAL_SECONDS, and it discards such a worktree as soon as
# the outcome is idle. A full disk stops the database and every turn, which costs more than the lost build files.
LOW_DISK_BYTES = 20 * 1024 ** 3
LOW_DISK_INTERVAL_SECONDS = 10 * 60
# A bot keeps its own files in bots/<id>/workspace: clones, review checkouts, evidence. Its tools write temporary files
# to bots/<id>/tmp (TMPDIR). Nothing else removes them. An entry that nothing in it changed for this long goes, unless
# a Git checkout in it holds work that no remote has. Below LOW_DISK_BYTES the wait is the shorter one.
BOT_WORKSPACE_STALE_SECONDS = 14 * 24 * 60 * 60
BOT_WORKSPACE_LOW_DISK_STALE_SECONDS = 3 * 24 * 60 * 60
# The folders of bots/<id> that the stale entry cleanup reads.
BOT_FILE_FOLDERS = ('workspace', 'tmp')
# bots/<id>/tmp holds only temporary files, so its entries go after this shorter wait. The shorter wait applies only
# while no turn of the bot runs. During a turn, the tmp folder gets the wait of the workspace folder.
BOT_TMP_STALE_SECONDS = 3 * 24 * 60 * 60
# An empty scratch workspace that nothing changed for this long goes. A turn creates its scratch workspace again.
EMPTY_SCRATCH_SECONDS = 24 * 60 * 60
# One pass deletes at most this many empty scratch workspaces, so it holds the service lock only briefly.
EMPTY_SCRATCH_BATCH = 2000
# A scratch workspace is named by its outcome. execution_key gives a task id, a session id, or a bot id.
SCRATCH_NAME = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|t_[0-9a-f]{8}')
# A pull request in one of these states holds no more work for its outcome.
DONE_PR_STATES = ('merged', 'closed')
PR_URL = re.compile(r'https://github\.com/([^/]+/[^/]+)/pull/\d+')
# An origin remote in https or ssh form, with or without .git.
REMOTE_NAME = re.compile(r'^(?:https://(?:[^@/]+@)?|ssh://git@|git@)github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$')
# These artifact fields name the tasks that a pull request belongs to.
PR_TASK_FIELDS = ('task_id', '_work_origin_task_id', '_review_task_id', '_activity_task_id')
# Before the registry lock, retirement waits this long for the service lock. Under the registry lock, it waits only LOCKED_WAIT_SECONDS.
SERVICE_WAIT_SECONDS = 5
# The service usually holds its lock for milliseconds. A short wait under the registry lock lets retirement finish on
# a busy service. The wait stays short, because the removal trusts the content checks that ran just before it.
LOCKED_WAIT_SECONDS = 0.25
# A worktree that meets a busy service is tried again in the same pass, after this pause, at most BUSY_RETRIES times.
# Some service passes hold the lock for seconds, and the next pass is an hour away. A retry can repeat the archive
# write, so there is one.
BUSY_RETRY_SECONDS = 30
BUSY_RETRIES = 1
BUSY_MESSAGE = 'The service was busy. Coconut tries again in an hour.'


def repositories(saved):
    return {b['repository'] for b in saved.values() if isinstance(b, dict) and isinstance(b.get('repository'), str)}


def repository_name(repository, cache):
    """Return owner/name of the origin remote of a repository, in lower case, or None. Cache it for one pass."""
    if repository not in cache:
        cache[repository] = None
        with contextlib.suppress(*work_workspace.GIT_ERRORS, TypeError):
            match = REMOTE_NAME.match(work_workspace.git(repository, 'remote', 'get-url', 'origin'))
            cache[repository] = match.group(1).lower() if match else None
    return cache[repository]


def tracked_places(saved):
    """Return the resolved paths of the tracked worktrees that still exist."""
    places = set()
    for binding in saved.values():
        if isinstance(binding, dict) and isinstance(binding.get('path'), str):
            with contextlib.suppress(OSError):
                # A binding of a checkout that is gone holds no files.
                if Path(binding['path']).exists():
                    places.add(Path(binding['path']).resolve())
    return places


def holds_tracked(folder, places):
    folder = folder.resolve()
    return any(place == folder or folder in place.parents for place in places)


# An entry waits in the trash folder under this ending until the second walk finds no change. empty_moved never deletes
# such a name, so a crash or a failed undo cannot lose an entry that the walk did not clear.
CHECKING = '.checking'
# put_back leaves an entry that it cannot return in this folder of the parent.
STALE_KEPT = '.stale-kept'
# Coconut names a moved entry <name>-<nanoseconds>. Only such names in a trash folder are its own.
MOVED_NAME = re.compile(r'.+-\d{16,20}')


def empty_moved(parent):
    """Delete the entries that Coconut moved to the trash folder of parent. A bot's own files in it stay."""
    trash = parent / work_workspace.SCRATCH_TRASH
    if trash.is_symlink() or not trash.is_dir():
        return
    for item in trash.iterdir():
        if not MOVED_NAME.fullmatch(item.name):
            continue
        try:
            work_workspace.delete_tree(item)
        except OSError as error:
            logging.getLogger(__name__).warning('Coconut could not delete %s: %s', item, error)


def linked_repositories(folders):
    """Return the main checkouts that own the linked worktrees among folders. Each folder holds a .git file or folder."""
    found = set()
    for folder in folders:
        marker = folder / '.git'
        if marker.is_file():
            with contextlib.suppress(OSError, IndexError, ValueError):
                line = marker.read_text().strip()
                if line.startswith('gitdir: '):
                    # A linked worktree names <repository>/.git/worktrees/<name>. A submodule names another place.
                    record = (folder / line[len('gitdir: '):]).resolve()
                    if record.parent.name == 'worktrees' and record.parent.parent.name == '.git':
                        found.add(record.parent.parent.parent)
    return found


def prune_repositories(repositories_to_prune):
    """Drop the records of the worktrees that went, in each main checkout."""
    for repository in repositories_to_prune:
        if repository.is_dir():
            with contextlib.suppress(*work_workspace.GIT_ERRORS):
                work_workspace.git(repository, 'worktree', 'prune', timeout=work_workspace.CHECKOUT_SECONDS)


def settle_checking(parent, stale_before):
    """Finish the moves that a stop or a failed rename left in the trash folder of parent.

    Walk each entry with the CHECKING name as the second walk does. A clean one gets its plain moved name, so the next
    sweep deletes it. A changed one goes back to its old place, or to STALE_KEPT.
    """
    trash = parent / work_workspace.SCRATCH_TRASH
    if trash.is_symlink() or not trash.is_dir():
        return
    items = [item for item in sorted(trash.iterdir())
             if item.name.endswith(CHECKING) and MOVED_NAME.fullmatch(item.name[:-len(CHECKING)])]
    # A turn can open or enter the entry after the first scan, and an open file leaves no change time.
    try:
        held = work_workspace.holders_by_path({str(item) for item in items}) if items else {}
    except ValueError as error:
        # Without the scan nothing leaves the checking name. The trash of released entries still goes.
        logging.getLogger(__name__).warning('Coconut could not check the entries that wait in %s: %s', trash, error)
        return
    states = {}
    for item in items:
        if held.get(str(item)):
            # A process uses it, so it goes back to its place, as in the pass that moved it.
            original = parent / item.name[:-len(CHECKING)].rsplit('-', 1)[0]
            try:
                where = put_back(item, original)
                logging.getLogger(__name__).warning('Coconut put back %s, because a process uses it (%s).', original, where)
            except OSError as error:
                logging.getLogger(__name__).warning('Coconut could not put back %s from %s: %s', original, item, error)
            continue
        try:
            states[item] = stale_tree(item, stale_before, skip_root=True)
        except OSError as error:
            logging.getLogger(__name__).warning('Coconut could not settle %s: %s', item, error)
    # A worktree goes before its main checkout, and a main checkout waits while a worktree of it waits or is back.
    for item in sorted(states, key=lambda item: states[item] is not None and has_main(item, states[item][1], item)):
        stem = item.name[:-len(CHECKING)]
        state = states[item]
        try:
            if state is not None:
                if has_main(item, state[1], item):
                    if worktrees_outside(item, state[1], item, waiting=False):
                        # A worktree of it lives, and it needs the objects now. Put the main checkout back.
                        where = put_back(item, parent / stem.rsplit('-', 1)[0])
                        logging.getLogger(__name__).warning('Coconut put back %s, because a worktree of it is back (%s).',
                                                            parent / stem.rsplit('-', 1)[0], where)
                        continue
                    if worktrees_outside(item, state[1], item):
                        # A worktree of it waits under the checking name. They leave together, or come back together.
                        continue
                # The worktrees in it go, so their main checkouts forget them.
                repositories = linked_repositories(state[1])
                os.rename(item, item.with_name(stem))
                prune_repositories(repositories)
            else:
                where = put_back(item, parent / stem.rsplit('-', 1)[0])
                logging.getLogger(__name__).warning('Coconut put back %s, because it changed while Coconut moved it (%s).',
                                                    parent / stem.rsplit('-', 1)[0], where)
        except OSError as error:
            logging.getLogger(__name__).warning('Coconut could not settle %s: %s', item, error)


def put_back(target, entry):
    """Undo a move, because the entry changed after the walk. Return the place where it is now.

    target still has the CHECKING name, which empty_moved does not delete. When no rename works, the entry stays there.
    """
    try:
        if not os.path.lexists(entry):
            os.rename(target, entry)
            return entry
    except OSError:
        pass
    # Something took the old path. Keep the entry where nothing deletes it.
    kept = entry.parent / STALE_KEPT
    kept.mkdir(mode=0o700, exist_ok=True)
    os.rename(target, kept / target.name)
    return kept / target.name


def is_repository(folder):
    """Return True when folder is a Git checkout or a bare repository. Its parts belong together, so nothing judges them apart."""
    return (folder / '.git').exists() or all((folder / name).exists() for name in ('HEAD', 'objects', 'refs'))


def changed_at(info):
    """Return when a file or folder last changed. ctime moves on an extract, a copy and a rename, and a user cannot set it."""
    return max(info.st_mtime, info.st_ctime)


def stale_tree(path, stale_before, skip_root=False):
    """Return (bytes, checkouts) when nothing in the tree changed since stale_before, else None. Never follow a link.

    skip_root leaves the time of path itself out. A rename changes it, and a check after a move needs the files only.

    checkouts lists the folders of the tree that hold a Git checkout, and the bare repositories. A fresh file ends the
    walk at once.
    """
    total, checkouts, stack = 0, [], [path]
    while stack:
        current = stack.pop()
        info = os.lstat(current)
        if changed_at(info) >= stale_before and not (skip_root and current == path):
            return None
        total += info.st_blocks * 512
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(current) as entries:
                names = set()
                for entry in entries:
                    names.add(entry.name)
                    stack.append(entry.path)
            inside_git = '.git' in str(current)[len(str(path)):].split(os.sep)
            if '.git' in names or (not inside_git and {'HEAD', 'objects', 'refs'} <= names):
                checkouts.append(Path(current))
    return total, checkouts


def top_changed(path, stale_before):
    """Return True when path or one of its direct entries changed since stale_before. This reads one folder."""
    if changed_at(os.lstat(path)) >= stale_before:
        return True
    if path.is_dir() and not path.is_symlink():
        with os.scandir(path) as entries:
            return any(changed_at(entry.stat(follow_symlinks=False)) >= stale_before for entry in entries)
    return False


def waiting_checking(path):
    """Return True when an entry that was at path waits in the trash folder of its parent under the CHECKING name."""
    trash = path.parent / work_workspace.SCRATCH_TRASH
    if trash.is_symlink() or not trash.is_dir():
        return False
    name = re.compile(re.escape(path.name) + r'-\d{16,20}' + re.escape(CHECKING))
    return any(name.fullmatch(item.name) for item in trash.iterdir())


def has_main(entry, checkouts, moved=None):
    """Return True when a checkout of entry is a main checkout (it has a .git folder). moved is where entry is now."""
    return any(((moved / folder.relative_to(entry)) if moved is not None else folder).joinpath('.git').is_dir()
               for folder in checkouts)


def worktrees_outside(entry, checkouts, moved=None, waiting=True):
    """Return True when a main checkout in entry has a linked worktree that exists outside entry, or when Git cannot tell.

    moved is the place where entry is now, when Coconut moved it. Git then runs there. A worktree that waits in the trash
    under the CHECKING name counts as there, because a put-back or a failed release can leave it needing the objects.
    """
    place = (moved or entry).resolve()
    for folder in checkouts:
        if moved is not None:
            folder = moved / folder.relative_to(entry)
        if not (folder / '.git').is_dir():
            continue
        try:
            for path in linked_worktrees(folder):
                # Before the move, a worktree in the trash belongs to this pass, and its release comes first.
                if (path.exists() or (waiting and moved is not None and waiting_checking(path))) and not (path.resolve() == place or place in path.resolve().parents):
                    return True
        except work_workspace.GIT_ERRORS:
            # Unknown worktrees keep the main checkout, as a failed Git check keeps any checkout.
            return True
    return False


def checkout_keeps(folder):
    """Return why a Git checkout holds work that a remote does not, or None. A failed Git call also keeps it."""
    if not (folder / '.git').exists():
        # A bare repository or a mirror has no remote-tracking branches to compare with.
        return 'it is a bare repository'
    try:
        # Git can refresh the index of a status call. That would make a stale checkout look fresh, so it may not write.
        if work_workspace.git(folder, '--no-optional-locks', 'status', '--porcelain'):
            return 'it has uncommitted files'
        if work_workspace.git(folder, 'rev-list', '--max-count=1', 'HEAD', '--not', '--remotes'):
            return 'it has commits that no remote has'
        # Ignored files such as .env or a local report are in no remote. Build folders (node_modules, tmp, log) do not count.
        if work_workspace.ignored_files(folder):
            return 'it has ignored files that a build does not make again'
        if (folder / '.git').is_dir():
            # The main checkout holds every branch and the stash. A linked worktree shares them, so only it checks these.
            if work_workspace.git(folder, 'rev-list', '--max-count=1', '--branches', '--not', '--remotes'):
                return 'a branch has commits that no remote has'
            if work_workspace.git(folder, 'stash', 'list'):
                return 'it has a stash'
    except work_workspace.GIT_ERRORS as error:
        return f'Git could not check it ({error})'
    return None


def linked_worktrees(folder):
    """Return the paths of the other worktrees of the main checkout in folder."""
    listed = work_workspace.git(folder, 'worktree', 'list', '--porcelain')
    paths = [Path(line[len('worktree '):]) for line in listed.splitlines() if line.startswith('worktree ')]
    return [path for path in paths if path.resolve() != folder.resolve()]


class ServiceBusy(ValueError):
    """The service lock stayed busy during a retirement check."""


class WorktreeRetirement:
    def _finished_outcomes(self, current, *, timeout=-1, only=None, idle=None, ages=None, urgent=False, settled=False):
        """Return the execution IDs whose work is idle, or None when the service lock is busy.

        Work that waits for a person, a failed task, and a blocked native outcome are idle.
        With urgent (a low disk), idle is URGENT_IDLE_SECONDS, and a task that waited long in the queue does not count.
        With settled, every native card of the outcome must be done or archived too. A merged pull request or a
        completed Coconut task does not end a native card that is still scheduled, blocked, or in review.
        Hold the service lock only to read.
        """
        if idle is None:
            idle = URGENT_IDLE_SECONDS if urgent else WORKTREE_RETIRE_SECONDS
        if not self.lock.acquire(timeout=timeout):
            return None
        try:
            groups = {}
            for task in self._all('tasks'):
                if only is None or execution_key(task) == only:
                    groups.setdefault(execution_key(task), []).append(task)
            native = dict(self.kanban.tasks) if self.kanban else {}
            by_native_id = {n['id']: n for n in native.values()}
            finished = set()
            for outcome, tasks in groups.items():
                try:
                    updated = {t['id']: datetime.fromisoformat((t.get('updated_at') or t['created_at']).replace('Z', '+00:00'))
                               .timestamp() for t in tasks}
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                latest = max(updated.values())
                if current - latest < idle:
                    continue

                def starts_soon(task, outcome=outcome):
                    if task['id'] in self.running:
                        return True
                    if task['status'] not in ACTIVE_STATUSES:
                        return False
                    return not (urgent and task['status'] == 'queued' and outcome not in self._kanban_claim_outcomes
                                and current - updated[task['id']] >= URGENT_QUEUED_SECONDS)
                if any(starts_soon(t, outcome) for t in tasks):
                    continue
                states = [(native.get(t['id']) or by_native_id.get(t.get('_native_outcome_id')) or {}).get('status', 'done')
                          for t in tasks]
                if any(state not in (NATIVE_DONE_STATUSES if settled else NATIVE_IDLE_STATUSES) for state in states):
                    continue
                if any(self._background_pending(t['id']) for t in tasks):
                    continue
                finished.add(outcome)
                if ages is not None:
                    # The caller reads how long each finished outcome is idle.
                    ages[outcome] = current - latest
            return finished
        finally:
            self.lock.release()

    def _stop_idle_orphans(self, finished, saved, urgent=False):
        """Stop the old orphaned processes of each idle outcome, in its worktrees and its scratch workspace.

        Retirement can refuse a worktree for another reason, for example a nested worktree with changes, and a
        scratch workspace is never a worktree. Their orphans still hold the disk, the CPU, and the memory, so this
        step runs for every idle outcome. It uses the retirement rules: launchd adopted the process, it is older
        than ORPHAN_AGE_SECONDS, and its environment names the outcome.
        """
        root = self.root / 'workspaces'
        places = {}
        for binding in saved.values():
            if (isinstance(binding, dict) and binding.get('outcome') in finished and not binding.get('retired')
                    and isinstance(binding.get('path'), str)):
                places.setdefault(binding['outcome'], set()).add(binding['path'])
        for outcome in finished:
            # acquire() uses the same rule for a scratch workspace name.
            if '/' not in outcome and outcome not in ('', '.', '..') and (root / outcome).is_dir():
                places.setdefault(outcome, set()).add(str(root / outcome))
        paths = {path for group in places.values() for path in group if Path(path).is_dir()}
        if not paths or self.stopping.is_set() or self.restart_pending:
            return []
        try:
            holders = work_workspace.holders_by_path(paths)
        except ValueError as error:
            logging.getLogger(__name__).warning('Coconut could not check idle outcome processes: %s', error)
            return []
        stopped = []
        for outcome, group in places.items():
            found = set().union(*(holders.get(path, set()) for path in group))
            if not found or self.stopping.is_set() or self.restart_pending:
                continue
            # The outcome can resume while lsof runs. A resumed turn may use an old process, so check again.
            again = self._finished_outcomes(time.time(), timeout=SERVICE_WAIT_SECONDS, only=outcome, urgent=urgent)
            if not again or outcome not in again:
                continue
            try:
                ended = work_workspace.stop_orphans(found, outcome, ORPHAN_AGE_SECONDS)
            except (ValueError, OSError, subprocess.TimeoutExpired) as error:
                logging.getLogger(__name__).warning('Coconut could not stop the orphans of idle outcome %s: %s', outcome, error)
                continue
            if ended:
                logging.getLogger(__name__).warning('Coconut stopped orphaned processes %s of idle outcome %s.',
                                                    sorted(ended), outcome)
                stopped.extend(ended)
        return stopped

    def _done_outcomes(self, finished):
        """Map each finished outcome whose pull requests all merged or closed to their repositories (owner/name).

        Also return the finished outcomes with an open pull request. Return None when the lock is busy.
        """
        if not self.lock.acquire(timeout=SERVICE_WAIT_SECONDS):
            return None
        try:
            keys = {task['id']: execution_key(task) for task in self._all('tasks')}
            states, repositories = {}, {}
            for artifact in self._all('artifacts'):
                if artifact.get('kind') != 'pr':
                    continue
                for outcome in {keys.get(artifact.get(field)) for field in PR_TASK_FIELDS} & set(finished):
                    states.setdefault(outcome, set()).add(artifact.get('pr_state'))
                    match = PR_URL.match(str(artifact.get('url') or ''))
                    if match:
                        repositories.setdefault(outcome, set()).add(match.group(1).lower())
            done = {outcome: repositories.get(outcome, set()) for outcome, found in states.items() if found <= set(DONE_PR_STATES)}
            return done, set(states) - set(done)
        finally:
            self.lock.release()

    def _clear_idle_scratch(self, root, eligible, current, saved, urgent=False, fresh=None):
        """Delete the files in the scratch workspaces (workspaces/<outcome>) of done or long idle outcomes.

        Nothing else removes them, and a bot often clones a repository or builds there. The folder stays, empty, because
        a later turn of the outcome uses it as its working directory. A folder that no task names goes after
        DISCARD_IDLE_SECONDS without changes. A bot's own workspace and a folder that a process uses stay.
        No archive keeps these files, so an outcome keeps its entire scratch workspace while any of its native cards is
        not done or archived. Scheduled, blocked, review, triage, and todo cards are unfinished work. The files stay even
        on a low disk and with no time limit, and that includes clones, dependencies, and build output. This step frees
        no part of that scratch: a tmp folder can hold setup that no build makes again, and a clone can hold work.
        Disk space is the price of keeping unfinished work. The cleanup of build folders in registered worktrees
        (free_rebuildable) is unaffected. `fresh` is the result of a board read earlier in the same pass, if any.
        """
        if not self.lock.acquire(timeout=SERVICE_WAIT_SECONDS):
            return
        try:
            known = {execution_key(task) for task in self._all('tasks')}
            bots = {bot['id'] for bot in self._all('bots')}
        finally:
            self.lock.release()
        # A tracked worktree that lies inside a scratch workspace is not scratch. Keep its folder.
        tracked = tracked_places(saved)
        candidates = []
        for folder in root.iterdir():
            name = folder.name
            if holds_tracked(folder, tracked):
                continue
            if (not SCRATCH_NAME.fullmatch(name) or name in bots or folder.is_symlink() or not folder.is_dir()
                    or (name in known and name not in eligible)):
                continue
            if name not in known and current - folder.stat().st_mtime < DISCARD_IDLE_SECONDS:
                continue
            if any(folder.iterdir()) or name not in known:
                candidates.append(folder)
        if not candidates or self.stopping.is_set() or self.restart_pending:
            return
        # The check below reads native states from the last board snapshot. Read the board again first, without the
        # service lock: the adapter call can wait, and a thread that holds the service lock may wait for the adapter.
        # Without a fresh snapshot, the scratch files of known outcomes stay until the next pass.
        fresh = not any(folder.name in known for folder in candidates) or (
            self._refresh_native_snapshot() if fresh is None else fresh)
        holders = work_workspace.holders_by_path({str(folder) for folder in candidates})
        cleared = []
        for folder in candidates:
            if holders.get(str(folder)) or self.stopping.is_set() or self.restart_pending:
                continue
            name = folder.name
            if name in known and not fresh:
                continue
            if not self.lock.acquire(timeout=SERVICE_WAIT_SECONDS):
                continue
            try:
                # A turn starts under this lock. Check the outcome again, and move the folder before the lock goes.
                # Confirm the idle time that made the outcome eligible, and not only that no task is active now.
                # Only finished native cards release the files, because no archive keeps them.
                again = (self._finished_outcomes(time.time(), only=name, urgent=urgent, settled=True,
                                                idle=URGENT_DISCARD_IDLE_SECONDS if urgent else None)
                         if name in known else {name})
                if not again or name not in again:
                    continue
                work_workspace.move_scratch(folder, keep=name in known)
            except OSError as error:
                logging.getLogger(__name__).warning('Coconut could not clear scratch workspace %s: %s', folder, error)
                continue
            finally:
                self.lock.release()
            cleared.append(name)
        # The deletion runs without the lock.
        work_workspace.empty_scratch_trash(root)
        if cleared:
            logging.getLogger(__name__).warning('Coconut cleared %s idle scratch workspaces.', len(cleared))
            # A bot can create a worktree of another repository in a scratch workspace. Drop the records of the deleted ones.
            for repository in repositories(saved):
                with contextlib.suppress(*work_workspace.GIT_ERRORS):
                    work_workspace.git(repository, 'worktree', 'prune', timeout=work_workspace.CHECKOUT_SECONDS)

    def _remove_empty_scratch(self, root, saved, current):
        """Delete the empty scratch workspaces (workspaces/<outcome>) that nothing changed for EMPTY_SCRATCH_SECONDS.

        _clear_idle_scratch leaves an empty folder, and nothing removed it. A folder of an outcome with an active task
        or a claim stays, and so do a bot's own folder and a folder that a binding names. A turn creates its scratch
        workspace again (providers.py). rmdir deletes only an empty folder, so a file written since the check keeps it.
        """
        # A binding path and each folder above it stay. Compare both spellings, because a path can hold a link.
        named = set()
        for binding in saved.values():
            if isinstance(binding, dict) and isinstance(binding.get('path'), str):
                for path in {Path(binding['path']), Path(os.path.realpath(binding['path']))}:
                    named.update((path, *path.parents))
        real = Path(os.path.realpath(root))
        candidates = []
        for folder in root.iterdir():
            if not SCRATCH_NAME.fullmatch(folder.name) or folder in named or real / folder.name in named:
                continue
            try:
                info = folder.lstat()
                if (not stat.S_ISDIR(info.st_mode) or current - changed_at(info) < EMPTY_SCRATCH_SECONDS
                        or any(folder.iterdir())):
                    continue
            except OSError:
                continue
            candidates.append((changed_at(info), folder))
        if not candidates or self.stopping.is_set() or self.restart_pending:
            return
        if not self.lock.acquire(timeout=SERVICE_WAIT_SECONDS):
            return
        removed = 0
        try:
            # A turn starts under this lock, so no outcome becomes active while the folders go.
            keep = {bot['id'] for bot in self._all('bots')} | set(getattr(self, '_kanban_claim_outcomes', ()))
            keep |= {execution_key(task) for task in self._all('tasks')
                     if task.get('status') in ACTIVE_STATUSES or task['id'] in self.running}
            for _, folder in sorted(candidates)[:EMPTY_SCRATCH_BATCH]:
                if folder.name in keep:
                    continue
                try:
                    folder.rmdir()
                except OSError:
                    continue
                removed += 1
        finally:
            self.lock.release()
        if removed:
            logging.getLogger(__name__).warning('Coconut removed %s empty scratch workspaces.', removed)

    def _bots_with_turns(self):
        """Return the IDs of the bots with a running turn or a claim, or None when Coconut cannot tell."""
        from .service import ServiceError
        if not self.lock.acquire(timeout=SERVICE_WAIT_SECONDS):
            return None
        try:
            busy = set(getattr(self, '_kanban_claim_bots', ()))
            # A task in self.running counts as running, as in _finished_outcomes. The other two keep ended threads.
            live = [('tasks', identity) for identity in self.running]
            for table, workers in (('contexts', self.context_threads), ('tasks', getattr(self, 'gateway_cron_threads', {}))):
                live += [(table, identity) for identity, worker in list(workers.items()) if worker.is_alive()]
            return busy | {self._get(table, identity).get('bot_id') for table, identity in live}
        except ServiceError:
            return None
        finally:
            self.lock.release()

    def _refresh_native_snapshot(self):
        """Read the native board again. Return False when it cannot be read. Call it without the service lock."""
        call = getattr(self.kanban, 'call', None)
        if call is None:
            # No adapter runs, so the saved native states are all that exists.
            return True
        try:
            call({'op': 'snapshot'})
        except (RuntimeError, OSError, ValueError) as error:
            logging.getLogger(__name__).warning('Coconut kept the scratch workspaces and worktrees of idle outcomes, '
                                                'because it could not read the native board: %s', error)
            return False
        return True

    def _clear_stale_bot_workspaces(self, current, saved, low_disk):
        """Delete the entries of bots/<id>/workspace and bots/<id>/tmp that nothing has changed for BOT_WORKSPACE_STALE_SECONDS.

        An entry is a file or a folder of the workspace. A folder that has a newer file is not stale, so Coconut then
        judges the entries inside it, one level down, unless it is a Git repository itself. A Git checkout that holds work
        that no remote has stays (checkout_keeps). A main checkout stays while a worktree of it stays. A process that
        uses an entry, a tracked worktree, the repository of a binding, a work-source bindings file, and a name that
        starts with a dot stay too. A folder that holds one of these protected paths stays, but it does not protect its
        other entries: Coconut judges them as if they were at the level of the folder. Without this rule, one protected
        file kept every old entry next to it, for example weeks of snapshots beside a bindings file. A work-source
        reader keeps its whole entry, because it can read the files next to it.
        An entry of bots/<id>/tmp goes after BOT_TMP_STALE_SECONDS instead, while the bot runs no turn.
        A bot with work in progress is cleaned too: it is busy all day on a busy install, and a rule that skips it would never
        free its disk. A turn can read an old entry without opening it during the pass, and a read changes no time.
        The age, the open-file check, and the Git work check are the protection. Coconut moves a folder to a
        trash folder of its parent first, and deletes it later, outside the lock.
        """
        stale_before = current - (BOT_WORKSPACE_LOW_DISK_STALE_SECONDS if low_disk else BOT_WORKSPACE_STALE_SECONDS)
        # A retired worktree comes back from the repository of its binding, so that repository stays too.
        tracked = tracked_places(saved) | {Path(repository).resolve() for repository in repositories(saved) if Path(repository).exists()}
        # Readers are usually idle between polls, so neither file age nor open-file
        # checks identify these live dependencies. Keep configured paths, including bindings that have not been
        # written yet. A reader can also import or read the files next to it, so a reader keeps its whole entry.
        sealed = set()
        sources_path = self.root / 'work-sources.json'
        try:
            if sources_path.exists():
                sources = json.loads(sources_path.read_text())
                if not isinstance(sources, list):
                    raise ValueError('Expected a list of work sources.')
                for source in sources:
                    if (not isinstance(source, dict) or not isinstance(source.get('bot_id'), str)
                            or not isinstance(source.get('bindings'), str)
                            or not isinstance(source.get('command'), list)
                            or not all(isinstance(token, str) for token in source['command'])):
                        raise ValueError('Invalid work source configuration.')
                    workspace = self.root / 'bots' / source['bot_id'] / 'workspace'
                    bindings = workspace / source['bindings']
                    tracked.update((bindings.parent.resolve() / bindings.name, bindings.resolve()))
                    paths = []
                    for index, value in enumerate(source['command']):
                        if not value or value.startswith('-'):
                            continue
                        # A bare executable is searched on PATH, not in the workspace.
                        # Other bare arguments protect only files that actually exist.
                        path = Path(value)
                        if '/' not in value and (index == 0 or not (workspace / path).is_file()):
                            continue
                        paths.append(path if path.is_absolute() else workspace / path)
                    for path in paths:
                        sealed.update((path.parent.resolve() / path.name, path.resolve()))
        except (OSError, ValueError, TypeError, KeyError) as error:
            # Fail closed for bot files when their dependencies cannot be read, but
            # allow the caller to continue ordinary worktree retirement in this pass.
            logging.getLogger(__name__).warning('Skipping bot workspace cleanup: work sources could not be read (%s).',
                                               type(error).__name__)
            return
        found, cutoffs = [], {}
        # A bot without a running turn loses its old temporary files sooner. A failed read gives every bot the long wait.
        busy = self._bots_with_turns()
        tmp_before = max(stale_before, current - BOT_TMP_STALE_SECONDS)
        for bot in sorted(place for name in BOT_FILE_FOLDERS for place in (self.root / 'bots').glob('*/' + name)):
            if bot.is_symlink() or not bot.is_dir():
                continue
            before = tmp_before if bot.name == 'tmp' and busy is not None and bot.parent.name not in busy else stale_before
            # A folder that Coconut cannot read, or that goes while it reads, must not stop the pass for the other bots.
            try:
                # A stop between a move and its deletion leaves trash. Empty it first, in the workspace and one level down.
                for place in (bot, *(child for child in bot.iterdir() if child.is_dir() and not child.is_symlink())):
                    settle_checking(place, before)
                    empty_moved(place)
                # Each folder to read, and whether a fresh folder in it is read too. Only the top level, and a folder
                # that is open because it holds a protected path, go one level down into a fresh folder.
                pending = [(bot, True)]
                while pending:
                    parent, deeper = pending.pop()
                    if parent != bot and parent.parent != bot:
                        # The loop above settled the top level. A deeper folder gets a trash folder too.
                        settle_checking(parent, before)
                        empty_moved(parent)
                    for entry in sorted(parent.iterdir()):
                        # .stale-kept holds the entries that put_back could not return. They age out like any other entry.
                        if ((entry.name.startswith('.') and entry.name != STALE_KEPT) or entry.is_symlink()
                                or holds_tracked(entry, sealed) or self.stopping.is_set() or self.restart_pending):
                            continue
                        if holds_tracked(entry, tracked):
                            # The entry is protected, or it leads to a protected path. Judge its other entries.
                            if entry.resolve() not in tracked and entry.is_dir() and not is_repository(entry):
                                pending.append((entry, deeper))
                            continue
                        try:
                            state = stale_tree(entry, before)
                        except OSError:
                            continue
                        if state is not None:
                            found.append((entry, *state))
                            if before != stale_before:
                                cutoffs[entry] = (before, bot.parent.name)
                        elif deeper and entry.is_dir() and not is_repository(entry):
                            pending.append((entry, False))
            except OSError as error:
                logging.getLogger(__name__).warning('Coconut could not read the bot workspace %s: %s', bot, error)
        if not found:
            return
        kept = {}
        for entry, _, checkouts in found:
            reasons = [checkout_keeps(folder) for folder in checkouts]
            kept[entry] = next((reason for reason in reasons if reason), None)
        candidates = [item for item in found if not kept[item[0]]]
        if not candidates or self.stopping.is_set() or self.restart_pending:
            return
        holders = work_workspace.holders_by_path({str(entry) for entry, _, _ in candidates})
        removed, freed, repositories_to_prune, cleared = [], 0, set(), []
        waiting = candidates
        while waiting:
            retry = []
            for item in waiting:
                entry, size, checkouts = item
                if holders.get(str(entry)) or self.stopping.is_set() or self.restart_pending:
                    continue
                # A main checkout stays while a worktree of it stays, because the worktree needs its objects. The check
                # runs here, so a worktree that a later filter keeps, or that goes in this pass, counts as it is now.
                if worktrees_outside(entry, checkouts):
                    retry.append(item)
                    continue
                if not self.lock.acquire(timeout=SERVICE_WAIT_SECONDS):
                    continue
                target = None
                try:
                    # A turn starts under this lock. Check the top of the entry again, and move it before the lock goes.
                    # The full walk ran before, and it can take seconds, so the walk of the moved entry comes after the lock.
                    before, owner = cutoffs.get(entry, (stale_before, None))
                    if owner is not None:
                        # The short tmp wait needs a bot without a running turn. Check again, because a turn can start.
                        now_busy = self._bots_with_turns()
                        if now_busy is None or owner in now_busy:
                            continue
                    if top_changed(entry, before):
                        continue
                    if entry.is_dir() and not entry.is_symlink():
                        trash = entry.parent / work_workspace.SCRATCH_TRASH
                        if trash.is_symlink():
                            raise OSError(f'{trash} is a link. Coconut does not move entries through it.')
                        trash.mkdir(mode=0o700, exist_ok=True)
                        target = trash / f'{entry.name}-{time.time_ns()}{CHECKING}'
                        os.rename(entry, target)
                    else:
                        entry.unlink()
                except OSError as error:
                    logging.getLogger(__name__).warning('Coconut could not clear the stale bot workspace entry %s: %s', entry, error)
                    continue
                finally:
                    self.lock.release()
                if target is not None:
                    # A worker that does not hold the service lock can write deep in the entry after the first walk.
                    # Nothing new starts in the moved entry, so one more full walk sees every change that happened.
                    try:
                        unchanged = stale_tree(target, before, skip_root=True) is not None
                    except OSError:
                        unchanged = False
                    if not unchanged:
                        try:
                            where = put_back(target, entry)
                        except OSError as error:
                            logging.getLogger(__name__).warning('Coconut could not put back %s from %s: %s', entry, target, error)
                        else:
                            logging.getLogger(__name__).warning('Coconut put back %s, because it changed while Coconut moved it (%s).', entry, where)
                        continue
                    # The walk cleared it. One scan for all moved entries decides the release, after the loop.
                    cleared.append((entry, target, size, checkouts))
                    continue
                removed.append(entry)
                freed += size
            # A main checkout waits for the worktrees that lie in other stale entries. Try again while entries go.
            if len(retry) == len(waiting):
                break
            waiting = retry
        # A turn can open or enter a moved entry after the first scan, and an open file leaves no change time. Scan the
        # moved entries once more. A held one goes back. An error of the scan leaves them under the checking name,
        # and the next pass walks them again (settle_checking).
        if cleared and not (self.stopping.is_set() or self.restart_pending):
            try:
                after = work_workspace.holders_by_path({str(target) for _, target, _, _ in cleared})
            except ValueError as error:
                logging.getLogger(__name__).warning('Coconut could not check the moved bot workspace entries: %s', error)
                after = None
            held = []
            for entry, target, size, checkouts in cleared if after is not None else ():
                if after.get(str(target)):
                    held.append((entry, target, size, checkouts))
            # A held entry goes back first. A main checkout then goes back too when a worktree of it is back, because the
            # worktree needs its objects. A worktree that goes in this pass no longer exists at its old place.
            for entry, target, size, checkouts in held:
                try:
                    where = put_back(target, entry)
                    logging.getLogger(__name__).warning('Coconut put back %s, because a process uses it (%s).', entry, where)
                except OSError as error:
                    logging.getLogger(__name__).warning('Coconut could not put back %s from %s: %s', entry, target, error)
            # A worktree goes before its main checkout, so the main checkout sees whether a worktree could not go.
            for entry, target, size, checkouts in sorted(cleared if after is not None else (), key=lambda c: has_main(c[0], c[3], c[1])):
                try:
                    if (entry, target, size, checkouts) in held:
                        continue
                    if worktrees_outside(entry, checkouts, moved=target):
                        where = put_back(target, entry)
                        logging.getLogger(__name__).warning('Coconut put back %s, because a worktree of it is back (%s).', entry, where)
                        continue
                    released = target.with_name(target.name[:-len(CHECKING)])
                    os.rename(target, released)
                except OSError as error:
                    # The entry waits under the checking name, and its worktree records stay, so a later put_back
                    # returns a registered worktree. The next pass walks it again (settle_checking).
                    logging.getLogger(__name__).warning('Coconut could not release %s for deletion: %s', target, error)
                    continue
                # The entry is out of the way now, and its worktrees go. Their main checkouts forget them.
                repositories_to_prune |= linked_repositories(released / folder.relative_to(entry) for folder in checkouts)
                removed.append(entry)
                freed += size
        # The deletion runs without the lock.
        for parent in {entry.parent for entry in removed}:
            empty_moved(parent)
        if removed:
            logging.getLogger(__name__).warning('Coconut cleared %s stale entries of bot workspaces and freed %.1f GiB.',
                                                len(removed), freed / 1024 ** 3)
        prune_repositories(repositories_to_prune)

    def _busy_pause(self):
        """Wait BUSY_RETRY_SECONDS. Return True at once when the service stops or a restart is pending."""
        deadline = time.monotonic() + BUSY_RETRY_SECONDS
        while not self.restart_pending:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            # A restart request does not set stopping, so check it every second.
            if self.stopping.wait(min(1, left)):
                return True
        return True

    def retire_finished_worktrees(self, current=None):
        """Run at most once an hour. Git and file work run outside the service lock."""
        current = time.time() if current is None else current
        if current < getattr(self, '_next_worktree_retirement', 0):
            return []
        root = self.root / 'workspaces'
        try:
            low_disk = shutil.disk_usage(self.root).free < LOW_DISK_BYTES
        except OSError:
            low_disk = False
        self._next_worktree_retirement = current + (LOW_DISK_INTERVAL_SECONDS if low_disk else RETIREMENT_INTERVAL_SECONDS)
        manifest = root / 'worktrees.json'
        # An outcome that used only a scratch workspace has no registry entry. Its orphans still go.
        if not root.is_dir():
            return []
        # Only this poller writes archives, and no retirement runs now.
        work_workspace.sweep_temporary(root)
        ages = {}
        finished = self._finished_outcomes(current, timeout=SERVICE_WAIT_SECONDS, ages=ages, urgent=low_disk)
        if finished is None:
            # The service was busy. Run the pass again soon instead of in an hour.
            self._next_worktree_retirement = current + BUSY_RETRY_SECONDS
            return []
        # The registry changes by atomic rename, so this read needs no lock. Retirement checks again under the lock.
        saved = json.loads(manifest.read_text()) if manifest.exists() else {}
        try:
            self._stop_idle_orphans(finished, saved, urgent=low_disk)
        except (ValueError, OSError, KeyError, subprocess.TimeoutExpired, sqlite3.Error) as error:
            # A failed orphan check must not stop retirement.
            logging.getLogger(__name__).warning('Coconut could not stop the orphans of idle outcomes: %s', error)
        # A merged pull request ends its Git work, so its worktree goes without the checks that protect resumable work,
        # once its native cards are done too.
        try:
            classified = self._done_outcomes(finished)
        except (ValueError, OSError, KeyError, sqlite3.Error) as error:
            # A failed read leaves the pull requests unknown. Then no long idle discard runs in this pass.
            logging.getLogger(__name__).warning('Coconut could not read the pull requests of idle outcomes: %s', error)
            classified = None
        # A busy service lock leaves the open pull requests unknown. Then no long idle discard runs in this pass.
        done, open_pull_requests = classified or ({}, set())
        # An outcome idle this long loses a worktree that retirement refuses, and the files of its scratch workspace.
        # An open pull request keeps its work until the disk is low.
        stale = ({outcome for outcome in finished if ages.get(outcome, 0) >= URGENT_DISCARD_IDLE_SECONDS} if low_disk else
                 {outcome for outcome in finished if ages.get(outcome, 0) >= DISCARD_IDLE_SECONDS
                  and outcome not in open_pull_requests} if classified is not None else set())
        names = {}
        # A merged pull request ends the work only in its own repository. An outcome that also works in another
        # repository keeps the scratch files, which can hold a clone of it.
        def in_own_repositories(outcome, merged_in):
            bindings = [b for b in saved.values() if isinstance(b, dict) and b.get('outcome') == outcome]
            # Without a binding, no repository is known, so a merged pull request does not end this scratch work early.
            return bool(bindings) and all(repository_name(b.get('repository'), names) in merged_in for b in bindings)
        scratch_done = {outcome for outcome, merged_in in done.items() if in_own_repositories(outcome, merged_in)}
        # A discard deletes the ignored files and the nested worktrees, for example the evidence of an install that a
        # scheduled native card still has to accept. So only an outcome whose native cards are all done or archived
        # loses a worktree that way. A merged pull request does not finish a native card. Read the board first, as for
        # the scratch files: a failed read keeps every worktree that only a discard would remove.
        fresh = None
        if any(b.get('outcome') in done.keys() | stale for b in saved.values() if isinstance(b, dict) and not b.get('retired')):
            fresh = self._refresh_native_snapshot()
        native_done = (self._finished_outcomes(current, timeout=SERVICE_WAIT_SECONDS, urgent=low_disk, settled=True)
                       if fresh else None) or set()
        try:
            self._clear_idle_scratch(root, scratch_done | stale, current, saved, urgent=low_disk, fresh=fresh)
        except (ValueError, OSError, subprocess.TimeoutExpired, sqlite3.Error) as error:
            logging.getLogger(__name__).warning('Coconut could not clear idle scratch workspaces: %s', error)
        try:
            self._clear_stale_bot_workspaces(current, saved, low_disk)
        except (ValueError, OSError, subprocess.TimeoutExpired, sqlite3.Error) as error:
            logging.getLogger(__name__).warning('Coconut could not clear stale bot workspaces: %s', error)
        try:
            self._remove_empty_scratch(root, saved, current)
        except (ValueError, OSError, sqlite3.Error) as error:
            logging.getLogger(__name__).warning('Coconut could not remove empty scratch workspaces: %s', error)
        if not saved:
            return []
        for repository in repositories(saved):
            with contextlib.suppress(*work_workspace.GIT_ERRORS):
                work_workspace.prune_discarded_refs(repository)
        bindings = {k: v['outcome'] for k, v in saved.items() if not v.get('retired')}
        retired = []
        pending = [(identity, outcome) for identity, outcome in bindings.items() if outcome in finished]
        # A checkout that is already gone has nothing to archive, so its binding closes through discard.
        gone = {identity for identity, _ in pending
                if isinstance(saved[identity].get('path'), str) and not os.path.lexists(saved[identity]['path'])}
        # Git has no record of an orphaned worktree, so every Git command in it fails. It needs its own removal.
        orphaned = {identity for identity, _ in pending if work_workspace.orphan_record(saved[identity])}

        def dropped(items):
            # A stop or a restart ended the pass. The next pass after the restart tries again.
            for identity, _ in items:
                logging.getLogger(__name__).warning('Coconut did not retire worktree %s: The service was busy, and '
                                                    'Coconut is stopping or restarting. The next pass tries again.', identity)
        for attempt in range(BUSY_RETRIES + 1):
            if attempt and not pending:
                break
            if attempt and self._busy_pause():
                dropped(pending)
                break
            busy = []
            for index, (identity, outcome) in enumerate(pending):
                if self.stopping.is_set() or self.restart_pending:
                    if attempt:
                        # These worktrees met a busy service before. Name them.
                        dropped(pending[index:])
                    dropped(busy)
                    return retired

                def still_finished(wait, outcome=outcome, settled=False):
                    # Under the registry lock, wait is False. No thread waits for the registry lock while it holds the service lock,
                    # so a short, bounded wait there cannot deadlock.
                    result = self._finished_outcomes(time.time(), timeout=SERVICE_WAIT_SECONDS if wait else LOCKED_WAIT_SECONDS,
                                                    only=outcome, urgent=low_disk, settled=settled)
                    if result is None:
                        raise ServiceBusy(BUSY_MESSAGE)
                    return outcome in result

                def still_settled(wait, outcome=outcome):
                    # A native card can reopen while the discard runs. Its checks then stop the removal.
                    return still_finished(wait, outcome, settled=True)
                # A merged pull request ends the work only in its own repository. An outcome can work in several.
                merged = outcome in done and repository_name(saved[identity].get('repository'), names) in done[outcome]
                if identity in gone:
                    # A checkout that is gone has no files left to lose.
                    action, check = work_workspace.discard, still_finished
                elif identity in orphaned:
                    # The checks and the archive limits of retirement apply. See work_workspace.discard_orphan.
                    action, check = work_workspace.discard_orphan, still_finished
                elif merged and outcome in native_done:
                    action, check = work_workspace.discard, still_settled
                else:
                    # Retirement archives the ignored files exactly, or it refuses and the worktree stays.
                    action, check = work_workspace.retire, still_finished
                options = (dict(merged=merged, urgent=low_disk) if action is work_workspace.discard else
                           dict(urgent=low_disk) if action is work_workspace.discard_orphan else {})
                try:
                    # A service restart leaves the processes of earlier turns to launchd. They keep the worktree in use.
                    if action(root, identity, check, orphan_age=ORPHAN_AGE_SECONDS, **options):
                        retired.append(identity)
                        continue
                    refusal = None
                except (ValueError, OSError, KeyError, tarfile.TarError, subprocess.TimeoutExpired, sqlite3.Error) as error:
                    if isinstance(error, ServiceBusy):
                        if attempt < BUSY_RETRIES:
                            busy.append((identity, outcome))
                        else:
                            logging.getLogger(__name__).warning('Coconut did not retire worktree %s: %s', identity, error)
                        continue
                    refusal = error
                if (action is work_workspace.retire and outcome in stale and outcome in native_done
                        and not (self.stopping.is_set() or self.restart_pending)):
                    # Retirement could not archive the worktree exactly, and the outcome is idle for long, or the disk is
                    # low. Discard it: the branch and an archive of the patches and small untracked files stay.
                    try:
                        if work_workspace.discard(root, identity, still_settled, orphan_age=ORPHAN_AGE_SECONDS, urgent=low_disk):
                            logging.getLogger(__name__).warning('Retirement refused worktree %s of a long idle outcome: %s',
                                                                identity, refusal or 'it waits a day after a failure')
                            retired.append(identity)
                            continue
                    except (ValueError, OSError, KeyError, tarfile.TarError, subprocess.TimeoutExpired, sqlite3.Error) as error:
                        if isinstance(error, ServiceBusy):
                            # Retry in this pass, as for the first attempt.
                            if attempt < BUSY_RETRIES:
                                busy.append((identity, outcome))
                                continue
                        refusal = f'{refusal}; the discard also failed: {error}' if refusal else error
                if refusal:
                    # sqlite3.Error covers a database that shutdown closed while this thread ran.
                    logging.getLogger(__name__).warning('Coconut did not retire worktree %s: %s', identity, refusal)
                # A retirement can run for minutes. A stop or a restart that arrived meanwhile ends the pass first.
                # Git cannot read an orphaned worktree, so free_rebuildable cannot check its build folders.
                if self.stopping.is_set() or self.restart_pending or identity in orphaned:
                    continue
                # The worktree stays. Free the disk space of its build folders, so a refused worktree does not fill the disk.
                try:
                    freed = work_workspace.free_rebuildable(root, identity, still_finished)
                except (ValueError, OSError, KeyError, subprocess.TimeoutExpired, sqlite3.Error) as error:
                    logging.getLogger(__name__).warning('Coconut did not free the build folders of worktree %s: %s', identity, error)
                else:
                    if freed:
                        logging.getLogger(__name__).warning('Coconut deleted the build folders %s of idle worktree %s.', freed, identity)
            pending = busy
        return retired
