import base64
import tempfile
import unittest
from uuid import uuid4

from coconut.service import Service, ServiceError
from test_service import ControlledProvider


class MainConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.main = self.write('POST', '/v1/bots', {'name': 'Coconut'})
        self.worker = self.write('POST', '/v1/bots', {'name': 'Builder'})
        self.write('PUT', '/v1/settings/conversation', {'bot_id': self.main['id']})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data, key=None):
        return self.service.mutate(method, path, data, key or str(uuid4()), 'phone')

    def source(self):
        task = self.write('POST', f"/v1/bots/{self.worker['id']}/messages", {'text': 'Check the form', 'mode': 'queue'})
        self.service._run_task(task['id'])
        message = next(m for m in self.service._all('messages') if m.get('task_id') == task['id'] and m['role'] == 'assistant')
        return task, message

    def test_quote_runs_on_original_worker_and_returns_to_main_chat(self):
        original, message = self.source()
        unrelated = self.write('POST', f"/v1/bots/{self.main['id']}/messages", {'text': 'An unrelated question', 'mode': 'queue'})
        key = str(uuid4())
        path = f"/v1/bots/{self.main['id']}/messages"
        data = {'text': 'What remains?', 'mode': 'auto', 'reply_to_message_id': message['id']}
        reply = self.write('POST', path, data, key)
        self.assertEqual(reply, self.write('POST', path, data, key))
        self.assertEqual(reply['bot_id'], self.worker['id'])
        self.assertEqual(reply['parent_task_id'], original['id'])
        self.assertEqual(self.service._get('tasks', unrelated['id'])['status'], 'queued')
        self.service._run_task(reply['id'])
        task, bot, history = self.provider.starts[-1]
        self.assertEqual(bot['id'], self.worker['id'])
        request = next(m for m in history if m['task_id'] == reply['id'] and m['role'] == 'user')
        self.assertEqual(request['bot_id'], self.main['id'])
        self.assertEqual(request['reply_to']['message_id'], message['id'])
        self.assertEqual(request['reply_to']['bot_id'], message['bot_id'])
        answer = self.service._all('messages')[-1]
        self.assertEqual(answer['bot_id'], self.main['id'])
        self.assertEqual(answer['sender_bot_id'], self.worker['id'])
        self.assertEqual(answer['reply_to']['message_id'], request['id'])
        self.assertNotIn('exchange_recipient_bot_ids', answer)
        following = self.write('POST', path, {'text': 'And after that?', 'mode': 'auto', 'reply_to_message_id': answer['id']})
        self.assertEqual(following['bot_id'], self.worker['id'])
        self.assertEqual(following['parent_task_id'], reply['id'])
        self.assertEqual(self.service._get('tasks', unrelated['id'])['status'], 'queued')

    def test_quote_carries_main_chat_attachment_into_worker_continuation(self):
        _, message = self.source()
        artifact = self.write('POST', '/v1/attachments', {'bot_id': self.main['id'], 'filename': 'notes.txt',
            'content_base64': base64.b64encode(b'Check this detail').decode()})
        reply = self.write('POST', f"/v1/bots/{self.main['id']}/messages", {'text': 'Use these notes',
            'reply_to_message_id': message['id'], 'attachment_ids': [artifact['id']]})
        self.service._run_task(reply['id'])
        task, _, _ = self.provider.starts[-1]
        self.assertEqual(task['attachments'][0]['id'], artifact['id'])
        attachment = next(m for m in self.service._task_messages(reply['id']) if m.get('artifact_id') == artifact['id'])
        self.assertEqual(attachment['bot_id'], self.main['id'])
        self.assertEqual(self.service._all('messages')[-1]['bot_id'], self.main['id'])

    def test_accepted_quote_keeps_destination_after_main_chat_changes(self):
        _, message = self.source()
        reply = self.write('POST', f"/v1/bots/{self.main['id']}/messages", {'text': 'Continue', 'reply_to_message_id': message['id']})
        other = self.write('POST', '/v1/bots', {'name': 'New main chat'})
        self.write('PUT', '/v1/settings/conversation', {'bot_id': other['id']})
        self.service._run_task(reply['id'])
        answer = self.service._all('messages')[-1]
        self.assertEqual(answer['bot_id'], self.main['id'])
        self.assertEqual(answer['reply_to']['text'], 'Continue')

    def test_live_quote_starts_a_new_answer_in_main_chat(self):
        task, message = self.source()
        task = self.service._get('tasks', task['id'])
        task.update(status='working', _native_turn_id=str(uuid4()))
        self.service._put('tasks', task)
        self.service.emit(task['id'], 'text_delta', {'text': 'An earlier answer.'})
        earlier = self.service._all('messages')[-1]
        self.write('POST', f"/v1/bots/{self.main['id']}/messages", {'text': 'What remains?', 'reply_to_message_id': message['id']})
        request = self.service._all('messages')[-1]
        self.service.emit(task['id'], 'steering_consumed', {'turn_id': task['_native_turn_id'], 'message_ids': [request['id']]})
        self.service.emit(task['id'], 'text_delta', {'text': 'Here is what remains.'})
        answer = self.service._all('messages')[-1]
        self.assertEqual(answer['bot_id'], self.main['id'])
        self.assertEqual(answer['reply_to']['message_id'], request['id'])
        self.assertEqual(answer['text'], 'Here is what remains.')
        self.assertFalse(self.service._get('messages', earlier['id'])['streaming'])
        self.assertEqual(self.service._get('messages', earlier['id'])['text'], 'An earlier answer.')

    def test_later_input_from_original_conversation_replaces_quote_destination(self):
        task, message = self.source()
        original_chat = self.write('POST', '/v1/bots', {'name': 'Original conversation'})
        task = self.service._get('tasks', task['id'])
        task.update(status='working', conversation_bot_id=original_chat['id'], _native_turn_id=str(uuid4()))
        self.service._put('tasks', task)
        self.write('POST', f"/v1/bots/{self.main['id']}/messages", {'text': 'What remains?', 'reply_to_message_id': message['id']})
        quote = self.service._all('messages')[-1]
        self.service.emit(task['id'], 'steering_consumed', {'turn_id': task['_native_turn_id'], 'message_ids': [quote['id']]})
        self.write('POST', f"/v1/tasks/{task['id']}/replies", {'text': 'Return here instead'})
        request = self.service._all('messages')[-1]
        self.service.emit(task['id'], 'steering_consumed', {'turn_id': task['_native_turn_id'], 'message_ids': [request['id']]})
        self.service.emit(task['id'], 'text_delta', {'text': 'Back in the original conversation.'})
        self.assertEqual(self.service._all('messages')[-1]['bot_id'], original_chat['id'])

    def test_main_selection_is_durable_and_cannot_be_deleted_or_archived(self):
        self.assertEqual(self.service.snapshot()['conversation_bot_id'], self.main['id'])
        self.assertEqual(self.service.native_snapshot()['conversation_bot_id'], self.main['id'])
        for method, data in [('DELETE', {}), ('PATCH', {'archived': True})]:
            with self.assertRaises(ServiceError):
                self.write(method, f"/v1/bots/{self.main['id']}", data)
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.assertEqual(self.service.snapshot()['conversation_bot_id'], self.main['id'])

    def test_other_bots_cannot_quote_across_conversations(self):
        _, message = self.source()
        other = self.write('POST', '/v1/bots', {'name': 'Other'})
        with self.assertRaises(ServiceError):
            self.write('POST', f"/v1/bots/{other['id']}/messages", {'text': 'Reply', 'reply_to_message_id': message['id']})

    def test_quote_rejects_changed_targets(self):
        task, message = self.source()
        for flag in ['streaming', '_hidden', '_deleted']:
            changed = dict(message, **{flag: True})
            self.service._put('messages', changed)
            with self.assertRaises(ServiceError):
                self.write('POST', f"/v1/bots/{self.main['id']}/messages", {'text': 'Reply', 'reply_to_message_id': message['id']})
            self.service._put('messages', message)

    def test_quote_does_not_approve_a_pending_decision(self):
        task = self.write('POST', f"/v1/bots/{self.worker['id']}/messages", {'text': 'Write a report', 'mode': 'queue'})
        with self.service.transaction():
            message = self.service._message(self.worker['id'], 'I will prepare the report.', task_id=task['id'])
        self.service._run_task(task['id'])
        decision = self.service._all('decisions')[-1]
        with self.assertRaises(ServiceError):
            self.write('POST', f"/v1/bots/{self.main['id']}/messages", {'text': 'Go ahead', 'reply_to_message_id': message['id']})
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_queued_quote_keeps_owner_while_main_chat_stays_independent(self):
        original, message = self.source()
        task = self.service._get('tasks', original['id'])
        task['status'] = 'waiting'
        self.service._put('tasks', task)
        reply = self.write('POST', f"/v1/bots/{self.main['id']}/messages", {'text': 'Continue this', 'reply_to_message_id': message['id']})
        self.assertEqual(reply['id'], original['id'])
        separate = self.write('POST', f"/v1/bots/{self.main['id']}/messages", {'text': 'An unrelated question', 'mode': 'auto'})
        self.assertEqual(separate['bot_id'], self.main['id'])
        self.assertNotEqual(separate['id'], original['id'])
        self.assertIsNone(separate['parent_task_id'])


if __name__ == '__main__':
    unittest.main()
