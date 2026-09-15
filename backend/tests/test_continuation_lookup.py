import tempfile
import unittest
from pathlib import Path

from coconut.providers import FixtureProvider
from coconut.service import Service


class ContinuationLookupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        self.service = Service(root, FixtureProvider(root), start_workers=False)
        self.addCleanup(temp.cleanup)
        self.addCleanup(self.service.close)

    def test_only_another_blocked_turn_in_the_assignment_stops_work(self):
        service = self.service
        root = dict(id='root', status='needs_you')
        child = dict(id='child', status='queued', _assignment_task_id='root')
        service._put('tasks', root)
        self.assertFalse(service._continuation_blocked(root))
        self.assertTrue(service._continuation_blocked(child))
        self.assertFalse(service._continuation_blocked(dict(id='other')))
        root['status'] = 'completed'
        service._put('tasks', root)
        self.assertFalse(service._continuation_blocked(child))
        child['status'] = 'paused'
        service._put('tasks', child)
        self.assertTrue(service._continuation_blocked(root))

    def test_completed_history_does_not_add_database_work_to_lookup(self):
        service = self.service
        with service.transaction():
            for index in range(5000):
                service._put('tasks', dict(id=str(index), status='completed'))
        # Bound database work without relying on the speed of the CI machine.
        service.db.set_progress_handler(lambda: 1, 2000)
        try:
            self.assertFalse(service._continuation_blocked(dict(id='current')))
        finally:
            service.db.set_progress_handler(None, 0)


if __name__ == '__main__':
    unittest.main()
