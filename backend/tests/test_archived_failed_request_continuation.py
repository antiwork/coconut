"""A failed ordinary request continues after Coconut's own settle pass archives its native task.

A requester delegates work to another bot through the native relay. The owner's turn fails before its worker starts,
Hermes moves the task to triage, and the requester can still continue it there. A few minutes later the settle pass
archives the task of the failed request. The request has no routine record, so the routine rule cannot reach it.

Only that exact archive continues: the task that Coconut created for this request, ended by the run of the claim of
the current input without success, with no run, worker, unknown tool call, decision, or other open input left.
The continuation keeps the request, its execution, its claim history, and its result route. Hermes keeps the archived
task, and Board.sync gives the new input a successor in the same workspace. A discarded worktree comes back only when
the owner's own turn starts. Native CI sets COCONUT_REQUIRE_NATIVE, and then the native tests fail instead of skipping.
"""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut import routine_coordinator
from coconut.kanban_runtime import IDLE_AGE
from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
from coconut.work_workspace import discard, git, head_log, preferred
import legacy_work_sync
import test_archived_failed_routine_continuation as failed_routine_fixtures
import test_archived_scheduled_routine_continuation as routine_fixtures
import test_finished_routine_workspace as workspace_fixtures
import test_native_kanban as native_fixtures
import test_workspace_reflog_rewrite as reflog_fixtures
import hermes_env

setUpModule, tearDownModule = legacy_work_sync.scope()

# The saved shape of the retained request: its relay envelope, its input, its claim, and its native runs.
ENVELOPE = '8c66535b42d54ec4b3e02545c46dfa86'
TURN = 'c9b722d6-87c8-4e87-bb88-7dfc413e9a7f'
NATIVE = 't_ee1d9c4e'
RUN = 28877
FAILURE = 'Work stopped: The saved worktree changed branches. Inspect it before continuing.'
RUNS = [dict(id=28715, status='scheduled', outcome='scheduled', ended=True),
        dict(id=28866, status='scheduled', outcome='scheduled', ended=True),
        dict(id=28873, status='blocked', outcome='blocked', ended=True),
        dict(id=RUN, status='blocked', outcome='blocked', ended=True)]
BILLING = 'The model provider reported a billing limit.'
PR_REVIEW = dict(url='https://github.com/antiwork/gumroad/pull/8525', pr_state='merged',
                 head_sha='7ac18975d0ddcbe95b42cb3637d81486a0739d6e', checked_at='2026-10-19T17:53:24.122308Z',
                 draft=False, checks_state=None, required_checks_state=None, review_checks=None, check_failures=None)


class ArchivedRequest:
    """A real Service whose relayed request failed before its worker started, and whose task the settle pass archived."""

    adapter = routine_fixtures.ScheduledOccurrence.adapter
    update = routine_fixtures.ScheduledOccurrence.update

    def setUp(self):
        self.store_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.store_tmp.cleanup)
        self.store = Service(self.store_tmp.name, FixtureProvider(self.store_tmp.name), start_workers=False)
        self.addCleanup(self.store.close)
        self.desk = self.store.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'test-phone')
        self.dev = self.store.mutate('POST', '/v1/bots', {'name': 'Dev'}, str(uuid4()), 'test-phone')
        self.runs, self.calls = {}, []
        self.store.kanban = Mock(tasks={})
        self.store.kanban.call.side_effect = self.adapter

    def working(self, bot, text):
        task = self.store.mutate('POST', '/v1/bots/' + bot['id'] + '/messages', {'text': text}, str(uuid4()), 'test-phone')
        return self.update(task['id'], status='working')

    def archived(self, failure=None):
        """The saved shape: Desk relayed the request, its input failed in preparation, and the settle pass archived it."""
        requester = self.working(self.desk, 'Ask Dev for the admin-only download grant.')
        delivered = self.store._store_bot_message(requester['id'], dict(
            target_bot_id=self.dev['id'], text='Implement the admin-only download grant.', native_envelope_id=ENVELOPE))
        owner = self.store._get('tasks', delivered['task_id'])
        self.assertEqual(owner['_native_relay'], dict(sender_bot_id=self.desk['id'], envelope_id=ENVELOPE))
        self.assertEqual((owner['source_bot_id'], owner['_reply_to_task_id']), (self.desk['id'], requester['id']))
        self.workspace = self.store.root / 'workspaces' / owner['id']
        self.workspace.mkdir(parents=True)
        owner = self.update(owner['id'], _native_intake=True, status='working', _execution_session_id=owner['id'],
                            _native_turn_id=TURN, _conversation_epoch=self.store._setting('conversation_epoch'),
                            _kanban_claim=dict(native_id=NATIVE, external_id=owner['id'], input=TURN, run_id=RUN,
                                               claim_lock='gumclaw:17363', workspace=str(self.workspace)),
                            _kanban_started=RUN, _pr_review=dict(PR_REVIEW))
        self.store.emit(owner['id'], 'failed', failure or dict(summary=FAILURE))
        self.store._drain_returns()
        owner = self.store._get('tasks', owner['id'])
        self.assertEqual(owner['status'], 'failed')
        for field in ('_native_outcome_id', '_native_job', '_return_pending', '_native_settled_failure'):
            self.assertNotIn(field, owner)
        self.store.kanban.tasks[owner['id']] = dict(
            external_id=owner['id'], id=NATIVE, status='archived', tenant=self.dev['id'],
            idempotency_key='coconut:' + owner['id'], created_by='coconut', current_run_id=None, worker_pid=None,
            claim_lock=None, input_id=None, workspace_path=str(self.workspace), last_activity_at=time.time() - 900,
            dependencies=[], dispatch_blocker=None, block_kind=None)
        self.runs[NATIVE] = copy.deepcopy(RUNS)
        self.requester = self.update(requester['id'], status='working')
        return owner

    def resume(self, caller=None, message='The worktree guard is fixed. Continue the admin-only grant.', identity=NATIVE):
        self.assertFalse(self.store.db.in_transaction, 'A continuation must not start inside another transaction.')
        return self.store.emit((caller or self.requester)['id'], 'work_continue', dict(task_id=identity, message=message))

    def assertRejected(self, owner, pattern='.', caller=None):
        before = self.store._get('tasks', owner['id'])
        with self.assertRaisesRegex(ServiceError, pattern):
            self.resume(caller)
        self.assertEqual(self.store._get('tasks', owner['id']), before)

    def cancellations(self, owner):
        return [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT data FROM events WHERE task_id=? AND type='task.updated' "
            "AND json_extract(data,'$.status')='cancelled'", (owner['id'],))]

    def dispatch_successor(self, owner, identity, run):
        """Board.sync gives the queued input a successor, the reconciler binds it, and Hermes claims it."""
        turn = self.store._get('tasks', owner['id'])['_native_turn_id']
        card = dict(self.store.kanban.tasks[owner['id']], id=identity, status='ready', input_id=turn, current_run_id=None,
                    worker_pid=None, claim_lock=None, idempotency_key='coconut-followup:' + owner['id'] + ':' + turn,
                    last_activity_at=time.time())
        self.store.kanban.tasks[owner['id']] = card
        self.store._reconcile_kanban()
        queued = self.store._get('tasks', owner['id'])
        self.assertEqual((queued['status'], queued['_native_outcome_id'], queued['_native_turn_id']), ('queued', identity, turn))
        card.update(status='running', current_run_id=run, worker_pid=4000 + run % 1000, claim_lock='host:' + str(run))
        self.runs[identity] = [dict(id=run, status='running', outcome=None, ended=False)]
        return self.update(owner['id'], status='working', _kanban_started=run, _kanban_claim=dict(
            native_id=identity, external_id=owner['id'], input=turn, run_id=run, claim_lock='host:' + str(run),
            workspace=str(self.workspace)))

    def settle_failure(self, owner, identity, change=None):
        """The turn fails and its result returns. Coconut ends the run as blocked, and the settle pass archives the card."""
        self.store.emit(owner['id'], 'failed', dict(summary='The grant tests failed.'))
        self.store._drain_returns()
        self.runs[identity][-1].update(status='blocked', outcome='blocked', ended=True)
        card = self.store.kanban.tasks[owner['id']]
        card.update(status='archived', current_run_id=None, worker_pid=None, claim_lock=None, last_activity_at=time.time() - 60)
        if change:
            task = self.store._get('tasks', owner['id'])
            change(task, card)
            self.store._put('tasks', task)
        self.store._reconcile_kanban()
        return self.store._get('tasks', owner['id'])


