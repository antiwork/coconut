"""Retire the worktrees of idle outcomes. An archive keeps their uncommitted changes.

A turn restores a retired worktree before it starts, so an idle outcome that resumes gets its files back."""
from datetime import datetime
import json
import logging
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
# These native states mean that a worker runs or claims the outcome soon.
NATIVE_ACTIVE_STATUSES = ('running', 'ready')
RETIREMENT_INTERVAL_SECONDS = 60 * 60
# Before the registry lock, retirement waits this long for the service lock. Under the registry lock, it waits only LOCKED_WAIT_SECONDS.
SERVICE_WAIT_SECONDS = 5
# The service holds its lock for milliseconds. A short wait under the registry lock lets retirement finish on a busy service.
LOCKED_WAIT_SECONDS = 0.25


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
                if any((native.get(t['id']) or by_native_id.get(t.get('_native_outcome_id')) or {}).get('status')
                       in NATIVE_ACTIVE_STATUSES for t in tasks):
                    continue
                if any(self._background_pending(t['id']) for t in tasks):
                    continue
                finished.add(outcome)
            return finished
        finally:
            self.lock.release()

    def retire_finished_worktrees(self, current=None):
        """Run at most once an hour. Git and file work run outside the service lock."""
        current = time.time() if current is None else current
        if current < getattr(self, '_next_worktree_retirement', 0):
            return []
        self._next_worktree_retirement = current + RETIREMENT_INTERVAL_SECONDS
        root = self.root / 'workspaces'
        manifest = root / 'worktrees.json'
        if not manifest.exists():
            return []
        # Only this poller writes archives, and no retirement runs now.
        work_workspace.sweep_temporary(root)
        # The registry changes by atomic rename, so this read needs no lock. Retirement checks again under the lock.
        bindings = {k: v['outcome'] for k, v in json.loads(manifest.read_text()).items() if not v.get('retired')}
        finished = self._finished_outcomes(current, timeout=SERVICE_WAIT_SECONDS)
        if finished is None:
            return []
        retired = []
        for identity, outcome in bindings.items():
            if outcome not in finished:
                continue
            if self.stopping.is_set() or self.restart_pending:
                break

            def still_finished(wait, outcome=outcome):
                # Under the registry lock, wait is False. No thread waits for the registry lock while it holds the service lock,
                # so a short, bounded wait there cannot deadlock.
                result = self._finished_outcomes(time.time(), timeout=SERVICE_WAIT_SECONDS if wait else LOCKED_WAIT_SECONDS, only=outcome)
                if result is None:
                    raise ValueError('The service was busy. Coconut tries again in an hour.')
                return outcome in result
            try:
                # A service restart leaves the processes of earlier turns to launchd. They keep the worktree in use.
                if work_workspace.retire(root, identity, still_finished, orphan_age=ORPHAN_AGE_SECONDS):
                    retired.append(identity)
            except (ValueError, OSError, KeyError, tarfile.TarError, subprocess.TimeoutExpired, sqlite3.Error) as error:
                # sqlite3.Error covers a database that shutdown closed while this thread ran.
                logging.getLogger(__name__).warning('Coconut did not retire worktree %s: %s', identity, error)
        return retired
