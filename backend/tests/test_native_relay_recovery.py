import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import unittest

import test_native_execution
from coconut.native_relay import recover_delivery
from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError


class NativeRelayRecoveryTests(unittest.TestCase):
    setUp = test_native_execution.NativeExecutionTests.setUp
    tearDown = test_native_execution.NativeExecutionTests.tearDown
    write = test_native_execution.NativeExecutionTests.write
    event = test_native_execution.NativeExecutionTests.event

    def prepare(self):
        peer = self.write('POST', '/v1/bots', {'name': 'Recipient'})
        arguments = {'target': peer['id'], 'message': 'Check the report.'}
        call = self.event('native_call', {'name': 'message_agent', 'arguments': arguments})
        envelope = dict(id=uuid4().hex, target_connection=peer['id'], message='Native transport wrapper')
        intent = dict(call_id=call['id'], task_id=self.task['id'], target_bot_id=peer['id'],
                      text=arguments['message'], previous_ids=[])
        home = self.service.profiles.home(self.bot)
        folder = home / '.coconut-relay'
        folder.mkdir()
        (folder / (call['id'] + '.json')).write_text(json.dumps(intent))
        return home, intent, envelope

    def recover(self, home, call_id):
        def save(home, intent):
            (Path(home) / '.coconut-relay' / (intent['call_id'] + '.json')).write_text(json.dumps(intent))
        with patch('coconut.native_relay.save_intent', save):
            return recover_delivery(home, call_id)

    def test_restart_after_native_enqueue_delivers_once_and_completes_tool_receipt(self):
        home, intent, envelope = self.prepare()
        outbox = home / 'bot_relay/outbox'
        outbox.mkdir(parents=True)
        (outbox / (envelope['id'] + '.json')).write_text(json.dumps(envelope))
        payload = self.recover(home, intent['call_id'])
        root = self.root
        class RecoveryProvider(FixtureProvider):
            def native_settings(self, operation, bots, **kwargs):
                if operation == 'relay_recover': return payload
                return {'ok': True}
        self.service.close()
        self.service = Service(root, RecoveryProvider(root), start_workers=False)
        self.assertEqual(len(self.service._all('deliveries')), 1)
        record = self.service._all('native_receipts')[0]
        self.assertEqual(record['state'], 'completed')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')
        receipt = self.service.native_relay_delivery(self.task['id'], payload)
        self.assertEqual(json.loads(record['result']), receipt)
        self.assertEqual(len(self.service._all('deliveries')), 1)
        self.service.close()
        self.service = Service(root, RecoveryProvider(root), start_workers=False)
        self.assertEqual(len(self.service._all('deliveries')), 1)

    def test_claimed_envelope_recovers_after_lost_delivery_receipt(self):
        home, intent, envelope = self.prepare()
        claimed = home / 'bot_relay/claimed'
        claimed.mkdir(parents=True)
        (claimed / (envelope['id'] + '.json')).write_text(json.dumps(envelope))
        payload = self.recover(home, intent['call_id'])
        first = self.event('native_bot_message', payload)
        second = self.event('native_bot_message', payload)
        self.assertEqual(first, second)
        self.assertEqual(len(self.service._all('deliveries')), 1)
        result = self.event('native_result', {'id': intent['call_id'], 'result': json.dumps(first, sort_keys=True)})
        self.assertEqual(result['status'], 'recorded')

    def test_saved_envelope_survives_native_relay_file_cleanup(self):
        home, intent, envelope = self.prepare()
        intent['envelope'] = envelope
        (home / '.coconut-relay' / (intent['call_id'] + '.json')).write_text(json.dumps(intent))
        self.assertEqual(self.recover(home, intent['call_id'])['native_envelope_id'], envelope['id'])

    def test_unknown_or_ambiguous_envelope_does_not_guess(self):
        home, intent, envelope = self.prepare()
        self.assertIsNone(self.recover(home, intent['call_id']))
        outbox = home / 'bot_relay/outbox'
        outbox.mkdir(parents=True)
        for _ in range(2):
            row = dict(envelope, id=uuid4().hex)
            (outbox / (row['id'] + '.json')).write_text(json.dumps(row))
        self.assertIsNone(self.recover(home, intent['call_id']))

    def test_target_and_text_revalidation_prevents_receipt_substitution(self):
        home, intent, envelope = self.prepare()
        payload = dict(native_call_id=intent['call_id'], native_envelope_id=envelope['id'],
                       target_bot_id=intent['target_bot_id'], text='Substituted action')
        with self.assertRaises(ServiceError): self.event('native_bot_message', payload)
        self.assertEqual(self.service._all('deliveries'), [])
        payload['text'] = intent['text']
        self.write('DELETE', '/v1/bots/' + intent['target_bot_id'], {})
        with self.assertRaises(ServiceError): self.event('native_bot_message', payload)
        self.assertEqual(self.service._all('deliveries'), [])


if __name__ == '__main__': unittest.main()
