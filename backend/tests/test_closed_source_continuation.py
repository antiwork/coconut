"""A request that Coconut ended because its sources closed continues only when its owner or requester asks explicitly.

The assignment pass archives an idle Hermes card when every GitHub item that links it closed, and comments why
(work_sources.stale_cards, Board.archive_stale). The reconciler then cancels the request of the archived input. That
stays the rule. The saved shape is Dev's private acceptance of PR 7931 for Desk: its retained outcome blocked on a
repair PR, PR 7931 merged, and a month later the pass archived the card and cancelled the request before Dev could
verify the merge locally. Its input after the block, a PR check, never ran.

Only that exact archive continues, and only with continue_closed_source: the archive that wrote Coconut's note at once,
of the card that holds the request's current input, after the claim's run ended without success, with every linked
source still closed. A person's archive, an idle archive, a Stop, a cancelled or dismissed Work item, and changed
input, claim, or run stay final. The continuation keeps the request, its execution, workspace, Work item, and result
route. Native CI sets COCONUT_REQUIRE_NATIVE, and then the native tests fail instead of skipping.
"""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut import routine_coordinator
from coconut.kanban_runtime import Board
from coconut.native_outcomes import CLOSED_SOURCE_REFUSED, CONTINUE_SCHEMA
from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError, now
from coconut.work_sources import STALE_IDLE_AGE, STALE_NOTE, stale_cards
import legacy_work_sync
import test_archived_failed_request_continuation as failed_request_fixtures
import test_archived_scheduled_routine_continuation as routine_fixtures
import test_native_kanban as native_fixtures
import test_native_source_ownership as ownership_fixtures
import test_work_source_coverage as source_fixtures
import hermes_env

setUpModule, tearDownModule = legacy_work_sync.scope()

# The saved shape: Desk's relay, Dev's retained outcome, its blocked run, the claimed input, and the later input.
ENVELOPE = '39e5333e480744e3b0d9a828b9ea03f4'
NATIVE = 't_6fbf4af4'
RUN = 8097
CLAIMED = '9b8db55b-d4d0-4ec6-8b61-569d0ce03071'
TURN = 'f332b129-a6ac-4a1e-b3cf-47b8303e7046'
PR = 'https://github.com/antiwork/gumroad/pull/7931'
REPAIR = 'https://github.com/antiwork/gumroad/pull/7932'
RUNS = [dict(id=RUN, status='blocked', outcome='blocked', ended=True)]
PR_REVIEW = dict(url=PR, pr_state='open', draft=True, head_sha='9651d580c55eb199439803545eecc26f83e57311',
                 checked_at='2026-09-24T14:01:29.674280Z', checks_state='running', required_checks_state='running',
                 review_checks=None, check_failures=None)
MESSAGE = 'PR 7931 merged as 0608665d. Verify the merged change locally in the same workspace and report the result.'
SOURCE_KEY = 'work-source:desk-github-assigned'
# The fields of kanban_runtime.Board.completion. ClosedSourceNativeTests checks them against the real adapter.
COMPLETION_FIELDS = {'id', 'tenant', 'idempotency_key', 'status', 'current_run_id', 'worker_pid', 'input_id', 'runs', 'archive'}
ARCHIVE_FIELDS = {'at', 'run_id', 'author', 'note', 'later'}


