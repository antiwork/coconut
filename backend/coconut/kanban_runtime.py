"""Run the installed Hermes Kanban kernel outside the phone API process."""
from __future__ import annotations

import contextlib
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys


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
        self.conn.execute('CREATE TABLE IF NOT EXISTS coconut_outcomes (native_id TEXT PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0)')
        if 'input_id' not in {r[1] for r in self.conn.execute('PRAGMA table_info(coconut_outcomes)')}:
            self.conn.execute("ALTER TABLE coconut_outcomes ADD COLUMN input_id TEXT NOT NULL DEFAULT ''")

    def sync(self, rows):
        for row in rows:
            old = self.conn.execute('SELECT native_id,intent FROM coconut_bindings WHERE external_id=?',
                                    (row['id'],)).fetchone()
            intent = fingerprint({k: row[k] for k in ('state', 'input')})
            if row.get('native_id'):
                task = self.kb.get_task(self.conn, old[0] if old else row['native_id'])
                previous_input = self.conn.execute('SELECT input_id FROM coconut_outcomes WHERE native_id=?', (task.id,)).fetchone()
                new_input = not old or bool(previous_input and previous_input[0] and previous_input[0] != row['input'])
                if task.status in ('done', 'archived') and row['state'] in ('ready', 'scheduled') and new_input:
                    # Native terminal results stay immutable. New input owns a durable successor.
                    identity = self.kb.create_task(self.conn, title=row['title'], body=task.body,
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
                old = self.conn.execute('SELECT native_id,intent FROM coconut_bindings WHERE external_id=?', (row['id'],)).fetchone()
                if row.get('workspace_changed') and Path(row['workspace']).is_dir() and task.workspace_path != row['workspace']:
                    self.workspace.set_workspace_path(self.conn, task.id, row['workspace'])
                # Only new input or cancellation can change a native-owned lifecycle here.
                if row['state'] == 'archived' and task.status != 'archived':
                    self.kb.archive_task(self.conn, task.id)
                elif task.status == 'ready' and row['state'] != 'ready':
                    self.kb.schedule_task(self.conn, task.id, reason=row.get('summary') or 'Waiting for delivery.')
                elif row['state'] == 'ready' and (task.status == 'scheduled' or old[1] and old[1] != intent):
                    if task.status == 'review':
                        self.kb.reopen_review_task(self.conn, task.id)
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

    def snapshot(self):
        result = []
        for binding in self.conn.execute(
                'SELECT external_id,native_id FROM coconut_bindings b JOIN tasks t ON t.id=b.native_id '
                "WHERE t.status NOT IN ('done','archived') OR t.id IN (SELECT native_id FROM coconut_outcomes) OR b.rowid IN "
                '(SELECT rowid FROM coconut_bindings ORDER BY rowid DESC LIMIT 32) ORDER BY b.rowid'):
            task = self.kb.get_task(self.conn, binding[1])
            blocker = self.dispatch.check_respawn_guard(self.conn, task.id) if task.status == 'ready' else None
            progress = self.conn.execute(
                "SELECT json_extract(payload,'$.note') FROM task_events WHERE task_id=? AND run_id=? "
                "AND kind='heartbeat' AND json_extract(payload,'$.note') IS NOT NULL ORDER BY id DESC LIMIT 1",
                (task.id, task.current_run_id)).fetchone()
            accepted = self.conn.execute('SELECT input_id FROM coconut_outcomes WHERE native_id=?', (task.id,)).fetchone()
            result.append(dict(external_id=binding[0], dispatch_blocker=blocker, input_id=accepted[0] if accepted else None,
                progress_note=progress[0] if progress else None, outcome_summary=self.kb.latest_summary(self.conn, task.id), **asdict(task)))
        for row in self.conn.execute('SELECT id FROM tasks WHERE id NOT IN (SELECT native_id FROM coconut_bindings) AND id NOT IN (SELECT native_id FROM coconut_outcomes)'):
            task = self.kb.get_task(self.conn, row[0])
            result.append(dict(external_id='native:' + task.id, unbound=True, **asdict(task)))
        return result

    def tick(self, rows, capacity, spawn):
        self.sync(rows)
        # Native retries and dependency promotion can outlive Coconut input.
        # Park only ready lanes, including omitted terminal/legacy requests.
        # Blocked outcomes retain their reason and continuation eligibility;
        # todo dependencies must remain eligible for native promotion.
        selected = {r['id'] for r in rows if r['state'] == 'ready'}
        for external, identity in self.conn.execute(
                'SELECT b.external_id,t.id FROM tasks t LEFT JOIN coconut_bindings b ON b.native_id=t.id '
                "WHERE t.status = 'ready'").fetchall():
            if external not in selected:
                self.kb.schedule_task(self.conn, identity, reason='Waiting for Coconut input readiness.')
        deferred = set()
        def selected_spawn(task, workspace):
            binding = self.conn.execute('SELECT external_id FROM coconut_bindings WHERE native_id=?', (task.id,)).fetchone()
            if not binding or binding[0] not in selected:
                # The kernel can reclaim/promote another task inside this tick.
                # Release that claim through its native lifecycle, without a
                # worker or a bogus spawn-failure/retry loop.
                self.kb.schedule_task(self.conn, task.id, expected_run_id=task.current_run_id,
                                      reason='Waiting for Coconut input readiness.')
                deferred.add(task.id)
                return None
            return spawn(task, workspace)
        result = self.dispatch.dispatch_once(self.conn, spawn_fn=selected_spawn, max_in_progress=capacity,
                                       max_spawn=capacity, reconcile_orphans=True)
        result.spawned = [r for r in result.spawned if r[0] not in deferred]
        return {'tasks': self.snapshot(), 'dispatch': asdict(result)}


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
                    result = {'tasks': board.snapshot()}
                else:
                    raise ValueError('Unknown Kanban operation.')
            send({'result': result})
        except Exception as error:
            send({'error': str(error)})


if __name__ == '__main__':
    main()