class ArchivedFailedRequestAdmissionTests(ArchivedRequest, unittest.TestCase):
    def test_saved_shape_continues_through_its_original_requester(self):
        owner = self.archived()
        before = copy.deepcopy(owner)
        receipt = self.resume()
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=NATIVE, status='queued'))
        continued = self.store._get('tasks', owner['id'])
        self.assertEqual((continued['status'], continued['_native_outcome_id']), ('queued', NATIVE))
        self.assertNotEqual(continued['_native_turn_id'], TURN)
        for key in ('id', 'bot_id', 'session_id', 'thread_id', 'exchange_id', 'conversation_bot_id', '_execution_session_id',
                    '_kanban_claim', '_kanban_started', 'source_bot_id', 'source_task_id', 'parent_task_id',
                    '_reply_to_task_id', '_native_relay', '_native_intake'):
            self.assertEqual(continued.get(key), before.get(key), key)
        # The failed turn never received the PR facts. The retry does.
        self.assertEqual(continued['_pr_review'], PR_REVIEW)
        self.assertEqual(continued['_self_recovery']['previous_task_id'], owner['id'])
        self.assertEqual(continued['_self_recovery']['unknown_tools'], [])
        self.assertEqual(continued['_bot_messages'][-1]['sender_bot_id'], self.desk['id'])
        self.assertIn({'op': 'completion', 'id': NATIVE}, self.calls)
        self.assertEqual(self.cancellations(owner), [])
        # The same delivery is idempotent.
        self.assertEqual(self.resume(), receipt)
        self.assertEqual(self.store._get('tasks', owner['id']), continued)

    def test_the_new_input_gets_a_successor_and_reconciliation_keeps_it(self):
        owner = self.archived()
        self.resume()
        continued = self.store._get('tasks', owner['id'])
        row = self.store._kanban_row(continued, continued, 'ready', 10)
        # Board.sync records the failed input as accepted, then makes a successor for the new one in the same workspace.
        self.assertEqual((row['native_id'], row['previous_input'], row['input'], row['workspace']),
                         (NATIVE, TURN, continued['_native_turn_id'], str(self.workspace)))
        # Before Board.sync runs, reconciliation sees the archived task, which never accepted the new input.
        self.store._reconcile_kanban()
        self.assertEqual(self.store._get('tasks', owner['id']), continued)
        card = self.store.kanban.tasks[owner['id']]
        self.store.kanban.tasks[owner['id']] = dict(card, id='t_successor', status='ready', input_id=continued['_native_turn_id'],
            idempotency_key='coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'], last_activity_at=time.time())
        self.store._reconcile_kanban()
        queued = self.store._get('tasks', owner['id'])
        self.assertEqual((queued['status'], queued['_native_outcome_id'], queued['_native_turn_id']),
                         ('queued', 't_successor', continued['_native_turn_id']))
        self.assertEqual(queued['_reply_to_task_id'], self.requester['id'])
        self.assertEqual(self.cancellations(owner), [])

    def test_only_the_owner_or_its_requester_can_continue(self):
        owner = self.archived()
        other = self.store.mutate('POST', '/v1/bots', {'name': 'Other'}, str(uuid4()), 'test-phone')
        self.assertRejected(owner, 'Only the owner or its requesting bot', caller=self.working(other, 'Continue it.'))

    def test_stop_and_cancellation_stay_final(self):
        for name in ('cancelled', 'stopped_before'):
            with self.subTest(name):
                ArchivedRequest.setUp(self)
                owner = self.archived()
                if name == 'cancelled':
                    owner = self.update(owner['id'], status='cancelled')
                else:
                    self.store._event('task.updated', dict(self.store._public(owner), status='cancelled'),
                                      owner['bot_id'], owner['id'])
                self.assertRejected(owner)

    def test_idle_archive_by_the_person_stays_final(self):
        owner = self.archived()
        card = self.store.kanban.tasks[owner['id']]
        card['last_activity_at'] = time.time() - IDLE_AGE - 60
        self.assertRejected(owner)
        card['last_activity_at'] = time.time() - 900
        self.store._put('decisions', dict(id='archived', task_id=owner['id'], bot_id=owner['bot_id'],
                                          status='rejected', decided_by='archive', outcome='Task archived.'))
        self.assertRejected(owner)

    def test_unknown_effects_decisions_workers_and_other_input_hold(self):
        owner = self.archived()
        self.store._put('decisions', dict(id='pending', task_id=owner['id'], bot_id=owner['bot_id'], status='pending'))
        self.assertRejected(owner, 'decision must be answered')
        self.store.db.execute("DELETE FROM decisions WHERE id='pending'")
        self.store._put('native_receipts', dict(id='unknown', task_id=owner['id'], turn_id=TURN, state='executing',
            action_hash='hash', action={'name': 'write_file', 'arguments': {}}, created_at='2026-10-19T19:09:40Z'))
        self.assertRejected(owner)
        self.store.db.execute("DELETE FROM native_receipts WHERE id='unknown'")
        self.store.running[owner['id']] = Mock(is_alive=Mock(return_value=True))
        self.assertRejected(owner)
        self.store.running.pop(owner['id'])
        self.assertEqual(self.resume()['status'], 'queued')
        for changes in (dict(_self_recovery=dict(unknown_tools=[dict(id='earlier-write')])),
                        dict(_steering=['Use the other checkout.']), dict(_live_messages=['Wait.']),
                        dict(_blocked_followup=dict(message_ids=['m'], source_ids=['m'], attachment_ids=[])),
                        dict(_return_pending=dict(text='Saved result')), dict(_waiting_on=['dependency']),
                        dict(_resume_decision='decision'), dict(_work_resume=dict(reason='resume'))):
            with self.subTest(changes=changes):
                ArchivedRequest.setUp(self)  # A fresh store, so one admission cannot answer the next case.
                owner = self.archived()
                self.assertRejected(self.update(owner['id'], **changes))

    def test_the_archive_must_be_this_claim_input_and_failed_run(self):
        cases = dict(
            other_input=lambda owner, card, runs: owner['_kanban_claim'].update(input='another-input'),
            not_started=lambda owner, card, runs: owner.update(_kanban_started=RUN - 4),
            run_not_int=lambda owner, card, runs: owner['_kanban_claim'].update(run_id=str(RUN)),
            foreign_tenant=lambda owner, card, runs: card.update(tenant=self.desk['id']),
            other_key=lambda owner, card, runs: card.update(idempotency_key='coconut-followup:' + owner['id'] + ':other'),
            not_coconut=lambda owner, card, runs: card.update(created_by='someone'),
            live_run=lambda owner, card, runs: card.update(current_run_id=RUN + 1),
            accepted_other_input=lambda owner, card, runs: card.update(input_id='another-input'),
            completed_run=lambda owner, card, runs: runs[1].update(outcome='completed'),
            last_run_scheduled=lambda owner, card, runs: runs[-1].update(outcome='scheduled'),
            last_run_open=lambda owner, card, runs: runs[-1].update(ended=False),
            last_run_other=lambda owner, card, runs: runs.append(dict(id=RUN + 1, status='blocked', outcome='blocked', ended=True)),
            no_runs=lambda owner, card, runs: runs.clear(),
            completed_request=lambda owner, card, runs: owner.update(status='completed'),
            created_twice=lambda owner, card, runs: self.store._event('task.created', self.store._public(owner),
                                                                       owner['bot_id'], owner['id']),
            routine=lambda owner, card, runs: owner.update(routine_id=str(uuid4())),
            routine_job=lambda owner, card, runs: owner.update(_native_job=dict(id='job')))
        for name, change in cases.items():
            with self.subTest(name):
                ArchivedRequest.setUp(self)
                owner = self.archived()
                card = self.store.kanban.tasks[owner['id']]
                change(owner, card, self.runs[NATIVE])
                self.store._put('tasks', owner)
                owner = self.store._get('tasks', owner['id'])
                self.assertFalse(self.store.settled_failed_request(owner, card, lambda: self.adapter(
                    {'op': 'completion', 'id': NATIVE})['card']))
                self.assertRejected(owner)

    def test_adapter_without_an_answer_admits_nothing(self):
        owner = self.archived()
        def silent(request):
            if request['op'] == 'completion':
                raise RuntimeError('The Hermes Kanban adapter stopped.')
            return self.adapter(request)
        self.store.kanban.call.side_effect = silent
        self.assertRejected(owner)
        self.store.kanban.call.side_effect = lambda request: {'card': None} if request['op'] == 'completion' else self.adapter(request)
        self.assertRejected(owner)

    def test_the_request_events_statement_uses_the_task_index(self):
        plan = ' '.join(row[-1] for row in self.store.db.execute('EXPLAIN QUERY PLAN ' + routine_coordinator.REQUEST_EVENTS, ('x',)))
        self.assertIn('events_task', plan)
        self.assertNotIn('SCAN events', plan)


