"""A waiting routine occurrence that ended itself natively releases its fire claim, on proof and on nothing else.

The saved shape is the one of a real incident. The occurrence called kanban_complete on its own card. Coconut then
bound the retained Work to a successor card, a later turn overwrote the claim of the task with the successor, and the
task now waits on a delegated task. The occurrence keeps heartbeating its fire claim and blocks its routine.
"""
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from coconut import native_routine_receipts as receipts, native_routines
from coconut.native_bridge import NativeBridge
from coconut.providers import FixtureProvider
from coconut.service import Service
import test_failed_routine_continuation as native_fixtures

SUMMARY = 'Completed bounded fast intake. Retained the existing owner and recorded an active dependency.'


class Kanban:
    """The native cards by id, as the adapter's completion operation reads them."""
    def __init__(self, cards):
        self.cards, self.calls, self.tasks = cards, [], {}

    def close(self):
        pass

    def call(self, request, spawn=None):
        self.calls.append(request)
        if request['op'] == 'completion':
            return {'card': copy.deepcopy(self.cards.get(request['id']))}
        if request['op'] == 'snapshot':
            return {'tasks': []}
        raise AssertionError(request)


class WaitingOccurrenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(self.tmp.name, FixtureProvider(self.tmp.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        self.native = native_fixtures.FailedClaimTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.calls, self.routine, self.view = [], None, None
        self.service.provider.native_settings = self.native_settings
        self.routine_id = str(uuid5(UUID(self.bot['id']), 'cron:job'))

    # The native side: the real completion receipts and finish of the provider, over a fake Hermes job.
    def native_settings(self, operation, bots, **arguments):
        self.calls.append((operation, arguments))
        homes = {self.bot['id']: self.native.home}
        if operation == 'routines_read':
            # The view carries the live job, as the provider's own read does. A test can freeze an older view of it.
            routine = copy.deepcopy(self.routine or dict(id=self.routine_id, bot_id=self.bot['id'], name='Fast intake',
                                                         enabled=False, _native_id='job', next_run_at=None))
            routine['_job'] = copy.deepcopy(self.native.job if self.view is None else self.view)
            return {'routines': [routine],
                    'completions': {self.bot['id']: receipts.completion_receipts(self.native.home)}}
        if operation == 'routine_heartbeat':
            # The claim of the next occurrence is Hermes' to keep. Only the fake job tracks the first owner.
            return {'ok': arguments['owner'] == 'next-owner' or (self.native.job.get('fire_claim') or {}).get('by') == arguments['owner']}
        if operation == 'routine_claim':
            return {'job': dict(self.native.job, fire_claim={'by': 'next-owner'})}
        return native_routines.worker(operation, dict(arguments, bot_id=self.bot['id']), homes)

    def event_count(self, kind):
        return self.service.db.execute('SELECT count(*) FROM events WHERE type=?', (kind,)).fetchone()[0]

    def operations(self, *names):
        return [name for name, _ in self.calls if name in (names or ('routine_finish', 'routine_heartbeat'))]

    def tick(self):
        self.service._native_cron_next = 0
        self.service.tick_native_routines()
        self.assertFalse(self.service._native_cron_error)

    def claim(self, native_id, run, turn):
        return dict(native_id=native_id, external_id=self.task_id, run_id=run, input=turn, context='')

    def card(self, identity, key=None, run=5332, **changes):
        return dict(id=identity, tenant=self.bot['id'], idempotency_key=key or 'coconut:' + self.task_id, status='done',
                    current_run_id=None, worker_pid=None, input_id=None,
                    runs=[dict(id=run, status='done', outcome='completed', ended=True)]) | changes

    def update(self, **fields):
        with self.service.transaction():
            task = self.service._get('tasks', self.task_id)
            task.update(fields)
            self.service._put('tasks', task)
        return task

    def receipt(self, name, arguments, result):
        """Save a tool receipt through the service's own call and result path."""
        service = self.service
        saved = service._get('tasks', self.task_id)
        self.update(status='working')
        call = service.emit(self.task_id, 'native_call', dict(name=name, arguments=arguments))
        service.emit(self.task_id, 'native_result', dict(id=call['id'], result=json.dumps(result)))
        self.update(status=saved['status'])
        return call['id']

    def build(self, capture=True, status='waiting', **fields):
        """The incident. Turn one ended card t_orig by run 5332. Turn two completed the successor card t_succ."""
        service = self.service
        task = service.mutate('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Fast intake', 'mode': 'queue'}, str(uuid4()))
        self.task_id = task['id']
        self.update(status='working', routine_id=self.routine_id, _native_job=copy.deepcopy(self.native.job),
                    _native_turn_id='turn-original', _kanban_claim=self.claim('t_orig', 5332, 'turn-original'))
        service._event('routine.triggered', dict(routine_id=self.routine_id, task_id=self.task_id), self.bot['id'], self.task_id)
        self.original = self.receipt('kanban_complete', dict(task_id='t_orig', summary=SUMMARY),
                                     dict(ok=True, task_id='t_orig', run_id=5332))
        # Retained Work moved the binding to the successor, and the delegated task is awaited.
        self.update(status='working', _native_outcome_id='t_succ', _work_context_id='work', _waiting_on=['dev-task'],
                    _native_turn_id='turn-successor', _kanban_claim=self.claim('t_succ', 5380, 'turn-successor'))
        self.successor = self.receipt('kanban_complete', dict(task_id='t_succ', summary='Verified the dependency.'),
                                      dict(ok=True, task_id='t_succ', run_id=5380))
        task = self.update(status=status, summary='Waiting for a result.', **fields)
        if not capture:
            task.pop('_native_occurrence', None)
            self.update()
            with service.transaction():
                task = service._get('tasks', self.task_id)
                task.pop('_native_occurrence', None)
                service._put('tasks', task)
        self.service.kanban = Kanban({'t_orig': self.card('t_orig'),
                                      't_succ': self.card('t_succ', key='coconut-outcome:work', run=5380, status='running',
                                                          current_run_id=5380, runs=[dict(id=5380, status='running', outcome=None, ended=False)])})
        return service._get('tasks', self.task_id)

    def stored(self):
        return self.service._get('tasks', self.task_id)

    def assert_released(self):
        self.assertEqual(self.operations(), ['routine_finish'])
        arguments = next(a for n, a in self.calls if n == 'routine_finish')
        self.assertTrue(arguments['success'])
        self.assertIsNone(arguments['failure'])
        self.assertTrue(self.stored()['_native_job_finished'])
        self.assertIsNone(self.native.job['fire_claim'])
        self.assertEqual(self.native.job['_coconut_completion'], dict(job_id='job', owner='owner', success=True))
        self.assertEqual(receipts.read(self.native.home, receipts.completion_id('job', 'owner')),
                         dict(job_id='job', owner='owner', success=True))

    def assert_held(self):
        self.assertEqual(self.operations(), ['routine_heartbeat'])
        self.assertFalse(self.stored().get('_native_job_finished'))
        self.assertEqual(self.native.job['fire_claim'], {'by': 'owner'})
        self.assertNotIn('_native_release', self.stored())

    # The saved shape.
    def test_return_held_by_paused_sibling_blocks_release_until_dispatched(self):
        owner = self.build()
        service = self.service
        helper = service.mutate('POST', '/v1/bots', {'name': 'Helper', 'role': ''}, str(uuid4()))
        source = service.mutate('POST', f"/v1/bots/{helper['id']}/messages", {'text': 'Result'}, str(uuid4()))
        source = service._get('tasks', source['id'])
        source.update(status='completed', source_bot_id=self.bot['id'], _reply_to_task_id=self.task_id,
                      conversation_bot_id=self.bot['id'], thread_id=owner['thread_id'], exchange_id=owner['exchange_id'])
        service._put('tasks', source)
        sibling = dict(owner, id=str(uuid4()), status='paused', _assignment_task_id=self.task_id)
        sibling.pop('_native_job')
        sibling.pop('routine_id')
        service._put('tasks', sibling)
        service.kanban.tasks[self.task_id] = dict(id='t_succ', status='scheduled')
        service._store_bot_message(source['id'], dict(target_bot_id=self.bot['id'], text='New helper result'), returning=True)
        self.assertEqual(self.stored()['status'], 'waiting')
        self.tick()
        self.assert_held()
        self.assertTrue(self.stored()['_routine_pending_bot_result'])
        service._put('tasks', dict(sibling, status='completed'))
        self.update(status='queued', _native_turn_id='result-turn')
        seen = []
        def start(task, bot, history, emit):
            seen.extend(task['_bot_messages'])
            self.assertNotIn('_routine_pending_bot_result', self.stored())
        with patch.object(service.provider, 'start_task', start):
            service._run_task(self.task_id)
        self.assertEqual(seen[-1]['text'], 'New helper result')
        self.assertNotIn('_routine_pending_bot_result', self.stored())

    def test_historical_bot_messages_do_not_keep_completed_intake_alive(self):
        self.build(_bot_messages=[dict(text='Already processed result', sender_bot_id=self.bot['id'])])
        self.tick()
        self.assert_released()

    def test_occurrence_guards_use_indexed_queries_without_materializing_tables(self):
        task = self.build()
        queries = []
        self.service.db.set_trace_callback(queries.append)
        with patch.object(self.service, '_all', side_effect=AssertionError('full table read')):
            self.assertTrue(self.service._occurrence_candidates(task))
        self.service.db.set_trace_callback(None)
        for table in ('contexts', 'decisions'):
            query = next(q for q in queries if q.startswith('SELECT 1 FROM ' + table))
            plan = ' '.join(str(row) for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + query))
            self.assertIn('USING INDEX', plan)
            self.assertNotIn('SCAN ' + table, plan)

    def test_overwritten_claim_of_a_waiting_occurrence_is_released_by_the_native_finish(self):
        task = self.build(capture=False)
        self.assertEqual(task['_kanban_claim']['native_id'], 't_succ')
        self.assertEqual(task['_native_outcome_id'], 't_succ')
        self.assertNotIn('_native_occurrence', task)
        self.assertEqual(self.service._active_routine_task(self.routine_id)['id'], self.task_id)
        self.tick()
        self.assert_released()
        # Nothing else about the occurrence or its retained Work changes. The product outcome is not completed.
        after = self.stored()
        for field in ('status', 'summary', '_waiting_on', '_native_outcome_id', '_work_context_id',
                      '_kanban_claim', '_native_turn_id', '_return_pending'):
            self.assertEqual(after.get(field), task.get(field), field)
        self.assertEqual(self.service.kanban.cards['t_succ']['status'], 'running')
        # Native state is read by id for both cards that the task reported done. Only the original proves it.
        self.assertEqual(sorted(c['id'] for c in self.service.kanban.calls if c['op'] == 'completion'), ['t_orig', 't_succ'])
        self.assertIsNone(self.service._active_routine_task(self.routine_id))
        # The decision to release does not depend on how long ago or what narrative the task has.
        self.assertEqual(after['_native_release'], dict(native_id='t_orig', run_id=5332, turn_id='turn-original',
                         receipt_id=self.original, job_id='job', owner='owner'))

    def test_release_is_idempotent_and_the_gateway_fires_the_next_occurrence(self):
        self.build(capture=False)
        self.routine = dict(id=self.routine_id, bot_id=self.bot['id'], name='Fast intake', enabled=True, _native_id='job',
                            next_run_at='2000-01-01T00:00:00Z')
        self.tick()
        self.assert_released()
        # The Hermes gateway fires the next occurrence, so no pass of Coconut claims it.
        self.tick()
        self.tick()
        self.assertEqual(self.operations('routine_claim'), [])
        # The old task is neither finished nor heartbeated again.
        self.assertEqual(self.operations(), ['routine_finish'])
        self.assertEqual(len(self.service._all('tasks')), 1)
        self.assertEqual(self.event_count('routine.reconciliation_required'), 0)

    def test_released_occurrence_continues_with_its_original_provenance(self):
        self.build(capture=False)
        self.tick()
        self.tick()
        task = self.stored()
        # Hermes holds the proof. The next refresh saves it with the task.
        self.assertEqual(task['_native_job_completion_receipt'], dict(job_id='job', owner='owner', success=True))
        proof = self.service.native_routine_completion_input(task)
        self.assertEqual(proof, dict(job_id='job', owner='owner', success=True, native_confirmed=True))
        bridge = NativeBridge({'task': dict(task, _native_job_completion=proof)}, None, None)
        self.assertEqual(self.native.run_tool(bridge), 'executed')
        # An unrelated owner never gets this provenance.
        with self.assertRaises(BaseException):
            self.native.run_tool(self.native.bridge(owner='another-owner'))
        self.assertEqual(self.operations(), ['routine_finish', 'routine_heartbeat'][:1])

    def test_capture_at_completion_keeps_the_original_card_when_the_successor_completes(self):
        task = self.build(capture=True)
        self.assertEqual(task['_native_occurrence'], dict(native_id='t_orig', run_id=5332, turn_id='turn-original',
                                                          receipt_id=self.original, input='turn-original'))
        self.assertEqual(task['_kanban_claim']['native_id'], 't_succ')
        self.tick()
        self.assert_released()

    def test_capture_requires_the_exact_claim_and_result(self):
        for case in ('failed', 'other card', 'other run', 'other task claim', 'other outcome', 'finished'):
            with self.subTest(case=case):
                service = self.service
                task = service.mutate('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': case, 'mode': 'queue'}, str(uuid4()))
                self.task_id = task['id']
                claim = self.claim('t_orig', 5332, 'turn-original')
                fields = dict(status='working', routine_id=self.routine_id, _native_job=copy.deepcopy(self.native.job),
                              _native_turn_id='turn-original', _kanban_claim=claim)
                result = dict(ok=True, task_id='t_orig', run_id=5332)
                if case == 'failed': result['ok'] = False
                if case == 'other card': result['task_id'] = 't_other'
                if case == 'other run': result['run_id'] = 1
                if case == 'other task claim': fields['_kanban_claim'] = dict(claim, external_id='another-task')
                if case == 'other outcome': fields['_native_outcome_id'] = 't_other'
                if case == 'finished': fields['_native_job_finished'] = True
                self.update(**fields)
                self.receipt('kanban_complete', dict(task_id='t_orig'), result)
                self.assertNotIn('_native_occurrence', self.stored())

    def test_capture_ignores_a_task_that_is_not_a_routine_occurrence(self):
        task = self.service.mutate('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Plain', 'mode': 'queue'}, str(uuid4()))
        self.task_id = task['id']
        self.update(status='working', _native_turn_id='turn', _kanban_claim=self.claim('t_orig', 5332, 'turn'))
        self.receipt('kanban_complete', dict(task_id='t_orig'), dict(ok=True, task_id='t_orig', run_id=5332))
        self.assertNotIn('_native_occurrence', self.stored())

    def test_a_saved_capture_must_match_the_durable_receipt(self):
        for case in ('receipt', 'turn', 'card', 'run'):
            with self.subTest(case=case):
                self.setUp()
                task = self.build(capture=True)
                saved = dict(task['_native_occurrence'])
                if case == 'receipt': saved['receipt_id'] = self.successor
                if case == 'turn': saved['turn_id'] = 'turn-successor'
                if case == 'card': saved['native_id'] = 't_succ'
                if case == 'run': saved['run_id'] = 5380
                self.update(_native_occurrence=saved)
                self.tick()
                self.assert_held()

    # Fail closed.
    def break_receipt(self, change):
        record = self.service._get('native_receipts', self.original)
        change(record)
        with self.service.transaction():
            self.service._put('native_receipts', record)

    def assert_unproven(self, mutate, capture=False, native=True):
        self.build(capture=capture)
        mutate()
        self.tick()
        self.assert_held()

    def test_missing_or_mismatched_durable_receipt_keeps_the_claim(self):
        def result(**changes):
            return lambda record: record.update(result=json.dumps(dict(ok=True, task_id='t_orig', run_id=5332) | changes))
        cases = {
            'missing': lambda: self.service.db.execute('DELETE FROM native_receipts WHERE id=?', (self.original,)),
            'not ok': lambda: self.break_receipt(result(ok=False)),
            'ok is truthy only': lambda: self.break_receipt(result(ok='yes')),
            'other card': lambda: self.break_receipt(result(task_id='t_succ')),
            'other run': lambda: self.break_receipt(result(run_id=1)),
            'run is text': lambda: self.break_receipt(result(run_id='5332')),
            'run missing': lambda: self.break_receipt(lambda r: r.update(result=json.dumps(dict(ok=True, task_id='t_orig')))),
            'unparseable': lambda: self.break_receipt(lambda r: r.update(result='done')),
            'other task': lambda: self.break_receipt(lambda r: r.update(task_id=str(uuid4()))),
            'other action': lambda: self.break_receipt(lambda r: r['action'].update(name='kanban_comment')),
            'no turn': lambda: self.break_receipt(lambda r: r.pop('turn_id')),
            'not completed': lambda: self.break_receipt(lambda r: r.update(state='executing')),
            'neither executing nor completed': lambda: self.break_receipt(lambda r: r.update(state='rejected')),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self.setUp()
                self.assert_unproven(mutate)

    def test_a_card_that_two_turns_claim_to_have_ended_is_ambiguous(self):
        def duplicate():
            record = dict(self.service._get('native_receipts', self.original), id=str(uuid4()), turn_id='another-turn')
            with self.service.transaction():
                self.service._put('native_receipts', record)
        self.assert_unproven(duplicate)

    def test_two_cards_that_both_prove_completion_are_ambiguous(self):
        def both():
            self.service.kanban.cards['t_succ'] = self.card('t_succ', run=5380)
        self.assert_unproven(both)

    def test_only_the_successor_receipt_proves_nothing_about_the_original(self):
        # The task completed its successor card, but Coconut has no receipt of the original card ending.
        self.assert_unproven(lambda: self.service.db.execute('DELETE FROM native_receipts WHERE id=?', (self.original,)))
        self.assertEqual([c['id'] for c in self.service.kanban.calls if c['op'] == 'completion'], ['t_succ'])

    def test_native_card_must_be_the_exact_finished_card_of_this_task(self):
        def card(**changes):
            return lambda: self.service.kanban.cards['t_orig'].update(changes)
        run = dict(id=5332, status='done', outcome='completed', ended=True)
        cases = {
            'absent': lambda: self.service.kanban.cards.pop('t_orig'),
            'other tenant': card(tenant=str(uuid4())),
            'created for another task': card(idempotency_key='coconut:' + str(uuid4())),
            'follow-up card': card(idempotency_key='coconut-outcome:work'),
            'no key': card(idempotency_key=None),
            'ready': card(status='ready'),
            'archived': card(status='archived'),
            'running': card(status='running'),
            'current run': card(current_run_id=5333),
            'worker': card(worker_pid=42),
            'input of another turn': card(input_id='turn-successor'),
            'run of another id': card(runs=[dict(run, id=5331)]),
            'run not completed': card(runs=[dict(run, outcome='crashed')]),
            'no run': card(runs=[]),
            'runs unreadable': card(runs=None),
            'second completed run': card(runs=[run, dict(run, id=5333)]),
            'open run': card(runs=[run, dict(run, id=5334, outcome=None, ended=False)]),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self.setUp()
                self.assert_unproven(mutate)

    def test_card_input_that_matches_the_receipt_turn_is_accepted(self):
        self.build(capture=False)
        self.service.kanban.cards['t_orig']['input_id'] = 'turn-original'
        self.tick()
        self.assert_released()

    def test_an_adapter_that_cannot_answer_confirms_nothing(self):
        def raises():
            def call(request, spawn=None): raise RuntimeError('The Hermes Kanban adapter stopped.')
            self.service.kanban.call = call
        self.assert_unproven(raises)
        self.setUp()
        self.assert_unproven(lambda: setattr(self.service.kanban, 'call', lambda request, spawn=None: {'error': 'old adapter'}))
        self.setUp()
        self.assert_unproven(lambda: setattr(self.service, 'kanban', None))

    def test_routine_history_must_be_one_clean_trigger(self):
        cases = {
            'no trigger': lambda: self.service.db.execute("DELETE FROM events WHERE type='routine.triggered'"),
            'second trigger': lambda: self.service._event('routine.triggered', dict(routine_id=self.routine_id, task_id=self.task_id), self.bot['id'], self.task_id),
            'trigger of another routine': lambda: self.service._event('routine.triggered', dict(routine_id=str(uuid4()), task_id=self.task_id), self.bot['id'], self.task_id),
            'failed after trigger': lambda: self.service._event('task.updated', dict(id=self.task_id, status='failed'), self.bot['id'], self.task_id),
            'cancelled after trigger': lambda: self.service._event('task.updated', dict(id=self.task_id, status='cancelled'), self.bot['id'], self.task_id),
            'another routine': lambda: self.update(routine_id=str(uuid4())),
            'another job': lambda: self.update(_native_job=dict(self.native.job, id='other')),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self.setUp()
                self.assert_unproven(mutate)

    # The active occurrence, tool, unknown receipt and decision protections stay.
    def test_active_work_and_open_input_keep_the_claim(self):
        def worker():
            thread = threading.Thread(target=threading.Event().wait, args=(5,), daemon=True)
            thread.start()
            self.service.running[self.task_id] = thread
        def context(status):
            return lambda: self.service._put('contexts', dict(id=str(uuid4()), task_id=self.task_id, bot_id=self.bot['id'], status=status))
        cases = {
            'live worker': worker,
            'unknown tool result': lambda: self.break_receipt_copy('executing'),
            'pending decision': lambda: self.service._put('decisions', dict(id='d', task_id=self.task_id, status='pending')),
            'queued context': context('queued'),
            'working context': context('working'),
            'unknown tools saved': lambda: self.update(_self_recovery=dict(unknown_tools=[dict(id='u')])),
            'assignment': lambda: self.update(_assignment_task_id=str(uuid4())),
            'paused by an archived bot': lambda: self.service._put('bots', dict(self.service._get('bots', self.bot['id']), archived=True)),
        }
        for field in ('_steering', '_live_messages', '_blocked_followup', '_native_reply_pending', '_return_pending',
                      '_resume_decision', '_background_completion', '_pr_review', '_work_resume'):
            cases['open ' + field] = lambda field=field: self.update(**{field: [dict(text='next')] if field.endswith('s') else dict(text='next')})
        for name, mutate in cases.items():
            with self.subTest(name):
                self.setUp()
                self.assert_unproven(mutate)
        self.setUp()
        self.build()
        with patch.object(self.service, '_background_pending', return_value=True):
            self.tick()
        self.assert_held()

    def break_receipt_copy(self, state):
        record = dict(self.service._get('native_receipts', self.original), id=str(uuid4()), state=state,
                      action=dict(name='terminal', arguments=dict(command='git push')))
        record.pop('result', None)
        with self.service.transaction():
            self.service._put('native_receipts', record)

    def test_no_other_status_is_released_or_even_proven(self):
        for status in ('queued', 'working', 'needs_you', 'paused', 'scheduled'):
            with self.subTest(status=status):
                self.setUp()
                self.build(status=status)
                self.service.kanban.calls.clear()
                self.tick()
                self.assert_held()
                self.assertEqual(self.service.kanban.calls, [])

    def test_a_completed_or_failed_occurrence_still_finishes_by_its_status(self):
        for status, success in (('completed', True), ('failed', False)):
            with self.subTest(status=status):
                self.setUp()
                self.build(status=status)
                self.update(summary='The turn ended.')
                self.tick()
                arguments = next(a for n, a in self.calls if n == 'routine_finish')
                self.assertEqual(arguments['success'], success)
                self.assertEqual(self.service.kanban.calls, [])

    def test_time_narrative_and_merge_neither_prove_nor_block_a_release(self):
        # Old, plausible and merged is not a completion.
        self.build()
        self.service.db.execute('DELETE FROM native_receipts WHERE id=?', (self.original,))
        self.update(created_at='2020-01-01T00:00:00Z', updated_at='2020-01-01T00:00:00Z',
                    summary='Completed bounded fast intake. The pull request merged.')
        self.service._put('artifacts', dict(id='pr', kind='pr', task_id=self.task_id, bot_id=self.bot['id'], title='PR',
                                            status='merged', pr_state='merged', url='https://github.com/example/project/pull/1',
                                            created_at='2020-01-01T00:00:00Z'))
        self.tick()
        self.assert_held()
        # A proven completion is released whether recent, unmerged or silent.
        self.setUp()
        self.build()
        self.update(created_at='2999-01-01T00:00:00Z', updated_at='2999-01-01T00:00:00Z', summary='Nothing to report.')
        self.service._put('artifacts', dict(id='pr', kind='pr', task_id=self.task_id, bot_id=self.bot['id'], title='PR',
                                            status='open', pr_state='open', url='https://github.com/example/project/pull/1',
                                            created_at='2999-01-01T00:00:00Z'))
        self.tick()
        self.assert_released()

    # The ordered races.
    def test_failed_native_finish_keeps_the_task_active_and_is_retried(self):
        self.build()
        original = self.native_settings
        results = [{'ok': False}]
        def settings(operation, bots, **arguments):
            if operation == 'routine_finish' and results:
                self.calls.append((operation, arguments))
                return results.pop()
            return original(operation, bots, **arguments)
        self.service.provider.native_settings = settings
        self.tick()
        task = self.stored()
        self.assertFalse(task.get('_native_job_finished'))
        self.assertTrue(task['_native_finish_unconfirmed'])
        self.assertEqual(task['status'], 'waiting')
        self.assertEqual(self.event_count('routine.reconciliation_required'), 1)
        self.tick()
        self.assertTrue(self.stored()['_native_job_finished'])
        self.assertEqual(self.operations(), ['routine_finish', 'routine_finish'])
        self.assertEqual(self.event_count('routine.reconciliation_required'), 1)

    def test_a_continuation_that_starts_during_the_native_finish_is_not_cancelled_and_keeps_its_tools(self):
        service = self.service
        self.build()
        original = self.native_settings
        started, begun, end = [], threading.Event(), threading.Event()
        def start(task, bot, history, emit):
            started.append(copy.deepcopy(task))
            begun.set()
            self.assertTrue(end.wait(5))
        threads = []
        def settings(operation, bots, **arguments):
            if operation == 'routine_finish':
                intent = service._get('tasks', self.task_id)
                # The proof is saved before Hermes is asked, and the release is not yet confirmed.
                self.assertEqual(intent['_native_release']['native_id'], 't_orig')
                self.assertFalse(intent.get('_native_job_finished'))
                # A continuation queues and its turn starts before Hermes answers.
                self.update(status='queued', _native_turn_id='turn-continuation',
                            _kanban_claim=self.claim('t_succ', 5381, 'turn-continuation'))
                with patch.object(service.provider, 'start_task', start):
                    threads.append(threading.Thread(target=service._run_task, args=(self.task_id,)))
                    threads[0].start()
                    self.assertTrue(begun.wait(5))
                self.assertEqual(service._get('tasks', self.task_id)['status'], 'working')
            return original(operation, bots, **arguments)
        service.provider.native_settings = settings
        try:
            self.tick()
            # Hermes released the claim, and the running turn is neither cancelled nor failed.
            self.assert_released()
            task = self.stored()
            self.assertEqual(task['status'], 'working')
            self.assertEqual(task['_native_turn_id'], 'turn-continuation')
            self.assertNotEqual(task.get('summary'), 'The routine lost its native execution claim.')
            self.assertEqual(self.event_count('routine.reconciliation_required'), 0)
            # The turn started before the commit and still treats the claim as released. Its tools run once Hermes confirms.
            self.assertEqual(len(started), 1)
            self.assertTrue(started[0]['_native_job_finished'])
            self.assertEqual(self.native.run_tool(NativeBridge({'task': started[0]}, None, None)), 'executed')
            # The next passes neither finish nor heartbeat it, and do not cancel it for a lost claim.
            self.tick()
            self.tick()
            self.assertEqual(self.operations(), ['routine_finish'])
            self.assertEqual(self.stored()['status'], 'working')
        finally:
            end.set()
            for thread in threads:
                thread.join(5)

    def test_a_continuation_that_queues_before_the_commit_is_recorded_as_released(self):
        service = self.service
        self.build()
        original = self.native_settings
        def settings(operation, bots, **arguments):
            result = original(operation, bots, **arguments)
            if operation == 'routine_finish':
                self.update(status='queued', _native_turn_id='turn-continuation')
            return result
        service.provider.native_settings = settings
        self.tick()
        self.assert_released()
        self.assertEqual(self.stored()['status'], 'queued')
        self.tick()
        self.assertEqual(self.operations(), ['routine_finish'])
        self.assertEqual(self.stored()['status'], 'queued')
        # The queued continuation may start. Its provider task already carries the finished flag.
        started = []
        with patch.object(service.provider, 'start_task', lambda task, bot, history, emit: started.append(task)):
            service._run_task(self.task_id)
        self.assertTrue(started[0]['_native_job_finished'])
        self.assertEqual(self.native.run_tool(NativeBridge({'task': started[0]}, None, None)), 'executed')

    def test_a_continuation_does_not_release_on_a_stale_proof(self):
        # The proof was read while waiting. A continuation then took the task before the lock was held again.
        service = self.service
        self.build()
        original = service._authenticate_occurrence
        def authenticate(task, candidates):
            proof = original(task, candidates)
            self.update(status='queued')
            return proof
        with patch.object(service, '_authenticate_occurrence', authenticate):
            self.tick()
        self.assert_held_unfinished()

    def assert_held_unfinished(self):
        self.assertEqual(self.operations('routine_finish'), [])
        self.assertFalse(self.stored().get('_native_job_finished'))
        self.assertNotIn('_native_release', self.stored())
        self.assertEqual(self.native.job['fire_claim'], {'by': 'owner'})

    def test_a_receipt_that_turns_unknown_before_the_lock_is_held_again_keeps_the_claim(self):
        service = self.service
        self.build()
        original = service._authenticate_occurrence
        def authenticate(task, candidates):
            proof = original(task, candidates)
            self.break_receipt_copy('executing')
            return proof
        with patch.object(service, '_authenticate_occurrence', authenticate):
            self.tick()
        self.assert_held_unfinished()

    def test_a_restart_between_the_saved_intent_and_the_native_finish_repeats_the_release(self):
        service = self.service
        self.build()
        original = self.native_settings
        def crash(operation, bots, **arguments):
            if operation == 'routine_finish':
                raise OSError('Interrupted before Hermes answered.')
            return original(operation, bots, **arguments)
        service.provider.native_settings = crash
        service._native_cron_next = 0
        service.tick_native_routines()
        self.assertTrue(service._native_cron_error)
        self.assertIn('_native_release', self.stored())
        self.assertFalse(self.stored().get('_native_job_finished'))
        service.provider.native_settings = self.native_settings
        self.tick()
        self.assert_released()

    # An intent that was saved for an attempt that did not end in a release.
    def test_a_finish_that_hermes_does_not_confirm_withdraws_the_intent(self):
        self.build()
        original = self.native_settings
        def refuse(operation, bots, **arguments):
            if operation == 'routine_finish':
                self.calls.append((operation, arguments))
                return {'ok': False}
            return original(operation, bots, **arguments)
        self.service.provider.native_settings = refuse
        self.tick()
        self.assertNotIn('_native_release', self.stored())
        self.assertFalse(self.stored().get('_native_job_finished'))
        # A turn that starts now holds no claim of a release, as Hermes still holds the fire claim.
        started = []
        self.update(status='queued')
        with patch.object(self.service.provider, 'start_task', lambda task, bot, history, emit: started.append(task)):
            self.service._run_task(self.task_id)
        self.assertFalse(started[0]['_native_job_finished'])

    def test_an_intent_whose_proof_no_longer_holds_is_cleared_and_not_consumed(self):
        service = self.service
        self.build()
        def crash(operation, bots, **arguments):
            if operation == 'routine_finish':
                raise OSError('Interrupted before Hermes answered.')
            return self.native_settings(operation, bots, **arguments)
        service.provider.native_settings = crash
        service._native_cron_next = 0
        service.tick_native_routines()
        self.assertIn('_native_release', self.stored())
        # The occurrence took new work before the retry, so it is no longer a completed one.
        service.provider.native_settings = self.native_settings
        self.update(status='working', _waiting_on=[])
        self.calls.clear()
        self.tick()
        self.assertNotIn('_native_release', self.stored())
        self.assertEqual(self.operations(), ['routine_heartbeat'])
        self.assertFalse(self.stored().get('_native_job_finished'))
        # Its later turns do not start as released.
        started = []
        self.update(status='queued')
        with patch.object(service.provider, 'start_task', lambda task, bot, history, emit: started.append(task)):
            service._run_task(self.task_id)
        self.assertFalse(started[0]['_native_job_finished'])
        # Once it waits and proves again, the release is saved again and completes.
        self.update(status='waiting')
        self.calls.clear()
        self.tick()
        self.assert_released()
        self.assertIn('_native_release', self.stored())

    # The answer of the native finish is lost, or Hermes never saw it. Native state decides, not the task.
    def lose_the_answer(self, released=True):
        original = self.native_settings
        def crash(operation, bots, **arguments):
            if operation == 'routine_finish':
                if released:
                    original(operation, bots, **arguments)
                raise OSError('The answer was lost.')
            return original(operation, bots, **arguments)
        self.service.provider.native_settings = crash
        self.service._native_cron_next = 0
        self.service.tick_native_routines()
        self.assertTrue(self.service._native_cron_error)
        self.service.provider.native_settings = self.native_settings
        self.calls.clear()

    def assert_reconciled_without_touching_the_task(self, status):
        task = self.stored()
        self.assertEqual(self.operations(), [])
        self.assertTrue(task['_native_job_finished'])
        self.assertFalse(task['_native_finish_unconfirmed'])
        self.assertIn('_native_release', task)
        self.assertEqual(task['status'], status)
        self.assertNotEqual(task.get('summary'), 'The routine lost its native execution claim.')
        self.assertEqual(self.event_count('routine.reconciliation_required'), 0)
        self.assertEqual(self.service.kanban.cards['t_succ']['status'], 'running')

    def test_a_release_that_hermes_confirmed_before_the_answer_was_lost_is_recorded_after_new_intake(self):
        self.build()
        self.lose_the_answer()
        self.assertIsNone(self.native.job['fire_claim'])
        self.assertFalse(self.stored().get('_native_job_finished'))
        # New intake makes the occurrence active again, so it is no longer a candidate for release.
        self.update(status='working', _waiting_on=[])
        self.tick()
        self.assert_reconciled_without_touching_the_task('working')
        self.tick()
        self.assertEqual(self.operations(), [])

    def test_a_confirmed_release_is_recorded_when_the_proof_cannot_be_read_again(self):
        self.build()
        self.lose_the_answer()
        self.service.kanban.call = lambda request, spawn=None: (_ for _ in ()).throw(OSError('The adapter is silent.'))
        self.tick()
        self.assert_reconciled_without_touching_the_task('waiting')

    def test_a_confirmed_release_is_found_in_the_native_job_when_its_receipt_is_missing(self):
        self.build()
        self.lose_the_answer()
        receipts.receipt_path(self.native.home, receipts.completion_id('job', 'owner')).unlink()
        self.assertEqual(self.native.job['_coconut_completion'], dict(job_id='job', owner='owner', success=True))
        self.update(status='working', _waiting_on=[])
        self.tick()
        self.assert_reconciled_without_touching_the_task('working')

    def test_a_confirmed_release_of_another_owner_confirms_nothing(self):
        self.build()
        self.lose_the_answer()
        # Another occurrence now holds the job and its own record. Ours has no receipt and no job record left.
        receipts.receipt_path(self.native.home, receipts.completion_id('job', 'owner')).unlink()
        self.native.job.update(fire_claim={'by': 'next-owner'}, _coconut_completion=dict(job_id='job', owner='next-owner', success=True))
        self.tick()
        task = self.stored()
        self.assertEqual(self.operations(), [])
        self.assertTrue(task['_native_job_finished'])
        self.assertTrue(task['_native_finish_unconfirmed'])
        self.assertEqual(self.event_count('routine.reconciliation_required'), 1)
        self.assertEqual(task['status'], 'waiting')

    def test_a_confirmed_release_of_another_owner_does_not_spare_an_occurrence_that_took_new_intake(self):
        self.build()
        self.lose_the_answer()
        receipts.receipt_path(self.native.home, receipts.completion_id('job', 'owner')).unlink()
        self.native.job.update(fire_claim={'by': 'next-owner'}, _coconut_completion=dict(job_id='job', owner='next-owner', success=True))
        self.update(status='working', _waiting_on=[])
        self.tick()
        self.assert_cancelled_as_lost()

    def test_an_interrupted_finish_that_did_not_reach_hermes_is_not_a_release(self):
        self.build()
        self.lose_the_answer(released=False)
        self.assertEqual(self.native.job['fire_claim'], {'by': 'owner'})
        self.update(status='working', _waiting_on=[])
        self.tick()
        task = self.stored()
        self.assertEqual(self.operations(), ['routine_heartbeat'])
        self.assertFalse(task.get('_native_job_finished'))
        self.assertNotIn('_native_release', task)
        self.assertEqual(task['status'], 'working')

    # A claim that Hermes lost or gave to another owner cannot be released. The task is not what ends.
    def assert_lost_without_touching_the_task(self, status='waiting'):
        task = self.stored()
        self.assertTrue(task['_native_job_finished'])
        self.assertTrue(task['_native_finish_unconfirmed'])
        self.assertEqual(task['status'], status)
        self.assertNotEqual(task.get('summary'), 'The routine lost its native execution claim.')
        self.assertEqual(task['_waiting_on'], ['dev-task'])
        self.assertEqual(self.event_count('routine.reconciliation_required'), 1)
        self.assertEqual(self.service.kanban.cards['t_succ']['status'], 'running')
        self.assertNotIn('_coconut_completion', self.native.job)
        self.assertIsNone(self.service._active_routine_task(self.routine_id))

    def test_a_proven_occurrence_whose_claim_was_reowned_stops_reconciling_and_keeps_its_work(self):
        self.build()
        self.native.job['fire_claim'] = {'by': 'other-owner'}
        for _ in range(4):
            self.tick()
        self.assertEqual(self.operations(), ['routine_finish'])
        self.assert_lost_without_touching_the_task()

    def test_a_proven_occurrence_whose_claim_vanished_stops_reconciling_and_keeps_its_work(self):
        self.build()
        self.native.job['fire_claim'] = None
        for _ in range(4):
            self.tick()
        self.assertEqual(self.operations(), ['routine_finish'])
        self.assert_lost_without_touching_the_task()

    def test_a_lost_claim_is_not_a_reason_to_cancel_an_occurrence_that_tried_to_release(self):
        # The pass heartbeats because the proof cannot be read, and Hermes no longer knows the claim.
        self.build()
        self.service.kanban.call = lambda request, spawn=None: (_ for _ in ()).throw(OSError('The adapter is silent.'))
        self.update(_native_release_tried=self.tried())
        self.view = copy.deepcopy(self.native.job)
        self.native.job['fire_claim'] = None
        self.tick()
        self.assertEqual(self.operations(), ['routine_heartbeat'])
        self.assert_lost_without_touching_the_task()

    def tried(self, **changes):
        """The attempt record that a release saves with its intent: the authenticated card and the exact job and owner."""
        return dict(native_id='t_orig', run_id=5332, turn_id='turn-original', receipt_id=self.original,
                    job_id='job', owner='owner') | changes

    def assert_cancelled_as_lost(self):
        task = self.stored()
        self.assertEqual(self.operations(), ['routine_heartbeat'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['summary'], 'The routine lost its native execution claim.')
        self.assertTrue(task['_native_job_finished'])
        self.assertEqual(self.event_count('routine.reconciliation_required'), 0)

    def lose_claim(self):
        self.view = copy.deepcopy(self.native.job)
        self.native.job['fire_claim'] = None

    def test_a_stale_attempt_does_not_hide_a_lost_claim_after_the_occurrence_took_new_intake(self):
        # The release was tried, withdrawn and the occurrence resumed. It is a normal active occurrence again.
        self.build()
        self.update(_native_release_tried=self.tried(), _steering=['A new instruction.'])
        self.lose_claim()
        self.tick()
        self.assert_cancelled_as_lost()

    def test_a_stale_attempt_does_not_hide_a_lost_claim_of_a_working_occurrence(self):
        self.build()
        self.update(_native_release_tried=self.tried(), status='working')
        self.lose_claim()
        self.tick()
        self.assert_cancelled_as_lost()

    def test_an_attempt_for_another_card_does_not_hide_a_lost_claim(self):
        self.build()
        self.service.kanban.call = lambda request, spawn=None: (_ for _ in ()).throw(OSError('The adapter is silent.'))
        self.update(_native_release_tried=self.tried(native_id='t_other'))
        self.lose_claim()
        self.tick()
        self.assert_cancelled_as_lost()

    def silent_adapter(self):
        self.service.kanban.call = lambda request, spawn=None: (_ for _ in ()).throw(OSError('The adapter is silent.'))

    # The refreshed job shows the claim lost. Only the occurrence that still is the ended one is settled without a heartbeat.
    def test_an_abandoned_attempt_and_new_active_intake_with_a_lost_claim_is_cancelled_normally(self):
        self.build()
        self.update(_native_release_tried=self.tried(), _steering=['A new instruction.'])
        self.native.job['fire_claim'] = None
        self.tick()
        self.assert_cancelled_as_lost()

    def test_an_abandoned_attempt_of_a_working_occurrence_with_a_reowned_claim_is_cancelled_normally(self):
        self.build()
        self.update(_native_release_tried=self.tried(), status='working')
        self.native.job['fire_claim'] = {'by': 'other-owner'}
        self.tick()
        self.assert_cancelled_as_lost()

    def test_an_attempt_for_another_card_with_a_lost_claim_is_cancelled_normally(self):
        self.build()
        self.silent_adapter()
        self.update(_native_release_tried=self.tried(native_id='t_other'), _steering=['A new instruction.'])
        self.native.job['fire_claim'] = None
        self.tick()
        self.assert_cancelled_as_lost()

    def test_a_retained_ended_wait_with_a_lost_claim_is_settled_without_a_heartbeat(self):
        self.build()
        self.silent_adapter()
        self.update(_native_release_tried=self.tried())
        self.native.job['fire_claim'] = {'by': 'other-owner'}
        self.tick()
        self.assertEqual(self.operations(), [])
        self.assert_lost_without_touching_the_task()
        self.tick()
        self.assertEqual(self.operations(), [])

    def test_a_missing_job_view_does_not_end_the_tracking_of_a_held_claim(self):
        self.build()
        self.silent_adapter()
        self.update(_native_release_tried=self.tried())
        read = self.native_settings
        def without_job(operation, bots, **arguments):
            result = read(operation, bots, **arguments)
            for routine in result.get('routines', []):
                routine.pop('_job')
            return result
        self.service.provider.native_settings = without_job
        self.tick()
        self.assertEqual(self.operations(), ['routine_heartbeat'])
        self.assertFalse(self.stored().get('_native_job_finished'))
        self.assertEqual(self.stored()['status'], 'waiting')
        self.assertEqual(self.event_count('routine.reconciliation_required'), 0)

    def test_a_missing_job_view_is_no_proof_of_a_lost_claim_for_an_active_occurrence(self):
        self.build()
        self.update(_native_release_tried=self.tried(), _steering=['A new instruction.'])
        read = self.native_settings
        def without_job(operation, bots, **arguments):
            result = read(operation, bots, **arguments)
            for routine in result.get('routines', []):
                routine.pop('_job')
            return result
        self.service.provider.native_settings = without_job
        self.native.job['fire_claim'] = None
        self.tick()
        self.assert_cancelled_as_lost()

    def test_a_confirmed_completion_is_reconciled_even_with_new_active_intake(self):
        self.build()
        self.lose_the_answer()
        self.update(_steering=['A new instruction.'], status='working')
        self.tick()
        self.assert_reconciled_without_touching_the_task('working')

    def test_a_release_attempt_saves_the_authenticated_card_with_the_job_and_owner(self):
        self.build()
        self.tick()
        self.assert_released()
        self.assertEqual(self.stored()['_native_release_tried'], self.tried())

    def test_a_lost_claim_still_fails_an_occurrence_that_never_ended_natively(self):
        # Unchanged: no proof and no release was tried, so the claim is all that keeps this task going.
        self.build(capture=False)
        self.service.kanban.cards['t_orig']['status'] = 'archived'
        self.native.job['fire_claim'] = {'by': 'other-owner'}
        self.tick()
        task = self.stored()
        self.assertEqual(self.operations(), ['routine_heartbeat'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['summary'], 'The routine lost its native execution claim.')
        self.assertTrue(task['_native_job_finished'])

    # The adapter is not asked again on every tick for a card that cannot be proven.
    def completions(self):
        return [c['id'] for c in self.service.kanban.calls if c['op'] == 'completion']

    def expire(self):
        for probe in self.service._release_probes.values():
            probe['due'] = 0

    def test_an_unproven_card_is_not_read_again_on_every_tick(self):
        self.build(capture=False)
        self.service.kanban.cards['t_orig']['status'] = 'archived'
        self.tick()
        self.assertEqual(sorted(self.completions()), ['t_orig', 't_succ'])
        for _ in range(5):
            self.tick()
        self.assertEqual(len(self.completions()), 2)
        self.assertEqual(self.operations(), ['routine_heartbeat'] * 6)
        self.assertEqual(self.service._release_probes[self.task_id]['failures'], 1)
        # The pause is over, the card is read once more, and the pause then grows to a bound.
        self.expire()
        self.tick()
        self.assertEqual(len(self.completions()), 4)
        probe = self.service._release_probes[self.task_id]
        self.assertEqual(probe['failures'], 2)
        with patch('coconut.routine_coordinator.time.monotonic', return_value=1000.0):
            self.service._release_probes[self.task_id]['failures'] = 50
            self.expire()
            self.tick()
        self.assertEqual(self.service._release_probes[self.task_id]['due'], 1900.0)

    def test_a_silent_adapter_is_not_called_on_every_tick(self):
        self.build(capture=False)
        calls = []
        def call(request, spawn=None):
            calls.append(request)
            raise RuntimeError('The Hermes Kanban adapter timed out.')
        self.service.kanban.call = call
        for _ in range(4):
            self.tick()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.operations(), ['routine_heartbeat'] * 4)

    def test_a_card_that_settles_later_is_released_after_the_pause(self):
        self.build()
        self.service.kanban.cards['t_orig']['status'] = 'running'
        self.tick()
        self.tick()
        self.assertEqual(self.operations(), ['routine_heartbeat'] * 2)
        self.assertEqual(self.native.job['fire_claim'], {'by': 'owner'})
        self.service.kanban.cards['t_orig']['status'] = 'done'
        self.tick()
        self.assertEqual(self.operations(), ['routine_heartbeat'] * 3)
        self.assertEqual(len(self.completions()), 1)
        self.expire()
        self.calls.clear()
        self.tick()
        self.assert_released()
        self.assertNotIn(self.task_id, self.service._release_probes)

    def test_a_new_receipt_is_read_without_waiting_for_the_pause(self):
        self.build(capture=False)
        self.service.kanban.cards['t_orig']['status'] = 'archived'
        self.tick()
        self.tick()
        before = len(self.completions())
        self.assertEqual(before, 2)
        self.receipt('kanban_complete', dict(task_id='t_third', summary='Done.'), dict(ok=True, task_id='t_third', run_id=5400))
        self.service.kanban.cards['t_third'] = self.card('t_third', run=5400, status='archived')
        self.tick()
        self.assertEqual(len(self.completions()), before + 3)

    def test_the_pause_of_a_task_that_no_longer_waits_is_dropped(self):
        self.build(capture=False)
        self.service.kanban.cards['t_orig']['status'] = 'archived'
        self.tick()
        self.assertIn(self.task_id, self.service._release_probes)
        self.update(status='working')
        self.tick()
        self.assertNotIn(self.task_id, self.service._release_probes)
        self.update(status='waiting')
        self.tick()
        self.assertEqual(len(self.completions()), 4)


class AdapterCompletionTests(unittest.TestCase):
    def board(self, **task):
        from coconut.kanban_runtime import Board
        board = Board.__new__(Board)
        board.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(board.conn.close)
        board.conn.execute('CREATE TABLE coconut_outcomes (native_id TEXT PRIMARY KEY, input_id TEXT)')
        board.conn.execute('CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, outcome TEXT, ended_at INTEGER)')
        # Hermes' event and comment tables. archive_note reads them; this card was never archived.
        board.conn.execute('CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, '
                           'created_at INTEGER)')
        board.conn.execute('CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER)')
        board.conn.executemany('INSERT INTO task_runs VALUES (?,?,?,?,?)', [
            (5330, 't_orig', 'crashed', 'crashed', 1), (5332, 't_orig', 'done', 'completed', 2), (5380, 't_succ', 'running', None, None)])
        card = SimpleNamespace(id='t_orig', tenant='bot', idempotency_key='coconut:task', status='done',
                               current_run_id=None, worker_pid=None) if task is not None else None
        board.kb = SimpleNamespace(get_task=lambda conn, identity: card if identity == 't_orig' else None)
        return board

    def test_completion_reads_the_card_and_its_runs_by_native_id(self):
        board = self.board()
        board.conn.execute("INSERT INTO coconut_outcomes VALUES ('t_orig', 'turn-original')")
        self.assertEqual(board.completion('t_orig'), dict(
            id='t_orig', tenant='bot', idempotency_key='coconut:task', status='done', current_run_id=None, worker_pid=None,
            input_id='turn-original', runs=[dict(id=5330, status='crashed', outcome='crashed', ended=True),
                                           dict(id=5332, status='done', outcome='completed', ended=True)], archive=None))

    def test_a_card_without_accepted_input_or_that_does_not_exist(self):
        board = self.board()
        self.assertIsNone(board.completion('t_orig')['input_id'])
        board.conn.execute("INSERT INTO coconut_outcomes VALUES ('t_orig', '')")
        self.assertIsNone(board.completion('t_orig')['input_id'])
        self.assertIsNone(board.completion('t_missing'))


if __name__ == '__main__':
    unittest.main()
