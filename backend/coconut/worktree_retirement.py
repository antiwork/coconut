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
# An idle outcome keeps a worktree that retirement cannot archive exactly for at most this long. Then Coconut discards it:
# the branch keeps the commits, and an archive keeps the patches and the small untracked files.
DISCARD_IDLE_SECONDS = 2 * 24 * 60 * 60
# Below this free disk space, the pass runs every LOW_DISK_INTERVAL_SECONDS, and it discards such a worktree as soon as
# the outcome is idle. A full disk stops the database and every turn, which costs more than the lost build files.
LOW_DISK_BYTES = 20 * 1024 ** 3
LOW_DISK_INTERVAL_SECONDS = 10 * 60
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


class ServiceBusy(ValueError):
    """The service lock stayed busy during a retirement check."""


class WorktreeRetirement:
    def _finished_outcomes(self, current, *, timeout=-1, only=None, idle=WORKTREE_RETIRE_SECONDS, ages=None):
        """Return the execution IDs whose work is idle, or None when the service lock is busy.

        Work that waits for a person, a failed task, and a blocked native outcome are idle.
        Hold the service lock only to read.
        """
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
                    latest = max(datetime.fromisoformat((t.get('updated_at') or t['created_at']).replace('Z', '+00:00'))
                                 .timestamp() for t in tasks)
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                if current - latest < idle:
                    continue
                if any(t['status'] in ACTIVE_STATUSES or t['id'] in self.running for t in tasks):
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

    def _stop_idle_orphans(self, finished, saved):
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
            again = self._finished_outcomes(time.time(), timeout=SERVICE_WAIT_SECONDS, only=outcome)
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

    def _clear_idle_scratch(self, root, eligible, current, saved):
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
        tracked = set()
        for binding in saved.values():
            if isinstance(binding, dict) and isinstance(binding.get('path'), str):
                with contextlib.suppress(OSError):
                    # A binding of a checkout that is gone holds no files.
                    if Path(binding['path']).exists():
                        tracked.add(Path(binding['path']).resolve())
        candidates = []
        for folder in root.iterdir():
            name = folder.name
            if any(place == folder.resolve() or folder.resolve() in place.parents for place in tracked):
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
                again = (self._finished_outcomes(time.time(), only=name, idle=WORKTREE_RETIRE_SECONDS)
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
        finished = self._finished_outcomes(current, timeout=SERVICE_WAIT_SECONDS, ages=ages)
        if finished is None:
            # The service was busy. Run the pass again soon instead of in an hour.
            self._next_worktree_retirement = current + BUSY_RETRY_SECONDS
            return []
        # The registry changes by atomic rename, so this read needs no lock. Retirement checks again under the lock.
        saved = json.loads(manifest.read_text()) if manifest.exists() else {}
        try:
            self._stop_idle_orphans(finished, saved)
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
        stale = set(finished) if low_disk else {outcome for outcome in finished if ages.get(outcome, 0) >= DISCARD_IDLE_SECONDS
                                                and outcome not in open_pull_requests} if classified is not None else set()
        names = {}
        # A merged pull request ends the work only in its own repository. An outcome that also works in another
        # repository keeps the scratch files, which can hold a clone of it.
        def in_own_repositories(outcome, merged_in):
            bindings = [b for b in saved.values() if isinstance(b, dict) and b.get('outcome') == outcome]
            # Without a binding, no repository is known, so a merged pull request does not end this scratch work early.
            return bool(bindings) and all(repository_name(b.get('repository'), names) in merged_in for b in bindings)
        scratch_done = {outcome for outcome, merged_in in done.items() if in_own_repositories(outcome, merged_in)}
        try:
            self._clear_idle_scratch(root, scratch_done | stale, current, saved)
        except (ValueError, OSError, subprocess.TimeoutExpired, sqlite3.Error) as error:
            logging.getLogger(__name__).warning('Coconut could not clear idle scratch workspaces: %s', error)
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
                    result = self._finished_outcomes(time.time(), timeout=SERVICE_WAIT_SECONDS if wait else LOCKED_WAIT_SECONDS, only=outcome)
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