class ArchivedFailedRequestRepeatTests(ArchivedRequest, unittest.TestCase):
    """A continuation that fails again keeps its failure when the settle pass archives its successor."""

    def test_a_second_failure_stays_failed_and_continues_again(self):
        owner = self.archived()
        self.resume()
        first = self.dispatch_successor(owner, 't_successor', RUN + 2)
        failed = self.settle_failure(owner, 't_successor')
        self.assertEqual(failed['status'], 'failed', 'Reconciliation cancelled a failure that no person stopped.')
        self.assertEqual(self.cancellations(owner), [])
        self.assertEqual((failed['_native_outcome_id'], failed['_native_settled_failure']), ('t_successor', 't_successor'))
        self.assertEqual(self.store.kanban.tasks[owner['id']]['input_id'], first['_native_turn_id'])
        for key in ('_execution_session_id', 'session_id', 'exchange_id', 'source_bot_id', 'source_task_id',
                    'parent_task_id', '_reply_to_task_id', '_native_relay'):
            self.assertEqual(failed.get(key), owner.get(key), key)
        self.assertEqual(failed['_pr_review'], PR_REVIEW)
        # Later passes keep it, also after the seven days in which it can continue.
        card = self.store.kanban.tasks[owner['id']]
        card['last_activity_at'] = time.time() - IDLE_AGE - 60
        self.store._reconcile_kanban()
        self.assertEqual(self.store._get('tasks', owner['id']), failed)
        card['last_activity_at'] = time.time() - 60
        # The requester continues the same execution again, through the archived successor.
        self.requester = self.update(self.requester['id'], status='working')
        receipt = self.resume(identity='t_successor', message='Run the grant tests again.')
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_successor', status='queued'))
        again = self.store._get('tasks', owner['id'])
        self.assertNotEqual(again['_native_turn_id'], first['_native_turn_id'])
        self.assertEqual((again['_pr_review'], again['_reply_to_task_id']), (PR_REVIEW, self.requester['id']))
        self.assertIn({'op': 'completion', 'id': 't_successor'}, self.calls)
        # The archived successor never accepted the new input, so the next rebind keeps the request queued.
        self.store._reconcile_kanban()
        self.assertEqual(self.store._get('tasks', owner['id']), again)
        second = self.dispatch_successor(owner, 't_successor_2', RUN + 3)
        self.assertEqual(second['_kanban_claim']['input'], again['_native_turn_id'])
        self.assertEqual(self.cancellations(owner), [])

    def test_reconciliation_still_cancels_other_archived_failures(self):
        cases = dict(
            stopped=lambda task, card: self.store._event('task.updated', dict(self.store._public(task), status='cancelled'),
                                                         task['bot_id'], task['id']),
            idle=lambda task, card: card.update(last_activity_at=time.time() - IDLE_AGE - 60),
            unknown_tool=lambda task, card: self.store._put('native_receipts', dict(id='unknown-' + task['id'],
                task_id=task['id'], turn_id=task['_native_turn_id'], state='executing', action_hash='hash',
                action={'name': 'write_file', 'arguments': {}}, created_at='2026-10-19T20:00:00Z')),
            recorded_unknown=lambda task, card: task.update(_self_recovery=dict(task['_self_recovery'],
                                                                                unknown_tools=[dict(id='earlier-write')])),
            foreign=lambda task, card: card.update(tenant=self.desk['id']),
            other_key=lambda task, card: card.update(idempotency_key='coconut-followup:' + task['id'] + ':another-input'),
            not_started=lambda task, card: task.update(_kanban_started=RUN),
            routine=lambda task, card: task.update(routine_id=str(uuid4())),
            open_input=lambda task, card: task.update(_steering=['Use the other checkout.']))
        for name, change in cases.items():
            with self.subTest(name):
                ArchivedRequest.setUp(self)  # A fresh store for each case.
                owner = self.archived()
                self.resume()
                self.dispatch_successor(owner, 't_successor', RUN + 2)
                cancelled = self.settle_failure(owner, 't_successor', change)
                self.assertEqual(cancelled['status'], 'cancelled')
                self.assertNotIn('_native_settled_failure', cancelled)
                # The reconciler adds one cancellation. The stopped case recorded the person's Stop before it.
                self.assertEqual(len(self.cancellations(owner)), 2 if name == 'stopped' else 1)

    def test_a_pending_decision_keeps_the_failure_and_holds_the_continuation(self):
        owner = self.archived()
        self.resume()
        self.dispatch_successor(owner, 't_successor', RUN + 2)
        decision = dict(id='pending', task_id=owner['id'], bot_id=owner['bot_id'], status='pending')
        failed = self.settle_failure(owner, 't_successor', lambda task, card: self.store._put('decisions', decision))
        self.assertEqual(failed['status'], 'failed')
        self.requester = self.update(self.requester['id'], status='working')
        with self.assertRaisesRegex(ServiceError, 'decision must be answered'):
            self.resume(identity='t_successor', message='Run the grant tests again.')
        self.assertEqual(self.store._get('tasks', owner['id']), failed)
        self.assertEqual(self.store._get('decisions', 'pending')['status'], 'pending')


