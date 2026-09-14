import unittest
from types import SimpleNamespace
from unittest.mock import patch

from coconut.native_processes import delegation_completion
import test_durable_delegations as delegation


class DelegationOriginTests(unittest.TestCase):
    def test_native_conversation_result_returns_to_the_receipt_owner(self):
        row = dict(delegation_id='child', origin_session='original-conversation', state='completed',
                   delivery_state='pending', result={'summary': 'Evidence returned'})
        calls = []
        native = SimpleNamespace(recover_abandoned_delegations=lambda: None,
            get_durable_delegation=lambda identity: row if identity == 'child' else None,
            mark_completion_delivered=calls.append)
        def resolve(identity):
            return 'current-conversation' if identity == 'original-conversation' else identity
        item = dict(id='receipt', task_id='coconut-task', delegation_ids=['child'])
        with patch.dict('sys.modules', {'tools': SimpleNamespace(async_delegation=native)}):
            result = delegation_completion(item, session_id='current-conversation', resolve_session=resolve)
            self.assertIn('Evidence returned', result[0]['text'])
            self.assertEqual(calls, [])
            self.assertEqual(delegation_completion(item, session_id='other-conversation', resolve_session=resolve), [])
            self.assertEqual(delegation_completion(item | {'delegation_ids': ['missing']},
                session_id='current-conversation', resolve_session=resolve), [])
            self.assertTrue(delegation_completion(item | {'acknowledge': True},
                session_id='current-conversation', resolve_session=resolve)[0]['acknowledged'])
            self.assertEqual(calls, ['child'])


class DelegationAcknowledgmentTests(unittest.TestCase):
    setUp = delegation.DurableDelegationTests.setUp

    def test_acknowledgment_keeps_the_conversation_epoch(self):
        with self.service.transaction():
            self.service.db.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)',
                                    ('conversation_epoch', '"after-reset"'))
            task = self.service._get('tasks', self.task['id'])
            task['_conversation_epoch'] = 'after-reset'
            self.service._put('tasks', task)
        self.provider.result = 'Evidence returned'
        self.service._drain_process_completions()
        expected = self.service._get('tasks', self.task['id'])['_conversation_epoch']
        original = self.provider.process_completions
        acknowledgments = []
        def inspect(bot, processes):
            for process in processes:
                if process.get('acknowledge'):
                    acknowledgments.append(process['id'])
                    self.assertEqual(process.get('epoch'), expected)
            return original(bot, processes)
        self.provider.process_completions = inspect
        self.service._drain_process_completions()
        self.assertEqual(len(acknowledgments), 1)
        self.assertNotIn('_delegation_ack_pending', self.service._all('native_receipts')[0])


if __name__ == '__main__':
    unittest.main()
