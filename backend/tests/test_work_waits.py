import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut.service import Service, ServiceError, now
from test_work_progress import WorkProgressTests, progress


class WorkWaitTests(unittest.TestCase):
    setUp = WorkProgressTests.setUp
    tearDown = WorkProgressTests.tearDown
    write = WorkProgressTests.write

    def target(self):
        with self.service.transaction():
            task = self.service._send(self.desk, {'text': 'Publish the reviewed cleanup', 'mode': 'queue'},
                                      exchange_id=self.parent['exchange_id'],
                                      thread_id=self.parent['thread_id'], conversation_bot_id=self.desk['id'])
        return task['id']

    def wait(self, target):
        original = self.provider.start_task
        def start(task, bot, history, emit):
            if task['id'] != self.child:
                return original(task, bot, history, emit)
            self.receipt = emit('work_wait', {'task_id': target, 'reason': 'Waiting for publication'})
            emit('completed', {'summary': 'Waiting for publication.', '_work_todos': progress()})
        self.provider.start_task = start
        self.service._run_task(self.child)
        self.provider.start_task = original
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        self.assertNotIn('_work_retry', self.service._get('tasks', self.child))

    def finish_target(self, target, status='completed'):
        with self.service.transaction():
            task = self.service._get('tasks', target)
            task.update(status=status, summary='Publication verified.' if status == 'completed' else 'Publication failed.',
                        _conversation_epoch=self.service._setting('conversation_epoch'), updated_at=now())
            self.service._put('tasks', task)

    def test_existing_task_wakes_waiter_once_after_restart(self):
        target = self.target()
        self.wait(target)
        count = len(self.service._all('tasks'))
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.finish_target(target)
        self.service._drain_process_completions()
        resumed = self.service._get('tasks', self.child)
        self.assertEqual(resumed['status'], 'queued')
        self.assertIn('Publication verified', resumed['_background_completion'][0])
        turn = resumed['_native_turn_id']
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['_native_turn_id'], turn)
        self.assertEqual(len(self.service._all('tasks')), count)
        self.provider.mode = 'done'
        self.service._run_task(self.child)
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'completed')

    def test_failed_dependency_wakes_waiter(self):
        target = self.target(); self.wait(target); self.finish_target(target, 'failed')
        self.service._drain_process_completions()
        self.assertIn('failed', self.service._get('tasks', self.child)['_background_completion'][0])

    def test_cancelled_waiter_does_not_resume(self):
        target = self.target(); self.wait(target)
        self.write(f'/v1/tasks/{self.child}/cancel', {})
        self.finish_target(target)
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'cancelled')

    def test_decision_defers_wakeup(self):
        target = self.target(); self.wait(target); self.finish_target(target)
        with self.service.transaction():
            task = self.service._get('tasks', self.child); task['status'] = 'needs_you'
            self.service._put('tasks', task)
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'needs_you')

    def test_rejects_self_other_exchange_and_circular_waits(self):
        unrelated = self.write(f'/v1/bots/{self.desk["id"]}/messages', {'text': 'Unrelated work'})
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', task)
        for target in (self.child, unrelated['id'], self.parent['id']):
            with self.subTest(target=target), self.assertRaises(ServiceError):
                self.service.wait_for_task(self.child, {'task_id': target, 'reason': 'Waiting'})
        listed = self.service.wait_for_task(self.child, {})['tasks']
        self.assertNotIn(unrelated['id'], {t['id'] for t in listed})

    def test_duplicate_registration_does_not_duplicate_wakeup(self):
        target = self.target(); self.wait(target)
        with self.service.transaction():
            task = self.service._get('tasks', self.child); task['status'] = 'working'
            self.service._put('tasks', task)
        self.service.wait_for_task(self.child, {'task_id': target, 'reason': 'Waiting for publication'})
        self.assertEqual(len([r for r in self.service._all('native_receipts') if r.get('_wait_task_id')]), 1)

    def native_dependency(self, target):
        return SimpleNamespace(call=Mock(), tasks={
            self.child: {'id': 'native-child', 'dependencies': []},
            target: {'id': 'native-target', 'dependencies': [{'id': 'native-middle'}]},
            'middle': {'id': 'native-middle', 'dependencies': [{'id': 'native-child'}]},
        })

    def test_rejects_wait_when_native_dependency_already_waits_for_caller(self):
        target = self.target()
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', task)
        native = self.native_dependency(target)
        def snapshot(_):
            acquired = []
            def callback():
                if self.service.lock.acquire(timeout=1):
                    acquired.append(True)
                    self.service.lock.release()
            thread = threading.Thread(target=callback)
            thread.start()
            thread.join(2)
            self.assertEqual(acquired, [True], 'Snapshot must not hold the service lock.')
        native.call.side_effect = snapshot
        with patch.object(self.service, 'kanban', native), self.assertRaisesRegex(ServiceError, 'circular wait'):
            self.service.wait_for_task(self.child, {'task_id': target, 'reason': 'Wait for merge'})
        native.call.assert_called_once_with({'op': 'snapshot', 'external_ids': [self.child, target]})
        self.assertFalse(any(r.get('_wait_task_id') for r in self.service._all('native_receipts')))

    def test_saved_native_cycle_wakes_original_waiter_once(self):
        target = self.target()
        self.wait(target)
        count = len(self.service._all('tasks'))
        with patch.object(self.service, 'kanban', self.native_dependency(target)):
            self.service._drain_task_completions()
            resumed = self.service._get('tasks', self.child)
            self.assertEqual(resumed['status'], 'queued')
            self.assertIn('target task waits for your result', resumed['_background_completion'][0])
            turn = resumed['_native_turn_id']
            self.service._drain_task_completions()
        self.assertEqual(self.service._get('tasks', self.child)['_native_turn_id'], turn)
        self.assertEqual(len(self.service._all('tasks')), count)
        self.assertEqual(self.service._get('tasks', target)['status'], 'queued')

    def test_finished_native_dependency_does_not_interrupt_saved_wait(self):
        target = self.target()
        self.wait(target)
        native = self.native_dependency(target)
        native.tasks['middle']['dependencies'] = []
        with patch.object(self.service, 'kanban', native):
            self.service._drain_task_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')

    def test_continuation_cannot_wait_on_its_own_assignment(self):
        with self.service.transaction():
            original = self.service._get('tasks', self.child)
            original.update(status='waiting', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', original)
            callback = dict(original, id=str(uuid4()), status='working', _assignment_task_id=original['id'])
            self.service._put('tasks', callback)
        with self.assertRaises(ServiceError):
            self.service.wait_for_task(callback['id'], {'task_id': original['id'], 'reason': 'Waiting for the assignment'})

    def test_existing_relay_subscription_is_not_registered_twice(self):
        target = self.target()
        with self.service.transaction():
            self.service._put('native_receipts', dict(id=str(uuid4()), task_id=self.child,
                action={'name': 'message_agent'}, state='completed', result='{}', _relay_task_id=target))
        self.wait(target)
        self.assertFalse(any(r.get('_wait_task_id') for r in self.service._all('native_receipts')))
        self.finish_target(target)
        with self.service.transaction():
            task = self.service._get('tasks', target)
            task['_native_relay_result'] = {'failed': False, 'text': 'Publication verified.'}
            self.service._put('tasks', task)
        self.service._drain_process_completions()
        self.provider.mode = 'done'
        self.service._run_task(self.child)
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'completed')

    def test_existing_handoff_dependency_is_not_registered_twice(self):
        target = self.target()
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task['_waiting_on'] = [target]; self.service._put('tasks', task)
        self.wait(target)
        self.assertFalse(any(r.get('_wait_task_id') for r in self.service._all('native_receipts')))

    def test_restart_during_registration_replays_a_valid_native_receipt(self):
        from coconut.native_bridge import NativeBridge, fingerprint
        target = self.target()
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', task)
        self.service.wait_for_task(self.child, {'task_id': target, 'reason': 'Waiting for publication'})
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        receipts = [r for r in self.service._all('native_receipts') if r['task_id'] == self.child]
        bridge = NativeBridge({'task': {'id': self.child, '_native_receipts': receipts}}, None, None)
        self.assertIn(fingerprint(receipts[0]['action']), bridge.completed)

    def test_completed_scope_does_not_retry_later_work(self):
        def start(task, bot, history, emit):
            state = progress('completed')
            state['todos'].append({'id': 'later', 'content': 'Monitor the later approved fix', 'status': 'pending'})
            emit('completed', {'summary': 'The requested review is complete.', '_work_todos': state})
        self.provider.start_task = start
        self.service._run_task(self.child)
        result = self.service._get('tasks', self.child)
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('_work_retry', result)
        self.assertEqual(result['_return_pending']['result_status'], 'completed')
