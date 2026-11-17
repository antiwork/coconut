"""Run the installed Hermes Kanban kernel outside the phone API process."""
from __future__ import annotations

import contextlib
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import time

try:
    from .native_failure import TRANSIENT_ERROR_CODES, TRANSIENT_NATIVE_ERROR
except ImportError:
    # The adapter runs as a script, so its folder is on the path and the package is not.
    from native_failure import TRANSIENT_ERROR_CODES, TRANSIENT_NATIVE_ERROR
try:
    from .native_settings import raw_config, save_config
    from .work_limits import board_limits, set_board_limit
except ImportError:
    from native_settings import raw_config, save_config
    from work_limits import board_limits, set_board_limit


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


# Hermes states that wait for someone to act: specify, finish a parent, unblock, review, or reach a scheduled time.
IDLE_STATUSES = ('triage', 'todo', 'blocked', 'review', 'scheduled')
# The lanes of an agent's queue on the phone, by Hermes status. A done task counts only on the day that it finished.
QUEUE_LANES = {'running': 'running', 'ready': 'waiting', 'todo': 'waiting', 'triage': 'waiting', 'scheduled': 'waiting',
               'blocked': 'needs_you', 'review': 'needs_you', 'done': 'done_today'}
# A waiting task without a Hermes event for this long is idle. Earlier app versions archived such tasks on request.
IDLE_AGE = 7 * 86400
# Events that leave a block as it is. Any other later event (a claim, an unblock, a new block) ends its proof.
BLOCK_KEEPING_EVENTS = ('commented', 'linked', 'unlinked', 'attached', 'reprioritized', 'heartbeat', 'terminal_worker_reaped')

