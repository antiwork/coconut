"""An ordinary assignment's continuation continues after Coconut's settle pass archives its scheduled card.

A requester delegates work to another bot. The assignment completes its input and its card. The requester continues
the same execution, and Board.sync gives the new input a successor card. That turn parks the card with kanban_schedule,
so Hermes ends its run as scheduled and the task completes. A day later the settle pass archives the card. The
assignment has no routine job, occurrence, or completion receipt, so the routine rule cannot reach the unfinished work.

Only that exact card continues: the successor of an authenticated continuation, holding the current authenticated
input, whose last run is the scheduled run of that input's claim. The original card, result, and receipts stay.
Native CI sets COCONUT_REQUIRE_NATIVE, and then a missing Hermes install fails the native tests instead of skipping them.
"""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4

from coconut.kanban_scheduler import SETTLE_SECONDS
from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
import legacy_work_sync
import test_archived_scheduled_routine_continuation as routine_fixtures
import test_native_kanban as native_fixtures
import hermes_env

setUpModule, tearDownModule = legacy_work_sync.scope()

REASON = routine_fixtures.REASON
stamp = routine_fixtures.stamp


class ScheduledAssignment:
    """A real Service whose ordinary delegated assignment was continued until the settle pass archived its card."""

    adapter = routine_fixtures.ScheduledOccurrence.adapter
    update = routine_fixtures.ScheduledOccurrence.update
    receipt = routine_fixtures.ScheduledOccurrence.receipt
    card = routine_fixtures.ScheduledOccurrence.card
    cancellations = routine_fixtures.ScheduledOccurrence.cancellations

    def setUp(self):
        self.store_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.store_tmp.cleanup)
        self.store = Service(self.store_tmp.name, FixtureProvider(self.store_tmp.name), start_workers=False)
        self.addCleanup(self.store.close)
        self.lead = self.store.mutate('POST', '/v1/bots', {'name': 'Lead'}, str(uuid4()), 'test-phone')
        self.dev = self.store.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'test-phone')
        self.runs, self.calls = {}, []
        self.store.kanban = Mock(tasks={})
        self.store.kanban.call.side_effect = self.adapter

    def working(self, bot, text):
        task = self.store.mutate('POST', '/v1/bots/' + bot['id'] + '/messages', {'text': text}, str(uuid4()), 'test-phone')
        return self.update(task['id'], status='working')

    def assignment(self, delegated=True, source=None, drain=True):
        """The assignment ends card t_original by run 26393, and its result returns.

        A delegated assignment comes from the requester, Lead. Otherwise the work source audit gave Desk the source item
        itself, as it does for an assigned issue, and Desk's own turns continue it.
        """
        if delegated:
            self.origin = self.working(self.lead, 'Validate the bank intake.')
            delivered = self.store._store_bot_message(self.origin['id'], dict(target_bot_id=self.dev['id'],
                                                                               text='Validate the bank details read-only.'))
            owner = self.store._get('tasks', delivered['task_id'])
            self.assertEqual(owner['source_bot_id'], self.lead['id'])
        else:
            owner = self.store._get('tasks', self.store.mutate('POST', '/v1/bots/' + self.dev['id'] + '/messages',
                {'text': 'Review the assigned bank intake issue.'}, str(uuid4()), 'test-phone')['id'])
            self.assertIsNone(owner.get('source_bot_id'))
        if source:
            owner = self.update(owner['id'], _source_assignment=dict(source))
        self.execution_dir = self.store.root / 'workspaces' / owner['id']
        self.execution_dir.mkdir(parents=True)
        owner = self.update(owner['id'], status='working', _execution_session_id=owner['id'],
                            _native_turn_id='turn-original', _conversation_epoch=self.store._setting('conversation_epoch'),
                            _kanban_claim=dict(native_id='t_original', external_id=owner['id'], input='turn-original',
                                               run_id=26393, claim_lock='host:1', workspace=str(self.execution_dir)),
                            _kanban_started=26393)
        self.receipt(owner['id'], 'kanban_complete', dict(task_id='t_original', summary='Validated the intake.'),
                     dict(ok=True, task_id='t_original', run_id=26393))
        self.store.emit(owner['id'], 'completed', dict(summary='Validated the intake.'))
        owner = self.store._get('tasks', owner['id'])
        self.assertEqual(owner['status'], 'completed')
        # A delegated result waits for the return drain, which a dispatcher pass runs first. Only then can work continue.
        self.assertEqual(bool(owner.get('_return_pending')), delegated)
        if drain:
            self.store._drain_returns()
            owner = self.store._get('tasks', owner['id'])
            self.assertFalse(owner.get('_return_pending'))
        self.store.kanban.tasks[owner['id']] = self.card(owner, 't_original', 'coconut:' + owner['id'],
                                                         input_id='turn-original')
        self.runs['t_original'] = [dict(id=26393, status='done', outcome='completed', ended=True)]
        self.requester = self.working(self.lead if delegated else self.dev, 'Check the validation.')
        return owner

    def resume(self, identity, message, caller=None):
        self.assertFalse(self.store.db.in_transaction, 'A continuation must not start inside another transaction.')
        return self.store.emit((caller or self.requester)['id'], 'work_continue', dict(task_id=identity, message=message))

    dispatch = routine_fixtures.ScheduledOccurrence.dispatch

    def schedule(self, owner):
        """The worker parks its card with kanban_schedule, the turn completes, and its result returns."""
        task = self.store._get('tasks', owner['id'])
        identity, run = task['_kanban_claim']['native_id'], task['_kanban_claim']['run_id']
        self.receipt(owner['id'], 'kanban_schedule', dict(task_id=identity, reason=REASON),
                     dict(ok=True, task_id=identity, status='scheduled', reason=REASON))
        self.store.kanban.tasks[owner['id']].update(status='scheduled', current_run_id=None, worker_pid=None,
                                                    claim_lock=None, last_activity_at=time.time() - 60)
        self.runs[identity][-1].update(status='scheduled', outcome='scheduled', ended=True)
        self.store.emit(owner['id'], 'completed', dict(summary='Complete.'))
        self.store._drain_returns()
        task = self.store._get('tasks', owner['id'])
        self.assertFalse(task.get('_return_pending'))
        return task

    settle = routine_fixtures.ScheduledOccurrence.settle

    def saved(self, delegated=True, source=None):
        """The first continuation made t_scheduled, a later one reused it, and both turns scheduled it."""
        owner = self.assignment(delegated, source)
        self.resume('t_original', 'Validate the bank details again.')
        first = self.store._get('tasks', owner['id'])['_native_turn_id']
        self.dispatch(owner, 't_scheduled', 26559, key='coconut-followup:' + owner['id'] + ':' + first)
        self.schedule(owner)
        self.resume('t_scheduled', 'Continue the bank validation.')
        self.dispatch(owner, 't_scheduled', 26630)
        scheduled = self.schedule(owner)
        self.assertEqual(scheduled['status'], 'completed')
        archived = self.settle(owner)
        self.assertEqual(archived, dict(scheduled, updated_at=archived['updated_at']))
        self.assertEqual(self.store.kanban.tasks[owner['id']]['idempotency_key'],
                         'coconut-followup:' + owner['id'] + ':' + first)
        return archived

    def assertRejected(self, owner, pattern='.', identity='t_scheduled', caller=None):
        before = self.store._get('tasks', owner['id'])
        with self.assertRaisesRegex(ServiceError, pattern):
            self.resume(identity, 'Finish the bank validation.', caller)
        self.assertEqual(self.store._get('tasks', owner['id']), before)


