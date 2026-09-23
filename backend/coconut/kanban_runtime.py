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


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


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
        self.conn.execute('CREATE TABLE IF NOT EXISTS coconut_bindings '
                          '(external_id TEXT PRIMARY KEY, native_id TEXT NOT NULL UNIQUE, intent TEXT NOT NULL)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS coconut_pr_followups '
                          '(native_id TEXT PRIMARY KEY, input_id TEXT NOT NULL)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS coconut_outcomes (native_id TEXT PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0)')
        if 'input_id' not in {r[1] for r in self.conn.execute('PRAGMA table_info(coconut_outcomes)')}:
            self.conn.execute("ALTER TABLE coconut_outcomes ADD COLUMN input_id TEXT NOT NULL DEFAULT ''")

    def transfer_worker_pid(self, claimed, pid):
        """Remove prior leases before Hermes assigns this worker to a new run."""
        pid = int(pid)
        fingerprint = (self.dispatch._process_fingerprint(pid)
                       or self.dispatch.UNVERIFIED_WORKER_FINGERPRINT)
        self.conn.execute(
            'UPDATE task_runs SET worker_pid=NULL,worker_started_at=NULL '
            'WHERE id<>? AND worker_pid=?',
            (claimed.current_run_id, pid))
        self.conn.execute('UPDATE tasks SET worker_pid=NULL,worker_started_at=NULL '
                          'WHERE id<>? AND worker_pid=?',
                          (claimed.id, pid))
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

    def finish(self, identity, run_id, state, summary):
        task = self.kb.get_task(self.conn, identity)
        if not task or task.current_run_id != run_id or task.status != 'running':
            return False
        # Reviewer runs belong to the native reviewer, including the PID-less
        # interval after claim. Coconut must never finish one as a preparation.
        if self.kb._retry_status_for_run(self.conn, identity, run_id) == 'review':
            return False
        owned = self.conn.execute('SELECT 1 FROM coconut_outcomes WHERE native_id=?', (identity,)).fetchone()
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

    def snapshot(self, external_ids=()):
        result = []
        for binding in self.conn.execute(
                "SELECT coalesce(b.external_id,'outcome:' || t.id),t.id FROM tasks t "
                'LEFT JOIN coconut_bindings b ON b.native_id=t.id '
                'WHERE t.id IN (SELECT native_id FROM coconut_outcomes) OR (b.external_id IS NOT NULL AND '
                "(t.status NOT IN ('done','archived') OR b.rowid IN "
                '(SELECT rowid FROM coconut_bindings ORDER BY rowid DESC LIMIT 32))) '
                'OR b.external_id IN (SELECT value FROM json_each(?)) ORDER BY t.created_at,t.rowid',
                (json.dumps(external_ids),)):
            task = self.kb.get_task(self.conn, binding[1])
            blocker = self.dispatch.check_respawn_guard(self.conn, task.id) if task.status == 'ready' else None
            progress = self.conn.execute(
                "SELECT json_extract(payload,'$.note'),created_at FROM task_events WHERE task_id=? AND run_id=? "
                "AND kind='heartbeat' AND json_extract(payload,'$.note') IS NOT NULL ORDER BY id DESC LIMIT 1",
                (task.id, task.current_run_id)).fetchone()
            accepted = self.conn.execute('SELECT input_id FROM coconut_outcomes WHERE native_id=?', (task.id,)).fetchone()
            result.append(dict(external_id=binding[0], dispatch_blocker=blocker, input_id=accepted[0] if accepted else None,
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
        return result

    def tick(self, rows, capacity, spawn):
        self.sync(rows)
        result = self._dispatch_ready(rows, capacity, spawn)
        return {'tasks': self.snapshot(), 'dispatch': asdict(result)}

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
                continuing = task['followup_input'] == row['input']
                new_input = row.get('previous_input') and row['previous_input'] != row['input']
                recovering = (row.get('recovering') and row.get('native_id') == task['id']
                              and row.get('previous_input') == row['input'])
                guard = self.dispatch.check_respawn_guard(self.conn, task['id'])
                if not (continuing or (new_input or recovering) and guard == 'active_pr'):
                    def spawn_with_lease(claimed, workspace):
                        pid = spawn(claimed, workspace)
                        if pid:
                            with self.kb.write_txn(self.conn):
                                self.record_worker_pid(claimed, pid, emit_event=False)
                        return pid
                    self.dispatch._dispatch_lane_task(self.conn, task, task['assignee'], result,
                        lane='ready', dry_run=False, ttl_seconds=None, board=None,
                        failure_limit=self.dispatch.DEFAULT_FAILURE_LIMIT, spawn_fn=spawn_with_lease,
                        per_profile_cap=None, per_profile_running={})
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
                except Exception as error:
                    if self.dispatch._record_task_failure(self.conn, claimed.id, str(error),
                            outcome='spawn_failed', release_claim=True, end_run=True):
                        result.auto_blocked.append(claimed.id)
        return result


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
        send({'spawn': {'external_id': binding[0], 'native_id': task.id,
                        'run_id': task.current_run_id, 'claim_lock': task.claim_lock, 'workspace': workspace,
                        'context': board.kb.build_worker_context(board.conn, task.id) if owned else ''}})
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
                    result = {'ok': board.finish(request['id'], request['run_id'], request['state'], request['summary'])}
                elif request['op'] == 'promote':
                    result = {'id': board.promote(request['row'])}
                elif request['op'] == 'bind':
                    board.bind(request['id'], request['external_id'])
                    result = {'ok': True}
                elif request['op'] == 'snapshot':
                    result = {'tasks': board.snapshot(request.get('external_ids', []))}
                else:
                    raise ValueError('Unknown Kanban operation.')
            send({'result': result})
        except Exception as error:
            send({'error': str(error)})


if __name__ == '__main__':
    main()
