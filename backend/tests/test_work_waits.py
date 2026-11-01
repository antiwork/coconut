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
        self.service._drain_task_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.finish_target(target)
        self.service._drain_task_completions()
        resumed = self.service._get('tasks', self.child)
        self.assertEqual(resumed['status'], 'queued')
        self.assertIn('Publication verified', resumed['_background_completion'][0])
        turn = resumed['_native_turn_id']
        self.service._drain_task_completions()
        self.assertEqual(self.service._get('tasks', self.child)['_native_turn_id'], turn)
        self.assertEqual(len(self.service._all('tasks')), count)
        self.provider.mode = 'done'
        self.service._run_task(self.child)
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'completed')

    def test_failed_dependency_wakes_waiter(self):
        target = self.target(); self.wait(target); self.finish_target(target, 'failed')
        self.service._drain_task_completions()
        self.assertIn('failed', self.service._get('tasks', self.child)['_background_completion'][0])

    def test_cancelled_waiter_does_not_resume(self):
        target = self.target(); self.wait(target)
        self.write(f'/v1/tasks/{self.child}/cancel', {})
        self.finish_target(target)
        self.service._drain_task_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'cancelled')

    def test_decision_defers_wakeup(self):
        target = self.target(); self.wait(target); self.finish_target(target)
        with self.service.transaction():
            task = self.service._get('tasks', self.child); task['status'] = 'needs_you'
            self.service._put('tasks', task)
        self.service._drain_task_completions()
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

    def test_existing_handoff_dependency_is_not_registered_twice(self):
        target = self.target()
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task['_waiting_on'] = [target]; self.service._put('tasks', task)
        self.wait(target)
        self.assertFalse(any(r.get('_wait_task_id') for r in self.service._all('native_receipts')))

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

    def working(self):
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', task)

    def task_waits(self, target):
        return [r for r in self.service._all('native_receipts') if r.get('_wait_task_id') == target]

    def test_completed_target_returns_its_result_without_a_wait(self):
        target = self.target(); self.finish_target(target); self.working()
        result = self.service.wait_for_task(self.child, {'task_id': target, 'reason': 'Waiting for publication'})
        self.assertEqual(result, {'status': 'completed', 'task_id': target,
                                  'result': f'Task {target} completed: Publication verified.'})
        self.assertEqual(self.task_waits(target), [])
        self.service._drain_task_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'working')
        self.assertNotIn('_background_completion', self.service._get('tasks', self.child))

    def test_repeated_wait_after_delivery_does_not_resume_the_turn_again(self):
        target = self.target()
        self.wait(target)
        self.finish_target(target)
        self.service._drain_task_completions()
        resumed = self.service._get('tasks', self.child)
        self.assertEqual(resumed['status'], 'queued')
        self.assertIn('Publication verified', resumed['_background_completion'][0])
        turn = resumed['_native_turn_id']
        self.assertEqual([r['_process_delivery'] for r in self.task_waits(target)], [turn])
        replies, original = [], self.provider.start_task
        def start(task, bot, history, emit):
            if task['id'] != self.child:
                return original(task, bot, history, emit)
            # The resumed turn has a new native turn ID and repeats the same wait.
            replies.append(emit('work_wait', {'task_id': target, 'reason': 'Waiting for publication'}))
            emit('text_delta', {'text': 'Published the validated patch.'})
            emit('completed', {'summary': 'Complete.', '_work_todos': progress('completed')})
        self.provider.start_task = start
        self.service._run_task(self.child)
        self.provider.start_task = original
        self.assertEqual(replies, [{'status': 'completed', 'task_id': target,
                                    'result': f'Task {target} completed: Publication verified.'}])
        self.assertEqual(len(self.task_waits(target)), 1)
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'completed')
        for restart in (False, True):
            if restart:
                self.service.close()
                self.service = Service(self.temp.name, self.provider, start_workers=False)
            self.service._drain_task_completions()
            self.service._drain_returns()
            finished = self.service._get('tasks', self.child)
            self.assertEqual((finished['status'], finished['_native_turn_id']), ('completed', turn))
            self.assertEqual(len(self.task_waits(target)), 1)
        returns = [t for t in self.service._all('tasks') if t.get('_is_return') and t['bot_id'] == self.desk['id']]
        self.assertEqual(len(returns), 1)

    def test_unsettled_targets_keep_a_durable_wait(self):
        cases = {
            'dependency': {'_waiting_on': [str(uuid4())]},
            'unfinished work': {'_work_todos': progress()},
            'failed': {'status': 'failed'},
        }
        for label, change in cases.items():
            with self.subTest(label):
                target = self.target(); self.finish_target(target); self.working()
                with self.service.transaction():
                    task = self.service._get('tasks', target); task.update(change); self.service._put('tasks', task)
                result = self.service.wait_for_task(self.child, {'task_id': target, 'reason': 'Waiting for publication'})
                self.assertEqual(result['status'], 'waiting')
                self.assertEqual(len(self.task_waits(target)), 1)

    def test_native_target_returns_its_result_only_after_the_board_settles(self):
        target = self.target(); self.finish_target(target); self.working()
        with self.service.transaction():
            task = self.service._get('tasks', target)
            task.update(_native_outcome_id='native-target', _native_turn_id='target-input')
            self.service._put('tasks', task)
        card = {'id': 'native-target', 'status': 'running', 'input_id': 'target-input', 'dependencies': []}
        native = SimpleNamespace(call=Mock(), tasks={target: card, self.child: {'id': 'native-child', 'dependencies': []}})
        with patch.object(self.service, 'kanban', native):
            result = self.service.wait_for_task(self.child, {'task_id': target, 'reason': 'Waiting for publication'})
            self.assertEqual(result['status'], 'waiting')
            self.assertEqual(len(self.task_waits(target)), 1)
            with self.service.transaction():
                task = self.service._get('tasks', self.child); task['_native_turn_id'] = 'next-turn'
                self.service._put('tasks', task)
                receipt = self.task_waits(target)[0]; receipt['_process_delivery'] = 'earlier-turn'
                self.service._put('native_receipts', receipt)
            card['status'] = 'done'
            result = self.service.wait_for_task(self.child, {'task_id': target, 'reason': 'Waiting for publication'})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(self.task_waits(target)), 1)

    def test_changed_target_result_reaches_a_later_wait(self):
        target = self.target(); self.finish_target(target); self.working()
        first = self.service.wait_for_task(self.child, {'task_id': target, 'reason': 'Waiting for publication'})
        self.assertEqual((first['status'], first.get('result')), ('completed', f'Task {target} completed: Publication verified.'))
        with self.service.transaction():
            task = self.service._get('tasks', target); task.update(status='working', summary='Refreshing the PR.')
            self.service._put('tasks', task)
            waiter = self.service._get('tasks', self.child); waiter['status'] = 'queued'
            self.service._put('tasks', waiter)
        self.wait(target)
        self.assertEqual(len(self.task_waits(target)), 1)
        with self.service.transaction():
            task = self.service._get('tasks', target); task.update(status='completed', summary='The refreshed PR passed.')
            self.service._put('tasks', task)
        self.service._drain_task_completions()
        self.assertEqual(self.service._get('tasks', self.child)['_background_completion'],
                         [f'Task {target} completed: The refreshed PR passed.'])
