import base64
import tempfile
import unittest
from uuid import uuid4

from turn_text import turn_input
from coconut.gateway_chat import gateway_turn_own
from coconut.service import Service
from test_service import ControlledProvider


class ConversationInputTests(unittest.TestCase):
    def setUp(self):
        self.root = str(uuid4())
        self.request = dict(operation='start', bot=dict(id=str(uuid4()), session_id=self.root),
                            task=dict(id='current', title='Truncated title'), history=[
                                self.message('old-user', 'earlier', 'user', 'Use the blue label.'),
                                self.message('old-answer', 'earlier', 'assistant', 'I will use blue.'),
                                self.message('current-user', 'current', 'user', 'Which label?'),
                                self.message('queued-user', 'queued', 'user', 'Do a different task later.')])

    def message(self, identity, task, role, text, **kwargs):
        return dict(id=identity, task_id=task, role=role, kind='text', text=text, **kwargs)

    def test_new_task_keeps_earlier_bot_conversation_and_ignores_later_queue(self):
        prompt, _ = turn_input(self.request)
        # The turn's own text is the current message. The notes of the turn follow it.
        self.assertEqual(gateway_turn_own(self.request), 'Which label?')
        self.assertTrue(prompt.startswith('Which label?'))
        self.assertNotIn('Do a different task later.', prompt)

    def test_all_queued_messages_enter_the_prompt_in_order(self):
        self.request['history'].append(self.message('follow-up', 'current', 'user', 'Keep it short.'))
        prompt, _ = turn_input(self.request)
        self.assertEqual(gateway_turn_own(self.request), 'Which label?\n\nKeep it short.')
        self.assertTrue(prompt.startswith('Which label?\n\nKeep it short.'))

    def test_background_work_text_has_its_audit_and_not_the_chat(self):
        self.request['task'].update(id='audit', _execution_session_id='audit', _turn_message_ids=[],
                                   _source_audit_prompt='Check the assigned launch issue.')
        prompt, _ = turn_input(self.request)
        self.assertIn('Check the assigned launch issue.', prompt)
        self.assertNotIn('Which label?', prompt)
        self.assertNotIn('Do a different task later.', prompt)

    def test_old_input_without_a_visibility_snapshot_does_not_infer_context_later(self):
        self.request['task']['_chat_session'] = True
        prompt, _ = turn_input(self.request)
        self.assertNotIn('I will use blue.', prompt)

    def test_explicit_reply_and_background_work_keep_their_own_context(self):
        self.request['task']['_chat_session'] = True
        self.request['task']['_visible_reply_contexts'] = {'current-user': dict(message_id='old-answer', text='I will use blue.')}
        self.request['history'][2]['reply_to'] = dict(message_id='quoted', text='A specific older reply.',
            author='Desk', role='assistant', author_bot_id=self.request['bot']['id'])
        prompt, _ = turn_input(self.request)
        self.assertIn('A specific older reply.', prompt)
        self.assertNotIn('I will use blue.', prompt)
        self.request['history'][2].pop('reply_to')
        self.request['task']['_execution_session_id'] = 'background-work'
        prompt, _ = turn_input(self.request)
        self.assertNotIn('I will use blue.', prompt)


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

    def test_chat_captures_visible_reply_before_a_queued_message_or_later_completion(self):
        self.provider.supports_native_outcomes = True
        bot = self.bot('Desk')
        other = self.bot('Developer')
        with self.service.transaction():
            visible = self.service._message(bot['id'], 'The finance-alert card remains pending.', sender_bot_id=bot['id'])
            self.service._message(bot['id'], ' \n ', sender_bot_id=bot['id'])
            for extra in [dict(_hidden=True), dict(_deleted=True), dict(streaming=True),
                          dict(exchange_recipient_bot_ids=[other['id']]), dict(kind='bot_exchange')]:
                row = self.service._message(bot['id'], 'Excluded reply.', sender_bot_id=bot['id'])
                self.service._put('messages', row | extra)
            self.service._message(other['id'], 'Another chat.', sender_bot_id=other['id'])
            partial = self.service._message(bot['id'], 'A Stripe answer still in progress.', sender_bot_id=bot['id'], streaming=True)
        task = self.send(bot, 'Incorrect there is no card pending')
        source = next(m for m in self.service._task_messages(task['id']) if m['role'] == 'user')
        self.assertEqual(source['_visible_reply_context']['message_id'], visible['id'])
        self.assertNotIn('_visible_reply_context', self.service._public(source))
        with self.service.transaction():
            partial.update(streaming=False, text='The Stripe card is ready.')
            self.service._put('messages', partial)
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.service._run_task(task['id'])
        sent_task, _, _ = self.provider.starts[-1]
        # The turn gets the reply that was visible when the person sent the message, not a later one.
        self.assertEqual(sent_task['_visible_reply_contexts'][source['id']]['text'], 'The finance-alert card remains pending.')

    def test_chat_skips_redundant_completion_but_keeps_completion_without_a_result(self):
        bot = self.bot('Desk')
        task = self.send(bot, 'Prepare a report.')
        with self.service.transaction():
            answer = self.service._message(bot['id'], 'The finance-alert card remains pending.', task_id=task['id'])
            self.service._message(bot['id'], 'The report is ready.', task_id=task['id'], kind='artifact')
            self.service._message(bot['id'], 'Done.', task_id=task['id'])
            self.assertEqual(self.service._last_visible_reply(bot['id'])['message_id'], answer['id'])
        other = self.send(bot, 'Finish a separate check.')
        with self.service.transaction():
            complete = self.service._message(bot['id'], 'Complete.', task_id=other['id'])
            self.assertEqual(self.service._last_visible_reply(bot['id'])['message_id'], complete['id'])

    def test_queued_replies_pass_each_arrival_snapshot_to_the_provider(self):
        self.provider.supports_native_outcomes = True
        bot = self.bot('Desk')
        with self.service.transaction():
            self.service._message(bot['id'], 'The finance card is pending.')
        task = self.send(bot, 'Incorrect there is no card pending')
        with self.service.transaction():
            self.service._message(bot['id'], 'The Stripe test is ready.')
        self.write('POST', f'/v1/tasks/{task["id"]}/replies', {'text': 'How long will that test take?'})
        self.service._run_task(task['id'])
        sent_task, _, _ = self.provider.starts[-1]
        # Each message keeps the reply that was visible when it arrived.
        self.assertEqual(sorted(context['text'] for context in sent_task['_visible_reply_contexts'].values()),
                         ['The Stripe test is ready.', 'The finance card is pending.'])

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
        self.provider.start_task = lambda task, bot, history, emit: captured.append((task, turn_input(dict(operation='start', task=task, history=history))[0]))
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
            prompt, _ = turn_input(dict(operation='start', task=task, history=history))
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
            prompt, _ = turn_input(dict(operation='start', task=task, history=history))
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
            prompts.append(turn_input(dict(operation='start', task=task, history=history))[0])
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
            turn_input(dict(operation='start', task=task, history=history))[0])
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
            received[bot['id']] = (turn_input(request)[0], gateway_turn_own(request))
            emit('completed', {'summary': 'Complete.'})

        self.provider.start_task = start
        for task_id in result['dispatched_task_ids']:
            self.service._run_task(task_id)
        for bot, expected in ((first, shared), (second, shared + '\n\nKeep it short.')):
            prompt, own = received[bot['id']]
            self.assertEqual(prompt, expected)
            # Hermes saves the same messages as the turn's own text.
            self.assertEqual(own, expected)

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
            turn_input(dict(operation='start', task=task, history=history))[0])
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
