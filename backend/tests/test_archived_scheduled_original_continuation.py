"""A relayed request continues after Coconut's settle pass archives the original card that its worker scheduled.

A requesting bot relays work to another bot through its native envelope. Board.sync gives the request its original card,
under the key coconut:<id>. The worker parks that card with kanban_schedule to check again later, and Hermes ends the
run as scheduled. A helper's result is new input, and Board.sync reuses the scheduled card for it. That turn parks the
card again, the task completes, and a day later the settle pass archives the card. No continuation ever made a
successor, so the rule for a continuation's scheduled successor cannot reach the unfinished work.

Only that exact card continues: the original card of a request that Coconut created once from the requesting bot's
relay, whose current input has the bridge's receipt of the kanban_schedule call that the claim's run made on it. The key
alone admits nothing. The new input gets a successor in the same execution, and the owner's own turn restores its
discarded worktree. Native CI sets COCONUT_REQUIRE_NATIVE, and then the native tests fail instead of skipping.
"""
import copy
import json
import os
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut import routine_coordinator
from coconut.kanban_runtime import IDLE_AGE
from coconut.kanban_scheduler import SETTLE_SECONDS
from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
from coconut.work_workspace import changes, discard, git, preferred
import legacy_work_sync
import test_archived_failed_request_continuation as failed_request_fixtures
import test_archived_scheduled_assignment_continuation as assignment_fixtures
import test_archived_scheduled_routine_continuation as routine_fixtures
import test_finished_routine_workspace as workspace_fixtures
import test_native_kanban as native_fixtures
import test_workspace_reflog_rewrite as reflog_fixtures

setUpModule, tearDownModule = legacy_work_sync.scope()

# The saved shape of the request: its relay envelope, its original card, and the runs that ended on that card.
ENVELOPE = '5d0c2b7e9a4f4c1e8b3a6d2f0e9c7b14'
NATIVE = 't_225d91e8'
REQUEST = 'Preserve the legacy rental copy, and check it with the copy reviewer.'
QUESTION = 'Does the legacy rental copy read well?'
ANSWER = 'The copy reads well. Keep the second paragraph.'
REASON = 'SCHEDULED_UNTIL=2026-10-20T16:00:00Z. The local copy edit is unfinished. Recheck it in this worktree.'
# Ten earlier turns parked the card, and one of them asked for review. The helper turn and the last turn follow.
HISTORY = [dict(id=run, status=outcome, outcome=outcome, ended=True) for run, outcome in (
    (29156, 'scheduled'), (29197, 'scheduled'), (29199, 'scheduled'), (29234, 'scheduled'), (29235, 'scheduled'),
    (29242, 'review_requested'), (29282, 'scheduled'), (29291, 'scheduled'), (29297, 'scheduled'), (29616, 'scheduled'))]
HELPER_RUN, RUN = 29623, 29647
# The unfinished local copy edit that the discarded worktree held. Only its archive keeps it.
COPY = 'Legacy rentals stay available.\n\nRenters keep access for the rental period.\n'
EDITED = 'Legacy rentals stay available to everyone who rented them.\n\nRenters keep access for the whole rental period.\n'
NOTE = 'Unfinished: recheck the second paragraph against the help center.\n'
BRANCH = 'gianfranco/preserve-legacy-rentals'
SOURCE = assignment_fixtures.SOURCE
stamp = routine_fixtures.stamp


