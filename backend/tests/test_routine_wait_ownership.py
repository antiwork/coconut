import json
import unittest
from uuid import uuid4

from coconut.service import ServiceError
import test_native_scheduler as fixtures


class RoutineWaitOwnershipTests(unittest.TestCase):
    setUp = fixtures.SchedulerIntegrationTests.setUp

    def start(self):
        task = self.service.mutate('POST', '/v1/bots/' + self.bot['id'] + '/messages',
                                  {'text': 'Check existing intake', 'mode': 'queue'}, str(uuid4()))
        stored = self.service._get('tasks', task['id'])
        stored['status'] = 'working'
        self.service._put('tasks', stored)
        return stored

    def trigger(self, task):
        routine = dict(id='routine', bot_id=self.bot['id'], name='Intake', enabled=True,
                       _native_id=self.job['id'], next_run_at='2000-01-01T00:00:00Z')
        self.service._put('routines', routine)
        self.service.provider.native_settings = lambda *args, **kwargs: {
            'job': dict(self.job, fire_claim={'by': 'owner'})}
        receipt = self.service.native_event(task['id'], 'native_call', dict(name='cronjob_manage',
            arguments=dict(action='run', job_id=self.job['id'])))
        return json.loads(receipt['routine_result'])['task_id']

    def test_caller_can_wait_for_the_routine_returned_by_its_tool(self):
        task = self.start()
        target = self.trigger(task)
        self.assertNotEqual(task['exchange_id'], self.service._get('tasks', target)['exchange_id'])
        result = self.service.wait_for_task(task['id'], dict(task_id=target, reason='Read the intake result'))
        self.assertEqual(result['status'], 'waiting')
        wait = next(r for r in self.service._all('native_receipts') if r.get('_wait_task_id') == target)
        self.assertEqual(wait['task_id'], task['id'])
        self.assertIsNone(self.service._wait_completion(wait, task))
        running = self.service._get('tasks', target)
        running['status'] = 'working'
        self.service._put('tasks', running)
        self.service.emit(target, 'completed', {'summary': 'No new receipts arrived.'})
        self.assertIn('No new receipts arrived.', self.service._wait_completion(wait, task))

    def test_recovery_uses_the_original_work_receipt(self):
        task = self.start()
        target = self.trigger(task)
        work = self.service._register_work(task['id'], dict(key='receipt-recovery', title='Recover receipts',
            activity='Checking existing intake', status='waiting'))
        task['status'] = 'failed'
        self.service._put('tasks', task)
        recovery = self.start()
        recovery.update(_work_context_id=work['id'], _self_recovery={'previous_task_id': task['id']})
        self.service._put('tasks', recovery)
        result = self.service.wait_for_task(recovery['id'], dict(task_id=target, reason='Read the existing intake result'))
        self.assertEqual(result['status'], 'waiting')

    def test_an_unrelated_request_cannot_use_another_calls_routine(self):
        target = self.trigger(self.start())
        unrelated = self.start()
        with self.assertRaises(ServiceError):
            self.service.wait_for_task(unrelated['id'], dict(task_id=target, reason='Read the intake result'))

    def test_a_task_from_an_old_session_is_not_a_wait_target(self):
        task = self.start()
        target = self.trigger(task)
        routine_task = self.service._get('tasks', target)
        routine_task['session_id'] = 'old-session'
        self.service._put('tasks', routine_task)
        with self.assertRaises(ServiceError):
            self.service.wait_for_task(task['id'], dict(task_id=target, reason='Read the intake result'))
