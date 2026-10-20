"""Run with COCONUT_HERMES_ROOT/PYTHON. A finished Coconut request must not leave a native task that blocks its dependents."""
import json
import time
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


REKIND = '''
def snapshot(name):
    return next(t for t in board.snapshot([name]) if t['external_id'] == name)

def rekinded(name):
    # A callback's run links its finished owner and files a dependency block. Hermes re-kinds it to an input block.
    owner = native(name + '-owner')
    assert kb.complete_task(conn, owner, summary='Owner finished.')
    callback = native(name)
    claimed = kb.claim_task(conn, callback)
    kb.link_tasks(conn, owner, callback, expected_child_run_id=claimed.current_run_id)
    assert kb.block_task(conn, callback, reason='Waiting for the owner', kind='dependency',
                         expected_run_id=claimed.current_run_id)
    task = kb.get_task(conn, callback)
    assert (task.status, task.block_kind, task.current_run_id) == ('blocked', 'needs_input', None), task
    return callback, claimed.current_run_id
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

    def test_a_rekinded_callback_block_carries_its_proof_and_completes_with_its_result(self):
        # The callback linked itself to an owner that finished moments earlier, then filed a dependency block.
        self.run_native(REKIND + textwrap.dedent('''
            callback, run = rekinded('callback')
            event = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
                                 (callback,)).fetchone()[0]
            # Comments and links keep the block. They do not end its proof.
            kb.add_comment(conn, callback, 'default', 'Waiting for the owner.')
            proof = snapshot('callback')['block_rekind']
            assert proof == dict(event_id=event, run_id=run, requested_kind='dependency', rekind_reason='no_open_parent'), proof
            assert board.archive_settled(callback, 'Complete.') is False
            assert board.archive_settled(callback, 'Complete.', event + 1) is False
            assert kb.get_task(conn, callback).status == 'blocked'
            assert board.archive_settled(callback, 'Complete.', event) is True
            assert kb.get_task(conn, callback).status == 'done', kb.get_task(conn, callback).status
            assert kb.latest_summary(conn, callback) == 'Complete.', kb.latest_summary(conn, callback)
        '''))

    def test_a_deliberate_input_or_capability_hold_has_no_proof_and_stays(self):
        self.run_native(REKIND + textwrap.dedent('''
            for kind in ('needs_input', 'capability'):
                held = native('held-' + kind)
                claimed = kb.claim_task(conn, held)
                assert kb.block_task(conn, held, reason='A person must answer', kind=kind, expected_run_id=claimed.current_run_id)
                task = kb.get_task(conn, held)
                assert task.block_kind == kind, task
                assert snapshot('held-' + kind)['block_rekind'] is None
                event = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
                                     (held,)).fetchone()[0]
                for proof in (None, event):
                    assert board.archive_settled(held, 'Complete.', proof) is False
                assert kb.get_task(conn, held).status == task.status
            # A newer hold after a re-kind ends the old proof.
            callback, run = rekinded('later')
            stale = snapshot('later')['block_rekind']['event_id']
            assert kb.unblock_task(conn, callback)
            claimed = kb.claim_task(conn, callback)
            assert kb.block_task(conn, callback, reason='A person must answer', kind='needs_input',
                                 expected_run_id=claimed.current_run_id)
            held = kb.get_task(conn, callback)
            # The second input block of the same kind is a recurrence, which Hermes may move to triage for a person.
            assert held.status in ('blocked', 'triage') and held.block_kind == 'needs_input', held
            assert snapshot('later')['block_rekind'] is None
            assert board.archive_settled(callback, 'Complete.', stale) is False
            assert kb.get_task(conn, callback).status == held.status
            # Any lifecycle event after the re-kind ends its proof, even without a new block.
            import time
            callback, run = rekinded('touched')
            proof = snapshot('touched')['block_rekind']
            assert proof, proof
            conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, 'specified', '{}', ?)",
                         (callback, int(time.time())))
            assert snapshot('touched')['block_rekind'] is None
            assert board.archive_settled(callback, 'Complete.', proof['event_id']) is False
            assert kb.get_task(conn, callback).status == 'blocked'
        '''))

    def test_a_todo_task_waiting_on_a_stuck_parent_is_archived(self):
        self.run_native('''
            parent = native('parent')
            child = native('child', parents=[parent])
            assert board.archive_settled(child) is True
            assert kb.get_task(conn, child).status == 'archived'
        ''')


class SettleSchedulerTests(unittest.TestCase):
    def service(self, tasks, natives, running=(), held=(), paused=False, contexts=(), threads=(), executing=()):
        calls = []
        service = SimpleNamespace(kanban=SimpleNamespace(tasks=natives, call=calls.append), lock=MagicMock(), db=MagicMock(),
                                  running={i: None for i in running}, context_threads={i: None for i in threads},
                                  _task_paused=lambda task: paused, _automatic_work_is_paused=lambda task: False)
        def execute(statement, values):
            wanted = set(json.loads(values[0]))
            if 'FROM decisions' in statement:
                return [(identity,) for identity in held if identity in wanted]
            if 'FROM native_receipts' in statement:
                return [(identity,) for identity in executing if identity in wanted]
            if 'FROM contexts' in statement:
                return [(json.dumps(c),) for c in contexts if c['id'] in wanted]
            return [(json.dumps(t),) for t in tasks if t['id'] in wanted]
        service.db.execute.side_effect = execute
        service._native_orphans = lambda slack=0: NativeKanban._native_orphans(service, slack)
        service._rekinded_callback = NativeKanban._rekinded_callback
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
        # A failed request is finished too. A fresh, a running and an active request are not.
        self.assertEqual(self.settled(service), ['t_cancelled', 't_done', 't_failed'])
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

    def test_only_a_proven_rekind_of_the_callbacks_own_run_settles_its_input_block(self):
        # Hermes turns a callback's dependency block into an input block when its parent already finished. The snapshot
        # carries that event as proof. Every other input or capability block is someone's hold and stays.
        from coconut.kanban_scheduler import SETTLE_PLAIN_SECONDS
        old, young = self.ago(SETTLE_PLAIN_SECONDS + 60), self.ago(60)
        proof = dict(event_id=7, run_id=41, requested_kind='dependency', rekind_reason='no_open_parent')
        callback = dict(status='completed', updated_at=old, summary='Complete.', _is_return=True,
                        _assignment_task_id='root')
        blocked = dict(status='blocked', block_kind='needs_input', block_rekind=proof)
        cases = {
            'rekind': ({}, {}, True),
            'explicit input': ({}, dict(block_rekind=None), False),
            'capability': ({}, dict(status='triage', block_kind='capability', block_rekind=None), False),
            'capability with proof': ({}, dict(block_kind='capability'), False),
            'triage with proof': ({}, dict(status='triage'), False),
            'other run': ({}, dict(block_rekind=dict(proof, run_id=42)), False),
            'other kind': ({}, dict(block_rekind=dict(proof, requested_kind='needs_input')), False),
            'no reason': ({}, dict(block_rekind=dict(proof, rekind_reason=None)), False),
            'no event': ({}, dict(block_rekind=dict(proof, event_id=None)), False),
            'other card': ({'claim': 't_elsewhere'}, {}, False),
            'failed': ({'status': 'failed'}, {}, False),
            'cancelled': ({'status': 'cancelled'}, {}, False),
            'outcome': ({'_native_outcome_id': 't_outcome'}, {}, False),
            'request': ({'_is_return': False, '_assignment_task_id': None}, {}, False),
            'young': ({'updated_at': young}, {}, False),
            'parent': ({}, dict(dependencies=[{'id': 't_open'}]), False),
            'held': ({}, {}, False),
            'live': ({}, {}, False),
            'unknown tool': ({}, {}, False),
        }
        tasks, natives = [], {}
        for name, (changes, card, _) in cases.items():
            claim = dict(native_id=changes.pop('claim', 't_' + name), external_id=name, run_id=41)
            tasks.append(dict(callback, id=name, _kanban_claim=claim, **changes))
            natives[name] = dict(blocked, id='t_' + name, **card)
        service = self.service(tasks, natives, held=['held'], running=['live'], executing=['unknown tool'])
        self.assertEqual(self.settled(service), ['t_rekind'])
        # The adapter closes the block only while the event that Coconut read is still its proof.
        self.assertEqual(service.calls, [{'op': 'archive_settled', 'id': 't_rekind', 'summary': 'Complete.', 'rekind_event': 7}])
        # A context keeps its input block, and a paused callback keeps its task.
        context = self.service([], {'c': dict(blocked, id='t_c', created_at=time.time() - SETTLE_PLAIN_SECONDS - 60)},
                               contexts=[dict(id='c', status='completed', _is_return=True, _kanban_claim=dict(native_id='t_c', run_id=41))])
        self.assertEqual(self.settled(context), [])
        paused = self.service([dict(callback, id='p', _kanban_claim=dict(native_id='t_p', run_id=41))],
                              {'p': dict(blocked, id='t_p')}, paused=True)
        self.assertEqual(self.settled(paused), [])

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

    def test_a_failed_request_is_settled_like_a_finished_one(self):
        from coconut.kanban_scheduler import SETTLE_PLAIN_SECONDS
        minutes, day = self.ago(SETTLE_PLAIN_SECONDS + 60), self.ago(SETTLE_SECONDS + 60)
        natives = {name: dict(id='t_' + name, status='blocked') for name in ('plain', 'young', 'outcome', 'owned')}
        tasks = [dict(id='plain', status='failed', updated_at=minutes, summary='The worker could not start.'),
                 dict(id='young', status='failed', updated_at=self.ago(60)),
                 dict(id='outcome', status='failed', updated_at=minutes, _native_outcome_id='t_outcome'),
                 dict(id='owned', status='failed', updated_at=day, _native_outcome_id='t_owned', _block_check=dict(attempt=1))]
        service = self.service(tasks, natives)
        # A plain failed request goes after minutes, an outcome after a day, and a block check keeps its task.
        self.assertEqual(self.settled(service), ['t_plain'])
        # A failed request did not succeed, so it never passes its summary as a result.
        self.assertEqual(service.calls, [{'op': 'archive_settled', 'id': 't_plain', 'summary': None}])

    def test_a_finished_decision_context_is_settled_and_a_live_one_is_not(self):
        from coconut.kanban_scheduler import SETTLE_PLAIN_SECONDS
        old, young = time.time() - SETTLE_PLAIN_SECONDS - 60, time.time() - 60
        names = ('failed', 'done', 'cancelled', 'queued', 'live', 'young')
        natives = {name: dict(id='t_' + name, status='blocked', created_at=young if name == 'young' else old) for name in names}
        # A stored context has no dates, so the age comes from its native task.
        contexts = [dict(id='failed', status='failed', error_code='decision_refresh_incomplete'),
                    dict(id='done', status='completed'), dict(id='cancelled', status='cancelled'),
                    dict(id='queued', status='queued'), dict(id='live', status='failed'), dict(id='young', status='failed')]
        service = self.service([], natives, contexts=contexts, threads=['live'])
        self.assertEqual(self.settled(service), ['t_cancelled', 't_done', 't_failed'])

    def test_a_context_is_settled_even_when_its_task_is_paused(self):
        from coconut.kanban_scheduler import SETTLE_PLAIN_SECONDS
        service = self.service([], {'c': dict(id='t_c', status='blocked', created_at=time.time() - SETTLE_PLAIN_SECONDS - 60)},
                               paused=True, contexts=[dict(id='c', status='failed')])
        self.assertEqual(self.settled(service), ['t_c'])

    def test_a_context_that_ran_long_waits_after_its_last_heartbeat(self):
        from coconut.kanban_scheduler import SETTLE_PLAIN_SECONDS
        started, beat = time.time() - 3 * SETTLE_PLAIN_SECONDS, time.time() - 60
        natives = {'long': dict(id='t_long', status='blocked', created_at=started, last_heartbeat_at=beat),
                   'quiet': dict(id='t_quiet', status='blocked', created_at=started, last_heartbeat_at=None)}
        service = self.service([], natives, contexts=[dict(id='long', status='failed'), dict(id='quiet', status='failed')])
        # The long turn ended a minute ago. The quiet one has no heartbeat, so its start is all we know.
        self.assertEqual(self.settled(service), ['t_quiet'])

    def test_a_context_whose_native_task_has_a_null_date_is_left_alone(self):
        service = self.service([], {'c': dict(id='t_c', status='blocked', created_at=None)}, contexts=[dict(id='c', status='failed')])
        self.assertEqual(self.settled(service), [])

    def test_a_context_whose_native_task_has_no_date_is_left_alone(self):
        service = self.service([], {'c': dict(id='t_c', status='blocked')}, contexts=[dict(id='c', status='failed')])
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

    def test_a_task_that_survives_the_settle_pass_is_reported(self):
        # The dispatcher settles before it reports. The archive call here changes nothing, as when it fails.
        from coconut.kanban_scheduler import ORPHAN_ALERT_SLACK_SECONDS
        late = self.ago(SETTLE_SECONDS + ORPHAN_ALERT_SLACK_SECONDS + 60)
        service = self.service([dict(id='late', status='completed', updated_at=late, _native_outcome_id='t_late')],
                               {'late': dict(id='t_late', status='triage')})
        NativeKanban._settle_native_orphans(service)
        NativeKanban._report_native_disagreements(service)
        service._orphan_report_after = 0
        NativeKanban._settle_native_orphans(service)
        with self.assertLogs('coconut.kanban_scheduler', level='WARNING') as logged:
            NativeKanban._report_native_disagreements(service)
        self.assertEqual([c['id'] for c in service.calls], ['t_late', 't_late'])
        self.assertIn('(t_late)', logged.output[0])

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
