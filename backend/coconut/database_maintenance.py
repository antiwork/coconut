"""Bound the event log, the native receipt journal, and the old text of contexts and tasks, and give free pages back.

The event log and the journal grow with every turn, and a context or a task keeps its recovery text after it ends.
Maintenance deletes old rows and shortens old text in batches. Each batch is one short transaction,
so a phone request or a tool call waits for at most one batch.
"""
from __future__ import annotations

from datetime import datetime, timezone
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import time

from .background_processes import PENDING_COMPLETIONS
from .log_rotation import rotate_log
from .session_maintenance import SESSION_RETENTION_DAYS
from .state_sync import index_exchange_audience
from .work_capacity import execution_key
from .worktree_retirement import NATIVE_IDLE_STATUSES

DATABASE_MAINTENANCE_SECONDS = 60 * 60
# A pass that fails runs again after this delay, not after an hour.
DATABASE_RETRY_SECONDS = 5 * 60
# The phone replays at most 1000 events. After a larger gap, or a gap that this deletion makes, it loads a full
# snapshot (StateSync.updates, and MessageReplay on the phone). Older events serve no sync.
EVENT_RETENTION_DAYS = 2
# The newest events stay at any age. MAX(seq) is the sync cursor, so the log never becomes empty.
EVENT_MINIMUM_ROWS = 10000
# Lookups read these old events: NativeOutcomes._unlink_conversation_current reads task.created and the newest
# bots.restarted, NativeRoutines.native_routine_completion_input reads the history of a routine task, and
# PullRequestReviews reads the swipe that dismissed a merge card. The newest task.updated and
# bot_message.delivered of each task also stay (see _prune_events).
# The events of a task stay only until the task is retired (see HISTORY_DAYS). The task readers above read the events
# of a task that runs, waits, or is queued, and a retired task does none of these.
KEPT_EVENTS = """
    type='bots.restarted'
    OR (type='decision.updated' AND COALESCE(json_extract(data, '$.status'), '')='dismissed'
        AND json_extract(data, '$.inbox_swipe_id') IS NOT NULL)
    OR ((type IN ('task.created', 'routine.triggered')
         OR seq IN (SELECT value FROM json_each(:latest))
         OR (type='task.updated' AND task_id IN (SELECT value FROM json_each(:routines))))
        AND (task_id IS NULL OR task_id NOT IN (SELECT value FROM json_each(:retired))))
"""
# A task that ended and did not change for this long is retired. Its kept events and its completed receipts go, also
# when other tasks of its execution session still run. Hermes prunes an idle native session after a quarter of this
# time, so no later turn resumes the turns of a retired task.
HISTORY_DAYS = 30
# The first pass that retires tasks copies every row that it deletes to <data>/backups/retention-<date>.sqlite3.
# A backup goes after this many days.
BACKUP_DAYS = 30
BACKUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, type TEXT NOT NULL, bot_id TEXT, task_id TEXT, data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS native_receipts (id TEXT PRIMARY KEY, data TEXT NOT NULL);
"""
# Hermes prunes the native session of an idle outcome after this time (SessionMaintenance). The completed
# receipts of its tasks go after the same time. A resumed outcome then continues from its visible conversation.
RECEIPT_RETENTION_DAYS = SESSION_RETENTION_DAYS
FINISHED = ('completed', 'failed', 'cancelled')
# PENDING_COMPLETIONS is NULL, not false, for some receipts, and NOT NULL is NULL, so the deletion kept them. A
# delivered process result is one of them, and a withdrawal check reads it later, so it stays. A terminal result without
# notify_on_complete is the other one. It is a plain command output, and most stored tool output is of this kind.
UNKNOWN_COMPLETION_KEPT = ("CASE WHEN json_extract(data,'$.action.name')='terminal' AND json_valid(json_extract(data,'$.result')) "
                           "THEN json_extract(json_extract(data,'$.result'),'$.notify_on_complete') IS NOT NULL ELSE 1 END")
# Lookups read these receipts by content at any age: NativeOutcomes adopts a native task by the idempotency key
# of its kanban_create receipt, routine_reports.created_occurrences finds the routines that a task created, and
# WorkProgress links a manual routine run. Other cronjob_manage calls, such as a list of every job, have no reader.
KEPT_ROUTINE_CALLS = ('create', 'run', 'run_now', 'trigger')
KEPT_RECEIPT = ("(json_extract(data,'$.action.name')='kanban_create' OR (json_extract(data,'$.action.name')='cronjob_manage' "
                "AND lower(trim(COALESCE(json_extract(data,'$.action.arguments.action'),''))) IN "
                "(SELECT value FROM json_each(:routine_calls))))")
# A context that ended keeps its question and its recovery text for a pending decision, because a recovery reads them.
# For a settled decision nothing reads them, and they made up 58 of the 67 MB of the contexts of a live store.
# The question keeps this many characters, to name the context.
CONTEXT_QUESTION_CHARACTERS = 200
# A task that ended keeps the context text of its last claim. A new claim writes it again. Every pass that reads the
# whole table decodes it, so it shrinks after this time. The claim keeps a short start of its context, because some
# code tests whether a claim has one. The recovery copies (_self_recovery.work and .decisions) stay: a native retry
# or a block check of a task that ended long ago copies them into its next recovery.
TASK_HISTORY_DAYS = 3
CLAIM_CONTEXT_CHARACTERS = 200
TASK_HISTORY_FIELDS = (
    ("json_type(data,'$._kanban_claim.context')='text' AND length(json_extract(data,'$._kanban_claim.context'))>:keep",
     "json_set(data,'$._kanban_claim.context',substr(json_extract(data,'$._kanban_claim.context'),1,:keep))"),
)
BATCH_ROWS = 1000
# A pause between batches lets waiting requests take the service lock.
BATCH_PAUSE_SECONDS = 0.05
# An old store without incremental vacuum gets one full VACUUM above this free-page fraction.
VACUUM_MIN_FREELIST_RATIO = 0.25
# VACUUM writes a temporary copy of the used pages and then the same pages to the WAL. It runs only when the disk
# keeps this much space after two such copies, so that it never fills the disk of a busy host.
VACUUM_DISK_RESERVE_BYTES = 2 * 1024 ** 3
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
        self._next_database_maintenance = current + DATABASE_RETRY_SECONDS
        retire = self._open_retention_backup(current)
        try:
            result = dict(events=self._prune_events(current, retire), receipts=self._prune_receipts(current, retire),
                          contexts=self._prune_contexts(), tasks=self._prune_tasks(current))
            self._close_retention_backup(complete=not self._maintenance_stopped())
        except BaseException:
            self._close_retention_backup(complete=False)
            raise
        result['vacuum'] = self._release_free_pages()
        logs = self.root / 'logs'
        result['rotated'] = [name for fd, name in ((1, 'service.log'), (2, 'error.log')) if rotate_log(fd, logs / name)]
        self._next_database_maintenance = current + DATABASE_MAINTENANCE_SECONDS
        result['retired'] = retire
        if result['events'] or result['receipts'] or result['contexts'] or result['tasks'] or result['vacuum']:
            logging.getLogger(__name__).warning('coconut.database_maintenance %s', json.dumps(result))
        return result

    def _open_retention_backup(self, current):
        """Return True when this pass may delete the history of retired tasks.

        The first such pass copies each row that it deletes to a new backup file. It runs only when the disk keeps
        VACUUM_DISK_RESERVE_BYTES after a copy as large as the store. Later passes delete without a copy. Old backups go.
        """
        self._retention_backup = None
        backups = self.root / 'backups'
        for path in backups.glob('retention-*.sqlite3'):
            if path.stat().st_mtime < current - BACKUP_DAYS * 86400:
                path.unlink()
        if self._setting('retention_backup'):
            return True
        size = (self.root / 'coconut.sqlite3').stat().st_size
        if shutil.disk_usage(self.root).free < size + VACUUM_DISK_RESERVE_BYTES:
            logging.getLogger(__name__).warning('coconut.retention_backup_waits %s', json.dumps(dict(store_bytes=size)))
            return False
        backups.mkdir(exist_ok=True, mode=0o700)
        path = backups / f"retention-{datetime.fromtimestamp(current, timezone.utc):%Y%m%d}.sqlite3"
        backup = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        os.chmod(path, 0o600)
        backup.executescript(BACKUP_SCHEMA)
        self._retention_backup = (path, backup)
        return True

    def _close_retention_backup(self, complete):
        backup, self._retention_backup = getattr(self, '_retention_backup', None), None
        if backup is None:
            return
        backup[1].close()
        with contextlib.closing(sqlite3.connect(backup[0])) as copy:
            copied = copy.execute('SELECT EXISTS(SELECT 1 FROM events) OR EXISTS(SELECT 1 FROM native_receipts)').fetchone()[0]
        if not copied:
            # A pass that deleted nothing copied nothing. The first pass that deletes rows still makes the backup.
            backup[0].unlink()
            return
        if complete:
            with self.transaction():
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('retention_backup', ?)", (json.dumps(backup[0].name),))

    def _delete_rows(self, table, where, arguments):
        """Delete the rows of table that match where. During the first pass that retires tasks, copy them first."""
        backup = getattr(self, '_retention_backup', None)
        if backup is None:
            return self.db.execute(f'DELETE FROM {table} WHERE {where}', arguments).rowcount
        columns = 'seq,type,bot_id,task_id,data,created_at' if table == 'events' else 'id,data'
        key = columns.split(',')[0]
        rows = self.db.execute(f'SELECT {columns} FROM {table} WHERE {where}', arguments).fetchall()
        if not rows:
            return 0
        # The copy commits before the deletion, in one transaction for the batch. A deletion that rolls back leaves an
        # extra copy, never a lost row.
        with backup[1]:
            backup[1].execute('BEGIN')
            backup[1].executemany(f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' * len(rows[0]))})", rows)
        return self.db.execute(f'DELETE FROM {table} WHERE {key} IN (SELECT value FROM json_each(?))',
                               (json.dumps([row[0] for row in rows]),)).rowcount

    @staticmethod
    def _retired_tasks(db, current):
        """The IDs of the tasks that ended more than HISTORY_DAYS ago and did not change since."""
        return [row[0] for row in db.execute(
            "SELECT id FROM tasks WHERE json_extract(data,'$.status') IN ('completed','failed','cancelled') "
            "AND COALESCE(json_extract(data,'$.updated_at'),json_extract(data,'$.created_at'),'')<?",
            (stamp(current - HISTORY_DAYS * 86400),))]

    def _maintenance_stopped(self):
        return self.stopping.is_set() or self.restart_pending

    def _prune_events(self, current, retire=False):
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
            # A retired task that runs again changes, so its new events are recent and stay.
            retired = json.dumps(self._retired_tasks(view.db, current) if retire else [])
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
                    deleted += self._delete_rows('events', f'seq>:after AND seq<=:last AND NOT ({KEPT_EVENTS})',
                        dict(after=after, last=old[-1], latest=latest, routines=routines, retired=retired))
            if len(old) < BATCH_ROWS:
                break
            after = old[-1]
            self.stopping.wait(BATCH_PAUSE_SECONDS)
        return deleted

    def _prune_receipts(self, current, retire=False):
        """Delete the completed receipts of outcomes that stayed idle for RECEIPT_RETENTION_DAYS.

        All tasks of an idle outcome are finished, and none changed within that time. The rule keeps a receipt that
        a lookup still reads:
        - an executing receipt;
        - the receipts of a task with a pending completion (PENDING_COMPLETIONS), because the task resumes;
        - a receipt that a decision (_receipt_id), a task (_continuation_receipt_ids), or a waiting receipt
          (_wait_process_receipt) names, because _get fails without it;
        - the receipts of a task in the scope of a pending decision, because its freshness fingerprint lists them;
        - the receipts of a task with a queued or working context;
        - kanban_create receipts and the cronjob_manage calls in KEPT_ROUTINE_CALLS (KEPT_RECEIPT).

        With `retire`, a task that ended HISTORY_DAYS ago loses its completed receipts also when its outcome is not
        idle, for example a chat task of a bot that works every day. The rules above still keep what a lookup reads.
        """
        cutoff = current - RECEIPT_RETENTION_DAYS * 86400
        history = current - HISTORY_DAYS * 86400
        with self.lock, self._shared_reads():
            tasks = self._task_map()
            pending = self._shared_pending_receipts()
            groups = {}
            for task in tasks.values():
                groups.setdefault(execution_key(task), []).append(task)
            native = dict(self.kanban.tasks) if self.kanban else {}
            by_native_id = {n['id']: n for n in native.values()}
            waiting = {r['task_id'] for r in pending}
            def ended(task, before):
                changed = seconds(task.get('updated_at') or task.get('created_at'))
                return (changed is not None and changed < before and task['status'] in FINISHED and task['id'] not in waiting
                        and not (self.running.get(task['id']) and self.running[task['id']].is_alive())
                        and (native.get(task['id']) or by_native_id.get(task.get('_native_outcome_id')) or {}).get('status', 'done')
                        in NATIVE_IDLE_STATUSES)
            idle = {key for key, members in groups.items() if all(ended(t, cutoff) for t in members)}
            decisions = self._all_shared('decisions')
            artifacts = self._all_shared('artifacts')
            held = {c['task_id'] for c in self._all_shared('contexts') if c['status'] in ('queued', 'working')}
            scoped = set()
            for decision in decisions:
                if decision['status'] == 'pending' and decision['task_id'] in tasks:
                    scoped.add(decision['id'])
                    held.add(decision['task_id'])
                    held.update(self._decision_scope(decision, tasks, pending, artifacts)[0])
            # A candidate of an idle outcome names its outcome. A retired task of a busy outcome names none, so only its
            # own state keeps it.
            candidates = {t['id']: execution_key(t) for t in tasks.values() if execution_key(t) in idle and t['id'] not in held}
            if retire:
                candidates.update({t['id']: None for t in tasks.values()
                                   if t['id'] not in candidates and t['id'] not in held and ended(t, history)})
            named = ({d['_receipt_id'] for d in decisions if d.get('_receipt_id')}
                     | {i for t in tasks.values() for i in t.get('_continuation_receipt_ids', [])}
                     | {r['_wait_process_receipt'] for r in pending if r.get('_wait_process_receipt')})
        if not candidates:
            return 0
        with self._read_view() as view, view._read_transaction():
            rows = [row[0] for row in view.db.execute(
                "SELECT rowid FROM native_receipts WHERE json_extract(data,'$.task_id') IN (SELECT value FROM json_each(:tasks)) "
                "AND json_extract(data,'$.state')='completed' AND NOT " + KEPT_RECEIPT + " ORDER BY rowid",
                dict(tasks=json.dumps(sorted(candidates)), routine_calls=json.dumps(KEPT_ROUTINE_CALLS)))]
        deleted = 0
        for start in range(0, len(rows), BATCH_ROWS):
            if self._maintenance_stopped():
                break
            with self.transaction():
                # A turn, a context, a wait, or a decision can start between batches. Indexes find them quickly.
                decisions = self.db.execute("SELECT id, json_extract(data,'$.task_id'), json_extract(data,'$.status'), "
                                            "json_extract(data,'$._receipt_id') FROM decisions").fetchall()
                if any(status == 'pending' and identity not in scoped for identity, _, status, _ in decisions):
                    # A new pending decision has a scope that this pass did not compute. The next pass computes it.
                    break
                live = [json.loads(row[0]) for row in self.db.execute(
                    "SELECT data FROM tasks WHERE json_extract(data,'$.status') IN (SELECT value FROM json_each(?))",
                    (json.dumps(sorted(self.ACTIVE)),))]
                pending = self._pending_completion_receipts()
                keys = {execution_key(t) for t in live}
                excluded = {identity for identity, key in candidates.items() if key is not None and key in keys}
                excluded.update(t['id'] for t in live)
                excluded.update(row[0] for row in self.db.execute(
                    "SELECT json_extract(data,'$.task_id') FROM contexts WHERE json_extract(data,'$.status') IN ('queued','working')"))
                excluded.update(task_id for _, task_id, status, _ in decisions if status == 'pending')
                excluded.update(r['task_id'] for r in pending)
                names = (named | {i for t in live for i in t.get('_continuation_receipt_ids', [])}
                         | {receipt for *_, receipt in decisions if receipt}
                         | {r['_wait_process_receipt'] for r in pending if r.get('_wait_process_receipt')})
                deleted += self._delete_rows('native_receipts',
                    'rowid IN (SELECT value FROM json_each(?)) '
                    "AND json_extract(data,'$.state')='completed' AND NOT COALESCE((" + PENDING_COMPLETIONS + '), '
                    + UNKNOWN_COMPLETION_KEPT + ') '
                    'AND id NOT IN (SELECT value FROM json_each(?)) '
                    "AND json_extract(data,'$.task_id') NOT IN (SELECT value FROM json_each(?))",
                    (json.dumps(rows[start:start + BATCH_ROWS]), json.dumps(sorted(names)),
                     json.dumps(sorted(excluded - {None}))))
            self.stopping.wait(BATCH_PAUSE_SECONDS)
        return deleted

    def _prune_contexts(self):
        """Shorten the question and drop the recovery text of the contexts that ended for a settled decision.

        The recovery count goes with the text: `_start_context` reads the text whenever the count is set, and a copy of
        an ended context keeps both fields, so the two must never part.

        A dismissed proposal stays as it is while its Undo window is open (`inbox_undo_until` is set, for 10 seconds): it
        returns to pending when a person restores it (DecisionInbox), with the same revision, and a recovery then reads
        its contexts. After the window, finish_inbox_swipes clears the mark, and the proposal cannot return.

        A recovery reads `question`, `_context_recovery`, and `_recovery_unknown_tools` of a context only for a
        pending decision (WorkRecovery._queue_context_recoveries and DecisionFreshness._refresh_recovery_records).
        Every pass that reads the whole table decodes this text. The rows stay, with their status and links.
        """
        shortened = 0
        while not self._maintenance_stopped():
            with self.transaction():
                ids = [row[0] for row in self.db.execute(
                    "SELECT id FROM contexts WHERE json_extract(data,'$.status') IN ('completed','failed','cancelled') "
                    "AND (json_type(data,'$._context_recovery') IS NOT NULL OR json_type(data,'$._recovery_unknown_tools') IS NOT NULL "
                    "OR length(COALESCE(json_extract(data,'$.question'),'')) > ?) "
                    "AND COALESCE(json_extract(data,'$.decision_id'),'') NOT IN "
                    "(SELECT id FROM decisions WHERE json_extract(data,'$.status')='pending' "
                    "OR (json_extract(data,'$.status')='dismissed' AND json_extract(data,'$.inbox_undo_until') IS NOT NULL)) LIMIT ?",
                    (CONTEXT_QUESTION_CHARACTERS, BATCH_ROWS))]
                if not ids:
                    break
                self.db.execute(
                    "UPDATE contexts SET data=json_set(json_remove(data,'$._context_recovery','$._recovery_unknown_tools','$._recovery_attempt'),"
                    "'$.question',substr(COALESCE(json_extract(data,'$.question'),''),1,?)) "
                    "WHERE id IN (SELECT value FROM json_each(?))", (CONTEXT_QUESTION_CHARACTERS, json.dumps(ids)))
                cache = self._shared_cache()
                if cache is not None:
                    cache.pop('contexts', None)
                shortened += len(ids)
            if len(ids) < BATCH_ROWS:
                break
            self.stopping.wait(BATCH_PAUSE_SECONDS)
        return shortened

    def _prune_tasks(self, current):
        """Shorten the claim context of the tasks that ended more than TASK_HISTORY_DAYS ago.

        A later claim of the same task writes the context again. The rows stay, with their status, links, and the
        marks that other code tests. A task that fails after a long wait shortens at once, which is safe: nothing
        reads the context of an ended claim.
        """
        cutoff, shortened = stamp(current - TASK_HISTORY_DAYS * 86400), 0
        for condition, change in TASK_HISTORY_FIELDS:
            while not self._maintenance_stopped():
                arguments = dict(cutoff=cutoff, keep=CLAIM_CONTEXT_CHARACTERS, rows=BATCH_ROWS)
                with self.transaction():
                    ids = [row[0] for row in self.db.execute(
                        "SELECT id FROM tasks WHERE json_extract(data,'$.status') IN ('completed','failed','cancelled') "
                        "AND COALESCE(json_extract(data,'$.updated_at'),json_extract(data,'$.created_at'),'')<:cutoff "
                        f'AND {condition} LIMIT :rows', arguments)]
                    if not ids:
                        break
                    self.db.execute(f'UPDATE tasks SET data={change} WHERE id IN (SELECT value FROM json_each(:ids))',
                                    dict(arguments, ids=json.dumps(ids)))
                    cache = self._shared_cache()
                    if cache is not None:
                        for key in ('tasks', 'task map', 'graphs', 'latest graphs'):
                            cache.pop(key, None)
                    shortened += len(ids)
                if len(ids) < BATCH_ROWS:
                    break
                self.stopping.wait(BATCH_PAUSE_SECONDS)
        return shortened

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
                elif (pages and free / pages > VACUUM_MIN_FREELIST_RATIO and self._database_quiet()
                      and self._vacuum_fits(pages - free)):
                    started = time.monotonic()
                    self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
                    self.db.execute('VACUUM')
                    # VACUUM can renumber rowids, so the kept tasks can lose their row order, and the exchange
                    # positions of the message pages can name other rows.
                    self._forget_cached_tasks(renumbered=True)
                    self._forget_kept_history()
                    with self.transaction():
                        index_exchange_audience(self.db, rebuild=True)
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

    def _vacuum_fits(self, used_pages):
        size = self.db.execute('PRAGMA page_size').fetchone()[0]
        return shutil.disk_usage(self.root).free >= 2 * used_pages * size + VACUUM_DISK_RESERVE_BYTES

    def _database_quiet(self):
        """Return True when no task worker, context, or native run is live. The caller holds the service lock.

        A queued task can wait for capacity for hours. It starts only through the service lock, so it does not block.
        """
        if any(thread.is_alive() for thread in list(self.running.values()) + list(self.context_threads.values())):
            return False
        return not any(native.get('status') == 'running' for native in (self.kanban.tasks.values() if self.kanban else ()))
