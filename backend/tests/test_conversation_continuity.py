import base64
import copy
import tempfile
import unittest
from uuid import uuid4

from coconut.hermes_worker import conversation_input
from coconut.native_conversation import NativeConversation, current_message, seed_messages, missing_prefix
from coconut.service import Service
from test_service import ControlledProvider


class NativeStore:
    def __init__(self):
        self.meta, self.sessions, self.tips = {}, {}, {}
        self.replacements = 0

    def get_meta(self, key): return self.meta.get(key)
    def set_meta(self, key, value): self.meta[key] = value
    def create_session(self, session_id, **kwargs): self.sessions.setdefault(session_id, [])
    def get_session(self, session_id): return {'id': session_id} if session_id in self.sessions else None
    def end_session(self, session_id, reason): pass
    def resolve_resume_session_id(self, root): return self.tips.get(root, root)
    def get_resume_conversations(self, session_id):
        return copy.deepcopy(self.sessions[session_id]), copy.deepcopy(self.sessions[session_id])
    def replace_messages(self, session_id, messages, **kwargs):
        self.replacements += 1
        self.sessions[session_id] = copy.deepcopy(messages)
    def close(self): pass


class ConversationInputTests(unittest.TestCase):
    def setUp(self):
        self.root = str(uuid4())
        self.request = dict(operation='start', bot=dict(id=str(uuid4()), session_id=self.root),
                            task=dict(id='current', title='Truncated title'), history=[
                                self.message('old-user', 'earlier', 'user', 'Use the blue label.'),
                                self.message('old-answer', 'earlier', 'assistant', 'I will use blue.'),
                                self.message('current-user', 'current', 'user', 'Which label?'),
                                self.message('queued-user', 'queued', 'user', 'Do a different task later.')])
        self.db = NativeStore()

    def message(self, identity, task, role, text, **kwargs):
        return dict(id=identity, task_id=task, role=role, kind='text', text=text, **kwargs)

    def test_new_task_keeps_earlier_bot_conversation_and_ignores_later_queue(self):
        prompt, history = conversation_input(self.request)
        self.assertEqual(prompt, 'Which label?')
        self.assertEqual([row['content'] for row in history], ['Use the blue label.', 'I will use blue.'])

    def test_old_card_keeps_exact_request_after_native_compaction(self):
        original = 'Detailed request. ' * 400 + 'Keep the final filename: annual-report.md'
        self.request['history'][0]['text'] = original
        decision = dict(id='old-card', task_id='earlier', action_type='question', status='approved',
                        title='Which format?', request_message_id='old-user', request_text=original[:4000],
                        selected_option_id='markdown', selected_option_label='Markdown')
        self.db.meta['coconut.history-context.' + self.root] = 'complete'
        self.db.sessions[self.root] = [{'role': 'user', 'content': 'Native summary of unrelated recent work.'}]
        for operation in ('resume', 'context'):
            with self.subTest(operation=operation):
                request = dict(self.request, operation=operation, decision=decision, question='Use the original filename?',
                               task=dict(self.request['task'], pending_decision=decision, _decision_reply=True))
                prompt, _ = conversation_input(request)
                self.assertIn(original, prompt)
                self.assertNotIn('Do a different task later.', prompt)
                self.assertEqual(NativeConversation(request, self.db).history, self.db.sessions[self.root])
                self.assertEqual(self.db.replacements, 0)
        self.assertEqual(decision['request_text'], original[:4000])

    def test_old_reply_and_reaction_keep_reference_after_native_continuation(self):
        source = self.request['history'][1]
        source['text'] = 'The original recommendation has a unique detail: cobalt-17.'
        current = self.request['history'][2]
        current['reply_to'] = dict(message_id=source['id'], text=source['text'], author='Desk', role='assistant', author_bot_id=self.request['bot']['id'])
        child = str(uuid4())
        self.db.meta['coconut.history-context.' + self.root] = 'complete'
        self.db.tips[self.root] = child
        self.db.sessions[child] = [{'role': 'user', 'content': 'Recent work summary.'}]
        for reaction in (False, True):
            with self.subTest(reaction=reaction):
                if reaction:
                    current['reaction_event'] = dict(emoji='👍', action='added')
                    current['text'] = 'Reaction added: 👍'
                prompt, _ = conversation_input(self.request)
                self.assertIn(source['text'], prompt)
                active = NativeConversation(self.request, self.db)
                self.assertEqual(active.session_id, child)
                self.assertEqual(active.history, self.db.sessions[child])
                self.assertEqual(self.db.replacements, 0)
                if reaction:
                    self.assertIn('does not approve, reject, or select a pending decision', prompt)

    def test_all_queued_messages_enter_the_prompt_in_order(self):
        self.request['history'].append(self.message('follow-up', 'current', 'user', 'Keep it short.'))
        prompt, history = conversation_input(self.request)
        self.assertEqual(prompt, 'Which label?\n\nKeep it short.')
        self.assertEqual([row['content'] for row in history], ['Use the blue label.', 'I will use blue.'])
        self.assertEqual([row['content'] for row in seed_messages(self.request)],
                         ['Use the blue label.', 'I will use blue.'])

    def test_background_work_does_not_import_an_unrelated_unanswered_chat(self):
        self.request['task'].update(id='audit', _execution_session_id='audit', _turn_message_ids=[],
                                   _source_audit_prompt='Check the assigned launch issue.')
        history = NativeConversation(self.request, self.db).history
        prompt, _ = conversation_input(self.request)
        self.assertEqual(history, [])
        self.assertIn('Check the assigned launch issue.', prompt)
        self.assertNotIn('Which label?', prompt)
        self.assertNotIn('Do a different task later.', prompt)

    def test_work_history_keeps_the_owner_and_explicit_assignment(self):
        self.request['task'].update(_execution_session_id='earlier')
        self.assertEqual([row['content'] for row in seed_messages(self.request)],
                         ['Use the blue label.', 'I will use blue.'])
        self.request['task'].update(_execution_session_id='new-owner', _handoff_assignment={'task_id': 'earlier'})
        self.assertEqual([row['content'] for row in seed_messages(self.request)],
                         ['Use the blue label.', 'I will use blue.'])

    def test_import_runs_once_and_never_replaces_native_tool_history(self):
        first = NativeConversation(self.request, self.db)
        self.assertEqual(len(first.history), 2)
        self.db.sessions[self.root].append({'role': 'tool', 'content': 'Durable native result', 'tool_call_id': 'tool-1'})
        second = NativeConversation(self.request, self.db)
        self.assertEqual(second.history[-1]['content'], 'Durable native result')
        self.assertEqual(self.db.replacements, 1)

    def test_native_compaction_tip_resumes_without_regrowing_old_messages(self):
        NativeConversation(self.request, self.db)
        tip = str(uuid4())
        self.db.tips[self.root] = tip
        self.db.sessions[tip] = [{'role': 'user', 'content': 'Native summary', '_compressed_summary': True}]
        resumed = NativeConversation(self.request, self.db)
        self.assertEqual(resumed.session_id, tip)
        self.assertEqual(resumed.history, self.db.sessions[tip])
        self.assertNotIn('Use the blue label.', str(resumed.history))

    def test_restart_uses_fresh_native_root_and_preserves_old_history(self):
        NativeConversation(self.request, self.db)
        self.request['task']['_conversation_epoch'] = str(uuid4())
        fresh = NativeConversation(self.request, self.db)
        self.assertNotEqual(fresh.session_id, self.root)
        self.assertEqual(fresh.history, [])
        self.assertEqual(len(self.db.sessions[self.root]), 2)
        self.assertEqual(NativeConversation(self.request, self.db).session_id, fresh.session_id)

    def test_import_retry_does_not_duplicate_a_committed_batch(self):
        self.db.create_session(self.root)
        self.db.replace_messages(self.root, seed_messages(self.request))
        imported = NativeConversation(self.request, self.db)
        self.assertEqual(len(imported.history), 2)
        self.assertEqual(self.db.replacements, 1)

    def test_missing_app_prefix_enters_active_context_without_losing_native_tools(self):
        self.db.sessions[self.root] = [{'role': 'assistant', 'content': 'Existing native reply',
                                       'tool_calls': [{'id': 'native-call'}],
                                       'reasoning_details': [{'type': 'reasoning.encrypted', 'data': 'opaque'}]}]
        original = copy.deepcopy(self.db.sessions[self.root])
        resumed = NativeConversation(self.request, self.db)
        self.assertEqual(resumed.session_id, self.root)
        self.assertEqual(resumed.history[0]['content'], 'Use the blue label.')
        self.assertEqual(resumed.history[2:], original)
        self.assertEqual(set(self.db.sessions), {self.root})
        NativeConversation(self.request, self.db)
        self.assertEqual(self.db.replacements, 1)

    def test_searchable_only_legacy_import_is_repaired_once_in_current_session(self):
        archived = str(uuid4())
        self.db.sessions[archived] = seed_messages(self.request)
        self.db.set_meta('coconut.history-import.' + self.root, 'complete')
        self.db.set_meta('coconut.history-import.' + self.root + '.target', archived)
        self.db.sessions[self.root] = [{'role': 'user', 'content': 'Which label?'}]
        repaired = NativeConversation(self.request, self.db)
        self.assertEqual([row['content'] for row in repaired.history],
                         ['Use the blue label.', 'I will use blue.', 'Which label?'])
        self.assertEqual(len(self.db.sessions[archived]), 2)
        self.assertEqual(NativeConversation(self.request, self.db).history, repaired.history)
        self.assertEqual(self.db.replacements, 1)

    def test_migration_does_not_duplicate_the_overlapping_native_tail(self):
        saved = seed_messages(self.request)
        native = dict(saved[1], message_id=saved[1]['platform_message_id'])
        self.assertEqual(missing_prefix(saved, [native]), saved[:1])

    def test_repeated_text_uses_native_timestamp_to_find_the_boundary(self):
        saved = [{'role': 'user', 'content': 'Hello', 'timestamp': stamp} for stamp in (10, 20, 30)]
        self.assertEqual(missing_prefix(saved, [{'role': 'user', 'content': 'Hello', 'timestamp': 21}]), saved[:1])

    def test_no_overlap_imports_only_messages_before_native_history(self):
        saved = [{'role': 'user', 'content': str(stamp), 'timestamp': stamp} for stamp in (10, 30)]
        self.assertEqual(missing_prefix(saved, [{'role': 'user', 'content': 'Native start', 'timestamp': 20}]), saved[:1])

    def test_import_excludes_hidden_deleted_and_partial_responses(self):
        self.request['history'][:0] = [self.message('hidden', 'old', 'assistant', 'Hidden', _hidden=True),
                                      self.message('deleted', 'old', 'assistant', 'Deleted', _deleted=True),
                                      self.message('partial', 'old', 'assistant', 'Partial', streaming=True)]
        self.assertEqual(len(seed_messages(self.request)), 2)


class BotConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None, key=None):
        return self.service.mutate(method, path, data or {}, key or str(uuid4()), 'test-phone')

    def bot(self, name): return self.write('POST', '/v1/bots', {'name': name})
    def send(self, bot, text): return self.write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': text})

    def check_failed_turn_preserves_replies(self, route, uncertain=False):
        bot = self.bot('Writer')
        task = self.send(bot, 'Draft the report.')
        attachment = self.write('POST', '/v1/attachments', {'bot_id': bot['id'], 'filename': 'labels.txt',
            'content_base64': base64.b64encode(b'Label details').decode()})
        calls = []
        def fail(current, bot, history, emit):
            calls.append(current['id'])
            self.write('POST', f'/v1/tasks/{task["id"]}/replies', {'text': 'Create Scout for research.'})
            self.write('POST', f'/v1/tasks/{task["id"]}/replies', {'text': 'Use the blue label.', 'attachment_ids': [attachment['id']]})
            if uncertain:
                emit('native_call', {'name': 'terminal', 'arguments': {'command': 'fixture'}})
                self.service.stopping.set()
            else:
                emit('failed', {'summary': 'The provider stopped.'})
        self.provider.start_task = fail
        self.service._run_task(task['id'])
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        failed = self.service._get('tasks', task['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed.get('steering_status'), 'blocked')
        self.assertIn('Reply to continue', failed['summary'])
        self.service._run_task(task['id'])
        self.assertEqual(calls, [task['id']])
        path = f'/v1/tasks/{task["id"]}/replies' if route == 'thread' else f'/v1/bots/{bot["id"]}/messages'
        continuation = self.write('POST', path, {'text': 'Continue with those changes.'})
        self.assertNotEqual(continuation['id'], task['id'])
        self.assertEqual(continuation['thread_id'], task['thread_id'])
        captured = []
        self.provider.start_task = lambda task, bot, history, emit: captured.append((task, conversation_input(dict(operation='start', task=task, history=history))[0]))
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.service._run_task(continuation['id'])
        current, prompt = captured[0]
        self.assertTrue(prompt.startswith('Create Scout for research.\n\nUse the blue label.\n\nContinue with those changes.'))
        self.assertEqual([a['id'] for a in current['attachments']], [attachment['id']])
        self.assertIn('Create Scout for research.', [s['text'] for s in current['_bot_creation_sources']])
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'failed')
        self.assertEqual(self.service._get('tasks', task['id']).get('steering_status'), 'continued')
        later = self.send(bot, 'Next task.')
        self.service._run_task(later['id'])
        self.assertEqual(captured[1][1], 'Next task.')

    def test_failed_turn_keeps_pending_replies_for_a_chat_continuation(self):
        self.check_failed_turn_preserves_replies('chat')

    def test_failed_turn_keeps_pending_replies_for_a_thread_continuation(self):
        self.check_failed_turn_preserves_replies('thread')

    def test_uncertain_turn_keeps_pending_replies_without_automatic_retry(self):
        self.check_failed_turn_preserves_replies('chat', uncertain=True)

    def test_queued_and_active_replies_reach_distinct_native_turns(self):
        bot = self.bot('Writer')
        task = self.send(bot, 'Draft the report.')
        route = f'/v1/tasks/{task["id"]}/replies'
        self.write('POST', route, {'text': 'Keep it short.'})
        self.send(bot, 'Unrelated queued task.')
        prompts = []

        def start(task, bot, history, emit):
            prompt, _ = conversation_input(dict(operation='start', task=task, history=history))
            prompts.append(prompt)
            if len(prompts) == 1:
                self.write('POST', route, {'text': 'Use the blue label.'})
                self.write('POST', route, {'text': 'Include the totals.'})
            emit('completed', {'summary': 'Complete.'})

        self.provider.start_task = start
        self.service._run_task(task['id'])
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')
        self.service._run_task(task['id'])
        self.assertEqual(prompts, ['Draft the report.\n\nKeep it short.',
                                   'Use the blue label.\n\nInclude the totals.'])

    def test_restart_keeps_inflight_input_separate_from_later_replies(self):
        bot = self.bot('Writer')
        task = self.send(bot, 'Draft the report.')
        route = f'/v1/tasks/{task["id"]}/replies'
        prompts = []
        files = []
        attachment = self.write('POST', '/v1/attachments',
                                {'bot_id': bot['id'], 'filename': 'labels.txt',
                                 'content_base64': base64.b64encode(b'Updated label details').decode()})

        def start(task, bot, history, emit):
            prompt, _ = conversation_input(dict(operation='start', task=task, history=history))
            prompts.append(prompt)
            files.append([a['id'] for a in task.get('attachments', [])])
            if len(prompts) == 1:
                self.write('POST', route, {'text': 'Keep it short.'})
                self.write('POST', route, {'text': 'Use the blue label.', 'attachment_ids': [attachment['id']]})
                self.service.stopping.set()
            else:
                emit('completed', {'summary': 'Complete.'})

        self.provider.start_task = start
        self.service._run_task(task['id'])
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.service._run_task(task['id'])
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')
        self.service._run_task(task['id'])
        self.assertEqual(files, [[], [], [attachment['id']]])
        self.assertEqual(prompts[:2], ['Draft the report.', 'Draft the report.'])
        self.assertTrue(prompts[2].startswith('Keep it short.\n\nUse the blue label.'))
        self.assertIn('Updated label details', prompts[2])

    def test_replies_while_recovered_input_waits_are_dispatched_after_it(self):
        bot = self.bot('Writer')
        task = self.send(bot, 'Draft the report.')
        prompts = []

        def start(task, bot, history, emit):
            prompts.append(conversation_input(dict(operation='start', task=task, history=history))[0])
            if len(prompts) == 1:
                self.service.stopping.set()
            else:
                emit('completed', {'summary': 'Complete.'})

        self.provider.start_task = start
        self.service._run_task(task['id'])
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.write('POST', f'/v1/tasks/{task["id"]}/replies', {'text': 'Keep it short.'})
        self.service._run_task(task['id'])
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')
        self.service._run_task(task['id'])
        self.assertEqual(prompts, ['Draft the report.', 'Draft the report.', 'Keep it short.'])

    def test_reply_after_approval_waits_for_the_native_continuation(self):
        bot = self.bot('Writer')
        task = self.send(bot, 'Write the report.')
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][0]
        self.write('POST', f'/v1/decisions/{decision["id"]}/answer', {'choice': 'approve', 'revision': decision['revision']})
        self.write('POST', f'/v1/tasks/{task["id"]}/replies', {'text': 'Explain the totals.'})
        self.service._run_task(task['id'])
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')
        prompts = []
        self.provider.start_task = lambda task, bot, history, emit: prompts.append(
            conversation_input(dict(operation='start', task=task, history=history))[0])
        self.service._run_task(task['id'])
        self.assertEqual(prompts, ['Explain the totals.'])

    def test_every_mentioned_bot_receives_and_persists_the_full_shared_request(self):
        origin, first, second = self.bot('Desk'), self.bot('Writer'), self.bot('Reader')
        shared = 'Read this full request, including its final instruction. ' * 3 + 'Use the blue label.'
        result = self.write('POST', f'/v1/bots/{origin["id"]}/messages',
                            {'text': shared, 'recipient_bot_ids': [first['id'], second['id']]})
        second_task = next(t for t in self.service.snapshot()['tasks'] if t['bot_id'] == second['id'])
        self.write('POST', f'/v1/tasks/{second_task["id"]}/replies', {'text': 'Keep it short.'})
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        received = {}

        def start(task, bot, history, emit):
            request = dict(operation='start', task=task, history=history)
            received[bot['id']] = (conversation_input(request)[0], current_message(request))
            emit('completed', {'summary': 'Complete.'})

        self.provider.start_task = start
        for task_id in result['dispatched_task_ids']:
            self.service._run_task(task_id)
        for bot, expected in ((first, shared), (second, shared + '\n\nKeep it short.')):
            prompt, persisted = received[bot['id']]
            self.assertEqual(prompt, expected)
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted['text'], expected)

    def test_context_questions_keep_their_attachment_binding_after_restart(self):
        bot = self.bot('Writer')
        files = [self.write('POST', '/v1/attachments',
                            {'bot_id': bot['id'], 'filename': name + '.txt',
                             'content_base64': base64.b64encode(name.encode()).decode()})
                 for name in ('original', 'first', 'second')]
        task = self.write('POST', f'/v1/bots/{bot["id"]}/messages',
                          {'text': 'Write the report.', 'attachment_ids': [files[0]['id']]})
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][0]
        for index, question in enumerate(('First question', 'Second question'), 1):
            self.write('POST', f'/v1/tasks/{task["id"]}/replies',
                       {'text': question, 'decision_id': decision['id'], 'attachment_ids': [files[index]['id']]})
        with self.service.transaction():
            first = self.service._all('contexts')[0]
            first['status'] = 'working'
            self.service._put('contexts', first)
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        received = {}
        self.provider.answer_context = lambda task, bot, history, question, emit: received.update(
            {question: [a['id'] for a in task['attachments']]})
        for context in self.service._all('contexts'):
            self.service._start_context(context['id'])
            thread = self.service.context_threads[context['id']]
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(received, {'First question': [files[0]['id'], files[1]['id']],
                                    'Second question': [files[0]['id'], files[2]['id']]})
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_rejecting_a_file_proposal_preserves_a_queued_reply(self):
        bot = self.bot('Writer')
        task = self.send(bot, 'Write the report.')
        original_start = self.provider.start_task

        def propose(task, bot, history, emit):
            self.write('POST', f'/v1/tasks/{task["id"]}/replies', {'text': 'Explain the totals.'})
            original_start(task, bot, history, emit)

        self.provider.start_task = propose
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][0]
        key = str(uuid4())
        route = f'/v1/decisions/{decision["id"]}/answer'
        data = {'choice': 'reject', 'revision': decision['revision']}
        self.write('POST', route, data, key)
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.write('POST', route, data, key)
        received = []
        self.provider.start_task = lambda task, bot, history, emit: received.append(
            conversation_input(dict(operation='start', task=task, history=history))[0])
        self.service._run_task(task['id'])
        self.service._run_task(task['id'])
        self.assertEqual(received, ['Explain the totals.'])
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'rejected')
        self.assertEqual(self.service.snapshot()['artifacts'], [])

    def check_file_answer_preserves_queued_bot_requests(self, choice):
        bot, peer = self.bot('Writer'), self.bot('Old reader')
        task = self.send(bot, 'Write the report.')
        followup = 'Create Scout for research and delete Old reader.'
        original_start = self.provider.start_task

        def propose(task, bot, history, emit):
            self.write('POST', f'/v1/tasks/{task["id"]}/replies', {'text': followup})
            original_start(task, bot, history, emit)

        self.provider.start_task = propose
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][0]
        answer = {'choice': choice, 'revision': decision['revision']}
        if choice == 'modify':
            answer['correction'] = 'Keep the report short.'
        self.write('POST', f'/v1/decisions/{decision["id"]}/answer', answer)
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.provider.resume_task = lambda task, bot, history, decision, emit: emit('completed', {'summary': 'Done.'})
        self.service._run_task(task['id'])
        received, results = [], []

        def follow(task, bot, history, emit):
            received.append(task)
            source = task['_bot_creation_sources'][0]['id']
            results.append(emit('bot_create', {'source_message_id': source, 'name': 'Scout',
                                               'role': 'Research', 'instructions': 'Check sources.'}))
            results.append(emit('bot_delete', {'source_message_id': source, 'target_bot_id': peer['id'],
                                               'expected_name': peer['name']}))
            emit('completed', {'summary': 'Done.'})

        self.provider.start_task = follow
        self.service._run_task(task['id'])
        self.assertEqual(len(received), 1)
        for field in ('_bot_creation_sources', '_bot_deletion_sources'):
            self.assertEqual([source['text'] for source in received[0][field]], [followup])
        self.assertEqual([result['status'] for result in results], ['created', 'deleted'])

    def test_approval_preserves_queued_bot_requests(self):
        self.check_file_answer_preserves_queued_bot_requests('approve')

    def test_modification_preserves_queued_bot_requests(self):
        self.check_file_answer_preserves_queued_bot_requests('modify')

    def check_native_correction_can_manage_bots(self, approval):
        bot, peer = self.bot('Writer'), self.bot('Old reader')
        task = self.send(bot, 'Choose the next action.')
        correction = 'Create Scout for research and delete Old reader.'

        def propose(task, bot, history, emit):
            call = emit('native_call', {'name': 'terminal' if approval else 'coconut_question', 'arguments': {}})
            emit('native_approval' if approval else 'native_question',
                 {'receipt_id': call['id'], 'title': 'Continue?', 'options': [{'id': 'yes', 'label': 'Continue'}]})

        self.provider.start_task = propose
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][0]
        key = str(uuid4())
        route = f'/v1/decisions/{decision["id"]}/answer'
        answer = {'choice': 'modify', 'correction': correction, 'revision': decision['revision']}
        self.write('POST', route, answer, key)
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.write('POST', route, answer, key)
        sources, results = [], []

        def resume(task, bot, history, decision, emit):
            sources.extend(task['_bot_creation_sources'])
            source = next(source['id'] for source in sources if source['text'] == correction)
            results.append(emit('bot_create', {'source_message_id': source, 'name': 'Scout',
                                               'role': 'Research', 'instructions': 'Check sources.'}))
            results.append(emit('bot_delete', {'source_message_id': source, 'target_bot_id': peer['id'],
                                               'expected_name': peer['name']}))
            self.write('POST', f'/v1/tasks/{task["id"]}/replies', {'text': 'Thanks.'})
            emit('completed', {'summary': 'Done.'})

        self.provider.resume_task = resume
        self.service._run_task(task['id'])
        self.assertIn(correction, [source['text'] for source in sources])
        self.assertEqual([result['status'] for result in results], ['created', 'deleted'])
        corrections = [m for m in self.service._all('messages') if m['role'] == 'user' and m.get('decision_id') == decision['id']]
        self.assertEqual([m['text'] for m in corrections], [correction])
        received = []
        self.provider.start_task = lambda task, bot, history, emit: received.append(task)
        self.service._run_task(task['id'])
        self.assertEqual([s['text'] for s in received[0]['_bot_creation_sources']], ['Thanks.'])
        self.assertEqual(received[0]['_bot_creation_sources'], received[0]['_bot_deletion_sources'])

    def test_native_approval_correction_can_manage_bots_after_restart(self):
        self.check_native_correction_can_manage_bots(approval=True)

    def test_native_question_correction_can_manage_bots_after_restart(self):
        self.check_native_correction_can_manage_bots(approval=False)

    def test_restart_bots_preserves_pending_work_and_context_until_completion(self):
        bot = self.bot('Writer')
        task = self.send(bot, 'Keep the blue label and choose a format.')
        epochs = []

        def propose(task, bot, history, emit):
            epochs.append(task['_conversation_epoch'])
            emit('native_question', {'title': 'Which format?', 'options': [{'id': 'short', 'label': 'Short'}]})

        self.provider.start_task = propose
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][0]
        self.write('POST', '/v1/bots/restart')
        new_epoch = self.service._setting('conversation_epoch')
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.write('POST', f'/v1/decisions/{decision["id"]}/context', {'question': 'Which label did I choose?'})
        context = self.service._all('contexts')[0]
        self.provider.answer_context = lambda task, bot, history, question, emit: epochs.append(task['_conversation_epoch'])
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=2)
        self.write('POST', f'/v1/decisions/{decision["id"]}/answer', {'option_id': 'short', 'revision': decision['revision']})

        def resume(task, bot, history, decision, emit):
            epochs.append(task['_conversation_epoch'])
            self.write('POST', f'/v1/tasks/{task["id"]}/replies', {'text': 'Start the next report.'})
            emit('completed', {'summary': 'Done.'})

        self.provider.resume_task = resume
        self.service._run_task(task['id'])
        self.provider.start_task = lambda task, bot, history, emit: epochs.append(task['_conversation_epoch'])
        self.service._run_task(task['id'])
        self.assertEqual(epochs, [None, None, None, new_epoch])

    def test_restart_bots_preserves_the_epoch_of_an_interrupted_turn(self):
        bot = self.bot('Writer')
        task = self.send(bot, 'Finish the blue report.')
        epochs = []
        self.provider.start_task = lambda task, bot, history, emit: (epochs.append(task['_conversation_epoch']), self.service.stopping.set())
        self.service._run_task(task['id'])
        self.write('POST', '/v1/bots/restart')
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.provider.start_task = lambda task, bot, history, emit: epochs.append(task['_conversation_epoch'])
        self.service._run_task(task['id'])
        self.assertEqual(epochs, [None, None])

    def test_context_keeps_its_epoch_after_the_task_advances_to_new_input(self):
        bot = self.bot('Writer')
        task = self.send(bot, 'Write the report.')
        original_start = self.provider.start_task

        def propose(task, bot, history, emit):
            self.write('POST', f'/v1/tasks/{task["id"]}/replies', {'text': 'Prepare the next report.'})
            original_start(task, bot, history, emit)

        self.provider.start_task = propose
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][0]
        self.write('POST', f'/v1/decisions/{decision["id"]}/context', {'question': 'Explain this report.'})
        context = self.service._all('contexts')[0]
        self.write('POST', '/v1/bots/restart')
        self.write('POST', f'/v1/decisions/{decision["id"]}/answer', {'choice': 'reject', 'revision': decision['revision']})
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        epochs = []
        self.provider.answer_context = lambda task, bot, history, question, emit: epochs.append(task['_conversation_epoch'])
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=2)
        self.assertEqual(epochs, [None])

    def test_large_accepted_text_events_remain_complete_after_restart(self):
        bot = self.bot('Writer')
        task = self.send(bot, 'Explain the result.')
        first, second = 'Result. ' * 4000, 'Details. ' * 4000

        def start(task, bot, history, emit):
            emit('text_delta', {'text': first})
            emit('text_delta', {'text': second})
            emit('completed', {'summary': 'Done.'})

        self.provider.start_task = start
        self.service._run_task(task['id'])
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        response = next(m for m in self.service.snapshot()['messages'] if m['role'] == 'assistant' and m['kind'] == 'text')
        self.assertEqual(response['text'], first + second)

    def test_large_context_answers_remain_complete(self):
        bot = self.bot('Writer')
        task = self.send(bot, 'Write the report.')
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][0]
        self.write('POST', f'/v1/decisions/{decision["id"]}/context', {'question': 'Explain the details.'})
        context = self.service._all('contexts')[0]
        answer = 'Detailed answer. ' * 2000
        self.provider.answer_context = lambda task, bot, history, question, emit: emit('text_delta', {'text': answer})
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=2)
        response = next(m for m in self.service.snapshot()['messages'] if m['role'] == 'assistant' and m['kind'] == 'text')
        self.assertEqual(response['text'], answer)

    def test_bot_recalls_all_its_threads_without_peer_private_conversation(self):
        first, second = self.bot('First'), self.bot('Second')
        old = self.send(first, 'A private blue label')
        self.service._run_task(old['id'])
        self.send(second, 'A private orange label')
        current = self.send(first, 'Recall the earlier label')
        self.service._run_task(current['id'])
        history = self.provider.starts[-1][2]
        self.assertIn('A private blue label', str(history))
        self.assertNotIn('A private orange label', str(history))

    def test_restart_is_idempotent_and_applies_to_all_bots_without_losing_state(self):
        first, second = self.bot('First'), self.bot('Second')
        pending = self.send(first, 'Write a report')
        self.service._run_task(pending['id'])
        queued = self.send(second, 'Read the result')
        before = self.service.snapshot()
        key = str(uuid4())
        receipt = self.write('POST', '/v1/bots/restart', key=key)
        epoch = self.service._setting('conversation_epoch')
        self.assertEqual(self.write('POST', '/v1/bots/restart', key=key), receipt)
        self.assertEqual(self.service._setting('conversation_epoch'), epoch)
        after = self.service.snapshot()
        for field in ('messages', 'decisions', 'bots', 'routines'):
            self.assertEqual(after[field], before[field])
        self.service._run_task(queued['id'])
        self.assertEqual(self.provider.starts[-1][0]['_conversation_epoch'], epoch)
        again = self.send(first, 'Read again')
        self.service._run_task(again['id'])
        self.assertEqual(self.provider.starts[-1][0]['_conversation_epoch'], epoch)
        self.assertEqual(self.provider.cancelled, [])
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.assertEqual(self.service._setting('conversation_epoch'), epoch)
