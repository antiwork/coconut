"""Retire the worktrees of idle outcomes. An archive keeps their uncommitted changes.

A turn restores a retired worktree before it starts, so an idle outcome that resumes gets its files back."""
from datetime import datetime
import json
import logging
from pathlib import Path
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


class ServiceBusy(ValueError):
    """The service lock stayed busy during a retirement check."""


class WorktreeRetirement:
    def _finished_outcomes(self, current, *, timeout=-1, only=None):
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
                if current - latest < WORKTREE_RETIRE_SECONDS:
                    continue
                if any(t['status'] in ACTIVE_STATUSES or t['id'] in self.running for t in tasks):
                    continue
                if any((native.get(t['id']) or by_native_id.get(t.get('_native_outcome_id')) or {}).get('status', 'done')
                       not in NATIVE_IDLE_STATUSES for t in tasks):
                    continue
                if any(self._background_pending(t['id']) for t in tasks):
                    continue
                finished.add(outcome)
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

    def retire_finished_worktrees(self, current=None):
        """Run at most once an hour. Git and file work run outside the service lock."""
        current = time.time() if current is None else current
        if current < getattr(self, '_next_worktree_retirement', 0):
            return []
        self._next_worktree_retirement = current + RETIREMENT_INTERVAL_SECONDS
        root = self.root / 'workspaces'
        manifest = root / 'worktrees.json'
        # An outcome that used only a scratch workspace has no registry entry. Its orphans still go.
        if not root.is_dir():
            return []
        # Only this poller writes archives, and no retirement runs now.
        work_workspace.sweep_temporary(root)
        finished = self._finished_outcomes(current, timeout=SERVICE_WAIT_SECONDS)
        if finished is None:
            return []
        # The registry changes by atomic rename, so this read needs no lock. Retirement checks again under the lock.
        saved = json.loads(manifest.read_text()) if manifest.exists() else {}
        try:
            self._stop_idle_orphans(finished, saved)
        except (ValueError, OSError, KeyError, subprocess.TimeoutExpired, sqlite3.Error) as error:
            # A failed orphan check must not stop retirement.
            logging.getLogger(__name__).warning('Coconut could not stop the orphans of idle outcomes: %s', error)
        if not saved:
            return []
        bindings = {k: v['outcome'] for k, v in saved.items() if not v.get('retired')}
        retired = []
        pending = [(identity, outcome) for identity, outcome in bindings.items() if outcome in finished]

        def dropped(items):
            # A stop or a restart ended the pass. The next pass after the restart tries again.
            for identity, _ in items:
                logging.getLogger(__name__).warning('Coconut did not retire worktree %s: The service was busy, and '
                                                    'Coconut is stopping. The next pass tries again.', identity)
        for attempt in range(BUSY_RETRIES + 1):
            if attempt and not pending:
                break
            if attempt and (self.stopping.wait(BUSY_RETRY_SECONDS) or self.restart_pending):
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
                try:
                    # A service restart leaves the processes of earlier turns to launchd. They keep the worktree in use.
                    if work_workspace.retire(root, identity, still_finished, orphan_age=ORPHAN_AGE_SECONDS):
                        retired.append(identity)
                except (ValueError, OSError, KeyError, tarfile.TarError, subprocess.TimeoutExpired, sqlite3.Error) as error:
                    if isinstance(error, ServiceBusy) and attempt < BUSY_RETRIES:
                        busy.append((identity, outcome))
                        continue
                    # sqlite3.Error covers a database that shutdown closed while this thread ran.
                    logging.getLogger(__name__).warning('Coconut did not retire worktree %s: %s', identity, error)
            pending = busy
        return retired