class ClosedSource:
    """A real Service whose retained outcome the assignment pass archived after its sources closed."""

    update = routine_fixtures.ScheduledOccurrence.update
    working = failed_request_fixtures.ArchivedRequest.working

    def setUp(self):
        self.store_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.store_tmp.cleanup)
        self.store = Service(self.store_tmp.name, FixtureProvider(self.store_tmp.name), start_workers=False)
        self.addCleanup(self.store.close)
        self.desk = self.store.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'test-phone')
        self.dev = self.store.mutate('POST', '/v1/bots', {'name': 'Dev'}, str(uuid4()), 'test-phone')
        self.runs, self.archives, self.calls = {}, {}, []
        self.store.kanban = Mock(tasks={})
        self.store.kanban.call.side_effect = self.adapter

    def adapter(self, request):
        """The adapter operations that these passes use, with the fields of kanban_runtime.Board."""
        self.calls.append(request)
        cards = {card['id']: card for card in self.store.kanban.tasks.values()}
        if request['op'] == 'snapshot':
            return {'tasks': [dict(card, external_id=identity) for identity, card in self.store.kanban.tasks.items()]}
        if request['op'] == 'completion':
            card = cards.get(request['id'])
            return {'card': card and dict(id=card['id'], tenant=card['tenant'], idempotency_key=card['idempotency_key'],
                status=card['status'], current_run_id=card['current_run_id'], worker_pid=card['worker_pid'],
                input_id=card['input_id'], runs=copy.deepcopy(self.runs.get(card['id'], [])),
                archive=copy.deepcopy(self.archives.get(card['id'])))}
        raise AssertionError(request)

    def links(self, *names):
        """Desk's work source links the card to PR 7931 and its repair PR. Both left the open list."""
        left = time.time() - 30
        state = dict(native_links={PR: dict(updated_at='2026-09-24T14:01:28Z', task_ids=['t_333493c8', *names], decision_ids=[]),
                                   REPAIR: dict(updated_at='2026-09-24T13:41:36Z', task_ids=[*names, 't_c8b29d3a'], decision_ids=[])},
                     source_left={PR: left, REPAIR: left})
        with self.store.transaction():
            self.store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (SOURCE_KEY, json.dumps(state)))
        return state

    def source_state(self, change):
        with self.store.transaction():
            state = self.store._setting(SOURCE_KEY)
            change(state)
            self.store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (SOURCE_KEY, json.dumps(state)))

    def stale_archive(self, owner, identity, *, note=STALE_NOTE, author='coconut', later=('commented',)):
        """Board.archive_stale archives the idle card and comments the note. Then the reconciler reads the archive."""
        card = self.store.kanban.tasks[owner['id']]
        self.assertEqual(card['id'], identity)
        card.update(status='archived', current_run_id=None, worker_pid=None, claim_lock=None)
        self.archives[identity] = dict(at=int(time.time()), run_id=None, author=author, note=note, later=list(later))
        self.store._reconcile_kanban()
        return self.store._get('tasks', owner['id'])

    def closed(self, owner_status='queued'):
        """The saved shape, from Desk's relay to the reconciler's cancellation after the stale archive."""
        requester = self.working(self.desk, 'Own private acceptance and scoped follow-through for PR 7931.')
        delivered = self.store._store_bot_message(requester['id'], dict(
            target_bot_id=self.dev['id'], text='Own private acceptance and scoped follow-through for assigned PR 7931.',
            native_envelope_id=ENVELOPE))
        owner = self.store._get('tasks', delivered['task_id'])
        self.workspace = self.store.root / 'workspaces' / owner['id']
        self.workspace.mkdir(parents=True)
        self.item = dict(id=str(uuid4()), bot_id=self.dev['id'], task_id=owner['id'], kind='work',
                         title='Own private acceptance for PR 7931', status='waiting', activity='Waiting for a result',
                         preview='', url=None, created_at=now(), observed_at=now(), _work_key='retained-pr:' + PR,
                         _native_outcome_id=NATIVE, completion_condition='Own private acceptance for PR 7931')
        self.store._put('artifacts', self.item)
        # The blocked run ended the claimed input. A PR check then queued a later input, which the card accepted.
        owner = self.update(owner['id'], _native_intake=True, status=owner_status, _execution_session_id=owner['id'],
                            _native_turn_id=TURN, _conversation_epoch=self.store._setting('conversation_epoch'),
                            _native_outcome_id=NATIVE, _work_context_id=self.item['id'], _reported_pr_urls=[PR],
                            _kanban_claim=dict(native_id=NATIVE, external_id=owner['id'], input=CLAIMED, run_id=RUN,
                                               claim_lock='gumclaw:70352', workspace=str(self.workspace)),
                            _kanban_started=RUN, _pr_review=dict(PR_REVIEW))
        card = dict(external_id=owner['id'], id=NATIVE, status='blocked', tenant=self.dev['id'],
                    idempotency_key='coconut-outcome:' + self.item['id'], created_by='coconut', current_run_id=None,
                    worker_pid=None, claim_lock=None, input_id=TURN, workspace_path=str(self.workspace),
                    last_activity_at=time.time() - 31 * 86400, dependencies=[], dispatch_blocker=None,
                    block_kind='dependency', outcome_summary='Waiting on the repair PR 7932.')
        self.store.kanban.tasks[owner['id']] = card
        self.runs[NATIVE] = copy.deepcopy(RUNS)
        state = self.links(NATIVE)
        # The real rule picks this card: every linked source left, and nobody worked on it since.
        tasks = {t['id']: t for t in self.store._all('tasks')}
        self.assertEqual(stale_cards(set(), state['native_links'], state['source_left'], dict(self.store.kanban.tasks),
                                     tasks, {}, time.time()), {NATIVE: state['source_left'][PR]})
        cancelled = self.stale_archive(owner, NATIVE)
        self.assertEqual((cancelled['status'], cancelled['summary']), ('cancelled', 'Waiting on the repair PR 7932.'))
        self.assertEqual(len(self.cancellations(owner)), 1)
        self.requester = self.update(requester['id'], status='working')
        return cancelled

    def resume(self, caller=None, message=MESSAGE, identity=NATIVE, reopen=True):
        self.assertFalse(self.store.db.in_transaction, 'A continuation must not start inside another transaction.')
        payload = dict(task_id=identity, message=message)
        if reopen is not None:
            payload['continue_closed_source'] = reopen
        return self.store.emit((caller or self.requester)['id'], 'work_continue', payload)

    def assertRejected(self, owner, pattern='.', caller=None, reopen=True, identity=NATIVE):
        before = self.store._get('tasks', owner['id'])
        deliveries = len(self.store._all('deliveries'))
        with self.assertRaisesRegex(ServiceError, pattern):
            self.resume(caller, identity=identity, reopen=reopen)
        self.assertEqual(self.store._get('tasks', owner['id']), before)
        self.assertEqual(len(self.store._all('deliveries')), deliveries)

    def qualifies(self, owner):
        return self.store.settled_closed_source(owner, self.store.kanban.tasks[owner['id']], lambda: self.adapter(
            {'op': 'completion', 'id': self.store.kanban.tasks[owner['id']]['id']})['card'])

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