class ArchivedScheduledAssignmentAdmissionTests(ScheduledAssignment, unittest.TestCase):
    def deliveries(self, owner):
        return [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT data FROM deliveries WHERE json_extract(data,'$.source_task_id')=? ORDER BY rowid", (owner['id'],))]

    def test_saved_shape_continues_its_scheduled_work_in_the_same_execution(self):
        owner = self.saved()
        before = copy.deepcopy(owner)
        results = self.deliveries(owner)
        self.assertEqual(self.cancellations(owner), [])
        receipt = self.resume('t_scheduled', 'Finish the bank validation.')
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_scheduled', status='queued'))
        self.assertIn({'op': 'completion', 'id': 't_scheduled'}, self.calls)
        continued = self.store._get('tasks', owner['id'])
        for key in ('id', 'bot_id', 'session_id', 'thread_id', 'exchange_id', 'conversation_bot_id', 'source_bot_id',
                    'source_task_id', '_reply_to_task_id', '_execution_session_id', '_kanban_claim', '_native_outcome_id',
                    '_work_context_id'):
            self.assertEqual(continued.get(key), before.get(key), key)
        self.assertEqual(continued['status'], 'queued')
        self.assertNotEqual(continued['_native_turn_id'], before['_native_turn_id'])
        # An ordinary request gains no routine record. The routine rules still do not apply to it.
        from coconut.routine_coordinator import ROUTINE_PROVENANCE
        self.assertFalse(any(continued.get(field) for field in ROUTINE_PROVENANCE))
        self.assertEqual(self.store.native_routine_completion_input(continued), {})
        # A replay returns the saved receipt and changes nothing.
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.'), receipt)
        self.assertEqual(self.store._get('tasks', owner['id']), continued)
        # Every saved receipt and the original result stay.
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM native_receipts WHERE json_extract(data,'$.task_id')=? "
                                               "AND json_extract(data,'$.state')='completed'", (owner['id'],)).fetchone()[0], 3)
        self.assertEqual(self.deliveries(owner), results)
        self.assertEqual(self.runs['t_original'], [dict(id=26393, status='done', outcome='completed', ended=True)])
        # The next row names the archived card and the new input, in the execution's own workspace.
        row = self.store._kanban_row(continued, continued, 'ready', 10)
        self.assertEqual((row['native_id'], row['input'], row['previous_input'], row['workspace']),
                         ('t_scheduled', continued['_native_turn_id'], before['_native_turn_id'], str(self.execution_dir)))
        # Reconciliation before dispatch reads the archived card of the earlier input. It neither cancels nor completes.
        settled = [c for c in self.calls if c['op'] == 'archive_settled']
        self.store._reconcile_kanban()
        self.assertEqual(self.store._get('tasks', owner['id']), continued)
        self.assertEqual(self.cancellations(owner), [])
        self.assertEqual([c for c in self.calls if c['op'] == 'archive_settled'], settled)
        self.assertEqual(self.store.kanban.tasks[owner['id']]['status'], 'archived')

    def test_completed_assignment_waits_for_its_result_to_reach_the_requester(self):
        owner = self.assignment(drain=False)
        self.assertEqual(owner['_return_pending']['target_bot_id'], self.lead['id'])
        results = self.deliveries(owner)
        # The undelivered result holds the first continuation, and the refusal writes nothing.
        self.assertRejected(owner, 'previous result to be delivered', identity='t_original')
        self.assertEqual(self.deliveries(owner), results)
        self.store._drain_returns()
        delivered = self.store._get('tasks', owner['id'])
        self.assertFalse(delivered.get('_return_pending'))
        self.assertIn((self.lead['id'], 'Validated the intake.'), [(d['target_bot_id'], d['text']) for d in self.deliveries(owner)])
        self.assertTrue(any(t.get('_is_return') and t['bot_id'] == self.lead['id'] for t in self.store._all('tasks')))
        self.assertEqual(self.resume('t_original', 'Validate the bank details again.')['task_id'], owner['id'])

    def test_evicted_archived_card_is_read_back_from_the_adapter(self):
        owner = self.saved()
        # A periodic snapshot no longer lists the archived binding. The targeted snapshot and completion still read it.
        card = self.store.kanban.tasks.pop(owner['id'])
        adapter = self.adapter

        def board(request):
            if request['op'] == 'snapshot':
                self.assertEqual(request.get('external_ids'), [owner['id']])
                return {'tasks': [dict(card, external_id=owner['id'])]}
            if request['op'] == 'completion':
                self.store.kanban.tasks[owner['id']] = card
                try:
                    return adapter(request)
                finally:
                    self.store.kanban.tasks.pop(owner['id'])
            return adapter(request)
        self.store.kanban.call.side_effect = board
        receipt = self.resume('t_scheduled', 'Finish the bank validation.')
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_scheduled', status='queued'))
        self.assertIn({'op': 'snapshot', 'external_ids': [owner['id']]},
                      [call.args[0] for call in self.store.kanban.call.call_args_list])

    def test_owner_bot_can_continue_its_own_assignment(self):
        owner = self.saved()
        caller = self.working(self.dev, 'Finish the delegated validation.')
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.', caller)['task_id'], owner['id'])

    def test_successor_bound_before_claim_keeps_the_input_and_returns_to_the_requester(self):
        owner = self.saved()
        receipt = self.resume('t_scheduled', 'Finish the bank validation.')
        turn = self.store._get('tasks', owner['id'])['_native_turn_id']
        key = 'coconut-followup:' + owner['id'] + ':' + turn
        # Board.sync makes the successor, and the reconciler binds it before Hermes claims it.
        self.store.kanban.tasks[owner['id']] = self.card(owner, 't_next', key, status='ready', input_id=None)
        self.store._reconcile_kanban()
        queued = self.store._get('tasks', owner['id'])
        self.assertEqual((queued['status'], queued['_native_outcome_id'], queued['_native_turn_id']), ('queued', 't_next', turn))
        # The rebind keeps the accepted input. A replay returns its saved receipt and starts nothing new.
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.'), receipt)
        self.assertEqual(self.store._get('tasks', owner['id']), queued)
        self.dispatch(owner, 't_next', 26700)
        self.receipt(owner['id'], 'kanban_complete', dict(task_id='t_next', summary='Validated.'),
                     dict(ok=True, task_id='t_next', run_id=26700))
        self.store.emit(owner['id'], 'completed', dict(summary='Validated the bank details.'))
        self.store.kanban.tasks[owner['id']].update(status='done', current_run_id=None, worker_pid=None)
        self.runs['t_next'][-1].update(status='done', outcome='completed', ended=True)
        self.store._reconcile_kanban()
        self.store._drain_returns()
        done = self.store._get('tasks', owner['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['_native_return_prepared'], ['t_next', turn])
        # The result goes to the requester's route of the original assignment.
        result = self.deliveries(owner)[-1]
        self.assertEqual((result['target_bot_id'], result['text']), (self.lead['id'], 'Validated the bank details.'))
        self.assertEqual(done['_reply_to_task_id'], self.origin['id'])
        self.assertEqual(self.cancellations(owner), [])
        # A done card is no settled scheduled card. Its own continuation path applies.
        self.store.kanban.tasks[owner['id']].update(status='archived')
        self.assertRejected(done, identity='t_next')

    def test_repeated_scheduled_continuations_continue_in_order(self):
        owner = self.saved()
        identity = 't_scheduled'
        turns = [self.store._get('tasks', owner['id'])['_native_turn_id']]
        for index, run in enumerate((26700, 26800, 26900)):
            with self.subTest(round=index):
                receipt = self.resume(identity, 'Finish the bank validation, round %d.' % index)
                self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=identity, status='queued'))
                turn = self.store._get('tasks', owner['id'])['_native_turn_id']
                self.assertNotIn(turn, turns)
                turns.append(turn)
                successor = 't_next_%d' % index
                self.dispatch(owner, successor, run, key='coconut-followup:' + owner['id'] + ':' + turn)
                self.schedule(owner)
                settled = self.settle(owner)
                self.assertEqual(settled['status'], 'completed')
                self.assertEqual(self.cancellations(owner), [])
                # The earlier card stays archived with its own runs.
                self.assertEqual(self.runs[identity][-1]['outcome'], 'scheduled')
                identity = successor
        self.assertEqual(self.runs['t_original'], [dict(id=26393, status='done', outcome='completed', ended=True)])

    def test_archive_must_be_the_scheduled_run_of_the_current_input(self):
        owner = self.saved()
        runs = copy.deepcopy(self.runs['t_scheduled'])
        last = runs[-1]
        for change in ([dict(last, outcome='review_requested', status='review')], [dict(last, outcome='blocked', status='blocked')],
                       [dict(last, outcome='reclaimed', status='reclaimed')], [dict(last, outcome='completed', status='done')],
                       [dict(last, ended=False, outcome=None)], [dict(last, id=26631)],
                       [dict(runs[0], outcome='completed', status='done'), last], runs[:1], []):
            with self.subTest(runs=change):
                self.runs['t_scheduled'] = change
                self.assertRejected(owner, 'not blocked or completed')
        self.runs['t_scheduled'] = runs
        card = dict(self.store.kanban.tasks[owner['id']])
        for change in (dict(tenant=self.lead['id']), dict(created_by='human'), dict(current_run_id=26631),
                       dict(worker_pid=4343), dict(input_id='an-earlier-input'), dict(input_id=None),
                       dict(idempotency_key='coconut:' + owner['id']),
                       dict(idempotency_key='coconut-followup:another-task:' + card['idempotency_key'].rsplit(':', 1)[1]),
                       dict(idempotency_key='coconut-followup:' + owner['id'] + ':an-unauthenticated-input'),
                       dict(idempotency_key='coconut-outcome:work'),
                       # A newer card owns the execution now. The archived card is history.
                       dict(id='t_newer', status='ready'),
                       # The phone archives only cards that waited for seven days. This archive is not Coconut's.
                       dict(last_activity_at=time.time() - 8 * 86400), dict(last_activity_at=None)):
            with self.subTest(card=change):
                self.store.kanban.tasks[owner['id']] = card | change
                self.assertRejected(owner)
        self.store.kanban.tasks[owner['id']] = card
        claim = owner['_kanban_claim']
        for changes in (dict(_kanban_claim=dict(claim, run_id=26623)), dict(_kanban_claim=dict(claim, input='an-earlier-input')),
                        dict(_kanban_claim=dict(claim, run_id=str(claim['run_id']))),
                        dict(_kanban_claim=dict(claim, native_id='t_original')),
                        dict(_kanban_claim=dict(claim, external_id='another-task')), dict(_kanban_started=26623),
                        dict(_native_turn_id='an-unclaimed-turn'), dict(_native_outcome_id='t_original')):
            with self.subTest(task=changes):
                self.store._put('tasks', dict(owner, **changes))
                self.assertRejected(self.store._get('tasks', owner['id']))
        self.store._put('tasks', owner)
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.')['task_id'], owner['id'])

    def test_completion_read_must_match_the_card(self):
        owner = self.saved()
        adapter = self.adapter
        for change in (dict(id='t_other'), dict(tenant=self.lead['id']), dict(idempotency_key='coconut:' + owner['id']),
                       dict(status='scheduled'), dict(current_run_id=26631), dict(worker_pid=4343),
                       dict(input_id='an-earlier-input')):
            with self.subTest(change=change):
                self.store.kanban.call.side_effect = lambda request, change=change: (
                    {'card': adapter(request)['card'] | change} if request['op'] == 'completion' else adapter(request))
                self.assertRejected(owner)

        def silent(request):
            if request['op'] == 'completion':
                raise RuntimeError('The Hermes Kanban adapter stopped.')
            return adapter(request)
        self.store.kanban.call.side_effect = silent
        self.assertRejected(owner)
        self.store.kanban.call.side_effect = lambda request: {'card': None} if request['op'] == 'completion' else adapter(request)
        self.assertRejected(owner)
        self.store.kanban.call.side_effect = adapter
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.')['task_id'], owner['id'])

    def test_inputs_must_be_authenticated_continuations_of_this_task(self):
        owner = self.saved()
        rows = [(row[0], json.loads(row[1])) for row in self.store.db.execute(
            "SELECT id,data FROM deliveries WHERE json_extract(data,'$.receipt.task_id')=? ORDER BY rowid", (owner['id'],))]
        # The first delivery is the original assignment. The two continuations follow it.
        self.assertEqual([delivery.get('turn_id') for _, delivery in rows][::2], [None, owner['_native_turn_id']])
        rows = rows[1:]
        for index, (identity, delivery) in enumerate(rows):
            for change in (dict(target_bot_id=self.lead['id']), dict(receipt=dict(delivery['receipt'], status='completed')),
                           dict(receipt=dict(delivery['receipt'], extra=1)), dict(source_task_id='a-missing-task'),
                           dict(text='A message that the task never received.')):
                with self.subTest(delivery=index, change=change):
                    self.store.db.execute('UPDATE deliveries SET data=? WHERE id=?', (json.dumps(delivery | change), identity))
                    self.assertRejected(owner)
            self.store.db.execute('UPDATE deliveries SET data=? WHERE id=?', (json.dumps(delivery), identity))
        stranger = self.store.mutate('POST', '/v1/bots', {'name': 'Stranger'}, str(uuid4()), 'test-phone')
        self.assertRejected(owner, 'Only the owner', caller=self.working(stranger, 'Continue that work.'))
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.')['task_id'], owner['id'])

    def test_routine_records_keep_the_routine_rule(self):
        owner = self.saved()
        from coconut.routine_coordinator import ROUTINE_PROVENANCE
        # Only the routine code writes these. A leftover one, even empty or malformed, is no ordinary request.
        for field in ROUTINE_PROVENANCE:
            for value in (dict(native_id='t_original') if field == '_native_occurrence' else 'x', None, False, '', {}):
                with self.subTest(field=field, value=value):
                    self.store._put('tasks', dict(owner, **{field: value}))
                    self.assertRejected(self.store._get('tasks', owner['id']))
        self.store._put('tasks', owner)
        self.store._event('routine.triggered', dict(routine_id='a-routine', task_id=owner['id']), owner['bot_id'], owner['id'])
        self.assertRejected(owner, 'not blocked or completed')

    def test_manual_stop_and_other_endings_stay_final(self):
        owner = self.saved()
        for changes in (dict(status='cancelled'), dict(status='failed'), dict(status='waiting')):
            with self.subTest(changes=changes):
                self.store._put('tasks', dict(owner, **changes))
                self.assertRejected(self.store._get('tasks', owner['id']))
        self.store._put('tasks', owner)
        # A Stop that the person later reversed still leaves the cancellation on record.
        self.store._event('task.updated', dict(owner, status='cancelled'), owner['bot_id'], owner['id'])
        self.assertRejected(owner, 'not blocked or completed')

    def test_requester_stop_stays_final(self):
        owner = self.saved()
        with self.store.transaction():
            self.store._cancel_one(self.store._get('tasks', owner['id']), [], include_finished=True)
        self.assertRejected(self.store._get('tasks', owner['id']))

    def test_idle_archive_by_the_person_stays_final(self):
        owner = self.saved()
        for field in ('task_id', '_execution_task_id'):
            with self.subTest(field=field):
                self.store._put('decisions', {'id': 'archived-question', 'task_id': self.requester['id'], 'bot_id': owner['bot_id'],
                                              'status': 'rejected', 'decided_by': 'archive', 'outcome': 'Task archived.',
                                              field: owner['id']})
                self.assertRejected(owner)
                self.store.db.execute("DELETE FROM decisions WHERE id='archived-question'")
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.')['task_id'], owner['id'])

    def test_open_input_decision_waits_background_and_unknown_effects_hold(self):
        owner = self.saved()
        self.store._put('decisions', dict(id='pending', task_id=owner['id'], bot_id=owner['bot_id'], status='pending'))
        self.assertRejected(owner)
        self.store.db.execute("DELETE FROM decisions WHERE id='pending'")
        for changes in (dict(_steering=['Use the other checkout.']), dict(_blocked_followup=dict(
                message_ids=['m'], source_ids=['m'], attachment_ids=[])), dict(_waiting_on=['dependency']),
                dict(_return_pending=dict(text='Saved result')), dict(_native_reply_pending=True),
                dict(_self_recovery=dict(owner['_self_recovery'], unknown_tools=[dict(id='unknown')]))):
            with self.subTest(changes=changes):
                self.store._put('tasks', dict(owner, **changes))
                self.assertRejected(self.store._get('tasks', owner['id']))
        self.store._put('tasks', owner)
        base = dict(task_id=owner['id'], turn_id=owner['_native_turn_id'], action_hash='hash',
                    created_at='2026-10-16T20:16:00Z')
        self.store._put('native_receipts', dict(base, id='process', state='completed',
            action={'name': 'terminal', 'arguments': {}},
            result=json.dumps({'notify_on_complete': True, 'session_id': 'proc-1'})))
        self.assertRejected(owner)
        self.store.db.execute("DELETE FROM native_receipts WHERE id='process'")
        self.store._put('native_receipts', dict(base, id='unknown', state='executing',
                                                action={'name': 'write_file', 'arguments': {}}))
        self.assertRejected(owner)
        self.store.db.execute("DELETE FROM native_receipts WHERE id='unknown'")
        self.store.running[owner['id']] = Mock(is_alive=Mock(return_value=True))
        self.assertRejected(owner)
        self.store.running.pop(owner['id'])
        peer = self.working(self.dev, 'Peer turn')
        self.update(peer['id'], _execution_session_id=owner['id'])
        self.assertRejected(owner, 'active work')
        self.update(peer['id'], status='needs_you', _execution_session_id='another-execution', _assignment_task_id=owner['id'])
        self.assertRejected(owner, 'paused')

    def test_pending_decision_of_the_same_outcome_holds(self):
        owner = self.saved()
        # A completed peer turn of the same execution asked for a decision. Its answer comes first.
        peer = self.update(self.working(self.dev, 'Weigh the merits.')['id'], status='completed', _execution_session_id=owner['id'])
        self.store._put('decisions', dict(id='merits', task_id=peer['id'], bot_id=peer['bot_id'], status='pending',
                                          inbox_kind='proposal'))
        self.assertRejected(owner, 'existing decision must be answered')
        self.store.db.execute("DELETE FROM decisions WHERE id='merits'")
        self.store.db.execute('DELETE FROM tasks WHERE id=?', (peer['id'],))
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.')['task_id'], owner['id'])

    def reconciliation(self, owner, decision):
        """A separate execution reported on this outcome's Work row, and may have asked about it.

        It shares no source assignment with the owner, so only the Work row links them. See the source decision tests.
        """
        from coconut.service import now
        source = dict(source_id='issues', id='https://github.com/example/bank/issues/7', updated_at='2026-10-16T10:00:00Z',
                      labels=[])
        reconciler = self.update(self.working(self.dev, 'Reconcile the bank intake.')['id'], status='completed',
                                 _execution_session_id='source-reconciliation',
                                 conversation_bot_id=owner['conversation_bot_id'], session_id=owner['session_id'])
        item = dict(id=str(uuid4()), kind='work', bot_id=owner['bot_id'], task_id=reconciler['id'], title='Bank validation',
                    status='active', _work_origin_task_id=owner['id'], _work_previous_task_ids=[], _work_key='bank-validation',
                    created_at=now(), updated_at=now())
        self.store._put('artifacts', item)
        self.update(owner['id'], _work_context_id=item['id'])
        if decision:
            self.store._put('decisions', dict(id='merits', task_id=reconciler['id'], bot_id=owner['bot_id'], status=decision,
                                              inbox_kind='proposal', title='Accept the bank validation?',
                                              sources=[dict(kind='issue', title='Bank intake', url=source['id'])]))
        return self.store._get('tasks', owner['id']), item

    def test_pending_decision_of_a_separate_reconciliation_of_the_same_outcome_holds(self):
        owner, item = self.reconciliation(self.saved(), 'pending')
        # The reconciliation holds the outcome's Work row, and its card waits. Continue that owner instead.
        self.assertRejected(owner, 'another execution')
        self.assertNotEqual(self.store._get('artifacts', item['id'])['task_id'], owner['id'])

    def test_answered_decision_of_a_separate_reconciliation_releases_the_outcome(self):
        owner, item = self.reconciliation(self.saved(), 'approved')
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.')['task_id'], owner['id'])
        self.assertEqual(self.store._get('artifacts', item['id'])['task_id'], owner['id'])

    def test_separate_reconciliation_without_a_decision_releases_the_outcome(self):
        owner, item = self.reconciliation(self.saved(), None)
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.')['task_id'], owner['id'])
        self.assertEqual(self.store._get('artifacts', item['id'])['task_id'], owner['id'])

    def test_unrelated_cards_stay_final(self):
        owner = self.saved()
        # The original card of the assignment no longer names its execution.
        self.assertRejected(owner, 'Choose one', identity='t_original')
        # Another completed assignment whose archived scheduled card has no authenticated continuation.
        other = self.working(self.dev, 'Another delegated check.')
        other = self.update(other['id'], status='completed', _execution_session_id='another', _native_turn_id='turn-other',
                            _native_outcome_id='t_other', _kanban_started=27000, _kanban_claim=dict(
                                native_id='t_other', external_id=other['id'], input='turn-other', run_id=27000))
        self.store.kanban.tasks[other['id']] = self.card(other, 't_other', 'coconut-followup:' + other['id'] + ':turn-other',
                                                         status='archived', input_id='turn-other')
        self.runs['t_other'] = [dict(id=27000, status='scheduled', outcome='scheduled', ended=True)]
        self.assertRejected(other, 'not blocked or completed', identity='t_other',
                            caller=self.working(self.dev, 'Continue the other check.'))
        # The saved assignment itself still continues.
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.')['task_id'], owner['id'])

    def test_failed_assignment_keeps_its_own_rule(self):
        owner = self.saved()
        self.store._put('tasks', dict(owner, status='failed'))
        failed = self.store._get('tasks', owner['id'])
        self.assertRejected(failed, 'not blocked or completed')
        self.assertFalse(self.store.keeps_settled_failure(failed, self.store.kanban.tasks[owner['id']]))


