"""Requester replies resume the delegated task through durable native receipts."""
import unittest
from uuid import uuid4

from test_work_progress import WorkProgressTests, progress


class RelayWakeupTests(unittest.TestCase):
    setUp = WorkProgressTests.setUp
    tearDown = WorkProgressTests.tearDown
    write = WorkProgressTests.write
    # Use the existing service fixture without changing its provider or assertions.
    def send_back(self):
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task.update(status='working', _work_todos=progress())
            self.service._put('tasks', task)
        call = self.service.emit(self.child, 'native_call', {
            'name': 'message_agent', 'arguments': {'target': self.desk['id'], 'message': 'Please commit the reviewed file.'}})
        receipt = self.service.emit(self.child, 'native_bot_message', {
            'native_call_id': call['id'], 'native_envelope_id': uuid4().hex,
            'target_bot_id': self.desk['id'], 'text': 'Please commit the reviewed file.'})
        self.service.emit(self.child, 'completed', {'summary': 'Complete.', '_work_todos': progress()})
        return call['id'], receipt['task_id']

    def test_native_owner_answers_helper_without_losing_its_callback(self):
        with self.service.transaction():
            parent = self.service._get('tasks', self.parent['id'])
            parent['_native_outcome_id'] = 'native-owner'
            self.service._put('tasks', parent)
        record_id, reply_id = self.send_back()
        self.assertNotEqual(reply_id, self.parent['id'])
        self.service._run_task(reply_id)
        self.service._drain_returns()
        self.service._drain_process_completions()
        helper = self.service._get('tasks', self.child)
        self.assertEqual(helper['status'], 'queued')
        self.assertIn('Received the current result.', helper['_background_completion'][0])
        self.assertEqual(self.service._get('tasks', self.parent['id'])['status'], 'waiting')
        self.assertEqual(self.service._get('native_receipts', record_id)['_process_delivery'], helper['_native_turn_id'])

    def test_requester_reply_waits_without_retries_then_resumes_once(self):
        record_id, reply_id = self.send_back()
        task = self.service._get('tasks', self.child)
        self.assertEqual(task['status'], 'waiting')
        self.assertNotIn('_work_retry', task)
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        self.service._run_task(reply_id)
        self.service._drain_returns()
        self.service._drain_process_completions()
        task = self.service._get('tasks', self.child)
        self.assertEqual(task['status'], 'queued')
        turn_id = task['_native_turn_id']
        self.assertIn('Received the current result.', task['_background_completion'][0])
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['_native_turn_id'], turn_id)
        self.assertEqual(self.service._get('native_receipts', record_id)['_process_delivery'], turn_id)
        self.provider.mode = 'done'
        self.service._run_task(self.child)
        self.service._drain_returns()
        final = [t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == self.child]
        self.assertEqual(len(final), 1)

    def test_restart_retains_reply_after_native_reply_file_is_written(self):
        record_id, reply_id = self.send_back()
        self.service._run_task(reply_id)
        self.service._drain_returns()
        self.service.close()
        from coconut.service import Service
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'queued')
        self.assertTrue(self.service._get('native_receipts', record_id)['_process_delivery'])

    def test_failed_requester_turn_delivers_failure_without_resending(self):
        _, reply_id = self.send_back()
        with self.service.transaction():
            reply = self.service._get('tasks', reply_id)
            reply['status'] = 'working'
            self.service._put('tasks', reply)
        self.service.emit(reply_id, 'failed', {'summary': 'GitHub access expired.'})
        self.service._drain_returns()
        self.service._drain_process_completions()
        task = self.service._get('tasks', self.child)
        self.assertEqual(task['status'], 'queued')
        self.assertIn('GitHub access expired.', task['_background_completion'][0])
        self.assertEqual(len(self.service._all('deliveries')), 2)

    def test_cancelled_sender_does_not_resume(self):
        _, reply_id = self.send_back()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        self.service._run_task(reply_id)
        with self.service.transaction():
            child = self.service._get('tasks', self.child)
            child['status'] = 'cancelled'
            self.service._put('tasks', child)
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'cancelled')

    def test_requester_background_work_delivers_final_result(self):
        import json
        _, reply_id = self.send_back()
        with self.service.transaction():
            reply = self.service._get('tasks', reply_id)
            reply['status'] = 'working'
            self.service._put('tasks', reply)
        call = self.service.emit(reply_id, 'native_call', {
            'name': 'terminal', 'arguments': {'background': True, 'notify': True}})
        self.service.emit(reply_id, 'native_result', {'id': call['id'], 'result': json.dumps({
            'session_id': 'proc_desk', 'notify_on_complete': True, 'status': 'running'})})
        self.service.emit(reply_id, 'completed', {'summary': 'The commit is running.'})
        self.service._drain_returns()
        self.service._drain_process_completions()
        self.assertNotIn('_native_relay_result', self.service._get('tasks', reply_id))
        self.assertNotIn('_background_completion', self.service._get('tasks', self.child))
        self.provider.results['proc_desk'] = 'The commit completed.'
        self.service._drain_process_completions()
        self.service._run_task(reply_id)
        self.service._drain_returns()
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'queued')
        self.assertIn('Received the current result.', self.service._get('tasks', self.child)['_background_completion'][0])

    def test_requester_handoff_delivers_result_after_the_third_bot_finishes(self):
        _, reply_id = self.send_back()
        tuner = self.write('/v1/bots', {'name': 'Tuner', 'role': 'System checks'})
        with self.service.transaction():
            reply = self.service._get('tasks', reply_id)
            reply['status'] = 'working'
            self.service._put('tasks', reply)
            dependency = self.service._store_bot_message(reply_id, {
                'target_bot_id': tuner['id'], 'text': 'Verify access before the commit.'})['task_id']
        self.service.emit(reply_id, 'completed', {'summary': 'Tuner is checking access.'})
        self.service._drain_returns()
        self.service._drain_process_completions()
        self.assertNotIn('_native_relay_result', self.service._get('tasks', reply_id))
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        with self.service.transaction():
            worker = self.service._get('tasks', dependency)
            worker['status'] = 'working'
            self.service._put('tasks', worker)
        self.service.emit(dependency, 'completed', {'summary': 'Access works.'})
        self.service._drain_returns()
        continuation = next(t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == dependency)
        self.assertEqual(continuation['_relay_callback_owner'], reply_id)
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        self.service._run_task(continuation['id'])
        self.service._drain_returns()
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'queued')
        self.assertIn('Received the current result.', self.service._get('tasks', self.child)['_background_completion'][0])

    def test_forwarded_native_handoff_keeps_the_original_reply_receipt(self):
        tuner = self.write('/v1/bots', {'name': 'Tuner', 'role': 'System checks'})
        with self.service.transaction():
            child = self.service._get('tasks', self.child)
            child.update(status='working', _work_todos=progress(),
                         _native_relay={'sender_bot_id': self.desk['id'], 'envelope_id': uuid4().hex})
            self.service._put('tasks', child)
            dependency = self.service._store_bot_message(self.child, {
                'target_bot_id': tuner['id'], 'text': 'Verify access.'})['task_id']
        self.service.emit(self.child, 'completed', {'summary': 'Waiting for access.', '_work_todos': progress()})
        with self.service.transaction():
            worker = self.service._get('tasks', dependency)
            worker['status'] = 'working'
            self.service._put('tasks', worker)
        self.service.emit(dependency, 'completed', {'summary': 'Access works.'})
        self.service._drain_returns()
        continuation = next(t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == dependency)
        self.provider.mode = 'done'
        self.service._run_task(continuation['id'])
        result = self.service._get('tasks', self.child)
        self.assertIn('Published the validated patch.', result['_native_relay_result']['text'])
        self.assertIn('_native_reply_pending', result)
