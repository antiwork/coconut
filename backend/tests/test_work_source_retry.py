import time
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import test_work_source_coverage as fixtures


class WorkSourceRetryTests(unittest.TestCase):
    setUp = fixtures.WorkSourceTests.setUp
    audit = fixtures.WorkSourceTests.audit

    def finish(self, identity, status='completed'):
        with self.service.transaction():
            task = self.service._get('tasks', identity)
            task['status'] = status
            self.service._put('tasks', task)

    def test_unresolved_assignment_retries_after_backoff_without_a_lifetime_limit(self):
        first = self.audit()['task_id']
        self.finish(first)
        second = self.audit()['task_id']
        self.finish(second, 'failed')
        self.assertEqual(self.audit()['task_id'], second)
        with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
            third = self.audit()['task_id']
        self.assertNotIn(third, [first, second])
        self.assertEqual(len(self.service._all('messages')), 0)

    def test_dead_wait_is_revisited_but_active_child_is_preserved(self):
        first = self.audit()['task_id']
        self.finish(first, 'waiting')
        child = self.service._send(self.bot, {'text': 'Read evidence', 'mode': 'queue'}, record_message=False)
        with self.service.transaction():
            task = self.service._get('tasks', first)
            task['_waiting_on'] = [child['id']]
            self.service._put('tasks', task)
        with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
            self.assertEqual(self.audit()['task_id'], first)
            self.finish(child['id'])
            self.assertNotEqual(self.audit()['task_id'], first)

    def test_receipt_wait_and_its_pending_decision_prevent_duplicate_recovery(self):
        first = self.audit()['task_id']
        self.finish(first, 'waiting')
        child = self.service._send(self.bot, {'text': 'Check evidence', 'mode': 'queue'}, record_message=False)
        with self.service.transaction():
            self.service._put('native_receipts', dict(id='wait-receipt', task_id=first, state='completed',
                action={'name': 'coconut_wait'}, _wait_task_id=child['id']))
        with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
            self.assertEqual(self.audit()['task_id'], first)
            self.finish(child['id'], 'needs_you')
            with self.service.transaction():
                self.service._put('decisions', dict(id='card', task_id=child['id'], status='pending'))
            self.assertEqual(self.audit()['task_id'], first)
            with self.service.transaction():
                self.service._put('decisions', dict(id='card', task_id=child['id'], status='executed'))
            self.assertNotEqual(self.audit()['task_id'], first)

    def test_process_receipt_wait_preserves_running_and_saved_results(self):
        first = self.audit()['task_id']
        self.finish(first, 'waiting')
        process = dict(id='process', task_id='other-task', state='completed', action={'name': 'terminal'},
                       result={'session_id': 'process-one', 'notify_on_complete': True},
                       _process_status='running', _process_observed_at=datetime.now(timezone.utc).isoformat())
        with self.service.transaction():
            self.service._put('native_receipts', process)
            self.service._put('native_receipts', dict(id='wait', task_id=first, state='completed',
                action={'name': 'coconut_wait'}, _wait_process_receipt='process'))
        with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
            self.assertEqual(self.audit()['task_id'], first)
            process.update(_process_status='exited', _completion_text='Evidence returned')
            self.service._put('native_receipts', process)
            self.assertEqual(self.audit()['task_id'], first)
            process['_process_delivery'] = 'consumed'
            self.service._put('native_receipts', process)
            self.assertNotEqual(self.audit()['task_id'], first)


if __name__ == '__main__':
    unittest.main()