SOURCE = dict(source_id='desk-github-assigned', id='https://github.com/example/bank/issues/7',
              updated_at='2026-10-16T10:00:00Z', labels=[])


class ArchivedScheduledAssignmentSourceDecisionTests(ScheduledAssignment, unittest.TestCase):
    """A later audit of the same source item asked the person. Its pending card holds the archived recovery."""

    def reconciliation(self, bot=None, source=SOURCE, status='pending', **decision):
        """A separate audit execution of the bot, with no Work row, parent, thread, or native outcome of the owner."""
        bot = bot or self.dev
        task = self.store._get('tasks', self.store.mutate('POST', '/v1/bots/' + bot['id'] + '/messages',
            {'text': 'Reconcile the assigned bank intake issue.'}, str(uuid4()), 'test-phone')['id'])
        task = self.update(task['id'], status='completed', _execution_session_id=task['id'],
                           **({'_source_assignment': dict(source)} if source is not None else {}))
        record = dict(id=str(uuid4()), task_id=task['id'], bot_id=bot['id'], status=status, inbox_kind='proposal',
                      title='Is the bank intake change worth merging?', revision=1,
                      sources=[dict(kind='issue', title='Bank intake', url=SOURCE['id'])]) | decision
        self.store._put('decisions', record)
        return task, record

    def snapshot(self):
        return {table: [row[0] for row in self.store.db.execute('SELECT data FROM ' + table + ' ORDER BY rowid')]
                for table in ('tasks', 'deliveries', 'decisions', 'artifacts', 'messages')}

    def assertHeld(self, owner, caller=None):
        before, calls = self.snapshot(), len(self.calls)
        with self.assertRaisesRegex(ServiceError, 'decision about the same source is pending'):
            self.resume('t_scheduled', 'Finish the bank validation.', caller)
        # The refusal writes nothing: no receipt, no input, no answer.
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([c['op'] for c in self.calls[calls:]], ['completion'])
        self.assertEqual(self.store._get('tasks', owner['id']), owner)

    def assertContinues(self, owner, caller=None):
        receipt = self.resume('t_scheduled', 'Finish the bank validation.', caller)
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_scheduled', status='queued'))
        return receipt

    def test_saved_shape_waits_for_the_pending_decision_of_a_separate_reconciliation(self):
        owner = self.saved(delegated=False, source=SOURCE)
        reconciler, decision = self.reconciliation()
        # The saved shape: nothing links the two executions but the source item.
        for key in ('_execution_session_id', 'thread_id', 'exchange_id'):
            self.assertNotEqual(reconciler.get(key), owner.get(key), key)
        for key in ('_work_context_id', '_native_outcome_id', 'parent_task_id', '_assignment_task_id', 'source_task_id'):
            self.assertIsNone(reconciler.get(key), key)
        # The owner's Work row still belongs to the owner's archived execution. The reconciler has none.
        rows = [json.loads(row[0]) for row in self.store.db.execute('SELECT data FROM artifacts')]
        self.assertEqual([(row['id'], row['task_id']) for row in rows], [(owner['_work_context_id'], owner['id'])])
        self.assertHeld(owner)
        self.assertEqual(self.store._get('artifacts', owner['_work_context_id'])['task_id'], owner['id'])
        # A second attempt stays held. A refusal saves no receipt that a replay could return.
        self.assertHeld(owner)
        # The person answers. The same continuation now proceeds.
        self.store._put('decisions', dict(decision, status='approved', decided_by='test-phone', decided_at='2026-10-17T10:00:00Z'))
        self.assertContinues(owner)

    def test_owner_caller_and_requester_caller_are_both_held(self):
        owner = self.saved(delegated=True, source=SOURCE)
        self.reconciliation()
        self.assertHeld(owner)
        self.assertHeld(owner, caller=self.working(self.dev, 'Finish the delegated validation.'))

    def test_answered_or_closed_decisions_release_the_card(self):
        for status in ('approved', 'modified', 'rejected', 'replied', 'executed', 'failed', 'dismissed', 'resolved'):
            with self.subTest(status=status):
                owner = self.saved(delegated=False, source=SOURCE)
                self.reconciliation(status=status)
                self.assertContinues(owner)
                self.doCleanups()
                ScheduledAssignment.setUp(self)

    def test_no_reconciliation_decision_releases_the_card(self):
        owner = self.saved(delegated=False, source=SOURCE)
        self.assertContinues(owner)

    def test_decisions_of_other_sources_items_and_bots_stay_distinct(self):
        other = self.store.mutate('POST', '/v1/bots', {'name': 'Other desk'}, str(uuid4()), 'test-phone')
        cases = (dict(source=dict(SOURCE, source_id='lead-github-assigned')),
                 dict(source=dict(SOURCE, id='https://github.com/example/bank/issues/8')),
                 # No substring, prefix, or URL normalization.
                 dict(source=dict(SOURCE, id=SOURCE['id'] + '/')),
                 dict(source=dict(SOURCE, id=SOURCE['id'] + '#issuecomment-1')),
                 dict(source=dict(SOURCE, id='https://github.com/example/bank/issues/70')),
                 # The same item for another bot is that bot's work.
                 dict(source=SOURCE, bot=other),
                 # A card whose sources link the issue, from a task without that source assignment.
                 dict(source=None))
        owner = self.saved(delegated=False, source=SOURCE)
        for case in cases:
            self.reconciliation(**case)
        # A pending decision of an unrelated task of the bot.
        unrelated = self.update(self.working(self.dev, 'Draft the release notes.')['id'], status='needs_you',
                                _execution_session_id='release-notes')
        self.store._put('decisions', dict(id='notes', task_id=unrelated['id'], bot_id=self.dev['id'], status='pending'))
        self.assertContinues(owner)

    def test_malformed_source_assignment_holds_the_card(self):
        owner = self.saved(delegated=False, source=SOURCE)
        for value in ('desk-github-assigned', None, [], {}, dict(SOURCE, id=''), dict(SOURCE, source_id=''),
                      dict(SOURCE, id=7), {'id': SOURCE['id']}, {'source_id': SOURCE['source_id']}):
            with self.subTest(value=value):
                self.store._put('tasks', dict(owner, _source_assignment=value))
                self.assertHeld(self.store._get('tasks', owner['id']))
        self.store._put('tasks', owner)
        self.assertContinues(owner)

    def test_request_without_a_source_assignment_has_no_source_decision(self):
        owner = self.saved(delegated=True)
        self.assertNotIn('_source_assignment', owner)
        self.reconciliation()
        self.assertContinues(owner)

    def test_only_the_ordinary_archived_recovery_reads_source_decisions(self):
        owner = self.saved(delegated=False, source=SOURCE)
        self.reconciliation()
        self.assertTrue(self.store.pending_source_decision(owner))
        from coconut.routine_coordinator import ROUTINE_PROVENANCE
        for field in ROUTINE_PROVENANCE:
            with self.subTest(field=field):
                self.assertFalse(self.store.pending_source_decision(dict(owner, **{field: None})))

    def test_source_decision_lookups_use_indexes(self):
        from coconut import routine_coordinator
        for statement, values in ((routine_coordinator.PENDING_DECISIONS, ('b',)),
                                  (routine_coordinator.EXECUTED_PROPOSAL, ('p', 'c', 'b', *routine_coordinator.ANSWERED))):
            plan = ' | '.join(row[3] for row in self.store.db.execute('EXPLAIN QUERY PLAN ' + statement, values))
            self.assertIn('USING INDEX', plan)
            self.assertNotIn('SCAN', plan)


