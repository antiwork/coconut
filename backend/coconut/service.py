"""Durable local coordinator. Providers propose actions; this service enforces them."""
from __future__ import annotations

import base64
import contextlib
import copy
import fcntl
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import unicodedata
from concurrent.futures import Future
from queue import Empty, Queue
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4, uuid5

from .pr_tracking import REVIEW_CHECK_NAMES, GitHubPRTracker, PRTrackingError, parse_pr_url
from .routines import validate_schedule, next_run
from .native_conversation import reaction_only_turn
from .profiles import HermesProfiles, ProfileError, profile_id
from .state_sync import READ_INDEXES, StateSync, index_exchange_audience


class ServiceError(Exception):
    def __init__(self, status: int, message: str, **details):
        super().__init__(message)
        self.status = status
        # Extra fields of the error response, for example a code that the phone acts on.
        self.details = details


class YieldForDecision(RuntimeError):
    """Stop provider output, optionally after a durable decision."""

    def __init__(self, message, *, decision_id=None):
        super().__init__(message)
        self.decision_id = decision_id


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def uid() -> str:
    return str(uuid4())


def encoded(value: Any) -> str:
    return json.dumps(value, separators=(',', ':'), sort_keys=True)


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def migrate_device_peers(db):
    """Add the tunnel columns to a store from before the relay transport.

    `devices.peer_key` is the static Noise key of a phone that paired through the tunnel sidecar. `pairing.tunnel` marks
    a pairing secret from a version 2 link, which pairs only through the tunnel.
    """
    for table, column, kind in (('devices', 'peer_key', 'TEXT'), ('pairing', 'tunnel', 'INTEGER NOT NULL DEFAULT 0')):
        columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        # A missing table comes later from the full schema, and this runs again then.
        if columns and column not in columns:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')


def issue_pairing_code(db, lifetime: float, tunnel: bool = False) -> tuple[str, int]:
    """Store the hash of a new pairing code, and return the code and its expiry in Unix seconds.

    A tunnel secret is for a version 2 link: 32 random bytes that pair only through the tunnel sidecar.
    """
    code = tunnel_secret() if tunnel else ''.join(secrets.choice('0123456789') for _ in range(10))
    expires = int(time.time() + lifetime)
    db.execute('DELETE FROM pairing WHERE expires < ? OR used=1', (time.time(),))
    db.execute('INSERT INTO pairing(hash,expires,tunnel) VALUES (?,?,?)', (digest(code), expires, int(tunnel)))
    return code, expires


# A version 2 link is valid for ten minutes, because the person may get it from Hermes in a chat.
TUNNEL_PAIRING_LIFETIME = 600


def pair_device(db, data: dict, paired, peer: str | None = None) -> dict | ServiceError:
    """Use a pairing code inside the caller's write transaction. Return the device token, or the error to raise.

    Failed attempts commit with the transaction, so retries cannot bypass the rate limit. `paired(device_id)` records the event.
    `peer` is the phone's static key from the tunnel sidecar, or None for a request to the main loopback listener. A tunnel
    secret pairs only through the tunnel, and a 10-digit code only on the main listener, for a tool on this Mac. The
    device keeps the key, and `device_for_token` requires it.
    """
    stamp = time.time()
    db.execute('DELETE FROM pair_attempts WHERE created < ?', (stamp - 60,))
    attempts = db.execute('SELECT COUNT(*) FROM pair_attempts').fetchone()[0]
    db.execute('INSERT INTO pair_attempts(created) VALUES (?)', (stamp,))
    row = db.execute('SELECT expires,used,tunnel FROM pairing WHERE hash=?', (digest(str(data.get('code', ''))),)).fetchone()
    if attempts >= 5:
        return ServiceError(429, 'Too many pairing attempts. Wait one minute.')
    if not row or row[0] < stamp or row[1] or bool(row[2]) != (peer is not None):
        return ServiceError(401, 'The pairing code is invalid or expired.')
    name = str(data.get('device_name', 'iPhone'))[:80]
    device_id, token = uid(), secrets.token_urlsafe(32)
    db.execute('UPDATE pairing SET used=1 WHERE hash=?', (digest(str(data['code'])),))
    db.execute('INSERT INTO devices(id,name,token_hash,created_at,peer_key) VALUES (?,?,?,?,?)',
               (device_id, name, digest(token), now(), peer))
    paired(device_id)
    return {'token': token, 'device_id': device_id}


