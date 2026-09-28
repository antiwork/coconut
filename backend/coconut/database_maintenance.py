"""Bound the event log and the native receipt journal, and give their free pages back to the file system.

Both tables grow with every turn. Maintenance deletes old rows in batches. Each batch is one short transaction,
so a phone request or a tool call waits for at most one batch.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import time

from .background_processes import PENDING_COMPLETIONS
from .log_rotation import rotate_log
from .session_maintenance import SESSION_RETENTION_DAYS
from .work_capacity import execution_key
from .worktree_retirement import NATIVE_IDLE_STATUSES

DATABASE_MAINTENANCE_SECONDS = 60 * 60
# The phone replays at most 1000 events. After a larger gap, or a gap that this deletion makes, it loads a full
# snapshot (StateSync.updates, and MessageReplay on the phone). Older events serve no sync.
EVENT_RETENTION_DAYS = 2
# The newest events stay at any age. MAX(seq) is the sync cursor, so the log never becomes empty.
EVENT_MINIMUM_ROWS = 10000
# Lookups read these old events: NativeOutcomes._unlink_conversation_current reads task.created and the newest
# bots.restarted, NativeRoutines.native_routine_completion_input reads the history of a routine task, and
# PullRequestReviews reads the swipe that dismissed a merge card. The newest task.updated and
# bot_message.delivered of each task also stay (see _prune_events).
KEPT_EVENTS = """
    type IN ('task.created', 'bots.restarted', 'routine.triggered')
    OR seq IN (SELECT value FROM json_each(:latest))
    OR (type='task.updated' AND task_id IN (SELECT value FROM json_each(:routines)))
    OR (type='decision.updated' AND COALESCE(json_extract(data, '$.status'), '')='dismissed'
        AND json_extract(data, '$.inbox_swipe_id') IS NOT NULL)
