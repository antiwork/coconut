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
from unittest.mock import MagicMock, patch

from coconut.kanban_scheduler import NativeKanban, SETTLE_BATCH, SETTLE_SECONDS
import test_native_kanban as fixtures
from hermes_env import needs_hermes, PYTHON, SOURCE


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


@needs_hermes
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

    def test_a_stale_rekind_proof_never_closes_a_card_that_was_unblocked_after_the_snapshot(self):
        # Coconut read the proof, then the card left its block before the settle call arrived. The card must keep its
        # state and result and still start fresh work, whatever status the unblock gave it.
        self.run_native(REKIND + textwrap.dedent('''
            def stale(name):
                callback, run = rekinded(name)
                proof = snapshot(name)['block_rekind']
                assert proof and proof['run_id'] == run, proof
                assert kb.unblock_task(conn, callback)
                return callback, proof['event_id']

            def refused(callback, event):
                before = kb.get_task(conn, callback)
                summary = kb.latest_summary(conn, callback)
                assert snapshot(callback_names[callback])['block_rekind'] is None
                assert board.archive_settled(callback, 'Complete.', event) is False
                after = kb.get_task(conn, callback)
                assert (after.status, after.result, after.completed_at) == (before.status, before.result, before.completed_at), after
                assert kb.latest_summary(conn, callback) == summary
                return after

            callback_names = {}
            ready, event = stale('ready')
            callback_names[ready] = 'ready'
            task = refused(ready, event)
            assert task.status == 'ready', task
            claimed = kb.claim_task(conn, ready)
            assert claimed is not None and claimed.status == 'running', claimed

            scheduled, event = stale('scheduled')
            callback_names[scheduled] = 'scheduled'
            assert kb.schedule_task(conn, scheduled, reason='Waiting for its time')
            assert refused(scheduled, event).status == 'scheduled'
            assert kb.unblock_task(conn, scheduled)
            assert kb.claim_task(conn, scheduled) is not None

            waiting, event = stale('waiting')
            callback_names[waiting] = 'waiting'
            parent = native('waiting-parent')
            kb.link_tasks(conn, parent, waiting)
            kb.recompute_ready(conn)
            assert refused(waiting, event).status == 'todo', kb.get_task(conn, waiting)
        '''))

    def test_a_released_block_completes_without_a_proof_and_a_new_block_after_the_snapshot_refuses(self):
        # Hermes keeps the kind of a block through the unblock. A ready task holds nothing, so the settle call has no
        # proof to send. When the task is blocked again before the call arrives, the new block is a hold.
        self.run_native(REKIND + textwrap.dedent('''
            def released(name, kind):
                if kind == 'dependency':
                    callback, run = rekinded(name)
                else:
                    callback = native(name)
                    claimed = kb.claim_task(conn, callback)
                    assert kb.block_task(conn, callback, reason='A person must answer', kind=kind,
                                         expected_run_id=claimed.current_run_id)
                assert kb.unblock_task(conn, callback)
                card = snapshot(name)
                assert (card['status'], card['block_kind'], card['block_rekind']) == (
                    'ready', 'capability' if kind == 'capability' else 'needs_input', None), card
                return callback

            for kind in ('dependency', 'needs_input', 'capability'):
                callback = released('released-' + kind, kind)
                assert board.archive_settled(callback, 'Complete.') is True
                assert kb.get_task(conn, callback).status == 'done', kb.get_task(conn, callback)
                assert kb.latest_summary(conn, callback) == 'Complete.', kb.latest_summary(conn, callback)

                again = released('again-' + kind, kind)
                claimed = kb.claim_task(conn, again)
                assert kb.block_task(conn, again, reason='A person must answer', kind='needs_input',
                                     expected_run_id=claimed.current_run_id)
                held = kb.get_task(conn, again)
                assert held.status in ('blocked', 'triage') and held.block_kind == 'needs_input', held
                assert board.archive_settled(again, 'Complete.') is False
                after = kb.get_task(conn, again)
                assert (after.status, after.result, after.completed_at) == (held.status, held.result, held.completed_at), after
        '''))

    def test_a_todo_task_waiting_on_a_stuck_parent_is_archived(self):
        self.run_native('''
            parent = native('parent')
            child = native('child', parents=[parent])
            assert board.archive_settled(child) is True
            assert kb.get_task(conn, child).status == 'archived'
        ''')