class ProviderStoppedSuccessorTests(ArchivedRequest, unittest.TestCase):
    """The request has no retained native outcome, so Board.sync gave its new input the successor coconut:<id>:<input>.

    The model provider stopped that input's turn. Hermes retries such a stop for a card that Coconut owns, but this card
    is not owned, so the stop blocked it, and the settle pass archived it. This is Dev's PR 8606 execution. Only that
    stop continues the original owner, with its task, input, claim, run, creation, and result route. Its legacy result
    pointer names a finished helper of the same execution, and the continuation leaves that helper alone.
    """

    def provider_stopped(self, error_code='native_billing'):
        failure = dict(summary=BILLING)
        if error_code:
            failure['error_code'] = error_code
        owner = self.archived(failure)
        if error_code == 'native_billing':
            # The worker's own failure report saved the provider's reason.
            self.assertEqual((owner['error_code'], owner['summary']), (error_code, BILLING))
        elif error_code:
            # A reason that only the service records, such as a dispatch failure.
            owner = self.update(owner['id'], error_code=error_code)
        self.store.kanban.tasks[owner['id']]['idempotency_key'] = 'coconut:' + owner['id'] + ':' + TURN
        self.runs[NATIVE] = [dict(id=RUN, status='blocked', outcome='blocked', ended=True)]
        # A finished helper of the same execution, which another bot started for the owner's assignment.
        helper = dict(owner, id=str(uuid4()), status='completed', summary='Complete.', _assignment_task_id=owner['id'],
                      _native_turn_id='helper-turn', _kanban_started=RUN - 100, _kanban_claim=dict(
                          native_id='t_helper', external_id=None, input='helper-turn', run_id=RUN - 100))
        helper['_kanban_claim']['external_id'] = helper['id']
        for key in ('source_task_id', 'source_bot_id', 'parent_task_id', '_reply_to_task_id', '_native_relay', 'error_code'):
            helper.pop(key, None)
        self.store._put('tasks', helper)
        return self.update(owner['id'], _result_task_id=helper['id']), helper

    def completion(self):
        return lambda: self.adapter({'op': 'completion', 'id': NATIVE})['card']

    def test_the_provider_stopped_successor_continues_its_original_owner(self):
        owner, helper = self.provider_stopped()
        before = copy.deepcopy(owner)
        self.assertTrue(self.store.settled_failed_request(owner, self.store.kanban.tasks[owner['id']], self.completion()))
        receipt = self.resume()
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=NATIVE, status='queued'))
        continued = self.store._get('tasks', owner['id'])
        self.assertEqual((continued['status'], continued['_native_outcome_id']), ('queued', NATIVE))
        self.assertNotEqual(continued['_native_turn_id'], TURN)
        for key in ('id', 'bot_id', 'session_id', 'thread_id', 'exchange_id', 'conversation_bot_id', '_execution_session_id',
                    '_kanban_claim', '_kanban_started', 'source_bot_id', 'source_task_id', 'parent_task_id',
                    '_reply_to_task_id', '_native_relay', '_native_intake', 'created_at'):
            self.assertEqual(continued.get(key), before.get(key), key)
        self.assertEqual(continued['_pr_review'], PR_REVIEW)
        self.assertEqual(continued['_self_recovery']['previous_task_id'], owner['id'])
        self.assertEqual(continued['_self_recovery']['unknown_tools'], [])
        self.assertEqual(continued['_bot_messages'][-1]['sender_bot_id'], self.desk['id'])
        self.assertIn({'op': 'completion', 'id': NATIVE}, self.calls)
        self.assertEqual(self.cancellations(owner), [])
        self.assertEqual(self.store._get('tasks', helper['id']), helper)
        self.assertEqual(self.resume(), receipt)
        self.assertEqual(self.store._get('tasks', owner['id']), continued)
        self.assertEqual(len([d for d in self.store._all('deliveries') if d.get('receipt') == receipt]), 1)

    def test_its_result_returns_once_to_the_original_requester(self):
        owner, helper = self.provider_stopped()
        self.resume()

        def returns():
            deliveries = [d for d in self.store._all('deliveries') if d['source_task_id'] == owner['id']]
            callbacks = [t for t in self.store._all('tasks') if t.get('_is_return') and t.get('source_task_id') == owner['id']]
            return deliveries, callbacks
        earlier = [{item['id'] for item in found} for found in returns()]
        running = self.dispatch_successor(owner, 't_successor', RUN + 2)
        self.assertEqual(self.store.kanban.tasks[owner['id']]['idempotency_key'],
                         'coconut-followup:' + owner['id'] + ':' + running['_native_turn_id'])
        # The owner's own turn completes, and Hermes completes the successor card of its input.
        self.store.emit(owner['id'], 'completed', dict(summary='The grant passed its tests.'))
        self.runs['t_successor'][-1].update(status='done', outcome='completed', ended=True)
        self.store.kanban.tasks[owner['id']].update(status='done', current_run_id=None, worker_pid=None, claim_lock=None,
                                                    outcome_summary='The grant passed its tests.')
        for _ in range(2):
            self.store._reconcile_kanban()
            self.store._drain_returns()
        deliveries, callbacks = [[item for item in found if item['id'] not in seen] for found, seen in zip(returns(), earlier)]
        self.assertEqual(len(deliveries), 1, deliveries)
        self.assertEqual((deliveries[0]['target_bot_id'], deliveries[0]['text']), (self.desk['id'], 'The grant passed its tests.'))
        self.assertEqual([(t['bot_id'], t['_is_return']) for t in callbacks], [(self.desk['id'], True)])
        finished = self.store._get('tasks', owner['id'])
        self.assertEqual((finished['status'], finished['_reply_to_task_id']), ('completed', self.requester['id']))
        self.assertFalse(finished.get('_return_pending'))
        # The legacy result pointer still names the helper, and the helper receives nothing.
        self.assertEqual(finished['_result_task_id'], helper['id'])
        self.assertEqual(self.store._get('tasks', helper['id']), helper)
        self.assertFalse([d for d in self.store._all('deliveries') if helper['id'] in (d['source_task_id'],
                                                                                      (d.get('receipt') or {}).get('task_id'))])

    def test_other_failures_of_that_successor_stay_final(self):
        for error_code in (None, 'native_turn_failed', 'native_auth_permanent', 'native_context_overflow',
                           'native_interrupted', 'native_dispatch_failed'):
            with self.subTest(error_code=error_code):
                ArchivedRequest.setUp(self)
                owner, helper = self.provider_stopped(error_code)
                self.assertFalse(self.store.settled_failed_request(owner, self.store.kanban.tasks[owner['id']], self.completion()))
                self.assertRejected(owner, 'not blocked or completed')

    def test_the_successor_must_be_this_input_claim_and_blocked_run(self):
        cases = dict(
            stale_predecessor=lambda owner, card, runs: card.update(idempotency_key='coconut:' + owner['id'] + ':earlier-input'),
            other_request=lambda owner, card, runs: card.update(idempotency_key='coconut:' + str(uuid4()) + ':' + TURN),
            other_claim_input=lambda owner, card, runs: owner['_kanban_claim'].update(input='another-input'),
            other_claimed_card=lambda owner, card, runs: owner['_kanban_claim'].update(native_id='t_other'),
            retained_outcome=lambda owner, card, runs: owner.update(_native_outcome_id='t_other'),
            not_started=lambda owner, card, runs: owner.update(_kanban_started=RUN - 4),
            crashed_run=lambda owner, card, runs: runs[-1].update(status='crashed', outcome='crashed'),
            timed_out_run=lambda owner, card, runs: runs[-1].update(status='timed_out', outcome='timed_out'),
            open_run=lambda owner, card, runs: runs[-1].update(ended=False),
            completed_run=lambda owner, card, runs: runs.insert(0, dict(id=RUN - 1, status='done', outcome='completed', ended=True)),
            later_run=lambda owner, card, runs: runs.append(dict(id=RUN + 1, status='blocked', outcome='blocked', ended=True)),
            live_run=lambda owner, card, runs: card.update(current_run_id=RUN + 1),
            live_worker=lambda owner, card, runs: card.update(worker_pid=4242),
            accepted_other_input=lambda owner, card, runs: card.update(input_id='another-input'),
            foreign_tenant=lambda owner, card, runs: card.update(tenant=self.desk['id']),
            not_coconut=lambda owner, card, runs: card.update(created_by='someone'),
            created_twice=lambda owner, card, runs: self.store._event('task.created', self.store._public(owner),
                                                                       owner['bot_id'], owner['id']),
            routine=lambda owner, card, runs: owner.update(routine_id=str(uuid4())))
        for name, change in cases.items():
            with self.subTest(name):
                ArchivedRequest.setUp(self)
                owner, helper = self.provider_stopped()
                card = self.store.kanban.tasks[owner['id']]
                change(owner, card, self.runs[NATIVE])
                self.store._put('tasks', owner)
                owner = self.store._get('tasks', owner['id'])
                self.assertFalse(self.store.settled_failed_request(owner, card, self.completion()))
                self.assertRejected(owner)

    def test_a_person_or_idle_archive_stays_final(self):
        def stop(owner, card):
            self.store._event('task.updated', dict(self.store._public(owner), status='cancelled'), owner['bot_id'], owner['id'])

        def archive_decision(owner, card):
            self.store._put('decisions', dict(id='archived', task_id=owner['id'], bot_id=owner['bot_id'],
                                              status='rejected', decided_by='archive', outcome='Task archived.'))
        cases = dict(
            cancelled=lambda owner, card: self.update(owner['id'], status='cancelled'),
            stopped_before=stop,
            idle_archive=lambda owner, card: card.update(last_activity_at=time.time() - IDLE_AGE - 60),
            archive_decision=archive_decision)
        for name, change in cases.items():
            with self.subTest(name):
                ArchivedRequest.setUp(self)
                owner, helper = self.provider_stopped()
                change(owner, self.store.kanban.tasks[owner['id']])
                self.assertRejected(self.store._get('tasks', owner['id']))

    def test_unknown_effects_decisions_workers_and_other_input_hold(self):
        def pending_decision(owner):
            self.store._put('decisions', dict(id='pending', task_id=owner['id'], bot_id=owner['bot_id'], status='pending'))

        def unknown_tool(owner):
            self.store._put('native_receipts', dict(id='unknown', task_id=owner['id'], turn_id=TURN, state='executing',
                action_hash='hash', action={'name': 'write_file', 'arguments': {}}, created_at='2026-10-23T20:12:00Z'))

        def helper_unknown_tool(owner):
            helper = self.store._get('tasks', owner['_result_task_id'])
            self.store._put('native_receipts', dict(id='helper-unknown', task_id=helper['id'], turn_id='helper-turn',
                state='executing', action_hash='hash', action={'name': 'terminal', 'arguments': {}},
                created_at='2026-10-23T16:40:00Z'))

        def active_helper(owner):
            self.update(owner['_result_task_id'], status='working')

        def live_worker(owner):
            self.store.running[owner['id']] = Mock(is_alive=Mock(return_value=True))
        cases = dict(
            pending_decision=pending_decision, unknown_tool=unknown_tool, live_worker=live_worker,
            active_helper=active_helper,
            recorded_unknown=lambda owner: self.update(owner['id'], _self_recovery=dict(unknown_tools=[dict(id='earlier-write')])),
            steering=lambda owner: self.update(owner['id'], _steering=['Use the other checkout.']),
            return_pending=lambda owner: self.update(owner['id'], _return_pending=dict(text='Saved result')),
            waiting=lambda owner: self.update(owner['id'], _waiting_on=['dependency']),
            resume_decision=lambda owner: self.update(owner['id'], _resume_decision='decision'))
        for name, change in cases.items():
            with self.subTest(name):
                ArchivedRequest.setUp(self)
                owner, helper = self.provider_stopped()
                change(owner)
                self.assertRejected(self.store._get('tasks', owner['id']))
        # The helper's own unknown tool is part of the execution, so the owner's continuation records it.
        ArchivedRequest.setUp(self)
        owner, helper = self.provider_stopped()
        helper_unknown_tool(owner)
        self.resume()
        recorded = self.store._get('tasks', owner['id'])['_self_recovery']['unknown_tools']
        self.assertEqual([r['id'] for r in recorded], ['helper-unknown'])


