import json
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from coconut.service import Service
from turn_text import turn_input
from test_service import ControlledProvider


class BackgroundProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.bot = self.write('POST', '/v1/bots', {'name': 'Dev', 'role': 'Development'})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), 'test-phone')

    def wait_receipt(self, task_id, target_id, identity=None):
        """A wait of coconut_wait: task `task_id` waits for task `target_id`."""
        return dict(id=identity or str(uuid4()), task_id=task_id, state='completed', result='{}',
                    action={'name': 'coconut_wait', 'arguments': {'task_id': target_id, 'reason': 'Review'}},
                    _wait_task_id=target_id, _wait_session_id=self.bot['session_id'])

    def test_pending_check_reads_only_the_assignment_receipts(self):
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Run the review'})
        target = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'The review'})
        other = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Other work'})
        original = self.wait_receipt(task['id'], target['id'])
        with self.service.transaction():
            for index in range(100):
                self.service._put('native_receipts', self.wait_receipt(other['id'], target['id'], f'unrelated-{index}'))
            self.service._put('native_receipts', dict(original, _process_delivery='consumed'))
        with patch('coconut.background_processes.json.loads', wraps=json.loads) as reads:
            self.assertFalse(self.service._background_pending(task['id']))
            self.assertLess(reads.call_count, 10, 'Unrelated waits must not increase the read cost.')
        with self.service.transaction():
            child = self.service._get('tasks', other['id'])
            child['_assignment_task_id'] = task['id']
            self.service._put('tasks', child)
        self.assertTrue(self.service._background_pending(task['id']))

    def test_completion_preserves_a_followup_queued_before_dispatch(self):
        request = dict(operation='start', task=dict(id='task', title='Deploy', _turn_message_ids=['new'],
                       _background_completion=['Process exited with code 0']),
                       history=[dict(id='new', task_id='task', role='user', text='Do not deploy yet')])
        prompt = turn_input(request)[0]
        self.assertIn('Process exited with code 0', prompt)
        self.assertIn('Do not deploy yet', prompt)

    def test_completion_does_not_replace_an_approval_answer(self):
        request = dict(operation='resume', task=dict(id='task', title='Deploy',
                       _background_completion=['Process exited with code 0']), history=[],
                       decision=dict(action_type='native_approval', status='approved', action={'name': 'terminal', 'arguments': {'command': 'deploy'}}))
        prompt = turn_input(request)[0]
        self.assertIn('recorded answer', prompt)
        self.assertIn('"status": "approved"', prompt)
        self.assertIn('"command": "deploy"', prompt)

    def test_internal_completion_replaces_original_request(self):
        request = dict(operation='start', task=dict(id='task', title='Deploy the app',
                       _background_completion=['Process exited with code 0']), history=[])
        prompt = turn_input(request)[0]
        self.assertIn('Process exited with code 0', prompt)
        self.assertNotIn('Deploy the app', prompt)


if __name__ == '__main__':
    unittest.main()
