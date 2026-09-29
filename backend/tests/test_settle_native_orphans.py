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

    def test_a_ready_task_completes_with_the_result_of_its_request_and_a_todo_task_is_archived(self):
        self.run_native('''
            ready = native('ready')
            assert board.archive_settled(ready, 'Ready to review.') is True
            task = kb.get_task(conn, ready)
            # A successor and a routine report read the result of a done task.
            assert task.status == 'done', task.status
            assert kb.latest_summary(conn, ready) == 'Ready to review.', kb.latest_summary(conn, ready)
            parent = native('parent')
            child = native('child', parents=[parent])
            assert board.archive_settled(child, 'Ready to review.') is True
            assert kb.get_task(conn, child).status == 'archived'
        ''')

    def test_a_todo_task_waiting_on_a_stuck_parent_is_archived(self):
        self.run_native('''
            parent = native('parent')
            child = native('child', parents=[parent])
            assert board.archive_settled(child) is True
            assert kb.get_task(conn, child).status == 'archived'
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
        service._native_orphans = lambda slack=0: NativeKanban._native_orphans(service, slack)
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

    def test_a_plain_request_settles_after_minutes_and_an_outcome_after_a_day(self):
        from coconut.kanban_scheduler import SETTLE_PLAIN_SECONDS
        recent = self.ago(SETTLE_PLAIN_SECONDS + 60)
        natives = {'plain': dict(id='t_plain', status='triage'), 'young': dict(id='t_young', status='triage'),
                   'outcome': dict(id='t_outcome', status='triage')}
        tasks = [dict(id='plain', status='completed', updated_at=recent, summary='Complete.'),
                 dict(id='young', status='completed', updated_at=self.ago(60)),
                 dict(id='outcome', status='completed', updated_at=recent, _native_outcome_id='t_outcome')]
        service = self.service(tasks, natives)
        self.assertEqual(self.settled(service), ['t_plain'])
        # The result of the request goes with the call, so a done task keeps it.
        self.assertEqual(service.calls, [{'op': 'archive_settled', 'id': 't_plain', 'summary': 'Complete.'}])

    def test_a_cancelled_request_is_archived_and_never_completed(self):
        old = self.ago(SETTLE_SECONDS + 60)
        service = self.service([dict(id='a', status='cancelled', updated_at=old, summary='Cancelled by you.'),
                                dict(id='b', status='completed', updated_at=old, summary='Complete.')],
                               {'a': dict(id='t_a', status='ready'), 'b': dict(id='t_b', status='ready')})
        self.assertEqual(self.settled(service), ['t_a', 't_b'])
        summaries = {call['id']: call['summary'] for call in service.calls}
        self.assertEqual(summaries, {'t_a': None, 't_b': 'Complete.'})

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


    def report(self, service):
        # The first report only lists. A task must stay on the list through a second report.
        NativeKanban._report_native_disagreements(service)
        service._orphan_report_after = 0
        with self.assertLogs('coconut.kanban_scheduler', level='WARNING') as logged:
            NativeKanban._report_native_disagreements(service)
        return logged.output[0]

    def test_the_report_names_a_task_the_settle_pass_should_have_archived(self):
        from coconut.kanban_scheduler import ORPHAN_ALERT_SLACK_SECONDS
        late = self.ago(SETTLE_SECONDS + ORPHAN_ALERT_SLACK_SECONDS + 60)
        service = self.service([dict(id='late', status='completed', updated_at=late, _native_outcome_id='t_late'),
                                dict(id='due', status='completed', updated_at=self.ago(SETTLE_SECONDS + 60), _native_outcome_id='t_due'),
                                dict(id='held', status='completed', updated_at=late, _native_outcome_id='t_held')],
                               {'late': dict(id='t_late', status='triage'), 'due': dict(id='t_due', status='triage'),
                                'held': dict(id='t_held', status='triage'), 'x': dict(id='t_x', status='todo', unbound=True)},
                               held=['held'])
        line = self.report(service)
        # A task that is only due is the work of the next settle pass, and a held card keeps its task.
        # A task with no request is always reported.
        self.assertIn('1 finished requests keep a waiting native task (t_late)', line)
        self.assertIn('1 native tasks have no request (t_x)', line)

    def test_the_report_is_silent_when_both_lifecycles_agree(self):
        service = self.service([dict(id='a', status='completed', updated_at=self.ago(60))],
                               {'a': dict(id='t_a', status='triage'), 'b': dict(id='t_b', status='done', unbound=True)})
        NativeKanban._report_native_disagreements(service)
        service._orphan_report_after = 0
        with self.assertNoLogs('coconut.kanban_scheduler', level='WARNING'):
            NativeKanban._report_native_disagreements(service)

    def test_a_task_that_leaves_before_the_second_report_is_not_reported(self):
        # The settle pass archived it, or adoption bound it, after the first report.
        from coconut.kanban_scheduler import ORPHAN_ALERT_SLACK_SECONDS
        late = self.ago(SETTLE_SECONDS + ORPHAN_ALERT_SLACK_SECONDS + 60)
        natives = {'late': dict(id='t_late', status='triage'), 'x': dict(id='t_x', status='todo', unbound=True)}
        service = self.service([dict(id='late', status='completed', updated_at=late, _native_outcome_id='t_late')], natives)
        NativeKanban._report_native_disagreements(service)
        natives['late']['status'] = 'archived'
        natives['x']['status'] = 'done'
        service._orphan_report_after = 0
        with self.assertNoLogs('coconut.kanban_scheduler', level='WARNING'):
            NativeKanban._report_native_disagreements(service)

    def test_a_failed_read_does_not_stop_the_pass(self):
        service = self.service([], {})
        service._native_orphans = MagicMock(side_effect=RuntimeError('store is busy'))
        with self.assertLogs('coconut.kanban_scheduler', level='ERROR'):
            NativeKanban._report_native_disagreements(service)

    def test_the_report_runs_once_in_its_interval(self):
        service = self.service([], {'x': dict(id='t_x', status='todo', unbound=True)})
        self.report(service)
        with self.assertNoLogs('coconut.kanban_scheduler', level='WARNING'):
            NativeKanban._report_native_disagreements(service)


if __name__ == '__main__':
    unittest.main()