class ScheduledOriginal:
    """A real Service whose relayed request parked its original card, completed, and had that card archived."""

    adapter = routine_fixtures.ScheduledOccurrence.adapter
    update = routine_fixtures.ScheduledOccurrence.update
    receipt = routine_fixtures.ScheduledOccurrence.receipt
    cancellations = routine_fixtures.ScheduledOccurrence.cancellations

    def setUp(self):
        self.store_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.store_tmp.cleanup)
        self.store = Service(self.store_tmp.name, FixtureProvider(self.store_tmp.name), start_workers=False)
        self.addCleanup(self.store.close)
        self.desk = self.store.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'test-phone')
        self.reviews = self.store.mutate('POST', '/v1/bots', {'name': 'Reviews'}, str(uuid4()), 'test-phone')
        self.helper = self.store.mutate('POST', '/v1/bots', {'name': 'Copy'}, str(uuid4()), 'test-phone')
        self.runs, self.calls = {}, []
        self.store.kanban = Mock(tasks={})
        self.store.kanban.call.side_effect = self.adapter

    def working(self, bot, text):
        task = self.store.mutate('POST', '/v1/bots/' + bot['id'] + '/messages', {'text': text}, str(uuid4()), 'test-phone')
        return self.update(task['id'], status='working')

    def claim(self, owner, run):
        """Hermes claims the original card for the current input. Board.sync reuses it while it is not done or archived."""
        turn = self.store._get('tasks', owner['id'])['_native_turn_id']
        self.store.kanban.tasks[owner['id']].update(status='running', current_run_id=run, worker_pid=4000 + run % 1000,
                                                    claim_lock='gumclaw:13195', input_id=turn,
                                                    last_activity_at=time.time() - 60)
        self.runs[NATIVE].append(dict(id=run, status='running', outcome=None, ended=False))
        return self.update(owner['id'], status='working', _native_outcome_id=NATIVE, _kanban_started=run,
                           _kanban_claim=dict(native_id=NATIVE, external_id=owner['id'], input=turn, run_id=run,
                                              claim_lock='gumclaw:13195', workspace=str(self.workspace)))

    def park(self, owner):
        """The worker parks the original card with kanban_schedule, and Hermes reports the run that it ended."""
        run = self.store._get('tasks', owner['id'])['_kanban_claim']['run_id']
        self.receipt(owner['id'], 'kanban_schedule', dict(task_id=NATIVE, reason=REASON),
                     dict(ok=True, task_id=NATIVE, run_id=run, status='scheduled', reason=REASON))
        self.store.kanban.tasks[owner['id']].update(status='scheduled', current_run_id=None, worker_pid=None,
                                                    claim_lock=None, last_activity_at=time.time() - 60)
        self.runs[NATIVE][-1].update(status='scheduled', outcome='scheduled', ended=True)

    def original(self, source=None):
        """The saved shape: Desk relayed the request, a helper's result was new input, and the settle pass archived the card."""
        requester = self.working(self.desk, 'Ask Reviews to preserve the legacy rental copy.')
        delivered = self.store._store_bot_message(requester['id'], dict(
            target_bot_id=self.reviews['id'], text=REQUEST, native_envelope_id=ENVELOPE))
        owner = self.store._get('tasks', delivered['task_id'])
        self.assertEqual(owner['_native_relay'], dict(sender_bot_id=self.desk['id'], envelope_id=ENVELOPE))
        self.assertEqual((owner['source_bot_id'], owner['source_task_id'], owner['_reply_to_task_id']),
                         (self.desk['id'], requester['id'], requester['id']))
        self.workspace = self.store.root / 'workspaces' / owner['id']
        self.workspace.mkdir(parents=True)
        owner = self.update(owner['id'], _native_intake=True, _execution_session_id=owner['id'], _native_turn_id=str(uuid4()),
                            _conversation_epoch=self.store._setting('conversation_epoch'),
                            **({'_source_assignment': dict(source)} if source else {}))
        self.store.kanban.tasks[owner['id']] = dict(
            external_id=owner['id'], id=NATIVE, status='ready', tenant=self.reviews['id'],
            idempotency_key='coconut:' + owner['id'], created_by='coconut', current_run_id=None, worker_pid=None,
            claim_lock=None, input_id=owner['_native_turn_id'], workspace_path=str(self.workspace),
            last_activity_at=time.time() - 60, dependencies=[], dispatch_blocker=None, block_kind=None)
        self.runs[NATIVE] = copy.deepcopy(HISTORY)
        # The helper turn asks the copy reviewer, parks the card, and waits for the answer.
        owner = self.claim(owner, HELPER_RUN)
        asked = self.store._store_bot_message(owner['id'], dict(target_bot_id=self.helper['id'], text=QUESTION))
        self.park(owner)
        self.store.emit(owner['id'], 'completed', dict(summary='Waiting for the copy review.'))
        owner = self.store._get('tasks', owner['id'])
        self.assertEqual((owner['status'], owner['_waiting_on']), ('waiting', [asked['task_id']]))
        # The answer returns to the owner. It is new input on the same scheduled card.
        self.update(asked['task_id'], status='working')
        self.store.emit(asked['task_id'], 'completed', dict(summary=ANSWER))
        self.store._drain_returns()
        owner = self.store._get('tasks', owner['id'])
        self.assertEqual((owner['status'], owner['_waiting_on']), ('queued', []))
        self.assertEqual(owner['_bot_messages'][-1]['_return_source_task_id'], asked['task_id'])
        self.helper_task = self.store._get('tasks', asked['task_id'])
        # The last turn applies the copy edit locally, parks the card again, and completes.
        owner = self.claim(owner, RUN)
        self.park(owner)
        self.store.emit(owner['id'], 'completed', dict(summary='Complete.'))
        self.store._drain_returns()
        owner = self.store._get('tasks', owner['id'])
        self.assertEqual(owner['status'], 'completed')
        self.assertFalse(owner.get('_return_pending'))
        # A day later the settle pass archives the scheduled card, and reconciliation keeps the completed task.
        self.update(owner['id'], updated_at=stamp(SETTLE_SECONDS + 1))
        card = self.store.kanban.tasks[owner['id']]
        card['last_activity_at'] = time.time() - SETTLE_SECONDS - 1
        self.store._settle_native_orphans()
        self.assertIn({'op': 'archive_settled', 'id': NATIVE, 'summary': 'Complete.'}, self.calls)
        self.assertEqual(card['status'], 'archived', 'The settle pass completed unfinished scheduled work.')
        self.store._reconcile_kanban()
        owner = self.store._get('tasks', owner['id'])
        self.assertEqual(owner['status'], 'completed')
        self.assertEqual(self.cancellations(owner), [])
        # The saved shape has no continuation, no successor, and no routine record.
        self.assertEqual([r['outcome'] for r in self.runs[NATIVE]], [r['outcome'] for r in HISTORY] + ['scheduled'] * 2)
        self.assertEqual(self.store._continuation_turns(owner), set())
        self.assertFalse(any(field in owner for field in routine_coordinator.ROUTINE_PROVENANCE))
        self.requester = self.update(requester['id'], status='working')
        return owner

    def resume(self, caller=None, message='Finish the legacy rental copy edit.', identity=NATIVE):
        self.assertFalse(self.store.db.in_transaction, 'A continuation must not start inside another transaction.')
        return self.store.emit((caller or self.requester)['id'], 'work_continue', dict(task_id=identity, message=message))

    def settled(self, owner):
        card = self.store.kanban.tasks[owner['id']]
        return self.store.settled_scheduled_continuation(owner, card, lambda: self.adapter({'op': 'completion', 'id': card['id']})['card'])

    def assertRejected(self, owner, pattern='.', caller=None, identity=NATIVE):
        before = self.store._get('tasks', owner['id'])
        with self.assertRaisesRegex(ServiceError, pattern):
            self.resume(caller, identity=identity)
        self.assertEqual(self.store._get('tasks', owner['id']), before)

    def deliveries(self, owner):
        return [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT data FROM deliveries WHERE json_extract(data,'$.source_task_id')=? ORDER BY rowid", (owner['id'],))]


class ArchivedScheduledOriginalAdmissionTests(ScheduledOriginal, unittest.TestCase):
    def test_saved_shape_continues_its_original_card_in_the_same_execution(self):
        owner = self.original()
        before = copy.deepcopy(owner)
        tasks, results = {t['id'] for t in self.store._all('tasks')}, self.deliveries(owner)
        self.assertTrue(self.settled(owner))
        receipt = self.resume()
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=NATIVE, status='queued'))
        self.assertIn({'op': 'completion', 'id': NATIVE}, self.calls)
        continued = self.store._get('tasks', owner['id'])
        for key in ('id', 'bot_id', 'session_id', 'thread_id', 'exchange_id', 'conversation_bot_id', 'source_bot_id',
                    'source_task_id', 'parent_task_id', '_reply_to_task_id', '_execution_session_id', '_kanban_claim',
                    '_kanban_started', '_native_outcome_id', '_native_relay', '_native_intake', '_work_context_id'):
            self.assertEqual(continued.get(key), before.get(key), key)
        self.assertEqual((continued['status'], continued['_native_outcome_id']), ('queued', NATIVE))
        self.assertNotEqual(continued['_native_turn_id'], before['_native_turn_id'])
        self.assertEqual(continued['_bot_messages'][:-1], before['_bot_messages'])
        self.assertEqual(continued['_bot_messages'][-1]['sender_bot_id'], self.desk['id'])
        # An ordinary request gains no routine record, and no new task or card owns the execution.
        self.assertFalse(any(field in continued for field in routine_coordinator.ROUTINE_PROVENANCE))
        self.assertEqual(self.store.native_routine_completion_input(continued), {})
        self.assertEqual({t['id'] for t in self.store._all('tasks')}, tasks)
        self.assertEqual({k: c['id'] for k, c in self.store.kanban.tasks.items()}, {owner['id']: NATIVE})
        # A replay returns the saved receipt and changes nothing.
        self.assertEqual(self.resume(), receipt)
        self.assertEqual(self.store._get('tasks', owner['id']), continued)
        # Every saved receipt, result, and run stays.
        self.assertEqual(self.deliveries(owner), results)
        self.assertEqual([r['id'] for r in self.runs[NATIVE]], [r['id'] for r in HISTORY] + [HELPER_RUN, RUN])
        self.assertEqual(self.cancellations(owner), [])
        # The next row names the archived card and the new input, in the execution's own workspace.
        row = self.store._kanban_row(continued, continued, 'ready', 10)
        self.assertEqual((row['native_id'], row['input'], row['previous_input'], row['workspace']),
                         (NATIVE, continued['_native_turn_id'], before['_native_turn_id'], str(self.workspace)))
        # Reconciliation before dispatch reads the archived card of the earlier input. It neither cancels nor completes.
        settled = [c for c in self.calls if c['op'] == 'archive_settled']
        self.store._reconcile_kanban()
        self.assertEqual(self.store._get('tasks', owner['id']), continued)
        self.assertEqual(self.cancellations(owner), [])
        self.assertEqual([c for c in self.calls if c['op'] == 'archive_settled'], settled)

    def test_successor_returns_the_result_to_the_original_requester_once(self):
        owner = self.original()
        receipt = self.resume()
        turn = self.store._get('tasks', owner['id'])['_native_turn_id']
        returned = [d for d in self.deliveries(owner) if d['target_bot_id'] == self.desk['id']]
        # Board.sync makes the successor of the original card, and the reconciler binds it before Hermes claims it.
        card = self.store.kanban.tasks[owner['id']]
        self.store.kanban.tasks[owner['id']] = dict(card, id='t_successor', status='ready', input_id=None,
                                                    idempotency_key='coconut-followup:' + owner['id'] + ':' + turn,
                                                    last_activity_at=time.time())
        self.store._reconcile_kanban()
        queued = self.store._get('tasks', owner['id'])
        self.assertEqual((queued['status'], queued['_native_outcome_id'], queued['_native_turn_id']),
                         ('queued', 't_successor', turn))
        self.assertEqual(self.resume(), receipt)
        self.assertEqual(self.store._get('tasks', owner['id']), queued)
        self.store.kanban.tasks[owner['id']].update(status='running', current_run_id=29700, worker_pid=4700,
                                                    claim_lock='gumclaw:13195', input_id=turn)
        self.runs['t_successor'] = [dict(id=29700, status='running', outcome=None, ended=False)]
        self.update(owner['id'], status='working', _kanban_started=29700, _kanban_claim=dict(
            native_id='t_successor', external_id=owner['id'], input=turn, run_id=29700, claim_lock='gumclaw:13195',
            workspace=str(self.workspace)))
        self.receipt(owner['id'], 'kanban_complete', dict(task_id='t_successor', summary='Preserved the copy.'),
                     dict(ok=True, task_id='t_successor', run_id=29700))
        self.store.emit(owner['id'], 'completed', dict(summary='Preserved the legacy rental copy.'))
        self.store.kanban.tasks[owner['id']].update(status='done', current_run_id=None, worker_pid=None)
        self.runs['t_successor'][-1].update(status='done', outcome='completed', ended=True)
        self.store._reconcile_kanban()
        self.store._drain_returns()
        # A second pass delivers nothing more.
        self.store._drain_returns()
        done = self.store._get('tasks', owner['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['_reply_to_task_id'], self.requester['id'])
        # The result reaches the original requester's route exactly once, as a return callback of that request.
        result = [d for d in self.deliveries(owner) if d['target_bot_id'] == self.desk['id']][len(returned):]
        self.assertEqual([d['text'] for d in result], ['Preserved the legacy rental copy.'])
        callback = self.store._get('tasks', result[0]['receipt']['task_id'])
        self.assertEqual((callback['bot_id'], callback['_is_return'], callback['_assignment_task_id'], callback['source_task_id']),
                         (self.desk['id'], True, self.requester['id'], owner['id']))
        self.assertEqual(self.cancellations(owner), [])
        # The original card keeps its history, and the done successor is no settled scheduled card.
        self.assertEqual([r['id'] for r in self.runs[NATIVE]], [r['id'] for r in HISTORY] + [HELPER_RUN, RUN])
        self.store.kanban.tasks[owner['id']].update(status='archived')
        self.assertRejected(done, identity='t_successor')

    def test_owner_bot_can_continue_and_a_stranger_cannot(self):
        owner = self.original()
        stranger = self.store.mutate('POST', '/v1/bots', {'name': 'Stranger'}, str(uuid4()), 'test-phone')
        self.assertRejected(owner, 'Only the owner', caller=self.working(stranger, 'Continue that copy edit.'))
        # The helper answered, but it neither owns nor requested the work.
        self.assertRejected(owner, 'Only the owner', caller=self.working(self.helper, 'Continue that copy edit.'))
        caller = self.working(self.reviews, 'Finish the copy edit.')
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])

    def test_adapter_without_an_answer_admits_nothing(self):
        owner = self.original()
        adapter = self.adapter

        def silent(request):
            if request['op'] == 'completion':
                raise RuntimeError('The Hermes Kanban adapter stopped.')
            return adapter(request)
        self.store.kanban.call.side_effect = silent
        self.assertRejected(owner, 'not blocked or completed')
        self.store.kanban.call.side_effect = lambda request: {'card': None} if request['op'] == 'completion' else adapter(request)
        self.assertRejected(owner, 'not blocked or completed')
        self.store.kanban.call.side_effect = adapter
        self.assertEqual(self.resume()['task_id'], owner['id'])

    def test_the_first_delivery_statement_uses_the_receipt_index(self):
        plan = ' '.join(row[-1] for row in self.store.db.execute(
            'EXPLAIN QUERY PLAN ' + routine_coordinator.CREATION_DELIVERY, ('x',)))
        self.assertIn('deliveries_receipt', plan)
        self.assertNotIn('SCAN deliveries', plan)