"""
# Hermes prunes the native session of an idle outcome after this time (SessionMaintenance). The completed
# receipts of its tasks go after the same time. A resumed outcome then continues from its visible conversation.
RECEIPT_RETENTION_DAYS = SESSION_RETENTION_DAYS
FINISHED = ('completed', 'failed', 'cancelled')
# Lookups read these receipts by content at any age: NativeOutcomes adopts a native task by the idempotency key
# of its kanban_create receipt, and WorkProgress links a manual routine run by its cronjob_manage receipt.
KEPT_ACTIONS = ('kanban_create', 'cronjob_manage')
BATCH_ROWS = 1000
# A pause between batches lets waiting requests take the service lock.
BATCH_PAUSE_SECONDS = 0.05
# An old store without incremental vacuum gets one full VACUUM above this free-page fraction.
VACUUM_MIN_FREELIST_RATIO = 0.25
# One incremental step frees this many pages (8 MB at 4 KB pages).
VACUUM_PAGES = 2048


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


def seconds(text):
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


class DatabaseMaintenance:
    def maintain_database(self, current=None):
        """Run at most once an hour. Delete old events and receipts, then free pages, then rotate the logs."""
        current = time.time() if current is None else current
        if current < getattr(self, '_next_database_maintenance', 0):
            return {}
        self._next_database_maintenance = current + DATABASE_MAINTENANCE_SECONDS
        result = dict(events=self._prune_events(current), receipts=self._prune_receipts(current))
        result['vacuum'] = self._release_free_pages()
        logs = self.root / 'logs'
        result['rotated'] = [name for fd, name in ((1, 'service.log'), (2, 'error.log')) if rotate_log(fd, logs / name)]
        if result['events'] or result['receipts'] or result['vacuum']:
            logging.getLogger(__name__).warning('coconut.database_maintenance %s', json.dumps(result))
        return result

    def _maintenance_stopped(self):
        return self.stopping.is_set() or self.restart_pending

    def _prune_events(self, current):
        """Delete events older than EVENT_RETENTION_DAYS outside the newest EVENT_MINIMUM_ROWS, except KEPT_EVENTS.

        A later event of the same type replaces the newest one of a task, so a snapshot of the newest ones stays
        correct while new events arrive. Events have no time index, but seq grows with created_at, so the
        deletion walks up from the oldest event and stops at the first recent one.
        """
        with self._read_view() as view, view._read_transaction():
            newest = view.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            latest = json.dumps([row[0] for row in view.db.execute(
                "SELECT MAX(seq) FROM events WHERE type IN ('task.updated','bot_message.delivered') "
                'AND task_id IS NOT NULL GROUP BY type, task_id')])
            routines = json.dumps([row[0] for row in view.db.execute(
                "SELECT id FROM tasks WHERE json_extract(data,'$.routine_id') IS NOT NULL")])
        cutoff, ceiling = stamp(current - EVENT_RETENTION_DAYS * 86400), newest - EVENT_MINIMUM_ROWS
        after, deleted = 0, 0
        while after < ceiling and not self._maintenance_stopped():
            with self.transaction():
                rows = self.db.execute('SELECT seq,created_at FROM events WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?',
                                       (after, ceiling, BATCH_ROWS)).fetchall()
                old = []
                for seq, created in rows:
                    if created >= cutoff:
                        break
                    old.append(seq)
                if old:
                    deleted += self.db.execute(f'DELETE FROM events WHERE seq>:after AND seq<=:last AND NOT ({KEPT_EVENTS})',
                        dict(after=after, last=old[-1], latest=latest, routines=routines)).rowcount
            if len(old) < BATCH_ROWS:
                break
            after = old[-1]
            self.stopping.wait(BATCH_PAUSE_SECONDS)
        return deleted

    def _prune_receipts(self, current):
        """Delete the completed receipts of outcomes that stayed idle for RECEIPT_RETENTION_DAYS.

        All tasks of an idle outcome are finished, and none changed within that time. The rule keeps a receipt that
        a lookup still reads:
        - an executing receipt;
        - the receipts of a task with a pending completion (PENDING_COMPLETIONS), because the task resumes;
        - a receipt that a decision (_receipt_id), a task (_continuation_receipt_ids), or a waiting receipt
          (_wait_process_receipt) names, because _get fails without it;
        - the receipts of a task in the scope of a pending decision, because its freshness fingerprint lists them;
        - the receipts of a task with a queued or working context;
        - KEPT_ACTIONS.
        """
        cutoff = current - RECEIPT_RETENTION_DAYS * 86400
        with self.lock, self._shared_reads():
            tasks = self._task_map()
            pending = self._shared_pending_receipts()
            groups = {}
            for task in tasks.values():
                groups.setdefault(execution_key(task), []).append(task)
            native = dict(self.kanban.tasks) if self.kanban else {}
            by_native_id = {n['id']: n for n in native.values()}
            waiting = {r['task_id'] for r in pending}
            idle = set()
            for key, members in groups.items():
                times = [seconds(t.get('updated_at') or t.get('created_at')) for t in members]
                if (None in times or max(times) >= cutoff
                        or any(t['status'] not in FINISHED or t['id'] in waiting
                               or (self.running.get(t['id']) and self.running[t['id']].is_alive())
                               or (native.get(t['id']) or by_native_id.get(t.get('_native_outcome_id')) or {}).get('status', 'done')
                               not in NATIVE_IDLE_STATUSES for t in members)):
                    continue
                idle.add(key)
            decisions = self._all_shared('decisions')
            artifacts = self._all_shared('artifacts')
            held = {c['task_id'] for c in self._all_shared('contexts') if c['status'] in ('queued', 'working')}
            for decision in decisions:
                if decision['status'] == 'pending' and decision['task_id'] in tasks:
                    held.add(decision['task_id'])
                    held.update(self._decision_scope(decision, tasks, pending, artifacts)[0])
            candidates = {t['id']: execution_key(t) for t in tasks.values() if execution_key(t) in idle and t['id'] not in held}
            named = ({d['_receipt_id'] for d in decisions if d.get('_receipt_id')}
                     | {i for t in tasks.values() for i in t.get('_continuation_receipt_ids', [])}
                     | {r['_wait_process_receipt'] for r in pending if r.get('_wait_process_receipt')})
        if not candidates:
            return 0
        with self._read_view() as view, view._read_transaction():
            rows = [row[0] for row in view.db.execute(
                "SELECT rowid FROM native_receipts WHERE json_extract(data,'$.task_id') IN (SELECT value FROM json_each(?)) "
                "AND json_extract(data,'$.state')='completed' "
                "AND COALESCE(json_extract(data,'$.action.name'),'') NOT IN (SELECT value FROM json_each(?)) ORDER BY rowid",
                (json.dumps(sorted(candidates)), json.dumps(KEPT_ACTIONS)))]
        deleted = 0
        for start in range(0, len(rows), BATCH_ROWS):
            if self._maintenance_stopped():
                break
            with self.transaction():
                # A turn, a context, or a decision can start between batches. The status indexes find them quickly.
                live = [json.loads(row[0]) for row in self.db.execute(
                    "SELECT data FROM tasks WHERE json_extract(data,'$.status') IN (SELECT value FROM json_each(?))",
                    (json.dumps(sorted(self.ACTIVE)),))]
                keys = {execution_key(t) for t in live}
                excluded = {identity for identity, key in candidates.items() if key in keys}
                excluded.update(row[0] for row in self.db.execute(
                    "SELECT json_extract(data,'$.task_id') FROM contexts WHERE json_extract(data,'$.status') IN ('queued','working') "
                    "UNION SELECT json_extract(data,'$.task_id') FROM decisions WHERE json_extract(data,'$.status')='pending'"))
                names = named | {i for t in live for i in t.get('_continuation_receipt_ids', [])}
                deleted += self.db.execute(
                    'DELETE FROM native_receipts WHERE rowid IN (SELECT value FROM json_each(?)) '
                    "AND json_extract(data,'$.state')='completed' AND NOT (" + PENDING_COMPLETIONS + ') '
                    'AND id NOT IN (SELECT value FROM json_each(?)) '
                    "AND json_extract(data,'$.task_id') NOT IN (SELECT value FROM json_each(?))",
                    (json.dumps(rows[start:start + BATCH_ROWS]), json.dumps(sorted(names)),
                     json.dumps(sorted(excluded - {None})))).rowcount
            self.stopping.wait(BATCH_PAUSE_SECONDS)
        return deleted

    def _release_free_pages(self):
        """Give free pages back to the file system in short steps.

        A store in incremental mode frees VACUUM_PAGES per step. An older store needs one full VACUUM to enter that
        mode. The VACUUM holds the service lock while it rewrites the file, so it runs only when no task or context
        runs, and only when more than VACUUM_MIN_FREELIST_RATIO of the file is free.
        """
        freed = 0
        while not self._maintenance_stopped():
            with self.lock:
                mode = self.db.execute('PRAGMA auto_vacuum').fetchone()[0]
                pages = self.db.execute('PRAGMA page_count').fetchone()[0]
                free = self.db.execute('PRAGMA freelist_count').fetchone()[0]
                if mode == 2:
                    if not free:
                        break
                    # Python 3.11 steps a statement without result columns once, which frees one page.
                    # executescript runs the pragma to the end. The connection has no open transaction here.
                    self.db.executescript(f'PRAGMA incremental_vacuum({VACUUM_PAGES});')
                    freed += min(free, VACUUM_PAGES)
                elif pages and free / pages > VACUUM_MIN_FREELIST_RATIO and self._database_quiet():
                    started = time.monotonic()
                    self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
                    self.db.execute('VACUUM')
                    logging.getLogger(__name__).warning('coconut.database_vacuum %s', json.dumps(dict(
                        pages_before=pages, free_before=free, seconds=round(time.monotonic() - started, 1))))
                    freed += free
                    break
                else:
                    break
            self.stopping.wait(BATCH_PAUSE_SECONDS)
        if freed:
            with self.lock:
                # The WAL keeps its largest size until a truncating checkpoint. A busy reader defers it to the next hour.
                self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        return freed

    def _database_quiet(self):
        """Return True when no task worker, context, or native run is live. The caller holds the service lock.

        A queued task can wait for capacity for hours. It starts only through the service lock, so it does not block.
        """
        if any(thread.is_alive() for thread in list(self.running.values()) + list(self.context_threads.values())):
            return False
        return not any(native.get('status') == 'running' for native in (self.kanban.tasks.values() if self.kanban else ()))
