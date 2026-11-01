import unittest
from coconut.service import Service
import test_service_pr as fixtures


class SilentResponseTests(unittest.TestCase):
    setUp = fixtures.PRServiceTests.setUp
    tearDown = fixtures.PRServiceTests.tearDown
    write = fixtures.PRServiceTests.write

    def test_silent_completion_finishes_without_message_or_unread(self):
        before = self.service.snapshot()['messages']
        unread = self.service._get('bots', self.bot['id'])['unread_count']
        self.provider.start_task = lambda task, bot, history, emit: emit('completed', dict(summary='Complete.', silent_response=True))
        self.service._run_task(self.task['id'])
        self.assertEqual(self.service.snapshot()['messages'], before)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')
        self.assertEqual(self.service._get('bots', self.bot['id'])['unread_count'], unread)

    def test_withdrawn_draft_restores_preview_and_unread(self):
        bot = self.service._get('bots', self.bot['id'])
        def output(task, bot, history, emit):
            emit('text_delta', {'text': 'Draft reply'})
            emit('text_delta', {'text': '', 'replace': True})
            emit('completed', {'summary': 'Complete.', 'silent_response': True})
        self.provider.start_task = output
        self.service._run_task(self.task['id'])
        after = self.service._get('bots', self.bot['id'])
        self.assertEqual(after['last_message'], bot['last_message'])
        self.assertEqual(after['unread_count'], bot['unread_count'])
        self.assertNotIn('Draft reply', [m['text'] for m in self.service.snapshot()['messages']])

    def test_worker_disconnection_withdraws_partial_reply(self):
        def output(task, bot, history, emit):
            emit('text_delta', {'text': 'Draft reply'})
            raise ConnectionError('The worker disconnected.')
        self.provider.start_task = output
        self.service._run_task(self.task['id'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'failed')
        self.assertNotIn('Draft reply', [m['text'] for m in self.service.snapshot()['messages']])
        self.assertNotEqual(self.service._get('bots', self.bot['id'])['last_message'], 'Draft reply')

    def test_service_restart_withdraws_interrupted_text(self):
        bot = self.service._get('bots', self.bot['id'])
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.service.emit(task['id'], 'text_delta', {'text': 'Interrupted reply'})
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.tracker)
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')
        self.assertNotIn('Interrupted reply', [m['text'] for m in self.service.snapshot()['messages']])
        self.assertEqual(self.service._get('bots', bot['id'])['last_message'], bot['last_message'])
        self.assertEqual(self.service._get('bots', bot['id'])['unread_count'], bot['unread_count'])

    def test_delegated_stream_only_updates_its_own_conversation(self):
        peer = self.write('POST', '/v1/bots', {'name': 'Worker', 'role': ''})
        bot = self.service._get('bots', self.bot['id'])
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task.update(status='working', bot_id=peer['id'], source_bot_id=bot['id'])
            self.service._put('tasks', task)
        for text in ('Draft ', 'reply'):
            self.service.emit(task['id'], 'text_delta', {'text': text})
        self.assertEqual(self.service._get('bots', bot['id'])['last_message'], bot['last_message'])
        self.assertEqual(self.service._get('bots', peer['id'])['last_message'], 'Draft reply')
        self.service.emit(task['id'], 'text_delta', {'text': '', 'replace': True})
        self.assertEqual(self.service._get('bots', peer['id'])['last_message'], '')

    def test_provider_marker_does_not_create_a_bubble_or_fallback(self):
        before = self.service.snapshot()['messages']
        def output(task, bot, history, emit):
            emit('text_delta', {'text': '[SILENT]'})
            emit('completed', {'summary': 'Complete.'})
        self.provider.start_task = output
        self.service._run_task(self.task['id'])
        self.assertEqual(self.service.snapshot()['messages'], before)

    def test_streamed_marker_with_more_text_remains_complete(self):
        for chunks in (('[SILENT]', ' means no reply.'), ('[SI', 'LENT]', ' means no reply.'), ('[SI',)):
            with self.subTest(chunks=chunks):
                self.task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Explain the marker', 'mode': 'queue'})
                before = {m['id'] for m in self.service.snapshot()['messages']}
                def output(task, bot, history, emit):
                    for text in chunks:
                        emit('text_delta', {'text': text})
                    emit('completed', {'summary': 'Complete.'})
                self.provider.start_task = output
                self.service._run_task(self.task['id'])
                texts = [m['text'] for m in self.service.snapshot()['messages'] if m['id'] not in before]
                self.assertEqual(texts, [''.join(chunks)])

    def test_fragmented_silence_has_no_unread_or_preview_effect(self):
        before = self.service.snapshot()['messages']
        bot = self.service._get('bots', self.bot['id'])
        def output(task, bot, history, emit):
            for text in ('[SI', 'LENT', ']'):
                emit('text_delta', {'text': text})
            emit('completed', {'summary': 'Complete.'})
        self.provider.start_task = output
        self.service._run_task(self.task['id'])
        self.assertEqual(self.service.snapshot()['messages'], before)
        after = self.service._get('bots', self.bot['id'])
        self.assertEqual(after['last_message'], bot['last_message'])
        self.assertEqual(after['unread_count'], bot['unread_count'])

    def test_normal_text_and_human_literal_remain_visible(self):
        with self.service.transaction():
            user = self.service._message(self.bot['id'], '[SILENT]', role='user', human_authored=True)
            explanation = self.service._message(self.bot['id'], 'The marker is [SILENT].')
        messages = self.service.snapshot()['messages']
        self.assertIn(user['id'], [m['id'] for m in messages])
        self.assertIn(explanation['id'], [m['id'] for m in messages])

    def test_bot_marker_has_no_notification_or_unread_effect(self):
        before = self.service._get('bots', self.bot['id'])
        with self.service.transaction():
            hidden = self.service._message(self.bot['id'], ' [SILENT]\n', task_id=self.task['id'], streaming=False)
        self.assertNotIn(hidden['id'], [m['id'] for m in self.service.snapshot()['messages']])
        after = self.service._get('bots', self.bot['id'])
        self.assertEqual(after['unread_count'], before['unread_count'])
        self.assertEqual(after['last_message'], before['last_message'])

    def test_restart_hides_legacy_markers_but_keeps_audit_and_real_messages(self):
        with self.service.transaction():
            normal = self.service._message(self.bot['id'], 'The review finished.')
            old = self.service._message(self.bot['id'], 'legacy fixture', sender_bot_id=self.bot['id'], recipient_bot_ids=['peer'])
            old.update(text='[SILENT]')
            self.service._put('messages', old)
            bot = self.service._get('bots', self.bot['id'])
            bot['last_message'] = '[SILENT]'
            self.service._put('bots', bot)
            self.service.db.execute("DELETE FROM settings WHERE key='silent_responses_migrated'")
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.tracker)
        messages = self.service.snapshot()['messages']
        self.assertNotIn(old['id'], [m['id'] for m in messages])
        self.assertIn(normal['id'], [m['id'] for m in messages])
        self.assertEqual(self.service._get('messages', old['id'])['text'], '[SILENT]')
        self.assertNotEqual(self.service._get('bots', self.bot['id'])['last_message'], '[SILENT]')

    def test_failure_remains_visible_even_with_silence_flag(self):
        self.provider.start_task = lambda task, bot, history, emit: emit('failed', dict(summary='The export failed.', silent_response=True))
        self.service._run_task(self.task['id'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'failed')
        self.assertTrue(any(m['text'] == 'The export failed.' for m in self.service.snapshot()['messages']))


import test_notification_categories as notifications


class SilentNotificationTests(unittest.TestCase):
    setUp = notifications.NotificationCategoryTests.setUp
    tearDown = notifications.NotificationCategoryTests.tearDown
    write = notifications.NotificationCategoryTests.write
    preferences = notifications.NotificationCategoryTests.preferences
    task = notifications.NotificationCategoryTests.task

    def test_silence_creates_no_message_or_legacy_completion_notification(self):
        task = self.task()
        with self.service.transaction():
            self.service.db.execute('DELETE FROM push_preferences')
            stored = self.service._get('tasks', task['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
            self.service._message(self.bot['id'], '[SILENT]', task_id=task['id'])
        self.service.emit(task['id'], 'completed', dict(summary='Complete.', silent_response=True))
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM push_items').fetchone()[0], 0)
