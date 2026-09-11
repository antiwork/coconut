import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.profiles import HermesProfiles
from coconut.service import Service, ServiceError, YieldForDecision


class NativeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Service(self.root, FixtureProvider(self.root), start_workers=False)
        self.bot = self.write('POST', '/v1/bots', {'name': 'Native', 'role': ''})
        self.task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check', 'mode': 'queue'})
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()))

    def event(self, kind, data):
        return self.service.emit(self.task['id'], kind, data)

    def restart(self):
        self.service.close()
        self.service = Service(self.root, FixtureProvider(self.root), start_workers=False)

    def approval(self):
        call = self.event('native_call', {'name': 'terminal', 'arguments': {'command': 'fixture'}})
        with self.assertRaises(YieldForDecision):
            self.event('native_approval', {'receipt_id': call['id'], 'title': 'Run the check?', 'explanation': 'Native approval check.'})
        return self.service.snapshot()['decisions'][0]

    def test_uncertain_tool_is_not_repeated_after_restart(self):
        self.event('text_delta', {'text': 'Checking the target.'})
        self.event('native_call', {'name': 'terminal', 'arguments': {'command': 'fixture'}})
        self.restart()
        self.assertEqual(self.service.snapshot()['tasks'][0]['status'], 'failed')
        self.assertFalse(any(m['streaming'] for m in self.service.snapshot()['messages']))

    def test_unknown_tool_result_leaves_one_visible_reply_after_restart(self):
        self.event('native_call', {'name': 'execute_code', 'arguments': {'code': 'write_file()'}})
        self.restart()
        replies = [m for m in self.service.snapshot()['messages'] if m['role'] == 'assistant' and m['kind'] == 'text']
        self.assertEqual(len(replies), 1)
        self.assertIn('Reply to continue', replies[0]['text'])
        self.assertFalse(replies[0]['streaming'])
        self.restart()
        self.assertEqual([m for m in self.service.snapshot()['messages'] if m['role'] == 'assistant' and m['kind'] == 'text'], replies)

    def test_recovered_response_does_not_append_to_interrupted_text(self):
        self.event('text_delta', {'text': 'An unfinished response'})
        partial = self.service.snapshot()['messages'][-1]
        self.restart()
        self.service._run_task(self.task['id'])
        messages = self.service.snapshot()['messages']
        self.assertEqual(next(m for m in messages if m['id'] == partial['id'])['text'], partial['text'])
        self.assertFalse(any(m['streaming'] for m in messages))
        self.assertNotEqual(messages[-1]['id'], partial['id'])

    def test_completed_tool_receipt_survives_restart(self):
        call = self.event('native_call', {'name': 'terminal', 'arguments': {'command': 'fixture'}})
        self.event('native_result', {'id': call['id'], 'result': '{"output":"done"}'})
        self.restart()
        self.assertEqual(self.service.snapshot()['tasks'][0]['status'], 'queued')
        self.assertEqual(self.service._all('native_receipts')[0]['state'], 'completed')

    def test_pending_native_approval_survives_restart(self):
        decision = self.approval()
        self.restart()
        state = self.service.snapshot()
        self.assertEqual(state['decisions'][0]['id'], decision['id'])
        self.assertEqual(state['tasks'][0]['status'], 'needs_you')
        result = self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'once'})
        self.assertEqual(result['status'], 'approved')
        self.assertEqual(self.service.snapshot()['tasks'][0]['status'], 'queued')

    def request_context(self, decision):
        before = {row['id'] for row in self.service._all('contexts')}
        self.write('POST', f"/v1/decisions/{decision['id']}/context", {'question': 'Check the file again.'})
        context = next(row for row in self.service._all('contexts') if row['id'] not in before)
        with self.service.transaction():
            context['status'] = 'working'
            self.service._put('contexts', context)
        return context

    def context_call(self, context, result=None):
        call = self.service.native_event(self.task['id'], 'native_call',
                                        {'name': 'read_file', 'arguments': {'path': 'note.txt'}}, context_id=context['id'])
        if result is not None:
            self.service.native_event(self.task['id'], 'native_result',
                                      {'id': call['id'], 'result': result}, context_id=context['id'])
        return call['id']

    def test_unknown_context_result_blocks_recovery_and_preserves_the_decision(self):
        decision = self.approval()
        context = self.request_context(decision)
        self.context_call(context)
        self.restart()
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'failed')
        self.assertEqual(self.service._get('contexts', context['id'])['error_code'], 'tool_result_unknown')
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        messages = [m for m in self.service._all('messages') if m.get('decision_id') == decision['id']]
        self.assertIn('Check its result on the Mac', messages[-1]['text'])
        count = len(messages)
        self.restart()
        self.assertEqual(len([m for m in self.service._all('messages') if m.get('decision_id') == decision['id']]), count)

    def test_queued_context_with_unknown_result_does_not_start_a_provider(self):
        context = self.request_context(self.approval())
        self.context_call(context)
        with self.service.transaction():
            context['status'] = 'queued'
            self.service._put('contexts', context)
        self.service.provider.answer_context = Mock()
        self.service._start_context(context['id'])
        thread = self.service.context_threads[context['id']]
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.service.provider.answer_context.assert_not_called()
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'failed')

    def test_failed_context_does_not_block_approval_or_recovery_of_the_original_task(self):
        decision = self.approval()
        context = self.request_context(decision)
        self.context_call(context)
        self.restart()
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'once'})
        resumes = []

        def interrupted(task, bot, history, decision, emit):
            resumes.append(task['id'])
            self.service.stopping.set()

        self.service.provider.resume_task = interrupted
        self.service._run_task(self.task['id'])
        self.assertEqual(resumes, [self.task['id']])
        self.restart()
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')

        def finish(task, bot, history, decision, emit):
            call = emit('native_call', {'name': 'terminal', 'arguments': {'command': 'fixture'}})
            emit('native_result', {'id': call['id'], 'result': '{"output":"done"}'})
            emit('completed', {'summary': 'Done.'})

        self.service.provider.resume_task = finish
        self.service._run_task(self.task['id'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'executed')
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'failed')

    def test_context_replays_only_its_own_completed_tools_after_restart(self):
        original = self.event('native_call', {'name': 'read_file', 'arguments': {'path': 'note.txt'}})
        self.event('native_result', {'id': original['id'], 'result': 'original task content'})
        decision = self.approval()
        previous = self.request_context(decision)
        self.context_call(previous, 'previous question content')
        with self.service.transaction():
            previous['status'] = 'completed'
            self.service._put('contexts', previous)
        current = self.request_context(decision)
        current_call = self.context_call(current, 'current question content')
        self.restart()
        self.service.provider.answer_context = Mock()
        self.service._start_context(current['id'])
        thread = self.service.context_threads[current['id']]
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.service.provider.answer_context.assert_called_once()
        task = self.service.provider.answer_context.call_args.args[0]
        self.assertEqual([r['id'] for r in task['_native_receipts']], [current_call])
        self.assertEqual(self.service._get('contexts', current['id'])['status'], 'completed')
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_other_bot_can_work_while_native_approval_waits(self):
        self.approval()
        bot = self.write('POST', '/v1/bots', {'name': 'Independent'})
        task = self.write('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Check', 'mode': 'queue'})
        self.service._run_task(task['id'])
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'pending')

    def check_context_waits_for_restoration(self, conversation_owner):
        archived_bot = self.bot
        if conversation_owner:
            archived_bot = self.write('POST', '/v1/bots', {'name': 'Desk'})
            with self.service.transaction():
                task = self.service._get('tasks', self.task['id'])
                task['conversation_bot_id'] = archived_bot['id']
                self.service._put('tasks', task)
        decision = self.approval()
        self.write('POST', f"/v1/decisions/{decision['id']}/context", {'question': 'Explain the action.'})
        context = self.service._all('contexts')[0]
        self.write('PATCH', f"/v1/bots/{archived_bot['id']}", {'archived': True})
        self.service.provider.answer_context = Mock()
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=2)
        self.service.provider.answer_context.assert_not_called()
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'queued')
        self.write('PATCH', f"/v1/bots/{archived_bot['id']}", {'archived': False})
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=2)
        self.service.provider.answer_context.assert_called_once()
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'completed')
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_archived_bot_defers_its_context_request(self):
        self.check_context_waits_for_restoration(conversation_owner=False)

    def test_archived_conversation_defers_its_delegated_context_request(self):
        self.check_context_waits_for_restoration(conversation_owner=True)

    def test_unknown_option_does_not_approve(self):
        decision = self.approval()
        with self.assertRaises(ServiceError):
            self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'unknown'})
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'pending')

    def test_question_options_come_from_provider(self):
        with self.assertRaises(YieldForDecision):
            self.event('native_question', {'title': 'Which route?', 'options': [{'id': 'custom', 'label': 'Take the long route'}, {'id': 'hold', 'label': 'Do nothing today'}]})
        options = self.service.snapshot()['decisions'][0]['options']
        self.assertEqual([o['label'] for o in options], ['Take the long route', 'Do nothing today'])
        self.assertTrue(all('explanation' in option for option in options))

    def test_invalid_native_choices_do_not_create_a_pending_decision(self):
        examples = [
            {'options': [{'id': 'x' * 81, 'label': 'Long ID'}]},
            {'options': [{'id': 'x', 'label': 'Choice'}], 'recommended_option_id': 'missing'},
            {'options': [{'id': 'x', 'label': 'First'}, {'id': 'x', 'label': 'Second'}]},
        ]
        for payload in examples:
            with self.subTest(payload=payload), self.assertRaises(ServiceError):
                self.event('native_question', dict(title='Invalid choice', **payload))
        self.assertEqual(self.service._all('decisions'), [])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'working')

    def test_receipt_cannot_complete_another_task(self):
        call = self.event('native_call', {'name': 'terminal', 'arguments': {}})
        other = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Second', 'mode': 'queue'})
        with self.service.transaction():
            task = self.service._get('tasks', other['id']);task['status']='working';self.service._put('tasks',task)
        with self.assertRaises(ServiceError):
            self.service.emit(other['id'], 'native_result', {'id': call['id'], 'result': 'wrong task'})

    def test_no_workflow_permissions_in_snapshot_or_database(self):
        self.assertNotIn('policies', self.service.snapshot())
        names = {row[0] for row in self.service.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('policies', names)
        self.assertNotIn('workflow', self.service.snapshot()['tasks'][0])
        with self.assertRaises(ServiceError):
            self.write('PUT', '/v1/policies/missing', {'mode': 'review'})

    def test_blank_optional_label_and_notifications_roundtrip(self):
        result = self.write('PATCH', f"/v1/bots/{self.bot['id']}", {'role': '', 'notifications': False})
        self.assertEqual(result['role'], '')
        self.assertFalse(result['notifications'])


class NativeMemoryMigrationTests(unittest.TestCase):
    def test_existing_native_memory_is_preserved_and_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            profiles = HermesProfiles(temporary)
            bot = dict(id=str(uuid4()), session_id=str(uuid4()), name='Memory', instructions='Native instructions', memory='Older note.', preferences=[{'text': 'Always offer do nothing.'}])
            home = profiles.home(bot)
            (home / 'memories').mkdir(parents=True)
            (home / 'memories/USER.md').write_text('Existing native preference.\n')
            profiles.sync(bot)
            profiles.sync(bot)
            content = (home / 'memories/USER.md').read_text()
            self.assertEqual(content.count('Always offer do nothing.'), 1)
            self.assertIn('Existing native preference.', content)
            self.assertIn('Older note.', (home / 'memories/MEMORY.md').read_text())
            self.assertFalse((home / 'COCONUT_MEMORY.md').exists())
            (home / 'memories/USER.md').write_text('Changed in Hermes.')
            profiles.sync(bot)
            self.assertEqual((home / 'memories/USER.md').read_text(), 'Changed in Hermes.')
