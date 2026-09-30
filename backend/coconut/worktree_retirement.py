"""Retire the worktrees of idle outcomes. An archive keeps their uncommitted changes.

A turn restores a retired worktree before it starts, so an idle outcome that resumes gets its files back.
An idle outcome whose pull requests all merged or closed loses its worktree instead (work_workspace.discard)."""
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
# A bot keeps its own files in bots/<id>/workspace: clones, review checkouts, evidence. Nothing else removes them. An entry
# that nothing in it changed for this long goes, unless a Git checkout in it holds work that no remote has. Below
# LOW_DISK_BYTES the wait is the shorter one.
BOT_WORKSPACE_STALE_SECONDS = 14 * 24 * 60 * 60
BOT_WORKSPACE_LOW_DISK_STALE_SECONDS = 3 * 24 * 60 * 60
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


def is_repository(folder):
    """Return True when folder is a Git checkout or a bare repository. Its parts belong together, so nothing judges them apart."""
    return (folder / '.git').exists() or all((folder / name).exists() for name in ('HEAD', 'objects', 'refs'))


def changed_at(info):
    """Return when a file or folder last changed. ctime moves on an extract, a copy and a rename, and a user cannot set it."""
    return max(info.st_mtime, info.st_ctime)


def stale_tree(path, stale_before):
    """Return (bytes, checkouts) when nothing in the tree changed since stale_before, else None. Never follow a link.

    checkouts lists the folders of the tree that hold a Git checkout, and the bare repositories. A fresh file ends the
    walk at once.
    """
    total, checkouts, stack = 0, [], [path]
    while stack:
        current = stack.pop()
        info = os.lstat(current)
        if changed_at(info) >= stale_before:
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