class ArchivedScheduledAssignmentSourceProvenanceTests(ScheduledAssignment, unittest.TestCase):
    """A card of the same source work holds the recovery, even when its own task has no source assignment.

    Source work continues only through a recorded proposal execution or an explicit assignment, as in
    work_sources.answered_decisions. A plain parent link is no such continuation, because human replies have one too.
    """

    assertHeld = ArchivedScheduledAssignmentSourceDecisionTests.assertHeld
    assertContinues = ArchivedScheduledAssignmentSourceDecisionTests.assertContinues
    snapshot = ArchivedScheduledAssignmentSourceDecisionTests.snapshot

    def task(self, **fields):
        task = self.store._get('tasks', self.store.mutate('POST', '/v1/bots/' + fields.pop('bot', self.dev)['id'] + '/messages',
            {'text': 'Source work turn.'}, str(uuid4()), 'test-phone')['id'])
        return self.update(task['id'], **{'status': 'completed', '_execution_session_id': task['id']} | fields)

    def pending(self, task, **fields):
        decision = dict(id=str(uuid4()), task_id=task['id'], bot_id=task['bot_id'], status='pending', inbox_kind='proposal',
                        title='Is the bank intake change worth merging?', revision=1) | fields
        self.store._put('decisions', decision)
        return decision

    def executed(self, parent, child, **fields):
        """An answered proposal of the parent that the child carries out."""
        self.store._put('decisions', dict(id=str(uuid4()), task_id=parent['id'], bot_id=parent['bot_id'], status='approved',
                                          inbox_kind='proposal', title='Weigh the merits?', revision=2,
                                          decided_by='test-phone', _execution_task_id=child['id']) | fields)
        return self.update(child['id'], parent_task_id=parent['id'])

    def answer(self, decision, **data):
        return self.store.mutate('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1) | data,
                                 str(uuid4()), 'test-phone')

    def test_card_of_an_accepted_proposal_continuation_holds_the_card(self):
        owner = self.saved(delegated=False, source=SOURCE)
        reconciler = self.update(self.working(self.dev, 'Reconcile the assigned bank intake issue.')['id'],
                                 _execution_session_id='source-reconciliation', _source_assignment=dict(SOURCE))
        # The audit offers a proposal through the native tool path, and the person accepts it.
        recorded = self.store.emit(reconciler['id'], 'native_proposal', dict(
            key='merits', title='Weigh the merits of the bank intake change?', options=[dict(id='go', label='Weigh the merits')]))
        self.answer(self.store._get('decisions', recorded['decision_id']), option_id='go')
        proposal = self.store._get('decisions', recorded['decision_id'])
        continuation = self.store._get('tasks', proposal['_execution_task_id'])
        self.assertEqual((proposal['status'], continuation['parent_task_id']), ('approved', reconciler['id']))
        self.assertNotIn('_source_assignment', continuation)
        # The continuation turn raises the next merits card.
        self.update(continuation['id'], status='working')
        recorded = self.store.emit(continuation['id'], 'native_proposal', dict(
            key='merits-next', title='Merge the bank intake change?', options=[dict(id='merge', label='Merge it')]))
        card = self.store._get('decisions', recorded['decision_id'])
        self.assertEqual((card['task_id'], card['status']), (continuation['id'], 'pending'))
        self.update(continuation['id'], status='completed')
        self.update(reconciler['id'], status='completed')
        self.assertHeld(owner)
        self.assertHeld(owner, caller=self.working(self.dev, 'Finish the validation.'))
        # The person declines the next card. The answer releases the archived recovery.
        self.answer(card, choice='reject')
        self.assertEqual(self.store._get('decisions', card['id'])['status'], 'rejected')
        self.assertContinues(owner)

    def test_card_of_an_explicitly_assigned_task_holds_the_card(self):
        owner = self.saved(delegated=False, source=SOURCE)
        reconciler = self.task(_source_assignment=dict(SOURCE))
        self.pending(self.task(_assignment_task_id=reconciler['id']))
        self.assertHeld(owner)

    def test_card_of_a_deeper_continuation_holds_the_card(self):
        owner = self.saved(delegated=False, source=SOURCE)
        reconciler = self.task(_source_assignment=dict(SOURCE))
        first = self.executed(reconciler, self.task())
        second = self.task(_assignment_task_id=first['id'])
        self.pending(self.executed(second, self.task()))
        self.assertHeld(owner)

    def test_human_reply_and_other_parent_links_are_no_source_work(self):
        owner = self.saved(delegated=False, source=SOURCE)
        reconciler = self.task(_source_assignment=dict(SOURCE))
        # A plain reply under the audit shares its parent link only.
        self.pending(self.task(parent_task_id=reconciler['id']))
        # A dismissed proposal recorded no execution that the person accepted.
        waiting = self.task()
        self.executed(reconciler, waiting, status='dismissed', decided_by=None)
        self.pending(waiting)
        # The proposal names another execution.
        other = self.task()
        self.executed(reconciler, self.task(), _execution_task_id='another-task')
        self.update(other['id'], parent_task_id=reconciler['id'])
        self.pending(other)
        # The answered proposal belongs to another parent than the child's own.
        stray = self.task(parent_task_id=reconciler['id'])
        unrelated = self.task()
        self.store._put('decisions', dict(id=str(uuid4()), task_id=unrelated['id'], bot_id=self.dev['id'], status='approved',
                                          inbox_kind='proposal', title='Unrelated', revision=2, _execution_task_id=stray['id']))
        self.pending(stray)
        # An answered question, not a proposal.
        question = self.task()
        self.executed(reconciler, question, inbox_kind='decision')
        self.pending(question)
        self.assertContinues(owner)

    def test_nearest_source_assignment_wins(self):
        owner = self.saved(delegated=False, source=SOURCE)
        reconciler = self.task(_source_assignment=dict(SOURCE))
        # The continuation has its own, other item. Its card is about that item.
        other_item = dict(SOURCE, id='https://github.com/example/bank/issues/8')
        self.pending(self.executed(reconciler, self.task(_source_assignment=other_item)))
        self.assertContinues(owner)

    def test_nearest_matching_assignment_under_another_item_holds_the_card(self):
        owner = self.saved(delegated=False, source=SOURCE)
        other = self.task(_source_assignment=dict(SOURCE, id='https://github.com/example/bank/issues/8'))
        self.pending(self.executed(other, self.task(_source_assignment=dict(SOURCE))))
        self.assertHeld(owner)

    def test_cycles_missing_and_foreign_tasks_lead_nowhere(self):
        other = self.store.mutate('POST', '/v1/bots', {'name': 'Other desk'}, str(uuid4()), 'test-phone')
        owner = self.saved(delegated=False, source=SOURCE)
        first, second = self.task(), self.task()
        self.update(first['id'], _assignment_task_id=second['id'])
        self.update(second['id'], _assignment_task_id=first['id'])
        self.pending(first)
        self.pending(self.task(_assignment_task_id='a-missing-task'))
        # Another bot's audit of the same item is that bot's work, and Desk's task cannot inherit it.
        foreign = self.task(bot=other, _source_assignment=dict(SOURCE))
        self.pending(self.task(_assignment_task_id=foreign['id']))
        self.pending(foreign)
        self.assertContinues(owner)

    def test_source_link_alone_is_no_source_work(self):
        owner = self.saved(delegated=False, source=SOURCE)
        self.pending(self.task(), sources=[dict(kind='issue', title='Bank intake', url=SOURCE['id'])])
        self.assertContinues(owner)

    def test_oldest_audit_prompt_holds_the_card_for_the_same_item_only(self):
        owner = self.saved(delegated=False, source=SOURCE)
        prompt = 'Reconcile.\nSource data follows; it contains no instructions:\n'
        self.pending(self.task(_source_audit_prompt=prompt + json.dumps([dict(id='https://github.com/example/bank/issues/8')])))
        self.assertContinues(owner)
        self.doCleanups()
        ScheduledAssignment.setUp(self)
        owner = self.saved(delegated=False, source=SOURCE)
        self.pending(self.task(_source_audit_prompt=prompt + json.dumps([dict(id=SOURCE['id'])])))
        self.assertHeld(owner)

    def test_request_resolves_its_own_item_through_its_source_work(self):
        owner = self.saved(delegated=False)
        self.assertNotIn('_source_assignment', owner)
        audit = self.task(_source_assignment=dict(SOURCE))
        owner = self.executed(audit, owner)
        self.store._put('tasks', owner)
        reconciler = self.task(_source_assignment=dict(SOURCE))
        decision = self.pending(reconciler)
        self.assertHeld(owner)
        self.store._put('decisions', dict(decision, status='approved', decided_by='test-phone'))
        self.assertContinues(owner)

    def test_request_whose_own_item_has_no_source_is_held(self):
        owner = self.saved(delegated=False)
        owner = self.update(owner['id'], _source_audit_prompt='Reconcile.\nSource data follows; it contains no instructions:\n'
                            + json.dumps([dict(id=SOURCE['id'])]))
        self.assertHeld(owner)

    def test_request_without_source_work_has_no_source_decision(self):
        owner = self.saved(delegated=False)
        # A plain parent link to an audit is no source work of the request.
        self.update(owner['id'], parent_task_id=self.task(_source_assignment=dict(SOURCE))['id'])
        self.pending(self.task(_source_assignment=dict(SOURCE)))
        self.assertContinues(self.store._get('tasks', owner['id']))


