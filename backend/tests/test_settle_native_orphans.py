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


class SettleSchedulerTests(unittest.TestCase):
    def service(self, tasks, natives, running=()):
        calls = []
        service = SimpleNamespace(kanban=SimpleNamespace(tasks=natives, call=calls.append), lock=MagicMock(), db=MagicMock(),
                                  running={i: None for i in running}, context_threads={})
        wanted = [json.dumps(t) for t in tasks]
        service.db.execute.return_value = [(text,) for text in wanted]
        service.calls = calls
        return service

    def ago(self, seconds):
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()

    def test_only_a_long_finished_request_is_settled(self):
        old, new = self.ago(SETTLE_SECONDS + 60), self.ago(60)
        natives = {name: dict(id='t_' + name, status='ready') for name in ('done', 'cancelled', 'fresh', 'failed', 'live', 'active')}
        tasks = [dict(id='done', status='completed', updated_at=old), dict(id='cancelled', status='cancelled', updated_at=old),
                 dict(id='fresh', status='completed', updated_at=new), dict(id='failed', status='failed', updated_at=old),
                 dict(id='live', status='completed', updated_at=old), dict(id='active', status='queued', updated_at=old)]
        service = self.service(tasks, natives, running=['live'])
        NativeKanban._settle_native_orphans(service)
        self.assertEqual(sorted(call['id'] for call in service.calls), ['t_cancelled', 't_done'])
        self.assertTrue(all(call['op'] == 'archive_settled' for call in service.calls))

    def test_a_native_task_that_is_running_or_final_is_not_read(self):
        service = self.service([], {'a': dict(id='t_a', status='running'), 'b': dict(id='t_b', status='done')})
        NativeKanban._settle_native_orphans(service)
        service.db.execute.assert_not_called()

    def test_one_pass_settles_a_bounded_batch(self):
        old = self.ago(SETTLE_SECONDS + 60)
        names = [f'n{i}' for i in range(SETTLE_BATCH + 10)]
        service = self.service([dict(id=n, status='completed', updated_at=old) for n in names],
                               {n: dict(id='t_' + n, status='todo') for n in names})
        NativeKanban._settle_native_orphans(service)
        self.assertEqual(len(service.calls), SETTLE_BATCH)


if __name__ == '__main__':
    unittest.main()
