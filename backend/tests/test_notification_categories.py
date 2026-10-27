import base64
import json
import tempfile
import unittest
from uuid import uuid4
from coconut.service import Service, ServiceError
from coconut.push_relay import notification_alert
from test_push import RecordingTransport
from test_push_relay import EncryptedTransport
from test_service import ControlledProvider


class NotificationCategoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.device = self.service.pair({'code': self.service.pairing_code()})['device_id']
        self.transport = RecordingTransport()
        self.service.push.transport = self.transport
        self.preferences()
        self.bot = self.write('POST', '/v1/bots', {'name': 'Desk'})

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), self.device)

    def preferences(self, level='grouped', messages=True):
        return self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True,
            preferences=dict(level=level, messages=messages)))

    def task(self, text='Hello'):
        return self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text=text, mode='queue'))

    def message(self, task, text='Finished', **values):
        with self.service.transaction():
            return self.service._message(self.bot['id'], text, task_id=task['id'], **values)

    def flush(self):
        self.service.db.execute('UPDATE push_items SET created=created-900')
        self.service.db.execute('UPDATE push_outbox SET not_before=0,created=created-900')
        while self.service.push.deliver_one():
            pass

    def test_finished_message_sends_once_without_completion_duplicate(self):
        task = self.task()
        self.service._run_task(task['id'])
        self.flush()
        self.assertEqual(len(self.transport.calls), 1)
        payload = self.transport.calls[0][2]['coconut']
        self.assertEqual(payload['kind'], 'message')
        message = self.service._get('messages', payload['message_id'])
        self.assertFalse(message['streaming'])
        self.service._event('message.updated', message, self.bot['id'], task['id'])
        self.flush()
        self.assertEqual(len(self.transport.calls), 1)

    def test_reacting_to_a_message_created_while_muted_does_not_notify_it(self):
        self.preferences(messages=False)
        task = self.task()
        self.service._run_task(task['id'])
        message = next(m for m in self.service._all('messages') if m['task_id'] == task['id'] and m['role'] == 'assistant')
        self.preferences(messages=True)
        self.write('POST', f'/v1/messages/{message["id"]}/reactions', dict(emoji='👍', active=True))
        self.flush()
        self.assertEqual(self.transport.calls, [])

    def test_finished_reply_to_a_card_question_notifies(self):
        task = self.task()
        task.update(source_task_id=str(uuid4()), source_bot_id=str(uuid4()))
        self.service._put('tasks', task)
        reply = self.message(task, 'It only changes the local file.', decision_id=str(uuid4()))
        self.flush()
        self.assertEqual(len(self.transport.calls), 1)
        target = self.transport.calls[0][2]['coconut']
        self.assertEqual(target['message_id'], reply['id'])
        self.assertIsNone(target['decision_id'])

    def test_messages_toggle_and_only_urgent_level(self):
        self.preferences(messages=False)
        self.service._run_task(self.task()['id'])
        self.service._run_task(self.task('Write report')['id'])
        self.flush()
        self.assertEqual([c[2]['coconut']['kind'] for c in self.transport.calls], ['decision'])
        self.preferences(level='urgent_only')
        self.service._run_task(self.task('Write another report')['id'])
        self.service._run_task(self.task()['id'])
        self.flush()
        self.assertEqual(len(self.transport.calls), 1)

    def test_older_apps_preferences_map_to_a_level(self):
        for old, new in ((dict(decisions=True, messages=False), dict(level='grouped', messages=False)),
                         (dict(decisions=False, messages=True), dict(level='urgent_only', messages=True))):
            self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True, preferences=old))
            self.assertEqual(self.service.push.preferences(self.device), new)
            self.service.db.execute('UPDATE push_preferences SET data=?', (json.dumps(old),))
            self.service.close()
            self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
            stored = self.service.db.execute('SELECT data FROM push_preferences').fetchone()[0]
            self.assertEqual(json.loads(stored), new)
        for bad in (dict(level='loud', messages=True), dict(level='grouped'), dict(level='grouped', messages=1),
                    dict(decisions=True, messages=True, level='grouped')):
            with self.subTest(bad=bad), self.assertRaises(ServiceError):
                self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True, preferences=bad))

    def test_disabling_discards_queued_messages_without_replaying_on_enable(self):
        self.message(self.task())
        self.preferences(messages=False)
        self.preferences(messages=True)
        self.flush()
        self.assertEqual(self.transport.calls, [])

    def test_streams_hidden_messages_and_peer_exchanges_do_not_notify(self):
        task = self.task()
        for values in ({'role': 'user'}, {'kind': 'activity'}, {'streaming': True}, {'recipient_bot_ids': [str(uuid4())]}):
            self.message(task, **values)
        hidden = self.message(task, streaming=True)
        hidden.update(_hidden=True, streaming=False)
        self.service._put('messages', hidden)
        self.service._event('message.updated', hidden, self.bot['id'], task['id'])
        task.update(source_task_id=str(uuid4()), source_bot_id=str(uuid4()))
        self.service._put('tasks', task)
        self.message(task)
        self.flush()
        self.assertEqual(self.transport.calls, [])

    def test_return_to_user_notifies_and_keeps_exact_message_after_new_turn(self):
        task = self.task()
        task.update(source_task_id=str(uuid4()), source_bot_id=str(uuid4()), _is_return=True)
        self.service._put('tasks', task)
        first = self.message(task, 'First reply')
        second = self.message(task, 'Second reply')
        task.update(_native_turn_id=str(uuid4()), status='working')
        self.service._put('tasks', task)
        self.flush()
        # Both replies arrived together, so they notify once as a group.
        self.assertEqual([c[2]['coconut'].get('count') for c in self.transport.calls], [2])
        self.assertEqual(self.transport.calls[0][2]['aps']['alert']['body'], '2 messages are waiting')
        self.assertEqual(self.service.db.execute("SELECT message_id FROM push_items WHERE status='sent' ORDER BY created,rowid").fetchall(),
                         [(first['id'],), (second['id'],)])
        alert = notification_alert(self.service, {'kind': 'message', 'message_id': first['id']}, self.bot, task, None)
        self.assertEqual(alert, {'title': 'Desk', 'body': 'First reply'})

    def test_preferences_survive_restart_and_do_not_change_another_phone(self):
        self.preferences(messages=False)
        other = self.service.pair({'code': self.service.pairing_code()})['device_id']
        self.service.push.registration(other, dict(token='b' * 64, environment='development', enabled=True,
            preferences=dict(level='every', messages=True)))
        self.service.close()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.service.push.transport = self.transport
        self.service._run_task(self.task()['id'])
        self.flush()
        self.assertEqual([c[2]['coconut']['device_id'] for c in self.transport.calls], [other])

    def test_message_preview_and_target_stay_inside_encrypted_relay_payload(self):
        relay = EncryptedTransport()
        self.service.push.relay = relay
        route = dict(route_id='a' * 64, send_token='b' * 64, public_key=base64.b64encode(b'x' * 32).decode())
        self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True,
            preferences=dict(decisions=True, messages=True), relay=route))
        task = self.task()
        message = self.message(task, 'Private reply preview')
        self.flush()
        self.assertEqual(relay.sealed[0]['aps']['alert'], dict(title='Desk', body='Private reply preview'))
        self.assertEqual(relay.sealed[0]['coconut']['message_id'], message['id'])
        self.assertNotIn('Private reply', json.dumps(relay.sent))
        self.assertNotIn(message['id'], json.dumps(relay.sent))

    def test_rejects_non_boolean_preferences(self):
        with self.assertRaises(ServiceError):
            self.preferences(messages='false')