class ArchivedScheduledAssignmentNativeTests(unittest.TestCase):
    """The installed Hermes kernel: the settle pass archives the scheduled card, continuation dispatches its successor."""

    write = native_fixtures.NativeKanbanTests.write
    native_program = native_fixtures.NativeKanbanTests.native_program
    tick = native_fixtures.NativeKanbanTests.tick
    native = routine_fixtures.ArchivedScheduledRoutineNativeTests.native

    def setUp(self):
        hermes_env.require(self)
        try:
            native_fixtures.NativeKanbanTests.setUp(self)
        except RuntimeError as error:
            # Board pins HERMES_KANBAN_DB to the fixture's own home. A Hermes descendant fence makes that pinned board
            # read-only, so the adapter cannot start. Report Hermes' own reason instead of working around the fence.
            self.addCleanup(self.tmp.cleanup)
            log = Path(self.tmp.name) / 'kanban' / 'adapter.log'
            reason = log.read_text().strip().splitlines()[-1:] if log.exists() else []
            self.fail('The native fixture board did not start: %s %s' % (error, reason))
        # The board is the fixture's own, never a board that the environment pins.
        self.assertTrue((Path(self.tmp.name) / 'kanban' / 'kanban.db').is_file())
        self.provider.tasks = []
        self.owner_id = None
        start = self.provider.start_task

        def record(task, bot, history, emit):
            if task['id'] == self.owner_id:
                self.provider.tasks.append(copy.deepcopy(task))
                claim = task['_kanban_claim']
                # The worker parks its card with kanban_schedule, then its turn completes.
                self.native_program("assert board.kb.schedule_task(board.conn, sys.argv[2], reason=sys.argv[4], "
                                    "expected_run_id=int(sys.argv[3]))", claim['native_id'], str(claim['run_id']), REASON)
            start(task, bot, history, emit)
        self.provider.start_task = record

    def dispatched(self, count):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(self.provider.tasks) < count:
            self.tick()
        self.assertEqual(len(self.provider.tasks), count, 'The continuation did not dispatch.')
        return self.provider.tasks[-1]

    completed = routine_fixtures.ArchivedScheduledRoutineNativeTests.completed

    def update(self, identity, **fields):
        """Change a task in one committed service transaction, as the service's own writers do."""
        self.assertFalse(self.service.db.in_transaction, 'A fixture write must not nest in another transaction.')
        with self.service.transaction():
            task = self.service._get('tasks', identity)
            task.update(fields)
            self.service._put('tasks', task)
        return task

    def settle_returns(self, owner, requester):
        """The dispatcher delivers the owner's saved result, and the requester's return callback runs to its end.

        The pass drains saved returns first, as a running service does. The callback is a turn of the requester, so it
        must end, and its worker must exit, before the next continuation, or it would share dispatch with that turn.
        """
        def callbacks():
            return [t for t in self.service._all('tasks') if t['bot_id'] == requester['id'] and t.get('_is_return')]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and (self.service._get('tasks', owner['id']).get('_return_pending') or not callbacks()
                                               or any(t['status'] in self.service.ACTIVE for t in callbacks())):
            self.tick()
            time.sleep(.05)
        self.assertFalse(self.service._get('tasks', owner['id']).get('_return_pending'), 'The saved result was not delivered.')
        self.assertTrue(callbacks(), 'The requester received no return callback.')
        for task in callbacks():
            self.assertNotIn(task['status'], self.service.ACTIVE, 'The return callback did not finish.')
            worker = self.service.running.get(task['id'])
            if worker:
                worker.join(20)
                self.assertFalse(worker.is_alive(), 'The return callback turn did not end.')
        delivered = [json.loads(row[0]) for row in self.service.db.execute(
            "SELECT data FROM deliveries WHERE json_extract(data,'$.source_task_id')=?", (owner['id'],))]
        self.assertIn((requester['id'], 'Validated the intake.'), [(d['target_bot_id'], d['text']) for d in delivered])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()

    def assertQuiescent(self, owner):
        """Before a continuation: no open transaction, no undelivered result, and no live turn of the owner."""
        self.assertFalse(self.service.db.in_transaction)
        task = self.service._get('tasks', owner['id'])
        self.assertFalse(task.get('_return_pending'), 'The previous result is still waiting for delivery.')
        worker = self.service.running.get(owner['id'])
        self.assertFalse(worker and worker.is_alive(), 'The previous turn is still running.')

    def continue_work(self, caller, owner, identity, message):
        self.assertQuiescent(owner)
        return self.service.emit(caller['id'], 'work_continue', dict(task_id=identity, message=message))

    def completed_assignment(self, delegated=True, source=None):
        """The assignment ends its own card with kanban_complete. A requester delegated it, or the source audit gave it."""
        lead = self.write('/v1/bots', {'name': 'Lead'}) if delegated else self.bot
        if delegated:
            origin = self.update(self.write('/v1/bots/' + lead['id'] + '/messages', {'text': 'Validate the intake.'})['id'],
                                 status='working', _chat_session=True)
            identity = self.service._store_bot_message(origin['id'], dict(target_bot_id=self.bot['id'],
                                                                           text='Validate the bank details read-only.'))['task_id']
            # The requester's turn ends after it delegates. The result returns to it in a later turn.
            self.update(origin['id'], status='completed')
        else:
            identity = self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'Review the assigned bank intake issue.'})['id']
        with self.service.transaction():
            owner = self.service._get('tasks', identity)
            owner.update(_execution_session_id=owner['id'], _conversation_epoch=self.service._setting('conversation_epoch'))
            if source:
                owner['_source_assignment'] = dict(source)
            self.service._put('tasks', owner)
        self.owner_id = owner['id']
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        card = self.service.kanban.tasks[owner['id']]
        identity = card['id']
        run = self.native_program('c = board.kb.claim_task(board.conn, sys.argv[2])\nprint(c.current_run_id)', identity)
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner.update(status='working', _native_turn_id='turn-original', _kanban_started=int(run), _kanban_claim=dict(
                native_id=identity, external_id=owner['id'], input=row['input'], run_id=int(run), workspace=card['workspace_path']))
            self.service._put('tasks', owner)
        self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='Validated the intake.', "
                            "expected_run_id=int(sys.argv[3]))", identity, run)
        call = self.service.emit(owner['id'], 'native_call', dict(name='kanban_complete', arguments=dict(task_id=identity)))
        self.service.emit(owner['id'], 'native_result', dict(id=call['id'], result=json.dumps(
            dict(ok=True, task_id=identity, run_id=int(run)))))
        self.service.emit(owner['id'], 'completed', dict(summary='Validated the intake.'))
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'done')
        owner = self.service._get('tasks', owner['id'])
        self.assertEqual(owner['status'], 'completed')
        self.assertFalse(any(owner.get(k) for k in ('_native_job', 'routine_id', '_native_occurrence')))
        if delegated:
            # A delegated result waits for delivery. Continuing now would be refused, so the dispatcher delivers it first.
            self.assertEqual(owner['_return_pending']['target_bot_id'], lead['id'])
            self.settle_returns(owner, lead)
            owner = self.service._get('tasks', owner['id'])
        else:
            self.assertFalse(owner.get('_return_pending'))
        Path(card['workspace_path']).mkdir(parents=True, exist_ok=True)
        source = self.update(self.write('/v1/bots/' + lead['id'] + '/messages', {'text': 'Check it.'})['id'],
                             status='working', _chat_session=True)
        return owner, source, identity, card['workspace_path']

    def scheduled(self, delegated=True, source=None):
        """Two continuations schedule one successor card. The settle pass archives it a day after the task completed."""
        owner, source, original, workspace = self.completed_assignment(delegated, source)
        self.continue_work(source, owner, original, 'Validate the bank details again.')
        first = self.dispatched(1)
        scheduled = first['_native_outcome_id']
        self.assertNotEqual(scheduled, original)
        self.completed(owner)
        self.tick()
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'scheduled')
        self.continue_work(source, owner, scheduled, 'Continue the bank validation.')
        second = self.dispatched(2)
        self.assertEqual(second['_native_outcome_id'], scheduled)
        self.completed(owner)
        self.tick()
        self.update(owner['id'], updated_at=stamp(SETTLE_SECONDS + 1))
        self.service._settle_native_orphans()
        self.service.kanban.call({'op': 'snapshot'})
        self.tick()
        native = self.native(scheduled)
        self.assertEqual(native['status'], 'archived')
        self.assertEqual([run[2] for run in native['runs']], ['scheduled', 'scheduled'])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + first['_native_turn_id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')
        return owner, source, original, scheduled, workspace

    def test_continuation_dispatches_a_successor_of_the_scheduled_card(self):
        owner, source, original, scheduled, workspace = self.scheduled()
        history = self.native(scheduled), self.native(original)
        receipt = self.continue_work(source, owner, scheduled, 'Finish the bank validation.')
        self.assertEqual(receipt['native_task_id'], scheduled)
        continued = self.service._get('tasks', owner['id'])
        task = self.dispatched(3)
        successor = task['_native_outcome_id']
        self.assertNotIn(successor, (original, scheduled))
        native = self.native(successor)
        self.assertEqual(native['parents'], [scheduled])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'])
        self.assertEqual((native['tenant'], native['path']), (owner['bot_id'], workspace))
        self.assertEqual(self.provider.prepared[-1][1:], (workspace, owner['id'], successor))
        self.assertEqual(task['_kanban_workspace'], workspace)
        self.assertEqual(task['_native_turn_id'], continued['_native_turn_id'])
        # The archived card and the original card keep their history. Hermes runs only the successor.
        self.assertEqual((self.native(scheduled), self.native(original)), history)
        # A replay returns the saved receipt and dispatches nothing more.
        self.assertEqual(self.service.emit(source['id'], 'work_continue',
                                           dict(task_id=scheduled, message='Finish the bank validation.')), receipt)
        self.completed(owner)
        self.tick()
        self.assertEqual(len(self.provider.tasks), 3)
        self.assertFalse(any(json.loads(row[0]).get('status') == 'cancelled' for row in self.service.db.execute(
            "SELECT data FROM events WHERE task_id=? AND type='task.updated'", (owner['id'],))))

    def test_pending_source_decision_holds_the_archived_card_until_answered(self):
        owner, caller, original, scheduled, workspace = self.scheduled(delegated=False, source=SOURCE)
        history = self.native(scheduled), self.native(original)
        # A separate audit of the same item, written through the service's own route, then committed as completed.
        reconciler = self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'Reconcile the assigned bank intake issue.'})
        reconciler = self.update(reconciler['id'], status='completed', _execution_session_id=reconciler['id'],
                                 _source_assignment=dict(SOURCE))
        decision = dict(id=str(uuid4()), task_id=reconciler['id'], bot_id=self.bot['id'], status='pending',
                        inbox_kind='proposal', title='Is the bank intake change worth merging?', revision=1,
                        sources=[dict(kind='issue', title='Bank intake', url=SOURCE['id'])])
        with self.service.transaction():
            self.service._put('decisions', decision)
        before = self.service._get('tasks', owner['id'])
        with self.assertRaisesRegex(ServiceError, 'decision about the same source is pending'):
            self.continue_work(caller, owner, scheduled, 'Finish the bank validation.')
        self.tick()
        self.assertEqual(self.service._get('tasks', owner['id']), before)
        self.assertEqual(len(self.provider.tasks), 2)
        self.assertEqual((self.native(scheduled), self.native(original)), history)
        with self.service.transaction():
            self.service._put('decisions', dict(decision, status='approved', decided_by='test-phone'))
        receipt = self.continue_work(caller, owner, scheduled, 'Finish the bank validation.')
        self.assertEqual(receipt['native_task_id'], scheduled)
        successor = self.dispatched(3)['_native_outcome_id']
        self.assertEqual(self.native(successor)['parents'], [scheduled])
        self.assertEqual(self.native(successor)['path'], workspace)

    def test_cancelled_assignment_stays_archived(self):
        owner, source, _, scheduled, _ = self.scheduled()
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            self.service._cancel_one(owner, [], include_finished=True)
        owner = self.service._get('tasks', owner['id'])
        with self.assertRaises(ServiceError):
            self.continue_work(source, owner, scheduled, 'Continue anyway.')
        self.assertEqual(self.service._get('tasks', owner['id']), owner)


if __name__ == '__main__':
    unittest.main()
