import json
import os
import unittest
from unittest.mock import patch
import test_provider as fixtures


class WorkWaitProviderTests(unittest.TestCase):
    setUp = fixtures.ProviderTests.setUp
    tearDown = fixtures.ProviderTests.tearDown
    fake_hermes = fixtures.ProviderTests.fake_hermes

    def test_wait_tool_registers_receipt_through_worker(self):
        self.task['source_bot_id'] = 'coordinator'
        provider = self.fake_hermes("""
            import json
            choices = json.loads(registry.dispatch('coconut_wait', {}))
            assert choices['tasks'][0]['id'] == 'existing-task'
            receipt = json.loads(registry.dispatch('coconut_wait', {'task_id': 'existing-task', 'reason': 'Waiting for publication'}))
            assert receipt['status'] == 'waiting'
            return {'completed': True, 'final_response': 'Waiting for publication.'}
        """)
        source = provider.hermes_root / 'run_agent.py'
        text = source.read_text().replace('class AIAgent:', '''class Store:
    def restore(self, todos, revision): self.value = {'todos': todos, 'revision': revision}
    def snapshot(self): return self.value
class AIAgent:''').replace('self.skip_memory =', 'self._todo_store = Store()\n        self.skip_memory =')
        source.write_text(text)
        requests = []
        events = []
        def emit(kind, data):
            events.append(kind)
            if kind == 'work_wait':
                requests.append(data)
                return {'status': 'waiting'} if data else {'tasks': [{'id': 'existing-task'}]}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(requests, [{}, {'task_id': 'existing-task', 'reason': 'Waiting for publication'}])
        self.assertEqual(events[-1], 'completed')

    def test_direct_request_can_wait_without_delegated_todos(self):
        provider = self.fake_hermes("""
            import json
            choices = json.loads(registry.dispatch('coconut_wait', {}))
            assert choices['tasks'][0]['id'] == 'existing-task'
            receipt = json.loads(registry.dispatch('coconut_wait', {'task_id': 'existing-task', 'reason': 'Waiting for review'}))
            assert receipt['status'] == 'waiting'
            return {'completed': True, 'final_response': 'Waiting for review.'}
        """)
        requests, events = [], []
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'work_wait':
                requests.append(data)
                return {'status': 'waiting'} if data else {'tasks': [{'id': 'existing-task'}]}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(requests, [{}, {'task_id': 'existing-task', 'reason': 'Waiting for review'}])
        self.assertEqual(events[-1][0], 'completed')
        self.assertNotIn('_work_todos', events[-1][1])