class ArchivedFailedRequestWorkerTests(ArchivedRequest, unittest.TestCase):
    """The provider starts a real worker process for the continued owner. Its native modules are the shared stub.

    The service discarded the owner's worktree after its native task was archived, as it does for an idle outcome
    whose pull request merged. The branch and the archive keep the work, and only the owner's own turn restores them.
    """

    def setUp(self):
        workspace_fixtures.FinishedRoutineWorkspaceTests.setUp(self)
        ArchivedRequest.setUp(self)
        reflog_fixtures.use_test_git(self)
        self.registry = self.data / 'workspaces'
        self.owner = self.archived()
        # The Dev worktree of this execution: renamed branch, a commit, and an uncommitted file. Then the discard.
        reflog_fixtures.retain_worktree(self, self.root, self.registry, self.owner['id'])
        git(self.path, 'commit', '--allow-empty', '-m', 'Scoped change')
        git(self.path, 'branch', '-m', 'reviewed-name')
        (self.path / 'pending.txt').write_text('Unfinished change')
        self.head = git(self.path, 'rev-parse', 'HEAD')
        identity = self.owner['id'] + '-key'
        with patch('coconut.work_workspace.holders', return_value=set()):
            self.assertTrue(discard(self.registry, identity, lambda wait: True, merged=True))
        self.assertFalse(self.path.exists())
        self.discarded = json.loads(self.manifest.read_text())[identity]
        self.assertEqual((self.discarded['branch'], self.discarded['retired']['done']), ('reviewed-name', True))

    def turn(self, task):
        events = []
        prepared = task['_kanban_workspace']
        self.provider.prepare_kanban_worker(task['id'], self.bot, prepared, task['_execution_session_id'])
        with patch.object(self.provider, 'observe_bot', return_value=self.bot), patch.object(
                self.provider, 'resolve_task_model', return_value=dict(
                    model='gpt-6-astra', provider='openai-codex', base_url=self.native['base_url'])):
            self.provider._run_turn('start', task, self.bot, [], lambda kind, data: events.append((kind, data)), isolated=True)
        scopes = Path(str(self.capture) + '.cwd')
        rows = [json.loads(line) for line in scopes.read_text().splitlines()] if scopes.exists() else []
        scopes.unlink(missing_ok=True)
        return events, rows

    def continued_task(self):
        """The continued owner after Board.sync made its successor in the registry's path and Hermes claimed it."""
        self.resume()
        continued = self.store._get('tasks', self.owner['id'])
        # Board.sync reads the registry's path hint for the successor. Hermes creates that empty folder for its claim.
        workspace = preferred(self.registry, continued['_execution_session_id'], verify=False)
        self.assertEqual(workspace, self.path)
        workspace.mkdir(parents=True)
        claim = dict(continued['_kanban_claim'], native_id='t_successor', run_id=RUN + 1,
                     input=continued['_native_turn_id'], workspace=str(workspace))
        return dict(id=continued['id'], title=continued['title'], bot_id=self.bot['id'],
                    _execution_session_id=continued['_execution_session_id'], _native_turn_id=continued['_native_turn_id'],
                    _native_outcome_id='t_successor', _kanban_claim=claim, _kanban_external_id=continued['id'],
                    _kanban_workspace=str(workspace), _reply_to_task_id=continued['_reply_to_task_id'])

    def test_the_owner_turn_restores_its_discarded_worktree(self):
        task = self.continued_task()
        events, rows = self.turn(task)
        self.assertIn(('completed', {'summary': 'Complete.'}), events)
        self.assertEqual([row[1]['cwd'] for row in rows if row[0] == 'session'], [str(self.path)])
        self.assertEqual(git(self.path, 'branch', '--show-current'), 'reviewed-name')
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD'), self.head)
        self.assertEqual((self.path / 'pending.txt').read_text(), 'Unfinished change')
        restored = json.loads(self.manifest.read_text())[self.owner['id'] + '-key']
        self.assertNotIn('retired', restored)
        self.assertEqual({k: restored[k] for k in ('path', 'branch', 'repository', 'outcome')},
                         {k: self.discarded[k] for k in ('path', 'branch', 'repository', 'outcome')})
        log = head_log(self.path)
        self.assertEqual(restored['_head_log']['size'], len(log))

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


