import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from coconut.native_processes import delegation_completion
from coconut.providers import FixtureProvider
from coconut.service import Service


class DelegateProvider(FixtureProvider):
    result = None

    def start_task(self, task, bot, history, emit):
        if not task.get('_background_completion'):
            record = emit('native_call', {'name': 'delegate_task', 'arguments': {'goal': 'Read evidence'}})
            emit('native_result', {'id': record['id'], 'result': json.dumps({
                'status': 'dispatched', 'delegation_id': 'deleg-one',
                'units': [{'delegation_id': 'deleg-one'}, {'delegation_id': 'deleg-two'}]})})
        emit('completed', {'summary': 'Finished turn'})

    def process_completions(self, bot, processes):
        results = []
        for item in processes:
            if item.get('acknowledge'):
                results.append(dict(id=item['id'], acknowledged=True))
            elif self.result:
                self.assert_ids = item['delegation_ids']
                results.append(dict(id=item['id'], text=self.result))
        return results


class DurableDelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = DelegateProvider(self.tmp.name)
        self.service = Service(self.tmp.name, self.provider, start_workers=False)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.service.close())
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        self.task = self.service.mutate('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Read the source'}, str(uuid4()))
        self.service._run_task(self.task['id'])

    def test_delegation_keeps_owner_waiting_and_delivers_once_across_restart(self):
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'waiting')
        self.provider.result = 'Unknown outcome: inspect the existing evidence before any retry.'
        self.service._drain_process_completions()
        task = self.service._get('tasks', self.task['id'])
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(self.provider.assert_ids, ['deleg-one', 'deleg-two'])
        receipt = self.service._all('native_receipts')[0]
        self.assertTrue(receipt['_delegation_ack_pending'])
        turn = task['_native_turn_id']
        self.service.close()
        self.service = Service(self.tmp.name, self.provider, start_workers=False)
        self.service._drain_process_completions()
        self.assertNotIn('_delegation_ack_pending', self.service._all('native_receipts')[0])
        self.service._run_task(self.task['id'])
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.task['id'])['_native_turn_id'], turn)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')

    def test_cancelled_owner_cannot_resume(self):
        self.service.mutate('POST', f"/v1/tasks/{self.task['id']}/cancel", {}, str(uuid4()))
        self.provider.result = 'Finished'
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'cancelled')

    def test_native_results_are_read_without_early_acknowledgment(self):
        rows = {'one': dict(delegation_id='one', origin_session='task', state='unknown',
                            delivery_state='pending', result={'error': 'Owner exited'})}
        calls = []
        native = SimpleNamespace(recover_abandoned_delegations=lambda: None, get_durable_delegation=rows.get,
                                 mark_completion_delivered=lambda key: calls.append(key))
        with patch.dict('sys.modules', {'tools': SimpleNamespace(async_delegation=native)}):
            item = dict(id='receipt', task_id='task', delegation_ids=['one'])
            result = delegation_completion(item)
            self.assertIn('Owner exited', result[0]['text'])
            self.assertEqual(calls, [])
            self.assertEqual(delegation_completion(item | {'task_id': 'other'}), [])
            rows['one']['state'] = 'running'
            self.assertEqual(delegation_completion(item)[0]['observation'], 'running')
            rows['one']['state'] = 'unknown'
            self.assertTrue(delegation_completion(item | {'acknowledge': True})[0]['acknowledged'])
            self.assertEqual(calls, ['one'])


class ChildReceiptIsolationTests(unittest.TestCase):
    def test_native_child_does_not_use_parent_receipts_or_parent_cards(self):
        from coconut.native_bridge import NativeBridge
        callbacks, records = {}, []
        class Context:
            def __init__(self, *args): pass
            def register_middleware(self, kind, callback):
                callbacks['execute'] = callback
                return SimpleNamespace(dispose=lambda: None)
        modules = {
            'tools.approval': SimpleNamespace(register_gateway_notify=lambda *a: None),
            'hermes_cli.plugins': SimpleNamespace(PluginContext=Context, PluginManifest=lambda **kw: None, get_plugin_manager=lambda: None),
            'hermes_cli.middleware': SimpleNamespace(TOOL_EXECUTION_MIDDLEWARE='tool'),
        }
        def receipt(kind, payload):
            records.append(kind)
            return {'status': 'recorded', 'id': 'receipt'}
        bridge = NativeBridge({'operation': 'start', 'task': {'id': 'parent'}}, None, receipt)
        with patch.dict('sys.modules', modules):
            bridge.install(None)
        execute = callbacks['execute']
        result = execute('read_file', {}, lambda args: 'child result', task_id='sa-child')
        self.assertEqual(result, 'child result')
        execute('execute_code', {}, lambda args: execute('read_file', {}, lambda args: 'nested result'), task_id='sa-child')
        self.assertEqual(records, [])
        self.assertIn('Return this request', execute('coconut_ask', {}, lambda args: self.fail('Child must return its question'), task_id='sa-child'))
        execute('read_file', {}, lambda args: 'parent result', task_id='parent')
        self.assertEqual(records, ['native_call', 'native_result'])
