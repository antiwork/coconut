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

    def worker_call(self, task_id, command='npm test', name='terminal'):
        """A local command of an earlier worker turn that stopped before its result."""
        return dict(id=str(uuid4()), task_id=task_id, turn_id=None, state='executing', created_at='2026-09-21T20:17:08Z',
                    action={'name': name, 'arguments': {'command': command}})

    def native(self, task_id):
        """Give the task the Kanban claim of a native outcome, as a worker of the claim ran it."""
        with self.service.transaction():
            task = self.service._get('tasks', task_id)
            task['_kanban_claim'] = dict(native_id='t_' + task_id[:8], external_id=task_id, run_id=1)
            self.service._put('tasks', task)

    def restart(self, outcomes, holders=None, listing_fails=False):
        """Start the service again while the process scan finds `outcomes` (None: the scan cannot see environments),
        and lsof finds `holders` (path: {pid: only its cwd}) in the workspaces, or fails."""
        self.service.close()

        def listing(paths, detail=False):
            if listing_fails:
                raise ValueError('lsof did not list all open files.')
            return {str(path): dict((holders or {}).get(str(path), {})) for path in paths}
        with patch('coconut.work_workspace.process_outcomes', return_value=outcomes), \
                patch('coconut.work_workspace.holders_by_path', side_effect=listing), \
                patch('coconut.work_workspace.process_table', return_value={}):
            self.service = Service(self.temp.name, self.provider, start_workers=False)

    def test_a_call_ends_only_when_no_process_holds_a_workspace_of_its_outcome(self):
        # macOS can hide the environment of a /bin/sh or git child, so the scan of outcomes can miss it. A process
        # in a workspace of the outcome keeps the call, and so does a listing that cannot complete.
        from datetime import datetime, timezone
        from pathlib import Path
        from coconut.work_capacity import execution_key
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Run the tests'})
        self.native(task['id'])
        old, recent = self.worker_call(task['id']), self.worker_call(task['id'])
        recent['created_at'] = datetime.now(timezone.utc).isoformat()
        with self.service.transaction():
            self.service._put('native_receipts', old)
            self.service._put('native_receipts', recent)
            workspace = self.service.root / 'workspaces' / execution_key(self.service._get('tasks', task['id']))
        workspace.mkdir(parents=True)

        def ended():
            with self.service.lock:
                return {name for name, call in (('old', old), ('recent', recent))
                        if self.service._get('native_receipts', call['id']).get('_process_ended_at')}
        self.restart(set(), holders={str(workspace): {4242: False}})
        self.assertEqual(ended(), set())
        self.restart(set(), listing_fails=True)
        self.assertEqual(ended(), set())
        # The workspace is free. A call that started less than an hour ago still waits.
        self.restart(set())
        self.assertEqual(ended(), {'old'})

    def test_a_start_ends_the_calls_of_earlier_workers_whose_processes_are_gone(self):
        from coconut.work_capacity import execution_key
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Run the tests'})
        other = self.write('POST', '/v1/bots', {'name': 'Care', 'role': 'Support'})
        running = self.write('POST', f'/v1/bots/{other["id"]}/messages', {'text': 'Build the app'})
        chat = self.write('POST', f'/v1/bots/{other["id"]}/messages', {'text': 'Check the logs'})
        self.native(task['id'])
        self.native(running['id'])
        calls = dict(gone=self.worker_call(task['id']), running=self.worker_call(running['id']),
                     privileged=self.worker_call(task['id'], 'bash -c "sudo make install"'),
                     delegated=self.worker_call(task['id'], name='delegate_task'),
                     other=self.worker_call(task['id'], name='read_file'),
                     # A chat worker carried no outcome, so no scan can prove that its call ended.
                     chat=self.worker_call(chat['id']))
        # Some earlier receipts have a time without a zone. It is UTC.
        calls['delegated']['created_at'] = '2026-09-21T20:17:08.125000'
        with self.service.transaction():
            for call in calls.values():
                self.service._put('native_receipts', call)
            key = execution_key(self.service._get('tasks', running['id']))
        # A system that hides process environments proves nothing, so no call ends.
        self.restart(None)
        with self.service.lock:
            self.assertFalse(any(self.service._get('native_receipts', c['id']).get('_process_ended_at')
                                 for c in calls.values()))
        self.restart({key})
        with self.service.lock:
            saved = {name: self.service._get('native_receipts', call['id']) for name, call in calls.items()}
        # Only the calls without a process end. Each keeps its state, so its effect stays unknown to the next turn.
        self.assertEqual({name for name, call in saved.items() if call.get('_process_ended_at')}, {'gone', 'delegated'})
        self.assertEqual({call['state'] for call in saved.values()}, {'executing'})
        # A later start ends the call whose process stopped meanwhile.
        self.restart(set())
        with self.service.lock:
            self.assertTrue(self.service._get('native_receipts', calls['running']['id']).get('_process_ended_at'))
            self.assertFalse(self.service._get('native_receipts', calls['privileged']['id']).get('_process_ended_at'))

    def test_a_failed_repair_of_worker_calls_does_not_stop_the_start(self):
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Run the tests'})
        self.native(task['id'])
        with self.service.transaction():
            self.service._put('native_receipts', self.worker_call(task['id']))
        self.service.close()
        with patch('coconut.work_workspace.process_outcomes', side_effect=RuntimeError('ps failed')), \
                self.assertLogs('coconut.service', 'ERROR') as logs:
            self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.assertIn('Could not check the interrupted calls of earlier workers.', logs.output[0])
        self.assertTrue(self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Still here'}))

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
