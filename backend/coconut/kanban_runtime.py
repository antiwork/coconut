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

    def sync(self, rows):
        for row in rows:
            old = self.conn.execute('SELECT native_id,intent FROM coconut_bindings WHERE external_id=?',
                                    (row['id'],)).fetchone()
            intent = fingerprint({k: row[k] for k in ('state', 'input')})
            if old:
                existing = self.kb.get_task(self.conn, old[0])
                if existing.title != row['title'] or existing.priority != row['priority']:
                    with self.kb.write_txn(self.conn):
                        self.conn.execute('UPDATE tasks SET title=?,priority=? WHERE id=?', (row['title'], row['priority'], old[0]))
                if existing.workspace_path != row['workspace']:
                    self.workspace.set_workspace_path(self.conn, old[0], row['workspace'])
            # Schedule and unblock commit separately. Replay readiness after an interrupted finish.
            if old and old[1] == intent and not (row['state'] == 'ready' and existing.status == 'scheduled'):
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

    def finish(self, identity, run_id, state, summary):
        task = self.kb.get_task(self.conn, identity)
        if not task or task.current_run_id != run_id or task.status != 'running':
            return False
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
                "WHERE t.status NOT IN ('done','archived') OR b.rowid IN "
                '(SELECT rowid FROM coconut_bindings ORDER BY rowid DESC LIMIT 32) ORDER BY b.rowid'):
            task = self.kb.get_task(self.conn, binding[1])
            blocker = self.dispatch.check_respawn_guard(self.conn, task.id) if task.status == 'ready' else None
            result.append(dict(external_id=binding[0], dispatch_blocker=blocker, **asdict(task)))
        return result

    def tick(self, rows, capacity, spawn):
        self.sync(rows)
        result = self.dispatch.dispatch_once(self.conn, spawn_fn=spawn, max_in_progress=capacity,
                                       max_spawn=capacity, reconcile_orphans=True)
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
        send({'spawn': {'external_id': binding[0], 'native_id': task.id,
                        'run_id': task.current_run_id, 'workspace': workspace}})
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
                elif request['op'] == 'snapshot':
                    result = {'tasks': board.snapshot()}
                else:
                    raise ValueError('Unknown Kanban operation.')
            send({'result': result})
        except Exception as error:
            send({'error': str(error)})


if __name__ == '__main__':
    main()
