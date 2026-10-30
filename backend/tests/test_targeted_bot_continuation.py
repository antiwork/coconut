"""Bot continuations keep the saved execution without changing worktree ownership."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.native_relay import delivery_payload, message_tool_schema
from coconut.service import Service, ServiceError, YieldForDecision
import test_work_progress as fixtures
from test_work_progress import progress
import legacy_work_sync
from hermes_env import needs_hermes, SOURCE
from coconut.answer_undo import AnswerUndo
# These tests check what an answer does, so it applies at once. test_answer_undo.py tests the Undo window.
AnswerUndo.answer_undo_seconds = 0

setUpModule, tearDownModule = legacy_work_sync.scope()


class TargetedBotContinuationTests(unittest.TestCase):
    setUp = fixtures.WorkProgressTests.setUp
    tearDown = fixtures.WorkProgressTests.tearDown
    write = fixtures.WorkProgressTests.write

    def prepare(self):
        previous = self.service._get('tasks', self.child)
        previous.update(status='failed', _execution_session_id=self.child,
                        _conversation_epoch=self.service._setting('conversation_epoch'),
                        _work_todos=progress(), _preference_source_ids=['saved-human-source'])
        self.service._put('tasks', previous)
        source = self.service._get('tasks', self.parent['id'])
        source.update(status='working', _execution_session_id=source['id'])
        self.service._put('tasks', source)
        return previous, source

    def send(self, source, previous, **changes):
        arguments = dict(target=self.dev['id'], message='Continue the approved correction.',
                         continuation_task_id=previous['id']) | changes
        call = self.service.emit(source['id'], 'native_call', dict(name='message_agent', arguments=arguments))
        payload = dict(native_call_id=call['id'], native_envelope_id=uuid4().hex,
                       target_bot_id=arguments['target'], text=arguments['message'],
                       continuation_task_id=arguments['continuation_task_id'])
        return payload, self.service.emit(source['id'], 'native_bot_message', payload)

    def assert_rejected(self, source, previous, reason):
        payload, receipt = self.send(source, previous)
        self.assertEqual(receipt['status'], 'rejected')
        self.assertIn(reason, receipt['error'])
        self.assertEqual(self.service._get('native_receipts', payload['native_call_id'])['state'], 'completed')
        self.assertEqual(self.service._get('tasks', source['id'])['status'], 'working')
        self.assertEqual(self.service.emit(source['id'], 'native_bot_message', payload), receipt)
        return payload, receipt

    def test_target_survives_an_unrelated_helper_callback_and_restart(self):
        previous, source = self.prepare()
        helper = self.write('/v1/bots', {'name': 'Care'})
        with self.service.transaction():
            callback = self.service._store_bot_message(source['id'], {'target_bot_id': helper['id'], 'text': 'Check local setup.'})
            callback = self.service._get('tasks', callback['task_id'])
            source = dict(source, id=str(uuid4()), source_task_id=callback['id'], _is_return=True)
            self.service._put('tasks', source)
        before_users = len([m for m in self.service._all('messages') if m['role'] == 'user'])
        payload, receipt = self.send(source, previous)
        continued = self.service._get('tasks', receipt['task_id'])
        self.assertNotEqual(continued['id'], previous['id'])
        self.assertEqual(continued['_execution_session_id'], previous['_execution_session_id'])
        self.assertEqual(continued['parent_task_id'], previous['id'])
        self.assertEqual(continued['_work_todos'], previous['_work_todos'])
        self.assertEqual(continued['_preference_source_ids'], ['saved-human-source'])
        self.assertEqual(continued['_bot_messages'][-1]['sender_bot_id'], self.desk['id'])
        self.assertEqual(continued['source_task_id'], source['id'])
        self.assertEqual(continued['_native_relay']['envelope_id'], payload['native_envelope_id'])
        self.assertEqual(len([m for m in self.service._all('messages') if m['role'] == 'user']), before_users)
        self.assertEqual(self.service.emit(source['id'], 'native_bot_message', payload), receipt)
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.assertEqual(self.service.emit(source['id'], 'native_bot_message', payload), receipt)
        self.assertEqual(self.service._get('tasks', continued['id'])['_execution_session_id'], self.child)
        self.service._put('tasks', dict(self.service._get('tasks', source['id']), status='working'))
        self.assert_rejected(source, previous, 'already has active work')

    def test_wrong_owner_scope_and_terminal_state_do_not_change_execution(self):
        previous, source = self.prepare()
        cases = [dict(status=s) for s in ('working', 'queued', 'waiting', 'paused', 'cancelled', 'completed')]
        cases += [dict(bot_id=self.desk['id']), dict(session_id='old'), dict(thread_id='another'),
                  dict(conversation_bot_id=self.dev['id']), dict(_conversation_epoch='old'),
                  dict(_native_outcome_id='native-outcome'), dict(_chat_session=True),
                  dict(_native_job={'id': 'scheduled-job'}), dict(routine_id='scheduled-job')]
        for changes in cases:
            with self.subTest(changes=changes):
                self.service._put('tasks', dict(previous, **changes))
                before = len(self.service._all('tasks'))
                self.assert_rejected(source, previous, 'Choose a failed task')
                self.assertEqual(len(self.service._all('tasks')), before)
                self.assertEqual(self.service._get('tasks', previous['id'])['_execution_session_id'], self.child)
        self.service._put('tasks', previous)

    def test_pending_decision_and_cancelled_exchange_remain_blocked(self):
        previous, source = self.prepare()
        self.service._put('decisions', dict(id='pending', task_id=previous['id'], status='pending'))
        self.assert_rejected(source, previous, 'pending decision')
        self.service._put('decisions', dict(id='pending', task_id=previous['id'], status='executed'))
        exchange = self.service._get('exchanges', previous['exchange_id'])
        self.service._put('exchanges', dict(exchange, cancelled=True))
        self.assert_rejected(source, previous, 'paused or cancelled')

    def test_explicit_work_pause_and_live_process_remain_blocked(self):
        previous, source = self.prepare()
        with patch.object(self.service, '_automatic_work_is_paused', return_value=True):
            self.assert_rejected(source, previous, 'paused or cancelled')
        with patch.object(self.service, '_background_pending', return_value=True):
            self.assert_rejected(source, previous, 'already has active work')

    def test_optional_proposal_does_not_block_authorized_continuation(self):
        previous, source = self.prepare()
        self.service._put('decisions', dict(id='optional', task_id=previous['id'],
                                           status='pending', inbox_kind='proposal'))
        _, receipt = self.send(source, previous)
        self.assertEqual(self.service._get('tasks', receipt['task_id'])['_execution_session_id'], self.child)
        self.assertEqual(self.service._get('decisions', 'optional')['status'], 'pending')

    def test_receipt_rejects_target_substitution(self):
        previous, source = self.prepare()
        payload, receipt = self.send(source, previous)
        for identity in (None, source['id']):
            with self.subTest(identity=identity), self.assertRaisesRegex(ServiceError, 'does not match'):
                self.service.emit(source['id'], 'native_bot_message', dict(payload, continuation_task_id=identity))

    def test_malformed_continuation_id_returns_a_completed_rejection(self):
        previous, source = self.prepare()
        for value in (' ', '', None, 42, 'x' * 101):
            with self.subTest(value=value):
                payload, receipt = self.send(source, previous, continuation_task_id=value)
                self.assertEqual(receipt['status'], 'rejected')
                self.assertIn('continuation_task_id', receipt['error'])
                self.assertEqual(self.service._get('native_receipts', payload['native_call_id'])['state'], 'completed')
                self.assertEqual(self.service.emit(source['id'], 'native_bot_message', payload), receipt)
                self.assertEqual(self.service._get('tasks', source['id'])['status'], 'working')

    def test_schema_and_saved_delivery_keep_optional_task_target(self):
        native = {'name': 'message_agent', 'description': 'Send a message.', 'parameters': {
            'type': 'object', 'properties': {'target': {'type': 'string'}, 'message': {'type': 'string'}},
            'required': ['target', 'message']}}
        original = copy.deepcopy(native)
        schema = message_tool_schema(native)
        self.assertEqual(native, original)
        self.assertIn('continuation_task_id', schema['parameters']['properties'])
        self.assertNotIn('continuation_task_id', schema['parameters']['required'])
        intent = dict(call_id='call', target_bot_id='bot', text='Continue', envelope={'id': 'envelope'},
                      continuation_task_id='original')
        self.assertEqual(delivery_payload(intent)['continuation_task_id'], 'original')
        del intent['continuation_task_id']
        self.assertNotIn('continuation_task_id', delivery_payload(intent))

    def test_unknown_effects_use_existing_recovery_context_before_retry(self):
        from coconut.hermes_worker import conversation_input
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working'))
        unknown = self.service.emit(previous['id'], 'native_call', {
            'name': 'terminal', 'arguments': {'command': 'git push'}})
        previous['_self_recovery'] = dict(unknown_tools=[dict(id='older', action={'name': 'publish'})])
        self.service._put('tasks', previous)
        _, receipt = self.send(source, previous)
        captured = []
        def verify(task, bot, history, emit):
            captured.append(task)
            emit('completed', {'summary': 'Verified the saved result.'})
        self.provider.start_task = verify
        self.service._run_task(receipt['task_id'])
        recovery = captured[0]['_self_recovery']
        self.assertEqual({r['id'] for r in recovery['unknown_tools']}, {'older', unknown['id']})
        prompt = conversation_input(dict(operation='start', task=captured[0], history=[]))[0]
        self.assertIn('Never repeat a write while its effect remains unknown', prompt)
        self.assertIn('git push', prompt)
        self.assertEqual(self.service._get('native_receipts', unknown['id'])['state'], 'executing')

    def test_helper_can_target_failed_requester_without_becoming_a_return(self):
        previous, source = self.prepare()
        source.update(source_bot_id=self.dev['id'], source_task_id=previous['id'],
                      _reply_to_task_id=previous['id'])
        self.service._put('tasks', source)
        _, receipt = self.send(source, previous)
        continued = self.service._get('tasks', receipt['task_id'])
        self.assertFalse(continued['_is_return'])
        self.assertNotIn('_assignment_task_id', continued)
        self.assertEqual(continued['_reply_to_task_id'], source['id'])
        self.provider.mode = 'done'
        self.service._run_task(continued['id'])
        self.assertEqual(self.service._get('tasks', continued['id'])['status'], 'completed')

    def test_rejected_decision_survives_failure_before_provider_receives_it(self):
        from coconut.hermes_worker import conversation_input
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working'))
        with self.assertRaises(YieldForDecision):
            self.service.native_event(previous['id'], 'native_question', {
                'title': 'Publish the correction?',
                'options': [dict(id='yes', label='Publish'), dict(id='no', label='Keep local')]})
        decision = self.service._all('decisions')[-1]
        self.write('/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, choice='reject'))
        def fail_before_input(*args, **kwargs):
            raise RuntimeError('Provider unavailable before input')
        self.provider.resume_task = fail_before_input
        self.service._run_task(previous['id'])
        previous = self.service._get('tasks', previous['id'])
        self.assertEqual(previous['status'], 'failed')
        self.assertEqual(previous['_resume_decision'], decision['id'])
        _, receipt = self.send(source, previous)
        captured = []
        def check_restriction(task, bot, history, emit):
            captured.append(task)
            emit('completed', {'summary': 'The correction stays local.'})
        self.provider.start_task = check_restriction
        self.service._run_task(receipt['task_id'])
        saved = captured[0]['_self_recovery']['decisions']
        self.assertEqual(saved[0]['id'], decision['id'])
        self.assertEqual(saved[0]['status'], 'rejected')
        prompt = conversation_input(dict(operation='start', task=captured[0], history=[]))[0]
        self.assertIn('Publish the correction?', prompt)
        self.assertIn('rejected', prompt)
        self.assertIn('This is not a new human instruction or approval', prompt)
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'rejected')

    def test_undelivered_human_followup_reaches_the_continued_session(self):
        from coconut.hermes_worker import conversation_input
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working', _turn_message_ids=[]))
        self.write('/v1/tasks/' + previous['id'] + '/replies', {'text': 'Do not publish. Keep the correction local.'})
        self.service.emit(previous['id'], 'failed', {'summary': 'Provider interrupted.'})
        previous = self.service._get('tasks', previous['id'])
        pending = previous['_blocked_followup']
        _, receipt = self.send(source, previous)
        captured = []
        def check_followup(task, bot, history, emit):
            captured.append((task, history))
            emit('completed', {'summary': 'The correction stays local.', '_work_todos': progress('completed')})
        self.provider.start_task = check_followup
        self.service._run_task(receipt['task_id'])
        task, history = captured[0]
        self.assertTrue(set(pending['message_ids']).issubset(task['_turn_message_ids']))
        prompt = conversation_input(dict(operation='start', task=task, history=history))[0]
        self.assertIn('Do not publish. Keep the correction local.', prompt)
        self.assertNotIn('_blocked_followup', self.service._get('tasks', previous['id']))

    def test_selected_human_followup_survives_provider_failure(self):
        from coconut.hermes_worker import conversation_input
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='queued'))
        restriction = 'Do not publish. Keep the correction local.'
        self.write('/v1/tasks/' + previous['id'] + '/replies', {'text': restriction})
        def fail_before_input(*args, **kwargs):
            raise RuntimeError('Provider unavailable before input')
        self.provider.start_task = fail_before_input
        self.service._run_task(previous['id'])
        previous = self.service._get('tasks', previous['id'])
        self.assertEqual(previous['status'], 'failed')
        self.assertTrue(previous['_turn_message_ids'])
        self.assertFalse(previous.get('_steering'))
        self.assertNotIn('_blocked_followup', previous)
        _, receipt = self.send(source, previous)
        captured = []
        def check_followup(task, bot, history, emit):
            captured.append((task, history))
            emit('completed', {'summary': 'The correction stays local.', '_work_todos': progress('completed')})
        self.provider.start_task = check_followup
        self.service._run_task(receipt['task_id'])
        task, history = captured[0]
        self.assertTrue(set(previous['_turn_message_ids']).issubset(task['_turn_message_ids']))
        self.assertIn(restriction, conversation_input(dict(operation='start', task=task, history=history))[0])

    @needs_hermes
    def test_completed_result_survives_failure_and_prevents_repeated_write(self):
        from coconut.hermes_worker import conversation_input
        from coconut.native_bridge import NativeBridge
        from coconut.native_turn_scope import current, TurnScope
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working', _native_turn_id='older-turn'))
        older = self.service.emit(previous['id'], 'native_call', dict(name='terminal', arguments={'command': 'git status'}))
        self.service.emit(previous['id'], 'native_result', dict(id=older['id'], result='Earlier status'))
        self.service._put('tasks', dict(previous, status='queued', _native_turn_id='failed-turn'))
        saved = []
        action = dict(name='terminal', arguments={'command': 'git push origin HEAD'})
        def fail_after_result(task, bot, history, emit):
            record = emit('native_call', action)
            emit('native_result', dict(id=record['id'], result='Push accepted'))
            saved.append(record['id'])
            raise RuntimeError('Native session commit failed after the tool returned')
        self.provider.start_task = fail_after_result
        self.service._run_task(previous['id'])
        previous = self.service._get('tasks', previous['id'])
        self.assertEqual(previous['status'], 'failed')
        _, receipt = self.send(source, previous)
        # A second interruption before input must retain the first saved result.
        def fail_before_input(*args, **kwargs):
            raise RuntimeError('Provider unavailable before input')
        self.provider.start_task = fail_before_input
        self.service._run_task(receipt['task_id'])
        continued = self.service._get('tasks', receipt['task_id'])
        _, receipt = self.send(source, continued)
        captured = []
        def verify_result(task, bot, history, emit):
            captured.append((task, history))
            emit('completed', {'summary': 'The push already succeeded.', '_work_todos': progress('completed')})
        self.provider.start_task = verify_result
        self.service._run_task(receipt['task_id'])
        task, history = captured[0]
        self.assertEqual([r['id'] for r in task['_native_receipts']], saved)
        request = dict(operation='start', bot=self.dev, task=task, history=history)
        self.assertIn('coconut_read_receipts', conversation_input(request)[0])
        from coconut.native_receipt_context import read_receipts
        recovered = read_receipts(task['_native_receipts'], {'receipt_id': saved[0]})
        self.assertEqual(json.loads(recovered['json_chunk'])['result'], 'Push accepted')
        calls = []
        token = current.set(TurnScope(task['id']))
        home = Path(self.temp.name) / 'bridge-home'
        try:
            with patch.dict(os.environ, HERMES_HOME=str(home)), patch.object(
                    sys, 'path', [str(Path(__file__).parents[1] / 'coconut'), SOURCE, *sys.path]):
                bridge = NativeBridge(request, lambda *args: None, lambda *args: None)
                bridge.install(None)
                try:
                    result = bridge.recorded(action['name'], action['arguments'], lambda args: calls.append(args))
                    self.assertEqual(result, 'Push accepted')
                    self.assertEqual(calls, [])
                finally:
                    bridge.close()
        finally:
            current.reset(token)
        final = self.service._get('tasks', task['id'])
        self.service._advance_input(final)
        self.assertNotIn('_continuation_receipt_ids', final)
        self.assertEqual(self.service._get('native_receipts', saved[0])['task_id'], previous['id'])

    def test_bot_correction_survives_failure_before_provider_input(self):
        from coconut.hermes_worker import conversation_input
        previous, source = self.prepare()
        _, receipt = self.send(source, previous, message='Keep this change local. Do not publish.')
        def fail_before_input(*args, **kwargs):
            raise RuntimeError('Provider unavailable before input')
        self.provider.start_task = fail_before_input
        self.service._run_task(receipt['task_id'])
        previous = self.service._get('tasks', receipt['task_id'])
        self.assertEqual(previous['status'], 'failed')
        _, receipt = self.send(source, previous, message='Continue from the saved results.')
        captured = []
        def check_context(task, bot, history, emit):
            captured.append((task, history))
            emit('completed', {'summary': 'The change stays local.', '_work_todos': progress('completed')})
        self.provider.start_task = check_context
        self.service._run_task(receipt['task_id'])
        task, history = captured[0]
        prompt = conversation_input(dict(operation='start', task=task, history=history))[0]
        self.assertIn('Keep this change local. Do not publish.', prompt)
        self.assertIn('Continue from the saved results.', prompt)
        self.assertEqual(task['_bot_messages'][-2]['sender_bot_id'], self.desk['id'])

    def test_background_result_survives_failure_before_provider_input(self):
        from coconut.hermes_worker import conversation_input
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working', _native_turn_id='process-start'))
        record = self.service.emit(previous['id'], 'native_call', dict(name='terminal', arguments={'background': True}))
        self.service.emit(previous['id'], 'native_result', dict(id=record['id'], result=json.dumps({'session_id': 'proc_review', 'notify_on_complete': True, 'status': 'running'})))
        record = self.service._get('native_receipts', record['id'])
        with self.service.transaction():
            self.service._resume_process_task(previous['id'], [(record, 'Review completed. Fix the currency label.')])
        def fail_before_input(*args, **kwargs):
            raise RuntimeError('Provider unavailable before input')
        self.provider.start_task = fail_before_input
        self.service._run_task(previous['id'])
        previous = self.service._get('tasks', previous['id'])
        self.assertEqual(previous['status'], 'failed')
        self.assertTrue(self.service._get('native_receipts', record['id'])['_process_delivery'])
        _, receipt = self.send(source, previous)
        captured = []
        def check_result(task, bot, history, emit):
            captured.append((task, history))
            emit('completed', {'summary': 'The label is corrected.', '_work_todos': progress('completed')})
        self.provider.start_task = check_result
        self.service._run_task(receipt['task_id'])
        task, history = captured[0]
        self.assertIn('Review completed. Fix the currency label.', conversation_input(dict(operation='start', task=task, history=history))[0])

    @needs_hermes
    def test_completed_kanban_creation_keeps_its_original_replay_identity(self):
        from coconut.native_bridge import NativeBridge
        from coconut.native_turn_scope import current, TurnScope
        previous, source = self.prepare()
        previous.update(status='working', _native_turn_id='first-intake')
        self.service._put('tasks', previous)
        root = Path(self.temp.name)
        arguments = dict(title='Review the approved correction', body='Use the existing scope')
        calls = []
        def execute(task, args, repeats=1):
            token = current.set(TurnScope(task['id']))
            bridge = NativeBridge(dict(operation='start', bot=self.dev, task=task), lambda *a: None,
                                  lambda kind, data: self.service.emit(task['id'], kind, data))
            bridge.install(None)
            def create(payload):
                calls.append(payload)
                return create_task(conn, **payload)
            try:
                results = [bridge.recorded('kanban_create', args, create) for _ in range(repeats)]
                return results[0] if repeats == 1 else results
            finally:
                bridge.close()
                current.reset(token)
        with patch.dict(os.environ, HERMES_HOME=str(root / 'kanban-home')), patch.object(
                sys, 'path', [str(Path(__file__).parents[1] / 'coconut'), SOURCE, *sys.path]):
            from hermes_cli.kanban_db import create_task
            from hermes_cli.kanban_db_connect import connect
            conn = connect(root / 'kanban.db')
            try:
                original = execute(previous, arguments)
                previous.update(status='failed')
                self.service._put('tasks', previous)
                _, receipt = self.send(source, previous)
                captured = []
                def verify(task, bot, history, emit):
                    captured.extend(execute(task, arguments, repeats=2))
                    emit('completed', {'summary': 'The existing outcome is retained.', '_work_todos': progress('completed')})
                self.provider.start_task = verify
                self.service._run_task(receipt['task_id'])
                self.assertEqual(captured, [original, original])
                self.assertEqual(len(calls), 2)
                self.assertEqual(conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT session_id FROM tasks WHERE id=?', (original,)).fetchone()[0], previous['id'])
            finally:
                conn.close()

    def test_pending_pr_review_survives_provider_failure(self):
        from coconut.hermes_worker import conversation_input
        previous, source = self.prepare()
        review = dict(url='https://github.com/example/repo/pull/1', head_sha='current-head',
                      checks_state='failed', check_failures='The currency regression failed')
        self.service._put('tasks', dict(previous, status='queued', _pr_review=review))
        def fail_before_input(*args, **kwargs):
            raise RuntimeError('Provider unavailable before input')
        self.provider.start_task = fail_before_input
        self.service._run_task(previous['id'])
        previous = self.service._get('tasks', previous['id'])
        self.assertEqual(previous['status'], 'failed')
        _, receipt = self.send(source, previous)
        captured = []
        def check_review(task, bot, history, emit):
            captured.append((task, history))
            emit('completed', {'summary': 'The regression is fixed.', '_work_todos': progress('completed')})
        self.provider.start_task = check_review
        self.service._run_task(receipt['task_id'])
        task, history = captured[0]
        self.assertEqual(task['_pr_review'], review)
        prompt = conversation_input(dict(operation='start', task=task, history=history))[0]
        self.assertIn('The currency regression failed', prompt)
        self.assertIn('current-head', prompt)

    def test_pr_checks_resume_the_continuation_instead_of_the_failed_owner(self):
        from dataclasses import replace
        from test_service_pr import Tracker, observation, URL
        previous, source = self.prepare()
        self.service.pr_tracker = Tracker(replace(observation(), pr_state='open', draft=False, checks_state='running'))
        self.service._put('tasks', dict(previous, status='working'))
        artifact = self.service.emit(previous['id'], 'artifact', dict(kind='pr', url=URL))
        artifact = self.service._get('artifacts', artifact['id'])
        artifact['_reviewed_states'] = {'older-head': 'saved-review-signal'}
        self.service._put('artifacts', artifact)
        self.service._put('tasks', previous)
        _, receipt = self.send(source, previous)
        artifact = self.service._get('artifacts', artifact['id'])
        self.assertEqual(artifact['_review_task_id'], receipt['task_id'])
        self.assertEqual(artifact['_reviewed_states'], {'older-head': 'saved-review-signal'})
        artifact.update(checks_state='passed', review_checks='new-review-signal')
        self.service._put('artifacts', artifact)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', previous['id'])['status'], 'failed')
        continued = self.service._get('tasks', receipt['task_id'])
        self.assertEqual(continued['status'], 'queued')
        self.service._put('tasks', dict(continued, status='waiting'))
        self.service._queue_pr_reviews()
        resumed = self.service._get('tasks', continued['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertEqual(resumed['_pr_review']['review_checks'], 'new-review-signal')
        self.assertEqual(self.service._get('tasks', previous['id'])['status'], 'failed')

    def test_native_routine_owner_cannot_resume_through_a_failed_callback(self):
        previous, source = self.prepare()
        routine = dict(previous, id='routine-owner', _execution_session_id='routine-owner',
                       _native_job={'id': 'scheduled-job'}, created_at='2000-01-01T00:00:00Z')
        self.service._put('tasks', routine)
        for changes in ({'_assignment_task_id': routine['id']}, {'_execution_session_id': routine['id']}):
            with self.subTest(changes=changes):
                callback = dict(previous, **changes)
                self.service._put('tasks', callback)
                self.assert_rejected(source, callback, 'Choose a failed task')

    def test_continuation_preserves_newer_pr_ownership_on_another_bot(self):
        from dataclasses import replace
        from test_service_pr import Tracker, observation, URL
        previous, source = self.prepare()
        self.service.pr_tracker = Tracker(replace(observation(), pr_state='open', draft=False, checks_state='running'))
        self.service._put('tasks', dict(previous, status='working'))
        artifact = self.service.emit(previous['id'], 'artifact', dict(kind='pr', url=URL))
        self.service._put('tasks', previous)
        other = dict(self.service._get('artifacts', artifact['id']), id='newer-owner', bot_id=self.desk['id'],
                     task_id=source['id'], _review_task_id=source['id'], _review_ownership_at=source['updated_at'])
        self.service._put('artifacts', other)
        _, receipt = self.send(source, previous)
        peers = [self.service._get('artifacts', identity) for identity in (artifact['id'], other['id'])]
        self.assertEqual(self.service._pr_review_owner(peers)['id'], other['id'])
        self.assertEqual(peers[0]['_review_task_id'], previous['id'])
        self.assertEqual(self.service._get('tasks', receipt['task_id'])['_execution_session_id'], previous['_execution_session_id'])

    def test_stale_target_cannot_restart_a_completed_continuation(self):
        previous, source = self.prepare()
        _, receipt = self.send(source, previous)
        continued = self.service._get('tasks', receipt['task_id'])
        self.service._put('tasks', dict(continued, status='completed'))
        self.assert_rejected(source, previous, 'already continued')

    def test_live_delegation_keeps_its_original_callback(self):
        previous, source = self.prepare()
        helper = dict(source, id=str(uuid4()), _execution_session_id='helper-execution',
                      source_task_id=previous['id'], _reply_to_task_id=previous['id'])
        self.service._put('tasks', helper)
        previous['_waiting_on'] = [helper['id']]
        self.service._put('tasks', previous)
        self.assert_rejected(source, previous, 'Wait for the result')
        self.assertEqual(self.service._get('tasks', helper['id']), helper)
        helper.update(status='completed', _return_pending={'text': 'Saved helper result.'})
        self.service._put('tasks', helper)
        self.assert_rejected(source, previous, 'Wait for the result')
        self.assertEqual(self.service._get('tasks', helper['id']), helper)
        self.assertEqual(self.service._get('tasks', previous['id'])['_waiting_on'], [helper['id']])

    def test_stale_target_cannot_restart_an_implicit_continuation(self):
        previous, source = self.prepare()
        source.update(_is_return=True, source_task_id=previous['id'])
        self.service._put('tasks', source)
        with self.service.transaction():
            receipt = self.service._store_bot_message(source['id'], dict(
                target_bot_id=self.dev['id'], text='Continue the approved correction.'))
        continued = self.service._get('tasks', receipt['task_id'])
        self.assertEqual(continued['_execution_session_id'], previous['_execution_session_id'])
        self.assertNotEqual(continued['parent_task_id'], previous['id'])
        self.provider.mode = 'done'
        self.service._run_task(continued['id'])
        self.assertEqual(self.service._get('tasks', continued['id'])['status'], 'completed')
        self.assert_rejected(source, previous, 'already continued')

    def test_work_tracks_targeted_continuation_and_does_not_queue_recovery(self):
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working'))
        work = self.service._register_work(previous['id'], dict(
            key='approved-correction', title='Finish the approved correction',
            status='working', activity='Checking the correction'))
        self.service._put('tasks', previous)
        source = dict(source, id=str(uuid4()), _waiting_on=[])
        self.service._put('tasks', source)
        _, receipt = self.send(source, previous)
        row = self.service._work_projection([self.service._get('artifacts', work['id'])])[0]
        self.assertEqual(row['work_state'], 'queued')
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(self.service._get('tasks', receipt['task_id'])['status'], 'queued')
        self.write('/v1/work/items/' + work['id'] + '/cancel', {})
        self.assertEqual(self.service._get('tasks', receipt['task_id'])['status'], 'cancelled')
        self.service._run_task(receipt['task_id'])
        self.assertEqual(self.provider.starts, [])

    def test_completed_continuation_finishes_existing_automatic_work(self):
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working'))
        work = self.service._register_work(previous['id'], dict(
            key='approved-correction', title='Finish the approved correction',
            status='working', activity='Checking the correction'))
        self.service._put('artifacts', dict(self.service._get('artifacts', work['id']), _automatic_work=True))
        self.service._put('tasks', previous)
        _, receipt = self.send(source, previous)
        self.service._sync_automatic_work()
        item = self.service._get('artifacts', work['id'])
        self.assertEqual(item['task_id'], receipt['task_id'])
        self.assertEqual(item['_work_origin_task_id'], previous['id'])
        self.provider.mode = 'done'
        self.service._run_task(receipt['task_id'])
        self.service._sync_automatic_work()
        row = self.service._work_projection([self.service._get('artifacts', work['id'])])[0]
        self.assertEqual(row['work_state'], 'completed')
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(self.service._get('artifacts', work['id'])['task_id'], receipt['task_id'])

    @needs_hermes
    def test_native_delivery_race_returns_a_terminal_error_to_the_sender(self):
        from coconut.native_relay import NativeRelay, recover_delivery
        previous, source = self.prepare()
        arguments = dict(target=self.dev['id'], message='Continue the correction.', continuation_task_id=previous['id'])
        call = self.service.emit(source['id'], 'native_call', dict(name='message_agent', arguments=arguments))
        home = self.service.profiles.home(self.desk)
        request = dict(operation='start', bot=self.desk, task=dict(source, _available_bots=[self.dev]))
        with patch.dict(os.environ, HERMES_HOME=str(home)), patch.object(
                sys, 'path', [str(Path(__file__).parents[1] / 'coconut'), SOURCE, *sys.path]):
            relay = NativeRelay(request, lambda kind, payload: self.service.emit(source['id'], kind, payload))
            relay.sync_roster()
            def resume_before_delivery(args):
                result = relay.enqueue(args)
                other = dict(source, id=str(uuid4()))
                self.service._put('tasks', other)
                self.send(other, previous)
                return result
            result = json.loads(relay.deliver(arguments, resume_before_delivery, call['id']))
            payload = recover_delivery(home, call['id'])
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('already has active work', result['error'])
        self.assertEqual(self.service._get('tasks', source['id'])['status'], 'working')
        self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'completed')
        self.assertEqual(self.service.emit(source['id'], 'native_bot_message', payload), result)

    @needs_hermes
    def test_native_envelope_round_trip_keeps_target_after_interruption(self):
        from coconut.native_relay import NativeRelay, recover_delivery
        previous, source = self.prepare()
        arguments = dict(target=self.dev['id'], message='Continue the correction.', continuation_task_id=previous['id'])
        call = self.service.emit(source['id'], 'native_call', dict(name='message_agent', arguments=arguments))
        home = self.service.profiles.home(self.desk)
        request = dict(operation='start', bot=self.desk,
                       task=dict(source, _available_bots=[self.dev]))
        with patch.dict(os.environ, HERMES_HOME=str(home)), patch.object(
                sys, 'path', [str(Path(__file__).parents[1] / 'coconut'), SOURCE, *sys.path]):
            relay = NativeRelay(request, lambda kind, payload: self.service.emit(source['id'], kind, payload))
            relay.sync_roster()
            result = json.loads(relay.deliver(arguments, relay.enqueue, call['id']))
            payload = recover_delivery(home, call['id'])
            self.assertEqual(payload['continuation_task_id'], previous['id'])
            self.assertEqual(self.service.emit(source['id'], 'native_bot_message', payload), result)
            self.assertEqual(len(list((home / 'bot_relay/claimed').glob('*.json'))), 1)
        continued = self.service._get('tasks', result['task_id'])
        self.assertEqual(continued['_execution_session_id'], self.child)
        self.provider.mode = 'done'
        self.service._run_task(continued['id'])
        final = self.service._get('tasks', continued['id'])
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(final['_native_reply_pending']['envelope_id'], payload['native_envelope_id'])

    @needs_hermes
    def test_continuation_reopens_real_dirty_worktree_without_rebinding(self):
        from coconut.work_workspace import acquire, git
        previous, source = self.prepare()
        root = Path(self.temp.name)
        repo = root / 'repo'
        repo.mkdir()
        git(repo, 'init')
        git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'Initial')
        with patch.object(sys, 'path', [str(Path(__file__).parents[1] / 'coconut'), SOURCE, *sys.path]):
            binding = acquire(root / 'workspaces', previous['_execution_session_id'], str(repo))
            note = Path(binding['path']) / 'pending.txt'
            note.write_text('Uncommitted correction')
            _, receipt = self.send(source, previous)
            continued = self.service._get('tasks', receipt['task_id'])
            resumed = acquire(root / 'workspaces', continued['_execution_session_id'], str(repo))
            self.assertEqual(resumed, binding)
            self.assertEqual(note.read_text(), 'Uncommitted correction')
            with self.assertRaisesRegex(ValueError, 'Another outcome'):
                acquire(root / 'workspaces', continued['id'], str(repo), binding['path'])


class TargetedContinuationProviderTests(unittest.TestCase):
    import test_provider as provider_fixtures
    setUp = provider_fixtures.ProviderTests.setUp
    tearDown = provider_fixtures.ProviderTests.tearDown
    fake_hermes = provider_fixtures.ProviderTests.fake_hermes
    configure_peer = provider_fixtures.ProviderTests.configure_peer

    def test_rejected_continuation_reaches_native_bot_without_stopping_it(self):
        peer = self.configure_peer()
        arguments = repr(dict(target=peer['id'], message='Continue.', continuation_task_id='old-task'))
        provider = self.fake_hermes(
            "import json\nresult = json.loads(registry.dispatch('message_agent', " + arguments + "))\n"
            "assert result['status'] == 'rejected'\n"
            "assert result['error'] == 'This execution already has active work.'\n"
            "assert 'private_memory' not in result\n"
            "return {'completed': True, 'final_response': 'The existing worker is active.'}\n")
        events = []
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'native_call':
                return dict(status='recorded', id='call')
            if kind == 'native_bot_message':
                self.assertEqual(data['continuation_task_id'], 'old-task')
                return dict(status='rejected', error='This execution already has active work.',
                            target_bot_id=peer['id'], private_memory='Do not forward this.')
        with patch.dict(os.environ, ANTHROPIC_API_KEY='test-only'):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(events[-1][0], 'completed', events)
        self.assertNotIn('failed', [kind for kind, data in events])

    def test_work_validation_error_allows_the_bot_to_correct_its_request(self):
        provider = self.fake_hermes(
            "import json\nresult = json.loads(registry.dispatch('coconut_track_pr', {'url': 'https://github.com/example/repo/pull/1', 'work_id': 'pr-artifact'}))\n"
            "assert result == {'status': 'rejected', 'error': 'Choose a Work outcome to attach this pull request.'}\n"
            "fixed = json.loads(registry.dispatch('coconut_track_pr', {'url': 'https://github.com/example/repo/pull/1'}))\n"
            "assert fixed['id'] == 'pr-artifact'\n"
            "return {'completed': True, 'final_response': 'The PR is tracked.'}\n")
        requests, events = [], []
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'artifact':
                requests.append(data)
                if data.get('work_id'):
                    raise ServiceError(400, 'Choose a Work outcome to attach this pull request.')
                return dict(id='pr-artifact', kind='pr', url=data['url'])
        with patch.dict(os.environ, ANTHROPIC_API_KEY='test-only'):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(len(requests), 2)
        self.assertNotIn('work_id', requests[-1])
        self.assertEqual(events[-1][0], 'completed', events)