def tunnel_secret() -> str:
    """The one-time secret of a version 2 pairing link: 32 random bytes in unpadded base64url."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')


def device_for_token(db, token: str, peer: str | None = None) -> str:
    """Return the device of a token. A phone that paired through the tunnel must connect with the same static key."""
    row = db.execute('SELECT id,peer_key FROM devices WHERE token_hash=?', (digest(token),)).fetchone()
    if not row or row[1] != peer:
        raise ServiceError(401, 'Pair this phone with the Mac to continue.')
    return row[0]


def require_text(data: dict, key: str, maximum: int | None = 10000) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or (maximum is not None and len(value) > maximum):
        bound = f'between 1 and {maximum} characters' if maximum is not None else 'nonempty text'
        raise ServiceError(400, f'{key} must contain {bound}.')
    return value.strip()


def decision_correction(data: dict) -> str:
    value = data.get('correction')
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > 10000:
        raise ServiceError(400, 'Corrections must contain at most 10000 characters.')
    return value


from .work_sources import WorkSources, card_answer_steps
from .work_status import WorkStatus
from .background_processes import BackgroundProcesses, PENDING_COMPLETIONS
from .native_execution import NativeExecution
from .decision_inbox import DecisionInbox, migrate_notification_importance
from .pull_request_reviews import PENDING_CARDS, PR_ARTIFACTS, PR_URL_INDEX, PullRequestReviews
from .work_progress import WorkProgress, work_enabled


from .routine_coordinator import (CHILD_TASKS, NativeRoutines, OPEN_NATIVE_JOB, OPEN_ROUTINE_TASK, OPEN_ROUTINE_TASKS,
                                  RUNNING_ROUTINE_TASKS)
from .approval_updates import ApprovalUpdates
from .parallel_work_updates import ParallelWorkUpdates
from .native_fallback_updates import NativeFallbackUpdates
from .task_cache import SCHEMA as TASK_CHANGES_SCHEMA, TaskCache
from .work_capacity import execution_key, state as capacity_state
from .kanban_scheduler import NativeKanban


from .service_restart import ServiceRestart
from .workspace_recovery import WorkspaceRecovery
from .worktree_retirement import WorktreeRetirement
from .session_maintenance import SessionMaintenance
from .database_maintenance import DatabaseMaintenance
from .tracked_work import TrackedWork
from .work_recovery import WorkRecovery
from .decision_freshness import HUMAN_MESSAGE, DecisionFreshness
from .withdrawal_corrections import WithdrawalCorrections
from .silent_responses import SilentResponses, is_silent_message, is_silent_response
from .technical_lane import TechnicalLane
from .hermes_setup import HermesSetup
from .bot_profiles import BotProfiles
from .gateway_chat import GatewayChat


# The statements of Service._refresh_bot. Each needs an index and no sort. A test reads the plan of each one.
BOT_TASK_STATES = ("SELECT DISTINCT json_extract(data,'$.status') FROM tasks WHERE json_extract(data,'$.bot_id')=? "
                   "AND json_extract(data,'$.status') IN ('working','needs_you','scheduled')")
BOT_LATEST_TASK = "SELECT json_extract(data,'$.status') FROM tasks WHERE json_extract(data,'$.bot_id')=? ORDER BY rowid DESC LIMIT 1"
BOT_WORKING_CONTEXT = ("SELECT 1 FROM contexts WHERE json_extract(data,'$.status')='working' "
                       "AND json_extract(data,'$.bot_id')=? LIMIT 1")
# Task fields that the dispatcher looks for on every pass. Each has a partial index, so a pass reads only the tasks
# that carry the field and not the whole table.
MARKS = ('_return_pending', '_native_reply_pending', '_work_status_only')


def marked(field):
    """The SQL condition of a task that has a marker in the field. It is the condition of the index of the same field."""
    if field not in MARKS:
        raise ValueError(field)
    # A stored false or null is not a marker. The fields are objects or true.
    return f"json_type(data,'$.{field}') IN ('object','true')"


class Service(TaskCache, NativeFallbackUpdates, NativeKanban, StateSync, DecisionFreshness, WithdrawalCorrections, WorkRecovery, WorkSources, SilentResponses, TrackedWork, WorkStatus, WorkProgress, PullRequestReviews, DecisionInbox, BackgroundProcesses, NativeExecution, NativeRoutines, ApprovalUpdates, ParallelWorkUpdates, ServiceRestart, WorkspaceRecovery, WorktreeRetirement, SessionMaintenance, DatabaseMaintenance, TechnicalLane, HermesSetup, BotProfiles, GatewayChat):
    ACTIVE = {'queued', 'working', 'scheduled', 'needs_you', 'paused', 'waiting'}
    TABLES = ('bots', 'tasks', 'messages', 'decisions', 'artifacts', 'routines')
    BOT_CHANGE_LIMIT = 8
    ACTIONS = {'create_file'}

    def __init__(self, data_dir: str | Path, provider: Any, *, start_workers: bool = True, pr_tracker: Any = None,
                 hermes_home: str | Path | None = None):
        """hermes_home is the person's Hermes home. Each of its profiles is a bot (bot_profiles)."""
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.process_lock = open(self.root / 'service.lock', 'a')
        try:
            fcntl.flock(self.process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.process_lock.close()
            raise ServiceError(409, 'Another Coconut service already owns this data directory.')
        self.files = self.root / 'artifacts'
        self.files.mkdir(exist_ok=True, mode=0o700)
        self.provider = provider
        self._init_kanban()
        self.profiles = HermesProfiles(self.root)
        self.pr_tracker = pr_tracker
        self._profile_trackers = {}
        self.pr_refresh_requested = threading.Event()
        self.pr_refresh_lock = threading.Lock()
        self._pending_decision_sources = set()
        self._ready_decision_sources = None
        self.pr_follow_lock = threading.Lock()
        self.pr_last_request = float('-inf')
        self.lock = threading.RLock()
        self.dispatch_gate = threading.RLock()
        self.model_settings_lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.db = sqlite3.connect(self.root / 'coconut.sqlite3', check_same_thread=False, isolation_level=None)
        os.chmod(self.root / 'coconut.sqlite3', 0o600)
        # This takes effect only in a new store. An older store changes mode in DatabaseMaintenance.
        self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        for table in self.TABLES:
            self.db.execute(f'CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.db.executescript(TASK_CHANGES_SCHEMA)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, bot_id TEXT, task_id TEXT, data TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pairing (hash TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS pair_attempts (created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS exchanges (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS routine_occurrences (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contexts (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_creation_receipts (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_deletion_receipts (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workspace_recoveries (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS native_receipts (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS artifact_content (id TEXT PRIMARY KEY, content BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS native_receipts_executing ON native_receipts
            (json_extract(data, '$.task_id'), json_extract(data, '$.turn_id'))
            WHERE json_extract(data, '$.state')='executing';
        CREATE INDEX IF NOT EXISTS native_receipts_task ON native_receipts (json_extract(data, '$.task_id'));
        CREATE INDEX IF NOT EXISTS decisions_task ON decisions (json_extract(data,'$.task_id'));
        CREATE INDEX IF NOT EXISTS tasks_source_assignment ON tasks (json_extract(data,'$._source_assignment.id'));
        CREATE INDEX IF NOT EXISTS tasks_execution ON tasks (json_extract(data,'$._execution_session_id'));
        CREATE INDEX IF NOT EXISTS native_receipts_creation_key ON native_receipts
            (json_extract(data, '$.action.arguments.idempotency_key'))
            WHERE json_extract(data, '$.action.name')='kanban_create';
        CREATE INDEX IF NOT EXISTS messages_task ON messages (json_extract(data, '$.task_id'));
        CREATE INDEX IF NOT EXISTS messages_chat ON messages (json_extract(data, '$.bot_id'));
        CREATE INDEX IF NOT EXISTS messages_decision ON messages (json_extract(data, '$.decision_id'));
        CREATE INDEX IF NOT EXISTS tasks_bot_session ON tasks (json_extract(data,'$.bot_id'), json_extract(data,'$.session_id'));
        CREATE INDEX IF NOT EXISTS deliveries_source ON deliveries (json_extract(data,'$.source_task_id'));
        CREATE INDEX IF NOT EXISTS events_task ON events (task_id, seq);
        CREATE INDEX IF NOT EXISTS deliveries_receipt ON deliveries (json_extract(data,'$.receipt.task_id'));
        CREATE INDEX IF NOT EXISTS tasks_status ON tasks (json_extract(data,'$.status'));
        CREATE INDEX IF NOT EXISTS tasks_bot_status ON tasks (json_extract(data,'$.bot_id'), json_extract(data,'$.status'));
        CREATE INDEX IF NOT EXISTS tasks_bot ON tasks (json_extract(data,'$.bot_id'));
        CREATE INDEX IF NOT EXISTS contexts_status ON contexts (json_extract(data,'$.status'));
        CREATE INDEX IF NOT EXISTS decisions_status ON decisions (json_extract(data,'$.status'));
        CREATE INDEX IF NOT EXISTS decisions_undo ON decisions (id) WHERE json_extract(data,'$.inbox_undo_until') IS NOT NULL;
        CREATE INDEX IF NOT EXISTS decisions_merge_url ON decisions (lower(json_extract(data,'$.merge_pr_url')));
        CREATE INDEX IF NOT EXISTS tasks_assignment ON tasks (json_extract(data, '$._assignment_task_id'));
        CREATE INDEX IF NOT EXISTS tasks_blocked ON tasks (id)
            WHERE json_extract(data, '$.status') IN ('needs_you', 'paused');
        CREATE TABLE IF NOT EXISTS receipts (decision_id TEXT PRIMARY KEY, state TEXT NOT NULL, artifact_id TEXT NOT NULL, content_hash TEXT NOT NULL, detail TEXT NOT NULL);
        ''')
        migrate_device_peers(self.db)
        self._init_profiles(hermes_home)
        self.db.execute('CREATE INDEX IF NOT EXISTS native_receipts_pending_completion ON native_receipts (id) WHERE ' + PENDING_COMPLETIONS)
        for field in MARKS:
            self.db.execute(f'CREATE INDEX IF NOT EXISTS tasks{field} ON tasks (id) WHERE ' + marked(field))
        self.db.execute('CREATE INDEX IF NOT EXISTS tasks_native_job_open ON tasks (id) WHERE ' + OPEN_NATIVE_JOB)
        self.db.execute('CREATE INDEX IF NOT EXISTS tasks_routine_open ON tasks (id) WHERE ' + OPEN_ROUTINE_TASK)
        self.db.execute("CREATE INDEX IF NOT EXISTS tasks_source ON tasks (json_extract(data,'$.source_task_id'))")
        self.db.execute("CREATE INDEX IF NOT EXISTS tasks_relay_callback_owner ON tasks (json_extract(data,'$._relay_callback_owner'))")
        self.db.execute('CREATE INDEX IF NOT EXISTS messages_human ON messages (id) WHERE ' + HUMAN_MESSAGE)
        self.db.executescript(READ_INDEXES + PR_URL_INDEX)
        self._request_device_id = None
        self._request_key = None
        self._message_interaction = None
        self.stopping = threading.Event()
        self._init_gateway_chat()
        self.generation = uid()
        self.restart_enabled = False
        self.restart_pending = False
        self.restart_deadline = None
        self.running: dict[str, threading.Thread] = {}
        self.bot_locks: dict[str, threading.Lock] = {}
        self.context_threads: dict[str, threading.Thread] = {}
        self.dispatcher: threading.Thread | None = None
        self.chat_dispatcher: threading.Thread | None = None
        self._kanban_dispatching = False
        self._kanban_claiming = 0
        self._kanban_claim_outcomes = set()
        self._kanban_claim_bots = set()
        # Bots whose session store a VACUUM rewrites. A bot in this set starts no new turn (see session_maintenance).
        self._store_hold = set()
        self._kanban_trimming = False
        self._dependency_unlinking = False
        self._kanban_dispatch_uncertain = False
        self.pr_poller: threading.Thread | None = None
        self.process_poller: threading.Thread | None = None
        self.tool_checker: threading.Thread | None = None
        self.source_poller: threading.Thread | None = None
        from .push import PushDelivery
        self.push = PushDelivery(self)
        try:
            self._recover_workspace_binding()
            if not getattr(self, '_workspace_recovery_hold', False):
                if hasattr(self.provider, 'migrate_model_default'):
                    self.provider.migrate_model_default(self._setting('model'), self._setting('base_url'))
                elif hasattr(self.provider, 'configure') and self._setting('model'):
                    self.provider.configure(model=self._setting('model'), base_url=self._setting('base_url') or None)
            if self.kanban and not getattr(self, '_workspace_recovery_hold', False):
                self.kanban.call({'op': 'snapshot'})
            self._index_message_audience()
            with self.transaction():
                index_exchange_audience(self.db)
            with self.transaction():
                self._migrate_threads()
                self._migrate_bot_exchanges()
                self._migrate_process_deliveries()
                self._migrate_activity_notices()
                self._migrate_decision_link_notices()
                self._migrate_silent_responses()
                self._migrate_pr_review_sources()
                self._migrate_native_routine_prompts()
                migrate_notification_importance(self.db)
                self._move_artifact_content()
                if not getattr(self, '_workspace_recovery_hold', False):
                    self._recover()
        except BaseException:
            if self.kanban:
                self.kanban.close()
            self.db.close()
            fcntl.flock(self.process_lock.fileno(), fcntl.LOCK_UN)
            self.process_lock.close()
            raise
        with self.transaction():
            for bot in self._all('bots'):
                bot.pop('memory', None)
                bot.pop('preferences', None)
                bot.setdefault('notifications', True)
                self._put('bots', bot)
            self.db.execute('DROP TABLE IF EXISTS policies')
            self.db.execute('DROP TABLE IF EXISTS preference_receipts')
        try:
            checkout_was_held = getattr(self, '_workspace_recovery_hold', False)
            self._admit_checkout_recovery()
            if checkout_was_held and not self._workspace_recovery_hold:
                if hasattr(self.provider, 'migrate_model_default'):
                    self.provider.migrate_model_default(self._setting('model'), self._setting('base_url'))
                elif hasattr(self.provider, 'configure') and self._setting('model'):
                    self.provider.configure(model=self._setting('model'), base_url=self._setting('base_url') or None)
        except BaseException:
            if self.kanban:
                self.kanban.close()
            self.db.close()
            fcntl.flock(self.process_lock.fileno(), fcntl.LOCK_UN)
            self.process_lock.close()
            raise
        try:
            self.rescan_profiles()
        except Exception:
            logging.getLogger(__name__).exception('The Hermes profile scan failed at start. The watcher tries again.')
        if start_workers:
            self.start()
        if hasattr(self.provider, 'subscribe_model_catalog'):
            self.provider.subscribe_model_catalog(self._catalog_changed)

    def _catalog_changed(self):
        with self.lock:
            if self.stopping.is_set():
                return
            with self.transaction():
                self._event('model_catalog.updated', {})

    @contextlib.contextmanager
    def transaction(self):
        import sys
        from .turn_timings import timed_lock
        with timed_lock(self.lock, sys._getframe(2).f_code.co_name):
            # Unlink validates service guards, then calls the adapter without
            # this lock. Defer writes until that short native operation ends.
            while getattr(self, '_dependency_unlinking', False):
                self.changed.wait()
            self.db.execute('BEGIN IMMEDIATE')
            self._profile_pending = {}
            try:
                yield
                self.db.execute('COMMIT')
                self.__dict__.pop('_published_in_transaction', None)
                for bot_id in self.__dict__.pop('_forget_bots_on_commit', set()):
                    self.__dict__.get('_published_bots', {}).pop(bot_id, None)
                self.changed.notify_all()
            except BaseException:
                self._profile_pending = {}
                # A rollback removes the bot events of this transaction. Forget what only they said.
                for bot_id in self.__dict__.pop('_published_in_transaction', set()):
                    self.__dict__.get('_published_bots', {}).pop(bot_id, None)
                self.__dict__.pop('_forget_bots_on_commit', None)
                # The kept tasks can hold rows that this rollback removes. Forget them first: ROLLBACK can raise.
                self._forget_cached_tasks()
                self.db.execute('ROLLBACK')
                if getattr(self.profiles, 'rows', None) is not None:
                    self._reload_profile_rows()
                raise
            pending, self._profile_pending = self._profile_pending, {}
            for bot in pending.values():
                try:
                    self.profiles.sync(bot)
                except (ProfileError, OSError) as error:
                    raise ServiceError(503, 'The bot settings were saved, but its Hermes profile could not sync.') from error

    def _migrate_activity_notices(self):
        if self._setting('activity_notices_migrated'):
            return
        failures = set()
        for (data,) in self.db.execute("SELECT data FROM events WHERE type IN ('task.updated','task.cancelled')"):
            task = json.loads(data)
            if task.get('status') in ('failed', 'cancelled'):
                failures.add((task.get('id'), task.get('summary')))
        for message in self._all('messages'):
            if message['kind'] != 'activity':
                continue
            # Preserve recorded failures even when a later turn changes the task status.
            delivery_notice = message.get('role') == 'system' and message.get('text', '').startswith((
                'Bot messages paused after eight deliveries.', 'The result could not return to its sender:'))
            if delivery_notice or (message.get('task_id'), message.get('text')) in failures:
                message['kind'] = 'status'
                self._put('messages', message)
                self._event('message.updated', self._public(message), message['bot_id'], message.get('task_id'))
        self.db.execute("INSERT INTO settings VALUES ('activity_notices_migrated','true')")

    def _migrate_decision_link_notices(self):
        if self._setting('decision_link_notices_migrated'):
            return
        text = 'Work stopped: Link an answered question from this conversation.'
        seen, changed = set(), False
        for message in reversed(self._all('messages')):
            if (message.get('kind') != 'status' or message.get('role') != 'assistant'
                    or message.get('text') != text or message.get('_hidden')):
                continue
            # Keep one failure notice. The task and event records retain every failure.
            if message['bot_id'] in seen:
                message['_hidden'] = True
                self._put('messages', message)
                changed = True
            seen.add(message['bot_id'])
        if changed:
            self._event('transcript.updated', {})
        self.db.execute("INSERT INTO settings VALUES ('decision_link_notices_migrated','true')")

    def _all(self, table: str) -> list[dict]:
        with self.lock:
            cache = getattr(self, '_read_cache', None)
            table_key = ('table', table)
            if cache is not None and table_key in cache:
                return json.loads(cache[table_key])
            if cache is not None:
                # Keep each row's stored text for later _get calls. Encoding every parsed object again cost about
                # 0.13 s for the task table of the Mac mini data.
                rows = self.db.execute(f'SELECT id, data FROM {table} ORDER BY rowid').fetchall()
                data = '[' + ','.join(row[1] for row in rows) + ']'
                cache[table_key] = data
                for identity, text in rows:
                    cache[(f'SELECT data FROM {table} WHERE id=?', (identity,))] = (text,)
                return json.loads(data)
            # One SQLite result avoids a Python thread handoff for every stored row.
            try:
                row = self.db.execute(f"SELECT '[' || group_concat(data, ',') || ']' "
                                      f'FROM (SELECT data FROM {table} ORDER BY rowid)').fetchone()
                data = row[0] or '[]'
                return json.loads(data)
            except sqlite3.DataError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_TOOBIG:
                    raise
                # Valid rows can outgrow SQLite's limit for a single combined value.
                return [json.loads(row[0]) for row in self.db.execute(f'SELECT data FROM {table} ORDER BY rowid')]

    @contextlib.contextmanager
    def _shared_reads(self):
        """Parse each table at most once in one service operation, until the next write.

        Inside this block, _all_shared, _task_map, and _shared_pending_receipts return shared objects, and
        _work_chain reuses its graphs. Only code that reads may use them. Code that changes a row reads it with
        _all or _get and writes it with _put. Each _put drops the shared objects that the write can change, so a
        later read sees the write.
        The shared objects belong to the thread that opened the block. Other threads and read views never see them.
        The block holds the service lock, so no other thread writes or opens a second block meanwhile.
        """
        with self.lock:
            if self._shared_cache() is not None:
                yield
                return
            previous, self._shared_state = getattr(self, '_shared_state', None), (threading.get_ident(), {})
            try:
                yield
            finally:
                self._shared_state = previous

    def _shared_cache(self):
        state = getattr(self, '_shared_state', None)
        return state[1] if state is not None and state[0] == threading.get_ident() else None

    def _all_shared(self, table):
        cache = self._shared_cache()
        if cache is None:
            return self._all(table)
        if table not in cache:
            # The task list comes from the kept task map, in the same row order.
            cache[table] = list(self._task_map().values()) if table == 'tasks' else self._all(table)
        return cache[table]

    def _task_map(self):
        # _work_chain reuses a work graph for the same task map object, so every reader gets one map.
        cache = self._shared_cache()
        if cache is None:
            return {task['id']: task for task in self._all('tasks')}
        if 'task map' not in cache:
            cache['task map'] = self._cached_tasks()
        return cache['task map']

    def _shared_pending_receipts(self):
        cache = self._shared_cache()
        if cache is None:
            return self._pending_completion_receipts()
        if 'pending receipts' not in cache:
            cache['pending receipts'] = self._pending_completion_receipts()
        return cache['pending receipts']

    def _shared_work_receipts(self):
        cache = self._shared_cache()
        if cache is None:
            return self._work_receipts()
        if 'work receipts' not in cache:
            cache['work receipts'] = self._work_receipts()
        return cache['work receipts']

    def _marked_tasks(self, field):
        """The tasks with a true value in the field, in row order. A partial index finds them."""
        with self.lock:
            # No ORDER BY: it makes SQLite scan the table. The few rows sort here.
            rows = self.db.execute(f'SELECT rowid, data FROM tasks WHERE {marked(field)}').fetchall()
        return [task for task in (json.loads(row[1]) for row in sorted(rows)) if task.get(field)]

    def _read_one(self, statement, parameters):
        cache = getattr(self, '_read_cache', None)
        if cache is None:
            return self.db.execute(statement, parameters).fetchone()
        key = (statement, parameters)
        if key not in cache:
            cache[key] = self.db.execute(statement, parameters).fetchone()
        return cache[key]

    def _native_task_receipts(self, task_id, *, turn_id=None, state=None):
        clauses, values = ["json_extract(data,'$.task_id')=?"], [task_id]
        for field, value in (('turn_id', turn_id), ('state', state)):
            if value is not None:
                clauses.append(f"json_extract(data,'$.{field}')=?")
                values.append(value)
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT data FROM native_receipts WHERE ' + ' AND '.join(clauses) + ' ORDER BY rowid', values)]

    def _get(self, table: str, object_id: str) -> dict:
        with self.lock:
            row = self._read_one(f'SELECT data FROM {table} WHERE id=?', (object_id,))
        if not row:
            raise ServiceError(404, f'{table[:-1].capitalize()} was not found.')
        return json.loads(row[0])

    def _put(self, table: str, obj: dict):
        cache = self._shared_cache()
        if cache is not None:
            # Drop what this write can change. WorkStatus.GRAPH_TABLES names the tables of the work graphs.
            cache.pop(table, None)
            cache.pop({'tasks': 'task map', 'native_receipts': 'pending receipts'}.get(table), None)
            if table == 'native_receipts':
                cache.pop('work receipts', None)
            if table in self.GRAPH_TABLES:
                cache.pop('graphs', None)
                cache.pop('latest graphs', None)
            if table == 'messages':
                # Triggers keep message_audience in step with messages, so this drops every history answer.
                for key in [k for k in cache if isinstance(k, tuple) and k[0] == 'history decisions']:
                    del cache[key]
        if table == 'tasks' and obj.get('status') != 'working':
            # A phase describes only the running turn. The next turn starts without it.
            obj.pop('phase', None)
            obj.pop('turn_started_at', None)
        self.db.execute(f'INSERT INTO {table}(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (obj['id'], encoded(obj)))

    def _event(self, event_type: str, data: dict, bot_id: str | None = None, task_id: str | None = None):
        self.db.execute('INSERT INTO events(type,bot_id,task_id,data,created_at) VALUES (?,?,?,?,?)', (event_type, bot_id, task_id, encoded(data), now()))
        self.push.record(event_type, data, bot_id, task_id)

    def _message(self, bot_id: str, text: str, *, task_id: str | None = None, role: str = 'assistant', kind: str = 'text', decision_id: str | None = None, artifact_id: str | None = None, streaming: bool = False, sender_bot_id: str | None = None, recipient_bot_ids: list[str] | None = None, human_authored: bool = False, direct_reply_message_id: str | None = None, interim: bool = False) -> dict:
        task = self._get('tasks', task_id) if task_id else None
        thread_id = task['thread_id'] if task else None
        display_bot_id = (task['bot_id'] if task.get('source_bot_id') else task['conversation_bot_id']) if task else bot_id
        if sender_bot_id is None and task and role != 'user':
            sender_bot_id = task['bot_id']
        recipients = recipient_bot_ids if recipient_bot_ids is not None else ([task['bot_id']] if task and role == 'user' else [])
        if sender_bot_id and recipients and kind == 'text':
            kind = 'bot_exchange'
            display_bot_id = sender_bot_id
        message = dict(id=uid(), bot_id=display_bot_id, task_id=task_id, thread_id=thread_id, sender_bot_id=sender_bot_id, recipient_bot_ids=recipients, role=role, kind=kind, text=text, decision_id=decision_id, artifact_id=artifact_id, created_at=now(), streaming=streaming)
        if interim:
            message['_interim'] = True
        if role == 'assistant' and kind == 'text':
            direct_reply_message_id = direct_reply_message_id or (task or {}).get('_direct_reply_message_id')
            if direct_reply_message_id:
                message['_direct_reply_message_id'] = direct_reply_message_id
                display_bot_id = self._get('messages', direct_reply_message_id)['bot_id']
                message['bot_id'] = display_bot_id
            elif task and task.get('source_bot_id') and not task.get('_is_return') and not decision_id:
                message['exchange_recipient_bot_ids'] = [task['source_bot_id']]
        if human_authored and self._request_device_id is not None:
            message.update(_human_device_id=self._request_device_id, _human_source_kind='chat')
            if role == 'user' and kind == 'text' and self._request_key:
                message['client_request_id'] = self._request_key
        if human_authored and role == 'user' and self._message_interaction:
            message.update(self._message_interaction)
            if message.get('reaction_event'):
                message['_human_source_kind'] = 'reaction'
        if human_authored and role == 'user' and kind == 'text':
            message['_visible_reply_context'] = self._last_visible_reply(display_bot_id)
        if is_silent_message(message):
            message['_hidden'] = True
            self._put('messages', message)
            return message
        self._put('messages', message)
        bot = self._get('bots', display_bot_id)
        if not message.get('reaction_event'):
            bot['last_message'] = text[:160]
        if role == 'assistant':
            bot['unread_count'] += 1
        self._put('bots', bot)
        self._event('message.created', self._public(message), display_bot_id, task_id)
        return message

    def _last_visible_reply(self, bot_id):
        # Match the text rules in TranscriptPresentation before freezing chat context.
        completion = {'ready to review', 'done', 'complete', 'completed', 'task complete',
                      'task completed', 'work complete', 'work completed'}
        def label(message):
            return message.get('text', '').strip().strip('.!…').lower()
        rows = self.db.execute("SELECT data FROM messages WHERE json_extract(data,'$.bot_id')=? "
            "AND json_extract(data,'$.role')='assistant' AND json_extract(data,'$.kind')='text' "
            "AND COALESCE(json_extract(data,'$._hidden'),0)=0 AND COALESCE(json_extract(data,'$._deleted'),0)=0 "
            "AND COALESCE(json_extract(data,'$.streaming'),0)=0 "
            "AND COALESCE(json_array_length(data,'$.exchange_recipient_bot_ids'),0)=0 ORDER BY rowid DESC", (bot_id,))
        for raw in rows:
            reply = json.loads(raw[0])
            if not reply['text'].strip() or reply.get('reaction_event') or is_silent_message(reply):
                continue
            if (label(reply) in completion and reply.get('task_id') and not reply.get('decision_id')
                    and not reply.get('recipient_bot_ids')):
                author = reply.get('sender_bot_id') or reply['bot_id']
                if any(m['id'] != reply['id'] and m['bot_id'] == bot_id and m['role'] == 'assistant'
                       and (m.get('sender_bot_id') or m['bot_id']) == author
                       and m['kind'] in ('text', 'artifact', 'decision') and m['text'] and label(m) not in completion
                       and not m.get('_hidden') and not m.get('_deleted')
                       for m in self._task_messages(reply['task_id'])):
                    continue
            return self._reply_reference(reply)
        return None

    def _sync_profile(self, bot: dict):
        if self.db.in_transaction:
            self._profile_pending[bot['id']] = dict(bot)
            return
        try:
            self.profiles.sync(bot)
        except ProfileError as error:
            raise ServiceError(409, str(error)) from error
        except OSError as error:
            raise ServiceError(503, 'The bot settings were saved, but its Hermes profile could not sync.') from error


    def _human_source(self, task: dict, source_id: str, binding: dict, purpose: str) -> dict:
        if source_id not in binding.get('_preference_source_ids', []):
            raise ServiceError(409, f'This turn cannot use that message to {purpose}.')
        source = self._get('messages', source_id)
        if (not source.get('_human_device_id') or source.get('_human_source_kind') != 'chat'
                or source.get('role') != 'user' or source.get('kind') != 'text' or source.get('reaction_event')
                or source.get('sender_bot_id') is not None
                or source.get('thread_id') != task['thread_id']
                or task['bot_id'] not in source.get('recipient_bot_ids', [])):
            raise ServiceError(403, f'To {purpose}, use a direct human message addressed to this bot.')
        return source

    def _bot_creation_input(self, task: dict) -> dict:
        sources, receipts = [], []
        for source_id in task.get('_preference_source_ids', []):
            try:
                source = self._human_source(task, source_id, task, 'create bots')
            except ServiceError:
                continue
            sources.append({'id': source['id'], 'text': source['text']})
        source_ids = {source['id'] for source in sources}
        for record in self._all('bot_creation_receipts'):
            if record['source_message_id'] in source_ids:
                bot = self._get('bots', record['receipt']['bot_id'])
                if not bot.get('_deleted'):
                    # A committed bot may still need its profile after an interrupted sync.
                    self._sync_profile(bot)
                    receipts.append(record['receipt'] | {'status': 'duplicate'})
        return {'_bot_creation_sources': sources, '_bot_creation_receipts': receipts}

    def _create_bot_from_chat(self, task_id: str, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) != {'source_message_id', 'name', 'role', 'instructions'}:
            raise ServiceError(400, 'Send only source_message_id, name, role, and instructions to create a bot.')
        source_id = require_text(payload, 'source_message_id', 100)
        name = require_text(payload, 'name', 80)
        role = require_text(payload, 'role', 160)
        instructions = payload['instructions']
        if not isinstance(instructions, str) or len(instructions) > 20000:
            raise ServiceError(400, 'instructions must contain at most 20000 characters.')
        normalized_name = ' '.join(unicodedata.normalize('NFKC', name).casefold().split())
        receipt_id = digest(encoded([source_id, normalized_name]))
        fingerprint = digest(encoded([normalized_name, role, instructions]))
        with self.transaction():
            task = self._get('tasks', task_id)
            if self.stopping.is_set() or task['status'] != 'working':
                raise ServiceError(409, 'This task is no longer working and cannot create a bot.')
            if self._task_paused(task) or self._get('exchanges', task['exchange_id']).get('cancelled'):
                raise ServiceError(409, 'Restore this bot before creating a bot.')
            source = self._human_source(task, source_id, task, 'create bots')
            previous = self.db.execute('SELECT data FROM bot_creation_receipts WHERE id=?', (receipt_id,)).fetchone()
            if previous:
                record = json.loads(previous[0])
                if record['fingerprint'] != fingerprint:
                    raise ServiceError(409, 'This message already created that bot with different settings. Send a new request.')
                bot = self._get('bots', record['receipt']['bot_id'])
                if bot.get('_deleted'):
                    raise ServiceError(409, 'That bot was deleted. Send a new human request to create another bot.')
                self._sync_profile(bot)
                return record['receipt'] | {'status': 'duplicate'}
            if sum(record['source_message_id'] == source_id for record in self._all('bot_creation_receipts')) >= self.BOT_CHANGE_LIMIT:
                raise ServiceError(409, 'This message created eight bots. Send a new human request to create more.')
            audit = {'source_message_id': source_id, 'source_task_id': task_id, 'created_by': source['_human_device_id']}
            bot = self._create_bot({'name': name, 'role': role, 'instructions': instructions}, audit=audit)
            receipt = dict(status='created', bot_id=bot['id'], profile_id=bot['profile_id'], session_id=bot['session_id'], name=bot['name'], role=bot['role'], source_message_id=source_id)
            self._put('bot_creation_receipts', dict(id=receipt_id, fingerprint=fingerprint, source_message_id=source_id, source_task_id=task_id, source_bot_id=task['bot_id'], created_by=source['_human_device_id'], created_at=now(), receipt=receipt))
            return receipt

    def _bot_deletion_input(self, task: dict) -> dict:
        sources = []
        for source_id in task.get('_preference_source_ids', []):
            try:
                source = self._human_source(task, source_id, task, 'delete bots')
            except ServiceError:
                continue
            sources.append({'id': source['id'], 'text': source['text']})
        source_ids = {source['id'] for source in sources}
        return {'_bot_deletion_sources': sources,
                '_bot_deletion_receipts': [record['receipt'] for record in self._all('bot_deletion_receipts')
                                            if record['source_message_id'] in source_ids],
                '_deletable_bots': [{'id': bot['id'], 'name': bot['name'], 'role': bot['role'], 'archived': bot['archived']}
                                    for bot in self._all('bots') if not bot.get('_deleted')
                                    and bot['id'] not in (task['bot_id'], task['conversation_bot_id'])]}

    def _delete_bot_from_chat(self, task_id: str, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) != {'source_message_id', 'target_bot_id', 'expected_name'}:
            raise ServiceError(400, 'Send only source_message_id, target_bot_id, and expected_name to delete a bot.')
        source_id = require_text(payload, 'source_message_id', 100)
        target_id = require_text(payload, 'target_bot_id', 100)
        name = require_text(payload, 'expected_name', 160)
        receipt_id = digest(encoded([source_id, target_id]))
        callbacks = []
        with self.transaction():
            task = self._get('tasks', task_id)
            if self.stopping.is_set() or task['status'] != 'working' or self._task_paused(task):
                raise ServiceError(409, 'This task cannot delete a bot now.')
            if self._get('exchanges', task['exchange_id']).get('cancelled'):
                raise ServiceError(409, 'This exchange was cancelled.')
            source = self._human_source(task, source_id, task, 'delete bots')
            if target_id in (task['bot_id'], task['conversation_bot_id']):
                raise ServiceError(409, 'Use another conversation to delete this bot. A bot cannot delete itself.')
            bot = self._get('bots', target_id)
            if bot['name'] != name:
                raise ServiceError(409, 'The target bot changed its name. Refresh before requesting deletion.')
            row = self.db.execute('SELECT data FROM bot_deletion_receipts WHERE id=?', (receipt_id,)).fetchone()
            if row:
                receipt = json.loads(row[0])['receipt']
                if receipt['name'] != name:
                    raise ServiceError(409, 'Retry the exact deletion request.')
                receipt = receipt | {'status': 'duplicate'}
                callbacks.append(lambda: self._finish_bot_deletion(target_id))
            else:
                if sum(record['source_message_id'] == source_id for record in self._all('bot_deletion_receipts')) >= self.BOT_CHANGE_LIMIT:
                    raise ServiceError(409, 'This message deleted eight bots. Send a new request to delete more.')
                audit = {'source_message_id': source_id, 'source_task_id': task_id, 'deleted_by': source['_human_device_id']}
                if task_id in self._bot_deletion_scope(target_id):
                    raise ServiceError(409, 'Request deletion in a separate task. This task depends on that bot.')
                self._delete_bot(bot, callbacks, audit=audit)
                receipt = dict(status='deleted', bot_id=target_id, profile_id=bot['profile_id'], name=name,
                               source_message_id=source_id, cancelled_task_ids=bot.get('_delete_task_ids', []))
                self._put('bot_deletion_receipts', dict(id=receipt_id, source_message_id=source_id,
                          source_task_id=task_id, source_bot_id=task['bot_id'], deleted_by=source['_human_device_id'],
                          created_at=now(), receipt=receipt))
        for callback in callbacks:
            callback()
        return receipt






    def _refresh_bot(self, bot_id: str):
        """Set the status of a bot from its tasks and contexts, and publish the bot when its public row changed.

        Two indexed queries read the states. The event goes out only when the public row differs from the one that
        this service published last. The phone replays message events by themselves, and a bot event tells it to
        read the bots again, so a new last message or unread count still publishes. A rolled back transaction clears
        the record, and a restart starts without it, so the first refresh of each bot publishes.

        The sync response sends every bot with any change, so a suppressed event is safe while each write to the public
        row of a bot comes with another event: a message, a task, a read mark, or this event. Keep that pairing.
        """
        bot = self._get('bots', bot_id)
        states = {row[0] for row in self.db.execute(BOT_TASK_STATES, (bot_id,))}
        # The latest task of the bot decides whether a failure still shows.
        latest = self.db.execute(BOT_LATEST_TASK, (bot_id,)).fetchone()
        if latest and latest[0] == 'failed':
            states.add('failed')
        answering = self.db.execute(BOT_WORKING_CONTEXT, (bot_id,)).fetchone()
        status = 'working' if answering else next((s for s in ('needs_you', 'working', 'scheduled', 'failed') if s in states), 'idle')
        if bot.get('status') != status:
            bot['status'] = status
            self._put('bots', bot)
        public = self._public(bot)
        fingerprint = encoded(public)
        published = self.__dict__.setdefault('_published_bots', {})
        if published.get(bot_id) == fingerprint:
            return
        self._event('bot.updated', public, bot_id)
        self._note_published(bot_id, fingerprint)

    def _note_published(self, bot_id: str, fingerprint: str):
        """Record the public row that a bot event carried, and which transaction wrote it."""
        self.__dict__.setdefault('_published_bots', {})[bot_id] = fingerprint
        self.__dict__.setdefault('_published_in_transaction', set()).add(bot_id)

    @staticmethod
    def _public(obj: dict) -> dict:
        result = {key: value for key, value in obj.items() if not key.startswith('_')}
        if obj.get('_source_recheck') and obj.get('status') == 'pending':
            # Older clients render unknown statuses as approved draft receipts.
            # Keep their pending presentation; answers remain fenced server-side.
            result['source_refreshing'] = True
        return result

    def pairing_code(self, lifetime: int = 300, *, tunnel: bool = False) -> str:
        with self.transaction():
            return issue_pairing_code(self.db, lifetime, tunnel)[0]

    create_pairing_code = pairing_code

    def pair(self, data: dict, peer: str | None = None) -> dict:
        with self.transaction():
            result = pair_device(self.db, data, lambda device_id: self._event('device.paired', {'device_id': device_id}), peer)
        if isinstance(result, ServiceError):
            raise result
        return result

    def authenticate(self, token: str, peer: str | None = None) -> str:
        with self._read_view() as view:
            return device_for_token(view.db, token, peer)

    def _model_catalog(self, refresh_after=None) -> dict | None:
        if not hasattr(self.provider, 'model_catalog'):
            return None
        if refresh_after is not None and hasattr(self.provider, 'refresh_model_catalog'):
            self.provider.refresh_model_catalog(refresh_after)
        return self.provider.model_catalog()

    def _validation_model_catalog(self) -> dict | None:
        try:
            return self.provider.validation_model_catalog() if hasattr(self.provider, 'validation_model_catalog') else self._model_catalog()
        except ValueError as error:
            raise ServiceError(503, str(error)) from None

    def _bot_view(self, bot: dict, catalog: dict | None) -> dict:
        result = self._public(bot)
        if catalog is not None:
            try:
                route = self.provider.resolve_model(bot, catalog)
                result['effective_model'] = {key: route.get(key) for key in ('model', 'provider', 'reasoning_effort')}
            except ValueError as error:
                result.update(effective_model=None, model_error=str(error))
        return result

    def _model_override(self, value: Any, catalog: dict | None) -> dict | None:
        if value is None:
            return None
        if catalog is None:
            raise ServiceError(400, 'Model overrides require a configured Hermes catalog.')
        if (not isinstance(value, dict) or set(value) - {'model', 'provider', 'reasoning_effort'}
                or not all(isinstance(value.get(key), str) and value[key].strip() for key in ('model', 'provider'))
                or (value.get('reasoning_effort') is not None and not isinstance(value['reasoning_effort'], str))):
            raise ServiceError(400, 'Choose a model, provider, and optional effort from the Hermes catalog.')
        selection = {key: value.get(key) for key in ('model', 'provider', 'reasoning_effort')}
        try:
            self.provider.resolve_model({'model_override': selection}, catalog)
        except ValueError as error:
            raise ServiceError(400, str(error)) from None
        return selection

    @contextlib.contextmanager
    def _read_view(self):
        # WAL readers see committed data without waiting for worker bookkeeping.
        with contextlib.closing(sqlite3.connect((self.root / 'coconut.sqlite3').as_uri() + '?mode=ro',
                                               uri=True, isolation_level=None)) as db:
            view = copy.copy(self)
            view.db = db
            view.lock = threading.RLock()
            view._read_cache = None
            # A read view has its own connection. It must not use the writer's shared objects.
            view._shared_state = None
            view._task_rows = None
            yield view

    @contextlib.contextmanager
    def _read_transaction(self):
        self.db.execute('BEGIN')
        previous_graphs = getattr(self, '_work_graphs', None)
        self._work_graphs = {}
        if hasattr(self, '_read_cache'):
            self._read_cache = {}
        try:
            yield
        finally:
            if hasattr(self, '_read_cache'):
                self._read_cache = None
            self._work_graphs = previous_graphs
            self.db.execute('ROLLBACK')

    def snapshot(self, *, timings=None, content_only=False, compact_history=False, recent_work=False) -> dict:
        with self._read_view() as view:
            return view._snapshot(timings=timings, content_only=content_only, compact_history=compact_history, recent_work=recent_work)

    def _snapshot(self, *, timings=None, content_only=False, compact_history=False, recent_work=False) -> dict:
        started = time.perf_counter()
        def measured(name):
            nonlocal started
            stamp = time.perf_counter()
            if timings is not None:
                timings[name] = (stamp - started) * 1000
            started = stamp
        for _ in range(3):
            settings_cursor = self._cursor()
            stored_bots = [bot for bot in self._all('bots') if not bot.get('_deleted')]
            catalog = None if content_only else self._model_catalog()
            measured('models')
            if content_only:
                observed, native, routines = {}, None, None
            elif hasattr(self.provider, 'observe_state'):
                observed, native = self.provider.observe_state(stored_bots)
                routines = native['routines']
                measured('native')
            else:
                if hasattr(self.provider, 'observe_bots'):
                    observed = self.provider.observe_bots(stored_bots)
                else:
                    observed = {bot['id']: dict(bot, instructions=self.profiles.read_instructions(bot)) for bot in stored_bots}
                measured('bots')
                routines = (self.provider.native_settings('routines_read', stored_bots)['routines']
                            if hasattr(self.provider, 'native_settings') else None)
                measured('routines')
                native = self.provider.native_settings() if hasattr(self.provider, 'native_settings') else None
                measured('settings')
            with self._read_transaction():
                if self.db.execute("SELECT 1 FROM events WHERE seq>? AND "
                                   "(type IN ('settings.updated','bot.created','bot.deleted') OR type LIKE 'routine.%') LIMIT 1", (settings_cursor,)).fetchone():
                    continue
                data_started = time.perf_counter()
                bots = [self._bot_view(dict(b, **{key: value for key, value in observed.get(b['id'], {}).items() if key in ('name', 'role', 'instructions', 'approval_override', 'effective_approval', 'worker_limit', 'model_override', '_native_effective_model', '_model_error')}), catalog) for b in self._all('bots') if not b.get('_deleted')]
                visible = {b['id'] for b in bots}
                state = {'bots': bots, 'work_capacity': capacity_state(self._setting('work_capacity'))}
                for table in self.TABLES[1:]:
                    if content_only and table == 'tasks':
                        continue
                    if content_only and table == 'messages':
                        state[table], history = self._recent_messages(visible)
                        state['history_before'] = history
                        continue
                    source = self._page_decisions(state['messages'], pending=True) if content_only and compact_history and table == 'decisions' else self._all(table)
                    # Look up only the tasks of these rows. A scan of the whole task table cost 70 ms.
                    task_ids = (self._visible_task_ids(visible, {o.get('task_id') for o in source} - {None})
                                if table in ('decisions', 'artifacts') else None)
                    state[table] = [self._public(o) for o in source if self._visible(o, visible, task_ids)]
                work_started = time.perf_counter()
                state['artifacts'] = [self._public(a) for a in self._work_projection([self._get('artifacts', a['id']) for a in state['artifacts']])]
                if content_only and recent_work:
                    state['artifacts'], state['older_work_bot_ids'] = self._current_work(state['artifacts'], state['messages'])
                work_finished = time.perf_counter()
                if content_only:
                    referenced = {o.get('task_id') for name in ('messages', 'decisions', 'artifacts') for o in state[name]}
                    # The active tasks come from the tasks_status index and the referenced ones by ID, so the read
                    # does not scan every task.
                    state['tasks'] = [self._public(task) for task in (json.loads(row[0]) for row in self.db.execute(
                        "SELECT data FROM tasks WHERE id IN (SELECT id FROM tasks WHERE json_extract(data,'$.status') IN "
                        "(SELECT value FROM json_each(:active)) UNION SELECT value FROM json_each(:referenced)) "
                        "AND +json_extract(data,'$.bot_id') IN (SELECT value FROM json_each(:visible)) "
                        "AND json_extract(data,'$.conversation_bot_id') IN (SELECT value FROM json_each(:visible)) "
                        "AND NOT coalesce(json_extract(data,'$._hidden'),0) AND NOT coalesce(json_extract(data,'$._deleted'),0) ORDER BY rowid",
                        dict(active=json.dumps(sorted(self.ACTIVE)), referenced=json.dumps(sorted(referenced - {None})),
                             visible=json.dumps(sorted(visible)))))]
                if routines is not None:
                    previous = {r['id']: r.get('last_task_id') for r in state['routines']}
                    state['routines'] = [dict(self._public(r), last_task_id=previous.get(r['id']))
                                         for r in routines if r['bot_id'] in visible and not r.get('_deleted')]
                rows_finished = time.perf_counter()
                replying = {c['decision_id'] for c in self._all('contexts') if c['status'] in ('queued', 'working') and not c.get('_decision_refresh')}
                for decision in state['decisions']:
                    decision['reply_pending'] = decision['id'] in replying
                    if not decision.get('request_text') and decision.get('task_id'):
                        task = self._get('tasks', decision['task_id'])
                        decision.update(self.decision_origin(task))
                state.update(cursor=self._cursor(), server_time=now(), provider=getattr(self.provider, 'name', 'unknown'), push_configured=self.push.configured)
                if catalog is not None:
                    default = catalog.get('default', {})
                    state.update(model_catalog=catalog, model=default.get('model') or '', base_url=default.get('base_url') or '', model_configured=bool(catalog.get('default_configured', False)))
                elif content_only:
                    state.update(model='', base_url='', model_configured=False, content_only=True, sync_version=1)
                    receipts = self.db.execute("SELECT json_group_array(DISTINCT json_extract(data,'$.client_request_id')) FROM messages "
                        "WHERE json_type(data,'$.client_request_id')='text' AND EXISTS(SELECT 1 FROM bots "
                        "WHERE id=json_extract(messages.data,'$.bot_id') AND NOT COALESCE(json_extract(bots.data,'$._deleted'),0))").fetchone()
                    state['confirmed_request_ids'] = json.loads(receipts[0])
                else:
                    state.update(model=self._setting('model', getattr(self.provider, 'model', '') or ''), base_url=self._setting('base_url', getattr(self.provider, 'base_url', '') or ''), model_configured=bool(getattr(self.provider, 'ready', False)))
                if native is not None:
                    state['approvals'] = native['approvals']
                    state['parallel_work'] = native.get('parallel_work')
                    state['api_fallback'] = native.get('api_fallback')
                    state['mcp_servers'] = native.get('mcp_servers', [])
                if timings is not None:
                    timings.update(data_rows=(rows_finished - data_started - (work_finished - work_started)) * 1000,
                        data_work=(work_finished - work_started) * 1000,
                        data_decisions=(time.perf_counter() - rows_finished) * 1000)
            break
        else:
            raise ServiceError(503, 'Settings changed during this request. Try again.')
        measured('data')
        return state

    def _cursor(self) -> int:
        with self.lock:
            return self.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]

    def events(self, after: int, wait: float = 0) -> dict:
        if after < 0:
            raise ServiceError(400, 'The event cursor must be zero or greater.')
        deadline = time.monotonic() + min(max(wait, 0), 25)
        with self._read_view() as view:
            while view._cursor() <= after and not self.stopping.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.stopping.wait(min(remaining, .1))
            with view._read_transaction():
                rows = view.db.execute('SELECT seq,type,bot_id,task_id,data,created_at FROM events WHERE seq>? ORDER BY seq LIMIT 500', (after,)).fetchall()
                result = [dict(seq=r[0], type=r[1], bot_id=r[2], task_id=r[3], data=json.loads(r[4]), created_at=r[5]) for r in rows]
                return dict(events=result, cursor=result[-1]['seq'] if result else max(after, view._cursor()), server_time=now())

    def _setting(self, key: str, default: Any = None) -> Any:
        with self.lock:
            cache = getattr(self, '_read_cache', None)
            if cache is not None:
                if 'settings' not in cache:
                    row = self.db.execute('SELECT json_group_object(key,value) FROM settings').fetchone()
                    cache['settings'] = json.loads(row[0])
                value = cache['settings'].get(key)
                return json.loads(value) if value is not None else default
            row = self._read_one('SELECT value FROM settings WHERE key=?', (key,))
        return json.loads(row[0]) if row else default

    def mutate(self, method: str, path: str, data: dict, key: str, device_id: str = 'local') -> dict:
        try:
            UUID(key)
        except (ValueError, TypeError, AttributeError):
            raise ServiceError(400, 'Supply a UUID in Idempotency-Key.')
        fingerprint = digest(encoded([device_id, method, path, data]))
        if method == 'POST' and path == '/v1/work/pull-requests':
            with self.pr_follow_lock:
                with self.lock:
                    previous = self.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (key,)).fetchone()
                    if previous:
                        if not hmac.compare_digest(previous[0], fingerprint):
                            raise ServiceError(409, 'This idempotency key belongs to a different request.')
                        return json.loads(previous[1])
                result = self._register_pr_artifact(None, data, bot_id=data.get('bot_id'))
                with self.transaction():
                    self.db.execute('INSERT INTO requests(key,fingerprint,response) VALUES (?,?,?)', (key, fingerprint, encoded(result)))
                return result
        if method == 'POST' and path == '/v1/work/refresh':
            with self.lock:
                if time.monotonic() - self.pr_last_request >= 10:
                    self.pr_last_request = time.monotonic()
                    self.pr_refresh_requested.set()
            return {'ok': True}
        if method == 'POST' and path.startswith('/v1/bots/') and path.endswith('/work/archive-idle') and path.count('/') == 5:
            return self.archive_idle_work(path.split('/')[3], data)
        if method == 'POST' and path.startswith('/v1/settings/google/'):
            return self.google_connection(path.rsplit('/', 1)[1], data, device_id, key)
        if method == 'PUT' and path == '/v1/settings/api-fallback' and hasattr(self.provider, 'native_settings'):
            return self.mutate_native_fallback(data, key, fingerprint)
        if method == 'PUT' and path == '/v1/settings/parallel-work' and hasattr(self.provider, 'native_settings'):
            return self.mutate_parallel_work(data, key, fingerprint)
        if method == 'PUT' and path == '/v1/settings/approvals' and hasattr(self.provider, 'native_settings'):
            return self.mutate_approval_default(data, key, fingerprint)
        if method == 'POST' and path == '/v1/settings/mcp':
            return self.mutate_mcp(data, key, fingerprint)
        if method == 'PUT' and path == '/v1/settings/model' and hasattr(self.provider, 'set_model_default'):
            return self._mutate_model_default(data, key, fingerprint)
        parts = path.strip('/').split('/')
        if method == 'PATCH' and len(parts) == 3 and parts[:2] == ['v1', 'bots'] and isinstance(data, dict):
            return self._mutate_native_bot(parts[2], data, key, fingerprint, device_id)
        catalog = self._model_catalog() if path == '/v1/settings/model' or (isinstance(data, dict) and ('model_override' in data or path == '/v1/bots')) else None
        callbacks = []
        with self.transaction():
            previous = self.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (key,)).fetchone()
            if previous:
                if not hmac.compare_digest(previous[0], fingerprint):
                    raise ServiceError(409, 'This idempotency key belongs to a different request.')
                response = json.loads(previous[1])
                if method == 'POST' and path == '/v1/bots':
                    bot = self._get('bots', response['id'])
                    if bot.get('_deleted'):
                        raise ServiceError(409, 'That bot was deleted. Send a new request to create another bot.')
                    self._sync_profile(bot)
                if method == 'DELETE' and len(parts) == 3 and parts[:2] == ['v1', 'bots']:
                    callbacks.append(lambda: self._finish_bot_deletion(parts[2]))
                if method == 'POST' and len(parts) == 5 and parts[:2] == ['v1', 'bots'] and parts[3:] == ['profile', 'delete']:
                    callbacks.append(lambda: self._finish_bot_deletion(parts[2]))
                    row = self.profiles.rows.get(parts[2])
                    if row is not None:
                        callbacks.append(lambda: self._finish_profile_deletion(parts[2], row['name']))
            else:
                self._request_device_id = device_id
                self._request_key = key
                self._request_model_catalog = catalog
                try:
                    if hasattr(self.provider, 'native_settings') and ((len(parts) == 4 and parts[1] == 'bots' and parts[3] == 'routines') or (len(parts) >= 3 and parts[1] == 'routines')):
                        response = self.mutate_native_routine(method, parts, data, key)
                    else:
                        response = self._write(method, parts, data, device_id, callbacks)
                finally:
                    self._request_device_id = None
                    self._request_key = None
                    self._request_model_catalog = None
                self.db.execute('INSERT INTO requests VALUES (?,?,?)', (key, fingerprint, encoded(response)))
        for callback in callbacks:
            callback()
        if isinstance(response, dict) and response.get('steering_status') == 'next_safe_turn':
            self._deliver_live_messages(response['id'])
        return response

    @staticmethod
    def _model_result(catalog: dict) -> dict:
        default = catalog['default']
        return dict(model_catalog=catalog, model=default.get('model') or '', base_url=default.get('base_url') or '', model_configured=bool(catalog.get('default_configured', False)))

    def _finish_model_update(self, key: str, catalog: dict | None, *, error: str | None = None, intent: dict | None = None) -> dict:
        response = {'_model_error': error} if error else ({} if intent and intent.get('bot_id') else self._model_result(catalog))
        observed = None
        if not error and intent and intent.get('bot_id'):
            with self.lock:
                bot = self._model_intent_bot(self._get('bots', intent['bot_id']), intent)
            observed = self.provider.observe_bot(bot) if hasattr(self.provider, 'observe_bot') else dict(bot, instructions=self.profiles.read_instructions(bot))
        with self.transaction():
            if not error and intent and intent.get('bot_id'):
                self._request_model_catalog = catalog
                self._request_device_id = intent['device_id']
                try:
                    response = self._write('PATCH', ['v1', 'bots', intent['bot_id']], intent['data'], intent['device_id'], [])
                    response = self._bot_view(dict(response, **{field: value for field, value in observed.items() if field in ('name', 'role', 'instructions', 'approval_override', 'effective_approval', 'worker_limit', 'model_override', '_native_effective_model', '_model_error')}), catalog)
                finally:
                    self._request_model_catalog = None
                    self._request_device_id = None
            self.db.execute('UPDATE requests SET response=? WHERE key=?', (encoded(response), key))
            if error:
                self._event('settings.update_unconfirmed', {'request_id': key})
            elif not intent or not intent.get('bot_id'):
                default = catalog['default']
                self._event('settings.updated', {field: default.get(field) for field in ('model', 'provider', 'reasoning_effort')})
        if not error:
            # A new default, a bot's choice or its restore: the reconcile brings the profiles in line after the commit.
            self.wake_model_follow()
        return response

    def _reconcile_model_updates(self, catalog: dict | None) -> None:
        with self.lock:
            pending = []
            for key, value in self.db.execute("SELECT key,response FROM requests WHERE response LIKE '%\"_model_pending\"%'"):
                response = json.loads(value)
                if isinstance(response, dict) and set(response) == {'_model_pending'}:
                    pending.append((key, response['_model_pending']))
        for key, intent in pending:
            if intent.get('bot_id'):
                try:
                    with self.lock:
                        bot = self._model_intent_bot(self._get('bots', intent['bot_id']), intent)
                    observed = self.provider.observe_bot(bot) if hasattr(self.provider, 'observe_bot') else dict(bot, instructions=self.profiles.read_instructions(bot))
                    actual = {field: observed.get(field) for field in intent['expected']}
                    matches = not bot.get('_deleted') and actual == intent['expected']
                except (ValueError, ServiceError):
                    matches = False
            else:
                actual = {field: catalog['default'].get(field) for field in ('model', 'provider', 'reasoning_effort')} if catalog else None
                matches = actual == intent['expected']
            message = None if matches else 'The Mac could not confirm that settings change. Check the current Hermes settings before trying again.'
            self._finish_model_update(key, catalog, error=message, intent=intent)

    @staticmethod
    def _model_intent_bot(bot: dict, intent: dict) -> dict:
        """The bot as the pending change leaves it. A model_override of null makes it follow the default model."""
        if 'model_override' in intent.get('expected', {}):
            return dict(bot, model_follows_default=intent['expected']['model_override'] is None)
        return bot

    def _previous_request(self, key: str, fingerprint: str) -> dict | None:
        with self.lock:
            previous = self.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (key,)).fetchone()
        if not previous:
            return None
        if not hmac.compare_digest(previous[0], fingerprint):
            raise ServiceError(409, 'This idempotency key belongs to a different request.')
        return json.loads(previous[1])

    def _remember_request(self, key: str, fingerprint: str, response: dict) -> None:
        with self.transaction():
            self.db.execute('INSERT INTO requests(key,fingerprint,response) VALUES (?,?,?)', (key, fingerprint, encoded(response)))

    def _previous_model_response(self, key: str, fingerprint: str) -> dict | None:
        response = self._previous_request(key, fingerprint)
        if response is None:
            return None
        if response.get('_model_error'):
            raise ServiceError(409, response['_model_error'])
        return response

    def _mutate_native_bot(self, bot_id: str, data: dict, key: str, fingerprint: str, device: str) -> dict:
        with self.model_settings_lock:
            catalog = self._validation_model_catalog() if data.get('model_override') is not None else self._model_catalog()
            self._reconcile_model_updates(catalog)
            previous = self._previous_model_response(key, fingerprint)
            if previous is not None:
                return previous
            with self.lock:
                bot = self._get('bots', bot_id)
            if bot.get('_deleted'):
                raise ServiceError(404, 'This bot was deleted.')
            if 'profile_id' in data and data['profile_id'] != bot['profile_id']:
                raise ServiceError(409, 'A bot keeps its original Hermes profile ID.')
            for field in ('instructions', 'memory'):
                if field in data and (not isinstance(data[field], str) or len(data[field]) > 20000):
                    raise ServiceError(400, f'{field} must contain at most 20000 characters.')
            for field in ('name', 'avatar', 'color'):
                if field in data:
                    require_text(data, field, 160)
            if 'role' in data and (not isinstance(data['role'], str) or len(data['role']) > 160):
                raise ServiceError(400, 'The label must contain at most 160 characters.')
            if 'worker_limit' in data:
                from .parallel_work import validate_limit
                try:
                    validate_limit(data['worker_limit'])
                except ValueError as error:
                    raise ServiceError(400, str(error)) from None
            if 'approval_override' in data and data['approval_override'] not in (None, 'off', 'smart', 'manual'):
                raise ServiceError(400, 'Choose an approval mode from Hermes settings.')
            for field in ('archived', 'pinned', 'notifications'):
                if field in data and not isinstance(data[field], bool):
                    raise ServiceError(400, f'{field} must be true or false.')
            expected = {}
            if 'model_override' in data:
                expected['model_override'] = self._model_override(data['model_override'], catalog)
            if 'instructions' in data:
                expected['instructions'] = data['instructions']
            expected.update({field: data[field] for field in ('name', 'role', 'approval_override', 'worker_limit') if field in data})
            intent = dict(bot_id=bot_id, expected=expected, data=data, device_id=device)
            with self.transaction():
                self.db.execute('INSERT INTO requests VALUES (?,?,?)', (key, fingerprint, encoded({'_model_pending': intent})))
                self._event('bot.settings_pending', {'request_id': key, 'fields': sorted(data)}, bot_id)
            try:
                # Only the bot's own model is a write here. null only records the choice: the reconcile writes the default.
                if expected.get('model_override') is not None:
                    if hasattr(self.provider, 'set_bot_model'):
                        self.provider.set_bot_model(bot, expected['model_override'])
                if hasattr(self.provider, 'set_bot_settings'):
                    self.provider.set_bot_settings(bot, {field: expected[field] for field in ('name', 'role', 'approval_override', 'worker_limit') if field in expected})
                if 'instructions' in expected:
                    self.profiles.write_instructions(bot, expected['instructions'])
            except ProfileError as error:
                # For example a missing profile folder. Coconut never creates one here.
                raise ServiceError(409, str(error)) from None
            except Exception:
                raise ServiceError(503, 'The Mac could not confirm the profile change. Retry with the same request key.') from None
            return self._finish_model_update(key, catalog, intent=intent)

    def mutate_mcp(self, data: dict, key: str, fingerprint: str) -> dict:
        if not hasattr(self.provider, 'native_settings'):
            raise ServiceError(503, 'Hermes is not available.')
        payload = {name: data[name] for name in ('action', 'name', 'url', 'token') if isinstance(data, dict) and name in data}
        # Other writes to the default Hermes config take this lock, so one save cannot erase another.
        # The lock also covers the replay check, so a retry cannot repeat a write that still runs.
        with self.model_settings_lock:
            previous = self._previous_request(key, fingerprint)
            if previous is not None:
                return previous
            try:
                result = self.provider.native_settings('mcp', **payload)
            except ValueError:
                raise ServiceError(400, 'Check the MCP name, HTTPS address, and authentication.') from None
            with self.transaction():
                self.db.execute('INSERT INTO requests(key,fingerprint,response) VALUES (?,?,?)', (key, fingerprint, encoded(result)))
                self._event('settings.updated', {})
        return result

    def _mutate_model_default(self, data: dict, key: str, fingerprint: str) -> dict:
        with self.model_settings_lock:
            catalog = self._validation_model_catalog()
            self._reconcile_model_updates(catalog)
            previous = self._previous_model_response(key, fingerprint)
            if previous is not None:
                return previous
            selection = self._model_override(data, catalog)
            expected = dict(selection)
            with self.transaction():
                self.db.execute('INSERT INTO requests VALUES (?,?,?)', (key, fingerprint, encoded({'_model_pending': {'expected': expected}})))
                self._event('settings.pending', {'request_id': key, 'fields': sorted(selection)})
            try:
                catalog = self.provider.set_model_default(selection)
            except Exception:
                # Keep the durable intent. A retry observes the native result first.
                raise ServiceError(503, 'The Mac could not confirm the model change. Retry with the same request key.') from None
            return self._finish_model_update(key, catalog)

    def _create_bot(self, data: dict, *, audit: dict | None = None, profile: dict | None = None) -> dict:
        """Create a bot. `profile` is {name, home} of a profile that a scan found. Without it, Hermes creates one."""
        if data.get('model_override') is not None:
            raise ServiceError(400, 'Create the bot, then choose its model in bot settings.')
        if not isinstance(data.get('pinned', False), bool):
            raise ServiceError(400, 'pinned must be true or false.')
        for field in ('instructions', 'memory'):
            if not isinstance(data.get(field, ''), str) or len(data.get(field, '')) > 20000:
                raise ServiceError(400, f'{field} must contain at most 20000 characters.')
        avatar = secrets.choice(('round', 'oval', 'wide', 'square', 'pointed', 'bean'))
        color = secrets.choice(('A85F32', 'C76552', 'B5913F', '64866A', '448A89', '6087B0', '9272B3', 'BF759B', 'D6C6A2', '858680'))
        bot = dict(id=uid(), session_id=uid(), name=require_text(data, 'name', 80), role=str(data.get('role', ''))[:160], avatar=str(data.get('avatar', f'coconut.{avatar}'))[:80], color=str(data.get('color', color))[:20], instructions=data.get('instructions', ''), notifications=True, status='idle', last_message='', unread_count=0, archived=False, pinned=data.get('pinned', False), created_at=now())
        bot['profile_id'] = profile_id(bot)
        bot['model_override'] = None
        if profile is None:
            try:
                profile = self._new_profile(bot)
            except (ProfileError, OSError) as error:
                raise ServiceError(503, f'Hermes did not create the profile of this bot: {error}') from None
            # A bot that Coconut creates follows the default model. A found profile gets its choice in the reconcile.
            bot['model_follows_default'] = True
            self.wake_model_follow()
        self._attach_profile(bot, profile)
        self._put('bots', bot)
        self._event('bot.created', bot | (audit or {}), bot['id'], (audit or {}).get('source_task_id'))
        return bot

    def _bot_deletion_scope(self, bot_id: str) -> set[str]:
        tasks = self._all('tasks')
        affected = {task['id'] for task in tasks if bot_id in (task['bot_id'], task['conversation_bot_id'])}
        while True:
            expanded = affected | {task['id'] for task in tasks if task.get('source_task_id') in affected}
            if expanded == affected:
                return affected
            affected = expanded

    def _delete_bot(self, bot: dict, callbacks: list, *, audit: dict | None = None):
        if not bot.get('_deleted'):
            affected = self._bot_deletion_scope(bot['id'])
            cancelled = []
            for task_id in sorted(affected):
                task = self._get('tasks', task_id)
                if task.pop('_return_pending', None) is not None:
                    self._put('tasks', task)
                if task['status'] in self.ACTIVE:
                    cancelled.append(task_id)
                    self._cancel_one(task, [])
            for routine in self._all('routines'):
                if routine['bot_id'] == bot['id'] and routine['enabled']:
                    routine.update(enabled=False, next_run_at=None, updated_at=now())
                    self._put('routines', routine)
                    self._event('routine.updated', self._public(routine), bot['id'])
            bot.update(_deleted=True, archived=True, _delete_task_ids=cancelled)
            self._put('bots', bot)
            # Remove from Coconut keeps the Hermes profile folder.
            self._remove_profile_row(bot['id'])
            self._event('bot.deleted', {'id': bot['id']} | (audit or {}), bot['id'], (audit or {}).get('source_task_id'))
            # A deleted bot publishes no more rows, so its record has no use. Forget it when the transaction commits.
            self.__dict__.setdefault('_forget_bots_on_commit', set()).add(bot['id'])
            for task_id in affected:
                task = self._get('tasks', task_id)
                owner_id = task.get('_reply_to_task_id')
                if not owner_id or owner_id in affected:
                    continue
                owner = self._get('tasks', owner_id)
                if task_id not in owner.get('_waiting_on', []):
                    continue
                text = f'{bot["name"]} was deleted. Its dependency work cannot continue.'
                task['_dependency_cancelled'] = True
                self._prepare_return(task, text, 'failed')
                task['_return_pending'] = dict(target_bot_id=owner['bot_id'], text=text, result_status='failed')
                self._put('tasks', task)
                recipients = [self._get('bots', identity) for identity in {owner['bot_id'], task['conversation_bot_id']}]
                if not any(recipient['archived'] or recipient.get('_deleted') for recipient in recipients):
                    self._store_bot_message(task_id, task['_return_pending'], returning=True)
                    task.pop('_return_pending', None)
                    self._put('tasks', task)
        callbacks.append(lambda: self._finish_bot_deletion(bot['id']))

    def _finish_bot_deletion(self, bot_id: str):
        with self.lock:
            bot = self._get('bots', bot_id)
            if not bot.get('_deleted'):
                raise ServiceError(409, 'This bot has no recorded deletion.')
            bot_lock = self.bot_locks.setdefault(bot_id, threading.Lock())
        # Cancellation waits outside the database lock so worker receipts can finish.
        errors = []
        for task_id in bot.get('_delete_task_ids', []):
            try:
                self.provider.cancel_task(task_id)
            except Exception as error:
                errors.append(error)
        try:
            if hasattr(self.provider, 'stop_bot'):
                self.provider.stop_bot(bot_id)
            # A worker can still be preparing its profile before its process starts.
            if not bot_lock.acquire(timeout=5):
                raise TimeoutError('The deleted bot is still stopping.')
            try:
                self.profiles.sync(bot)
                if hasattr(self.provider, 'native_settings'):
                    for routine in self._all('routines'):
                        if routine['bot_id'] == bot_id and routine.get('_native_id'):
                            self.provider.native_settings('routine_update', [bot], bot_id=bot_id, native_id=routine['_native_id'], changes={'enabled': False})
            finally:
                bot_lock.release()
        except Exception as error:
            errors.append(error)
        if errors:
            raise ServiceError(503, 'Deletion was recorded, but cleanup is unconfirmed. Retry the exact request.') from errors[0]

    def google_connection(self, action, data, device, key=None):
        from .google_connection import GoogleConnections, GoogleError
        manager = GoogleConnections(self.root)
        try:
            if action == 'status':
                return manager.profile().status()
            if action == 'start':
                return manager.start(device, key)
            if action == 'finish':
                result = manager.finish(device, data.get('state'), data.get('code'))
            elif action == 'disconnect':
                result = manager.disconnect(device, key)
            else:
                raise ServiceError(404, 'This Google connection action was not found.')
            with self.transaction():
                self._event('google.updated', result)
            return result
        except (GoogleError, ValueError) as error:
            raise ServiceError(409, str(error)) from None

    def _write(self, method: str, parts: list[str], data: dict, device: str, callbacks: list) -> dict:
        if not isinstance(data, dict) or not parts or parts[0] != 'v1':
            raise ServiceError(400, 'Send a JSON object to a v1 route.')
        if method == 'POST' and len(parts) == 5 and parts[:2] == ['v1', 'work'] and parts[2] in ('pull-requests', 'items') and parts[4] == 'dismiss':
            return self._dismiss_work(parts[3], data)
        if method == 'POST' and len(parts) == 5 and parts[:3] == ['v1', 'work', 'items'] and parts[4] == 'cancel':
            return self._cancel_work(parts[3], callbacks)
        if method == 'POST' and len(parts) == 5 and parts[:3] == ['v1', 'work', 'items'] and parts[4] == 'resume':
            return self._resume_work(parts[3])
        if parts == ['v1', 'settings', 'work-capacity'] and method == 'PUT':
            if set(data) != {'limit'}:
                raise ServiceError(400, 'Specify a work limit, or null for Automatic.')
            try:
                result = capacity_state(data['limit'])
            except ValueError as error:
                raise ServiceError(400, str(error)) from None
            self.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            ('work_capacity', encoded(data['limit'])))
            self._event('settings.updated', {'work_capacity': result})
            return result
        if parts == ['v1', 'device', 'push']:
            if method == 'PUT':
                return self.push.registration(device, data)
            if method == 'DELETE':
                return self.push.unregister(device)
        if parts == ['v1', 'bots', 'restart'] and method == 'POST':
            epoch = uid()
            self.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            ('conversation_epoch', encoded(epoch)))
            result = dict(ok=True, requested_at=now(), starts_after_current_work=True)
            self._event('bots.restarted', result | {'requested_by': device})
            return result
        if parts == ['v1', 'bots'] and method == 'POST':
            return self._bot_view(self._create_bot(data), self._request_model_catalog)
        if len(parts) >= 3 and parts[1] == 'bots':
            bot = self._get('bots', parts[2])
            if bot.get('_deleted'):
                raise ServiceError(404, 'This bot was deleted.')
            if len(parts) == 3 and method == 'PATCH':
                if 'model_override' in data:
                    bot['model_override'] = self._model_override(data['model_override'], self._request_model_catalog)
                    # null makes the bot follow the default model again. A model makes it the bot's own.
                    bot['model_follows_default'] = bot['model_override'] is None
                    if not bot['model_follows_default']:
                        bot.pop('model_follow_error', None)
                if 'profile_id' in data and data['profile_id'] != bot['profile_id']:
                    raise ServiceError(409, 'A bot keeps its original Hermes profile ID.')
                for field in ('name', 'role', 'avatar', 'color', 'instructions'):
                    if field in data:
                        if field in ('instructions', 'role'):
                            if not isinstance(data[field], str) or len(data[field]) > 20000:
                                raise ServiceError(400, f'{field} must contain at most 20000 characters.')
                            bot[field] = data[field]
                        else:
                            bot[field] = require_text(data, field, 160)
                for field in ('archived', 'pinned', 'notifications'):
                    if field in data:
                        if not isinstance(data[field], bool):
                            raise ServiceError(400, f'{field} must be true or false.')
                        bot[field] = data[field]
                self._sync_profile(bot)
                self._put('bots', bot)
                # Publish the public row, as _refresh_bot does, so that the record and the event agree by construction.
                self._event('bot.updated', self._public(bot), bot['id'])
                self._note_published(bot['id'], encoded(self._public(bot)))
                self._event('settings.updated', {'bot_id': bot['id']}, bot['id'])
                return self._bot_view(bot, self._request_model_catalog)
            if len(parts) == 3 and method == 'DELETE':
                self._delete_bot(bot, callbacks, audit={'deleted_by': device})
                return {'ok': True}
            if parts[3:] == ['profile', 'delete'] and method == 'POST':
                return self._delete_profile_request(bot, data, callbacks, device)
            if parts[3:] == ['read'] and method == 'POST':
                bot['unread_count'] = 0
                self._put('bots', bot)
                self._event('bot.read', {'id': bot['id']}, bot['id'])
                return {'ok': True}
            if parts[3:] == ['routines'] and method == 'POST':
                return self._create_routine(bot, data)
            if parts[3:] == ['messages'] and method == 'POST':
                return self._send_message_interaction(bot, data)
        if len(parts) == 4 and parts[1] == 'messages' and parts[3] == 'reactions' and method == 'POST':
            return self._react_to_message(parts[2], data)
        if len(parts) >= 3 and parts[1] == 'routines':
            return self._write_routine(method, parts, data)
        if len(parts) == 4 and parts[1] == 'tasks' and parts[3] == 'replies' and method == 'POST':
            task = self._get('tasks', parts[2])
            return self._send_message_interaction(self._get('bots', task['conversation_bot_id']), data, task=task)
        if len(parts) == 4 and parts[1] == 'tasks' and parts[3] == 'cancel' and method == 'POST':
            task = self._get('tasks', parts[2])
            self._cancel(task, callbacks)
            return self._public(task)
        if len(parts) == 4 and parts[1] == 'decisions' and method == 'POST':
            decision = self._get('decisions', parts[2])
            task = self._get('tasks', decision['task_id'])
            if parts[3] == 'inbox':
                return self.update_inbox(decision, task, data)
            if decision['status'] != 'pending' or task['status'] == 'cancelled':
                raise ServiceError(409, 'This decision is no longer pending.')
            if parts[3] == 'draft':
                return self.edit_message_draft(decision, task, data, device)
            if parts[3] == 'reply':
                if data.get('revision') != decision['revision']:
                    raise ServiceError(409, 'This decision changed. Review it before replying.')
                return self._request_context(decision, task, require_text(data, 'text', 4000), [], reply_device=device)
            if parts[3] == 'context':
                question = require_text(data, 'question', 4000)
                return self._request_context(decision, task, question, [])
            if parts[3] == 'answer':
                if decision.get('_native'):
                    return self.answer_native(decision, task, data, device, callbacks)
                return self._answer(decision, task, data, device)
        if parts == ['v1', 'settings', 'approvals'] and method == 'PUT':
            if not hasattr(self.provider, 'native_settings'):
                raise ServiceError(503, 'Hermes is not available.')
            if data.get('mode') not in ('off', 'smart', 'manual'):
                raise ServiceError(400, 'Choose an approval mode from Hermes settings.')
            result = self.provider.native_settings('set_default_approval', mode=data['mode'])
            self._event('settings.updated', {'approvals': result})
            return result
        if parts == ['v1', 'settings', 'model'] and method == 'PUT':
            model = require_text(data, 'model', 200)
            base_url = data.get('base_url', self._setting('base_url', getattr(self.provider, 'base_url', '') or ''))
            if not isinstance(base_url, str) or (base_url and not base_url.startswith(('http://', 'https://'))):
                raise ServiceError(400, 'Use an HTTP or HTTPS endpoint, or an empty string to clear it.')
            for k, v in (('model', model), ('base_url', base_url)):
                self.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, encoded(v)))
            self._event('settings.updated', {'model': model})
            if hasattr(self.provider, 'configure'):
                callbacks.append(lambda: self.provider.configure(model=model, base_url=base_url or None))
            return {'model': model, 'base_url': base_url, 'model_configured': bool(getattr(self.provider, 'ready', False))}
        if parts == ['v1', 'attachments'] and method == 'POST':
            return self._attachment(data)
        raise ServiceError(404, 'This route was not found.')

    @staticmethod
    def _utc_string(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

    def _routine_fields(self, data: dict, previous: dict | None = None) -> dict:
        result = dict(previous or {})
        for field in ('name', 'prompt', 'cron', 'timezone'):
            if field in data:
                result[field] = require_text(data, field, 20000 if field == 'prompt' else 160)
        result.setdefault('enabled', True)
        if 'enabled' in data:
            if not isinstance(data['enabled'], bool):
                raise ServiceError(400, 'enabled must be true or false.')
            result['enabled'] = data['enabled']
        for field in ('name', 'prompt', 'cron', 'timezone'):
            if not result.get(field):
                raise ServiceError(400, f'{field} is required.')
        try:
            validate_schedule(result['cron'], result['timezone'])
            next_time = next_run(result['cron'], result['timezone'], datetime.now(timezone.utc))
        except ValueError as error:
            raise ServiceError(400, str(error)) from error
        schedule_changed = previous is None or any(result.get(key) != previous.get(key) for key in ('cron', 'timezone', 'enabled'))
        if schedule_changed:
            result['next_run_at'] = self._utc_string(next_time) if result['enabled'] else None
        result['updated_at'] = now()
        return result

    def _create_routine(self, bot: dict, data: dict) -> dict:
        if bot['archived'] or bot.get('_deleted'):
            raise ServiceError(409, 'Restore this bot before adding a routine.')
        routine = self._routine_fields(data)
        routine.update(id=uid(), bot_id=bot['id'], last_run_at=None, last_task_id=None, created_at=now())
        self._put('routines', routine)
        self._event('routine.created', routine, bot['id'])
        return routine

    def _write_routine(self, method: str, parts: list[str], data: dict) -> dict:
        routine = self._get('routines', parts[2])
        bot = self._get('bots', routine['bot_id'])
        if routine.get('_deleted') or bot.get('_deleted'):
            raise ServiceError(404, 'This routine was removed.')
        if len(parts) == 3 and method == 'PATCH':
            routine = self._routine_fields(data, routine)
            self._put('routines', routine)
            self._event('routine.updated', self._public(routine), bot['id'])
            return self._public(routine)
        if len(parts) == 3 and method == 'DELETE':
            routine.update(enabled=False, next_run_at=None, _deleted=True, updated_at=now())
            self._put('routines', routine)
            self._event('routine.deleted', {'id': routine['id']}, bot['id'])
            return {'ok': True}
        if parts[3:] == ['run'] and method == 'POST':
            if bot['archived']:
                raise ServiceError(409, 'Restore the bot before running this routine.')
            active = self._active_routine_task(routine['id'])
            if active:
                return self._public(active)
            current = datetime.now(timezone.utc)
            return self._queue_routine(routine, current, current, manual=True)
        raise ServiceError(404, 'This routine route was not found.')

    def _routine_index(self) -> dict:
        """Group the tasks that can keep a routine busy by routine and by source task once, for a pass over many routines.

        _active_routine_task starts from an open routine task, or from a queued or working one, and follows the tasks
        that each task started. The index holds those tasks and every task that they started, and no other task.
        """
        with self.lock:
            rows = dict(self.db.execute(OPEN_ROUTINE_TASKS).fetchall() + self.db.execute(RUNNING_ROUTINE_TASKS).fetchall())
            frontier = rows
            while frontier:
                frontier = {rowid: data for rowid, data in self.db.execute(
                    CHILD_TASKS, (json.dumps([json.loads(data)['id'] for data in frontier.values()]),)) if rowid not in rows}
                rows.update(frontier)
        tasks = [json.loads(rows[rowid]) for rowid in sorted(rows)]
        index = dict(tasks=tasks, order={}, by_id={}, by_routine={}, by_source={})
        for position, task in enumerate(tasks):
            index['order'][task['id']] = position
            index['by_id'][task['id']] = task
            if task.get('routine_id') is not None:
                index['by_routine'].setdefault(task['routine_id'], []).append(task)
            if task.get('source_task_id') is not None:
                index['by_source'].setdefault(task['source_task_id'], []).append(task)
        return index

    def _active_routine_task(self, routine_id: str, index: dict | None = None) -> dict | None:
        """The task that keeps a routine busy. A pass over many routines gives one _routine_index to every call."""
        index = index or self._routine_index()
        # A closed occurrence releases intake unless its exact authorized
        # continuation is queued/running, including gaps between tool fences.
        related = {task['id'] for task in index['by_routine'].get(routine_id, ())
                   if not task.get('_native_job_finished') or (
                       task['status'] in ('queued', 'working')
                       and self.native_routine_completion_input(task).get('continuation'))}
        pending = list(related)
        while pending:
            for child in index['by_source'].get(pending.pop(), ()):
                if child['id'] not in related:
                    related.add(child['id'])
                    pending.append(child['id'])
        active = [index['by_id'][identity] for identity in sorted(related, key=index['order'].get)
                  if index['by_id'][identity]['status'] in self.ACTIVE or index['by_id'][identity].get('_return_pending')]
        return next((task for task in active if task['status'] != 'waiting'), active[0] if active else None)

    def _queue_routine(self, routine: dict, scheduled: datetime, current: datetime, *, manual: bool = False) -> dict:
        occurrence_id = uid() if manual else str(uuid5(UUID(routine['id']), self._utc_string(scheduled)))
        previous = self.db.execute('SELECT data FROM routine_occurrences WHERE id=?', (occurrence_id,)).fetchone()
        if previous:
            return self._public(self._get('tasks', json.loads(previous[0])['task_id']))
        bot = self._get('bots', routine['bot_id'])
        task = self._send(bot, {'text': routine['prompt'], 'mode': 'queue'}, human_authored=False)
        task = self._get('tasks', task['id'])
        task.update(routine_id=routine['id'], routine_occurrence_id=occurrence_id)
        self._put('tasks', task)
        occurrence = dict(id=occurrence_id, routine_id=routine['id'], task_id=task['id'], scheduled_at=self._utc_string(scheduled), started_at=self._utc_string(current), manual=manual)
        self._put('routine_occurrences', occurrence)
        routine.update(last_run_at=self._utc_string(current), last_task_id=task['id'], updated_at=now())
        if not manual:
            routine['next_run_at'] = self._utc_string(next_run(routine['cron'], routine['timezone'], current))
        self._put('routines', routine)
        self._message(bot['id'], f'Routine: {routine["name"]}', task_id=task['id'], role='system', kind='activity')
        self._event('routine.triggered', {'routine_id': routine['id'], 'occurrence_id': occurrence_id, 'task_id': task['id'], 'scheduled_at': occurrence['scheduled_at'], 'manual': manual}, bot['id'], task['id'])
        self._event('routine.updated', self._public(routine), bot['id'], task['id'])
        self._event('task.updated', self._public(task), bot['id'], task['id'])
        return self._public(task)

    def tick_routines(self, current: datetime | None = None):
        if hasattr(self.provider, 'native_settings'):
            return self.tick_native_routines()
        current = current or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError('Routine ticks require an aware time.')
        with self.transaction():
            index = None
            for routine in self._all('routines'):
                if self.stopping.is_set() or routine.get('_deleted') or not routine['enabled'] or not routine.get('next_run_at'):
                    continue
                scheduled = datetime.fromisoformat(routine['next_run_at'].replace('Z', '+00:00'))
                bot = self._get('bots', routine['bot_id'])
                # The grouping of the tasks is built for the first due routine. A tick with none due reads no task.
                if scheduled > current or bot['archived'] or bot.get('_deleted') or self._active_routine_task(routine['id'], index := index or self._routine_index()):
                    continue
                self._queue_routine(routine, scheduled, current)

    def _deliver_bot_message(self, source_task_id: str, payload: dict, *, returning: bool = False, record_message: bool = True) -> dict:
        with self.transaction():
            return self._store_bot_message(source_task_id, payload, returning=returning, record_message=record_message)

    def _bot_continuation_owner(self, source, target_id, identity):
        from .work_capacity import execution_key
        previous = self._get('tasks', identity)
        tasks = {t['id']: t for t in self._all('tasks')}
        owners = (previous, self._assignment(previous), tasks.get(execution_key(previous), previous))
        native = any(owner.get(key) for owner in owners
                     for key in ('_chat_session', '_native_outcome_id', '_native_job', 'routine_id'))
        # A failed turn of the bot's gateway conversation has no work session to continue. After the same checks as
        # for a worker task, the message joins that conversation as a new turn (join_conversation), which has the
        # earlier turns in its history.
        gateway_task = (self.gateway_enabled() and previous.get('_chat_session') and not previous.get('_execution_session_id')
                        and not any(owner.get(key) for owner in owners for key in ('_native_outcome_id', '_native_job', 'routine_id')))
        if gateway_task:
            native = False
        if (previous['status'] != 'failed' or native
                or previous['bot_id'] != target_id
                or previous['session_id'] != self._get('bots', target_id)['session_id']
                or previous['thread_id'] != source['thread_id']
                or previous['conversation_bot_id'] != source['conversation_bot_id']
                or previous.get('_conversation_epoch') != self._setting('conversation_epoch')):
            raise ServiceError(409, 'Choose a failed task owned by this bot in the same conversation. Use the native workflow for an outcome or routine.')
        if (self._task_paused(previous) or self._continuation_blocked(previous)
                or self._get('exchanges', previous['exchange_id']).get('cancelled')
                or self._automatic_work_is_paused(dict(previous, _work_resume=True))):
            raise ServiceError(409, 'This work is paused or cancelled. Preserve the existing decision.')
        if any(d['status'] == 'pending' and d.get('inbox_kind') != 'proposal'
               and self._get('tasks', d['task_id'])['thread_id'] == previous['thread_id']
               for d in self._all('decisions')):
            raise ServiceError(409, 'Resolve the pending decision in this conversation before continuing.')
        if previous.get('_waiting_on'):
            identity = previous['_waiting_on'][0]
            raise ServiceError(409, f'Wait for the result of delegated task {identity} before continuing its owner.')
        if gateway_task:
            return None
        self._ensure_execution_session(previous)
        for peer in tasks.values():
            if execution_key(peer) != execution_key(previous):
                continue
            if (peer['id'] != previous['id'] and peer['status'] in self.ACTIVE
                    or self._background_pending(peer['id'])):
                raise ServiceError(409, 'This execution already has active work. Reuse its current task.')
            if (peer['id'] == previous.get('_continued_task_id') or peer.get('parent_task_id') == previous['id']
                    or peer['created_at'] > previous['created_at']):
                raise ServiceError(409, f'This task already continued as {peer["id"]}. Check that task before continuing.')
        return previous

    def _store_bot_message(self, source_task_id: str, payload: dict, *, returning: bool = False, record_message: bool = True) -> dict:
        target_id = require_text(payload, 'target_bot_id', 100)
        text = require_text(payload, 'text', None if returning else 20000)
        native_id = payload.get('native_envelope_id')
        if native_id is not None and (not isinstance(native_id, str) or len(native_id) != 32 or any(c not in '0123456789abcdef' for c in native_id)):
            raise ServiceError(400, 'The native envelope ID is invalid.')
        try:
            delivery_key = [source_task_id, target_id, text]
            continuation_id = require_text(payload, 'continuation_task_id', 100) if 'continuation_task_id' in payload else None
            if continuation_id:
                delivery_key.append(continuation_id)
            if returning and payload.get('return_id'):
                delivery_key.append(payload['return_id'])
            delivery_id = digest(encoded([source_task_id, native_id])) if native_id else digest(encoded(delivery_key))
            source = self._get('tasks', source_task_id)
            existing = self.db.execute('SELECT data FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
            if existing:
                receipt = json.loads(existing[0])['receipt']
                return receipt | {'status': 'duplicate' if receipt['task_id'] else receipt['status']}
            continuation_owner = self._bot_continuation_owner(source, target_id, continuation_id) if continuation_id else None
        except ServiceError as error:
            if not native_id:
                raise
            return {'status': 'rejected', 'error': str(error), 'target_bot_id': target_id}
        progress_only = False
        exchange = self._get('exchanges', source['exchange_id'])
        cancellation_result = returning and source.get('_dependency_cancelled')
        if self.stopping.is_set() or exchange.get('cancelled') or (source['status'] == 'cancelled' and not cancellation_result):
            return {'task_id': None, 'target_bot_id': target_id, 'thread_id': source['thread_id'], 'status': 'cancelled'}
        if source['status'] != 'working' and not (returning and source['status'] in ('completed', 'failed')) and not cancellation_result:
            raise YieldForDecision('This task cannot send bot messages.')
        self._recipient_ids([target_id])
        if target_id == source['bot_id']:
            raise ServiceError(400, 'Choose another bot for this message.')
        origin = self._get('bots', source['conversation_bot_id'])
        if origin['archived'] or origin.get('_deleted'):
            raise ServiceError(409, 'The originating conversation is unavailable.')
        assignment = self._assignment(source)
        dependency_id = assignment.get('_return_dependency_id', assignment['id'])
        progress_only = not returning and target_id == assignment.get('source_bot_id') and (
            self._unfinished_work(assignment) or bool(assignment.get('_waiting_on')) or self._background_pending(assignment['id']))
        target = self._get('bots', target_id)
        sender = self._get('bots', source['bot_id'])
        owner_id = assignment.get('_reply_to_task_id') or assignment.get('source_task_id')
        owner = self._assignment(self._get('tasks', owner_id)) if target_id == assignment.get('source_bot_id') and owner_id else None
        callback = self._get('tasks', source['_relay_callback_owner']) if source.get('_relay_callback_owner') else source
        native_reply = bool(not returning and source.get('_is_return') and callback.get('_native_relay', {}).get('sender_bot_id') == target_id
                            and not callback.get('_native_relay_result') and self.db.execute(
                                "SELECT 1 FROM native_receipts WHERE json_extract(data, '$.task_id')=? "
                                "AND json_extract(data, '$._relay_task_id')=? AND json_extract(data, '$._process_delivery') IS NULL LIMIT 1",
                                (callback.get('source_task_id'), callback['id'])).fetchone())
        # Helper questions retain a separate callback while the native owner waits for that helper.
        native_return = bool(owner and owner.get('_native_outcome_id') and not progress_only)
        bot_message = {'sender_bot_id': sender['id'], 'sender_name': sender['name'], 'text': text, 'result_status': payload.get('result_status'), 'progress_only': progress_only}
        if continuation_owner:
            # Keep the native envelope's return route while restoring the saved execution.
            task = self._send(target, {'text': text, 'mode': 'queue'}, thread_id=continuation_owner['thread_id'],
                              parent_task_id=continuation_owner['id'], conversation_bot_id=origin['id'],
                              exchange_id=source['exchange_id'], source_task=source, record_message=False)
            task = self._get('tasks', task['id'])
            for key in ('_execution_session_id', '_conversation_epoch', '_work_context_id', '_work_todos',
                        '_attachment_ids', '_shared_message_ids', '_preference_source_ids', '_self_recovery', '_background_completion', '_pr_review'):
                if key in continuation_owner:
                    task[key] = copy.deepcopy(continuation_owner[key])
            # The continuation owns its copy. A later reply must not drop it as a block-check context.
            for key in ('from_block_check', 'blocked'):
                (task.get('_self_recovery') or {}).pop(key, None)
            task['_shared_message_ids'] = list(dict.fromkeys(task.get('_shared_message_ids', [])
                + continuation_owner.get('_turn_message_ids', [])))
            pending = continuation_owner.pop('_blocked_followup', None)
            if pending:
                for key, source_key in (('_shared_message_ids', 'message_ids'),
                                        ('_preference_source_ids', 'source_ids'), ('_attachment_ids', 'attachment_ids')):
                    task[key] = list(dict.fromkeys(task.get(key, []) + pending[source_key]))
                continuation_owner.update(_steering=[], steering_status='continued')
            completed = [r['id'] for r in self._native_task_receipts(continuation_owner['id'], state='completed')
                         if r.get('turn_id') == continuation_owner.get('_native_turn_id')]
            task['_continuation_receipt_ids'] = list(dict.fromkeys(
                continuation_owner.get('_continuation_receipt_ids', []) + completed))
            related_ids = {t['id'] for t in self._all('tasks') if execution_key(t) == execution_key(continuation_owner)}
            recovery = task.setdefault('_self_recovery', {})
            recovery['previous_task_id'] = continuation_owner['id']
            decisions = [self._public(d) for d in self._all('decisions') if d['task_id'] in related_ids]
            recovery['decisions'] = list({d['id']: d for d in recovery.get('decisions', []) + decisions}.values())
            unknown = [r for identity in related_ids for r in self._native_task_receipts(identity, state='executing')]
            if unknown:
                recovery['unknown_tools'] = list({r['id']: r for r in recovery.get('unknown_tools', [])
                    + [{k: r.get(k) for k in ('id', 'task_id', 'turn_id', 'action', 'created_at')} for r in unknown]}.values())
            task.update(title=continuation_owner['title'],
                        _bot_messages=copy.deepcopy(continuation_owner.get('_bot_messages', [])) + [bot_message], _is_return=False)
            continuation_owner['_continued_task_id'] = task['id']
            for artifact in self._all('artifacts'):
                if (artifact['kind'] == 'pr' and artifact['bot_id'] == task['bot_id']
                        and artifact.get('_review_task_id') in related_ids):
                    peers = [a for a in self._all('artifacts')
                             if a['kind'] == 'pr' and a['url'].lower() == artifact['url'].lower()]
                    owner = self._pr_review_owner(peers)
                    if not owner or owner['_review_task_id'] not in related_ids:
                        continue
                    self._set_review_owner(artifact, task['id'])
                    artifact['_review_ownership_at'] = now()
                    self._put('artifacts', artifact)
                    self._event('artifact.updated', self._public(artifact), artifact['bot_id'], artifact.get('task_id'))
            self._put('tasks', continuation_owner)
            native_reply = native_return = False
        elif native_reply:
            # Reply to the waiting sender through its existing Hermes envelope.
            # A new delegation would make both workers wait for each other.
            reply = dict(callback['_native_relay'], text=text, failed=payload.get('result_status') == 'failed')
            callback.update(_native_reply_pending=reply, _native_relay_result=reply, _native_explicit_reply=True)
            self._put('tasks', callback)
            task = self._get('tasks', callback['source_task_id'])
        elif native_return:
            # A dependency result resumes the native owner, including its claim and workspace.
            task = owner
            task.setdefault('_bot_messages', []).append(dict(bot_message,
                _return_source_task_id=source['id'], _return_delivery_id=delivery_id, _returning=returning))
            if task.get('_native_job') and not task.get('_native_job_finished') and task['status'] != 'working':
                # A blocked owner can remain waiting even though a new result arrived.
                # Historical _bot_messages stay saved, so track pending input separately.
                task['_routine_pending_bot_result'] = delivery_id
            if not progress_only:
                task['_waiting_on'] = [identity for identity in task.get('_waiting_on', []) if identity != dependency_id]
            native = self.kanban.tasks.get(task['id'], {})
            if task['status'] not in ('cancelled', 'paused', 'needs_you') and native.get('status') not in ('done', 'archived'):
                if task['status'] == 'working':
                    task.setdefault('_steering', []).append(sender['name'] + ': ' + text)
                elif not self._automatic_work_is_paused(task) and not self._continuation_blocked(task) and not any(
                        d['task_id'] == task['id'] and d['status'] == 'pending' for d in self._all('decisions')):
                    if task['status'] != 'queued':
                        self._advance_input(task)
                    task.update(status='queued', summary='Continuing with the result.')
            task['updated_at'] = now()
            self._put('tasks', task)
            self._event('task.updated', self._public(task), task['bot_id'], task['id'])
            self._refresh_bot(task['bot_id'])
        else:
            task = self._send(target, {'text': text, 'mode': 'queue'}, thread_id=source['thread_id'], parent_task_id=source['id'], conversation_bot_id=origin['id'], exchange_id=source['exchange_id'], source_task=source, record_message=False, text_limit=None if returning else 20000)
            task = self._get('tasks', task['id'])
            task['_bot_messages'] = [bot_message]
            task['_is_return'] = returning or target_id == assignment.get('source_bot_id')
            if not returning and source.get('_is_return') and source.get('source_task_id'):
                previous = self._get('tasks', source['source_task_id'])
                if (previous['status'] == 'failed'
                        and all(previous.get(k) == task.get(k) for k in ('bot_id', 'session_id', 'conversation_bot_id'))
                        and previous.get('_conversation_epoch') == self._setting('conversation_epoch')
                        and (previous.get('_execution_session_id') or not previous.get('_chat_session'))):
                    # The coordinator continues the failed worker in its saved workspace. A failed turn of the bot's
                    # gateway conversation has no work session: the retry joins that conversation (join_conversation).
                    self._ensure_execution_session(previous)
                    task['_execution_session_id'] = previous['_execution_session_id']
            # With the gateway, the message or the return is a turn of the target bot's conversation.
            self.join_conversation(task)
        if native_reply or native_return:
            pass
        elif not continuation_owner and target_id == assignment.get('source_bot_id') and owner_id:
            owner = self._assignment(self._get('tasks', owner_id))
            task.update(_is_return=True, _assignment_task_id=owner['id'])
            if assignment.get('_requester_reply_task_id'):
                task['_relay_callback_owner'] = assignment['_requester_reply_task_id']
            if not progress_only:
                task['_resolved_dependency_id'] = dependency_id
        elif not task['_is_return']:
            task['_reply_to_task_id'] = assignment['id']
            callback_owner = source.get('_relay_callback_owner') or (source['id'] if source.get('_native_relay') else None)
            if callback_owner:
                task['_requester_reply_task_id'] = callback_owner
            assignment['_waiting_on'] = list(dict.fromkeys([*assignment.get('_waiting_on', []), task['id']]))
            self._put('tasks', assignment)
        if native_id and not native_reply:
            task['_native_relay'] = {'sender_bot_id': source['bot_id'], 'envelope_id': native_id}
        self._put('tasks', task)
        returned_message = next((m for m in reversed(self._all('messages'))
                                 if returning and m.get('task_id') == source.get('_result_task_id', source['id'])
                                 and m.get('sender_bot_id') == sender['id'] and m['kind'] == 'text'
                                 and m['text'] == text and not m.get('_direct_reply_message_id')
                                 and not m.get('_hidden') and not m.get('_deleted')), None)
        if returned_message:
            returned_message['exchange_recipient_bot_ids'] = [target_id]
            self._put('messages', returned_message)
            self._event('message.updated', self._public(returned_message), sender['id'], source['id'])
        elif record_message:
            self._message(origin['id'], text, task_id=task['id'], sender_bot_id=sender['id'], recipient_bot_ids=[target_id])
        if not returning:
            exchange['deliveries'] += 1
        self._put('exchanges', exchange)
        receipt = {'task_id': task['id'], 'target_bot_id': target_id, 'thread_id': task['thread_id'], 'status': 'paused' if task['status'] == 'paused' else 'queued'}
        if native_reply:
            receipt['disposition'] = 'reply'
        self._event('bot_message.delivered', {'source_task_id': source['id'], 'source_bot_id': sender['id'], 'target_bot_id': target_id, 'task_id': task['id'], 'thread_id': task['thread_id'], 'conversation_bot_id': origin['id'], 'exchange_id': exchange['id'], 'deliveries': exchange['deliveries'], 'is_return': native_reply or native_return or task.get('_is_return', False)}, origin['id'], task['id'])
        self._put('deliveries', dict(id=delivery_id, source_task_id=source['id'], target_bot_id=target_id, text=text, receipt=receipt, created_at=now(), _progress_only=progress_only))
        return receipt

    def _assignment(self, task: dict) -> dict:
        return self._get('tasks', task['_assignment_task_id']) if task.get('_assignment_task_id') else task

    def _finish_assignment_turn(self, task: dict, text: str, result_status: str):
        assignment = self._assignment(task)
        dependency_id = task.get('_resolved_dependency_id')
        if dependency_id:
            assignment['_waiting_on'] = [identity for identity in assignment.get('_waiting_on', []) if identity != dependency_id]
        # A failed continuation must not fail the parent's independent dependencies.
        can_wait = result_status in ('completed', 'rejected') or (result_status == 'failed' and assignment['id'] != task['id'] and bool(assignment.get('_waiting_on')))
        waiting = can_wait and (bool(assignment.get('_waiting_on')) or self._background_pending(assignment['id'], finishing_task_id=task['id']))
        if assignment['id'] == task['id']:
            if waiting:
                task.update(status='waiting', summary='Waiting for a result.')
            else:
                self._prepare_return(task, text, result_status)
            return
        # The continuation keeps the original assignment and its return address.
        # A reply can queue while that assignment still has a running turn.
        self._prepare_return(task, text, result_status)
        # Preparing the callback can save the original assignment's reply receipt.
        assignment = self._get('tasks', assignment['id'])
        if dependency_id:
            assignment['_waiting_on'] = [identity for identity in assignment.get('_waiting_on', []) if identity != dependency_id]
        if waiting and self._background_pending(task['id'], finishing_task_id=task['id']):
            task.update(status='waiting', summary='Waiting for a result.')
            if assignment['status'] == 'completed':
                assignment['status'] = 'waiting'
        if task.get('_background_completion') or task.get('_pr_review') or task.get('_work_resume'):
            for key in ('_background_return_after', '_background_return_id'):
                assignment[key] = task[key]
        if assignment['status'] == 'waiting' or ((task.get('_background_completion') or task.get('_pr_review') or task.get('_work_resume')) and assignment['status'] == 'completed') or (result_status == 'failed' and assignment['status'] != 'cancelled'):
            if not waiting:
                assignment.update(status='completed' if result_status == 'rejected' else result_status,
                                  summary=task['summary'], updated_at=now(), _result_task_id=task['id'])
                self._prepare_return(assignment, text, result_status, finishing=task)
        self._put('tasks', assignment)
        self._event('task.updated', self._public(assignment), assignment['bot_id'], assignment['id'])

    def _prepare_return(self, task: dict, text: str, result_status: str, finishing: dict | None = None):
        result_key = None
        if task.get('_native_outcome_id') and result_status == 'completed':
            native = self.kanban.tasks.get(task['id'], {}) if self.kanban else {}
            if native.get('id') != task['_native_outcome_id'] or native.get('status') != 'done':
                return
            result_key = (native['id'], str(task.get('_native_turn_id') or task.get('_message_id') or task['id']))
            if native.get('input_id') != result_key[1]:
                return
            if task.get('_native_return_prepared') == list(result_key):
                return
        if result_status == 'failed':
            self._hold_failed_followup(task)
        if not self._prepare_native_reply(task, text, result_status, finishing=finishing):
            return
        if result_key:
            task['_native_return_prepared'] = list(result_key)
        if not task.get('source_bot_id') or task.get('_is_return'):
            return
        # Indexes find the turns of this assignment and their deliveries. A parse of the whole task table took about
        # 0.6 s of each finished turn under the service lock on the Mac mini copy.
        turn_ids = {row[0] for row in self.db.execute(
            "SELECT id FROM tasks WHERE id=? OR json_extract(data, '$._assignment_task_id')=?", (task['id'], task['id']))}
        deliveries = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM deliveries WHERE json_extract(data,'$.source_task_id') IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(turn_ids)),))]
        already_sent = any(not delivery.get('_progress_only') and delivery['source_task_id'] in turn_ids and delivery['target_bot_id'] == task['source_bot_id'] and delivery['created_at'] >= task.get('_background_return_after', '') and delivery['receipt']['task_id'] for delivery in deliveries)
        if not already_sent:
            task['_return_pending'] = {'target_bot_id': task['source_bot_id'], 'text': text, 'result_status': result_status,
                                       'return_id': task.get('_background_return_id')}

    def _continuation_blocked(self, task: dict) -> bool:
        assignment_id = task.get('_assignment_task_id', task['id'])
        with self.lock:
            # Work projection checks many receipts. Read only the indexed blockers.
            rows = self.db.execute("SELECT data FROM tasks WHERE json_extract(data, '$.status') IN ('needs_you', 'paused') AND id<>?", (task['id'],))
            return any(turn.get('_assignment_task_id', turn['id']) == assignment_id
                       for turn in (json.loads(row[0]) for row in rows))

    def _hold_failed_followup(self, task):
        if not task.get('_steering') or task.get('_blocked_followup'):
            return
        messages = [m for m in self._all('messages') if m['task_id'] == task['id']
                    and m['role'] == 'user' and m['kind'] == 'text' and not m.get('decision_id')]
        pending = messages[-len(task['_steering']):]
        task['_blocked_followup'] = dict(message_ids=[m['id'] for m in pending],
            source_ids=[m['id'] for m in pending if m.get('_human_device_id')],
            attachment_ids=list(task.get('_attachment_ids', [])))
        task['steering_status'] = 'blocked'
        task['summary'] += ' Your follow-up is saved. Reply to continue.'

    def _drain_returns(self):
        self._restore_saved_delegation_results()
        # A result can arrive after the owner failed on an unrelated tool.
        # Consume only the exact persisted delivery; never restart that tool.
        with self.lock:
            stopped = [row[0] for row in self.db.execute(
                "SELECT id FROM tasks WHERE json_extract(data,'$.status')='failed' "
                "AND json_extract(data,'$._is_return')=1 "
                "AND json_type(data,'$._resolved_dependency_id')='text'")]
        for task_id in stopped:
            with self.transaction():
                task = self._get('tasks', task_id)
                if task['status'] == 'failed' and task.get('_is_return'):
                    self._consume_stopped_dependency_result(task, self._assignment(task))
        with self.lock:
            native_replies = [(task['id'], task['_native_reply_pending']) for task in self._marked_tasks('_native_reply_pending')]
        for task_id, reply in native_replies:
            sender = self._get('bots', reply['sender_bot_id'])
            if not sender.get('_deleted') and hasattr(self.provider, 'native_settings'):
                try:
                    self.provider.native_settings('relay_reply', [sender], bot_id=sender['id'],
                                                  envelope_id=reply['envelope_id'], text=reply['text'], failed=reply['failed'])
                except (OSError, ValueError, RuntimeError):
                    continue
            with self.transaction():
                task = self._get('tasks', task_id)
                task.pop('_native_reply_pending', None)
                self._put('tasks', task)
        with self.lock:
            returns = [task['id'] for task in self._marked_tasks('_return_pending')]
        for task_id in returns:
            with self.transaction():
                task = self._get('tasks', task_id)
                payload = task.get('_return_pending')
                if not payload:
                    continue
                recipients = [self._get('bots', identity) for identity in {payload['target_bot_id'], task['conversation_bot_id']}]
                cancelled = (task['status'] == 'cancelled' and not task.get('_dependency_cancelled')) or self._get('exchanges', task['exchange_id']).get('cancelled')
                if not cancelled and not any(bot.get('_deleted') for bot in recipients) and any(bot['archived'] for bot in recipients):
                    continue
                # Recovery can queue a new turn while an earlier reply remains saved.
                # Keep that reply pending without blocking unrelated native claims.
                if not cancelled and task['status'] not in ('completed', 'failed') and not task.get('_dependency_cancelled'):
                    continue
                try:
                    self._store_bot_message(task_id, payload, returning=True)
                except ServiceError as error:
                    self._message(task['bot_id'], f'The result could not return to its sender: {error}', task_id=task_id, role='system', kind='status')
                task.pop('_return_pending', None)
                self._put('tasks', task)

    def _new_exchange(self) -> str:
        exchange_id = uid()
        self._put('exchanges', dict(id=exchange_id, deliveries=0, paused=False, created_at=now()))
        return exchange_id

    def _recipient_ids(self, values: Any) -> list[str]:
        if not isinstance(values, list) or len(values) > 20 or any(not isinstance(value, str) for value in values):
            raise ServiceError(400, 'recipient_bot_ids must contain at most 20 bot IDs.')
        recipients = list(dict.fromkeys(values))
        for recipient_id in recipients:
            bot = self._get('bots', recipient_id)
            if bot.get('_deleted') or bot['archived']:
                raise ServiceError(409, 'Choose bots that are active and not archived.')
        return recipients

    def _interaction_target(self, message_id, bot_id=None):
        if not isinstance(message_id, str):
            raise ServiceError(400, 'Choose a message from this conversation.')
        message = self._get('messages', message_id)
        if (message.get('_hidden') or message.get('_deleted') or message.get('streaming')
                or message['kind'] != 'text' or message['role'] not in ('user', 'assistant')
                or message.get('reaction_event') or message.get('exchange_recipient_bot_ids')
                or (bot_id is not None and message['bot_id'] != bot_id)):
            raise ServiceError(409, 'Choose a finished message from this conversation.')
        bot = self._get('bots', message['bot_id'])
        if bot.get('_deleted') or bot['archived']:
            raise ServiceError(409, 'Restore this conversation first.')
        return message

    def _reply_reference(self, message):
        author = 'You' if message['role'] == 'user' else self._get('bots', message.get('sender_bot_id') or message['bot_id'])['name']
        return dict(message_id=message['id'], text=message['text'], author=author, role=message['role'],
                    author_bot_id=message.get('sender_bot_id') if message['role'] == 'assistant' else None)

    def _send_message_interaction(self, bot, data, *, task=None):
        if task and task.get('_work_status_only'):
            data = dict(data, work_artifact_id=task['_work_context_id'], work_status_only=False, mode='queue')
        work_item = self._work_message_target(bot, data) if data.get('work_artifact_id') else None
        target_id = data.get('reply_to_message_id')
        mentioned = self._recipient_ids(data.get('mentioned_bot_ids', []))
        if mentioned and data.get('recipient_bot_ids'):
            raise ServiceError(400, 'A mention cannot change the message recipient.')
        interaction = dict(bot_mentions=[dict(id=identity, name=self._get('bots', identity)['name'])
                                         for identity in mentioned]) if mentioned else None
        if target_id is not None:
            # A saved cross-chat quote supplies context; the requested bot remains the recipient.
            target = self._interaction_target(target_id)
            interaction = (interaction or {}) | dict(reply_to=self._reply_reference(target))
        if work_item:
            interaction = (interaction or {}) | dict(work_context=dict(id=work_item['id'], title=work_item['title']))
            data = dict(data, mode='queue')
        self._message_interaction = interaction
        try:
            return self._send(bot, data, work_item=work_item) if work_item else (self._reply(task, data) if task else self._send(bot, data))
        finally:
            self._message_interaction = None

    def _react_to_message(self, message_id, data):
        message = self._interaction_target(message_id)
        emoji, active = data.get('emoji'), data.get('active')
        if emoji not in ('👍', '👎', '😄', '🎉', '😕', '❤️', '🚀', '👀') or not isinstance(active, bool):
            raise ServiceError(400, 'Choose one of the available reactions.')
        reactions = list(message.get('reactions', []))
        if (emoji in reactions) == active:
            return self._public(message)
        if active:
            reactions.append(emoji)
        else:
            reactions.remove(emoji)
        message['reactions'] = reactions
        self._put('messages', message)
        self._event('message.updated', self._public(message), message['bot_id'], message.get('task_id'))
        if active and emoji in ('👍', '👎'):
            self._message_interaction = dict(reply_to=self._reply_reference(message),
                                             reaction_event=dict(emoji=emoji, action='added'))
            try:
                author_id = message.get('sender_bot_id') if message['role'] == 'assistant' else None
                recipient = self._get('bots', author_id or message['bot_id'])
                pending = recipient.get('_pending_reactions', {}).get(message_id)
                if pending:
                    pending.update(event_id=uid(), reactions=reactions)
                    self._put('bots', recipient)
                self._send(recipient,
                           dict(text=f'Reaction added: {emoji}', mode='auto'),
                           conversation_bot_id=message['bot_id'])
            finally:
                self._message_interaction = None
            return self._public(message)
        event = dict(emoji=emoji, action='added' if active else 'removed', passive=True)
        reference = self._reply_reference(message)
        author_id = message.get('sender_bot_id') if message['role'] == 'assistant' else None
        recipient = self._get('bots', author_id or message['bot_id'])
        self._message_interaction = dict(reply_to=reference, reaction_event=event)
        try:
            feedback = self._message(message['bot_id'], f"Reaction {event['action']}: {emoji}",
                                     role='user', recipient_bot_ids=[recipient['id']], human_authored=True)
        finally:
            self._message_interaction = None
        recipient = self._get('bots', recipient['id'])
        recipient.setdefault('_pending_reactions', {})[message_id] = dict(
            event_id=feedback['id'], message_id=message_id, reactions=reactions,
            author=reference['author'], excerpt=message['text'][:1000])
        self._put('bots', recipient)
        return self._public(message)

    def _ack_reaction_feedback(self, bot_id, task_id, feedback):
        with self.transaction():
            if self.stopping.is_set():
                return
            bot = self._get('bots', bot_id)
            pending = bot.get('_pending_reactions', {})
            for item in feedback:
                current = pending.get(item['message_id'])
                if current and current['event_id'] == item['event_id']:
                    del pending[item['message_id']]
            self._put('bots', bot)
            task = self._get('tasks', task_id)
            if task.get('_reaction_feedback') == feedback:
                task.pop('_reaction_feedback', None)
                self._put('tasks', task)

    def _send(self, bot: dict, data: dict, *, thread_id: str | None = None, parent_task_id: str | None = None, conversation_bot_id: str | None = None, exchange_id: str | None = None, source_task: dict | None = None, record_message: bool = True, human_authored: bool = True, text_limit: int | None = 20000, work_item: dict | None = None) -> dict:
        text = require_text(data, 'text', text_limit)
        mode = data.get('mode', 'queue')
        if mode not in ('auto', 'steer', 'queue'):
            raise ServiceError(400, 'Unsupported message mode.')
        if bot['archived'] or bot.get('_deleted'):
            raise ServiceError(409, 'Restore this bot before sending work.')
        conversation_bot_id = conversation_bot_id or bot['id']
        origin = self._get('bots', conversation_bot_id)
        if origin['archived'] or origin.get('_deleted'):
            raise ServiceError(409, 'Restore this conversation before sending work.')
        recipients = self._recipient_ids(data.get('recipient_bot_ids', []))
        if recipients:
            if mode == 'steer':
                raise ServiceError(400, 'Mentioned bots receive separate messages.')
            return self._dispatch_user(origin, data, recipients, thread_id=thread_id, parent_task_id=parent_task_id)
        attachments = self._validate_attachments(conversation_bot_id, data.get('attachment_ids', []))
        if work_item and work_item.get('_native_outcome_id') and not data.get('work_status_only'):
            owner = self._get('tasks', work_item['task_id'])
            native = self.kanban.tasks.get(owner['id'], {}) if self.kanban else {}
            if owner['status'] != 'cancelled' and native.get('status') not in ('done', 'archived'):
                return self._reply(owner, data)
        if mode == 'auto' and not data.get('scheduled_at'):
            # Route from current server state. A bot exchange never owns a new human message.
            current = next((task for task in reversed(self._all('tasks'))
                            if task['bot_id'] == bot['id'] and task['conversation_bot_id'] == conversation_bot_id
                            and (not getattr(self.provider, 'supports_native_outcomes', False) or task.get('_chat_session'))
                            and not task.get('source_bot_id') and not task.get('scheduled_at') and not task.get('_work_status_only')
                            and not task.get('_source_audit_prompt') and not task.get('_native_job')
                            and task['status'] in ('working', 'queued', 'waiting')), None)
            if current:
                pending_tasks = {task['id'] for task in self._all('tasks') if task['thread_id'] == current['thread_id']}
                pending = any(d['task_id'] in pending_tasks and d['status'] == 'pending' and d.get('inbox_kind') != 'proposal' for d in self._all('decisions'))
                if not pending:
                    return self._reply(current, data)
        if mode == 'steer':
            if not data.get('task_id'):
                raise ServiceError(400, 'Choose an active task to steer.')
            task = self._get('tasks', data['task_id'])
            owner = task['bot_id'] if task.get('source_bot_id') else task['conversation_bot_id']
            if owner != conversation_bot_id or task.get('_work_status_only') or task['status'] not in ('working', 'needs_you'):
                raise ServiceError(409, 'Choose an active task from this conversation.')
            if task['status'] == 'needs_you':
                decisions = [decision for decision in self._all('decisions') if decision['task_id'] == task['id'] and decision['status'] == 'pending']
                if len(decisions) != 1:
                    raise ServiceError(409, 'Choose a pending decision before asking for context.')
                self._request_context(decisions[0], task, text, attachments)
                return self._public(task) | {'reply_status': 'context_requested'}
            return self._steer_task(task, text, attachments, conversation_bot_id, human_authored=human_authored)
        scheduled = data.get('scheduled_at')
        if scheduled:
            try:
                parsed = datetime.fromisoformat(scheduled.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError()
            except (ValueError, AttributeError):
                raise ServiceError(400, 'scheduled_at must contain an ISO 8601 date with a time zone.')
        task_id = uid()
        blocked = None
        if record_message and human_authored and not work_item:
            blocked = next((prior for prior in reversed(self._all('tasks'))
                if prior['bot_id'] == bot['id'] and prior['status'] == 'failed' and prior.get('_blocked_followup')
                and (prior['id'] == parent_task_id if parent_task_id else
                     (prior['bot_id'] if prior.get('source_bot_id') else prior['conversation_bot_id']) == conversation_bot_id)
                and (thread_id is None or prior['thread_id'] == thread_id)), None)
            if blocked:
                thread_id, parent_task_id = blocked['thread_id'], blocked['id']
        status = 'scheduled' if scheduled else 'queued'
        parent = self._get('tasks', parent_task_id) if parent_task_id and not source_task else None
        title = work_item['title'] if work_item else (parent['title'] if parent and parent['status'] == 'failed' else text[:72])
        task = dict(id=task_id, thread_id=thread_id or task_id, parent_task_id=parent_task_id, bot_id=bot['id'], conversation_bot_id=conversation_bot_id, session_id=bot['session_id'], source_bot_id=source_task['bot_id'] if source_task else None, source_task_id=source_task['id'] if source_task else None, exchange_id=exchange_id or self._new_exchange(), exchange_depth=source_task.get('exchange_depth', 0) + 1 if source_task else 0, title=title, status=status, summary='Waiting for its scheduled time.' if scheduled else 'Queued', created_at=now(), updated_at=now(), scheduled_at=scheduled, _attachment_ids=attachments)
        if getattr(self.provider, 'supports_native_outcomes', False) and source_task:
            task['_native_intake'] = True
        if (getattr(self.provider, 'supports_native_outcomes', False) and human_authored
                and not work_item and not source_task and (not parent or parent.get('_chat_session')) and not scheduled):
            task['_chat_session'] = True
        if work_item:
            task.update(_work_context_id=work_item['id'], _work_status_only=data.get('work_status_only', False),
                        _conversation_epoch=self._setting('conversation_epoch'))
            # Capture the owner before recovery transfers the visible work item to this task.
            self._ensure_execution_session(task)
            if work_item.get('_native_outcome_id') and not data.get('work_status_only'):
                task['_native_outcome_id'] = work_item['_native_outcome_id']
                self._carry_work_history(work_item, dict(work_item))
                work_item.update(task_id=task['id'], status='waiting', activity='Queued', _work_resuming=True)
                self._put('artifacts', work_item)
        elif parent and not parent.get('_chat_session') and parent['status'] == 'failed' and parent['bot_id'] == task['bot_id']:
            self._ensure_execution_session(parent)
            task['_execution_session_id'] = parent['_execution_session_id']
        if self._task_paused(task):
            task['status'] = 'paused'
        if blocked:
            pending = blocked.pop('_blocked_followup')
            task['_shared_message_ids'] = pending['message_ids']
            task['_preference_source_ids'] = pending['source_ids']
            task['_attachment_ids'] = list(dict.fromkeys(pending['attachment_ids'] + attachments))
            blocked.update(_steering=[], steering_status='continued', _continued_task_id=task_id,
                           summary=blocked['summary'].removesuffix(' Your follow-up is saved. Reply to continue.'))
            self._put('tasks', blocked)
            self._event('task.updated', self._public(blocked), blocked['conversation_bot_id'], blocked['id'])
        self._put('tasks', task)
        if record_message:
            message = self._message(bot['id'], text, task_id=task['id'], role='user', human_authored=human_authored)
            task.setdefault('_preference_source_ids', [])
            if message.get('_human_device_id'):
                task['_preference_source_ids'].append(message['id'])
            self._record_attachments(conversation_bot_id, task, attachments)
            self._put('tasks', task)
        self._event('task.created', self._public(task), conversation_bot_id, task['id'])
        self._refresh_bot(bot['id'])
        return self._public(task)

    def _record_direct_reply(self, task):
        bot_id = task['bot_id']
        direct = next((m for m in (self._get('messages', identity) for identity in reversed(task['_turn_message_ids']))
                       if m['role'] == 'user' and m['bot_id'] == bot_id
                       and not m.get('sender_bot_id') and not m.get('reaction_event')
                       and m.get('recipient_bot_ids') == [bot_id]), None) if task.get('source_bot_id') else None
        if direct:
            task['_direct_reply_message_id'] = direct['id']

    def _deliver_live_messages(self, task_id):
        deliver = getattr(self.provider, 'steer_task', None)
        if deliver is None:
            return
        with self.lock:
            task = self._get('tasks', task_id)
            from .native_conversation import chat_message_content
            messages = []
            # encoded() sorts mapping keys; database insertion order is the
            # conversation order, including messages with equal timestamps.
            for message in self._task_messages(task_id):
                identity = message['id']
                if identity not in task.get('_live_messages', {}):
                    continue
                current = dict(task, _visible_reply_contexts={identity: message.get('_visible_reply_context')})
                delivery = dict(id=identity, text=chat_message_content(
                    {'operation': 'start', 'task': current}, message))
                try:
                    source = self._human_source(task, identity, task, 'create or delete bots')
                except ServiceError:
                    pass
                else:
                    delivery['bot_source'] = {'id': source['id'], 'text': source['text']}
                messages.append(delivery)
            turn_id = task.get('_native_turn_id')
        if messages:
            try:
                deliver(task_id, turn_id, messages)
            except (OSError, RuntimeError):
                logging.getLogger(__name__).exception('Live input delivery failed; input remains queued.')

    def _steer_task(self, task, text, attachments, conversation_bot_id, human_authored=True):
        message = self._message(task['bot_id'], text, task_id=task['id'], role='user', human_authored=human_authored)
        task.setdefault('_preference_source_ids', [])
        if message.get('_human_device_id'):
            task['_preference_source_ids'].append(message['id'])
        self._record_attachments(conversation_bot_id, task, attachments)
        task.setdefault('_steering', []).append(text)
        # Never advance live input past an earlier message reserved for the next turn.
        if human_authored and not attachments and len(task['_steering']) == len(task.get('_live_messages', {})) + 1:
            task.setdefault('_live_messages', {})[message['id']] = text
        task.update(updated_at=now(), steering_status='next_safe_turn', exchange_id=self._new_exchange(), exchange_depth=0)
        self._put('tasks', task)
        self._event('task.steered', self._public(task), conversation_bot_id, task['id'])
        return self._public(task)

    def _dispatch_user(self, origin: dict, data: dict, recipients: list[str], *, thread_id: str | None, parent_task_id: str | None) -> dict:
        text = require_text(data, 'text', 20000)
        attachments = self._validate_attachments(origin['id'], data.get('attachment_ids', []))
        thread_id, exchange_id = thread_id or uid(), self._new_exchange()
        dispatched = []
        for recipient_id in recipients:
            recipient = self._get('bots', recipient_id)
            task = self._send(recipient, dict(text=text, mode='queue', scheduled_at=data.get('scheduled_at'), attachment_ids=attachments), thread_id=thread_id, parent_task_id=parent_task_id, conversation_bot_id=origin['id'], exchange_id=exchange_id, record_message=False)
            dispatched.append(task)
        first = self._get('tasks', dispatched[0]['id'])
        message = self._message(origin['id'], text, task_id=first['id'], role='user', recipient_bot_ids=recipients, human_authored=True)
        for dispatched_task in dispatched:
            stored_task = self._get('tasks', dispatched_task['id'])
            stored_task['_shared_message_ids'] = [message['id']]
            stored_task['_preference_source_ids'] = [message['id']] if message.get('_human_device_id') else []
            self._put('tasks', stored_task)
        first = self._get('tasks', first['id'])
        self._record_attachments(origin['id'], first, attachments, recipient_bot_ids=recipients)
        self._put('tasks', first)
        self._event('bots.addressed', {'recipient_bot_ids': recipients, 'task_ids': [t['id'] for t in dispatched], 'thread_id': thread_id}, origin['id'], first['id'])
        return dispatched[0] | {'dispatched_task_ids': [t['id'] for t in dispatched]}


    def _task_paused(self, task: dict) -> bool:
        for identity in {task['bot_id'], task['conversation_bot_id']}:
            bot = self._get('bots', identity)
            if bot['archived'] or bot.get('_deleted'):
                return True
        return False

    def _validate_attachments(self, bot_id: str, artifact_ids: Any) -> list[str]:
        if not isinstance(artifact_ids, list) or len(artifact_ids) > 20 or any(not isinstance(a, str) for a in artifact_ids):
            raise ServiceError(400, 'attachment_ids must contain at most 20 artifact IDs.')
        for artifact_id in artifact_ids:
            attachment = self._get('artifacts', artifact_id)
            if attachment['bot_id'] != bot_id or not attachment.get('_attachment'):
                raise ServiceError(400, 'This attachment does not belong to this bot.')
        return list(dict.fromkeys(artifact_ids))

    def _request_context(self, decision: dict, task: dict, question: str, attachments: list[str], reply_device=None) -> dict:
        message = self._message(task['bot_id'], question, task_id=task['id'], role='user', decision_id=decision['id'], human_authored=True)
        context_attachments = list(dict.fromkeys(task.get('_turn_attachment_ids', task.get('_attachment_ids', [])) + attachments))
        self._record_attachments(task['bot_id'], task, attachments)
        self._put('tasks', task)
        context = dict(id=uid(), bot_id=task['bot_id'], task_id=task['id'], decision_id=decision['id'], question=question, status='queued', _preference_source_ids=[message['id']] if message.get('_human_device_id') else [])
        context['_message_id'] = message['id']
        context['_reply_device'] = reply_device
        context['_decision_revision'] = decision['revision']
        context['_attachment_ids'] = context_attachments
        context['_conversation_epoch'] = task.get('_conversation_epoch', self._setting('conversation_epoch'))
        self._put('contexts', context)
        self._event('context.queued', {'id': context['id'], 'decision_id': decision['id']}, task['bot_id'], task['id'])
        return self._public(message)

    def _reply(self, task: dict, data: dict) -> dict:
        text = require_text(data, 'text', 20000)
        bot = self._get('bots', task['bot_id'])
        if bot.get('_deleted') or bot['archived']:
            raise ServiceError(409, 'Restore this bot before replying.')
        origin = self._get('bots', task['conversation_bot_id'])
        if origin['archived'] or origin.get('_deleted'):
            raise ServiceError(409, 'Restore this conversation before replying.')
        recipients = self._recipient_ids(data.get('recipient_bot_ids', []))
        if recipients:
            if data.get('decision_id'):
                raise ServiceError(400, 'Ask decision context or address bots in separate replies.')
            return self._dispatch_user(origin, data, recipients, thread_id=task['thread_id'], parent_task_id=task['id']) | {'reply_status': 'queued_recipients'}
        attachments = self._validate_attachments(origin['id'], data.get('attachment_ids', []))
        thread_tasks = {t['id'] for t in self._all('tasks') if t['thread_id'] == task['thread_id']}
        pending = [d for d in self._all('decisions') if d['task_id'] in thread_tasks and d['status'] == 'pending' and d.get('inbox_kind') != 'proposal']
        if data.get('decision_id'):
            decision = self._get('decisions', data['decision_id'])
            if decision['task_id'] not in thread_tasks or decision['status'] != 'pending':
                raise ServiceError(409, 'Choose a pending decision from this thread.')
            pending_task = self._get('tasks', decision['task_id'])
            self._request_context(decision, pending_task, text, attachments)
            result = self._public(pending_task)
            result['reply_status'] = 'context_requested'
            return result
        if pending:
            raise ServiceError(409, 'Choose the pending decision to ask for context. The decision will remain open.')
        if task['status'] == 'working':
            result = self._steer_task(task, text, attachments, origin['id'])
            result['reply_status'] = 'next_safe_turn'
            return result
        if task.get('_native_outcome_id'):
            native = self.kanban.tasks.get(task['id'], {}) if self.kanban else {}
            if (native.get('status') in ('blocked', 'triage')
                    or task['status'] in ('completed', 'failed') and native.get('status') not in ('done', 'archived')):
                task['status'] = 'waiting'
        if task['status'] in ('queued', 'paused', 'scheduled', 'waiting'):
            was_waiting = task['status'] == 'waiting'
            if was_waiting:
                self._advance_input(task)
                task['status'] = 'queued'
            message = self._message(bot['id'], text, task_id=task['id'], role='user', human_authored=True)
            # A human message replaces a queued blocked-work check. It is not a service check.
            self._end_block_context(task)
            if was_waiting or task.get('_steering') or '_turn_message_ids' in task:
                task.setdefault('_steering', []).append(text)
            task.setdefault('_preference_source_ids', [])
            if message.get('_human_device_id'):
                task['_preference_source_ids'].append(message['id'])
            self._record_attachments(bot['id'], task, attachments)
            task.update(updated_at=now(), summary='Your reply is queued with this task.', exchange_id=self._new_exchange(), exchange_depth=0)
            if task['status'] == 'scheduled' and task.get('_self_recovery'):
                task.update(status='queued', scheduled_at=None)
                task['_self_recovery']['attempt'] = 0
            if task['status'] == 'paused' and not self._task_paused(task):
                task['status'] = 'queued'
            self._put('tasks', task)
            self._event('task.updated', self._public(task), bot['id'], task['id'])
            return self._public(task) | {'reply_status': 'queued'}
        if task['status'] == 'needs_you':
            raise ServiceError(409, 'Refresh this thread before replying.')
        work_context = self._native_continuation_item(task) if task.get('_native_outcome_id') else None
        if work_context:
            result = self._send(bot, dict(text=text, mode='queue', attachment_ids=attachments),
                conversation_bot_id=task['conversation_bot_id'], work_item=work_context)
        else:
            result = self._send(bot, dict(text=text, mode='queue', attachment_ids=attachments), thread_id=task['thread_id'], parent_task_id=task['id'], conversation_bot_id=task['conversation_bot_id'])
        return result | {'reply_status': 'queued_continuation'}

    def _record_attachments(self, bot_id: str, task: dict, artifact_ids: list[str], *, recipient_bot_ids: list[str] | None = None):
        task['_attachment_ids'] = list(dict.fromkeys(task.get('_attachment_ids', []) + artifact_ids))
        for artifact_id in artifact_ids:
            artifact = self._get('artifacts', artifact_id)
            if artifact['task_id'] is None:
                artifact['task_id'] = task['id']
                self._put('artifacts', artifact)
                self._event('artifact.updated', self._public(artifact), bot_id, task['id'])
            self._message(bot_id, artifact['title'], task_id=task['id'], role='user', kind='artifact', artifact_id=artifact_id, recipient_bot_ids=recipient_bot_ids)

    def _cancel(self, task: dict, callbacks: list):
        exchange = self._get('exchanges', task['exchange_id'])
        exchange['cancelled'] = True
        self._put('exchanges', exchange)
        related = {t['id'] for t in self._all('tasks') if t.get('exchange_id') == task.get('exchange_id')} | {task['id']}
        while True:
            expanded = related | {t['id'] for t in self._all('tasks') if t.get('source_task_id') in related}
            if expanded == related:
                break
            related = expanded
        self._cancel_one(task, callbacks)
        for task_id in related - {task['id']}:
            self._cancel_one(self._get('tasks', task_id), callbacks)

    def _cancel_one(self, task: dict, callbacks: list, *, include_finished=False,
                    summary='Cancelled. An action already executing may have finished.'):
        if task['status'] == 'cancelled' or (task['status'] not in self.ACTIVE and not include_finished):
            return
        task.pop('_return_pending', None)
        task.update(status='cancelled', summary=summary, updated_at=now())
        self._put('tasks', task)
        for decision in self._all('decisions'):
            owner = decision.get('_execution_task_id', decision['task_id'])
            if owner == task['id'] and decision['status'] in ('pending', 'approved'):
                decision.update(status='rejected', decided_at=now(), decided_by='cancellation', decided_by_name='Task cancellation', outcome='Task cancelled.')
                self._put('decisions', decision)
                self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        for context in self._all('contexts'):
            if context['task_id'] == task['id'] and context['status'] in ('queued', 'working'):
                context['status'] = 'cancelled'
                self._put('contexts', context)
                self._event('context.cancelled', {'id': context['id'], 'decision_id': context['decision_id']}, task['bot_id'], task['id'])
        self._event('task.cancelled', self._public(task), task['bot_id'], task['id'])
        self._message(task['bot_id'], task['summary'], task_id=task['id'], role='system', kind='status')
        self._refresh_bot(task['bot_id'])
        callbacks.append(lambda: self.provider.cancel_task(task['id']))
        callbacks.append(lambda: self.gateway_bridge.cancel(task['id']))

    def _validate_action(self, decision: dict):
        action = decision.get('_action', {})
        if action.get('type') != 'create_file' or decision.get('action_type') != action.get('type'):
            raise ServiceError(409, 'This action is unavailable in v1. Ask the bot to prepare a local file.')
        filename = action.get('filename', '')
        if not isinstance(filename, str) or not filename or filename in ('.', '..') or Path(filename).name != filename or len(filename) > 160:
            raise ServiceError(400, 'The proposed file name is invalid.')
        content = action.get('content')
        if not isinstance(content, str) or len(content.encode()) > 4 * 1024 * 1024:
            raise ServiceError(400, 'The proposed file must contain at most 4 MB of text.')
        if digest(encoded(action)) != decision.get('_action_hash'):
            raise ServiceError(409, 'The proposed action changed. Request a new decision.')
        if self.files.is_symlink() or self.files.resolve().parent != self.root:
            raise ServiceError(409, 'The artifact directory changed. Check the Mac before approval.')

    def _decision_options(self, payload: dict, decision: dict) -> list[dict]:
        raw_options = payload.get('options', [])
        if not isinstance(raw_options, list) or len(raw_options) > 8:
            raise ServiceError(400, 'A decision can contain at most eight choices.')
        options = []
        seen = set()
        for raw in raw_options:
            if not isinstance(raw, dict):
                raise ServiceError(400, 'Each choice must contain an ID, label, explanation, and answer.')
            option_id = require_text(raw, 'id', 80)
            if option_id in seen:
                raise ServiceError(400, 'Each choice must have a different ID.')
            seen.add(option_id)
            answer = raw.get('answer')
            if answer not in ('approve', 'reject', 'modify'):
                raise ServiceError(400, 'Each choice must approve, reject, or request a modification.')
            option = dict(id=option_id, label=require_text(raw, 'label', 120), explanation=require_text(raw, 'explanation', 2000), answer=answer)
            if answer == 'approve':
                action = raw.get('action', decision['_action'])
                if not isinstance(action, dict) or set(action) != {'type', 'filename', 'content'}:
                    raise ServiceError(400, 'An approval choice must contain an exact file action.')
                self._validate_action(decision | {'_action': action, '_action_hash': digest(encoded(action))})
                option['action'] = action
            elif raw.get('action') is not None:
                raise ServiceError(400, 'Only an approval choice can contain an action.')
            if answer == 'modify':
                option['modification'] = require_text(raw, 'modification', 10000)
            elif raw.get('modification') is not None:
                raise ServiceError(400, 'Only a modification choice can contain correction text.')
            options.append(option)
        return options

    def _select_option(self, decision: dict, option_id: str) -> dict:
        options = decision.get('options', [])
        if digest(encoded(options)) != decision.get('_options_hash'):
            raise ServiceError(409, 'The decision choices changed. Request a new proposal.')
        option = next((option for option in options if option['id'] == option_id), None)
        if not option:
            raise ServiceError(400, 'Choose an option from this decision.')
        if option['answer'] == 'approve':
            action = option['action']
            decision.update(_action=action, _action_hash=digest(encoded(action)), proposal_preview=action['content'], proposal_hash=digest(encoded(action)), resource=action['filename'])
            self._validate_action(decision)
        decision.update(selected_option_id=option['id'], selected_option_label=option['label'])
        return option

    def _actor_name(self, actor_id: str) -> str:
        labels = {'workflow_permission': 'Workflow permission', 'cancellation': 'Task cancellation', 'local': 'This Mac'}
        if actor_id in labels:
            return labels[actor_id]
        device = self.db.execute('SELECT name FROM devices WHERE id=?', (actor_id,)).fetchone()
        return device[0].strip() if device and device[0].strip() else 'Paired device'

    def _record_decision_correction(self, task, decision, text, human_authored=True):
        message = self._message(task['bot_id'], text, task_id=task['id'], role='user',
                                decision_id=decision['id'], human_authored=human_authored)
        if human_authored and decision.get('inbox_kind') != 'proposal':
            task['_direct_reply_message_id'] = message['id']
        if message.get('_human_device_id'):
            task.setdefault('_preference_source_ids', []).append(message['id'])
            task.setdefault('_turn_preference_source_ids', []).append(message['id'])
        return message['id']

    def _answer(self, decision: dict, task: dict, data: dict, device: str) -> dict:
        if decision.get('_gateway_question'):
            # The answer or a reply goes to the waiting gateway turn. A dismissal goes there too (dismiss_gateway_question).
            return self.answer_gateway_question(decision, task, data, device)
        if decision.get('_source_recheck'):
            raise ServiceError(409, 'The source changed. Coconut is updating this decision.')
        if not isinstance(data.get('revision'), int) or isinstance(data['revision'], bool) or data['revision'] != decision['revision']:
            raise ServiceError(409, 'This decision changed. Refresh it before answering.')
        if data.get('option') is not None:
            raise ServiceError(400, 'Send the choice ID as option_id.')
        choice = data.get('choice')
        correction = decision_correction(data) or None
        option = None
        option_id = data.get('option_id')
        if option_id is not None:
            if not isinstance(option_id, str):
                raise ServiceError(400, 'option_id must contain a choice ID.')
            option = self._select_option(decision, option_id)
            if choice is not None and choice != option['answer']:
                raise ServiceError(400, 'The answer does not match the selected choice.')
            choice = option['answer']
            if choice == 'modify':
                correction = correction or option['modification']
        elif choice == 'approve' and decision.get('options'):
            raise ServiceError(400, 'Select a file choice before approval.')
        if choice not in ('approve', 'reject', 'modify', 'reply'):
            raise ServiceError(400, 'Choose approve, reject, modify, or a decision option.')
        if choice in ('modify', 'reply') and not correction:
            raise ServiceError(400, 'Describe the change before modifying this decision.')
        if choice == 'approve':
            if self._task_paused(task):
                raise ServiceError(409, 'Restore the bot before approving this action.')
            self._validate_action(decision)
        actor_name = self._actor_name(device)
        response_record = dict(choice=choice, option_id=option_id, option_label=option['label'] if option else None, correction=correction, decided_by=device, decided_by_name=actor_name, decided_at=now(), prediction=decision.get('_prediction'), predicted_option_id=decision.get('_predicted_option_id'), revision=decision['revision'], action_hash=decision['_action_hash'], original_action_hash=digest(encoded(decision.get('_proposal_action', decision['_action']))))
        decision.setdefault('_answers', []).append(response_record)
        decision.update(decided_at=response_record['decided_at'], decided_by=device, decided_by_name=actor_name, correction=correction, selected_option_id=option_id, selected_option_label=option['label'] if option else None)
        if choice in ('modify', 'reply'):
            decision.update(status='replied' if choice == 'reply' else 'modified',
                            outcome='Reply received. The bot will continue.' if choice == 'reply' else 'Changes requested. The bot will prepare a new decision.')
            task.update(status='queued', _resume_decision=decision['id'], updated_at=now())
            self._record_decision_correction(task, decision, correction, human_authored=bool(data.get('correction')))
        elif choice == 'reject':
            decision.update(status='rejected', outcome='Rejected. No file was created.')
            if task.get('_steering'):
                task.update(status='queued', summary='Applying your follow-up at the next safe turn.', updated_at=now())
                self._advance_input(task)
            else:
                task.update(status='completed', summary='The proposed action was rejected.', updated_at=now())
                self._finish_assignment_turn(task, 'The proposed action was rejected. No file was created.', 'rejected')
        else:
            decision.update(status='approved', outcome='Approved. The service will recheck the selected file before execution.')
            task.update(status='queued', _resume_decision=decision['id'], updated_at=now())
        self._put('decisions', decision)
        self._put('tasks', task)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        self._event('decision.answered', response_record, task['bot_id'], task['id'])
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
        self._refresh_bot(task['bot_id'])
        return self._public(decision)

    def _attachment(self, data: dict) -> dict:
        bot = self._get('bots', require_text(data, 'bot_id', 100))
        filename = require_text(data, 'filename', 160)
        if Path(filename).name != filename or filename in ('.', '..'):
            raise ServiceError(400, 'The attachment file name is invalid.')
        try:
            content = base64.b64decode(data.get('content_base64', ''), validate=True)
        except (ValueError, TypeError):
            raise ServiceError(400, 'The attachment must use base64 encoding.')
        if not content or len(content) > 4 * 1024 * 1024:
            raise ServiceError(413, 'Attachments must contain between 1 byte and 4 MB.')
        artifact_id = uid()
        # Store uploaded bytes in SQLite with the request, avoiding orphan files on retries.
        artifact = dict(id=artifact_id, bot_id=bot['id'], task_id=None, title=filename, kind='file', status='attached', preview=f'{len(content):,} bytes', url=None, created_at=now(), _attachment=True)
        self._put('artifacts', artifact)
        self.store_artifact_content(artifact_id, content)
        self._event('artifact.created', self._public(artifact), bot['id'])
        return self._public(artifact)

    def store_artifact_content(self, artifact_id: str, content: bytes):
        """Keep the bytes of an attachment or a preview in their own table. The artifact rows stay small.

        Every pass that reads a whole table decodes the rows, and the bytes made up 48 of the 51 MB of the artifacts.
        """
        self.db.execute('INSERT OR REPLACE INTO artifact_content(id, content) VALUES (?, ?)', (artifact_id, content))

    def artifact_bytes(self, artifact: dict) -> bytes | None:
        """Return the stored bytes of an artifact, or None when it has none. A row from before the move keeps them inline."""
        row = self.db.execute('SELECT content FROM artifact_content WHERE id=?', (artifact['id'],)).fetchone()
        if row:
            return bytes(row[0])
        try:
            return base64.b64decode(artifact['_content_base64']) if artifact.get('_content_base64') else None
        except ValueError:
            # A damaged value is not the same as no bytes. Say so, so that no turn goes on without the attachment.
            raise ServiceError(409, f'The stored bytes of the attachment {artifact["title"]} are damaged.') from None

    def turn_attachment_content(self, artifact_id: str) -> dict:
        """Return the content of an attachment for a turn: its bytes as base64, or the reason that it has none.

        A damaged inline value must not stop the turn, and a decision context that fails while it assembles its
        attachments would retry without end. The turn gets the reason in place of the preview, so the bot knows.
        """
        artifact = self._get('artifacts', artifact_id)
        try:
            content = self.artifact_bytes(artifact)
        except ServiceError as error:
            return {'content_base64': None, 'preview': str(error)}
        return {'content_base64': base64.b64encode(content).decode() if content is not None else None}

    def _move_artifact_content(self):
        """Move the inline bytes of older artifacts into artifact_content. Run at every start; it finds nothing after the first."""
        # Read the ids first and one value at a time, so the peak memory is one attachment, not all of them.
        # The caller holds the startup transaction.
        ids = [row[0] for row in self.db.execute("SELECT id FROM artifacts WHERE json_type(data,'$._content_base64')='text'")]
        for artifact_id in ids:
            encoded = self.db.execute("SELECT json_extract(data,'$._content_base64') FROM artifacts WHERE id=?", (artifact_id,)).fetchone()[0]
            try:
                content = base64.b64decode(encoded)
            except ValueError:
                # One damaged value must not stop the service. Its row keeps the inline value, and only its download fails.
                logging.getLogger(__name__).warning('Coconut left the inline bytes of artifact %s: they are not valid base64.', artifact_id)
                continue
            self.store_artifact_content(artifact_id, content)
            self.db.execute("UPDATE artifacts SET data=json_remove(data,'$._content_base64') WHERE id=?", (artifact_id,))

    def artifact_content(self, artifact_id: str) -> tuple[bytes, str]:
        with self.lock:
            artifact = self._get('artifacts', artifact_id)
            bot = self._get('bots', artifact['bot_id'])
            if bot.get('_deleted'):
                raise ServiceError(404, 'This artifact was removed.')
            stored = self.artifact_bytes(artifact)
            if stored is not None:
                return stored, artifact['title']
            path = self.files / artifact_id
            if path.is_symlink() or not path.is_file():
                raise ServiceError(404, 'The artifact file is unavailable on the Mac.')
            content = path.read_bytes()
            if digest(content) != artifact.get('_content_hash'):
                raise ServiceError(409, 'This artifact changed on the Mac.')
            return content, artifact['title']

    def push_status(self, device: str) -> dict:
        with self.lock:
            return self.push.status(device)

    def learning(self) -> dict:
        groups: dict[tuple, dict] = {}
        # A read view reads every decision without the service lock.
        with self._read_view() as view, view._read_transaction():
            for decision in view._all('decisions'):
                task = view._get('tasks', decision['task_id'])
                key = (task['bot_id'], decision['action_type'])
                for answer in decision.get('_answers', []):
                    prediction = answer.get('prediction')
                    predicted_option = answer.get('predicted_option_id')
                    if decision.get('options'):
                        if not predicted_option:
                            continue
                        agrees = answer.get('option_id') == predicted_option and not answer['correction']
                    else:
                        if prediction not in ('approve', 'reject'):
                            continue
                        agrees = answer['choice'] == prediction and not answer['correction']
                    group = groups.setdefault(key, dict(bot_id=key[0], action_type=key[1], total=0, agreed=0))
                    group['total'] += 1
                    group['agreed'] += int(agrees)
            for group in groups.values():
                group['agreement'] = group['agreed'] / group['total']
            return {'groups': list(groups.values())}

    def start(self):
        if self.dispatcher or getattr(self, '_workspace_recovery_hold', False):
            return
        self.dispatcher = threading.Thread(target=self._dispatch, name='coconut-dispatch', daemon=True)
        self.dispatcher.start()
        if self.kanban is not None:
            self.chat_dispatcher = threading.Thread(target=self._dispatch_conversations, name='coconut-chat-dispatch', daemon=True)
            self.chat_dispatcher.start()
        self.pr_poller = threading.Thread(target=self._poll_pull_requests, name='coconut-pr-poll', daemon=True)
        self.pr_poller.start()
        self.process_poller = threading.Thread(target=self._poll_processes, name='coconut-process-poll', daemon=True)
        self.process_poller.start()
        # Tool checks scan every process with ps and lsof, so they run apart from result delivery.
        self.tool_checker = threading.Thread(target=self._check_ended_tool_calls, name='coconut-tool-check', daemon=True)
        self.tool_checker.start()
        self.source_poller = threading.Thread(target=self._poll_work_sources, name='coconut-source-poll', daemon=True)
        self.source_poller.start()
        self._start_profile_watcher()
        self._start_hermes_entries()
        self._start_gateway_supervisor()
        self.push.start()

    def _register_pr_artifact(self, task_id: str | None, payload: dict, *, bot_id=None):
        try:
            owner, repo, number = parse_pr_url(payload.get('url'))
        except PRTrackingError as error:
            raise ServiceError(400, str(error)) from error
        url = f'https://github.com/{owner}/{repo}/pull/{number}'
        with self.lock, self._shared_reads():
            task = self._get('tasks', task_id) if task_id else dict(bot_id=bot_id, status='working')
            bot = self._get('bots', task['bot_id'])
            if bot.get('_deleted'):
                raise ServiceError(404, 'This bot was deleted.')
            if task['status'] != 'working' or self.stopping.is_set():
                raise YieldForDecision('This task no longer accepts provider output.')
            review_task = self._pr_review_source(bot['id'], task_id or payload.get('review_task_id'))
            existing = self._bot_pr_artifact(bot['id'], url)
            if existing and existing.get('pr_state') not in ('closed', 'merged') and existing['status'] != 'merged':
                self._bind_pr_review(existing, review_task, transfer=task_id is None and bool(payload.get('review_task_id')))
                with self.transaction():
                    self._set_work_activity(existing, task, payload)
                self.pr_refresh_requested.set()
                return self._public(existing)
        # GitHub reads must not hold the service lock or delay other bots.
        observation = self._pr_tracker_for(task['bot_id']).observe(url)
        if not observation.observed:
            raise ServiceError(502, observation.detail)
        with self.transaction(), self._shared_reads():
            task = self._get('tasks', task_id) if task_id else dict(bot_id=bot_id, status='working')
            bot = self._get('bots', task['bot_id'])
            if bot.get('_deleted'):
                raise ServiceError(404, 'This bot was deleted.')
            if task['status'] != 'working' or self.stopping.is_set():
                raise YieldForDecision('This task no longer accepts provider output.')
            review_task = self._pr_review_source(bot['id'], task_id or payload.get('review_task_id'))
            existing = self._bot_pr_artifact(bot['id'], url)
            if existing:
                existing.update(observation.fields(), title=(observation.title or existing['title'])[:200],
                                preview=observation.detail, checked_at=observation.checked_at,
                                observed_at=observation.checked_at, refresh_error=None)
                self._bind_pr_review(existing, review_task, transfer=task_id is None and bool(payload.get('review_task_id')))
                self._set_work_activity(existing, task, payload)
                self._put('artifacts', existing)
                self._refresh_merge_diff(existing)
                self._event('artifact.updated', self._public(existing), existing['bot_id'], existing['task_id'])
                self.pr_refresh_requested.set()
                return self._public(existing)
            artifact = dict(observation.fields(), id=uid(), bot_id=task['bot_id'], task_id=task_id, title=(observation.title or 'Pull request')[:200], kind='pr', preview=observation.detail, url=url, created_at=now(), checked_at=observation.checked_at, observed_at=observation.checked_at, refresh_error=None)
            # The list of owners starts with the first one, so this record knows every owner it will have.
            artifact['_review_owners'] = []
            if review_task:
                self._set_review_owner(artifact, review_task['id'])
            self._set_work_activity(artifact, task, payload)
            self._put('artifacts', artifact)
            if task_id:
                self._message(task['bot_id'], artifact['title'], task_id=task_id, kind='artifact', artifact_id=artifact['id'])
            self._event('artifact.created', artifact, task['bot_id'], task_id)
            self._refresh_bot(task['bot_id'])
            self.pr_refresh_requested.set()
            return self._public(artifact)

    def _pr_tracker_for(self, bot_id):
        if self.pr_tracker is not None:
            return self.pr_tracker
        with self.lock:
            home = self.profiles.home(self._get('bots', bot_id))
            # Remove expired trackers, so their tokens do not stay in memory.
            for key in [key for key, value in self._profile_trackers.items() if value[0] <= time.monotonic()]:
                del self._profile_trackers[key]
            cached = self._profile_trackers.get(home)
        # Each gh login read starts a process. Keep the tracker for a short time, so each turn does not wait for gh.
        # A tracker without a login expires sooner, so a new gh login takes effect soon.
        if cached and cached[0] > time.monotonic():
            return cached[1]
        tracker = GitHubPRTracker.for_profile(home, review_check_names=self._setting('review_check_names', REVIEW_CHECK_NAMES))
        with self.lock:
            self._profile_trackers[home] = (time.monotonic() + (120 if tracker._token else 30), tracker)
        return tracker

    def _poll_pull_requests(self):
        while not self.stopping.is_set():
            try:
                self.refresh_pull_requests()
            except Exception:
                logging.getLogger(__name__).exception("PR monitoring failed; the next refresh will retry.")
            self.pr_refresh_requested.wait(60)
            self.pr_refresh_requested.clear()

    def refresh_pull_requests(self):
        if not self.pr_refresh_lock.acquire(blocking=False):
            return
        try:
            self._refresh_pull_requests()
            self._resolve_merged_pr_decisions()
            self._queue_pr_reviews()
        finally:
            self.pr_refresh_lock.release()

    def _refresh_pull_requests(self):
        with self.lock:
            if self.stopping.is_set():
                return
            artifacts = [a for a in (json.loads(row[0]) for row in self.db.execute(PR_ARTIFACTS))
                         if a['status'] != 'merged' and a.get('pr_state') not in ('closed', 'merged') and not self._get('bots', a['bot_id']).get('_deleted')]
        from .decision_sources import github_links, github_source
        with self.lock:
            decisions = [d for d in (json.loads(row[0]) for row in self.db.execute(PENDING_CARDS))
                         if not d.get('_hidden') and not d.get('_deleted')]
        observations, grouped = {}, {}
        for artifact in artifacts:
            key = (artifact['bot_id'], artifact['url'].lower())
            grouped.setdefault(key, []).append(artifact)
        jobs, source_jobs, results = Queue(), Queue(), Queue()
        finished = threading.Event()
        source_reads, source_lock = {}, threading.Lock()
        def read_source(key, load):
            with source_lock:
                first = key not in source_reads
                future = source_reads.setdefault(key, Future())
            if first:
                try:
                    future.set_result(load())
                except Exception as error:
                    future.set_exception(error)
            return future.result()

        for key, items in grouped.items():
            jobs.put((key, items[0]['url']))

        def observe():
            while not self.stopping.is_set() and not finished.is_set():
                try:
                    key, url = jobs.get_nowait()
                except Empty:
                    return
                try:
                    tracker = self._pr_tracker_for(key[0])
                    if self.stopping.is_set():
                        return
                    result = tracker.observe(url)
                except Exception as error:
                    result = error
                results.put(('pr', key, result))

        def refresh_sources():
            while not self.stopping.is_set() and not finished.is_set():
                try:
                    identity, known = source_jobs.get(timeout=.2)
                except Empty:
                    continue
                try:
                    result = self._refresh_decision_sources(known, decision_ids={identity},
                        read_cache=read_source, cancelled=finished)
                except Exception as error:
                    result = error
                results.put(('source', identity, result))

        remaining, pending_sources, scheduled = len(grouped), 0, set()
        ready = {}
        def schedule_sources():
            nonlocal pending_sources
            with self.lock:
                self._pending_decision_sources.clear()
                # This runs every 0.2 s while the poll waits. Read only the pending cards.
                for decision in (json.loads(row[0]) for row in self.db.execute(PENDING_CARDS)):
                    if (decision['status'] != 'pending' or decision.get('_hidden') or decision.get('_deleted')
                            or self._get('bots', decision['bot_id']).get('_deleted')):
                        continue
                    links = github_links(decision)
                    if ready.get(decision['id']) == (decision['revision'], links):
                        continue
                    self._pending_decision_sources.add(decision['id'])
                    if decision['id'] in scheduled:
                        continue
                    keys = {(decision['bot_id'], url.lower()) for url in links
                            if github_source(url)[0] == 'pulls'}
                    # A card receives all its linked PR facts together, including discussion evidence.
                    if not keys.intersection(grouped).issubset(observations):
                        continue
                    scheduled.add(decision['id'])
                    pending_sources += 1
                    source_jobs.put((decision['id'], dict(observations)))

        # Fence global scheduling before any observation can change card evidence.
        with self.lock:
            self._pending_decision_sources.update(d['id'] for d in decisions)
            self._ready_decision_sources = ready
        try:
            # Active HTTP reads retain their timeout and never delay process shutdown.
            for _ in range(min(4, len(grouped))):
                threading.Thread(target=observe, name='pr-observation', daemon=True).start()
            # Workers also serve cards created after an empty decision snapshot.
            for _ in range(2):
                threading.Thread(target=refresh_sources, name='decision-observation', daemon=True).start()
            while not self.stopping.is_set():
                # Revisit live identities even when every outstanding HTTP read is blocked.
                schedule_sources()
                if not remaining and not pending_sources:
                    break
                try:
                    kind, key, observation = results.get(timeout=.2)
                except Empty:
                    continue
                if isinstance(observation, Exception):
                    raise observation
                if kind == 'source':
                    pending_sources -= 1
                    scheduled.remove(key)
                    with self.lock:
                        # Admission and rescheduling both compare these identities with live cards.
                        ready.update(observation)
                    schedule_sources()
                    self._queue_decision_refreshes()
                    continue
                remaining -= 1
                observations[key] = observation
                for artifact in grouped[key]:
                    with self.lock:
                        if self.stopping.is_set():
                            return
                        with self.transaction():
                            current = self._get('artifacts', artifact['id'])
                            if current['url'] != artifact['url'] or self._get('bots', current['bot_id']).get('_deleted'):
                                continue
                            before = self._public(current)
                            current['checked_at'] = observation.checked_at
                            if observation.observed:
                                self._invalidate_terminal_pr_cards(current['bot_id'], observation)
                                if current.get('pr_state') == 'open' and observation.pr_state in ('merged', 'closed'):
                                    current['_terminal_review_pending'] = True
                                current.update(observation.fields(), title=(observation.title or current['title'])[:200], preview=observation.detail, observed_at=observation.checked_at, refresh_error=None)
                                if before.get('diff') != current.get('diff'):
                                    self._refresh_merge_diff(current)
                            else:
                                current.update(refresh_error=observation.detail, preview=observation.detail)
                            self._restore_active_work(current)
                            self._put('artifacts', current)
                            public = self._public(current)
                            # Each poll moves the check times, and the phone marks a PR stale after 120 s without one.
                            # A change of only those times publishes artifact.observed, which updates only this card.
                            quiet = {key for key in before.keys() | public.keys()
                                     if before.get(key) != public.get(key)} <= {'checked_at', 'observed_at'}
                            self._event('artifact.observed' if quiet else 'artifact.updated', public, current['bot_id'], current['task_id'])
                self._resolve_merged_pr_decisions()
                self._queue_pr_reviews(artifact_ids={item['id'] for item in grouped[key]})
        finally:
            with self.lock:
                # Invalidate active writes before releasing their admission fences.
                finished.set()
                self._pending_decision_sources.clear()
                self._ready_decision_sources = None

    def _dispatch(self):
        next_routine_tick = 0
        next_recovery_tick = 0
        next_decision_tick = 0
        while not self.stopping.is_set():
            if self.restart_pending:
                try:
                    self._dispatch_pending()
                except Exception:
                    logging.getLogger(__name__).exception('Queue dispatch failed; queued work will retry.')
                    self.stopping.wait(1)
                else:
                    self.stopping.wait(.2)
                continue
            if time.monotonic() >= next_routine_tick:
                next_routine_tick = time.monotonic() + 1
                try:
                    self.tick_routines()
                except Exception:
                    logging.getLogger(__name__).exception('Queue maintenance failed: tick_routines')
            if time.monotonic() >= next_recovery_tick:
                next_recovery_tick = time.monotonic() + 5
                try:
                    self._queue_context_recoveries()
                except Exception:
                    logging.getLogger(__name__).exception('Work recovery failed; the next scan will retry.')
            if time.monotonic() >= next_decision_tick:
                next_decision_tick = time.monotonic() + 60
                try:
                    self._queue_decision_refreshes()
                except Exception:
                    logging.getLogger(__name__).exception('Decision refresh failed; the next scan will retry.')
            try:
                self._dispatch_pending()
            except Exception:
                logging.getLogger(__name__).exception('Queue dispatch failed; queued work will retry.')
                self.stopping.wait(1)

    def _dispatch_foreground(self):
        # Caller holds the service lock. Native admission reserves background slots.
        if (self.restart_pending or self.stopping.is_set() or self._dependency_unlinking
                or getattr(self, '_workspace_recovery_hold', False)):
            return
        self.running = {k: v for k, v in self.running.items() if v.is_alive()}
        self.context_threads = {k: v for k, v in self.context_threads.items() if v.is_alive()}
        self._dispatch_status_request()
        self._dispatch_chat(capacity_state(self._setting('work_capacity'))['effective_limit'])

    def _dispatch_conversations(self):
        while not self.stopping.is_set():
            try:
                with self.changed:
                    self._dispatch_foreground()
                    # Transactions wake this condition. The timeout admits scheduled messages.
                    self.changed.wait(1)
            except Exception:
                logging.getLogger(__name__).exception('Conversation dispatch failed; saved messages will retry.')
                self.stopping.wait(1)

    def _dispatch_pending(self):
        self.poll_service_drain()
        self.poll_hermes_change()
        with self.dispatch_gate:
            if getattr(self, '_workspace_recovery_hold', False):
                return
            if self.kanban is None:
                return self._dispatch_pending_locked()
            with self.lock:
                self._dispatch_foreground()
                self._kanban_dispatching = True
            result = self._dispatch_pending_locked()
        # The sole queue-dispatch thread keeps its foreground reservation through idle.
        # Claims and their reconciliation are finished. Slow idle-worker cleanup
        # and condition waits must not exclude an otherwise valid dependency unlink.
        self._idle_kanban()
        # Retain the reservation after an uncertain native response until reconciliation succeeds.
        with self.lock:
            self._kanban_dispatching = False
            self.changed.notify_all()
        return result

    def _dispatch_pending_locked(self):
        if self.kanban is not None:
            return self._dispatch_kanban()
        self.finish_inbox_swipes()
        self._drain_returns()
        with self.changed:
            if self.restart_pending:
                self.changed.wait(timeout=.2)
                return
            self._cancel_stale_decision_refreshes()
            self.running = {k: v for k, v in self.running.items() if v.is_alive()}
            self.context_threads = {k: v for k, v in self.context_threads.items() if v.is_alive()}
            capacity = capacity_state(self._setting('work_capacity'))['effective_limit']
            active = {execution_key(self._get('tasks', identity)) for identity in self.running}
            active.update(execution_key(self._get('tasks', self._get('contexts', identity)['task_id']))
                          for identity in self.context_threads)
            status_running = sum(bool(self._get('tasks', identity).get('_work_status_only')) for identity in self.running)
            slots = len(self.running) - status_running + len(self.context_threads)
            refresh_active = any(self._get('contexts', identity).get('_decision_refresh') for identity in self.context_threads)
            for context in self._all('contexts'):
                task = self._get('tasks', context['task_id'])
                if context.get('_decision_refresh') and refresh_active:
                    continue
                if (context['status'] != 'queued' or context['id'] in self.context_threads or self._task_paused(task)
                        or self._store_held(task)):
                    continue
                self._ensure_execution_session(task)
                key = execution_key(task)
                if slots >= capacity or key in active:
                    continue
                self._start_context(context['id'])
                refresh_active = refresh_active or bool(context.get('_decision_refresh'))
                active.add(key)
                slots += 1
            for task in self._dispatch_order(self._all('tasks')):
                bot = self._get('bots', task['bot_id'])
                if bot['archived'] or bot.get('_deleted') or task['id'] in self.running:
                    continue
                if not self._dispatch_eligible(task, time.time()):
                    continue
                self._ensure_execution_session(task)
                key = execution_key(task)
                status_only = task.get('_work_status_only')
                if key in active or (status_running >= 1 if status_only else slots >= capacity):
                    continue
                worker = threading.Thread(target=self._run_task, args=(task['id'],), daemon=True, name=f'task-{task["id"][:8]}')
                self.running[task['id']] = worker
                active.add(key)
                if status_only:
                    status_running += 1
                else:
                    slots += 1
                worker.start()
            self.changed.wait(.2)

    def _ensure_execution_session(self, task):
        if task.get('_execution_session_id') or task.get('_chat_session'):
            return
        owner = self._assignment(task)
        if task.get('_work_context_id') and not task.get('_work_status_only'):
            artifact = self._get('artifacts', task['_work_context_id'])
            identity = artifact.get('task_id')
            if artifact['kind'] == 'pr':
                peers = [a for a in self._all('artifacts') if a['kind'] == 'pr'
                         and a.get('url', '').lower() == artifact['url'].lower()]
                review = self._pr_review_owner(peers)
                if review and review['bot_id'] == task['bot_id']:
                    identity = review['_review_task_id']
            if identity:
                candidate = self._get('tasks', identity)
                if candidate['bot_id'] == task['bot_id']:
                    owner = self._assignment(candidate)
        task['_execution_session_id'] = owner.get('_execution_session_id') or owner['id']
        self._put('tasks', task)

    def _dispatch_eligible(self, task, timestamp):
        if task['status'] not in ('queued', 'scheduled'):
            return False
        if task.get('scheduled_at') and datetime.fromisoformat(task['scheduled_at'].replace('Z', '+00:00')).timestamp() > timestamp:
            return False
        return (not self._task_paused(task) and not self._store_held(task) and not self._continuation_blocked(task)
                and not self._automatic_work_is_paused(task) and not self.automatic_work_held(task))

    def _store_held(self, task):
        return task['bot_id'] in self._store_hold

    def _dispatch_order(self, tasks):
        # Consume results and decisions before starting more work.
        by_id = {task['id']: task for task in tasks}
        def priority(task):
            if task.get('_is_return') or task.get('_background_completion') or task.get('_resume_decision'):
                return 0
            return 2 if by_id.get(task.get('_assignment_task_id'), task).get('_source_audit_prompt') else 1
        return sorted(tasks, key=priority)


    def _index_message_audience(self):
        # Membership changes commit with the message, including direct imports and deletions.
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS message_audience (
            bot_id TEXT NOT NULL, message_id TEXT NOT NULL, PRIMARY KEY (bot_id, message_id));
        CREATE INDEX IF NOT EXISTS message_audience_message ON message_audience (message_id);
        CREATE TRIGGER IF NOT EXISTS message_audience_insert AFTER INSERT ON messages BEGIN
            DELETE FROM message_audience WHERE message_id=NEW.id;
            INSERT OR IGNORE INTO message_audience
                SELECT json_extract(NEW.data, '$.sender_bot_id'), NEW.id
                WHERE json_type(NEW.data, '$.sender_bot_id')='text';
            INSERT OR IGNORE INTO message_audience
                SELECT value, NEW.id FROM json_each(NEW.data, '$.recipient_bot_ids') WHERE type='text';
        END;
        CREATE TRIGGER IF NOT EXISTS message_audience_update AFTER UPDATE OF data ON messages
        WHEN json_extract(OLD.data, '$.sender_bot_id') IS NOT json_extract(NEW.data, '$.sender_bot_id')
          OR json_extract(OLD.data, '$.recipient_bot_ids') IS NOT json_extract(NEW.data, '$.recipient_bot_ids')
        BEGIN
            DELETE FROM message_audience WHERE message_id=OLD.id;
            INSERT OR IGNORE INTO message_audience
                SELECT json_extract(NEW.data, '$.sender_bot_id'), NEW.id
                WHERE json_type(NEW.data, '$.sender_bot_id')='text';
            INSERT OR IGNORE INTO message_audience
                SELECT value, NEW.id FROM json_each(NEW.data, '$.recipient_bot_ids') WHERE type='text';
        END;
        CREATE TRIGGER IF NOT EXISTS message_audience_delete AFTER DELETE ON messages BEGIN
            DELETE FROM message_audience WHERE message_id=OLD.id;
        END;
        """)
        with self.transaction():
            if self._setting('message_audience_indexed'):
                return
            self.db.execute("INSERT OR IGNORE INTO message_audience "
                            "SELECT json_extract(data, '$.sender_bot_id'), id FROM messages "
                            "WHERE json_type(data, '$.sender_bot_id')='text'")
            self.db.execute("INSERT OR IGNORE INTO message_audience "
                            "SELECT j.value, m.id FROM messages m, json_each(m.data, '$.recipient_bot_ids') j "
                            "WHERE j.type='text'")
            self.db.execute("INSERT INTO settings VALUES ('message_audience_indexed','true')")

    def _history(self, bot_id: str, *, task_id=None, shared_message_ids=()) -> list[dict]:
        # Threads organize Inbox decisions. They never restrict a bot's own recall.
        selection, values = '', [bot_id]
        if task_id is not None:
            selection = ("AND m.id IN (SELECT id FROM messages WHERE json_extract(data, '$.task_id')=? "
                         "UNION SELECT value FROM json_each(?)) ")
            values.extend((task_id, json.dumps(shared_message_ids)))
        row = self.db.execute("SELECT json_group_array(json(data)) FROM (SELECT m.data FROM "
            "message_audience a JOIN messages m ON m.id=a.message_id WHERE a.bot_id=? "
            "AND COALESCE(json_extract(m.data, '$._hidden'),0)=0 "
            "AND COALESCE(json_extract(m.data, '$._deleted'),0)=0 " + selection + "ORDER BY m.rowid)", values).fetchone()
        return [self._public(message) for message in json.loads(row[0])]

    def _execution_task_ids(self, task):
        """Return the IDs of the tasks of the task's execution that already had a turn, and the task itself.

        A reseed of the work session keeps their messages. A queued request waits for its own turn boundary.
        """
        with self.lock:
            return [row[0] for row in self.db.execute(
                "SELECT id FROM tasks WHERE json_extract(data, '$._execution_session_id')=? "
                "AND (id=? OR json_extract(data, '$.status') NOT IN ('queued', 'scheduled'))",
                (task['_execution_session_id'], task['id']))]

    def _task_messages(self, task_id):
        row = self.db.execute("SELECT json_group_array(json(data)) FROM (SELECT data FROM messages "
                              "WHERE json_extract(data, '$.task_id')=? ORDER BY rowid)", (task_id,)).fetchone()
        return json.loads(row[0])

    def _withdraw_stream(self, task_id):
        for message in self._task_messages(task_id):
            if not message.get('streaming') or message.get('_hidden'):
                continue
            bot = self._get('bots', message['bot_id'])
            visible = [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM messages WHERE json_extract(data, '$.bot_id')=? "
                "AND COALESCE(json_extract(data, '$._hidden'),0)=0 "
                "AND COALESCE(json_extract(data, '$._deleted'),0)=0 ORDER BY rowid DESC", (bot['id'],))]
            unread = [m['id'] for m in visible if m['role'] == 'assistant'][:bot['unread_count']]
            if message['id'] in unread:
                bot['unread_count'] -= 1
            bot['last_message'] = next((m['text'][:160] for m in visible
                if m['id'] != message['id'] and not m.get('reaction_event')), '')
            message.update(_hidden=True, streaming=False)
            self._put('messages', message)
            self._put('bots', bot)
            self._event('message.updated', message, bot['id'], task_id)

    @staticmethod
    def _advance_input(task):
        task['_native_turn_id'] = uid()
        if task.get('_turn_message_ids'):
            task['_input_after_message_id'] = task['_turn_message_ids'][-1]
        task.pop('_turn_message_ids', None)
        task.pop('_direct_reply_message_id', None)
        task.pop('_turn_attachment_ids', None)
        task.pop('_continuation_receipt_ids', None)
        task.pop('_conversation_epoch', None)
        task.pop('_resume_decision', None)
        task.pop('_background_completion', None)
        task.pop('_pr_review', None)
        task.pop('_work_retry', None)
        task.pop('_work_resume', None)
        task.pop('_reaction_feedback', None)
        task['_preference_source_ids'] = [source_id for source_id in task.get('_preference_source_ids', [])
                                          if source_id not in task.get('_turn_preference_source_ids', [])]

    def _consume_stopped_dependency_result(self, task, assignment):
        dependency_id = task.get('_resolved_dependency_id')
        result = (task.get('_bot_messages') or [None])[0]
        source_row = (self.db.execute('SELECT data FROM tasks WHERE id=?', (dependency_id,)).fetchone()
                      if dependency_id else None)
        source = json.loads(source_row[0]) if source_row else None
        if not (assignment['status'] == 'failed' and task.get('_is_return')
                and task.get('_assignment_task_id') == assignment['id']
                and dependency_id and dependency_id == task.get('source_task_id')
                and dependency_id in assignment.get('_waiting_on', [])
                and task['bot_id'] == assignment['bot_id'] and result
                and source and source['status'] in ('completed', 'failed')
                and self.db.execute("SELECT 1 FROM deliveries WHERE "
                    "json_extract(data,'$.receipt.task_id')=? AND "
                    "json_extract(data,'$.source_task_id')=? AND "
                    "json_extract(data,'$.target_bot_id')=? AND "
                    "json_extract(data,'$.text')=? LIMIT 1",
                    (task['id'], dependency_id, task['bot_id'], result['text'])).fetchone()):
            return False
        # The failed owner cannot execute this callback, but its saved result
        # must be part of the next continuation in the original execution.
        assignment.setdefault('_bot_messages', []).append(result)
        self._put('tasks', assignment)
        task.update(status='completed', summary='The result was saved for the stopped assignment.', updated_at=now())
        self._finish_assignment_turn(task, task['summary'], 'completed')
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
        return True

    def _run_task(self, task_id: str):
        from .turn_timings import TurnTimings
        timing = TurnTimings(task_id, 'service')
        with self.lock:
            task = self._get('tasks', task_id)
            bot_lock = self.bot_locks.setdefault(execution_key(task), threading.Lock())
        with bot_lock:
            reaction_feedback = []
            started = None
            source_seen = {}
            source_activity = []
            try:
                with self.transaction():
                    task = self._get('tasks', task_id)
                    if task['status'] not in ('queued', 'scheduled') or self.stopping.is_set():
                        return
                    bot = self._get('bots', task['bot_id'])
                    if bot.get('_deleted') or bot['archived']:
                        return
                    assignment = self._assignment(task)
                    if task.get('_assignment_task_id') and assignment['status'] in ('failed', 'cancelled'):
                        if not self._consume_stopped_dependency_result(task, assignment):
                            task.update(status=assignment['status'], summary='The original assignment stopped. Reply to continue.', updated_at=now())
                            self._prepare_return(task, task['summary'], task['status'])
                            self._put('tasks', task)
                            self._event('task.updated', self._public(task), bot['id'], task_id)
                        return
                    if self._task_paused(task) or self._continuation_blocked(task) or self._automatic_work_is_paused(task):
                        return
                    if self._has_uncertain_receipt(task_id, task.get('_native_turn_id')):
                        task.update(status='failed', summary='A previous tool has no receipt. Check its result before starting new work.')
                        self._finish_assignment_turn(task, 'Task failed: ' + task['summary'], 'failed')
                        self._put('tasks', task)
                        self._event('task.updated', self._public(task), bot['id'], task_id)
                        self._refresh_bot(bot['id'])
                        return
                    task.setdefault('_native_turn_id', uid())
                    task.setdefault('_conversation_epoch', self._setting('conversation_epoch'))
                    if '_turn_message_ids' not in task:
                        messages = [m for m in self._history(bot['id'], task_id=task_id,
                                    shared_message_ids=task.get('_shared_message_ids', []))
                                    if m['role'] == 'user' and m['kind'] == 'text' and not m.get('decision_id')]
                        after = task.get('_input_after_message_id')
                        if after:
                            boundary = next((i for i, m in enumerate(messages) if m['id'] == after), -1)
                            messages = messages[boundary + 1:]
                        task.update(_turn_message_ids=[m['id'] for m in messages], _turn_started_at=now(),
                                    _steering=[], _live_messages={}, _turn_preference_source_ids=list(task.get('_preference_source_ids', [])))
                    self._record_direct_reply(task)
                    task.setdefault('_reaction_feedback', [] if task.get('_work_status_only') else list(bot.get('_pending_reactions', {}).values()))
                    task.setdefault('_turn_attachment_ids', list(task.get('_attachment_ids', [])))
                    task.pop('error_code', None)
                    task.pop('_error_detail', None)
                    from .turn_timings import TurnTrace
                    queued_at = datetime.fromisoformat(task['updated_at'].replace('Z', '+00:00'))
                    TurnTrace(task, 'dispatch').mark('dispatch',
                        queue_age_ms=max(0, round((datetime.now(timezone.utc) - queued_at).total_seconds() * 1000, 1)))
                    task.update(status='working', summary='Working', updated_at=now())
                    self._put('tasks', task)
                    self._event('task.updated', self._public(task), bot['id'], task_id)
                    self._refresh_bot(bot['id'])
                    decision = self._get('decisions', task['_resume_decision']) if task.get('_resume_decision') else None
                    started = (task.get('_resume_decision'), task['_native_turn_id'])
                if decision and decision['status'] == 'approved' and not decision.get('_native'):
                    self._execute_decision(decision['id'])
                    with self.lock:
                        decision = self._get('decisions', decision['id'])
                with self.lock:
                    task = self._get('tasks', task_id)
                    if task['status'] != 'working' or self.stopping.is_set():
                        return
                    if task.pop('_routine_pending_bot_result', None):
                        # This exact snapshot supplies the bot messages to the provider.
                        # A later delivery writes its own pending marker under the same lock.
                        self._put('tasks', task)
                    history = self._history(bot['id'])
                    visible_replies = ({identity: self._get('messages', identity).get('_visible_reply_context')
                                        for identity in task.get('_turn_message_ids', [])}
                                       if task.get('_chat_session') else {})
                timing.mark('history')
                provider_task = self._public(task) | {'_native_turn_id': task['_native_turn_id'], '_execution_session_id': task.get('_execution_session_id'),
                                                       '_kanban_claim': task.get('_kanban_claim'),
                                                       '_kanban_external_id': task.get('_kanban_claim', {}).get('external_id'),
                                                       '_kanban_workspace': task.get('_kanban_claim', {}).get('workspace')}
                for field in ('_work_todos', '_work_resume', '_source_audit_prompt', '_self_recovery', '_chat_session', '_native_intake', '_native_outcome_id', '_work_context_id', '_direct_reply_message_id'):
                    if field in task:
                        provider_task[field] = task[field]
                if steps := card_answer_steps(task, decision):
                    provider_task['_source_answer_steps'] = steps
                if task.get('_native_outcome_id'):
                    provider_task['_native_outcome'] = task['_kanban_claim']
                if lane := self._technical_lane(task):
                    provider_task['_technical_lane'] = lane
                if task.get('_execution_session_id'):
                    provider_task['_execution_task_ids'] = self._execution_task_ids(task)
                reaction_feedback = task.get('_reaction_feedback', [])
                provider_task['_reaction_feedback'] = reaction_feedback
                provider_task['_turn_message_ids'] = task['_turn_message_ids']
                provider_task['_visible_reply_contexts'] = visible_replies
                provider_task['_conversation_epoch'] = task['_conversation_epoch']
                if task.get('_background_completion'):
                    provider_task['_background_completion'] = task['_background_completion']
                if task.get('_pr_review'):
                    provider_task['_pr_review'] = task['_pr_review']
                if task.get('_native_job'):
                    provider_task['_native_job'] = task['_native_job']
                    # A release in progress counts as finished. If Hermes clears the claim during this turn, the
                    # bridge then confirms the native completion instead of pausing the turn.
                    release = task.get('_native_release') or {}
                    provider_task['_native_job_finished'] = task.get('_native_job_finished', False) or (
                        release.get('job_id') == task['_native_job'].get('id')
                        and release.get('owner') == (task['_native_job'].get('fire_claim') or {}).get('by'))
                    with self.lock:
                        provider_task['_native_job_completion'] = self.native_routine_completion_input(task)
                with self.lock:
                    provider_task['_native_receipts'] = [json.loads(row[0]) for row in self.db.execute(
                        "SELECT data FROM native_receipts WHERE json_extract(data, '$.task_id')=? "
                        "AND json_extract(data, '$.turn_id')=? AND json_extract(data, '$.state')='completed' ORDER BY rowid",
                        (task_id, task.get('_native_turn_id')))]
                    carried = [self._get('native_receipts', identity) for identity in task.get('_continuation_receipt_ids', [])]
                    provider_task['_native_receipts'] = list({r['id']: r for r in carried + provider_task['_native_receipts']}.values())
                    provider_task.update(self._bot_creation_input(task))
                    provider_task['_withdrawal_corrections'] = self._withdrawal_corrections_available(task)
                timing.mark('receipts')
                # The context readers below only read. Each would parse every task again without the shared block.
                with self._read_view() as view, view._read_transaction(), view._shared_reads():
                    bot = view._get('bots', task['bot_id'])
                    if not task.get('_chat_session') and not task.get('_native_outcome_id'):
                        provider_task['_tracked_prs'] = view._tracked_work_input(task)
                        provider_task['_tracked_work'] = view._tracked_items_input(task)
                    provider_task['_tracked_pr_observations'] = view._tracked_pr_observations(task, provider_task.get('_tracked_prs', []))
                    # The context stage takes about 1 s on the Mac mini. These marks show which read costs the time.
                    timing.mark('context_tracked')
                    if not task.get('_work_status_only'):
                        provider_task['_recent_work_updates'] = view.recent_work_updates(task)
                    timing.mark('context_updates')
                    work_id = task.get('_work_context_id') or assignment.get('_work_context_id')
                    if work_id and not task.get('_native_outcome_id'):
                        provider_task['_work_context'] = view.work_context_input(work_id)
                        provider_task['_work_status_only'] = task.get('_work_status_only', False)
                    provider_task.update(view.decision_chat_input(task))
                    timing.mark('context_decisions')
                    provider_task.update(view._bot_deletion_input(task))
                    provider_task['attachments'] = [view._public(view._get('artifacts', a)) | {'content_base64': None} | view.turn_attachment_content(a) for a in task['_turn_attachment_ids']]
                    provider_task['_available_bots'] = [{'id': b['id'], 'name': b['name'], 'role': b['role']} for b in view._all('bots') if b['id'] != bot['id'] and not b['archived'] and not b.get('_deleted')]
                    if task.get('_is_return') and task.get('source_task_id'):
                        rows = view.db.execute(
                            "SELECT data FROM messages WHERE json_extract(data,'$.task_id')=? "
                            "AND json_extract(data,'$._human_device_id') IS NOT NULL "
                            "AND json_extract(data,'$.role')='user' AND json_extract(data,'$.kind')='text' "
                            "AND NOT coalesce(json_extract(data,'$._hidden'),0) "
                            "AND NOT coalesce(json_extract(data,'$._deleted'),0) ORDER BY rowid DESC LIMIT 10",
                            (task['source_task_id'],))
                        provider_task['_source_human_messages'] = list(reversed([
                            {k: message[k] for k in ('id', 'text', 'created_at')}
                            for message in (json.loads(row[0]) for row in rows)]))
                    provider_task['_bot_messages'] = task.get('_bot_messages', [])
                    provider_task['_is_return'] = bool(task.get('_is_return'))
                    timing.mark('context_inputs')
                    assignment = view._assignment(task)
                    if assignment.get('_work_todos'):
                        provider_task['_work_todos'] = assignment['_work_todos']
                    if task.get('_assignment_task_id'):
                        instructions = [m['text'] for m in view._history(bot['id'], task_id=assignment['id'])
                                        if m['role'] == 'user' and m['kind'] == 'text' and not m.get('decision_id')]
                        provider_task['_handoff_assignment'] = {
                            'task_id': assignment['id'], 'request': '\n\n'.join([*[m['text'] for m in assignment.get('_bot_messages', [])], *instructions]) or assignment['title'],
                            'return_to_bot_id': assignment.get('source_bot_id'),
                            'waiting_on_task_ids': [identity for identity in assignment.get('_waiting_on', []) if identity != task.get('_resolved_dependency_id')],
                        }
                    timing.mark('context_assignment')
                    # Same predicate as the worker's NativeWork, evaluated on the assembled provider task.
                    if work_enabled('resume' if decision else 'start', provider_task):
                        provider_task['_work_dependencies'] = view.work_dependencies(task)
                    timing.mark('context_dependencies')
                    deliveries = view.db.execute(
                        "SELECT d.data FROM deliveries d WHERE json_extract(d.data,'$.source_task_id') IN "
                        "(SELECT id FROM tasks WHERE id=? OR json_extract(data,'$._assignment_task_id')=?) ORDER BY d.rowid",
                        (assignment['id'], assignment['id']))
                    provider_task['_outgoing_bot_messages'] = [
                        dict(target_bot_id=d['target_bot_id'], text=d['text'], task_id=d['receipt']['task_id'],
                             status=view._get('tasks', d['receipt']['task_id'])['status'] if d['receipt']['task_id'] else d['receipt']['status'])
                        for row in deliveries for d in [json.loads(row[0])]]

                timing.mark('context')
                # Every turn passes here. Read the linked GitHub threads outside the lock, so the bot sees new comments before it acts.
                from .decision_sources import read_source_activity
                source_activity, source_seen = read_source_activity(self, task_id)
                if source_activity:
                    provider_task['_source_activity'] = source_activity
                timing.mark('sources')
                with self.transaction():
                    current = self._get('tasks', task_id)
                    if (current['status'] != 'working' or self.stopping.is_set()
                            or current.get('_native_turn_id') != task.get('_native_turn_id')):
                        return
                    if self._task_paused(current):
                        current.update(status='queued', summary='Waiting for the bot to be restored.', updated_at=now())
                        self._put('tasks', current)
                        self._event('task.updated', self._public(current), current['bot_id'], task_id)
                        return
                timing.mark('ready')
                timing.report()
                emit = lambda kind, payload: self.emit(task_id, kind, payload)
                if decision:
                    if not self.run_gateway_resume(provider_task, bot, history, decision, emit):
                        self.provider.resume_task(provider_task, self._public(bot), history, self._public(decision) | {'action': decision.get('_action')}, emit)
                elif not self.run_gateway_chat(provider_task, bot, history, emit):
                    self.provider.start_task(provider_task, self._public(bot), history, emit)
                with self.lock:
                    current = self._get('tasks', task_id)
                # A worker can fail before it constructs or receives the prompt.
                # Retain the activity for a retry instead of acknowledging that failed turn.
                if current['status'] not in ('failed', 'cancelled'):
                    self._record_source_seen(task_id, source_seen, source_activity)
                if current['status'] not in ('failed', 'cancelled'):
                    self._ack_reaction_feedback(bot['id'], task_id, reaction_feedback)
                if current['status'] == 'working':
                    self.emit(task_id, 'completed', {'summary': 'Ready to review.'})
            except YieldForDecision as pause:
                # The provider received the turn and paused it. The bot saw the source activity.
                self._record_source_seen(task_id, source_seen, source_activity)
                if pause.decision_id:
                    self._ack_reaction_feedback(task['bot_id'], task_id, reaction_feedback)
            except Exception as error:
                if not self.stopping.is_set():
                    summary = f'Work stopped: {str(error)[:240]}'
                    with self.transaction():
                        current = self._get('tasks', task_id)
                        # Fail only the decision this worker started with. A newer answer, given while an independent
                        # technical work turn ran, belongs to the next turn.
                        if started and started[0] and (current.get('_resume_decision'), current.get('_native_turn_id')) == started:
                            failed_decision = self._get('decisions', current['_resume_decision'])
                            if failed_decision['status'] == 'approved':
                                failed_decision.update(status='failed', outcome=summary)
                                self._put('decisions', failed_decision)
                                self._event('decision.updated', self._public(failed_decision), current['bot_id'], task_id)
                    if current['status'] == 'working':
                        self.emit(task_id, 'failed', {'summary': summary})
            finally:
                if not self.stopping.is_set():
                    with self.transaction():
                        for message in self._task_messages(task_id):
                            if message['task_id'] == task_id and message['streaming']:
                                message['streaming'] = False
                                self._put('messages', message)
                                self._event('message.updated', message, message['bot_id'], task_id)
                                self.push.record('message.completed', message, message['bot_id'], task_id)
                        self._refresh_bot(task['bot_id'])

    def _record_source_seen(self, task_id, seen, activity=None):
        """Record delivered revisions and item signatures only after the provider receives a turn.

        Failed delivery records nothing. Track the bounded items actually shown
        so same-second GitHub rereads can detect new or edited comments.
        """
        if not seen:
            return
        from .decision_sources import activity_signature, _issue_url
        signatures = {}
        for row in activity or []:
            if isinstance(row.get('new_activity'), list) and row.get('url'):
                key = _issue_url(row['url']).lower()
                signatures.setdefault(key, []).extend(activity_signature(item) for item in row['new_activity'])
        with self.transaction():
            task = self._get('tasks', task_id)
            for record in {id(r): r for r in (task, self._assignment(task))}.values():
                merged = dict(record.get('_source_activity_seen', {}))
                delivered = dict(record.get('_source_activity_items_seen', {}))
                for key, revision in seen.items():
                    old_revision = merged.get(key, '')
                    if revision < old_revision:
                        continue
                    merged[key] = revision
                    old = delivered.get(key, {})
                    # Carry signatures only across the same revision; older
                    # records no longer occur in a new timestamp's activity.
                    prior = old.get('signatures', []) if old.get('revision') == revision else []
                    delivered[key] = dict(revision=revision,
                                          signatures=list(dict.fromkeys([*prior, *signatures.get(key, [])]))[-100:])
                record['_source_activity_seen'] = merged
                record['_source_activity_items_seen'] = delivered
                self._put('tasks', record)

    def _record_phase(self, task_id: str, payload: dict):
        from .turn_phase import PHASES
        phase = payload.get('phase')
        if phase not in PHASES:
            return
        with self.transaction():
            task = self._get('tasks', task_id)
            if task['status'] != 'working' or task.get('phase') == phase:
                return
            task.setdefault('turn_started_at', now())
            task['phase'] = phase
            self._put('tasks', task)
            self._event('task.updated', self._public(task), task['bot_id'], task_id)

    def emit(self, task_id: str, kind: str, payload: dict):
        if kind == 'phase':
            return self._record_phase(task_id, payload)
        self._refuse_technical_event(task_id, kind)
        if kind == 'steering_ready':
            with self.lock:
                if payload.get('turn_id') != self._get('tasks', task_id).get('_native_turn_id'):
                    return
            return self._deliver_live_messages(task_id)
        if kind == 'dependency_unlink':
            return self.unlink_native_dependency(task_id, payload)
        if kind == 'block_pause':
            return self.record_block_pause(task_id, payload)
        if kind == 'work_continue':
            return self.continue_native_work(task_id, payload)
        if kind == 'work_wait':
            return self.wait_for_task(task_id, payload)
        if kind == 'decision_response':
            return self.resolve_chat_decision(task_id, payload)
        if kind == 'service_restart':
            return self.request_service_restart(task_id, payload)
        if kind == 'withdrawal_correction':
            return self.correct_withdrawal(task_id, payload)
        if kind in ('native_call', 'native_result', 'native_approval', 'native_question', 'native_proposal'):
            return self.native_event(task_id, kind, payload)
        if kind == 'bot_delete':
            return self._delete_bot_from_chat(task_id, payload)
        if kind == 'bot_create':
            return self._create_bot_from_chat(task_id, payload)
        if kind == 'native_bot_message':
            return self.native_relay_delivery(task_id, payload)
        if kind == 'bot_message':
            return self._deliver_bot_message(task_id, payload)
        if kind == 'artifact' and payload.get('kind') == 'work':
            return self._register_work(task_id, payload)
        if kind == 'artifact' and payload.get('kind') == 'pr':
            return self._register_pr_artifact(task_id, payload)
        pending = False
        with self.transaction():
            task = self._get('tasks', task_id)
            if task['status'] != 'working' or self.stopping.is_set():
                raise YieldForDecision('This task no longer accepts provider output.')
            bot_id = task['bot_id']
            if kind == 'text_delta':
                candidate = task.pop('_silence_candidate', '')
                replacing = payload.get('replace') is True
                text = ('' if replacing else candidate) + str(payload.get('text', payload.get('delta', '')))
                if payload.get('finalize_interim') is True:
                    from .interim_messages import confirm
                    if confirm(self, task, text):
                        return
                previous = next((m for m in reversed(self._task_messages(task_id)) if m['streaming']), None)
                if replacing and not text:
                    self._withdraw_stream(task_id)
                    # A held start of a silence marker goes too.
                    task.pop('_silence_candidate', None)
                    self._put('tasks', task)
                    return
                # A later chunk may turn the marker into an ordinary sentence.
                if not previous and ('[SILENT]'.startswith(text.lstrip()) or is_silent_response(text)):
                    task['_silence_candidate'] = text
                    self._put('tasks', task)
                    return
                self._put('tasks', task)
                if previous:
                    previous['text'] = text if replacing else previous['text'] + text
                    self._put('messages', previous)
                    bot = self._get('bots', previous['bot_id'])
                    bot['last_message'] = previous['text'][:160]
                    self._put('bots', bot)
                    self._event('message.updated', previous, bot_id, task_id)
                else:
                    message = self._message(bot_id, text, task_id=task_id, streaming=True)
                    from .turn_timings import TurnTrace
                    TurnTrace(task, 'delivery').mark('reply_created', message_id=message['id'])
            elif kind == 'interim_message':
                from .interim_messages import store
                store(self, task, payload.get('text'))
            elif kind == 'interim_seal':
                from .interim_messages import seal
                seal(self, task)
            elif kind == 'gateway_question':
                self.record_gateway_question(task, payload)
            elif kind == 'activity':
                self._message(bot_id, str(payload.get('text', payload.get('summary', 'Working')))[:500], task_id=task_id, kind='activity')
            elif kind == 'decision':
                action = payload.get('action', {})
                decision = dict(id=uid(), bot_id=bot_id, task_id=task_id, title=str(payload.get('title', 'Review proposed action'))[:160], explanation=str(payload.get('explanation', ''))[:2000], action_type=str(payload.get('action_type', action.get('type', 'unknown'))), resource=str(payload.get('resource', action.get('filename', ''))), consequences=str(payload.get('consequences', 'Creates a file on the Mac.'))[:2000], recommended=payload.get('recommended', 'approve'), options=[], status='pending', urgency=payload.get('urgency', 'normal'), needs_attention_now=payload.get('needs_attention_now') is True, created_at=now(), decided_at=None, decided_by=None, decided_by_name=None, correction=None, outcome=None, revision=1, _action=action, _action_hash=digest(encoded(action)), _prediction=payload.get('predicted_choice', payload.get('prediction')), _answers=[])
                if decision['_prediction'] not in ('approve', 'reject'):
                    decision['_prediction'] = None
                prior_id = task.get('_resume_decision')
                if prior_id:
                    prior = self._get('decisions', prior_id)
                    if prior['status'] in ('modified', 'replied'):
                        decision['replaces_decision_id'] = prior_id
                        prior['replaced_by_decision_id'] = decision['id']
                        prior['outcome'] = 'The bot prepared a new proposal with your requested changes.'
                        self._put('decisions', prior)
                        self._event('decision.updated', self._public(prior), bot_id, task_id)
                self._validate_action(decision)
                decision.update(proposal_preview=action['content'], proposal_hash=digest(encoded(action)), _proposal_action=action)
                options = self._decision_options(payload, decision)
                option_ids = {option['id'] for option in options}
                recommended_id = payload.get('recommended_option_id')
                predicted_id = payload.get('predicted_option_id')
                if recommended_id is not None and recommended_id not in option_ids:
                    raise ServiceError(400, 'The recommended choice must belong to this decision.')
                if predicted_id is not None and predicted_id not in option_ids:
                    raise ServiceError(400, 'The predicted choice must belong to this decision.')
                decision.update(options=options, _options_hash=digest(encoded(options)), recommended_option_id=recommended_id, _predicted_option_id=predicted_id, selected_option_id=None, selected_option_label=None)
                task.update(status='needs_you', summary=decision['title'], updated_at=now())
                self._put('decisions', decision)
                self._put('tasks', task)
                self._event('decision.created', self._public(decision), bot_id, task_id)
                self._message(bot_id, decision['title'], task_id=task_id, kind='decision', decision_id=decision['id'])
                self._event('task.updated', self._public(task), bot_id, task_id)
                pending = True
            elif kind == 'artifact':
                if payload.get('kind', 'task') != 'task':
                    raise ServiceError(409, 'Providers must request approval before creating a file.')
                artifact = dict(id=uid(), bot_id=bot_id, task_id=task_id, title=str(payload.get('title', task['title']))[:200], kind='task', status='completed', preview=str(payload.get('preview', ''))[:4000], url=None, created_at=now())
                self._put('artifacts', artifact)
                self._message(bot_id, artifact['title'], task_id=task_id, kind='artifact', artifact_id=artifact['id'])
                self._event('artifact.created', artifact, bot_id, task_id)
            elif kind == 'steering_consumed':
                if payload.get('turn_id') != task.get('_native_turn_id'):
                    return
                for identity in payload.get('message_ids', []):
                    text = task.get('_live_messages', {}).pop(identity, None)
                    if text is None:
                        continue
                    if text in task.get('_steering', []):
                        task['_steering'].remove(text)
                    task.setdefault('_turn_message_ids', []).append(identity)
                self._record_direct_reply(task)
                self._put('tasks', task)
            elif kind in ('completed', 'failed'):
                if kind == 'failed':
                    self._withdraw_stream(task_id)
                candidate = task.pop('_silence_candidate', '')
                marker_silent = is_silent_response(candidate)
                if candidate and not marker_silent:
                    self._message(bot_id, candidate, task_id=task_id)
                self._save_work_progress(task, payload)
                if self._finish_technical_lane(task, kind, payload.get('summary')):
                    return
                if kind == 'completed' and not task.get('_steering'):
                    continuation = self._continue_unfinished_work(task)
                    if continuation == 'queued':
                        return
                    if continuation:
                        kind = 'failed'
                        payload = {'summary': continuation}
                if task.get('_native_job'):
                    task['_routine_silent'] = kind == 'completed' and payload.get('routine_silent') is True
                reaction_silent = (kind == 'completed' and payload.get('reaction_silent') is True
                                   and reaction_only_turn(dict(operation='start', task=task, history=self._history(task['bot_id']))))
                silent_completion = kind == 'completed' and (marker_silent or payload.get('silent_response') is True or is_silent_response(payload.get('summary')))
                task['_silent_completion'] = silent_completion or reaction_silent or task.get('_routine_silent', False)
                if is_silent_response(payload.get('summary')):
                    payload = dict(payload, summary='Complete.')
                error_code = payload.get('error_code')
                from .native_failure import NATIVE_ERROR_CODES
                known_errors = set(NATIVE_ERROR_CODES)
                known_errors.update({'bot_creation_invalid_request', 'bot_creation_rejected', 'bot_creation_receipt_unconfirmed'})
                if kind == 'failed' and isinstance(error_code, str) and error_code in known_errors:
                    task['error_code'] = error_code
                    self.record_provider_failure(task['bot_id'], error_code, payload.get('retry_after'))
                elif kind == 'completed':
                    self.clear_provider_failure(task)
                # The class and the place of a worker failure, without its message. It stays on the Mac. A failure that has
                # none must not keep the place of an earlier one.
                if kind == 'failed' and isinstance(payload.get('error_detail'), str):
                    task['_error_detail'] = payload['error_detail'][:200]
                else:
                    task.pop('_error_detail', None)
                has_followup = kind == 'completed' and bool(task.get('_steering'))
                task.update(status='queued' if has_followup else kind, summary='Applying your follow-up at the next safe turn.' if has_followup else str(payload.get('summary', payload.get('text', 'Ready to review.' if kind == 'completed' else 'Work failed.')))[:500], updated_at=now())
                for decision in self._all('decisions'):
                    execution_task = decision.get('_execution_task_id') if decision.get('inbox_kind') == 'proposal' else decision['task_id']
                    if execution_task == task_id and decision.get('action_type') == 'question' and decision['status'] == 'approved':
                        decision.update(status='executed' if kind == 'completed' else 'failed',
                                        outcome='Hermes continued with your answer.' if kind == 'completed' else task['summary'])
                        self._put('decisions', decision)
                        self._event('decision.updated', self._public(decision), bot_id, task_id)
                if has_followup:
                    self._advance_input(task)
                else:
                    task['_preference_source_ids'] = []
                    turn_started_at = task.get('_turn_started_at', task['created_at'])
                    results = [message for message in reversed(self._task_messages(task_id)) if message['task_id'] == task_id and message.get('sender_bot_id') == task['bot_id'] and message['kind'] in ('text', 'artifact') and message['created_at'] >= turn_started_at and not message.get('_hidden') and not message.get('_interim') and not message.get('recipient_bot_ids')]
                    result = next((message for message in results if message['kind'] == 'text'), results[0] if results else None)
                    result_text = result['text'] if result and kind == 'completed' else task['summary']
                    if result and result['kind'] == 'artifact' and kind == 'completed':
                        artifact = self._get('artifacts', result['artifact_id'])
                        result_text = artifact['preview'] if artifact['kind'] == 'task' else f'{artifact["title"]}: {artifact["status"]}'
                    if kind == 'failed':
                        result_text = 'Task failed: ' + result_text
                    self._finish_assignment_turn(task, result_text, kind)
                self._put('tasks', task)
                self._event('task.updated', self._public(task), bot_id, task_id)
                generic_completion = kind == 'completed' and task['summary'].strip().rstrip('.!').casefold() in {'ready to review', 'done', 'complete', 'completed', 'task complete', 'task completed', 'work complete', 'work completed'}
                if task['status'] == 'waiting' or task.get('_routine_silent') or reaction_silent or silent_completion:
                    pass
                elif generic_completion:
                    turn_started_at = task.get('_turn_started_at', task['created_at'])
                    result = next((message for message in reversed(self._task_messages(task_id)) if message['task_id'] == task_id and message['role'] == 'assistant' and message['kind'] in ('text', 'artifact') and not message.get('_hidden') and not message.get('_interim') and message['created_at'] >= turn_started_at and message['text'].strip()), None)
                    if result:
                        bot = self._get('bots', task['bot_id'] if task.get('source_bot_id') else task['conversation_bot_id'])
                        bot['last_message'] = result['text'][:160]
                        self._put('bots', bot)
                    else:
                        self._message(bot_id, task['summary'], task_id=task_id, kind='activity')
                else:
                    self._message(bot_id, task['summary'], task_id=task_id, kind='status' if kind == 'failed' else 'text')
            else:
                raise ServiceError(400, f'Unknown provider event: {kind}')
            self._refresh_bot(bot_id)
        if pending:
            raise YieldForDecision('The service persisted the decision.', decision_id=decision['id'])

    def _execute_decision(self, decision_id: str):
        with self.transaction():
            decision = self._get('decisions', decision_id)
            task = self._get('tasks', decision['task_id'])
            if task['status'] != 'working' or decision['status'] != 'approved':
                return
            if self._task_paused(task):
                decision.update(status='pending', outcome='Permissions changed before execution. Review this action.', revision=decision['revision'] + 1)
                task.update(status='needs_you', summary='Permissions changed. Review the pending action.')
                self._put('decisions', decision)
                self._put('tasks', task)
                self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
                self._event('task.updated', self._public(task), task['bot_id'], task['id'])
                return
            self._validate_action(decision)
            receipt = self.db.execute('SELECT state,artifact_id,content_hash FROM receipts WHERE decision_id=?', (decision_id,)).fetchone()
            if receipt and receipt[0] == 'completed':
                self._complete_receipt(decision, receipt[1], receipt[2])
                return
            artifact_id = receipt[1] if receipt else uid()
            content_hash = digest(decision['_action']['content'])
            self.db.execute('INSERT INTO receipts VALUES (?,?,?,?,?) ON CONFLICT(decision_id) DO NOTHING', (decision_id, 'executing', artifact_id, content_hash, 'Local file creation'))
            self._event('execution.started', {'decision_id': decision_id, 'artifact_id': artifact_id, 'option_id': decision.get('selected_option_id')}, task['bot_id'], task['id'])
        # Revalidate at the execution boundary. Bot lifecycle changes share this lock.
        with self.lock:
            with self.transaction():
                decision = self._get('decisions', decision_id)
                task = self._get('tasks', decision['task_id'])
                if task['status'] != 'working' or self._task_paused(task):
                    self.db.execute('DELETE FROM receipts WHERE decision_id=?', (decision_id,))
                    if task['status'] != 'cancelled':
                        decision.update(status='pending', outcome='Permissions changed before execution.', revision=decision['revision'] + 1)
                        task.update(status='needs_you')
                        self._put('decisions', decision)
                        self._put('tasks', task)
                        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
                        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
                    return
                self._validate_action(decision)
            path = self.files / artifact_id
            if path.exists() or path.is_symlink():
                if path.is_symlink() or digest(path.read_bytes()) != content_hash:
                    raise ServiceError(409, 'The execution receipt does not match its file. Inspect the Mac before retrying.')
            else:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(decision['_action']['content'].encode())
                    output.flush()
                    os.fsync(output.fileno())
                directory = os.open(self.files, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            with self.transaction():
                self._complete_receipt(decision, artifact_id, content_hash)

    def _complete_receipt(self, decision: dict, artifact_id: str, content_hash: str):
        existing = self.db.execute('SELECT data FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
        if not existing:
            action = decision['_action']
            artifact = dict(id=artifact_id, bot_id=decision['bot_id'], task_id=decision['task_id'], title=action['filename'], kind='file', status='ready', preview=action['content'][:4000], url=None, created_at=now(), _content_hash=content_hash)
            self._put('artifacts', artifact)
            self._message(decision['bot_id'], artifact['title'], task_id=decision['task_id'], kind='artifact', artifact_id=artifact_id)
            self._event('artifact.created', self._public(artifact), decision['bot_id'], decision['task_id'])
        decision.update(status='executed', outcome='The file is ready to review.', artifact_id=artifact_id)
        self._put('decisions', decision)
        self.db.execute('UPDATE receipts SET state=? WHERE decision_id=?', ('completed', decision['id']))
        self._event('decision.updated', self._public(decision), decision['bot_id'], decision['task_id'])
        self._event('execution.completed', {'decision_id': decision['id'], 'artifact_id': artifact_id}, decision['bot_id'], decision['task_id'])

    def _migrate_threads(self):
        for bot in self._all('bots'):
            bot.setdefault('pinned', False)
            bot.setdefault('model_override', None)
            bot['profile_id'] = profile_id(bot)
            self._sync_profile(bot)
            self._put('bots', bot)
        tasks = self._all('tasks')
        for task in tasks:
            before = dict(task)
            task.pop('workflow', None)
            task.pop('_permission_ancestors', None)
            if task['status'] == 'paused' and not self._task_paused(task):
                task['status'] = 'queued'
            # Writing every unchanged task took about 0.8 s at each start on the Mac mini.
            if task != before:
                self._put('tasks', task)
        changed = False
        for task in tasks:
            if not task.get('thread_id') or 'conversation_bot_id' not in task or 'exchange_id' not in task:
                task.setdefault('thread_id', task['id'])
                task.setdefault('conversation_bot_id', task['bot_id'])
                if 'exchange_id' not in task:
                    task['exchange_id'] = self._new_exchange()
                task.setdefault('exchange_depth', 0)
                task.setdefault('source_bot_id', None)
                task.setdefault('source_task_id', None)
                self._put('tasks', task)
                changed = True
        threads = {task['id']: task['thread_id'] for task in tasks}
        task_map = {task['id']: task for task in tasks}
        for message in self._all('messages'):
            thread_id = threads.get(message.get('task_id'))
            if 'thread_id' not in message or message['thread_id'] != thread_id or 'sender_bot_id' not in message or 'recipient_bot_ids' not in message:
                message['thread_id'] = thread_id
                task = task_map.get(message.get('task_id'))
                message.setdefault('sender_bot_id', task['bot_id'] if task and message['role'] != 'user' else None)
                message.setdefault('recipient_bot_ids', [task['bot_id']] if task and message['role'] == 'user' else [])
                self._put('messages', message)
                changed = True
        if changed:
            self._event('threads.migrated', {})

    def _migrate_bot_exchanges(self):
        """Reclassify stored messages without changing native conversation history."""
        tasks = {task['id']: task for task in self._all('tasks')}
        messages = self._all('messages')
        changed = False
        for message in messages:
            task = tasks.get(message.get('task_id'), {})
            before = dict(message)
            if message.get('_direct_reply_message_id'):
                message.pop('exchange_recipient_bot_ids', None)
            elif task.get('source_bot_id') and not task.get('_is_return') and message['role'] == 'assistant' and message['kind'] == 'text' and not message.get('decision_id'):
                message.setdefault('exchange_recipient_bot_ids', [task['source_bot_id']])
            if message.get('sender_bot_id') and message.get('recipient_bot_ids') and message['kind'] == 'text':
                message['kind'] = 'bot_exchange'
            if message['kind'] == 'bot_exchange':
                message['bot_id'] = message['sender_bot_id']
            elif task.get('source_bot_id') and message['role'] != 'user' and not message.get('_direct_reply_message_id'):
                message['bot_id'] = task['bot_id']
            if message != before:
                self._put('messages', message)
                changed = True
        # Index the messages once. A scan of every message for each delivery grew with both tables at every start.
        exchanges = {(m.get('task_id'), m.get('sender_bot_id')) for m in messages if m['kind'] == 'bot_exchange'}
        texts = {}
        for m in messages:
            if m['kind'] == 'text':
                texts.setdefault(m.get('task_id'), []).append(m)
        for delivery in self._all('deliveries'):
            task = tasks.get(delivery['receipt'].get('task_id'))
            source = tasks.get(delivery['source_task_id'])
            if not task or not source:
                continue
            if (task['id'], source['bot_id']) in exchanges:
                continue
            result = next((m for m in reversed(texts.get(source.get('_result_task_id', source['id']), []))
                           if m['kind'] == 'text' and m.get('sender_bot_id') == source['bot_id']
                           and m['text'] == delivery['text'] and not m.get('_direct_reply_message_id')
                           and not m.get('_hidden') and not m.get('_deleted')), None)
            if result:
                if result.get('exchange_recipient_bot_ids') != [task['bot_id']]:
                    result['exchange_recipient_bot_ids'] = [task['bot_id']]
                    self._put('messages', result)
                    changed = True
                continue
            message = dict(id=str(uuid5(UUID(source['id']), delivery['id'])), bot_id=source['bot_id'],
                           task_id=task['id'], thread_id=task['thread_id'], sender_bot_id=source['bot_id'],
                           recipient_bot_ids=[task['bot_id']], role='assistant', kind='bot_exchange',
                           text=delivery['text'], created_at=delivery['created_at'], streaming=False,
                           decision_id=None, artifact_id=None)
            self._put('messages', message)
            messages.append(message)
            exchanges.add((message['task_id'], message['sender_bot_id']))
            changed = True
        if changed:
            self._event('transcript.updated', {})

    def _recover(self):
        self.recover_native_relays()
        for context in self._all('contexts'):
            if context['status'] == 'working' and not self._block_uncertain_context(context):
                context['status'] = 'queued'
                self._put('contexts', context)
        for task in self._all('tasks'):
            if task['status'] == 'working':
                self._withdraw_stream(task['id'])
                uncertain = self._has_uncertain_receipt(task['id'], task.get('_native_turn_id'))
                task.update(status='failed' if uncertain else 'queued', summary='A tool stopped without a receipt. Check its result before starting new work.' if uncertain else 'Resuming after the Mac service restarted.', updated_at=now())
                # A lane turn returns to its pending decision without a result. The executing receipt stays unknown,
                # so the next continuation is refused until someone checks it.
                if uncertain and self._finish_technical_lane(task, 'failed', task['summary']):
                    self._event('task.recovered', self._public(task), task['bot_id'], task['id'])
                    continue
                if uncertain:
                    if recovery := task.get('_self_recovery'):
                        # A service interruption does not consume a bot's recovery attempt.
                        task['_self_recovery'] = dict(recovery, attempt=max(0, recovery.get('attempt', 0) - 1))
                    self._finish_assignment_turn(task, 'Task failed: ' + task['summary'], 'failed')
                    if not task.get('_native_outcome_id'):
                        self._message(task['bot_id'], 'Coconut’s service restarted before a tool returned its result. I stopped to avoid repeating an action. Reply to continue.', task_id=task['id'])
                self._put('tasks', task)
                self._event('task.recovered', self._public(task), task['bot_id'], task['id'])
        for message in self._all('messages'):
            if message['streaming']:
                message['streaming'] = False
                self._put('messages', message)
                self._event('message.updated', self._public(message), message['bot_id'], message['task_id'])
        for decision_id, state, artifact_id, content_hash in self.db.execute('SELECT decision_id,state,artifact_id,content_hash FROM receipts').fetchall():
            if state != 'executing':
                continue
            decision = self._get('decisions', decision_id)
            path = self.files / artifact_id
            if path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == content_hash:
                self._complete_receipt(decision, artifact_id, content_hash)
            elif path.exists() or path.is_symlink() or decision.get('_action', {}).get('type') != 'create_file':
                decision.update(status='failed', outcome='Execution is uncertain. Inspect the Mac. The service will not repeat this action.')
                task = self._get('tasks', decision['task_id'])
                task.update(status='failed', summary=decision['outcome'])
                self._finish_assignment_turn(task, 'Task failed: ' + task['summary'], 'failed')
                self._put('decisions', decision)
                self._put('tasks', task)
                self.db.execute('UPDATE receipts SET state=? WHERE decision_id=?', ('uncertain', decision_id))
                self._event('execution.uncertain', self._public(decision), task['bot_id'], task['id'])
            # An absent private file can safely retry with O_EXCL and the same ID.
        for bot in self._all('bots'):
            self._refresh_bot(bot['id'])

    def _block_uncertain_context(self, context):
        if not self._has_uncertain_receipt(context['task_id'], context['id']):
            return False
        context.update(status='failed', error_code='tool_result_unknown', _failed_at=now())
        self._put('contexts', context)
        if not context.get('_decision_refresh'):
            self._message(context['bot_id'],
                          'A tool stopped without a result. Check its result on the Mac before trying again. The decision remains available.',
                          task_id=context['task_id'], decision_id=context['decision_id'])
        self._event('context.failed', {key: context[key] for key in ('id', 'decision_id', 'status', 'error_code')},
                    context['bot_id'], context['task_id'])
        return True

    def _start_context(self, context_id: str, completed=None):
        def run():
            with self.lock:
                context = self._get('contexts', context_id)
                task = self._get('tasks', context['task_id'])
                bot = self._get('bots', task['bot_id'])
                bot_lock = self.bot_locks.setdefault(execution_key(task), threading.Lock())
            with bot_lock:
                with self.transaction():
                    context = self._get('contexts', context_id)
                    task = self._get('tasks', context['task_id'])
                    bot = self._get('bots', task['bot_id'])
                    if context['status'] != 'queued' or task['status'] == 'cancelled' or self._task_paused(task) or self.stopping.is_set():
                        return
                    if context.get('_decision_refresh'):
                        decision = self._get('decisions', context['decision_id'])
                        if (decision['status'] != 'pending' or decision['revision'] != context['_decision_revision']
                                or context.get('_conversation_epoch') != task.get('_conversation_epoch')):
                            context['status'] = 'cancelled'
                            self._put('contexts', context)
                            self._refresh_bot(bot['id'])
                            self._event('context.cancelled', {'id': context_id, 'decision_id': decision['id']}, bot['id'], task['id'])
                            return
                    if context.get('_background_completion'):
                        decision = self._get('decisions', context['decision_id'])
                        if (decision['status'] != 'pending'
                                or not self._process_observation_current(task, bot)
                                or context.get('_conversation_epoch') != task.get('_conversation_epoch')):
                            context['status'] = 'cancelled'
                            self._put('contexts', context)
                            self._event('context.cancelled', {'id': context_id, 'decision_id': decision['id']}, bot['id'], task['id'])
                            return
                        context['_decision_revision'] = decision['revision']
                    if self._block_uncertain_context(context):
                        return
                    context['status'] = 'working'
                    self._put('contexts', context)
                    self._refresh_bot(bot['id'])
                    history = self._history(bot['id'])
                    decision = self._get('decisions', context['decision_id'])
                    provider_task = self._public(task) | {'_execution_session_id': task.get('_execution_session_id'),
                                                           '_kanban_external_id': context.get('_kanban_claim', {}).get('external_id'),
                                                           '_kanban_workspace': context.get('_kanban_claim', {}).get('workspace')}
                    provider_task['_conversation_epoch'] = context.get('_conversation_epoch', task.get('_conversation_epoch', self._setting('conversation_epoch')))
                    if task.get('_execution_session_id'):
                        provider_task['_execution_task_ids'] = self._execution_task_ids(task)
                    # Replies and maintenance use their own contexts, outside the source routine's execution claim.
                    provider_task['_native_receipts'] = self._native_task_receipts(task['id'], turn_id=context_id, state='completed')
                    if context.get('_background_completion'):
                        provider_task['_background_completion'] = context['_background_completion']
                    provider_task['_decision_reply'] = context.get('_reply_device') is not None
                    provider_task['_decision_refresh'] = bool(context.get('_decision_refresh'))
                    provider_task['_context_id'] = context_id
                    provider_task['_context_message_id'] = context.get('_message_id')
                    if context.get('_recovery_attempt'):
                        provider_task['_context_recovery'] = context['_context_recovery']
                    provider_task['pending_decision'] = self._public(decision) | {'action': decision.get('_action')}
                    provider_task['attachments'] = [self._public(self._get('artifacts', a)) | {'content_base64': None} | self.turn_attachment_content(a) for a in context.get('_attachment_ids', task.get('_turn_attachment_ids', []))]
                chunks = []
                context_failure = {}
                def emit(kind, payload):
                    if kind == 'steering_ready':
                        # Context workers announce readiness but do not accept chat steering.
                        return
                    if kind == 'decision_response':
                        if context.get('_decision_refresh'):
                            return self.refresh_decision(context_id, payload)
                        return self.resolve_decision_reply(context_id, payload)
                    if kind in ('native_call', 'native_result'):
                        return self.native_event(task['id'], kind, payload, context_id=context_id)
                    if kind == 'failed':
                        context_failure.update(summary=str(payload.get('summary', 'The context request failed.'))[:500], error_code=str(payload.get('error_code', 'context_failed'))[:80])
                        return
                    if kind not in ('text_delta', 'activity', 'completed'):
                        raise ServiceError(409, 'Context requests cannot propose or execute actions.')
                    text = str(payload.get('text', payload.get('summary', '')))
                    if kind == 'text_delta' and payload.get('replace') is True:
                        chunks.clear()
                    if kind != 'activity' and text:
                        chunks.append(text)
                try:
                    if not self.run_gateway_context(task, provider_task, bot, history, context, emit):
                        self.provider.answer_context(provider_task, self._public(bot), history, context['question'], emit)
                    answer = context_failure.get('summary') or ''.join(chunks).strip('\r\n') or 'No further context was returned. The decision remains available.'
                    status = 'failed' if context_failure else 'completed'
                except Exception:
                    answer = 'The context request failed. The decision remains available.'
                    status = 'failed'
                if self.stopping.is_set():
                    return
                with self.transaction():
                    context = self._get('contexts', context_id)
                    if context['status'] == 'cancelled' or self._get('tasks', task['id'])['status'] == 'cancelled':
                        return
                    if context.get('_decision_refresh') and not context.get('_refresh_recorded'):
                        status = 'failed'
                        context['error_code'] = 'decision_refresh_incomplete'
                    context['status'] = status
                    if status == 'failed':
                        # The wait before the next check of a card counts from here, not from when the check was queued.
                        context['_failed_at'] = now()
                    if context_failure:
                        context['error_code'] = context_failure['error_code']
                    self._put('contexts', context)
                    self._refresh_bot(bot['id'])
                    if not context.get('_decision_refresh'):
                        self._message(bot['id'], answer, task_id=task['id'], decision_id=context['decision_id'],
                                      direct_reply_message_id=context.get('_message_id'))
                    self._event('context.completed' if status == 'completed' else 'context.failed', {'id': context_id, 'decision_id': context['decision_id'], 'status': status, 'error_code': context.get('error_code')}, bot['id'], task['id'])
        def finish_run():
            try:
                run()
            finally:
                if completed:
                    completed()
        thread = threading.Thread(target=finish_run, daemon=True, name='decision-context')
        self.context_threads[context_id] = thread
        thread.start()

    def close(self):
        self.stopping.set()
        self.pr_refresh_requested.set()
        if hasattr(self.provider, 'subscribe_model_catalog'):
            self.provider.subscribe_model_catalog(None)
        with self.changed:
            self.changed.notify_all()
        if self.dispatcher:
            self.dispatcher.join(timeout=2)
        if self.chat_dispatcher:
            self.chat_dispatcher.join(timeout=2)
        if self.pr_poller:
            self.pr_poller.join(timeout=2)
        if self.process_poller:
            self.process_poller.join(timeout=20)
        if self.tool_checker:
            self.tool_checker.join(timeout=20)
        if self.source_poller:
            self.source_poller.join(timeout=65)
        if getattr(self, 'profile_watcher', None):
            self.profile_watcher.join(timeout=10)
        self.push.close()
        self._close_gateway_chat()
        with self.lock:
            context_task_ids = {c['task_id'] for c in self._all('contexts') if c['status'] in ('queued', 'working')}
        for task_id in set(self.running) | context_task_ids:
            self.provider.cancel_task(task_id)
        if hasattr(self.provider, 'close'):
            self.provider.close()
        for thread in [*self.running.values(), *self.context_threads.values(), *self.gateway_cron_threads.values()]:
            thread.join(timeout=3)
        if self.kanban is not None:
            self.kanban.close()
        # A provider must honor cancellation before its connection can close.
        if not any(thread.is_alive() for thread in [*self.running.values(), *self.context_threads.values(), *self.gateway_cron_threads.values()]):
            with self.lock:
                self.db.close()
                fcntl.flock(self.process_lock.fileno(), fcntl.LOCK_UN)
                self.process_lock.close()