class Board:
    def __init__(self, home):
        home = Path(home).resolve()
        os.environ['HERMES_KANBAN_HOME'] = str(home)
        os.environ['HERMES_HOME'] = str(home)
        os.environ['HERMES_KANBAN_BOARD'] = 'default'
        os.environ['HERMES_KANBAN_DB'] = str(home / 'kanban.db')
        os.environ['HERMES_KANBAN_WORKSPACES_ROOT'] = str(home / 'kanban/workspaces')
        os.environ['HERMES_KANBAN_ATTACHMENTS_ROOT'] = str(home / 'kanban/attachments')
        Path(home).mkdir(parents=True, exist_ok=True, mode=0o700)
        # Coconut delivers review cards. Native review workers must not answer them.
        config = Path(home) / 'config.yaml'
        if not config.exists():
            config.write_text('kanban:\n  review_dispatch: false\n')
            config.chmod(0o600)
        from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch, kanban_db_workspace
        self.kb, self.dispatch, self.workspace = kanban_db, kanban_db_dispatch, kanban_db_workspace
        self.conn = kanban_db_connect.connect()
        self._diagnostics = {}
        self.home, self._config = home, (None, {})
        self.conn.execute('CREATE TABLE IF NOT EXISTS coconut_bindings '
                          '(external_id TEXT PRIMARY KEY, native_id TEXT NOT NULL UNIQUE, intent TEXT NOT NULL)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS coconut_pr_followups '
                          '(native_id TEXT PRIMARY KEY, input_id TEXT NOT NULL)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS coconut_outcomes (native_id TEXT PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0)')
        if 'input_id' not in {r[1] for r in self.conn.execute('PRAGMA table_info(coconut_outcomes)')}:
            self.conn.execute("ALTER TABLE coconut_outcomes ADD COLUMN input_id TEXT NOT NULL DEFAULT ''")

    def limits(self):
        """The total and per-agent limits in the Hermes config of this board home (work_limits)."""
        path = self.home / 'config.yaml'
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp != self._config[0]:
            # Hermes reads its own config. A file that it cannot read leaves the Automatic limits.
            try:
                config = raw_config(self.home) if stamp is not None else {}
            except Exception:
                config = {}
            self._config = (stamp, config if isinstance(config, dict) else {})
        return board_limits(self._config[1])

    def set_limit(self, name, limit):
        config = raw_config(self.home)
        save_config(self.home, set_board_limit(config, name, limit))
        self._config = (None, {})
        return self.limits()

    def queues(self):
        """Each agent's queue on the board: tasks running, waiting, waiting for a person, and done today."""
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        result = {}
        for tenant, status, count in self.conn.execute(
                'SELECT tenant,status,COUNT(*) FROM tasks WHERE tenant IS NOT NULL AND status IN '
                "('running','ready','todo','triage','scheduled','blocked','review') "
                "OR tenant IS NOT NULL AND status='done' AND completed_at>=? GROUP BY tenant,status", (midnight,)):
            lanes = result.setdefault(tenant, dict.fromkeys(('running', 'waiting', 'needs_you', 'done_today'), 0))
            lanes[QUEUE_LANES[status]] += count
        return result

    def transfer_worker_pid(self, claimed, pid):
        """Remove prior leases before Hermes assigns this worker to a new run."""
        pid = int(pid)
        fingerprint = (self.dispatch._process_fingerprint(pid)
                       or self.dispatch.UNVERIFIED_WORKER_FINGERPRINT)
        self.conn.execute(
            'UPDATE task_runs SET worker_pid=NULL,worker_started_at=NULL '
            'WHERE task_id=? AND id<>? AND ended_at IS NOT NULL AND worker_pid=?',
            (claimed.id, claimed.current_run_id, pid))
        return fingerprint

    def record_worker_pid(self, claimed, pid, *, emit_event=True):
        fingerprint = self.transfer_worker_pid(claimed, pid)
        self.conn.execute('UPDATE tasks SET worker_pid=?,worker_started_at=? WHERE id=?',
                          (pid, fingerprint, claimed.id))
        self.conn.execute('UPDATE task_runs SET worker_pid=?,worker_started_at=? WHERE id=?',
                          (pid, fingerprint, claimed.current_run_id))
        if emit_event:
            self.kb._append_event(self.conn, claimed.id, 'spawned',
                                  {'pid': pid, 'started_at': fingerprint},
                                  run_id=claimed.current_run_id)

    def sync(self, rows):
        for row in rows:
            old = self.conn.execute('SELECT native_id,intent FROM coconut_bindings WHERE external_id=?',
                                    (row['id'],)).fetchone()
            intent = fingerprint({k: row[k] for k in ('state', 'input')})
            if row.get('native_id'):
                task = self.kb.get_task(self.conn, old[0] if old else row['native_id'])
                # A reviewer can claim after continuation's snapshot. Leave the
                # pending input unconsumed until that native run has finished.
                if task.status == 'running' and row['state'] != 'archived':
                    continue
                if old and old[0] == row['native_id'] and row.get('previous_input'):
                    # Reconcile a legacy claim before comparing accepted input.
                    # Keep the original result addressable when a follow-up moves
                    # its binding, and never replace an already accepted input.
                    self.conn.execute('INSERT INTO coconut_outcomes(native_id,input_id) VALUES (?,?) '
                        'ON CONFLICT(native_id) DO UPDATE SET input_id=excluded.input_id '
                        "WHERE coconut_outcomes.input_id=''", (task.id, row['previous_input']))
                previous_input = self.conn.execute('SELECT input_id FROM coconut_outcomes WHERE native_id=?', (task.id,)).fetchone()
                new_input = not old or bool(previous_input and previous_input[0] and previous_input[0] != row['input'])
                if task.status in ('done', 'archived') and row['state'] in ('ready', 'scheduled') and new_input:
                    # Native terminal results stay immutable. New input owns a durable successor.
                    identity = self.kb.create_task(self.conn, title=task.title, body=task.body,
                        assignee='default', created_by='coconut', tenant=row['bot_id'],
                        workspace_kind='dir', workspace_path=row['workspace'], parents=[task.id],
                        priority=row['priority'], initial_status='blocked',
                        idempotency_key='coconut-followup:' + row['id'] + ':' + row['input'])
                    with self.kb.write_txn(self.conn):
                        self.conn.execute('INSERT INTO coconut_bindings VALUES (?,?,?) ON CONFLICT(external_id) '
                            'DO UPDATE SET native_id=excluded.native_id,intent=excluded.intent', (row['id'], identity, ''))
                    task = self.kb.get_task(self.conn, identity)
                self.bind(task.id, row['id'])
                task = self.kb.get_task(self.conn, task.id)
                if task.status == 'running' and row['state'] != 'archived':
                    continue
                old = self.conn.execute('SELECT native_id,intent FROM coconut_bindings WHERE external_id=?', (row['id'],)).fetchone()
                if row.get('workspace_changed') and Path(row['workspace']).is_dir() and task.workspace_path != row['workspace']:
                    self.workspace.set_workspace_path(self.conn, task.id, row['workspace'])
                # Only new input or cancellation can change a native-owned lifecycle here.
                if row['state'] == 'archived' and task.status != 'archived':
                    self.kb.archive_task(self.conn, task.id)
                elif task.status == 'ready' and row['state'] in ('scheduled', 'review'):
                    self.kb.schedule_task(self.conn, task.id, reason=row.get('summary') or 'Waiting for delivery.')
                # An answered decision resumes the same input even when the
                # review pause occurred entirely between synchronization ticks.
                elif row['state'] == 'ready' and (task.status in ('scheduled', 'review') or old[1] and old[1] != intent):
                    if task.status == 'review':
                        if not self.kb.reopen_review_task(self.conn, task.id):
                            continue
                    elif task.status in ('scheduled', 'blocked'):
                        self.kb.unblock_task(self.conn, task.id)
                    elif task.status == 'triage':
                        self.kb.specify_triage_task(self.conn, task.id, author='coconut')
                self.conn.execute('UPDATE coconut_bindings SET intent=? WHERE external_id=?', (intent, row['id']))
                self.conn.execute('UPDATE coconut_outcomes SET input_id=? WHERE native_id=?', (row['input'], task.id))
                continue
            if old:
                existing = self.kb.get_task(self.conn, old[0])
                if existing.title != row['title'] or existing.priority != row['priority']:
                    with self.kb.write_txn(self.conn):
                        self.conn.execute('UPDATE tasks SET title=?,priority=? WHERE id=?', (row['title'], row['priority'], old[0]))
                if existing.workspace_path != row['workspace']:
                    self.workspace.set_workspace_path(self.conn, old[0], row['workspace'])
            # Schedule and unblock commit separately. Replay readiness after an interrupted finish.
            resume_triage = (old and row['state'] == 'ready' and existing.status == 'triage'
                             and row.get('previous_input') and row['previous_input'] != row['input'])
            if (old and old[1] == intent and not resume_triage
                    and not (row['state'] == 'ready' and existing.status == 'scheduled')):
                continue
            if old:
                identity = old[0]
            else:
                identity = self.kb.create_task(
                    self.conn, title=row['title'], body=json.dumps({'coconut_id': row['id'], 'bot_id': row['bot_id']}),
                    assignee='default', created_by='coconut', tenant=row['bot_id'],
                    workspace_kind='dir', workspace_path=row['workspace'],
                    priority=row['priority'], idempotency_key='coconut:' + row['id'], initial_status='blocked')
            task = self.kb.get_task(self.conn, identity)
            state = row['state']
            # A claim owns execution. Input synchronization cannot end an active run.
            if task.status != 'running':
                if state == 'ready' and task.status in ('blocked', 'scheduled'):
                    self.kb.unblock_task(self.conn, identity)
                elif resume_triage:
                    self.kb.specify_triage_task(self.conn, identity, author='coconut')
                elif state == 'ready' and task.status == 'review':
                    self.kb.reopen_review_task(self.conn, identity)
                elif state == 'review' and task.status in ('ready', 'blocked', 'scheduled'):
                    if task.status != 'ready':
                        self.kb.unblock_task(self.conn, identity)
                    self.kb.request_review(self.conn, identity, summary=row.get('summary'), metadata={'coconut_id': row['id']})
                elif state == 'scheduled' and task.status in ('ready', 'blocked'):
                    self.kb.schedule_task(self.conn, identity, reason=row.get('summary') or 'Waiting for input.')
                elif state == 'blocked' and task.status == 'ready':
                    self.kb.block_task(self.conn, identity, reason=row.get('summary') or 'Waiting for input.')
                elif state == 'done' and task.status not in ('done', 'archived'):
                    if task.status == 'scheduled':
                        self.kb.unblock_task(self.conn, identity)
                    self.kb.complete_task(self.conn, identity, summary=row.get('summary'))
                elif state == 'archived' and task.status != 'archived':
                    self.kb.archive_task(self.conn, identity)
                elif state == 'ready' and task.status in ('done', 'archived'):
                    # Follow-up input has a new native task; the prior result remains immutable.
                    identity = self.kb.create_task(
                        self.conn, title=row['title'], body=json.dumps({'coconut_id': row['id'], 'previous_task': identity}),
                        assignee='default', created_by='coconut', tenant=row['bot_id'], workspace_kind='dir',
                        workspace_path=row['workspace'], priority=row['priority'],
                        idempotency_key='coconut:' + row['id'] + ':' + row['input'])
            # These are presentation fields. All lifecycle writes above use the native kernel.
            with self.kb.write_txn(self.conn):
                self.conn.execute('UPDATE tasks SET title=?,priority=? WHERE id=?', (row['title'], row['priority'], identity))
                self.conn.execute('INSERT INTO coconut_bindings VALUES (?,?,?) ON CONFLICT(external_id) '
                                  'DO UPDATE SET native_id=excluded.native_id,intent=excluded.intent',
                                  (row['id'], identity, intent))

    def run_cut_off(self, identity, run_id):
        """Whether a run of the task ended without a result: it exists, it ended, and it did not complete. A claim
        whose run completed has its result, so the same input does not run again."""
        if not isinstance(run_id, int):
            return False
        run = self.conn.execute('SELECT outcome,ended_at FROM task_runs WHERE id=? AND task_id=?', (run_id, identity)).fetchone()
        return bool(run and run[1] is not None and run[0] != 'completed')

    def _note_respawn_guard(self, identity, reason):
        """Record why a ready task does not spawn, unless its newest event already says so.

        The dispatcher passes about once a second. Hermes writes one event per pass for each guarded task, which was
        288,000 of 404,000 events in 13 days, and each event is a write transaction. A comment or a heartbeat does not
        change the guard, so a guarded task that gets comments, for example from audits, keeps one event too.
        """
        last = self.conn.execute("SELECT kind,payload FROM task_events WHERE task_id=? AND kind NOT IN ('commented','heartbeat') "
                                 'ORDER BY id DESC LIMIT 1', (identity,)).fetchone()
        if last and last[0] == 'respawn_guarded' and json.loads(last[1] or '{}').get('reason') == reason:
            return
        with self.kb.write_txn(self.conn):
            self.kb._append_event(self.conn, identity, 'respawn_guarded', {'reason': reason})

    def promote(self, row):
        binding = self.conn.execute('SELECT native_id FROM coconut_bindings WHERE external_id=?', (row['id'],)).fetchone()
        previous = self.kb.get_task(self.conn, binding[0]) if binding else None
        if previous and previous.status == 'running':
            return None
        owned = previous and self.conn.execute('SELECT 1 FROM coconut_outcomes WHERE native_id=?', (previous.id,)).fetchone()
        if owned:
            return previous.id
        if previous and previous.status not in ('done', 'archived'):
            identity = previous.id
        else:
            identity = self.kb.create_task(self.conn, title=row['title'], body=row['body'], assignee='default',
                created_by='coconut', tenant=row['bot_id'], workspace_kind='dir', workspace_path=row['workspace'],
                parents=[previous.id] if previous else [], initial_status='blocked',
                idempotency_key='coconut-outcome:' + row['work_id'])
        # These fields describe the accepted outcome. Lifecycle changes use the native kernel.
        with self.kb.write_txn(self.conn):
            self.conn.execute('UPDATE tasks SET title=?,body=? WHERE id=?', (row['title'], row['body'], identity))
            self.conn.execute('INSERT INTO coconut_bindings VALUES (?,?,?) ON CONFLICT(external_id) '
                'DO UPDATE SET native_id=excluded.native_id,intent=excluded.intent', (row['id'], identity, 'migration'))
        self.sync([dict(row, native_id=identity)])
        return identity

    def bind(self, identity, external_id):
        if not self.kb.get_task(self.conn, identity):
            raise ValueError('The native outcome does not exist.')
        with self.kb.write_txn(self.conn):
            self.conn.execute('INSERT OR IGNORE INTO coconut_bindings VALUES (?,?,?)', (external_id, identity, ''))
            binding = self.conn.execute('SELECT native_id FROM coconut_bindings WHERE external_id=?', (external_id,)).fetchone()
            if not binding or binding[0] != identity:
                raise ValueError('The request already owns another native outcome.')
            self.conn.execute('INSERT OR IGNORE INTO coconut_outcomes(native_id) VALUES (?)', (identity,))
        if not self.conn.execute('SELECT initialized FROM coconut_outcomes WHERE native_id=?', (identity,)).fetchone()[0]:
            # Intake stays blocked until the phone record exists. Replay either committed half after a crash.
            if self.kb.get_task(self.conn, identity).status == 'blocked':
                self.kb.unblock_task(self.conn, identity)
            self.conn.execute('UPDATE coconut_outcomes SET initialized=1 WHERE native_id=?', (identity,))

    def finish(self, identity, run_id, state, summary, error_code=None):
        task = self.kb.get_task(self.conn, identity)
        if not task or task.current_run_id != run_id or task.status != 'running':
            return False
        # Reviewer runs belong to the native reviewer, including the PID-less
        # interval after claim. Coconut must never finish one as a preparation.
        if self.kb._retry_status_for_run(self.conn, identity, run_id) == 'review':
            return False
        owned = self.conn.execute('SELECT 1 FROM coconut_outcomes WHERE native_id=?', (identity,)).fetchone()
        if owned and state == 'blocked' and error_code in TRANSIENT_ERROR_CODES:
            # The provider stopped this turn for a while. Book an infrastructure failure: it leaves the failure count
            # alone, and the respawn guard spaces the retries. The stored text names no authorization blocker,
            # or the guard would park the task after the cooldown.
            self.dispatch._record_task_failure(self.conn, identity, TRANSIENT_NATIVE_ERROR, outcome='spawn_failed',
                release_claim=True, end_run=True, infrastructure=True)
            return True
        if owned and state in ('done', 'blocked'):
            # Hermes retries a stopped worker. A Coconut reply cannot complete the outcome.
            self.dispatch._record_task_failure(self.conn, identity, summary or 'The worker stopped without an outcome.',
                outcome='worker_protocol_violation' if state == 'done' else 'worker_failed', release_claim=True, end_run=True)
            return True
        if state == 'done':
            return self.kb.complete_task(self.conn, identity, summary=summary, expected_run_id=run_id)
        if state == 'review':
            return self.kb.request_review(self.conn, identity, summary=summary, expected_run_id=run_id)
        if state == 'archived':
            return self.kb.archive_task(self.conn, identity)
        # Scheduled releases capacity without turning a dependency wait into a failure.
        if state == 'ready':
            self.kb.schedule_task(self.conn, identity, reason=summary, expected_run_id=run_id)
            return self.kb.unblock_task(self.conn, identity)
        if state == 'scheduled':
            return self.kb.schedule_task(self.conn, identity, reason=summary, expected_run_id=run_id)
        return self.kb.block_task(self.conn, identity, reason=summary, expected_run_id=run_id)

    def rekinded_block(self, task):
        """The event that proves Hermes made this input block from a dependency block without an open parent, or None.

        Hermes re-kinds such a block so that a context-free respawn cannot start. Only the latest block counts, filed
        by the task's latest run, which ended blocked, with nothing but block-keeping events after it. A worker's or a
        person's own input block, and a capability block, have no such proof and keep their hold.
        """
        if task.status != 'blocked' or task.block_kind != 'needs_input' or task.current_run_id or task.worker_pid:
            return None
        event = self.conn.execute("SELECT id, run_id, payload FROM task_events WHERE task_id=? AND kind='blocked' "
                                  'ORDER BY id DESC LIMIT 1', (task.id,)).fetchone()
        run = self.conn.execute('SELECT id, outcome, ended_at FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1',
                                (task.id,)).fetchone()
        if not event or not run or event[1] != run[0] or run[1] != 'blocked' or run[2] is None:
            return None
        later = {row[0] for row in self.conn.execute('SELECT DISTINCT kind FROM task_events WHERE task_id=? AND id>?',
                                                    (task.id, event[0]))}
        try:
            payload = json.loads(event[2] or '{}')
        except ValueError:
            return None
        if (not isinstance(payload, dict) or later - set(BLOCK_KEEPING_EVENTS) or payload.get('kind') != 'needs_input'
                or payload.get('requested_kind') != 'dependency' or payload.get('rekind_reason') != 'no_open_parent'):
            return None
        return dict(event_id=event[0], run_id=event[1], requested_kind='dependency', rekind_reason='no_open_parent')

    def archive_settled(self, identity, summary=None, rekind_event=None):
        """Close a native task that waits while Coconut has finished its request, so its dependents can start.

        Completion comes first, because a successor and a routine report read the result of a done task. A task that
        still has an open parent, or that waits in triage, cannot complete, so it is archived. A running task keeps its
        worker, and a done or archived task is already final. An input or capability block is someone's hold. Only a
        re-kinded dependency block whose current proof is the event that Coconut read may close.
        """
        task = self.kb.get_task(self.conn, identity)
        if task is None or task.status not in ('ready', 'todo', 'triage', 'blocked', 'scheduled'):
            return False
        # A supplied proof must still hold now, whatever the status. An unblock after the snapshot ends it.
        if rekind_event is not None or (task.block_kind in ('needs_input', 'capability') and task.status in ('triage', 'blocked')):
            proof = self.rekinded_block(task)
            if rekind_event is None or not proof or proof['event_id'] != rekind_event:
                return False
        if task.status in ('ready', 'blocked') and summary:
            with contextlib.suppress(Exception):
                if self.kb.complete_task(self.conn, identity, summary=summary):
                    return True
        return self.kb.archive_task(self.conn, identity)

    def completion(self, identity):
        """The native facts that prove how one card ended. A card that Coconut rebound to a successor stays readable here."""
        task = self.kb.get_task(self.conn, identity)
        if task is None:
            return None
        accepted = self.conn.execute('SELECT input_id FROM coconut_outcomes WHERE native_id=?', (identity,)).fetchone()
        runs = [dict(id=row[0], status=row[1], outcome=row[2], ended=row[3] is not None) for row in self.conn.execute(
            'SELECT id,status,outcome,ended_at FROM task_runs WHERE task_id=? ORDER BY id', (identity,))]
        return dict(id=task.id, tenant=task.tenant, idempotency_key=task.idempotency_key, status=task.status,
                    current_run_id=task.current_run_id, worker_pid=task.worker_pid,
                    input_id=accepted[0] or None if accepted else None, runs=runs, archive=self.archive_note(identity))

    def archive_note(self, identity):
        """The note that the latest archive of a task wrote at once, or None.

        archive_stale archives a task and then comments why, so the comment is the next event of the task. A person's
        archive, an old idle archive, and a Stop write no such comment, so their next comment is someone else's
        or none. The comment's row is the latest comment of the event's author written from the archive to the event,
        because add_comment writes the row and the event together. Hermes writes some comments without an event, so a
        count of events does not find it. Its length must match the event too. `later` lists the kinds of the events
        after the comment.
        """
        archived = self.conn.execute("SELECT id, run_id, created_at FROM task_events WHERE task_id=? AND kind='archived' "
                                     'ORDER BY id DESC LIMIT 1', (identity,)).fetchone()
        if not archived:
            return None
        events = self.conn.execute('SELECT id, kind, payload, created_at FROM task_events WHERE task_id=? AND id>? ORDER BY id',
                                   (identity, archived[0])).fetchall()
        if not events or events[0][1] != 'commented':
            return None
        try:
            payload = json.loads(events[0][2] or '{}')
        except ValueError:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get('author'), str):
            return None
        comment = self.conn.execute('SELECT author, body FROM task_comments WHERE task_id=? AND author=? '
                                    'AND created_at BETWEEN ? AND ? ORDER BY id DESC LIMIT 1',
                                    (identity, payload['author'].strip(), archived[2], events[0][3])).fetchone()
        if not comment or len(comment[1]) != payload.get('len'):
            return None
        return dict(at=archived[2], run_id=archived[1], author=comment[0], note=comment[1],
                    later=[event[1] for event in events[1:]])

    def activity(self, task):
        """Return when the task last changed, and Hermes' own diagnostics for a task that waits for someone.

        The diagnostics read the task's events and runs, so they are computed again only after the task changes, and
        at least once an hour, because some rules depend on how long a task waits.
        """
        # Archiving is housekeeping, not work. An archived idle task keeps the time it last changed.
        last = next((created for created, kind in self.conn.execute(
            'SELECT created_at, kind FROM task_events WHERE task_id=? ORDER BY created_at DESC, id DESC LIMIT 3', (task.id,))
            if kind not in ('archived', 'archive_worker_termination')), None)
        last = max((value for value in (last, task.created_at, task.started_at, task.completed_at) if value), default=None)
        if task.status not in IDLE_STATUSES:
            self._diagnostics.pop(task.id, None)
            return last, []
        key = (task.status, last, int(time.time() // 3600))
        cached = self._diagnostics.get(task.id)
        if cached is None or cached[0] != key:
            from hermes_cli.kanban_diagnostics import compute_task_diagnostics
            found = compute_task_diagnostics(task, self.kb.list_events(self.conn, task.id), self.kb.list_runs(self.conn, task.id))
            cached = self._diagnostics[task.id] = (key, [dict(kind=d.kind, severity=d.severity, title=d.title) for d in found])
        return last, cached[1]

    def _idle(self, identity, idle_age, before=None):
        """Whether a task waits without a worker, unchanged for `idle_age` seconds and, with `before`, since that time."""
        task = self.kb.get_task(self.conn, identity)
        if (task is None or task.status not in IDLE_STATUSES
                or task.worker_pid or task.current_run_id):
            return False
        last, _ = self.activity(task)
        return bool(last) and time.time() - last >= idle_age and (before is None or last < before)

    def archive_stale(self, cards, note, idle_age):
        """Archive idle tasks whose sources closed, with a note on each task.

        `cards` maps each task to the time its last source left. A source of one bot also links the tasks that it
        delegated to other bots, so the tasks can belong to any bot. A task that changed after that time, or in the
        last `idle_age` seconds, stays. So does a task with an open child outside this list, because its archive
        would start the child.
        """
        eligible = {identity for identity, before in cards.items() if self._idle(identity, idle_age, before)}
        children = {}
        for parent, child in self.conn.execute(
                'SELECT l.parent_id, l.child_id FROM task_links l JOIN tasks c ON c.id=l.child_id '
                "WHERE c.status NOT IN ('done','archived') AND l.parent_id IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(eligible)),)):
            children.setdefault(parent, set()).add(child)
        while kept := {identity for identity in eligible if children.get(identity, set()) - eligible}:
            eligible -= kept
        archived = []
        for identity in sorted(eligible):
            # The note comes after the archive. A note on a task that stays would count as new work on it.
            if self.kb.archive_task(self.conn, identity):
                self.kb.add_comment(self.conn, identity, 'coconut', note)
                archived.append(identity)
        return archived

    def snapshot(self, external_ids=(), native_ids=()):
        result = []
        for binding in self.conn.execute(
                "SELECT coalesce(b.external_id,'outcome:' || t.id),t.id FROM tasks t "
                'LEFT JOIN coconut_bindings b ON b.native_id=t.id '
                'WHERE t.id IN (SELECT native_id FROM coconut_outcomes) OR (b.external_id IS NOT NULL AND '
                "(t.status NOT IN ('done','archived') OR b.rowid IN "
                '(SELECT rowid FROM coconut_bindings ORDER BY rowid DESC LIMIT 32))) '
                'OR b.external_id IN (SELECT value FROM json_each(?)) '
                'OR (b.external_id IS NOT NULL AND t.id IN (SELECT value FROM json_each(?))) ORDER BY t.created_at,t.rowid',
                (json.dumps(external_ids), json.dumps(native_ids))):
            task = self.kb.get_task(self.conn, binding[1])
            blocker = self.dispatch.check_respawn_guard(self.conn, task.id) if task.status == 'ready' else None
            progress = self.conn.execute(
                "SELECT json_extract(payload,'$.note'),created_at FROM task_events WHERE task_id=? AND run_id=? "
                "AND kind='heartbeat' AND json_extract(payload,'$.note') IS NOT NULL ORDER BY id DESC LIMIT 1",
                (task.id, task.current_run_id)).fetchone()
            accepted = self.conn.execute('SELECT input_id FROM coconut_outcomes WHERE native_id=?', (task.id,)).fetchone()
            last_activity_at, diagnostics = self.activity(task)
            result.append(dict(external_id=binding[0], dispatch_blocker=blocker, input_id=accepted[0] if accepted else None,
                block_rekind=self.rekinded_block(task),
                last_activity_at=last_activity_at, diagnostics=diagnostics,
                claim_source_status=self.kb._retry_status_for_run(self.conn, task.id) if task.status == 'running' else None,
                progress_note=progress[0] if progress and 0 <= time.time() - progress[1] < 120 else None,
                progress_at=progress[1] if progress else None, outcome_summary=self.kb.latest_summary(self.conn, task.id), **asdict(task)))
        for row in self.conn.execute('SELECT id FROM tasks WHERE id NOT IN (SELECT native_id FROM coconut_bindings) AND id NOT IN (SELECT native_id FROM coconut_outcomes)'):
            task = self.kb.get_task(self.conn, row[0])
            result.append(dict(external_id='native:' + task.id, unbound=True, **asdict(task)))
        graph = self.kb.task_graph_contexts(self.conn, [task['id'] for task in result])
        for task in result:
            task['dependencies'] = [parent for parent in graph[task['id']]['parents']
                                    if parent['status'] not in ('done', 'archived')]
            # Every current parent edge, including finished parents. A registered delegation reads it.
            task['parent_ids'] = [parent['id'] for parent in graph[task['id']]['parents']]
        return result

    def tick(self, rows, capacity, spawn):
        self.sync(rows)
        result = self._dispatch_ready(rows, capacity, spawn)
        return {'tasks': self.snapshot(), 'dispatch': asdict(result), 'limits': self.limits(), 'queues': self.queues()}

    def _dispatch_ready(self, rows, capacity, spawn):
        from hermes_cli.kanban_db_connect import _dispatch_tick_lock
        result = self.dispatch.DispatchResult()
        candidates = {row['id']: row for row in rows if row['state'] == 'ready'}
        # Hermes holds this same lock around its terminal reaper and dispatch.
        with _dispatch_tick_lock(self.kb.kanban_db_path()) as acquired:
            if not acquired:
                result.skipped_locked = True
                return result
            self.dispatch._run_reclaim_phase(self.conn, result, stale_timeout_seconds=0,
                failure_limit=self.dispatch.DEFAULT_FAILURE_LIMIT, reconcile_orphans=True)
            # Reclaim can promote dependencies after Coconut projected readiness.
            # Only explicit input waits park native work. A completed reply
            # cannot stop an outcome that Hermes just made ready.
            waiting = {row['id'] for row in rows if row['state'] in ('scheduled', 'review')}
            for external, identity in self.conn.execute(
                    'SELECT b.external_id,t.id FROM tasks t LEFT JOIN coconut_bindings b ON b.native_id=t.id '
                    "WHERE t.status='ready'").fetchall():
                if external in waiting:
                    self.kb.schedule_task(self.conn, identity, reason='Waiting for Coconut input readiness.')
            allowed, budget = self.dispatch._tick_spawn_budget(self.conn, result,
                max_spawn=capacity, max_in_progress=capacity, board=None)
            if not allowed:
                return result
            # Hermes caps each assignee (kanban.max_in_progress_per_profile). Every task of this board has the assignee
            # `default`, and its tenant is the agent, so the cap of Hermes applies here to each tenant.
            per_agent = self.limits()['per_agent']['effective_limit']
            running = dict(self.conn.execute("SELECT tenant,COUNT(*) FROM tasks WHERE status='running' "
                                             'AND tenant IS NOT NULL GROUP BY tenant').fetchall())
            pending = self.conn.execute('SELECT t.*,b.external_id,f.input_id AS followup_input FROM tasks t JOIN coconut_bindings b '
                'ON b.native_id=t.id LEFT JOIN coconut_pr_followups f ON f.native_id=t.id '
                "WHERE t.status='ready' ORDER BY t.priority DESC,t.created_at,t.rowid").fetchall()
            for task in pending:
                if budget is not None and len(result.spawned) >= budget:
                    break
                row = candidates.get(task['external_id'])
                profile_exists = self.dispatch._profile_exists_fn()
                if not row or not task['assignee'] or profile_exists and not profile_exists(task['assignee']):
                    continue
                agent = task['tenant']
                if agent is not None and running.get(agent, 0) >= per_agent:
                    result.skipped_per_profile_capped.append((task['id'], agent, running[agent]))
                    continue
                continuing = task['followup_input'] == row['input']
                new_input = row.get('previous_input') and row['previous_input'] != row['input']
                recovering = ((row.get('recovering') and row.get('native_id') == task['id']
                               or row.get('interrupted') == task['id'] and self.run_cut_off(task['id'], row.get('interrupted_run')))
                              and row.get('previous_input') == row['input'])
                guard = self.dispatch.check_respawn_guard(self.conn, task['id'])
                if guard is not None and not (continuing or (new_input or recovering) and guard == 'active_pr'):
                    # Hermes would refuse this task and write one event per pass. One event per reason tells the operator why.
                    result.respawn_guarded.append((task['id'], guard))
                    self._note_respawn_guard(task['id'], guard)
                    continue
                if not (continuing or (new_input or recovering) and guard == 'active_pr'):
                    def spawn_with_lease(claimed, workspace):
                        pid = spawn(claimed, workspace)
                        if pid:
                            with self.kb.write_txn(self.conn):
                                self.record_worker_pid(claimed, pid, emit_event=False)
                        return pid
                    if self.dispatch._dispatch_lane_task(self.conn, task, task['assignee'], result,
                            lane='ready', dry_run=False, ttl_seconds=None, board=None,
                            failure_limit=self.dispatch.DEFAULT_FAILURE_LIMIT, spawn_fn=spawn_with_lease,
                            per_profile_cap=None, per_profile_running={}) and agent is not None:
                        running[agent] = running.get(agent, 0) + 1
                    continue
                if guard not in (None, 'active_pr'):
                    result.respawn_guarded.append((task['id'], guard))
                    continue
                # Accepted input or saved recovery continues the same PR. Keep its native claim so failures
                # use normal retries. Remember the input across a lost spawn response or guard expiry.
                with self.kb.write_txn(self.conn):
                    self.conn.execute('INSERT INTO coconut_pr_followups VALUES (?,?) ON CONFLICT(native_id) '
                        'DO UPDATE SET input_id=excluded.input_id', (task['id'], row['input']))
                claimed = self.kb.claim_task(self.conn, task['id'])
                if not claimed:
                    continue
                try:
                    if claimed.workspace_kind == 'worktree':
                        workspace, branch = self.workspace._resolve_worktree_workspace(claimed)
                        self.workspace.set_branch_name(self.conn, claimed.id, branch or claimed.branch_name or 'wt/' + claimed.id)
                    else:
                        workspace = self.workspace.resolve_workspace(claimed)
                    self.workspace.set_workspace_path(self.conn, claimed.id, str(workspace))
                    # The dispatch lock excludes the native reaper during preparation.
                    pid = spawn(claimed, str(workspace))
                    if pid:
                        with self.kb.write_txn(self.conn):
                            self.record_worker_pid(claimed, pid)
                    self.kb._fire_worker_spawned_hook(self.conn, claimed, str(workspace), pid)
                    result.spawned.append((claimed.id, claimed.assignee or '', str(workspace)))
                    if agent is not None:
                        running[agent] = running.get(agent, 0) + 1
                except Exception as error:
                    if self.dispatch._record_task_failure(self.conn, claimed.id, str(error),
                            outcome='spawn_failed', release_claim=True, end_run=True):
                        result.auto_blocked.append(claimed.id)
        return result


# The board tools that a gateway turn calls through Coconut (gateway_chat.gateway_kanban_tool): the tools of an audit,
# and the tools of a native outcome, which act for the Kanban claim of its turn.
BOARD_TOOLS = ('kanban_create', 'kanban_comment', 'kanban_list', 'kanban_show', 'kanban_complete', 'kanban_block',
               'kanban_schedule', 'kanban_heartbeat', 'kanban_request_review', 'kanban_link', 'kanban_unblock')
# The worker env of a Kanban claim. Hermes checks with it that a call acts for the task and the run of the claim.
CLAIM_ENV = ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID', 'HERMES_KANBAN_CLAIM_LOCK')


def board_tool(name, arguments, profile=None, claim=None):
    """Run a board tool of Hermes on Coconut's board, as a worker of the bot runs it. Return its result text.

    `profile` is the Hermes profile of the bot. Hermes records it as the author of a comment and the creator of a task.
    With `claim` (native_id, run_id and claim_lock of a Kanban claim), the call has the worker env of that claim, as a
    worker that holds the claim has it. Hermes then refuses a call for another task or for an earlier run. The runtime
    takes one request at a time, so the env of one call never reaches another.
    """
    if name not in BOARD_TOOLS:
        raise ValueError('Coconut runs only ' + ', '.join(BOARD_TOOLS) + ' for a gateway turn.')
    import tools.kanban_tools  # noqa: F401  The import registers the tools.
    from tools.registry import registry
    values = {'HERMES_PROFILE': profile} if profile else {}
    if claim:
        values.update(zip(CLAIM_ENV, (str(claim['native_id']), str(claim['run_id']), str(claim['claim_lock']))))
    previous = {key: os.environ.get(key) for key in ('HERMES_PROFILE', *CLAIM_ENV)}
    for key in CLAIM_ENV:
        os.environ.pop(key, None)
    os.environ.update(values)
    try:
        result = registry.dispatch(name, dict(arguments))
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return result if isinstance(result, str) else json.dumps(result)


# The comments of a native task that a claim gives its turn, newest last, and the length of each.
CLAIM_COMMENTS = 50
CLAIM_COMMENT_CHARACTERS = 4000


def worker_context(board, identity):
    """Hermes's worker context of a native task without its comment thread, and the comments of the task with their
    IDs. A turn gets the comments as records, which its Hermes session keeps once (native_conversation.turn_records).

    Hermes builds the context with the comments that list_comments of its Kanban module returns, so the context
    without the thread is the context of a task without comments. The adapter handles one request at a time, so the
    swap of that function reaches no other caller."""
    kb, original = board.kb, board.kb.list_comments
    comments = original(board.conn, identity)
    try:
        kb.list_comments = lambda conn, task_id: []
        context = kb.build_worker_context(board.conn, identity)
    finally:
        kb.list_comments = original
    return context, [dict(id=comment.id, author=comment.author, at=comment.created_at,
                          body=(comment.body or '')[:CLAIM_COMMENT_CHARACTERS]) for comment in comments][-CLAIM_COMMENTS:]


def main():
    board = Board(sys.argv[1])
    output = sys.stdout
    def send(data):
        output.write(json.dumps(data) + '\n')
        output.flush()
    def spawn(task, workspace):
        binding = board.conn.execute('SELECT external_id FROM coconut_bindings WHERE native_id=?', (task.id,)).fetchone()
        if binding is None:
            raise ValueError('No Coconut request owns this task.')
        owned = board.conn.execute('SELECT 1 FROM coconut_outcomes WHERE native_id=?', (task.id,)).fetchone()
        context, comments = worker_context(board, task.id) if owned else ('', [])
        send({'spawn': {'external_id': binding[0], 'native_id': task.id,
                        'run_id': task.current_run_id, 'claim_lock': task.claim_lock, 'workspace': workspace,
                        'context': context, 'comments': comments}})
        reply = json.loads(sys.stdin.readline())
        if reply.get('error'):
            raise ValueError(reply['error'])
        return reply['pid']
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                if request['op'] == 'tick':
                    result = board.tick(request['rows'], request['capacity'], spawn)
                elif request['op'] == 'finish':
                    result = {'ok': board.finish(request['id'], request['run_id'], request['state'], request['summary'],
                                                 request.get('error_code'))}
                elif request['op'] == 'archive_stale':
                    result = {'archived': board.archive_stale(request['cards'], request['note'], request['idle_age'])}
                elif request['op'] == 'archive_settled':
                    result = {'ok': board.archive_settled(request['id'], request.get('summary'), request.get('rekind_event'))}
                elif request['op'] == 'promote':
                    result = {'id': board.promote(request['row'])}
                elif request['op'] == 'bind':
                    board.bind(request['id'], request['external_id'])
                    result = {'ok': True}
                elif request['op'] == 'snapshot':
                    result = {'tasks': board.snapshot(request.get('external_ids', []), request.get('native_ids', [])),
                              'limits': board.limits(), 'queues': board.queues()}
                elif request['op'] == 'limits':
                    result = {'limits': board.limits()}
                elif request['op'] == 'set_limit':
                    result = {'limits': board.set_limit(request['name'], request['limit'])}
                elif request['op'] == 'completion':
                    result = {'card': board.completion(request['id'])}
                elif request['op'] == 'tool':
                    result = {'result': board_tool(request['name'], request['arguments'], request.get('profile'),
                                                   request.get('claim'))}
                else:
                    raise ValueError('Unknown Kanban operation.')
            send({'result': result})
        except Exception as error:
            send({'error': str(error)})


if __name__ == '__main__':
    main()