class ArchivedScheduledOriginalGuardTests(ScheduledOriginal, unittest.TestCase):
    """Each case changes one record of the saved shape. Every refusal leaves the request unchanged."""

    def refused(self, cases, pattern='not blocked or completed'):
        for name, change in cases.items():
            with self.subTest(name):
                ScheduledOriginal.setUp(self)  # A fresh store, so one case cannot answer the next.
                owner = self.original()
                card = self.store.kanban.tasks[owner['id']]
                change(owner, card, self.runs[NATIVE])
                self.store._put('tasks', owner)
                owner = self.store._get('tasks', owner['id'])
                self.assertFalse(self.settled(owner))
                self.assertRejected(owner, pattern)

    def test_the_card_must_be_this_request_and_the_scheduled_run_of_its_claim(self):
        self.refused(dict(
            failed_request=lambda owner, card, runs: owner.update(status='failed'),
            waiting_request=lambda owner, card, runs: owner.update(status='waiting'),
            foreign_tenant=lambda owner, card, runs: card.update(tenant=self.desk['id']),
            not_coconut=lambda owner, card, runs: card.update(created_by='human'),
            live_run=lambda owner, card, runs: card.update(current_run_id=RUN + 1),
            live_worker=lambda owner, card, runs: card.update(worker_pid=4343),
            completed_run=lambda owner, card, runs: runs[3].update(status='done', outcome='completed'),
            last_run_blocked=lambda owner, card, runs: runs[-1].update(status='blocked', outcome='blocked'),
            last_run_review=lambda owner, card, runs: runs[-1].update(status='review', outcome='review_requested'),
            last_run_reclaimed=lambda owner, card, runs: runs[-1].update(status='reclaimed', outcome='reclaimed'),
            last_run_open=lambda owner, card, runs: runs[-1].update(ended=False, outcome=None),
            last_run_other=lambda owner, card, runs: runs.append(dict(id=RUN + 1, status='scheduled', outcome='scheduled', ended=True)),
            no_runs=lambda owner, card, runs: runs.clear(),
            idle_archive=lambda owner, card, runs: card.update(last_activity_at=time.time() - IDLE_AGE - 60),
            no_activity=lambda owner, card, runs: card.update(last_activity_at=None)))
        # The binding names a newer card now, so the archived card is history and no owner claims it.
        self.refused(dict(newer_card=lambda owner, card, runs: card.update(id='t_newer', status='ready')), 'mapping does not match')

    def test_the_key_must_be_the_original_key_of_this_request(self):
        self.refused(dict(
            suffix=lambda owner, card, runs: card.update(idempotency_key='coconut:' + owner['id'] + ':' + owner['_native_turn_id']),
            intake=lambda owner, card, runs: card.update(idempotency_key='coconut:' + owner['id'] + ':0123abcd'),
            other_request=lambda owner, card, runs: card.update(idempotency_key='coconut:' + self.requester['id']),
            unauthenticated_followup=lambda owner, card, runs: card.update(
                idempotency_key='coconut-followup:' + owner['id'] + ':' + owner['_native_turn_id']),
            outcome=lambda owner, card, runs: card.update(idempotency_key='coconut-outcome:' + owner['id']),
            missing=lambda owner, card, runs: card.update(idempotency_key=None)))

    def test_the_claim_and_input_must_be_current(self):
        self.refused(dict(
            other_claim_input=lambda owner, card, runs: owner['_kanban_claim'].update(input='an-earlier-input'),
            other_claim_run=lambda owner, card, runs: owner['_kanban_claim'].update(run_id=HELPER_RUN),
            run_not_int=lambda owner, card, runs: owner['_kanban_claim'].update(run_id=str(RUN)),
            other_claim_card=lambda owner, card, runs: owner['_kanban_claim'].update(native_id='t_other'),
            other_claim_owner=lambda owner, card, runs: owner['_kanban_claim'].update(external_id=self.requester['id']),
            not_started=lambda owner, card, runs: owner.update(_kanban_started=HELPER_RUN),
            unclaimed_turn=lambda owner, card, runs: owner.update(_native_turn_id=str(uuid4())),
            accepted_other_input=lambda owner, card, runs: card.update(input_id='an-earlier-input'),
            no_accepted_input=lambda owner, card, runs: card.update(input_id=None)))
        self.refused(dict(other_outcome=lambda owner, card, runs: owner.update(_native_outcome_id='t_other')),
                     'mapping does not match')

    def schedule_receipt(self, owner):
        rows = [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT data FROM native_receipts WHERE json_extract(data,'$.task_id')=? "
            "AND json_extract(data,'$.action.name')='kanban_schedule' ORDER BY rowid DESC LIMIT 1", (owner['id'],))]
        self.assertEqual(rows[0]['turn_id'], owner['_native_turn_id'])
        return rows[0]

    def receipt_change(self, **result):
        def change(owner, card, runs):
            record = self.schedule_receipt(owner)
            value = json.loads(record['result'])
            value.update({key: item() if callable(item) else item for key, item in result.items()})
            for key in [key for key, item in value.items() if item is None]:
                value.pop(key)
            self.store._put('native_receipts', dict(record, result=json.dumps(value)))
        return change

    def record_change(self, **fields):
        def change(owner, card, runs):
            self.store._put('native_receipts', dict(self.schedule_receipt(owner), **{
                key: value() if callable(value) else value for key, value in fields.items()}))
        return change

    def test_the_current_input_needs_its_schedule_receipt(self):
        self.refused(dict(
            missing=lambda owner, card, runs: self.store.db.execute(
                'DELETE FROM native_receipts WHERE id=?', (self.schedule_receipt(owner)['id'],)),
            earlier_turn=self.record_change(turn_id='an-earlier-input'),
            other_card=self.record_change(action=dict(name='kanban_schedule', arguments=dict(task_id='t_other', reason=REASON))),
            other_tool=self.record_change(action=dict(name='kanban_block', arguments=dict(task_id=NATIVE, reason=REASON))),
            other_task=self.record_change(task_id=lambda: self.requester['id']),
            not_ok=self.receipt_change(ok=False),
            other_result_card=self.receipt_change(task_id='t_other'),
            other_run=self.receipt_change(run_id=HELPER_RUN),
            no_run=self.receipt_change(run_id=None),
            not_scheduled=self.receipt_change(status='ready'),
            unreadable=self.record_change(result='not json'),
            not_recorded=self.record_change(result=None)))

    def test_an_unfinished_schedule_call_is_an_unknown_effect(self):
        self.refused(dict(executing=self.record_change(state='executing', result=None)))

    def creation_delivery(self, owner):
        return [(row[0], json.loads(row[1])) for row in self.store.db.execute(
            "SELECT id,data FROM deliveries WHERE json_extract(data,'$.receipt.task_id')=? ORDER BY rowid LIMIT 1",
            (owner['id'],))][0]

    def delivery_change(self, **fields):
        def change(owner, card, runs):
            identity, delivery = self.creation_delivery(owner)
            self.assertEqual(delivery['source_task_id'], owner['source_task_id'])
            fields_now = {key: value() if callable(value) else value for key, value in fields.items()}
            self.store.db.execute('UPDATE deliveries SET data=? WHERE id=?', (json.dumps(dict(delivery, **fields_now)), identity))
        return change

    def test_the_creation_must_be_the_requesting_bots_relay(self):
        self.refused(dict(
            no_relay=lambda owner, card, runs: owner.pop('_native_relay'),
            relay_of_another_bot=lambda owner, card, runs: owner['_native_relay'].update(sender_bot_id=self.helper['id']),
            relay_without_envelope=lambda owner, card, runs: owner['_native_relay'].pop('envelope_id'),
            no_native_intake=lambda owner, card, runs: owner.pop('_native_intake'),
            return_callback=lambda owner, card, runs: owner.update(_is_return=True),
            other_parent=lambda owner, card, runs: owner.update(parent_task_id=self.helper_task['id']),
            no_source_task=lambda owner, card, runs: owner.update(source_task_id=None),
            missing_source_task=lambda owner, card, runs: owner.update(source_task_id='a-missing-task', parent_task_id='a-missing-task'),
            source_of_another_bot=lambda owner, card, runs: owner.update(source_task_id=self.helper_task['id'],
                                                                          parent_task_id=self.helper_task['id']),
            first_message_of_another_bot=lambda owner, card, runs: owner['_bot_messages'][0].update(
                sender_bot_id=self.helper['id']),
            first_message_changed=lambda owner, card, runs: owner['_bot_messages'][0].update(text='Another request.'),
            no_messages=lambda owner, card, runs: owner.update(_bot_messages=[]),
            delivery_from_another_task=self.delivery_change(source_task_id=lambda: self.helper_task['id']),
            delivery_to_another_bot=self.delivery_change(target_bot_id=lambda: self.desk['id']),
            delivery_of_a_continuation=self.delivery_change(turn_id=str(uuid4())),
            delivery_text=self.delivery_change(text='A message that the task never received.'),
            created_twice=lambda owner, card, runs: self.store._event('task.created', self.store._public(owner),
                                                                       owner['bot_id'], owner['id']),
            created_by_another_bot=lambda owner, card, runs: self.store.db.execute(
                "UPDATE events SET bot_id=? WHERE task_id=? AND type='task.created'", (self.helper['id'], owner['id'])),
            routine_trigger=lambda owner, card, runs: self.store._event(
                'routine.triggered', dict(routine_id='a-routine', task_id=owner['id']), owner['bot_id'], owner['id'])))
        # A request without a requesting bot is no relay. Desk no longer requested it, so its caller is refused too.
        self.refused(dict(own_bot_source=lambda owner, card, runs: owner.update(source_bot_id=owner['bot_id'])), 'Only the owner')
        # A creation delivery that is not first does not count either.
        ScheduledOriginal.setUp(self)
        owner = self.original()
        identity, delivery = self.creation_delivery(owner)
        self.store.db.execute('DELETE FROM deliveries WHERE id=?', (identity,))
        self.store._put('deliveries', delivery)
        self.assertFalse(self.settled(owner))
        self.assertRejected(owner, 'not blocked or completed')

    def test_routine_records_keep_the_routine_rule(self):
        cases = {}
        for field in routine_coordinator.ROUTINE_PROVENANCE:
            # Only the routine code writes these, and it writes an occurrence as a record. Even an empty one counts.
            for value in (dict(native_id=NATIVE) if field == '_native_occurrence' else 'x', None, False, {}):
                cases['%s=%r' % (field, value)] = lambda owner, card, runs, field=field, value=value: owner.update({field: value})
        # A routine record makes this a routine occurrence, whose original card stays final under any of its guards.
        self.refused(cases, '.')

    def test_stop_and_cancellation_stay_final(self):
        owner = self.original()
        self.assertRejected(self.update(owner['id'], status='cancelled'))
        for name in ('stopped_before', 'requester_stop'):
            with self.subTest(name):
                ScheduledOriginal.setUp(self)
                owner = self.original()
                if name == 'stopped_before':
                    # A Stop that the person later reversed still leaves the cancellation on record.
                    self.store._event('task.updated', dict(self.store._public(owner), status='cancelled'),
                                      owner['bot_id'], owner['id'])
                    self.assertFalse(self.settled(owner))
                    self.assertRejected(owner, 'not blocked or completed')
                else:
                    with self.store.transaction():
                        self.store._cancel_one(self.store._get('tasks', owner['id']), [], include_finished=True)
                    self.assertRejected(self.store._get('tasks', owner['id']))

    def test_idle_archive_by_the_person_stays_final(self):
        owner = self.original()
        for field in ('task_id', '_execution_task_id'):
            with self.subTest(field=field):
                self.store._put('decisions', {'id': 'archived-question', 'task_id': self.requester['id'],
                                              'bot_id': owner['bot_id'], 'status': 'rejected', 'decided_by': 'archive',
                                              'outcome': 'Task archived.', field: owner['id']})
                self.assertFalse(self.settled(owner))
                self.assertRejected(owner, 'not blocked or completed')
                self.store.db.execute("DELETE FROM decisions WHERE id='archived-question'")
        self.assertEqual(self.resume()['task_id'], owner['id'])

    def test_undelivered_result_open_input_and_unknown_effects_hold(self):
        self.refused(dict(
            undelivered_result=lambda owner, card, runs: owner.update(_return_pending=dict(text='Saved result')),
            steering=lambda owner, card, runs: owner.update(_steering=['Use the other checkout.']),
            live_message=lambda owner, card, runs: owner.update(_live_messages={'m': 'Wait.'}),
            blocked_followup=lambda owner, card, runs: owner.update(_blocked_followup=dict(
                message_ids=['m'], source_ids=['m'], attachment_ids=[])),
            native_reply=lambda owner, card, runs: owner.update(_native_reply_pending=dict(text='Reply')),
            resume_decision=lambda owner, card, runs: owner.update(_resume_decision='decision'),
            background_completion=lambda owner, card, runs: owner.update(_background_completion=dict(id='process')),
            unknown_tools=lambda owner, card, runs: owner.update(_self_recovery=dict(unknown_tools=[dict(id='write')])),
            unknown_receipt=lambda owner, card, runs: self.store._put('native_receipts', dict(
                id='unknown', task_id=owner['id'], turn_id=owner['_native_turn_id'], state='executing',
                action_hash='hash', action={'name': 'write_file', 'arguments': {}}, created_at='2026-10-20T14:55:00Z'))))
        # A dependency still holds the request with its own refusal.
        ScheduledOriginal.setUp(self)
        owner = self.original()
        self.assertRejected(self.update(owner['id'], _waiting_on=['dependency']), 'existing dependencies')

    def test_live_worker_pending_decisions_and_peer_work_hold(self):
        owner = self.original()
        self.store.running[owner['id']] = Mock(is_alive=Mock(return_value=True))
        self.assertFalse(self.settled(owner))
        self.assertRejected(owner)
        self.store.running.pop(owner['id'])
        self.store._put('decisions', dict(id='pending', task_id=owner['id'], bot_id=owner['bot_id'], status='pending'))
        self.assertRejected(owner, 'decision must be answered')
        self.store.db.execute("DELETE FROM decisions WHERE id='pending'")
        peer = self.working(self.reviews, 'Peer turn')
        self.update(peer['id'], _execution_session_id=owner['id'])
        self.assertRejected(owner, 'active work')
        self.store.db.execute('DELETE FROM tasks WHERE id=?', (peer['id'],))
        self.assertEqual(self.resume()['task_id'], owner['id'])

    def test_pending_decision_about_the_same_source_holds_until_answered(self):
        owner = self.original(source=SOURCE)
        reconciler = self.update(self.working(self.reviews, 'Reconcile the legacy rental copy issue.')['id'],
                                 status='completed', _execution_session_id='source-reconciliation',
                                 _source_assignment=dict(SOURCE))
        decision = dict(id='merits', task_id=reconciler['id'], bot_id=self.reviews['id'], status='pending',
                        inbox_kind='proposal', title='Keep the legacy rental copy?')
        self.store._put('decisions', decision)
        self.assertTrue(self.settled(owner))
        self.assertRejected(owner, 'decision about the same source is pending')
        self.store._put('decisions', dict(decision, status='approved', decided_by='test-phone'))
        self.assertEqual(self.resume()['task_id'], owner['id'])

    def test_a_followup_key_still_needs_its_authenticated_continuations(self):
        # The relay and the schedule receipt prove the original card only. A successor key keeps its own proof.
        owner = self.original()
        card = self.store.kanban.tasks[owner['id']]
        card['idempotency_key'] = 'coconut-followup:' + owner['id'] + ':' + owner['_native_turn_id']
        self.assertFalse(self.settled(owner))
        self.assertRejected(owner, 'not blocked or completed')
        card['idempotency_key'] = 'coconut:' + owner['id']
        self.assertEqual(self.resume()['task_id'], owner['id'])



