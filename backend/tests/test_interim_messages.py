import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import Service, YieldForDecision, now
from test_service import ControlledProvider


class InterimMessageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.device = self.service.pair({'code': self.service.pairing_code()})['device_id']
        self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True,
                                               preferences=dict(decisions=True, messages=True)))
        self.bot = self.write('POST', '/v1/bots', {'name': 'Ada'})
        self.task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text='Check the test', mode='queue'))
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), self.device)

    def commentary(self, text='I will check the failing test.'):
        self.service.emit(self.task['id'], 'interim_message', {'text': text})

    def replies(self):
        return [message for message in self.service.snapshot()['messages']
                if message['task_id'] == self.task['id'] and message['role'] == 'assistant']

    def test_commentary_is_visible_while_the_task_stays_working(self):
        cursor = self.service.snapshot()['cursor']
        self.commentary()
        message, = self.replies()
        self.assertEqual((message['kind'], message['streaming']), ('text', False))
        self.assertEqual(message['text'], 'I will check the failing test.')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'working')
        events = self.service.events(cursor)['events']
        self.assertTrue(any(event['type'] == 'message.created' and event['data']['id'] == message['id'] for event in events))
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM push_outbox').fetchone()[0], 0)

    def test_final_reply_stays_separate_and_can_notify(self):
        self.commentary()
        self.service.emit(self.task['id'], 'text_delta', {'text': 'The test passes.'})
        self.service.emit(self.task['id'], 'completed', {'summary': 'Complete.'})
        self.assertEqual([message['text'] for message in self.replies()],
                         ['I will check the failing test.', 'The test passes.'])
        final = self.replies()[-1]
        with self.service.transaction():
            saved = self.service._get('messages', final['id'])
            saved['streaming'] = False
            self.service._put('messages', saved)
            self.service.push.record('message.completed', saved, self.bot['id'], self.task['id'])
        self.assertEqual(self.service.db.execute("SELECT message_id FROM push_outbox WHERE kind='message'").fetchall(),
                         [(final['id'],)])

    def test_failure_keeps_commentary_and_removes_unfinished_text(self):
        self.commentary()
        self.service.emit(self.task['id'], 'text_delta', {'text': 'Unverified success'})
        self.service.emit(self.task['id'], 'failed', {'summary': 'The check failed.'})
        self.assertEqual([message['text'] for message in self.replies()],
                         ['I will check the failing test.', 'The check failed.'])

    def test_final_confirmation_promotes_the_last_interim_message_once(self):
        self.commentary()
        self.commentary('The test passes.')
        final = self.replies()[-1]
        self.service.emit(self.task['id'], 'text_delta', {'text': 'The test passes.', 'finalize_interim': True})
        with patch.object(self.service, '_finish_assignment_turn') as finish:
            self.service.emit(self.task['id'], 'completed', {'summary': 'Complete.'})
        self.assertEqual([message['text'] for message in self.replies()],
                         ['I will check the failing test.', 'The test passes.'])
        self.assertEqual(self.replies()[-1]['id'], final['id'])
        self.assertFalse(self.replies()[-1]['streaming'])
        self.assertNotIn('_interim', self.service._get('messages', final['id']))
        self.assertEqual(finish.call_args.args[1:], ('The test passes.', 'completed'))
        self.assertEqual(self.service.db.execute("SELECT message_id FROM push_outbox WHERE kind='message'").fetchall(),
                         [(final['id'],)])

    def test_final_confirmation_does_not_replace_different_commentary(self):
        self.commentary()
        previous = self.replies()[-1]
        self.service.emit(self.task['id'], 'text_delta', {'text': 'The test passes.', 'finalize_interim': True})
        self.assertEqual([message['text'] for message in self.replies()],
                         ['I will check the failing test.', 'The test passes.'])
        self.assertNotEqual(self.replies()[-1]['id'], previous['id'])
        self.assertTrue(self.service._get('messages', previous['id'])['_interim'])

    def test_commentary_does_not_become_the_assignment_result(self):
        self.commentary()
        with patch.object(self.service, '_finish_assignment_turn') as finish:
            self.service.emit(self.task['id'], 'completed', {'summary': 'Complete.'})
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1:], ('Complete.', 'completed'))

    def test_empty_final_does_not_return_a_previous_turn_result(self):
        with self.service.transaction():
            self.service._message(self.bot['id'], 'The earlier check passes.', task_id=self.task['id'])
            task = self.service._get('tasks', self.task['id'])
            task['_turn_started_at'] = now()
            self.service._put('tasks', task)
        self.commentary()
        with patch.object(self.service, '_finish_assignment_turn') as finish:
            self.service.emit(self.task['id'], 'completed', {'summary': 'Complete.'})
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1:], ('Complete.', 'completed'))

    def test_final_confirmation_does_not_promote_previous_turn_commentary(self):
        self.commentary('The test passes.')
        previous = self.replies()[-1]
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['_turn_started_at'] = now()
            self.service._put('tasks', task)
        self.service.emit(self.task['id'], 'text_delta', {'text': 'The test passes.', 'finalize_interim': True})
        self.assertEqual(len(self.replies()), 2)
        self.assertNotEqual(self.replies()[-1]['id'], previous['id'])
        self.assertTrue(self.service._get('messages', previous['id'])['_interim'])

    def test_cancellation_refuses_late_commentary(self):
        self.commentary()
        with self.service.transaction():
            self.service._cancel(self.service._get('tasks', self.task['id']), [])
        with self.assertRaises(YieldForDecision):
            self.commentary('A late update must not appear.')
        self.assertEqual([message['text'] for message in self.replies()], ['I will check the failing test.'])

    def test_restart_keeps_completed_commentary_once(self):
        self.commentary()
        before, = self.replies()
        self.service.close()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        after, = self.replies()
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(after['text'], before['text'])
        self.assertFalse(after['streaming'])

    def test_empty_or_silent_commentary_creates_no_message(self):
        for text in ('', '  ', '[SILENT]', None):
            self.commentary(text)
        self.assertEqual(self.replies(), [])

    def test_silent_native_job_does_not_publish_completed_commentary(self):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task.update(status='queued', _native_job={'id': 'scheduled-check'})
            self.service._put('tasks', task)
        before = self.service.snapshot()['messages']
        bot = self.service._get('bots', self.bot['id'])
        def run(task, bot, history, emit):
            emit('interim_message', {'text': 'I will check the scheduled task.'})
            emit('completed', {'summary': 'Complete.', 'silent_response': True, 'routine_silent': True})
        self.service.provider.start_task = run
        self.service._run_task(self.task['id'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')
        self.assertTrue(self.service._get('tasks', self.task['id'])['_routine_silent'])
        self.assertEqual(self.service.snapshot()['messages'], before)
        after = self.service._get('bots', self.bot['id'])
        self.assertEqual(after['last_message'], bot['last_message'])
        self.assertEqual(after['unread_count'], bot['unread_count'])
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM push_outbox').fetchone()[0], 0)

    def test_native_job_keeps_its_normal_final_reply_and_alert(self):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task.update(status='queued', _native_job={'id': 'scheduled-check'})
            self.service._put('tasks', task)
        def run(task, bot, history, emit):
            emit('interim_message', {'text': 'I will check the scheduled task.'})
            emit('interim_message', {'text': 'The scheduled check finds a failure.'})
            emit('text_delta', {'text': 'The scheduled check finds a failure.', 'finalize_interim': True})
            emit('completed', {'summary': 'Complete.'})
        self.service.provider.start_task = run
        self.service._run_task(self.task['id'])
        reply, = self.replies()
        self.assertEqual(reply['text'], 'The scheduled check finds a failure.')
        self.assertFalse(reply['streaming'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')
        self.assertEqual(self.service.db.execute("SELECT message_id FROM push_outbox WHERE kind='message'").fetchall(),
                         [(reply['id'],)])


if __name__ == '__main__':
    unittest.main()