class ClosedSourceAdmissionTests(ClosedSource, unittest.TestCase):
    def test_saved_shape_continues_through_its_requester_only_when_asked_explicitly(self):
        owner = self.closed()
        before = copy.deepcopy(owner)
        # The plain call keeps the assignment pass's rule and says how to ask for the work again.
        self.assertRejected(owner, CLOSED_SOURCE_REFUSED, reopen=None)
        self.assertRejected(owner, CLOSED_SOURCE_REFUSED, reopen=False)
        receipt = self.resume()
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=NATIVE, status='queued'))
        continued = self.store._get('tasks', owner['id'])
        self.assertEqual((continued['status'], continued['_native_outcome_id']), ('queued', NATIVE))
        self.assertNotEqual(continued['_native_turn_id'], TURN)
        for key in ('id', 'bot_id', 'session_id', 'thread_id', 'exchange_id', 'conversation_bot_id', '_execution_session_id',
                    '_kanban_claim', '_kanban_started', 'source_bot_id', 'source_task_id', 'parent_task_id',
                    '_reply_to_task_id', '_native_relay', '_native_intake', '_work_context_id', '_reported_pr_urls',
                    'created_at'):
            self.assertEqual(continued.get(key), before.get(key), key)
        # The PR check of the ended turn said the PR was open. The continuation's message replaces it.
        self.assertNotIn('_pr_review', continued)
        self.assertEqual(continued['_self_recovery']['previous_task_id'], owner['id'])
        self.assertEqual(continued['_self_recovery']['unknown_tools'], [])
        self.assertEqual(continued['_bot_messages'][-1], dict(sender_bot_id=self.desk['id'], sender_name='Desk',
                                                              text=MESSAGE, result_status=None, progress_only=False))
        self.assertIn({'op': 'completion', 'id': NATIVE}, self.calls)
        self.assertEqual(len(self.cancellations(owner)), 1)
        # The Work item keeps presenting the outcome.
        self.assertEqual(self.store._get('artifacts', self.item['id']), self.item)
        # The same delivery is idempotent.
        self.assertEqual(self.resume(), receipt)
        self.assertEqual(self.store._get('tasks', owner['id']), continued)
        self.assertEqual(len([d for d in self.store._all('deliveries') if d.get('receipt') == receipt]), 1)

    def test_the_owner_itself_can_continue(self):
        owner = self.closed()
        caller = self.working(self.dev, 'Verify the merged PR 7931.')
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])
        continued = self.store._get('tasks', owner['id'])
        self.assertEqual(continued['_bot_messages'][-1]['sender_bot_id'], self.dev['id'])
        self.assertEqual(continued['_reply_to_task_id'], owner['_reply_to_task_id'])

    def test_only_the_owner_or_its_requester_can_continue(self):
        owner = self.closed()
        other = self.store.mutate('POST', '/v1/bots', {'name': 'Other'}, str(uuid4()), 'test-phone')
        self.assertRejected(owner, 'Only the owner or its requesting bot', caller=self.working(other, 'Continue it.'))
        # A caller that is not working, such as the requester's finished turn, cannot continue anything.
        self.requester = self.update(self.requester['id'], status='completed')
        self.assertRejected(owner, 'cannot continue work now')

    def test_the_new_input_gets_a_successor_and_later_passes_keep_it(self):
        owner = self.closed()
        self.resume()
        continued = self.store._get('tasks', owner['id'])
        row = self.store._kanban_row(continued, continued, 'ready', 10)
        # Board.sync sees new input for the archived card and makes its successor in the same workspace.
        self.assertEqual((row['native_id'], row['previous_input'], row['input'], row['workspace']),
                         (NATIVE, CLAIMED, continued['_native_turn_id'], str(self.workspace)))
        # Before Board.sync runs, reconciliation sees the archived card, which never accepted the new input.
        for _ in range(2):
            self.store._reconcile_kanban()
            self.assertEqual(self.store._get('tasks', owner['id']), continued)
        running = self.dispatch_successor(owner, 't_successor', RUN + 1)
        self.assertEqual(running['_reply_to_task_id'], self.requester['id'])
        self.assertEqual(self.store._get('artifacts', self.item['id'])['_native_outcome_id'], 't_successor')
        # The successor blocks again. The source is still closed, but someone worked on the card after it left, so the
        # assignment pass leaves it, however the source links it.
        self.store.kanban.tasks[owner['id']].update(status='blocked', current_run_id=None, worker_pid=None,
                                                    last_activity_at=time.time() - STALE_IDLE_AGE - 60)
        self.runs['t_successor'][-1].update(status='blocked', outcome='blocked', ended=True)
        self.update(owner['id'], status='waiting')
        tasks = {t['id']: t for t in self.store._all('tasks')}
        state = self.store._setting(SOURCE_KEY)
        state['source_left'] = {item: time.time() - STALE_IDLE_AGE - 120 for item in state['source_left']}
        for names in (['t_successor'], [owner['id']]):
            links = {item: dict(binding, task_ids=names) for item, binding in state['native_links'].items()}
            self.assertEqual(stale_cards(set(), links, state['source_left'], dict(self.store.kanban.tasks), tasks, {},
                                         time.time()), {})
        self.store._reconcile_kanban()
        self.assertEqual(self.store._get('tasks', owner['id'])['status'], 'waiting')
        self.assertEqual(len(self.cancellations(owner)), 1)

    def test_its_result_returns_once_to_the_original_requester(self):
        owner = self.closed()
        self.resume()

        def returns():
            deliveries = [d for d in self.store._all('deliveries') if d['source_task_id'] == owner['id']]
            callbacks = [t for t in self.store._all('tasks') if t.get('_is_return') and t.get('source_task_id') == owner['id']]
            return deliveries, callbacks
        earlier = [{item['id'] for item in found} for found in returns()]
        self.dispatch_successor(owner, 't_successor', RUN + 1)
        self.store.emit(owner['id'], 'completed', dict(summary='PR 7931 verified locally at 0608665d.'))
        self.runs['t_successor'][-1].update(status='done', outcome='completed', ended=True)
        self.store.kanban.tasks[owner['id']].update(status='done', current_run_id=None, worker_pid=None, claim_lock=None,
                                                    outcome_summary='PR 7931 verified locally at 0608665d.')
        for _ in range(2):
            self.store._reconcile_kanban()
            self.store._drain_returns()
        deliveries, callbacks = [[item for item in found if item['id'] not in seen] for found, seen in zip(returns(), earlier)]
        self.assertEqual([(d['target_bot_id'], d['text']) for d in deliveries],
                         [(self.desk['id'], 'PR 7931 verified locally at 0608665d.')])
        self.assertEqual([(t['bot_id'], t['_is_return']) for t in callbacks], [(self.desk['id'], True)])
        finished = self.store._get('tasks', owner['id'])
        self.assertEqual((finished['status'], finished['_reply_to_task_id']), ('completed', self.requester['id']))
        self.assertFalse(finished.get('_return_pending'))

    def test_a_reopened_source_that_closes_again_can_end_and_continue_the_work_again(self):
        owner = self.closed()
        self.resume()
        self.dispatch_successor(owner, 't_successor', RUN + 1)
        self.store.kanban.tasks[owner['id']].update(status='blocked', current_run_id=None, worker_pid=None,
                                                    last_activity_at=time.time() - 120)
        self.runs['t_successor'][-1].update(status='blocked', outcome='blocked', ended=True)
        self.update(owner['id'], status='waiting')
        # The item opened again, which cleared its time, and closed again after the successor's last activity.
        self.source_state(lambda state: state['source_left'].update({PR: time.time() - 60, REPAIR: time.time() - 60}))
        cancelled = self.stale_archive(owner, 't_successor')
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(len(self.cancellations(owner)), 2)
        # The earlier source left time names only the original card, so the request's own link carries the successor.
        self.assertFalse(self.qualifies(cancelled))
        self.source_state(lambda state: state['native_links'][PR]['task_ids'].append(owner['id']))
        self.assertTrue(self.qualifies(cancelled))
        self.requester = self.update(self.requester['id'], status='working')
        self.assertRejected(cancelled, CLOSED_SOURCE_REFUSED, reopen=None, identity='t_successor')
        receipt = self.resume(identity='t_successor', message='Verify PR 7931 again.')
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_successor', status='queued'))
        again = self.store._get('tasks', owner['id'])
        self.assertEqual(again['_reply_to_task_id'], self.requester['id'])
        second = self.dispatch_successor(owner, 't_successor_2', RUN + 2)
        self.assertEqual(second['_kanban_claim']['input'], again['_native_turn_id'])
        self.assertEqual(len(self.cancellations(owner)), 2)

    def test_the_flag_applies_only_to_closed_source_work(self):
        parameters = CONTINUE_SCHEMA['parameters']
        self.assertEqual(parameters['properties']['continue_closed_source']['type'], 'boolean')
        self.assertEqual(parameters['required'], ['task_id', 'message'])
        owner = self.closed()
        with self.assertRaisesRegex(ServiceError, 'must be true or false'):
            self.resume(reopen='yes')
        # A blocked outcome that nobody archived continues with the plain call only.
        ClosedSource.setUp(self)
        owner = self.closed()
        card = self.store.kanban.tasks[owner['id']]
        card['status'] = 'blocked'
        self.archives.pop(NATIVE)
        owner = self.update(owner['id'], status='waiting')
        self.assertRejected(owner, 'applies only to work that Coconut ended')
        self.assertEqual(self.resume(reopen=None)['task_id'], owner['id'])

    def test_the_request_events_and_source_states_use_their_indexes(self):
        plan = ' '.join(row[-1] for row in self.store.db.execute(
            'EXPLAIN QUERY PLAN ' + routine_coordinator.REQUEST_EVENT_TIMES, ('x',)))
        self.assertIn('events_task', plan)
        self.assertNotIn('SCAN events', plan)
        plan = ' '.join(row[-1] for row in self.store.db.execute('EXPLAIN QUERY PLAN ' + routine_coordinator.WORK_SOURCE_STATES))
        self.assertIn('SEARCH settings', plan)

    def test_adapter_without_an_answer_admits_nothing(self):
        owner = self.closed()

        def silent(request):
            if request['op'] == 'completion':
                raise RuntimeError('The Hermes Kanban adapter stopped.')
            return self.adapter(request)
        self.store.kanban.call.side_effect = silent
        self.assertRejected(owner)
        self.store.kanban.call.side_effect = lambda request: {'card': None} if request['op'] == 'completion' else self.adapter(request)
        self.assertRejected(owner)