class ArchivedFailedRequestNativeTests(unittest.TestCase):
    """The installed Hermes kernel: the settle pass archives the failed request's task, continuation dispatches a successor."""

    write = native_fixtures.NativeKanbanTests.write
    native_program = native_fixtures.NativeKanbanTests.native_program
    tick = native_fixtures.NativeKanbanTests.tick
    native = failed_routine_fixtures.ArchivedFailedRoutineNativeTests.native

    def setUp(self):
        hermes_env.require(self)
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
        self.dev = self.write('/v1/bots', {'name': 'Dev', 'role': ''})
        self.provider.tasks = []
        start = self.provider.start_task

        self.fail_turns = False
        self.owner_id = None

        # Every started turn is recorded: the owner's, and the requester's return callbacks. Only the owner's turns fail.
        def record(task, bot, history, emit):
            self.provider.tasks.append(copy.deepcopy(task))
            if self.fail_turns and task['id'] == self.owner_id:
                emit('failed', dict(summary='The grant tests failed.'))
                return
            start(task, bot, history, emit)
        self.provider.start_task = record

    def update(self, identity, **fields):
        with self.service.transaction():
            task = self.service._get('tasks', identity)
            task.update(fields)
            self.service._put('tasks', task)
        return task

    def dispatch_state(self, owner):
        """The task identities and native state that explain a missing turn. No prompt or message text."""
        task = self.service._get('tasks', owner['id'])
        card = self.service.kanban.tasks.get(owner['id']) or {}
        return json.dumps(dict(
            started=[{k: t.get(k) for k in ('id', 'bot_id', 'source_task_id', '_execution_session_id', '_native_outcome_id',
                                            '_native_turn_id')} for t in self.provider.tasks],
            prepared=[list(p[:3]) for p in self.provider.prepared],
            owner=dict({k: task.get(k) for k in ('status', '_native_outcome_id', '_native_turn_id', '_kanban_started',
                                                '_waiting_on')}, return_pending=bool(task.get('_return_pending')),
                       claim={k: (task.get('_kanban_claim') or {}).get(k) for k in ('native_id', 'input', 'run_id')},
                       worker_alive=bool(self.service.running.get(owner['id']) and self.service.running[owner['id']].is_alive())),
            card={k: card.get(k) for k in ('id', 'status', 'input_id', 'current_run_id', 'worker_pid', 'idempotency_key',
                                           'dispatch_blocker')},
            open=[{k: t.get(k) for k in ('id', 'bot_id', 'status', 'source_task_id', '_is_return')}
                  for t in self.service._all('tasks') if t['status'] in ('queued', 'working', 'scheduled', 'waiting')]),
            default=str, sort_keys=True)

    def owner_turn(self, owner, count):
        """Tick a real dispatcher until the owner's own turn number `count` starts, and return it."""
        def turns():
            return [t for t in self.provider.tasks if t['id'] == owner['id']]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(turns()) < count:
            self.tick()
            time.sleep(.05)
        self.assertEqual(len(turns()), count, 'The owner turn did not start: ' + self.dispatch_state(owner))
        turn = turns()[-1]
        self.assertEqual((turn['bot_id'], turn['_execution_session_id']), (owner['bot_id'], owner['_execution_session_id']),
                         self.dispatch_state(owner))
        self.assertIn('_native_outcome_id', turn, self.dispatch_state(owner))
        return turn

    def returned(self, owner, requester, count):
        """Tick until at least `count` return callbacks of the owner reached the requester, and every one of them ended.

        A failed relayed turn returns its result to the requester as a callback turn of the requester's bot. It is not a
        turn of the owner, and it must end before the next continuation, as a running service does.
        """
        def callbacks():
            return [t for t in self.service._all('tasks') if t.get('_is_return') and t.get('source_task_id') == owner['id']
                    and t['bot_id'] == requester['bot_id']]
        def done():
            started = {t['id'] for t in self.provider.tasks}
            found = callbacks()
            return (len(found) >= count and all(t['id'] in started and t['status'] not in self.service.ACTIVE for t in found)
                    and not self.service._get('tasks', owner['id']).get('_return_pending'))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not done():
            self.tick()
            time.sleep(.05)
        self.assertTrue(done(), 'The failure return did not reach the requester: ' + self.dispatch_state(owner))
        for task in callbacks():
            worker = self.service.running.get(task['id'])
            if worker:
                worker.join(20)
                self.assertFalse(worker.is_alive(), 'The return callback turn did not end: ' + self.dispatch_state(owner))

    def ended(self, owner, status):
        """Wait until the owner's turn reported `status` and its thread ended. Until then the task counts as running."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and self.service._get('tasks', owner['id'])['status'] != status:
            time.sleep(.05)
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], status, self.dispatch_state(owner))
        worker = self.service.running.get(owner['id'])
        if worker:
            worker.join(20)
            self.assertFalse(worker.is_alive(), 'The owner turn did not end: ' + self.dispatch_state(owner))

    def failed_request(self):
        requester = self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'Ask Dev for the grant.'})
        requester = self.update(requester['id'], status='working')
        delivered = self.service._store_bot_message(requester['id'], dict(
            target_bot_id=self.dev['id'], text='Implement the admin-only download grant.', native_envelope_id=ENVELOPE))
        # The saved input carries the PR facts that its failed turn never received.
        self.owner_id = delivered['task_id']
        owner = self.update(delivered['task_id'], status='queued', _native_intake=True, _native_turn_id=TURN,
                            _execution_session_id=delivered['task_id'], _pr_review=dict(PR_REVIEW),
                            _conversation_epoch=self.service._setting('conversation_epoch'))
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        card = self.service.kanban.tasks[owner['id']]
        identity = card['id']
        run = int(self.native_program('c = board.kb.claim_task(board.conn, sys.argv[2])\nprint(c.current_run_id)', identity))
        self.update(owner['id'], status='working', _kanban_started=run, _kanban_claim=dict(
            native_id=identity, external_id=owner['id'], input=TURN, run_id=run, workspace=card['workspace_path']))
        self.service.emit(owner['id'], 'failed', dict(summary=FAILURE))
        self.service._drain_returns()
        self.native_program("assert board.finish(sys.argv[2], int(sys.argv[3]), 'blocked', sys.argv[4])",
                            identity, str(run), FAILURE)
        self.native_program('assert board.archive_settled(sys.argv[2], None)', identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.native(identity)['status'], 'archived')
        Path(card['workspace_path']).mkdir(parents=True, exist_ok=True)
        requester = self.update(requester['id'], status='working')
        return self.service._get('tasks', owner['id']), requester, identity, card['workspace_path']

    def test_continuation_dispatches_a_successor_in_the_same_execution(self):
        owner, requester, archived, workspace = self.failed_request()
        self.assertEqual((owner['status'], owner.get('_native_outcome_id')), ('failed', None))
        self.assertEqual(owner['_pr_review'], PR_REVIEW)
        history = self.native(archived)
        receipt = self.service.emit(requester['id'], 'work_continue', dict(task_id=archived, message='Continue the grant.'))
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=archived, status='queued'))
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['_pr_review'], PR_REVIEW)
        task = self.owner_turn(owner, 1)
        # The initial failure's result reached the requester as its own callback turn.
        self.returned(owner, requester, 1)
        successor = task['_native_outcome_id']
        self.assertNotEqual(successor, archived)
        native = self.native(successor)
        self.assertEqual(native['parents'], [archived])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'])
        self.assertEqual((native['tenant'], native['path']), (owner['bot_id'], workspace))
        self.assertEqual([p for p in self.provider.prepared if p[0] == owner['id']][-1][1:3], (workspace, owner['id']))
        self.assertEqual(self.native(archived), history)
        self.assertEqual((task['_native_turn_id'], task['_kanban_workspace']), (continued['_native_turn_id'], workspace))
        # The provider task carries public fields only. The result route stays on the owner and its source.
        self.assertEqual((task['source_task_id'], task['parent_task_id']), (requester['id'], requester['id']))
        self.assertEqual(self.service._get('tasks', owner['id'])['_reply_to_task_id'], requester['id'])
        # The successor turn receives the PR facts that the failed turn never received.
        self.assertEqual(task['_pr_review'], PR_REVIEW)

    def test_settled_successor_of_a_failed_continuation_stays_failed_and_continues(self):
        owner, requester, archived, workspace = self.failed_request()
        self.fail_turns = True
        self.service.emit(requester['id'], 'work_continue', dict(task_id=archived, message='Continue the grant.'))
        first = self.owner_turn(owner, 1)
        successor = first['_native_outcome_id']
        self.assertNotEqual(successor, archived)
        self.ended(owner, 'failed')
        # Coconut finishes the failed run, and the settle pass archives the successor.
        self.native_program("assert board.finish(sys.argv[2], int(sys.argv[3]), 'blocked', 'The grant tests failed.')",
                            successor, str(first['_kanban_claim']['run_id']))
        self.native_program('assert board.archive_settled(sys.argv[2], None)', successor)
        # Real dispatcher passes deliver both failure returns and reconcile the archived successor of the accepted input.
        self.returned(owner, requester, 2)
        self.tick()
        self.assertEqual(self.service.kanban.tasks[owner['id']]['id'], successor, self.dispatch_state(owner))
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'archived')
        self.assertEqual(self.service.kanban.tasks[owner['id']]['input_id'], first['_native_turn_id'])
        failed = self.service._get('tasks', owner['id'])
        self.assertEqual(failed['status'], 'failed', 'Reconciliation cancelled a failure that no person stopped.')
        self.assertEqual(failed.get('_native_settled_failure'), successor, self.dispatch_state(owner))
        self.assertFalse(any(json.loads(row[0]).get('status') == 'cancelled' for row in self.service.db.execute(
            "SELECT data FROM events WHERE task_id=? AND type='task.updated'", (owner['id'],))))
        self.fail_turns = False
        requester = self.update(requester['id'], status='working')
        receipt = self.service.emit(requester['id'], 'work_continue', dict(task_id=successor, message='Run the tests again.'))
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=successor, status='queued'))
        continued = self.service._get('tasks', owner['id'])
        second = self.owner_turn(owner, 2)
        latest = second['_native_outcome_id']
        self.assertNotIn(latest, (archived, successor))
        native = self.native(latest)
        self.assertEqual(native['parents'], [successor])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'])
        self.assertEqual((native['tenant'], native['path']), (owner['bot_id'], workspace))
        self.assertEqual((second['_native_turn_id'], second['_kanban_workspace']), (continued['_native_turn_id'], workspace))
        self.assertEqual((second['source_task_id'], second['parent_task_id']), (requester['id'], requester['id']))
        self.assertEqual(self.service._get('tasks', owner['id'])['_reply_to_task_id'], requester['id'])
        self.assertEqual(second['_pr_review'], PR_REVIEW)

    def provider_stopped_request(self, error_code='native_billing'):
        """Dev in PR 8606. The request's first input completed its card. New input reached the request without a retained
        native outcome, so Board.sync gave it the successor coconut:<id>:<input>. The provider stopped that turn, Coconut
        finished the unowned card as blocked, and the settle pass archived it.
        """
        requester = self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'Ask Dev for the grant.'})
        requester = self.update(requester['id'], status='working')
        delivered = self.service._store_bot_message(requester['id'], dict(
            target_bot_id=self.dev['id'], text='Implement the admin-only download grant.', native_envelope_id=ENVELOPE))
        self.owner_id = delivered['task_id']
        owner = self.update(delivered['task_id'], status='queued', _native_intake=True, _native_turn_id='first-input',
                            _execution_session_id=delivered['task_id'],
                            _conversation_epoch=self.service._setting('conversation_epoch'))
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        original = self.service.kanban.tasks[owner['id']]['id']
        self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='First input complete')", original)
        # The new input reaches the request, which retained no native outcome.
        owner = self.update(owner['id'], status='queued', _native_turn_id=TURN, _kanban_claim=dict(
            native_id=original, external_id=owner['id'], input='first-input', run_id='completed-run'))
        self.assertIsNone(owner.get('_native_outcome_id'))
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.assertEqual((row['native_id'], row['previous_input'], row['input']), (None, 'first-input', TURN))
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        card = self.service.kanban.tasks[owner['id']]
        identity = card['id']
        self.assertNotEqual(identity, original)
        self.assertEqual(card['idempotency_key'], 'coconut:' + owner['id'] + ':' + TURN)
        run = int(self.native_program('c = board.kb.claim_task(board.conn, sys.argv[2])\nprint(c.current_run_id)', identity))
        self.update(owner['id'], status='working', _kanban_started=run, _kanban_claim=dict(
            native_id=identity, external_id=owner['id'], input=TURN, run_id=run, workspace=card['workspace_path']))
        self.service.emit(owner['id'], 'failed', dict(summary=BILLING, error_code=error_code))
        self.service._drain_returns()
        # The card is not an owned outcome, so Board.finish blocks it instead of retrying the provider stop.
        self.native_program("assert board.finish(sys.argv[2], int(sys.argv[3]), 'blocked', sys.argv[4], sys.argv[5])",
                            identity, str(run), BILLING, error_code)
        self.native_program('assert board.archive_settled(sys.argv[2], None)', identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.native(identity)['status'], 'archived')
        Path(card['workspace_path']).mkdir(parents=True, exist_ok=True)
        requester = self.update(requester['id'], status='working')
        return self.service._get('tasks', owner['id']), requester, identity, card['workspace_path'], original

    def test_a_provider_stopped_successor_continues_its_original_execution(self):
        owner, requester, archived, workspace, original = self.provider_stopped_request()
        self.assertEqual((owner['status'], owner.get('_native_outcome_id'), owner['error_code']),
                         ('failed', None, 'native_billing'))
        history = self.native(archived)
        self.assertEqual((history['key'], history['parents']), ('coconut:' + owner['id'] + ':' + TURN, []))
        self.assertEqual([run[2] for run in history['runs']], ['blocked'])
        receipt = self.service.emit(requester['id'], 'work_continue', dict(task_id=archived, message='Continue the grant.'))
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=archived, status='queued'))
        continued = self.service._get('tasks', owner['id'])
        task = self.owner_turn(owner, 1)
        self.returned(owner, requester, 1)
        successor = task['_native_outcome_id']
        self.assertNotIn(successor, (archived, original))
        native = self.native(successor)
        self.assertEqual(native['parents'], [archived])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'])
        self.assertEqual((native['tenant'], native['path']), (owner['bot_id'], workspace))
        self.assertEqual(self.native(archived), history)
        self.assertEqual((task['_native_turn_id'], task['_kanban_workspace']), (continued['_native_turn_id'], workspace))
        self.assertEqual((task['_execution_session_id'], task['source_task_id'], task['parent_task_id']),
                         (owner['_execution_session_id'], requester['id'], requester['id']))
        self.assertEqual(self.service._get('tasks', owner['id'])['_reply_to_task_id'], requester['id'])

    def test_another_failure_of_that_successor_stays_archived(self):
        owner, requester, archived, workspace, original = self.provider_stopped_request('native_turn_failed')
        with self.assertRaisesRegex(ServiceError, 'not blocked or completed'):
            self.service.emit(requester['id'], 'work_continue', dict(task_id=archived, message='Continue anyway.'))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_cancelled_request_stays_archived(self):
        owner, requester, archived, _ = self.failed_request()
        owner = self.update(owner['id'], status='cancelled')
        with self.assertRaises(ServiceError):
            self.service.emit(requester['id'], 'work_continue', dict(task_id=archived, message='Continue anyway.'))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)


if __name__ == '__main__':
    unittest.main()