def worktrees_outside(entry, checkouts):
    """Return True when a main checkout in entry has a linked worktree that exists outside entry, or when Git cannot tell."""
    place = entry.resolve()
    for folder in checkouts:
        if not (folder / '.git').is_dir():
            continue
        try:
            for path in linked_worktrees(folder):
                if path.exists() and not (path.resolve() == place or place in path.resolve().parents):
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
    def _finished_outcomes(self, current, *, timeout=-1, only=None, idle=None, ages=None, urgent=False):
        """Return the execution IDs whose work is idle, or None when the service lock is busy.

        Work that waits for a person, a failed task, and a blocked native outcome are idle.
        With urgent (a low disk), idle is URGENT_IDLE_SECONDS, and a task that waited long in the queue does not count.
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
                if any((native.get(t['id']) or by_native_id.get(t.get('_native_outcome_id')) or {}).get('status', 'done')
                       not in NATIVE_IDLE_STATUSES for t in tasks):
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

    def _clear_idle_scratch(self, root, eligible, current, saved, urgent=False):
        """Delete the files in the scratch workspaces (workspaces/<outcome>) of done or long idle outcomes.

        Nothing else removes them, and a bot often clones a repository or builds there. The folder stays, empty, because
        a later turn of the outcome uses it as its working directory. A folder that no task names goes after
        DISCARD_IDLE_SECONDS without changes. A bot's own workspace and a folder that a process uses stay.
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
        holders = work_workspace.holders_by_path({str(folder) for folder in candidates})
        cleared = []
        for folder in candidates:
            if holders.get(str(folder)) or self.stopping.is_set() or self.restart_pending:
                continue
            name = folder.name
            if not self.lock.acquire(timeout=SERVICE_WAIT_SECONDS):
                continue
            try:
                # A turn starts under this lock. Check the outcome again, and move the folder before the lock goes.
                # Confirm the idle time that made the outcome eligible, and not only that no task is active now.
                again = (self._finished_outcomes(time.time(), only=name, urgent=urgent,
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

    def _clear_stale_bot_workspaces(self, current, saved, low_disk):
        """Delete the entries of bots/<id>/workspace that nothing has changed for BOT_WORKSPACE_STALE_SECONDS.

        An entry is a file or a folder of the workspace. A folder that has a newer file is not stale, so Coconut then
        judges the entries inside it, one level down, unless it is a Git repository itself. A Git checkout that holds work
        that no remote has stays (checkout_keeps). A main checkout stays while a worktree of it stays. A process that
        uses an entry, a tracked worktree, and a name that starts with a dot stay too. Coconut moves a folder to a
        trash folder of its parent first, and deletes it later, outside the lock.
        """
        stale_before = current - (BOT_WORKSPACE_LOW_DISK_STALE_SECONDS if low_disk else BOT_WORKSPACE_STALE_SECONDS)
        tracked = tracked_places(saved)
        found = []
        for bot in sorted((self.root / 'bots').glob('*/workspace')):
            if bot.is_symlink() or not bot.is_dir():
                continue
            # A stop between a move and its deletion leaves trash. Empty it first, in the workspace and one level down.
            for place in (bot, *(child for child in bot.iterdir() if child.is_dir() and not child.is_symlink())):
                work_workspace.empty_scratch_trash(place)
            pending = [bot]
            while pending:
                parent = pending.pop()
                for entry in sorted(parent.iterdir()):
                    if (entry.name.startswith('.') or entry.is_symlink() or holds_tracked(entry, tracked)
                            or self.stopping.is_set() or self.restart_pending):
                        continue
                    try:
                        state = stale_tree(entry, stale_before)
                    except OSError:
                        continue
                    if state is not None:
                        found.append((entry, *state))
                    elif parent == bot and entry.is_dir() and not is_repository(entry):
                        pending.append(entry)
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
        removed, freed, repositories_to_prune = [], 0, set()
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
                try:
                    # A turn starts under this lock. Check the top of the entry again, and move it before the lock goes.
                    # The full walk ran before, and it can take seconds. A process that works in the entry holds it (see above).
                    if top_changed(entry, stale_before):
                        continue
                    for folder in checkouts:
                        marker = folder / '.git'
                        if marker.is_file():
                            with contextlib.suppress(OSError, IndexError):
                                line = marker.read_text().strip()
                                if line.startswith('gitdir: '):
                                    # A linked worktree names <repository>/.git/worktrees/<name>. A submodule names another place.
                                    record = (folder / line[len('gitdir: '):]).resolve()
                                    if record.parent.name == 'worktrees' and record.parent.parent.name == '.git':
                                        repositories_to_prune.add(record.parent.parent.parent)
                    if entry.is_dir():
                        work_workspace.move_scratch(entry, keep=False)
                    else:
                        entry.unlink()
                except OSError as error:
                    logging.getLogger(__name__).warning('Coconut could not clear the stale bot workspace entry %s: %s', entry, error)
                    continue
                finally:
                    self.lock.release()
                removed.append(entry)
                freed += size
            # A main checkout waits for the worktrees that lie in other stale entries. Try again while entries go.
            if len(retry) == len(waiting):
                break
            waiting = retry
        # The deletion runs without the lock.
        for parent in {entry.parent for entry in removed}:
            work_workspace.empty_scratch_trash(parent)
        if removed:
            logging.getLogger(__name__).warning('Coconut cleared %s stale entries of bot workspaces and freed %.1f GiB.',
                                                len(removed), freed / 1024 ** 3)
            for repository in repositories_to_prune:
                if repository.is_dir():
                    with contextlib.suppress(*work_workspace.GIT_ERRORS):
                        work_workspace.git(repository, 'worktree', 'prune', timeout=work_workspace.CHECKOUT_SECONDS)

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
        # A merged pull request ends its work, so its worktree goes without the checks that protect resumable work.
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
        try:
            self._clear_idle_scratch(root, scratch_done | stale, current, saved, urgent=low_disk)
        except (ValueError, OSError, subprocess.TimeoutExpired, sqlite3.Error) as error:
            logging.getLogger(__name__).warning('Coconut could not clear idle scratch workspaces: %s', error)
        try:
            self._clear_stale_bot_workspaces(current, saved, low_disk)
        except (ValueError, OSError, subprocess.TimeoutExpired, sqlite3.Error) as error:
            logging.getLogger(__name__).warning('Coconut could not clear stale bot workspaces: %s', error)
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

                def still_finished(wait, outcome=outcome):
                    # Under the registry lock, wait is False. No thread waits for the registry lock while it holds the service lock,
                    # so a short, bounded wait there cannot deadlock.
                    result = self._finished_outcomes(time.time(), timeout=SERVICE_WAIT_SECONDS if wait else LOCKED_WAIT_SECONDS,
                                                    only=outcome, urgent=low_disk)
                    if result is None:
                        raise ServiceBusy(BUSY_MESSAGE)
                    return outcome in result
                # A merged pull request ends the work only in its own repository. An outcome can work in several.
                merged = outcome in done and repository_name(saved[identity].get('repository'), names) in done[outcome]
                action = work_workspace.discard if merged or identity in gone else work_workspace.retire
                options = dict(merged=merged, urgent=low_disk) if action is work_workspace.discard else {}
                try:
                    # A service restart leaves the processes of earlier turns to launchd. They keep the worktree in use.
                    if action(root, identity, still_finished, orphan_age=ORPHAN_AGE_SECONDS, **options):
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
                if action is work_workspace.retire and outcome in stale and not (self.stopping.is_set() or self.restart_pending):
                    # Retirement could not archive the worktree exactly, and the outcome is idle for long, or the disk is
                    # low. Discard it: the branch and an archive of the patches and small untracked files stay.
                    try:
                        if work_workspace.discard(root, identity, still_finished, orphan_age=ORPHAN_AGE_SECONDS, urgent=low_disk):
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
                if self.stopping.is_set() or self.restart_pending:
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