@needs_hermes
class ReleasedBlockDispatcherTests(unittest.TestCase):
    """The service dispatcher settles a finished request whose native task was blocked during its run, then unblocked."""
    setUp = fixtures.NativeKanbanTests.setUp
    write = fixtures.NativeKanbanTests.write
    task = fixtures.NativeKanbanTests.task
    tick = fixtures.NativeKanbanTests.tick
    settle = fixtures.NativeKanbanTests.settle
    native_program = fixtures.NativeKanbanTests.native_program

    def card(self, request):
        self.service.kanban.call({'op': 'snapshot'})
        return self.service.kanban.tasks[request['id']]

    @staticmethod
    def identity(request):
        return {key: value for key, value in request.items()
                if key in ('id', 'bot_id', 'title', 'status', 'summary', 'created_at', 'updated_at', '_kanban_claim')
                or 'workspace' in key}

    def blocked_during_its_run(self, text, kind):
        # The request completes while its turn thread is still alive, so the dispatcher's reconciliation leaves the
        # claim, and the sync records the result while the card still runs. The worker's own run then files a block
        # before the claim is finished, so no finish or sync can complete the card later.
        import threading
        ended = threading.Event()
        self.addCleanup(ended.set)
        def provider(task, bot, history, emit):
            self.provider.started.append(task['id'])
            emit('text_delta', {'text': 'Checked.'})
            emit('completed', {'summary': 'Complete.'})
            ended.wait(30)
        self.provider.start_task = provider
        request = self.task(text)
        self.settle(lambda: self.service._get('tasks', request['id'])['status'] == 'completed')
        self.tick()
        card = self.card(request)
        self.assertEqual(card['status'], 'running')
        self.native_program('''
kb, conn = board.kb, board.conn
callback, run, kind = sys.argv[2], int(sys.argv[3]), sys.argv[4]
if kind == 'dependency':
    # The parent that the run waits for has already finished, so Hermes re-kinds the block to an input block.
    parent = kb.create_task(conn, title='Finished parent', body='Fixture', assignee='default', tenant='care')
    assert kb.complete_task(conn, parent, summary='Parent finished.')
    kb.link_tasks(conn, parent, callback, expected_child_run_id=run)
assert kb.block_task(conn, callback, reason='Waiting', kind=kind, expected_run_id=run)
task = kb.get_task(conn, callback)
assert task.status == 'blocked' and task.block_kind == ('needs_input' if kind == 'dependency' else kind), task
''', card['id'], str(card['current_run_id']), kind)
        ended.set()
        self.service.running[request['id']].join(10)
        self.assertFalse(self.service.running[request['id']].is_alive())
        self.tick()
        # The finished request keeps a blocked card. The settle pass waits SETTLE_PLAIN_SECONDS before it reads it.
        blocked = self.card(request)
        self.assertEqual((blocked['status'], blocked['current_run_id']), ('blocked', None))
        return self.service._get('tasks', request['id']), card['id'], card['current_run_id']

    def test_a_finished_request_whose_block_was_released_settles_and_its_dependent_starts(self):
        # As in t_9617fcd3: the run re-kinded a dependency block, someone unblocked it to todo behind an open parent,
        # and the card became ready when that parent finished. Hermes keeps block_kind needs_input on the ready card.
        import coconut.kanban_scheduler as scheduler
        request, identity, run = self.blocked_during_its_run('Check the release', 'dependency')
        # The run's own dependency block became an input block because its parent had finished.
        event = json.loads(self.native_program('''
import json
row = board.conn.execute("SELECT run_id, payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
                         (sys.argv[2],)).fetchone()
print(json.dumps(dict(json.loads(row[1]), run_id=row[0])))
''', identity))
        self.assertEqual((event['run_id'], event['kind'], event['requested_kind'], event['rekind_reason']),
                         (run, 'needs_input', 'dependency', 'no_open_parent'))
        parent = self.native_program('''
kb, conn = board.kb, board.conn
callback = sys.argv[2]
parent = kb.create_task(conn, title='Open parent', body='Fixture', assignee='default', tenant='care')
kb.link_tasks(conn, parent, callback)
assert kb.unblock_task(conn, callback)
dependent = kb.create_task(conn, title='Dependent', body='Fixture', assignee='default', tenant='care', parents=[callback])
assert kb.get_task(conn, callback).status == 'todo', kb.get_task(conn, callback)
print(parent + ' ' + dependent)
''', identity)
        parent, dependent = parent.split()
        history = [m for m in self.service._all('messages') if m.get('task_id') == request['id']]
        with patch.object(scheduler, 'SETTLE_PLAIN_SECONDS', 0):
            # An open parent still holds the card.
            self.tick()
            card = self.card(request)
            self.assertEqual((card['status'], card['block_kind'], [p['id'] for p in card['dependencies']]),
                             ('todo', 'needs_input', [parent]))
            self.native_program('''
kb, conn = board.kb, board.conn
assert kb.complete_task(conn, sys.argv[2], summary='Parent finished.')
kb.recompute_ready(conn)
''', parent)
            card = self.card(request)
            self.assertEqual((card['status'], card['block_kind'], card['block_rekind'], card['current_run_id']),
                             ('ready', 'needs_input', None, None))
            before = self.service._get('tasks', request['id'])
            self.tick()
        card = self.card(request)
        # The card completes with the request's result, so its dependent can start.
        self.assertEqual((card['status'], card['outcome_summary']), ('done', request['summary']))
        self.assertEqual(self.native_program('''
board.kb.recompute_ready(board.conn)
print(board.kb.get_task(board.conn, sys.argv[2]).status)
''', dependent), 'ready')
        # The request itself is unchanged: same identity, owner, result, workspace and history.
        after = self.service._get('tasks', request['id'])
        self.assertEqual(after, before)
        self.assertEqual(self.identity(after), self.identity(request))
        self.assertEqual([m for m in self.service._all('messages') if m.get('task_id') == request['id']], history)

    def test_an_explicit_block_settles_only_after_an_unblock(self):
        import coconut.kanban_scheduler as scheduler
        released, held = {}, {}
        for kind in ('needs_input', 'capability'):
            released[kind] = self.blocked_during_its_run('Released ' + kind, kind)
            held[kind] = self.blocked_during_its_run('Held ' + kind, kind)
            self.native_program('assert board.kb.unblock_task(board.conn, sys.argv[2])', released[kind][1])
        with patch.object(scheduler, 'SETTLE_PLAIN_SECONDS', 0):
            self.tick()
        for kind in ('needs_input', 'capability'):
            request = released[kind][0]
            card = self.card(request)
            self.assertEqual((card['status'], card['outcome_summary']), ('done', request['summary']), kind)
            self.assertEqual(self.identity(self.service._get('tasks', request['id'])), self.identity(request))
            # A block that was never released is still someone's hold.
            request = held[kind][0]
            card = self.card(request)
            self.assertEqual((card['status'], card['block_kind']), ('blocked', kind), kind)
            self.assertEqual(self.identity(self.service._get('tasks', request['id'])), self.identity(request))


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

    def test_an_unblocked_ready_task_settles_although_hermes_keeps_its_old_block_kind(self):
        # Hermes keeps `block_kind` through an unblock, so a released input or capability block still names its kind
        # when the task is ready again. As in t_9617fcd3: a re-kinded dependency block, unblocked to todo, then ready
        # after its parent finished. Board.rekinded_block gives a ready task no proof, and the request was long done.
        from coconut.kanban_scheduler import SETTLE_PLAIN_SECONDS
        old, young = self.ago(SETTLE_PLAIN_SECONDS + 60), self.ago(60)
        request = dict(status='completed', updated_at=old, summary='Complete.')
        released = dict(status='ready', block_kind='needs_input', block_rekind=None)
        cases = {
            'input': ({}, {}, True),
            'capability': ({}, dict(block_kind='capability'), True),
            'callback': (dict(_is_return=True, _kanban_claim=dict(native_id='t_callback', run_id=41)), {}, True),
            'failed': (dict(status='failed'), {}, True),
            # A native retry owns a ready outcome, and the block check owns its record.
            'outcome': (dict(_native_outcome_id='t_outcome'), {}, False),
            'check': (dict(_block_check=dict(attempt=1)), {}, False),
            'young': (dict(updated_at=young), {}, False),
            'queued': (dict(status='queued'), {}, False),
            'parent': ({}, dict(dependencies=[{'id': 't_open'}]), False),
            'held': ({}, {}, False),
            'live': ({}, {}, False),
            'thread': ({}, {}, False),
            'unknown tool': ({}, {}, False),
            # Only a ready task was released. A todo or scheduled task becomes ready first, and blocks still hold.
            'todo': ({}, dict(status='todo'), False),
            'scheduled': ({}, dict(status='scheduled'), False),
            'blocked': ({}, dict(status='blocked'), False),
            'triage': ({}, dict(status='triage', block_kind='capability'), False),
        }
        tasks, natives = [], {}
        for name, (changes, card, _) in cases.items():
            tasks.append(dict(request, id=name, **changes))
            natives[name] = dict(released, id='t_' + name, **card)
        service = self.service(tasks, natives, held=['held'], running=['live'], threads=['thread'],
                               executing=['unknown tool'])
        self.assertEqual(self.settled(service), sorted('t_' + name for name, (_, _, settles) in cases.items() if settles))
        # The old block is not current, so no re-kind proof goes with the call. Board still rechecks the live task.
        self.assertTrue(all('rekind_event' not in call for call in service.calls), service.calls)
        self.assertEqual(next(call for call in service.calls if call['id'] == 't_input'),
                         {'op': 'archive_settled', 'id': 't_input', 'summary': 'Complete.'})
        # A paused request keeps its task.
        paused = self.service([dict(request, id='p')], {'p': dict(released, id='t_p')}, paused=True)
        self.assertEqual(self.settled(paused), [])

    def test_an_unknown_tool_effect_keeps_a_released_card_of_either_kind_until_its_result(self):
        # A tool call of the finished request that has no result yet may still have an effect, whatever kind of block
        # the ready card still names. The card settles once the receipt has its result.
        from coconut.kanban_scheduler import SETTLE_PLAIN_SECONDS
        old = self.ago(SETTLE_PLAIN_SECONDS + 60)
        for kind in ('needs_input', 'capability'):
            executing = ['request']
            service = self.service([dict(id='request', status='completed', updated_at=old, summary='Complete.')],
                                   {'request': dict(id='t_request', status='ready', block_kind=kind, block_rekind=None)},
                                   executing=executing)
            self.assertEqual(self.settled(service), [], kind)
            executing.clear()
            self.assertEqual(self.settled(service), ['t_request'], kind)
            self.assertEqual(service.calls, [{'op': 'archive_settled', 'id': 't_request', 'summary': 'Complete.'}], kind)

    def test_the_report_names_a_released_ready_task_that_was_not_settled(self):
        from coconut.kanban_scheduler import ORPHAN_ALERT_SLACK_SECONDS, SETTLE_PLAIN_SECONDS
        late = self.ago(SETTLE_PLAIN_SECONDS + ORPHAN_ALERT_SLACK_SECONDS + 60)
        service = self.service([dict(id='late', status='completed', updated_at=late)],
                               {'late': dict(id='t_late', status='ready', block_kind='needs_input', block_rekind=None)})
        self.assertIn('1 finished requests keep a waiting native task (t_late)', self.report(service))

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