def unfinished_copy_edit(path):
    """The local copy edit in the owner's worktree: a tracked file changed, an untracked note, and an ignored preview."""
    (path / 'copy.md').write_text(EDITED)
    (path / 'notes.txt').write_text(NOTE)
    (path / 'build').mkdir()
    (path / 'build' / 'preview.html').write_text('<p>Legacy rentals</p>\n')


def archived_patch(binding):
    with tarfile.open(binding['retired']['archive']) as tar:
        return tar.extractfile('unstaged.patch').read(), tar.extractfile('staged.patch').read()


class ArchivedScheduledOriginalWorkerTests(ScheduledOriginal, unittest.TestCase):
    """The provider starts a real worker process for the continued owner. Its native modules are the shared stub.

    The owner's worktree held an unfinished local copy edit when the service discarded it, as it does for an idle
    outcome whose pull request merged. The branch and the discard archive keep the edit, and only the owner's own turn
    restores them, through the provider's normal workspace preparation. The continuation itself restores nothing.
    """

    def setUp(self):
        workspace_fixtures.FinishedRoutineWorkspaceTests.setUp(self)
        ScheduledOriginal.setUp(self)
        reflog_fixtures.use_test_git(self)
        self.registry = self.data / 'workspaces'
        self.owner = self.original()
        reflog_fixtures.retain_worktree(self, self.root, self.registry, self.owner['id'])
        (self.path / '.gitignore').write_text('build/\n')
        (self.path / 'copy.md').write_text(COPY)
        git(self.path, 'add', '.gitignore', 'copy.md')
        git(self.path, 'commit', '-q', '-m', 'Legacy rental copy')
        git(self.path, 'branch', '-m', BRANCH)
        unfinished_copy_edit(self.path)
        self.head = git(self.path, 'rev-parse', 'HEAD')
        self.unstaged = changes(self.path)[1]
        self.assertTrue(self.unstaged)
        self.identity = self.owner['id'] + '-key'
        with patch('coconut.work_workspace.holders', return_value=set()):
            self.assertTrue(discard(self.registry, self.identity, lambda wait: True, merged=True))
        self.assertFalse(self.path.exists())
        self.discarded = json.loads(self.manifest.read_text())[self.identity]
        self.assertEqual((self.discarded['branch'], self.discarded['retired']['done'], self.discarded['retired']['head']),
                         (BRANCH, True, self.head))
        self.archive = Path(self.discarded['retired']['archive'])
        self.assertEqual(self.archive.parent, self.registry / 'archive' / 'done')
        self.assertEqual(archived_patch(self.discarded), (self.unstaged, b''))
        self.archive_bytes = self.archive.read_bytes()

    turn = failed_request_fixtures.ArchivedFailedRequestWorkerTests.turn

    def continued_task(self):
        """The continued owner after Board.sync made its successor in the registry's path and Hermes claimed it."""
        self.resume()
        continued = self.store._get('tasks', self.owner['id'])
        # Admission restores nothing. Only the owner's own turn does.
        self.assertFalse(self.path.exists())
        self.assertEqual(json.loads(self.manifest.read_text())[self.identity], self.discarded)
        # Board.sync reads the registry's path hint for the successor. Hermes creates that empty folder for its claim.
        workspace = preferred(self.registry, continued['_execution_session_id'], verify=False)
        self.assertEqual(workspace, self.path)
        workspace.mkdir(parents=True)
        claim = dict(continued['_kanban_claim'], native_id='t_successor', run_id=RUN + 100,
                     input=continued['_native_turn_id'], workspace=str(workspace))
        return dict(id=continued['id'], title=continued['title'], bot_id=self.bot['id'],
                    _execution_session_id=continued['_execution_session_id'], _native_turn_id=continued['_native_turn_id'],
                    _native_outcome_id='t_successor', _kanban_claim=claim, _kanban_external_id=continued['id'],
                    _kanban_workspace=str(workspace), _reply_to_task_id=continued['_reply_to_task_id'])

    def test_the_owner_turn_restores_the_exact_unfinished_edit(self):
        task = self.continued_task()
        events, rows = self.turn(task)
        self.assertIn(('completed', {'summary': 'Complete.'}), events)
        self.assertEqual([row[1]['cwd'] for row in rows if row[0] == 'session'], [str(self.path)])
        self.assertEqual(git(self.path, 'branch', '--show-current'), BRANCH)
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD'), self.head)
        # The restored checkout holds exactly the saved patch, and the untracked and ignored files.
        staged, unstaged, untracked = changes(self.path)
        self.assertEqual((staged, unstaged, untracked), (b'', self.unstaged, ['notes.txt']))
        self.assertEqual(unstaged, archived_patch(self.discarded)[0])
        self.assertEqual((self.path / 'copy.md').read_text(), EDITED)
        self.assertEqual((self.path / 'notes.txt').read_text(), NOTE)
        self.assertEqual((self.path / 'build' / 'preview.html').read_text(), '<p>Legacy rentals</p>\n')
        saved = json.loads(self.manifest.read_text())
        restored = saved[self.identity]
        self.assertNotIn('retired', restored)
        self.assertEqual({k: restored[k] for k in ('path', 'branch', 'repository', 'outcome')},
                         {k: self.discarded[k] for k in ('path', 'branch', 'repository', 'outcome')})
        # The restore keeps the one registered binding of the execution. No second binding or branch owns the edit.
        self.assertEqual([k for k, v in saved.items() if v['outcome'] == self.owner['id']], [self.identity])
        self.assertEqual(git(self.repo, 'branch', '--list', '--format=%(refname:short)').split(), sorted(['main', BRANCH]))
        # The discard archive stays as it was. It is the durable copy of the edit.
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)

    def test_another_execution_does_not_restore_it(self):
        before = self.manifest.read_text()
        other = str(uuid4())
        scratch = self.registry / other
        scratch.mkdir(parents=True)
        events, rows = self.turn(dict(id=other, title='Other work', bot_id=self.bot['id'], _execution_session_id=other,
                                      _native_turn_id='other-turn', _native_outcome_id='t_other', _kanban_external_id=other,
                                      _kanban_workspace=str(scratch)))
        self.assertIn(('completed', {'summary': 'Complete.'}), events)
        self.assertEqual([row[1]['cwd'] for row in rows if row[0] == 'session'], [str(scratch)])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.manifest.read_text(), before)
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)


