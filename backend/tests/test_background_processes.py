import json
import tempfile
import threading
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

from coconut.service import Service
from coconut.hermes_worker import conversation_input
from test_service import ControlledProvider


class CompletionProvider(ControlledProvider):
    def __init__(self):
        super().__init__()
        self.results = {}
        self.polls = []

    def process_completions(self, bot, processes):
        self.polls.append((bot, processes))
        return [dict(id=p['id'], text=self.results[p['session_id']], consumed=False)
                for p in processes if p['session_id'] in self.results]

    def start_task(self, task, bot, history, emit):
        self.starts.append((task, bot, history))
        if task.get('_background_completion'):
            emit('text_delta', {'text': 'The review passed. Both fixtures passed.'})
        else:
            emit('text_delta', {'text': 'The review is running.'})
            record = emit('native_call', {'name': 'terminal', 'arguments': {'background': True, 'notify': True}})
            emit('native_result', {'id': record['id'], 'result': json.dumps({'session_id': 'proc_test', 'notify_on_complete': True, 'status': 'running'})})
        emit('completed', {'summary': 'Ready to review.'})


class BackgroundProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = CompletionProvider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.bot = self.write('POST', '/v1/bots', {'name': 'Dev', 'role': 'Development'})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), 'test-phone')

    def start(self):
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Run the review'})
        self.service._run_task(task['id'])
        return task['id']

    def test_idle_completion_resumes_same_task_without_a_human_message(self):
        task_id = self.start()
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'waiting')
        before = len(self.service._all('messages'))
        self.provider.results['proc_test'] = 'The process exited with code 0. Both fixtures passed.'
        self.service._drain_process_completions()
        task = self.service._get('tasks', task_id)
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(len(self.service._all('messages')), before)
        self.service._run_task(task_id)
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'completed')
        self.assertIn('Both fixtures passed', self.provider.starts[-1][0]['_background_completion'][0])
        self.service._drain_process_completions()
        self.assertEqual(len(self.provider.starts), 2)
        self.assertEqual(len(self.service._all('tasks')), 1)

    def test_delivery_survives_restart_before_model_continuation(self):
        task_id = self.start()
        self.provider.results['proc_test'] = 'Completed'
        self.service._drain_process_completions()
        turn = self.service._get('tasks', task_id)['_native_turn_id']
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', task_id)['_native_turn_id'], turn)
        self.service._run_task(task_id)
        self.assertEqual(len(self.provider.starts), 2)

    def test_native_result_recovery_after_restart(self):
        task_id = self.start()
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.provider.results['proc_test'] = 'Completed during restart'
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'queued')

    def test_running_bot_defers_delivery(self):
        task_id = self.start()
        self.provider.results['proc_test'] = 'Completed'
        lock = self.service.bot_locks[self.bot['id']]
        with lock:
            self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'waiting')
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'queued')

    def test_cancelled_exchange_does_not_wake(self):
        task_id = self.start()
        self.write('POST', f'/v1/tasks/{task_id}/cancel', {})
        self.provider.results['proc_test'] = 'Killed'
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'cancelled')

    def test_approval_and_archive_defer_completion(self):
        task_id = self.start()
        self.provider.results['proc_test'] = 'Completed'
        for field, value in [('status', 'needs_you'), ('status', 'paused')]:
            with self.service.transaction():
                task = self.service._get('tasks', task_id)
                task[field] = value
                self.service._put('tasks', task)
            self.service._drain_process_completions()
            self.assertEqual(self.service._get('tasks', task_id)['status'], value)

    def test_old_conversation_epoch_does_not_resume(self):
        task_id = self.start()
        with self.service.transaction():
            self.service.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', ('conversation_epoch', json.dumps(str(uuid4()))))
        self.provider.results['proc_test'] = 'Old completion'
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'waiting')

    def test_failure_output_reaches_the_bot(self):
        task_id = self.start()
        self.provider.results['proc_test'] = 'Process failed with exit code 1: permission denied'
        self.service._drain_process_completions()
        self.assertIn('exit code 1', self.service._get('tasks', task_id)['_background_completion'][0])

    def test_receipt_and_continuation_commit_together(self):
        task_id = self.start()
        self.provider.results['proc_test'] = 'Completed'
        put = self.service._put
        def fail(table, value):
            if table == 'tasks' and value['status'] == 'queued':
                raise OSError('disk failure')
            return put(table, value)
        with patch.object(self.service, '_put', side_effect=fail), self.assertRaises(OSError):
            self.service._drain_process_completions()
        self.assertFalse(self.service._all('native_receipts')[0].get('_process_delivery'))
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'waiting')
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'queued')

    def test_return_reaches_desk_without_a_new_user_message(self):
        desk = self.write('POST', '/v1/bots', {'name': 'Desk', 'role': 'Coordinator'})
        parent = self.write('POST', f'/v1/bots/{desk["id"]}/messages', {'text': 'Ask Dev to review'})
        with self.service.transaction():
            task = self.service._get('tasks', parent['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
            child = self.service._store_bot_message(task['id'], {'target_bot_id': self.bot['id'], 'text': 'Run the review'})
        self.service.emit(parent['id'], 'completed', {'summary': 'Waiting for Dev.'})
        self.service._run_task(child['task_id'])
        self.service._drain_returns()
        self.assertEqual(len(self.service._all('tasks')), 2)
        self.provider.results['proc_test'] = 'The fixtures passed'
        self.service._drain_process_completions()
        self.service._run_task(child['task_id'])
        self.service._drain_returns()
        returns = [t for t in self.service._all('tasks') if t.get('_is_return')]
        self.assertEqual(len(returns), 1)
        self.assertEqual(returns[0]['bot_id'], desk['id'])
        self.assertIn('Both fixtures passed', returns[0]['_bot_messages'][0]['text'])
        self.assertEqual(returns[0]['_assignment_task_id'], parent['id'])

    def test_already_sent_progress_does_not_suppress_final_return(self):
        desk = self.write('POST', '/v1/bots', {'name': 'Desk', 'role': 'Coordinator'})
        parent = self.write('POST', f'/v1/bots/{desk["id"]}/messages', {'text': 'Ask Dev'})
        with self.service.transaction():
            task = self.service._get('tasks', parent['id']);task['status'] = 'working';self.service._put('tasks', task)
            child = self.service._store_bot_message(task['id'], {'target_bot_id': self.bot['id'], 'text': 'Run the review'})
        self.service._run_task(child['task_id'])
        with self.service.transaction():
            task = self.service._get('tasks', child['task_id']);task['status'] = 'completed';self.service._put('tasks', task)
            self.service._store_bot_message(task['id'], {'target_bot_id': desk['id'], 'text': 'Review is running'}, returning=True)
        self.provider.results['proc_test'] = 'Completed'
        self.service._drain_process_completions()
        self.service._run_task(child['task_id'])
        self.service._drain_returns()
        returns = [t for t in self.service._all('tasks') if t.get('_is_return')]
        self.assertEqual(len(returns), 2)
        self.assertIn('Both fixtures passed', returns[-1]['_bot_messages'][0]['text'])

    def test_background_work_in_a_return_turn_reaches_original_sender(self):
        desk = self.write('POST', '/v1/bots', {'name': 'Desk', 'role': 'Coordinator'})
        tuner = self.write('POST', '/v1/bots', {'name': 'Tuner', 'role': 'Operations'})
        parent = self.write('POST', f'/v1/bots/{desk["id"]}/messages', {'text': 'Ask Dev'})
        with self.service.transaction():
            task = self.service._get('tasks', parent['id']);task['status'] = 'working';self.service._put('tasks', task)
            child = self.service._store_bot_message(task['id'], {'target_bot_id': self.bot['id'], 'text': 'Run the review'})
            task = self.service._get('tasks', child['task_id']);task['status'] = 'working';self.service._put('tasks', task)
            leaf = self.service._store_bot_message(task['id'], {'target_bot_id': tuner['id'], 'text': 'Check setup'})
        self.service.emit(child['task_id'], 'completed', {'summary': 'Waiting for Tuner.'})
        with self.service.transaction():
            task = self.service._get('tasks', leaf['task_id']);task['status'] = 'completed';self.service._put('tasks', task)
            receipt = self.service._store_bot_message(task['id'], {'target_bot_id': self.bot['id'], 'text': 'Setup passed'}, returning=True)
        continuation = receipt['task_id']
        self.service._run_task(continuation)
        self.assertEqual(self.service._get('tasks', continuation)['status'], 'waiting')
        self.provider.results['proc_test'] = 'The review passed'
        self.service._drain_process_completions()
        self.service._run_task(continuation)
        self.service._drain_returns()
        replies = [t for t in self.service._all('tasks') if t.get('_is_return') and t['bot_id'] == desk['id']]
        self.assertEqual(len(replies), 1)
        self.assertIn('Both fixtures passed', replies[0]['_bot_messages'][0]['text'])

    def test_failed_return_resumes_parent_with_its_process_result(self):
        parent_id = self.start()
        with self.service.transaction():
            for record in self.service._all('native_receipts'):
                record['_process_delivery'] = 'consumed'
                self.service._put('native_receipts', record)
        child = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Continue review'})
        with self.service.transaction():
            task = self.service._get('tasks', child['id'])
            task.update(_assignment_task_id=parent_id, _is_return=True)
            self.service._put('tasks', task)
        self.service._run_task(child['id'])
        with self.service.transaction():
            task = self.service._get('tasks', child['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.service.emit(child['id'], 'failed', {'summary': 'Model request failed.'})
        self.assertEqual(self.service._get('tasks', parent_id)['status'], 'failed')
        self.provider.results['proc_test'] = 'The process succeeded'
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', child['id'])['status'], 'queued')
        self.assertEqual(self.service._get('tasks', parent_id)['status'], 'waiting')
        self.service._run_task(child['id'])
        self.assertEqual(self.service._get('tasks', child['id'])['status'], 'completed')
        self.assertIn('The process succeeded', self.provider.starts[-1][0]['_background_completion'][0])

    def test_unrelated_parent_failure_keeps_completion_pending(self):
        parent_id = self.start()
        with self.service.transaction():
            for record in self.service._all('native_receipts'):
                record['_process_delivery'] = 'consumed'
                self.service._put('native_receipts', record)
        child = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Continue review'})
        with self.service.transaction():
            task = self.service._get('tasks', child['id'])
            task.update(_assignment_task_id=parent_id, _is_return=True)
            self.service._put('tasks', task)
        self.service._run_task(child['id'])
        with self.service.transaction():
            task = self.service._get('tasks', parent_id)
            task.update(status='failed', _result_task_id='another-failed-turn')
            self.service._put('tasks', task)
        self.provider.results['proc_test'] = 'Completed'
        self.service._drain_process_completions()
        receipts = [r for r in self.service._all('native_receipts') if r['task_id'] == child['id']]
        self.assertTrue(receipts)
        self.assertTrue(all(not r.get('_process_delivery') for r in receipts))
        self.assertNotEqual(self.service._get('tasks', child['id'])['status'], 'queued')

    def test_failure_return_drains_before_completion_resumes_task(self):
        desk = self.write('POST', '/v1/bots', {'name': 'Desk', 'role': 'Coordinator'})
        parent = self.write('POST', f'/v1/bots/{desk["id"]}/messages', {'text': 'Ask Dev'})
        with self.service.transaction():
            task = self.service._get('tasks', parent['id']);task['status'] = 'working';self.service._put('tasks', task)
            child = self.service._store_bot_message(task['id'], {'target_bot_id': self.bot['id'], 'text': 'Run review'})
        self.service._run_task(child['task_id'])
        with self.service.transaction():
            task = self.service._get('tasks', child['task_id']);task['status'] = 'working';self.service._put('tasks', task)
        self.service.emit(child['task_id'], 'failed', {'summary': 'Model request failed.'})
        self.assertTrue(self.service._get('tasks', child['task_id']).get('_return_pending'))
        self.provider.results['proc_test'] = 'Process succeeded'
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', child['task_id'])['status'], 'failed')
        self.assertTrue(self.service._background_pending(child['task_id']))
        self.service._drain_returns()
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', child['task_id'])['status'], 'queued')
        self.service._drain_returns()
        self.service._run_task(child['task_id'])
        self.service._drain_returns()
        replies = [t for t in self.service._all('tasks') if t.get('_is_return') and t['bot_id'] == desk['id']]
        self.assertEqual(len(replies), 2)
        self.assertIn('Both fixtures passed', replies[-1]['_bot_messages'][0]['text'])

    def test_parent_failure_return_defers_receipt_delivery(self):
        task_id = self.start()
        parent = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Original assignment'})
        with self.service.transaction():
            assignment = self.service._get('tasks', parent['id'])
            assignment.update(status='failed', _result_task_id=task_id, _return_pending={'text': 'Failure pending'})
            self.service._put('tasks', assignment)
            task = self.service._get('tasks', task_id)
            task.update(status='failed', _assignment_task_id=parent['id'])
            self.service._put('tasks', task)
        self.provider.results['proc_test'] = 'Completed'
        self.service._drain_process_completions()
        self.assertTrue(self.service._background_pending(task_id))
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'failed')
        self.assertEqual(self.service._get('tasks', parent['id'])['status'], 'failed')

    def test_two_return_completions_finish_before_assignment_returns(self):
        desk = self.write('POST', '/v1/bots', {'name': 'Desk', 'role': 'Coordinator'})
        parent = self.write('POST', f'/v1/bots/{desk["id"]}/messages', {'text': 'Ask Dev'})
        with self.service.transaction():
            task = self.service._get('tasks', parent['id']);task['status'] = 'working';self.service._put('tasks', task)
            child = self.service._store_bot_message(task['id'], {'target_bot_id': self.bot['id'], 'text': 'Run both reviews'})
            assignment = self.service._get('tasks', child['task_id'])
            assignment['status'] = 'waiting'
            self.service._put('tasks', assignment)
        turns = []
        for index in range(2):
            turn = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': f'Review {index}'})
            with self.service.transaction():
                task = self.service._get('tasks', turn['id'])
                task.update(_assignment_task_id=child['task_id'], _is_return=True)
                self.service._put('tasks', task)
            self.service._run_task(turn['id'])
            with self.service.transaction():
                receipt = next(r for r in self.service._all('native_receipts') if r['task_id'] == turn['id'])
                receipt['result'] = json.dumps({'session_id': f'proc_{index}', 'notify_on_complete': True})
                self.service._put('native_receipts', receipt)
            turns.append(turn['id'])
            self.provider.results[f'proc_{index}'] = f'Review {index} passed'
        self.service._drain_process_completions()
        self.assertTrue(all(self.service._get('tasks', task_id)['status'] == 'queued' for task_id in turns))
        self.service._run_task(turns[0])
        self.service._drain_returns()
        self.assertEqual(self.service._get('tasks', child['task_id'])['status'], 'waiting')
        self.assertFalse(any(t.get('_is_return') and t['bot_id'] == desk['id'] for t in self.service._all('tasks')))
        self.service._run_task(turns[1])
        self.service._drain_returns()
        self.assertEqual(self.service._get('tasks', child['task_id'])['status'], 'completed')
        self.assertEqual(len([t for t in self.service._all('tasks') if t.get('_is_return') and t['bot_id'] == desk['id']]), 1)

    def test_poller_resumes_idle_bot_without_requests(self):
        task_id = self.start()
        self.provider.results['proc_test'] = 'Completed'
        self.service.start()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with self.service.lock:
                done = self.service._get('tasks', task_id)['status'] == 'completed'
            if done:
                break
            time.sleep(.02)
        self.assertTrue(done)
        self.assertEqual(len(self.provider.starts), 2)

    def test_consumed_result_stays_consumed_after_restart(self):
        task_id = self.start()
        with self.service.transaction():
            task = self.service._get('tasks', task_id)
            task['status'] = 'working'
            self.service._put('tasks', task)
        receipt = self.service.native_event(task_id, 'native_call', {'name': 'process_manage', 'arguments': {'action': 'wait', 'session_id': 'proc_test'}})
        self.service.native_event(task_id, 'native_result', {'id': receipt['id'], 'result': '{"status":"exited"}', 'consumed_process_ids': ['proc_test']})
        self.service.emit(task_id, 'completed', {'summary': 'Reviewed the result.'})
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.provider.results['proc_test'] = 'Completed'
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'completed')
        self.assertEqual(len(self.provider.starts), 1)

    def test_migration_preserves_prior_observed_output(self):
        task_id = self.start()
        with self.service.transaction():
            record = self.service._all('native_receipts')[0]
            record.pop('_process_registered')
            self.service._put('native_receipts', record)
            self.service._put('native_receipts', dict(id=str(uuid4()), task_id=task_id, state='completed',
                action={'name': 'process_manage', 'arguments': {'action': 'poll', 'session_id': 'proc_test'}},
                result=json.dumps({'status': 'exited', 'session_id': 'proc_test'})))
            self.service._migrate_process_deliveries()
        self.assertEqual(self.service._get('native_receipts', record['id'])['_process_delivery'], 'consumed')

    def test_new_poll_observation_does_not_suppress_native_notification(self):
        task_id = self.start()
        with self.service.transaction():
            self.service._put('native_receipts', dict(id=str(uuid4()), task_id=task_id, state='completed',
                action={'name': 'process_manage', 'arguments': {'action': 'poll', 'session_id': 'proc_test'}},
                result=json.dumps({'status': 'exited', 'session_id': 'proc_test'})))
            self.service._migrate_process_deliveries()
        self.provider.results['proc_test'] = 'Completed'
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'queued')

    def test_consumption_cannot_cross_bot_boundaries(self):
        task_id = self.start()
        other = self.write('POST', '/v1/bots', {'name': 'Other', 'role': 'Research'})
        task = self.write('POST', f'/v1/bots/{other["id"]}/messages', {'text': 'Check'})
        with self.service.transaction():
            self.service._record_consumed_processes({'task_id': task['id']}, {'consumed_process_ids': ['proc_test']})
        self.assertTrue(self.service._background_pending(task_id))

    def test_two_completions_share_one_continuation(self):
        task_id = self.start()
        with self.service.transaction():
            record = self.service._all('native_receipts')[0]
            record.update(id=str(uuid4()), result=json.dumps({'session_id': 'proc_second', 'notify_on_complete': True}))
            self.service._put('native_receipts', record)
        self.provider.results.update(proc_test='First result', proc_second='Second result')
        self.service._drain_process_completions()
        task = self.service._get('tasks', task_id)
        self.assertEqual(len(task['_background_completion']), 2)
        self.assertEqual(len(self.service._all('tasks')), 1)
        self.assertTrue(all(r.get('_process_delivery') == task['_native_turn_id'] for r in self.service._all('native_receipts')))

    def test_completion_preserves_a_followup_queued_before_dispatch(self):
        request = dict(operation='start', task=dict(id='task', title='Deploy', _turn_message_ids=['new'],
                       _background_completion=['Process exited with code 0']),
                       history=[dict(id='new', task_id='task', role='user', text='Do not deploy yet')])
        prompt = conversation_input(request)[0]
        self.assertIn('Process exited with code 0', prompt)
        self.assertIn('Do not deploy yet', prompt)

    def test_completion_does_not_replace_an_approval_answer(self):
        request = dict(operation='resume', task=dict(id='task', title='Deploy',
                       _background_completion=['Process exited with code 0']), history=[],
                       decision=dict(action_type='native_approval', status='approved', action={'name': 'terminal', 'arguments': {'command': 'deploy'}}))
        prompt = conversation_input(request)[0]
        self.assertIn('recorded answer', prompt)
        self.assertIn('"status": "approved"', prompt)
        self.assertIn('"command": "deploy"', prompt)

    def test_internal_completion_replaces_original_request(self):
        request = dict(operation='start', task=dict(id='task', title='Deploy the app',
                       _background_completion=['Process exited with code 0']), history=[])
        prompt = conversation_input(request)[0]
        self.assertIn('Process exited with code 0', prompt)
        self.assertNotIn('Deploy the app', prompt)


if __name__ == '__main__':
    unittest.main()