class OtherEndingsStayFinalTests(ClosedSource, unittest.TestCase):
    def test_a_person_or_idle_archive_stays_final(self):
        cases = dict(
            person_archive=lambda owner: self.archives.update({NATIVE: None}),
            idle_archive=lambda owner: (self.archives.update({NATIVE: None}), self.store._put('decisions', dict(
                id='archived', task_id=owner['id'], bot_id=owner['bot_id'], status='rejected', decided_by='archive',
                decided_by_name='Idle task archive', outcome='Task archived.'))),
            closed_card=lambda owner: self.store._put('decisions', dict(
                id='archived', task_id=str(uuid4()), _execution_task_id=owner['id'], bot_id=owner['bot_id'],
                status='rejected', decided_by='archive', decided_by_name='Closed source', outcome='Task archived.')),
            note_of_a_person=lambda owner: self.archives[NATIVE].update(author='dashboard'),
            note_of_a_worker=lambda owner: self.archives[NATIVE].update(author='default'),
            other_note=lambda owner: self.archives[NATIVE].update(note='Coconut ended this task.'),
            archived_run=lambda owner: self.archives[NATIVE].update(run_id=RUN),
            later_claim=lambda owner: self.archives[NATIVE].update(later=['commented', 'claimed']),
            later_unblock=lambda owner: self.archives[NATIVE].update(later=['unblocked']),
            text_time=lambda owner: self.archives[NATIVE].update(at=str(self.archives[NATIVE]['at'])),
            cancelled_before_the_archive=lambda owner: self.archives[NATIVE].update(at=int(time.time()) + 3600))
        for name, change in cases.items():
            with self.subTest(name):
                ClosedSource.setUp(self)
                owner = self.closed()
                change(owner)
                self.assertFalse(self.qualifies(owner))
                self.assertRejected(owner)
                self.assertRejected(owner, 'not blocked or completed', reopen=None)

    def test_a_stop_and_a_cancelled_or_dismissed_work_item_stay_final(self):
        # The person stops the cancelled request: the exchange records it. The Stop ends the requester's turn too, so a
        # new turn of the requester asks.
        owner = self.closed()
        self.store.mutate('POST', '/v1/tasks/' + owner['id'] + '/cancel', {}, str(uuid4()), 'test-phone')
        self.assertEqual(self.store._get('tasks', self.requester['id'])['status'], 'cancelled')
        self.assertRejected(owner, 'paused, cancelled', caller=self.working(self.desk, 'Verify PR 7931 after all.'))
        # The person stopped the request before the archive. The reconciler then has nothing left to cancel.
        ClosedSource.setUp(self)
        requester = self.working(self.desk, 'Own private acceptance and scoped follow-through for PR 7931.')
        delivered = self.store._store_bot_message(requester['id'], dict(
            target_bot_id=self.dev['id'], text='Own private acceptance for PR 7931.', native_envelope_id=ENVELOPE))
        self.store.mutate('POST', '/v1/tasks/' + delivered['task_id'] + '/cancel', {}, str(uuid4()), 'test-phone')
        stopped = self.store._get('tasks', delivered['task_id'])
        self.assertEqual(stopped['status'], 'cancelled')
        self.assertTrue(self.store.db.execute("SELECT 1 FROM events WHERE task_id=? AND type='task.cancelled'",
                                              (stopped['id'],)).fetchone())
        # A Stop record alone holds the request, even when its exchange were open.
        for exchange in self.store._all('exchanges'):
            exchange.pop('cancelled', None)
            self.store._put('exchanges', exchange)
        self.store.kanban.tasks[stopped['id']] = dict(
            external_id=stopped['id'], id=NATIVE, status='archived', tenant=self.dev['id'], idempotency_key='coconut:' + stopped['id'],
            created_by='coconut', current_run_id=None, worker_pid=None, claim_lock=None, input_id=TURN, dependencies=[],
            last_activity_at=time.time() - 31 * 86400)
        self.runs[NATIVE] = copy.deepcopy(RUNS)
        self.archives[NATIVE] = dict(at=int(time.time()) - 5, run_id=None, author='coconut', note=STALE_NOTE, later=[])
        stopped = self.update(stopped['id'], _native_turn_id=TURN, _native_outcome_id=NATIVE, _kanban_started=RUN,
                              _execution_session_id=stopped['id'], _conversation_epoch=self.store._setting('conversation_epoch'),
                              _kanban_claim=dict(native_id=NATIVE, external_id=stopped['id'], input=CLAIMED, run_id=RUN))
        self.links(NATIVE)
        self.requester = self.update(requester['id'], status='working')
        self.assertFalse(self.qualifies(stopped))
        self.assertRejected(stopped)
        # The person cancelled or dismissed the request's Work.
        for prefix in ('work.cancelled.', 'work.dismissed.'):
            with self.subTest(prefix):
                ClosedSource.setUp(self)
                owner = self.closed()
                with self.store.transaction():
                    self.store.db.execute('INSERT INTO settings VALUES (?,?)',
                                          (prefix + 'work:' + self.item['id'], json.dumps(now())))
                self.assertFalse(self.qualifies(owner))
                self.assertRejected(owner)

    def test_the_archive_must_be_this_claim_input_and_failed_run(self):
        cases = dict(
            claim_of_another_card=lambda owner, card, runs: owner['_kanban_claim'].update(native_id='t_other'),
            claim_of_another_request=lambda owner, card, runs: owner['_kanban_claim'].update(external_id=str(uuid4())),
            run_not_int=lambda owner, card, runs: owner['_kanban_claim'].update(run_id=str(RUN)),
            not_started=lambda owner, card, runs: owner.update(_kanban_started=RUN - 4),
            other_outcome=lambda owner, card, runs: owner.update(_native_outcome_id='t_other'),
            unretained=lambda owner, card, runs: owner.pop('_native_outcome_id'),
            no_turn=lambda owner, card, runs: owner.pop('_native_turn_id'),
            later_input=lambda owner, card, runs: owner.update(_native_turn_id=str(uuid4())),
            accepted_other_input=lambda owner, card, runs: card.update(input_id=CLAIMED),
            foreign_tenant=lambda owner, card, runs: card.update(tenant=self.desk['id']),
            not_coconut=lambda owner, card, runs: card.update(created_by='someone'),
            live_run=lambda owner, card, runs: card.update(current_run_id=RUN + 1),
            live_worker=lambda owner, card, runs: card.update(worker_pid=4242),
            open_dependency=lambda owner, card, runs: card.update(dependencies=[dict(id='t_c8b29d3a', status='blocked')]),
            completed_run=lambda owner, card, runs: runs.insert(0, dict(id=RUN - 1, status='done', outcome='completed', ended=True)),
            scheduled_run=lambda owner, card, runs: runs[-1].update(status='scheduled', outcome='scheduled'),
            open_run=lambda owner, card, runs: runs[-1].update(ended=False),
            later_run=lambda owner, card, runs: runs.append(dict(id=RUN + 1, status='blocked', outcome='blocked', ended=True)),
            no_runs=lambda owner, card, runs: runs.clear(),
            created_twice=lambda owner, card, runs: self.store._event('task.created', self.store._public(owner),
                                                                       owner['bot_id'], owner['id']),
            routine=lambda owner, card, runs: owner.update(routine_id=str(uuid4())),
            routine_job=lambda owner, card, runs: owner.update(_native_job=dict(id='job')))
        for name, change in cases.items():
            with self.subTest(name):
                ClosedSource.setUp(self)
                owner = self.closed()
                card = self.store.kanban.tasks[owner['id']]
                change(owner, card, self.runs[NATIVE])
                self.store._put('tasks', owner)
                owner = self.store._get('tasks', owner['id'])
                self.assertFalse(self.qualifies(owner))
                self.assertRejected(owner)
        # The adapter must report the same card, key, and input as the snapshot.
        for name, change in dict(other_key=dict(idempotency_key='coconut:other'), other_input=dict(input_id=CLAIMED),
                                 not_archived=dict(status='blocked')).items():
            with self.subTest(name):
                ClosedSource.setUp(self)
                owner = self.closed()

                def changed(request, change=change):
                    result = self.adapter(request)
                    if request['op'] == 'completion':
                        result['card'].update(change)
                    return result
                self.store.kanban.call.side_effect = changed
                self.assertRejected(owner)

    def test_every_linked_source_must_still_be_closed(self):
        def other_open_item(state):
            state['native_links']['https://github.com/antiwork/gumroad/pull/8000'] = dict(task_ids=[NATIVE], decision_ids=[])

        def pending_card(state):
            self.store._put('decisions', dict(id='source-card', task_id=str(uuid4()), bot_id=self.desk['id'], status='pending'))
            state['native_links'][PR]['decision_ids'] = ['source-card']
        cases = dict(
            no_link=lambda state: [binding['task_ids'].remove(NATIVE) for binding in state['native_links'].values()],
            reopened=lambda state: state['source_left'].pop(PR),
            left_after_the_archive=lambda state: state['source_left'].update({REPAIR: time.time() + 3600}),
            other_open_item=other_open_item, pending_card=pending_card,
            broken_cards=lambda state: state['native_links'][PR].update(decision_ids='source-card'))
        for name, change in cases.items():
            with self.subTest(name):
                ClosedSource.setUp(self)
                owner = self.closed()
                self.source_state(change)
                self.assertFalse(self.qualifies(owner))
                self.assertRejected(owner)
        # A link that names the request instead of its card counts the same.
        ClosedSource.setUp(self)
        owner = self.closed()
        self.source_state(lambda state: [binding['task_ids'].__setitem__(binding['task_ids'].index(NATIVE), owner['id'])
                                         for binding in state['native_links'].values()])
        self.assertEqual(self.resume()['task_id'], owner['id'])

    def test_live_work_decisions_unknown_effects_and_other_input_hold(self):
        def pending_decision(owner):
            self.store._put('decisions', dict(id='pending', task_id=owner['id'], bot_id=owner['bot_id'], status='pending'))

        def unknown_tool(owner):
            self.store._put('native_receipts', dict(id='unknown', task_id=owner['id'], turn_id=TURN, state='executing',
                action_hash='hash', action={'name': 'terminal', 'arguments': {}}, created_at='2026-09-24T14:02:00Z'))

        def open_wait(owner):
            # A wait for another task that has no result yet.
            self.store._put('native_receipts', dict(id='wait', task_id=owner['id'], turn_id=CLAIMED, state='completed',
                action_hash='hash', action={'name': 'coconut_wait', 'arguments': {}}, created_at='2026-09-24T13:50:00Z',
                result='{}', _wait_task_id='dependency', _wait_session_id='session'))

        def active_helper(owner):
            helper = dict(owner, id=str(uuid4()), status='working', _assignment_task_id=owner['id'])
            for key in ('_native_outcome_id', '_kanban_claim', '_work_context_id'):
                helper.pop(key, None)
            self.store._put('tasks', helper)

        def source_decision(owner):
            # A later audit of PR 7931 waits for the person.
            audit = dict(owner, id=str(uuid4()), status='needs_you', _source_assignment=dict(source_id='desk-github-assigned', id=PR))
            for key in ('_native_outcome_id', '_kanban_claim', '_work_context_id', '_execution_session_id'):
                audit.pop(key, None)
            self.store._put('tasks', audit)
            self.store._put('decisions', dict(id='audit-card', task_id=audit['id'], bot_id=owner['bot_id'], status='pending'))
            self.update(owner['id'], _source_assignment=dict(source_id='desk-github-assigned', id=PR))

        def live_worker(owner):
            self.store.running[owner['id']] = Mock(is_alive=Mock(return_value=True))
        cases = dict(
            pending_decision=pending_decision, unknown_tool=unknown_tool, open_wait=open_wait,
            active_helper=active_helper, source_decision=source_decision, live_worker=live_worker,
            recorded_unknown=lambda owner: self.update(owner['id'], _self_recovery=dict(unknown_tools=[dict(id='earlier-write')])),
            steering=lambda owner: self.update(owner['id'], _steering=['Use the other checkout.']),
            live_message=lambda owner: self.update(owner['id'], _live_messages={'m': 'Wait.'}),
            blocked_followup=lambda owner: self.update(owner['id'], _blocked_followup=dict(
                message_ids=['m'], source_ids=['m'], attachment_ids=[])),
            return_pending=lambda owner: self.update(owner['id'], _return_pending=dict(text='Saved result')),
            waiting=lambda owner: self.update(owner['id'], _waiting_on=['dependency']),
            resume_decision=lambda owner: self.update(owner['id'], _resume_decision='decision'),
            work_resume=lambda owner: self.update(owner['id'], _work_resume=dict(reason='resume')),
            other_conversation=lambda owner: self.update(owner['id'], _conversation_epoch='earlier'))
        for name, change in cases.items():
            with self.subTest(name):
                ClosedSource.setUp(self)  # A fresh store, so one admission cannot answer the next case.
                owner = self.closed()
                change(owner)
                self.assertRejected(self.store._get('tasks', owner['id']))
        # The saved background completion of the ended turn is replaced like its PR check.
        ClosedSource.setUp(self)
        owner = self.closed()
        owner = self.update(owner['id'], _background_completion=dict(process_id='proc-1', result='Finished.'))
        self.resume()
        self.assertNotIn('_background_completion', self.store._get('tasks', owner['id']))


