"""Run with COCONUT_HERMES_ROOT/PYTHON. A finished Coconut request must not leave a native task that blocks its dependents."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from coconut.kanban_scheduler import NativeKanban, SETTLE_BATCH, SETTLE_SECONDS

SOURCE = os.environ.get('COCONUT_HERMES_ROOT')
PYTHON = os.environ.get('COCONUT_HERMES_PYTHON')

PRELUDE = '''
import sys
from pathlib import Path
from coconut.kanban_runtime import Board
home = Path(sys.argv[1]) / 'kanban'
board = Board(home)
kb, conn = board.kb, board.conn

def native(name, parents=()):
    workspace = home / name
    workspace.mkdir()
    identity = kb.create_task(conn, title=name, body='Fixture', assignee='default', tenant='care',
        workspace_kind='dir', workspace_path=str(workspace), parents=list(parents))
    board.bind(identity, name)
    return identity
'''


@unittest.skipUnless(SOURCE and PYTHON, 'Set Coconut Hermes paths for native integration checks.')
class ArchiveSettledTests(unittest.TestCase):
    def run_native(self, program):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([PYTHON, '-c', PRELUDE + textwrap.dedent(program), home],
                env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[1]), SOURCE))),
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_waiting_native_task_is_archived_and_its_dependent_starts(self):
        self.run_native('''
            parent = native('parent')
            child = native('child', parents=[parent])
            assert kb.get_task(conn, child).status == 'todo', kb.get_task(conn, child).status
            assert board.archive_settled(parent) is True
            assert kb.get_task(conn, parent).status == 'archived'
            kb.recompute_ready(conn)
            assert kb.get_task(conn, child).status == 'ready', kb.get_task(conn, child).status
        ''')

    def test_a_running_or_final_native_task_is_left_alone(self):
        self.run_native('''
            running = native('running')
            assert kb.claim_task(conn, running) is not None
            assert board.archive_settled(running) is False
            assert kb.get_task(conn, running).status == 'running'
            done = native('done')
            assert kb.complete_task(conn, done, summary='Finished.')
            assert board.archive_settled(done) is False
            assert kb.get_task(conn, done).status == 'done'
        ''')

    def test_a_todo_task_waiting_on_a_stuck_parent_is_archived(self):
        self.run_native('''
            parent = native('parent')
            child = native('child', parents=[parent])
            assert board.archive_settled(child) is True
            assert kb.get_task(conn, child).status == 'archived'
        ''')


@unittest.skipUnless(SOURCE and PYTHON, 'Set Coconut Hermes paths for native integration checks.')
class FinishAtSourceTests(ArchiveSettledTests):
    """A finished Coconut request must not leave its native task behind."""

    def test_a_plain_request_that_finished_archives_its_waiting_native_task_at_the_next_sync(self):
        self.run_native('''
            parent = native('parent')
            child = native('child', parents=[parent])
            for identity, name in ((parent, 'parent'), (child, 'child')):
                row = dict(id=name, state='done', input=name, bot_id='care', title=name, priority=10, workspace=str(home / name),
                           workspace_changed=False, native_id=None)
                board.sync([row])
                assert kb.get_task(conn, identity).status == 'archived', (name, kb.get_task(conn, identity).status)
        ''')

    def test_a_task_that_returns_to_ready_after_the_done_state_was_synced_is_archived(self):
        # The sync skips a row whose intent did not change. A dependency wait can move the task back to ready later.
        self.run_native('''
            from coconut.kanban_runtime import fingerprint
            identity = native('turn')
            row = dict(id='turn', state='done', input='turn', bot_id='care', title='turn', priority=10,
                       workspace=str(home / 'turn'), workspace_changed=False, native_id=None)
            conn.execute('UPDATE coconut_bindings SET intent=? WHERE external_id=?',
                         (fingerprint({'state': 'done', 'input': 'turn'}), 'turn'))
            assert kb.get_task(conn, identity).status == 'ready'
            board.sync([row])
            assert kb.get_task(conn, identity).status == 'archived', kb.get_task(conn, identity).status
        ''')

    def test_an_outcome_a_review_and_a_running_task_stay_as_they_are(self):
        self.run_native('''
            outcome = native('outcome')
            row = dict(id='outcome', state='done', input='outcome', bot_id='care', title='outcome', priority=10,
                       workspace=str(home / 'outcome'), workspace_changed=False, native_id=outcome)
            board.sync([row])
            assert kb.get_task(conn, outcome).status == 'ready', kb.get_task(conn, outcome).status
            running = native('running')
            assert kb.claim_task(conn, running) is not None
            board.sync([dict(row, id='running', native_id=None, input='running')])
            assert kb.get_task(conn, running).status == 'running'
        ''')

    def test_a_finish_that_native_completion_refuses_archives_the_task(self):
        self.run_native('''
            identity = native('turn')
            # A plain request is bound without an outcome record. bind() adds one for an outcome.
            conn.execute('DELETE FROM coconut_outcomes WHERE native_id=?', (identity,))
            task = kb.claim_task(conn, identity)
            # A parent that is still open, as when a worker or a recovery linked one during the turn.
            open_parent = native('open')
            with kb.write_txn(conn):
                conn.execute('INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)', (open_parent, identity))
            assert kb.complete_task(conn, identity, summary='Complete.', expected_run_id=task.current_run_id) is False
            assert board.finish(identity, task.current_run_id, 'done', 'Complete.') is True
            assert kb.get_task(conn, identity).status == 'archived', kb.get_task(conn, identity).status
        ''')


class SettleSchedulerTests(unittest.TestCase):
    def service(self, tasks, natives, running=(), held=(), paused=False):
        calls = []
        service = SimpleNamespace(kanban=SimpleNamespace(tasks=natives, call=calls.append), lock=MagicMock(), db=MagicMock(),
                                  running={i: None for i in running}, context_threads={},
                                  _task_paused=lambda task: paused, _automatic_work_is_paused=lambda task: False)
        def execute(statement, values):
            wanted = set(json.loads(values[0]))
            if 'FROM decisions' in statement:
                return [(identity,) for identity in held if identity in wanted]
            return [(json.dumps(t),) for t in tasks if t['id'] in wanted]
        service.db.execute.side_effect = execute
        service.calls = calls
        return service

    def ago(self, seconds):
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()

    def settled(self, service):
        NativeKanban._settle_native_orphans(service)
        return sorted(call['id'] for call in service.calls)

    def test_only_a_long_finished_request_is_settled(self):
        old, new = self.ago(SETTLE_SECONDS + 60), self.ago(60)
        natives = {name: dict(id='t_' + name, status='ready') for name in ('done', 'cancelled', 'fresh', 'failed', 'live', 'active')}
        tasks = [dict(id='done', status='completed', updated_at=old), dict(id='cancelled', status='cancelled', updated_at=old),
                 dict(id='fresh', status='completed', updated_at=new), dict(id='failed', status='failed', updated_at=old),
                 dict(id='live', status='completed', updated_at=old), dict(id='active', status='queued', updated_at=old)]
        service = self.service(tasks, natives, running=['live'])
        self.assertEqual(self.settled(service), ['t_cancelled', 't_done'])
        self.assertTrue(all(call['op'] == 'archive_settled' for call in service.calls))

    def test_the_mechanisms_of_coconut_keep_their_tasks(self):
        old = self.ago(SETTLE_SECONDS + 60)
        natives = {'retry': dict(id='t_retry', status='ready'), 'check': dict(id='t_check', status='triage'),
                   'input': dict(id='t_input', status='blocked', block_kind='needs_input'),
                   'capability': dict(id='t_capability', status='triage', block_kind='capability'),
                   'parent': dict(id='t_parent', status='todo', dependencies=[{'id': 't_open'}]),
                   'card': dict(id='t_card', status='ready'), 'plain': dict(id='t_plain', status='triage')}
        tasks = [dict(id='retry', status='completed', updated_at=old, _native_outcome_id='t_retry'),
                 dict(id='check', status='completed', updated_at=old, _block_check=dict(attempt=1)),
                 dict(id='input', status='completed', updated_at=old), dict(id='capability', status='completed', updated_at=old),
                 dict(id='parent', status='completed', updated_at=old), dict(id='card', status='completed', updated_at=old),
                 dict(id='plain', status='completed', updated_at=old, _native_outcome_id='t_plain')]
        service = self.service(tasks, natives, held=['card'])
        # Only the triage task of an outcome that no block check or card holds goes.
        self.assertEqual(self.settled(service), ['t_plain'])

    def test_a_paused_request_keeps_its_native_task(self):
        old = self.ago(SETTLE_SECONDS + 60)
        service = self.service([dict(id='a', status='completed', updated_at=old)], {'a': dict(id='t_a', status='ready')}, paused=True)
        self.assertEqual(self.settled(service), [])

    def test_a_native_task_that_is_running_or_final_is_not_read(self):
        service = self.service([], {'a': dict(id='t_a', status='running'), 'b': dict(id='t_b', status='done')})
        NativeKanban._settle_native_orphans(service)
        service.db.execute.assert_not_called()

    def test_one_pass_settles_a_bounded_batch(self):
        old = self.ago(SETTLE_SECONDS + 60)
        names = [f'n{i}' for i in range(SETTLE_BATCH + 10)]
        service = self.service([dict(id=n, status='completed', updated_at=old) for n in names],
                               {n: dict(id='t_' + n, status='todo') for n in names})
        self.assertEqual(len(self.settled(service)), SETTLE_BATCH)


if __name__ == '__main__':
    unittest.main()
