import base64
import json
import tempfile
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import Service
from coconut.push_relay import RelayTransport
from test_service import ControlledProvider


class EncryptedTransport:
    configured = True

    def __init__(self):
        self.sealed = []
        self.sent = []
        self.result = (200, 'Accepted')

    def seal(self, key, payload, notification_id, expires):
        self.sealed.append(payload)
        return {'id': notification_id, 'keyId': 'test-key', 'ciphertext': 'encrypted-placeholder'}

    def send(self, *args):
        self.sent.append(args)
        return self.result


class RelayPushTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.device = self.service.pair({'code': self.service.pairing_code()})['device_id']
        self.bot = self.write('POST', '/v1/bots', {'name': 'Private bot name', 'role': 'Research'})
        self.relay = EncryptedTransport()
        self.service.push.relay = self.relay
        self.route = {'route_id': 'a' * 64, 'send_token': 'b' * 64, 'public_key': base64.b64encode(b'x' * 32).decode()}
        self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True, relay=self.route))

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), self.device)

    def run_task(self, text='Private task content'):
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': text, 'mode': 'queue'})
        self.service._run_task(task['id'])
        return task

    def flush(self):
        with self.service.transaction():
            self.service.db.execute('UPDATE push_items SET created=created-900')
            self.service.db.execute('UPDATE push_outbox SET not_before=0,created=created-900,sent=sent-900')
        return self.service.push.deliver_one()

    def test_preview_uses_bot_and_actual_reply(self):
        task = self.run_task()
        self.flush()
        payload = self.relay.sealed[0]
        self.assertEqual(payload['aps']['alert'], {'title': 'Private bot name', 'body': 'The independent task is done.'})
        self.assertEqual(payload['coconut']['task_id'], task['id'])
        self.assertNotIn('Private', json.dumps(self.relay.sent))

    def test_legacy_completion_preview_does_not_publish_interim_text(self):
        def start(task, bot, history, emit):
            emit('interim_message', {'text': 'I will check the failing test.'})
            emit('completed', {'summary': 'Complete.'})
        self.service.provider.start_task = start
        task = self.run_task('Check the test')
        self.flush()
        self.assertEqual(self.relay.sealed[0]['coconut']['kind'], 'completed')
        self.assertEqual(self.relay.sealed[0]['aps']['alert'],
                         {'title': 'Private bot name', 'subtitle': 'Finished', 'body': task['title']})

    def test_decision_preview_uses_pending_question(self):
        self.run_task('Write report')
        self.flush()
        self.assertEqual(self.relay.sealed[0]['aps']['alert']['body'], 'Create report.md? Save the prepared report.')
        self.assertEqual(self.relay.sealed[0]['aps']['badge'], 1)
        self.assertNotIn('badge', json.dumps(self.relay.sent))

    def test_retry_reuses_persisted_ciphertext(self):
        self.run_task()
        self.relay.result = (503, 'TransportUnavailable')
        self.flush()
        self.relay.result = (200, 'Accepted')
        self.flush()
        self.assertEqual(len(self.relay.sealed), 1)
        self.assertEqual(self.relay.sent[0], self.relay.sent[1])

    def test_escaped_preview_fits_the_complete_encrypted_content_bound(self):
        self.write('PATCH', f'/v1/bots/{self.bot["id"]}', {'name': '\x00' * 80})

        def start(task, bot, history, emit):
            emit('text_delta', {'text': 'R' + '\x00' * 239})
            emit('completed', {'summary': 'Done.'})

        self.service.provider.start_task = start
        task = self.run_task()
        relay = RelayTransport(self.service.root)
        self.service.push.relay = relay
        contents = []

        def encrypt(args, **kwargs):
            request = json.loads(kwargs['input'])
            content = base64.b64decode(request['content'])
            contents.append(content)
            if len(content) > 2200:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0, json.dumps({'id': request['id'], 'ciphertext': 'test'}).encode())

        with patch.object(relay, '_private_file', return_value=True), patch.object(relay, 'send', return_value=(200, 'Accepted')), \
             patch('coconut.push_relay.subprocess.run', side_effect=encrypt):
            self.assertTrue(self.flush())
        self.assertEqual(len(contents), 1)
        self.assertLessEqual(len(contents[0]), 2200)
        payload = json.loads(contents[0])
        self.assertTrue(payload['aps']['alert']['body'].startswith('R'))
        self.assertTrue(payload['aps']['alert']['body'].endswith('…'))
        self.assertEqual(payload['coconut']['task_id'], task['id'])
        self.assertEqual(payload['coconut']['device_id'], self.device)
        self.assertEqual(self.service.db.execute('SELECT status FROM push_outbox').fetchone()[0], 'accepted')

    def test_encryption_failure_keeps_a_retry_and_sends_the_group_once(self):
        first, second = self.run_task('First result'), self.run_task('Second result')
        original_seal = self.relay.seal
        calls = []

        def seal(*args):
            calls.append(args)
            if len(calls) == 1:
                raise OSError('Private encryption error.')
            return original_seal(*args)

        self.relay.seal = seal
        self.assertTrue(self.flush())
        row = self.service.db.execute('SELECT status,attempts,not_before,reason,envelope FROM push_outbox ORDER BY rowid LIMIT 1').fetchone()
        self.assertEqual(row[:2], ('queued', 1))
        self.assertGreater(row[2], 0)
        self.assertEqual(row[3], 'EncryptionUnavailable')
        self.assertIsNone(row[4])
        self.assertFalse(self.service.push.deliver_one())
        self.assertTrue(self.flush())
        self.assertEqual({key: self.relay.sealed[0]['coconut'][key] for key in ('version', 'device_id', 'kind', 'count')},
                         dict(version=1, device_id=self.device, kind='group', count=2))
        self.assertEqual(self.relay.sealed[0]['aps']['alert'], dict(title='Coconut', body='2 finished tasks are waiting'))
        self.assertFalse(self.flush())
        self.assertEqual(len(self.relay.sent), 1)
        self.assertEqual(self.service.db.execute('SELECT status FROM push_outbox').fetchall(), [('accepted',)])
        self.assertEqual(self.service.db.execute('SELECT task_id,status FROM push_items ORDER BY created').fetchall(),
                         [(first['id'], 'sent'), (second['id'], 'sent')])

    def test_plaintext_registration_cannot_downgrade_encryption(self):
        from coconut.service import ServiceError
        with self.assertRaises(ServiceError):
            self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True))

    def test_failed_preview_excludes_raw_error(self):
        self.service.provider.start_task = lambda task, bot, history, emit: emit('failed', {'summary': 'token=secret-password'})
        self.run_task('Check deployment')
        self.flush()
        alert = self.relay.sealed[0]['aps']['alert']
        self.assertEqual(alert['subtitle'], 'Could not finish')
        self.assertEqual(alert['body'], 'Check deployment')
        self.assertNotIn('secret-password', json.dumps(self.relay.sealed))

    def test_preview_ignores_another_bot_and_past_turn(self):
        task = self.run_task()
        with self.service.transaction():
            current = self.service._get('tasks', task['id'])
            current['_turn_started_at'] = '2099-01-01T00:00:00Z'
            self.service._put('tasks', current)
        self.flush()
        self.assertEqual(self.relay.sealed[0]['aps']['alert']['body'], task['title'])

    def test_snapshot_excludes_delivery_credentials(self):
        self.assertNotIn(self.route['send_token'], json.dumps(self.service.snapshot()))


if __name__ == '__main__':
    unittest.main()