class ArchiveNoteTests(unittest.TestCase):
    """Board.archive_note reads only the task_events and task_comments rows that Hermes writes. The native tests check
    the same reads on a real board. These tables have Hermes' columns, so the cases that a real board rarely shows run
    here too."""

    def setUp(self):
        self.board = Board.__new__(Board)
        self.board.conn = sqlite3.connect(':memory:')
        self.board.conn.executescript(
            'CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER, '
            'kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);'
            'CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, author TEXT NOT NULL, '
            'body TEXT NOT NULL, created_at INTEGER NOT NULL);')
        self.clock = 1792981400

    def event(self, kind, payload=None, run_id=None):
        self.board.conn.execute('INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)',
                                (NATIVE, run_id, kind, payload and json.dumps(payload), self.clock))

    def comment(self, author, body, event=True):
        """kanban_db.add_comment writes a row and its event. kanban_db._insert_comment writes the row alone."""
        self.board.conn.execute('INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)',
                                (NATIVE, author, body, self.clock))
        if event:
            self.event('commented', dict(author=author, len=len(body)))

    def stale(self):
        self.event('archived')
        self.comment('coconut', STALE_NOTE)

    def test_the_note_of_archive_stale_and_later_comments(self):
        self.comment('worker', 'Readiness observation.')
        self.comment('dispatcher', 'Reclaimed an orphaned worker.', event=False)
        self.clock += 60
        self.stale()
        self.assertEqual(self.board.archive_note(NATIVE), dict(at=self.clock, run_id=None, author='coconut', note=STALE_NOTE,
                                                              later=[]))
        self.clock += 60
        self.comment('default', 'Recovery checkpoint.')
        self.assertEqual(self.board.archive_note(NATIVE)['later'], ['commented'])

    def test_other_archives_have_no_note(self):
        self.assertIsNone(self.board.archive_note(NATIVE))
        self.event('archived')
        self.assertIsNone(self.board.archive_note(NATIVE))
        # A person's comment after a person's archive is that person's, which settled_closed_source refuses.
        self.comment('dashboard', 'Archived by hand.')
        self.assertEqual(self.board.archive_note(NATIVE)['author'], 'dashboard')
        # Coconut's note after another event is no note of the archive.
        self.event('archived')
        self.event('unarchived')
        self.comment('coconut', STALE_NOTE)
        self.assertIsNone(self.board.archive_note(NATIVE))
        # Another Coconut comment that a later commented event does not describe is not the note: the length differs.
        self.event('archived')
        self.board.conn.execute('INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)',
                                (NATIVE, 'coconut', 'Specified.', self.clock))
        self.event('commented', dict(author='coconut', len=len(STALE_NOTE)))
        self.assertIsNone(self.board.archive_note(NATIVE))
        # Only the latest archive counts.
        self.clock += 60
        self.stale()
        self.assertEqual(self.board.archive_note(NATIVE)['note'], STALE_NOTE)
        self.clock += 60
        self.event('unarchived')
        self.event('archived')
        self.assertIsNone(self.board.archive_note(NATIVE))