class ArchivedScheduledOriginalNativeTests(unittest.TestCase):
    """The installed Hermes kernel: the settle pass archives the original card, and continuation dispatches a successor.

    Hermes syncs, claims, schedules, settles, and dispatches the cards. The owner's successor turn goes through the real
    provider's workspace preparation, which restores the discarded worktree, before the fixture worker reports.
    """

    write = native_fixtures.NativeKanbanTests.write
    native_program = native_fixtures.NativeKanbanTests.native_program
    tick = native_fixtures.NativeKanbanTests.tick
    card_state = routine_fixtures.ArchivedScheduledRoutineNativeTests.native
    completed = routine_fixtures.ArchivedScheduledRoutineNativeTests.completed

    def setUp(self):
        if not (native_fixtures.SOURCE and native_fixtures.PYTHON):
            if os.environ.get('COCONUT_REQUIRE_NATIVE'):
                self.fail('Native CI must install Hermes and set its paths. These tests must not skip.')
            self.skipTest('Set Coconut Hermes paths for native integration checks.')
        # The real provider and its stub worker, as in the worker tests. Its registry is the service's own below.
        workspace_fixtures.FinishedRoutineWorkspaceTests.setUp(self)
        reflog_fixtures.use_test_git(self)
        self.worker_bot, self.git_root = self.bot, self.root
        try:
            native_fixtures.NativeKanbanTests.setUp(self)
            self.service.kanban.call({'op': 'snapshot'})
        except RuntimeError as error:
            # A Hermes descendant fence makes the fixture's own board read-only, so the adapter cannot start.
            # Report Hermes' own reason instead of working around the fence.
            log = Path(self.tmp.name) / 'kanban' / 'adapter.log'
            reason = log.read_text().strip().splitlines()[-1:] if log.exists() else []
            self.fail('The native fixture board did not start: %s %s' % (error, reason))
        self.assertTrue((Path(self.tmp.name) / 'kanban' / 'kanban.db').is_file())
        from types import SimpleNamespace
        import sys
        from coconut.providers import HermesProvider
        catalog = SimpleNamespace(root=self.home, resolve_runtime=lambda *args, **kwargs: dict(self.native))
        self.worker = HermesProvider(self.service.root, python=sys.executable, hermes_root=self.source, catalog=catalog)
        self.addCleanup(self.worker.close)
        self.worker.environment = lambda bot_id: ({
            'PATH': os.environ.get('PATH', ''), 'HOME': str(self.home), 'HERMES_HOME': str(self.home),
            'TMPDIR': str(self.git_root), 'PYTHONPATH': str(self.source), 'WORKER_CAPTURE': str(self.capture),
            'EXPECTED_NATIVE_KEY': self.native['api_key']}, self.git_root)
        self.registry = self.service.root / 'workspaces'
        self.reviews = self.write('/v1/bots', {'name': 'Reviews', 'role': ''})
        self.helper = self.write('/v1/bots', {'name': 'Copy', 'role': ''})
        self.provider.tasks, self.restored = [], []
        self.owner_id, self.continued = None, False
        start = self.provider.start_task

        def record(task, bot, history, emit):
            self.provider.tasks.append(copy.deepcopy(task))
            if task['id'] == self.owner_id and self.continued:
                # The real provider prepares the owner's own workspace from the dispatched task, as a running host does.
                self.restored.append(self.successor_turn(copy.deepcopy(task)))
            elif task['id'] == self.owner_id:
                # The helper's answer is new input on the scheduled original card. The worker edits the copy locally
                # and parks the card again.
                unfinished_copy_edit(self.path)
                self.park(task)
            start(task, bot, history, emit)
        self.provider.start_task = record

    def park(self, task):
        """The worker parks its card with kanban_schedule. Hermes reports where it landed, and the bridge saves that."""
        claim = task['_kanban_claim']
        landed = self.native_program(
            'import json\nassert board.kb.schedule_task(board.conn, sys.argv[2], reason=sys.argv[4], expected_run_id=int(sys.argv[3]))\n'
            'run = board.kb.latest_run(board.conn, sys.argv[2])\nt = board.kb.get_task(board.conn, sys.argv[2])\n'
            'print(json.dumps(dict(ok=True, task_id=t.id, run_id=run.id, status=t.status, reason=sys.argv[4])))',
            claim['native_id'], str(claim['run_id']), REASON)
        self.assertEqual(json.loads(landed)['run_id'], claim['run_id'])
        call = self.service.emit(task['id'], 'native_call', dict(name='kanban_schedule',
                                                                  arguments=dict(task_id=claim['native_id'], reason=REASON)))
        self.service.emit(task['id'], 'native_result', dict(id=call['id'], result=landed))

    def update(self, identity, **fields):
        self.assertFalse(self.service.db.in_transaction, 'A fixture write must not nest in another transaction.')
        with self.service.transaction():
            task = self.service._get('tasks', identity)
            task.update(fields)
            self.service._put('tasks', task)
        return task

    def successor_turn(self, task):
        """The real provider prepares the dispatched task's workspace and runs the stub worker. Then the worker ends its card."""
        # Hermes creates the empty folder of a dir workspace for its claim. Nothing in it comes from the archive yet.
        self.provider_was = (self.path.exists() and any(self.path.iterdir()),
                             json.loads(self.manifest.read_text())[self.identity].get('retired'))
        events, bot = [], dict(self.worker_bot, id=task['bot_id'])
        self.worker.prepare_kanban_worker(task['_kanban_external_id'], bot, task['_kanban_workspace'], task['_execution_session_id'])
        with patch.object(self.worker, 'observe_bot', return_value=bot), patch.object(
                self.worker, 'resolve_task_model', return_value=dict(
                    model='gpt-6-astra', provider='openai-codex', base_url=self.native['base_url'])):
            self.worker._run_turn('start', task, bot, [], lambda kind, data: events.append((kind, data)), isolated=True)
        scopes = Path(str(self.capture) + '.cwd')
        rows = [json.loads(line) for line in scopes.read_text().splitlines()] if scopes.exists() else []
        scopes.unlink(missing_ok=True)
        claim = task['_kanban_claim']
        self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='Preserved the copy.', "
                            "expected_run_id=int(sys.argv[3]))", claim['native_id'], str(claim['run_id']))
        call = self.service.emit(task['id'], 'native_call', dict(name='kanban_complete', arguments=dict(task_id=claim['native_id'])))
        self.service.emit(task['id'], 'native_result', dict(id=call['id'], result=json.dumps(
            dict(ok=True, task_id=claim['native_id'], run_id=claim['run_id']))))
        return events, rows

    def dispatch_state(self, owner):
        """The identities and native state that explain a missing turn. No prompt or message text."""
        task = self.service._get('tasks', owner['id'])
        card = self.service.kanban.tasks.get(owner['id']) or {}
        return json.dumps(dict(
            started=[{k: t.get(k) for k in ('id', 'bot_id', '_native_outcome_id', '_native_turn_id')} for t in self.provider.tasks],
            prepared=[list(p[:3]) for p in self.provider.prepared],
            owner=dict({k: task.get(k) for k in ('status', '_native_outcome_id', '_native_turn_id', '_kanban_started')},
                       return_pending=bool(task.get('_return_pending')),
                       claim={k: (task.get('_kanban_claim') or {}).get(k) for k in ('native_id', 'input', 'run_id')}),
            card={k: card.get(k) for k in ('id', 'status', 'input_id', 'current_run_id', 'worker_pid', 'idempotency_key')}),
            default=str, sort_keys=True)

    def owner_turns(self, owner, count):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len([t for t in self.provider.tasks if t['id'] == owner['id']]) < count:
            self.tick()
            time.sleep(.05)
        turns = [t for t in self.provider.tasks if t['id'] == owner['id']]
        self.assertEqual(len(turns), count, 'The owner turn did not start: ' + self.dispatch_state(owner))
        return turns[-1]

    def results(self, owner):
        return [json.loads(row[0]) for row in self.service.db.execute(
            "SELECT data FROM deliveries WHERE json_extract(data,'$.source_task_id')=? ORDER BY rowid", (owner['id'],))]

    def returned(self, owner, count):
        """Tick until `count` results of the owner reached Desk, and every return callback turn ended."""
        def callbacks():
            return [t for t in self.service._all('tasks') if t.get('_is_return') and t.get('source_task_id') == owner['id']
                    and t['bot_id'] == self.desk['id']]
        def done():
            return (len([d for d in self.results(owner) if d['target_bot_id'] == self.desk['id']]) >= count
                    and not self.service._get('tasks', owner['id']).get('_return_pending')
                    and all(t['status'] not in self.service.ACTIVE for t in callbacks()))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not done():
            self.tick()
            time.sleep(.05)
        self.assertTrue(done(), 'The result did not reach the requester: ' + self.dispatch_state(owner))
        for task in callbacks():
            worker = self.service.running.get(task['id'])
            if worker:
                worker.join(20)
                self.assertFalse(worker.is_alive(), 'The return callback turn did not end.')
        return callbacks()

    def scheduled_original(self):
        """Desk relays the request, its worker edits the worktree and parks the original card, and the settle pass archives it."""
        self.desk = self.bot
        requester = self.update(self.write('/v1/bots/' + self.desk['id'] + '/messages',
                                           {'text': 'Ask Reviews to preserve the legacy rental copy.'})['id'], status='working')
        delivered = self.service._store_bot_message(requester['id'], dict(
            target_bot_id=self.reviews['id'], text=REQUEST, native_envelope_id=ENVELOPE))
        # The requester's turn ends after it relays. The result returns to it in a later turn.
        self.update(requester['id'], status='completed')
        self.owner_id = delivered['task_id']
        owner = self.update(delivered['task_id'], _native_intake=True, _native_turn_id=str(uuid4()),
                            _execution_session_id=delivered['task_id'],
                            _conversation_epoch=self.service._setting('conversation_epoch'))
        self.assertEqual(owner['_native_relay'], dict(sender_bot_id=self.desk['id'], envelope_id=ENVELOPE))
        # coconut_workspace bound the owner's worktree in the service's registry before the turn edited it.
        self.registry.mkdir(parents=True, exist_ok=True)
        reflog_fixtures.retain_worktree(self, self.git_root, self.registry, owner['id'])
        (self.path / '.gitignore').write_text('build/\n')
        (self.path / 'copy.md').write_text(COPY)
        git(self.path, 'add', '.gitignore', 'copy.md')
        git(self.path, 'commit', '-q', '-m', 'Legacy rental copy')
        git(self.path, 'branch', '-m', BRANCH)
        self.identity = owner['id'] + '-key'
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.assertEqual((row['workspace'], row['workspace_changed']), (str(self.path), True))
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        card = self.service.kanban.tasks[owner['id']]
        original = card['id']
        self.assertEqual(card['idempotency_key'], 'coconut:' + owner['id'])
        run = int(self.native_program('c = board.kb.claim_task(board.conn, sys.argv[2])\nprint(c.current_run_id)', original))
        # The saved shape: the retained Work migration bound the original card as the outcome while the turn worked.
        # Board.promote returns a card that the request already owns, so the outcome is that original card.
        owner = self.update(owner['id'], status='working', _native_outcome_id=original, _kanban_started=run, _kanban_claim=dict(
            native_id=original, external_id=owner['id'], input=row['input'], run_id=run, workspace=card['workspace_path']))
        # The first turn asks the copy reviewer, parks the card, and waits for the answer.
        asked = self.service._store_bot_message(owner['id'], dict(target_bot_id=self.helper['id'], text=QUESTION))
        self.park(self.service._get('tasks', owner['id']) | {'_kanban_claim': owner['_kanban_claim']})
        self.service.emit(owner['id'], 'completed', dict(summary='Waiting for the copy review.'))
        owner = self.service._get('tasks', owner['id'])
        self.assertEqual((owner['status'], owner['_waiting_on']), ('waiting', [asked['task_id']]))
        # Real dispatcher passes run the helper, return its answer, and dispatch that new input on the original card.
        self.owner_turns(owner, 1)
        self.completed(owner)
        self.service.kanban.call({'op': 'snapshot'})
        self.tick()
        owner = self.service._get('tasks', owner['id'])
        self.assertEqual((owner['status'], owner['_native_outcome_id']), ('completed', original), self.dispatch_state(owner))
        self.assertEqual(owner['_bot_messages'][-1]['_return_source_task_id'], asked['task_id'])
        card = self.service.kanban.tasks[owner['id']]
        self.assertEqual((card['id'], card['status'], card['input_id']), (original, 'scheduled', owner['_native_turn_id']))
        self.assertEqual(owner['_kanban_claim']['input'], owner['_native_turn_id'])
        # The idle outcome's worktree is discarded. Its branch and the discard archive keep the unfinished edit.
        self.head, self.unstaged = git(self.path, 'rev-parse', 'HEAD'), changes(self.path)[1]
        with patch('coconut.work_workspace.holders', return_value=set()):
            self.assertTrue(discard(self.registry, self.identity, lambda wait: True, merged=True))
        self.assertFalse(self.path.exists())
        self.discarded = json.loads(self.manifest.read_text())[self.identity]
        self.assertEqual(archived_patch(self.discarded), (self.unstaged, b''))
        self.archive = Path(self.discarded['retired']['archive'])
        self.archive_bytes = self.archive.read_bytes()
        # A day later the settle pass archives the scheduled card. The completed task keeps its status.
        self.update(owner['id'], updated_at=stamp(SETTLE_SECONDS + 1))
        self.service._settle_native_orphans()
        self.service.kanban.call({'op': 'snapshot'})
        self.tick()
        native = self.card_state(original)
        self.assertEqual((native['status'], native['key'], native['path'], native['parents']),
                         ('archived', 'coconut:' + owner['id'], str(self.path), []))
        self.assertEqual([r[2] for r in native['runs']], ['scheduled', 'scheduled'])
        owner = self.service._get('tasks', owner['id'])
        self.assertEqual(owner['status'], 'completed')
        self.assertEqual(self.service._continuation_turns(owner), set())
        requester = self.update(requester['id'], status='working')
        self.continued = True
        return owner, requester, original

    def accepted_input(self, identity):
        return self.native_program('row = board.conn.execute("SELECT input_id FROM coconut_outcomes WHERE native_id=?", '
                                   '(sys.argv[2],)).fetchone()\nprint(row[0] if row else "")', identity)

    def test_continuation_restores_the_worktree_and_returns_to_the_requester_once(self):
        owner, requester, original = self.scheduled_original()
        history, accepted = self.card_state(original), self.accepted_input(original)
        self.assertEqual(accepted, owner['_native_turn_id'])
        tasks = {t['id'] for t in self.service._all('tasks') if t['bot_id'] == self.reviews['id']}
        before = len([d for d in self.results(owner) if d['target_bot_id'] == self.desk['id']])
        receipt = self.service.emit(requester['id'], 'work_continue',
                                    dict(task_id=original, message='Finish the legacy rental copy edit.'))
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=original, status='queued'))
        continued = self.service._get('tasks', owner['id'])
        task = self.owner_turns(owner, 2)
        successor = task['_native_outcome_id']
        self.assertNotEqual(successor, original)
        native = self.card_state(successor)
        self.assertEqual(native['parents'], [original])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'])
        self.assertEqual((native['tenant'], native['path']), (owner['bot_id'], str(self.path)))
        self.assertEqual([p for p in self.provider.prepared if p[0] == owner['id']][-1][1:3], (str(self.path), owner['id']))
        # The successor turn carries the continued input, and the original card keeps its history and accepted input.
        self.assertEqual((task['_native_turn_id'], task['_kanban_claim']['input'], task['_kanban_workspace']),
                         (continued['_native_turn_id'], continued['_native_turn_id'], str(self.path)))
        self.assertEqual((self.card_state(original), self.accepted_input(original)), (history, accepted))
        # Before the provider ran, the worktree was the registry's retired path. The provider restored the exact edit.
        self.assertIs(self.provider_was[0], False, 'Admission or dispatch restored the worktree before the owner turn.')
        self.assertTrue(self.provider_was[1] and self.provider_was[1]['done'])
        events, rows = self.restored[0]
        self.assertIn(('completed', {'summary': 'Complete.'}), events)
        self.assertEqual([row[1]['cwd'] for row in rows if row[0] == 'session'], [str(self.path)])
        self.assertEqual((git(self.path, 'branch', '--show-current'), git(self.path, 'rev-parse', 'HEAD')), (BRANCH, self.head))
        self.assertEqual(changes(self.path), (b'', self.unstaged, ['notes.txt']))
        self.assertEqual(changes(self.path)[1], archived_patch(self.discarded)[0])
        self.assertEqual((self.path / 'copy.md').read_text(), EDITED)
        self.assertNotIn('retired', json.loads(self.manifest.read_text())[self.identity])
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        # The result reaches the original requester once, as a return callback of that request.
        self.completed(owner)
        callbacks = self.returned(owner, before + 1)
        results = [d for d in self.results(owner) if d['target_bot_id'] == self.desk['id']][before:]
        self.assertEqual(len(results), 1, self.dispatch_state(owner))
        callback = self.service._get('tasks', results[0]['receipt']['task_id'])
        self.assertIn(callback['id'], {t['id'] for t in callbacks})
        self.assertEqual((callback['_assignment_task_id'], callback['source_task_id']), (requester['id'], owner['id']))
        self.assertEqual(self.service._get('tasks', owner['id'])['_reply_to_task_id'], requester['id'])
        # No other task or card took the execution, and a replay dispatches nothing more.
        self.assertEqual({t['id'] for t in self.service._all('tasks') if t['bot_id'] == self.reviews['id']}, tasks)
        keys = json.loads(self.native_program('import json\nprint(json.dumps(sorted(r[0] for r in board.conn.execute('
                                              '"SELECT idempotency_key FROM tasks WHERE tenant=?", (sys.argv[2],)))))',
                                              owner['bot_id']))
        self.assertEqual(keys, sorted(['coconut:' + owner['id'], native['key']]))
        self.assertEqual(self.service.emit(requester['id'], 'work_continue',
                                           dict(task_id=original, message='Finish the legacy rental copy edit.')), receipt)
        self.tick()
        self.assertEqual(len([t for t in self.provider.tasks if t['id'] == owner['id']]), 2)
        self.assertFalse(any(json.loads(row[0]).get('status') == 'cancelled' for row in self.service.db.execute(
            "SELECT data FROM events WHERE task_id=? AND type='task.updated'", (owner['id'],))))

    def test_cancelled_request_stays_archived(self):
        owner, requester, original = self.scheduled_original()
        with self.service.transaction():
            self.service._cancel_one(self.service._get('tasks', owner['id']), [], include_finished=True)
        owner = self.service._get('tasks', owner['id'])
        with self.assertRaises(ServiceError):
            self.service.emit(requester['id'], 'work_continue', dict(task_id=original, message='Continue anyway.'))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertFalse(self.path.exists())

    def test_card_without_its_schedule_receipt_stays_archived(self):
        owner, requester, original = self.scheduled_original()
        self.service.db.execute("DELETE FROM native_receipts WHERE json_extract(data,'$.task_id')=? "
                                "AND json_extract(data,'$.action.name')='kanban_schedule'", (owner['id'],))
        owner = self.service._get('tasks', owner['id'])
        with self.assertRaisesRegex(ServiceError, 'not blocked or completed'):
            self.service.emit(requester['id'], 'work_continue', dict(task_id=original, message='Continue anyway.'))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.tick()
        self.assertEqual(len([t for t in self.provider.tasks if t['id'] == owner['id']]), 1)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
