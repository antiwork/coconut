import time
import unittest
from unittest.mock import patch

import test_work_source_coverage as fixtures


class SourceAuditReuseTests(unittest.TestCase):
    setUp = fixtures.WorkSourceTests.setUp
    audit = fixtures.WorkSourceTests.audit

    def status(self, identity, status):
        with self.service.transaction():
            task = self.service._get('tasks', identity)
            task['status'] = status
            self.service._put('tasks', task)

    def resumed_older_audit(self, legacy=False):
        first = self.audit()['task_id']
        self.status(first, 'completed')
        second = self.audit()['task_id']
        self.status(second, 'completed')
        self.status(first, 'queued')
        if legacy:
            with self.service.transaction():
                task = self.service._get('tasks', first)
                task.pop('_source_assignment', None)
                self.service._put('tasks', task)
        return first, second

    def test_late_result_reuses_older_audit_after_a_newer_audit_finishes(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                first, second = self.resumed_older_audit(legacy)
                count = len(self.service._all('tasks'))
                with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
                    self.assertEqual(self.audit()['task_id'], second)
                self.assertEqual(len(self.service._all('tasks')), count)
                self.status(first, 'completed')
                self.item = self.item | {'id': self.item['id'] + '-next'}

    def test_source_change_still_gets_a_fresh_audit(self):
        first, second = self.resumed_older_audit()
        self.item['updated_at'] = '2026-09-14T13:00:00Z'
        self.assertNotIn(self.audit()['task_id'], (first, second))

    def test_old_conversation_does_not_block_current_assignments(self):
        first, second = self.resumed_older_audit()
        with self.service.transaction():
            task = self.service._get('tasks', first)
            task['_conversation_epoch'] = 'previous-conversation'
            self.service._put('tasks', task)
        with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
            self.assertNotIn(self.audit()['task_id'], (first, second))


if __name__ == '__main__':
    unittest.main()