@hermes_env.needs_hermes
class ClosedSourceNativeTests(unittest.TestCase):
    """The installed Hermes kernel: the real assignment pass archives the card, and the continuation gets a successor."""

    setUp = failed_request_fixtures.ArchivedFailedRequestNativeTests.setUp
    write = native_fixtures.NativeKanbanTests.write
    native_program = native_fixtures.NativeKanbanTests.native_program
    tick = native_fixtures.NativeKanbanTests.tick
    update = failed_request_fixtures.ArchivedFailedRequestNativeTests.update
    native = failed_request_fixtures.ArchivedFailedRequestNativeTests.native
    owner_turn = failed_request_fixtures.ArchivedFailedRequestNativeTests.owner_turn
    dispatch_state = failed_request_fixtures.ArchivedFailedRequestNativeTests.dispatch_state
    audit = source_fixtures.WorkSourceTests.audit
    source = ownership_fixtures.NativeSourceOwnershipTests.source

    def completion(self, identity):
        return self.service.kanban.call({'op': 'completion', 'id': identity})['card']

    def blocked_outcome(self):
        """Desk relays the work to Dev. Dev's retained outcome runs, and its worker blocks it on another PR."""
        self.source()
        requester = self.update(self.write('/v1/bots/' + self.bot['id'] + '/messages',
                                           {'text': 'Ask Dev for private acceptance.'})['id'], status='working')
        delivered = self.service._store_bot_message(requester['id'], dict(
            target_bot_id=self.dev['id'], text='Own private acceptance for the assigned issue.', native_envelope_id=ENVELOPE))
        self.owner_id = delivered['task_id']
        owner = self.update(delivered['task_id'], status='queued', _native_intake=True, _native_turn_id=TURN,
                            _execution_session_id=delivered['task_id'],
                            _conversation_epoch=self.service._setting('conversation_epoch'))
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        card = self.service.kanban.tasks[owner['id']]
        identity = card['id']
        # Coconut retains the card as the request's outcome, and the card accepts the request's input.
        self.native_program('board.bind(sys.argv[2], sys.argv[3])', identity, owner['id'])
        owner = self.update(owner['id'], _native_outcome_id=identity)
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        run = int(self.native_program('c = board.kb.claim_task(board.conn, sys.argv[2])\nprint(c.current_run_id)', identity))
        self.native_program("board.kb.block_task(board.conn, sys.argv[2], reason='Waiting on the repair PR', "
                            'expected_run_id=int(sys.argv[3]))', identity, str(run))
        owner = self.update(owner['id'], status='waiting', _kanban_started=run, _kanban_claim=dict(
            native_id=identity, external_id=owner['id'], input=TURN, run_id=run, workspace=card['workspace_path']))
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual((self.service.kanban.tasks[owner['id']]['status'], self.service.kanban.tasks[owner['id']]['input_id']),
                         ('blocked', TURN))
        # The assignment's source links the card.
        with self.service.transaction():
            state = self.service._setting('work-source:assigned')
            state['native_links'].setdefault(self.item['id'], dict(updated_at=self.item['updated_at'], task_ids=[],
                                                                   decision_ids=[]))['task_ids'].append(identity)
            self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('work-source:assigned', json.dumps(state)))
        Path(card['workspace_path']).mkdir(parents=True, exist_ok=True)
        return owner, requester, identity, card['workspace_path']

    def reconcile(self, owner):
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        return self.service._get('tasks', owner['id'])

    def test_the_real_pass_archives_with_its_note_and_the_owner_continues_explicitly(self):
        owner, requester, archived, workspace = self.blocked_outcome()
        # The dispatcher's orphan reconciliation writes a comment without a commented event.
        self.native_program('import time\nwith board.kb.write_txn(board.conn):\n'
                            "    board.kb._insert_comment(board.conn, sys.argv[2], 'dispatcher', 'Reclaimed.', int(time.time()) - 5)",
                            archived)
        time.sleep(1.1)
        self.items = []
        with patch('coconut.work_sources.STALE_IDLE_AGE', 0):
            self.audit()
        cancelled = self.reconcile(owner)
        self.assertEqual(cancelled['status'], 'cancelled', self.dispatch_state(owner))
        card = self.completion(archived)
        # The fixture adapter of the admission tests has the same fields.
        self.assertEqual(set(card), COMPLETION_FIELDS)
        self.assertEqual(set(card['archive']), ARCHIVE_FIELDS)
        self.assertEqual((card['status'], card['input_id'], card['archive']['author'], card['archive']['note'],
                          card['archive']['run_id'], card['archive']['later']),
                         ('archived', TURN, 'coconut', STALE_NOTE, None, []))
        self.assertIsInstance(card['archive']['at'], int)
        self.assertEqual([(r['outcome'], r['ended']) for r in card['runs']], [('blocked', True)])
        # A later comment, such as a recovery checkpoint, stays after the note.
        self.native_program("board.kb.add_comment(board.conn, sys.argv[2], 'default', 'Recovery checkpoint')", archived)
        self.assertEqual(self.completion(archived)['archive']['later'], ['commented'])
        history = self.native(archived)
        requester = self.update(requester['id'], status='working')
        with self.assertRaisesRegex(ServiceError, 'continue_closed_source: true'):
            self.service.emit(requester['id'], 'work_continue', dict(task_id=archived, message='Verify the merge.'))
        self.assertEqual(self.service._get('tasks', owner['id']), cancelled)
        receipt = self.service.emit(requester['id'], 'work_continue', dict(
            task_id=archived, message='Verify the merge.', continue_closed_source=True))
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=archived, status='queued'))
        continued = self.service._get('tasks', owner['id'])
        task = self.owner_turn(owner, 1)
        successor = task['_native_outcome_id']
        self.assertNotEqual(successor, archived)
        native = self.native(successor)
        self.assertEqual(native['parents'], [archived])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'])
        self.assertEqual((native['tenant'], native['path']), (owner['bot_id'], workspace))
        self.assertEqual(self.native(archived), history)
        self.assertEqual((task['_native_turn_id'], task['_kanban_workspace']), (continued['_native_turn_id'], workspace))
        self.assertEqual(self.service._get('tasks', owner['id'])['_reply_to_task_id'], requester['id'])

    def test_a_person_archive_has_no_note_and_stays_final(self):
        owner, requester, archived, workspace = self.blocked_outcome()
        self.native_program('assert board.kb.archive_task(board.conn, sys.argv[2])', archived)
        cancelled = self.reconcile(owner)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIsNone(self.completion(archived)['archive'])
        requester = self.update(requester['id'], status='working')
        with self.assertRaisesRegex(ServiceError, 'applies only to work that Coconut ended'):
            self.service.emit(requester['id'], 'work_continue', dict(
                task_id=archived, message='Verify the merge.', continue_closed_source=True))
        self.assertEqual(self.service._get('tasks', owner['id']), cancelled)
        # A person's comment after the archive is that person's, so the archive still stays final.
        self.native_program("board.kb.add_comment(board.conn, sys.argv[2], 'dashboard', 'Looked at it.')", archived)
        self.assertEqual(self.completion(archived)['archive']['author'], 'dashboard')
        with self.assertRaisesRegex(ServiceError, 'applies only to work that Coconut ended'):
            self.service.emit(requester['id'], 'work_continue', dict(
                task_id=archived, message='Verify the merge again.', continue_closed_source=True))


if __name__ == '__main__':
    unittest.main()
