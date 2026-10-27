import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from coconut.service import Service, ServiceError
from test_service import ControlledProvider


class RecordingTransport:
    configured = True

    def __init__(self, result=(200, 'Accepted')):
        self.result = result
        self.calls = []

    def send(self, *args):
        self.calls.append(args)
        return self.result


class PushTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.device = self.service.pair({'code': self.service.pairing_code()})['device_id']
        self.transport = RecordingTransport()
        self.service.push.transport = self.transport
        self.register()
        self.bot = self.write('POST', '/v1/bots', {'name': 'Private bot name', 'role': 'Research'})

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def write(self, method, path, data=None, key=None):
        return self.service.mutate(method, path, data or {}, key or str(uuid4()), self.device)

    def register(self, token='a' * 64, enabled=True):
        return self.write('PUT', '/v1/device/push', dict(token=token, environment='development', enabled=enabled))

    def run_task(self, text='Private task content'):
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text=text, mode='queue'))
        self.service._run_task(task['id'])
        return task

    def flush(self):
        with self.service.transaction():
            self.service.db.execute('UPDATE push_items SET created=created-900')
            self.service.db.execute('UPDATE push_outbox SET not_before=0,created=created-900')
        return self.service.push.deliver_one()

    def states(self):
        return self.service.db.execute('SELECT status,reason FROM push_outbox ORDER BY rowid').fetchall()

    def items(self):
        return self.service.db.execute('SELECT kind,status,reason FROM push_items ORDER BY created,rowid').fetchall()

    def test_badge_matches_visible_pending_inbox_and_clears(self):
        self.run_task('Write report')
        decision = self.service.snapshot()['decisions'][0]
        with self.service.transaction():
            for change in ({'_hidden': True}, {'_deleted': True}, {'_source_recheck': True}, {'status': 'completed'}):
                self.service._put('decisions', dict(decision, id=str(uuid4()), **change))
        self.assertEqual(self.service.push.inbox_badge_count(), 1)
        self.assertTrue(self.flush())
        self.assertNotIn('badge', self.transport.calls[-1][2]['aps'])
        with self.service.transaction():
            current = self.service._get('decisions', decision['id'])
            current['status'] = 'approved'
            self.service._put('decisions', current)
        self.assertEqual(self.service.push.inbox_badge_count(), 0)
        self.run_task('Another independent reply')
        self.assertTrue(self.flush())
        self.assertNotIn('badge', self.transport.calls[-1][2]['aps'])

    def test_completed_payload_is_private_and_routes_to_task(self):
        task = self.run_task()
        self.assertTrue(self.flush())
        token, environment, payload, _, _, _ = self.transport.calls[0]
        self.assertEqual(environment, 'development')
        self.assertEqual(payload['coconut']['task_id'], task['id'])
        self.assertEqual(payload['coconut']['device_id'], self.device)
        self.assertEqual(payload['aps']['thread-id'], task['thread_id'])
        self.assertNotIn('Private', json.dumps(payload))
        self.assertNotIn('independent', json.dumps(payload))
        self.assertEqual(self.states(), [('accepted', 'Accepted')])

    def test_pending_decision_survives_restart_and_sends_once(self):
        task = self.run_task('Write report')
        decision = self.service.snapshot()['decisions'][0]
        self.service.close()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.service.push.transport = self.transport
        self.flush()
        self.assertEqual(self.transport.calls[0][2]['coconut']['decision_id'], decision['id'])
        self.service.close()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.service.push.transport = self.transport
        self.assertFalse(self.flush())
        self.assertEqual(len(self.transport.calls), 1)

    def test_delegated_decision_links_to_the_bot_with_the_card(self):
        origin = self.write('POST', '/v1/bots', {'name': 'Origin'})
        task = self.run_task('Write report')
        decision = self.service.snapshot()['decisions'][0]
        with self.service.transaction():
            current = self.service._get('tasks', task['id'])
            current['conversation_bot_id'] = origin['id']
            current['source_bot_id'] = origin['id']
            self.service._put('tasks', current)
        self.assertTrue(self.flush())
        target = self.transport.calls[0][2]['coconut']
        self.assertEqual(target['bot_id'], decision['bot_id'])
        self.assertEqual(target['decision_id'], decision['id'])

    def test_mentioned_bot_decision_links_to_its_original_conversation(self):
        origin = self.write('POST', '/v1/bots', {'name': 'Origin'})
        task = self.write('POST', f'/v1/bots/{origin["id"]}/messages',
            dict(text='Write report', mode='queue', recipient_bot_ids=[self.bot['id']]))
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][0]
        self.assertTrue(self.flush())
        target = self.transport.calls[0][2]['coconut']
        self.assertEqual(target['bot_id'], origin['id'])
        self.assertEqual(target['decision_id'], decision['id'])

    def test_answered_decision_is_discarded_before_delivery(self):
        self.run_task('Write report')
        decision = self.service.snapshot()['decisions'][0]
        self.write('POST', f'/v1/decisions/{decision["id"]}/answer', dict(choice='reject', revision=decision['revision']))
        self.flush()
        self.assertEqual([call[2]['coconut']['kind'] for call in self.transport.calls], ['completed'])
        self.assertEqual(self.items()[0], ('decision', 'dropped', 'NoLongerRelevant'))

    def test_bot_preference_is_checked_again_at_delivery(self):
        self.run_task()
        self.write('PATCH', f'/v1/bots/{self.bot["id"]}', {'notifications': False})
        self.flush()
        self.assertEqual(self.transport.calls, [])

    def test_muted_bot_does_not_enqueue(self):
        self.write('PATCH', f'/v1/bots/{self.bot["id"]}', {'notifications': False})
        self.run_task()
        self.assertEqual(self.states(), [])

    def test_event_and_registration_retries_do_not_duplicate_delivery(self):
        task = self.run_task()
        with self.service.transaction():
            current = self.service._get('tasks', task['id'])
            self.service._event('task.updated', self.service._public(current), self.bot['id'], task['id'])
        self.register()
        self.assertEqual(len(self.items()), 1)
        self.flush()
        self.assertFalse(self.flush())
        self.assertEqual(len(self.transport.calls), 1)

    def test_network_retry_reuses_notification_id(self):
        self.run_task()
        self.transport.result = (0, 'TransportUnavailable')
        self.flush()
        self.assertEqual(self.states()[0][0], 'queued')
        self.transport.result = (200, 'Accepted')
        self.flush()
        self.assertEqual(self.transport.calls[0][3], self.transport.calls[1][3])
        self.assertEqual(self.states()[0][0], 'accepted')

    def test_invalid_token_removes_the_registration_until_the_app_registers_again(self):
        for result in ((410, 'Unregistered'), (400, 'BadDeviceToken'), (400, 'DeviceTokenNotForTopic')):
            self.register()
            self.run_task()
            self.transport.result = result
            self.flush()
            for table in ('push_devices', 'push_routes', 'push_preferences', 'push_schedules'):
                self.assertEqual(self.service.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0, (table, result))
            status = self.service.push_status(self.device)
            self.assertEqual((status['registered'], status['last_error']), (False, result[1]))
            self.assertIsNotNone(status['last_error_at'])
        self.register(token='b' * 64)
        self.assertTrue(self.service.push_status(self.device)['registered'])

    def test_other_failures_keep_the_registration_and_record_the_error(self):
        self.run_task()
        self.transport.result = (401, 'RelayRejected')
        self.flush()
        self.assertEqual(self.service.db.execute('SELECT enabled FROM push_devices').fetchone()[0], 1)
        self.assertEqual(self.service.push_status(self.device)['last_error'], 'RelayRejected')

    def test_status_shows_the_last_delivery_and_the_last_error(self):
        status = self.service.push_status(self.device)
        self.assertEqual({key: status[key] for key in ('registered', 'last_delivered_at', 'last_error', 'last_error_at')},
                         dict(registered=True, last_delivered_at=None, last_error=None, last_error_at=None))
        self.assertEqual(status['preferences'], dict(level='grouped', messages=False))
        self.run_task()
        self.transport.result = (503, 'ServiceUnavailable')
        self.flush()
        self.transport.result = (200, 'Accepted')
        self.flush()
        status = self.service.push_status(self.device)
        self.assertEqual(status['last_error'], 'ServiceUnavailable')
        self.assertTrue(status['last_delivered_at'].endswith('Z'))
        self.assertGreaterEqual(status['last_delivered_at'], status['last_error_at'])

    def test_registrations_without_a_pairing_or_a_recent_refresh_are_removed(self):
        other = self.service.pair({'code': self.service.pairing_code()})['device_id']
        self.service.push.registration(other, dict(token='b' * 64, environment='development', enabled=True))
        self.service.db.execute('DELETE FROM devices WHERE id=?', (other,))
        old = self.service.pair({'code': self.service.pairing_code()})['device_id']
        self.service.push.registration(old, dict(token='c' * 64, environment='development', enabled=True,
                                                 preferences=dict(level='every', messages=True)))
        self.service.db.execute('UPDATE push_devices SET updated=updated-31*86400 WHERE device_id=?', (old,))
        with self.service.transaction():
            self.service.push.plan(__import__('time').time())
        self.assertEqual(self.service.db.execute('SELECT device_id FROM push_devices').fetchall(), [(self.device,)])
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM push_preferences WHERE device_id<>?', (self.device,)).fetchone()[0], 0)
        self.assertEqual(self.service.push_status(old)['last_error'], 'NotRefreshed')
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM push_status WHERE device_id=?', (other,)).fetchone()[0], 0)

    def test_new_token_is_not_invalidated_by_old_attempt(self):
        self.run_task()
        def send(*args):
            self.register(token='b' * 64)
            return 410, 'Unregistered'
        self.transport.send = send
        self.flush()
        self.assertEqual(self.service.db.execute('SELECT enabled FROM push_devices').fetchone()[0], 1)

    def test_repair_same_token_does_not_send_twice(self):
        self.run_task()
        self.device = self.service.pair({'code': self.service.pairing_code()})['device_id']
        self.register()
        self.flush()
        self.assertEqual(self.transport.calls, [])
        self.run_task()
        self.flush()
        self.assertEqual(len(self.transport.calls), 1)

    def test_disable_phone_cancels_queued_notifications(self):
        self.run_task()
        self.write('DELETE', '/v1/device/push')
        self.assertFalse(self.flush())
        self.assertEqual(self.items(), [('completed', 'dropped', 'Disabled')])
        self.assertEqual(self.states(), [])

    def test_registration_rejects_invalid_or_unpaired_input(self):
        with self.assertRaises(ServiceError):
            self.register(token='https://example.com')
        with self.assertRaises(ServiceError):
            self.service.mutate('PUT', '/v1/device/push', dict(token='a'*64, environment='development', enabled=True), str(uuid4()), 'unknown')

    def test_transaction_rollback_removes_notification(self):
        task = self.run_task()
        with self.service.transaction():
            self.service.db.execute('DELETE FROM push_outbox')
        with self.assertRaises(RuntimeError):
            with self.service.transaction():
                self.service._event('task.updated', self.service._get('tasks', task['id']), self.bot['id'], task['id'])
                raise RuntimeError('rollback')
        self.assertEqual(self.states(), [])
