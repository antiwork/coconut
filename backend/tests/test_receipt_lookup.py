import tempfile
import unittest
from pathlib import Path

from coconut.providers import FixtureProvider
from coconut.service import Service


class ReceiptLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = Service(self.root, FixtureProvider(self.root), start_workers=False)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)

    def test_uncertain_receipt_matches_task_turn_and_current_state(self):
        service = self.service
        record = dict(id='receipt', task_id='task', state='executing')
        service._put('native_receipts', record)
        self.assertTrue(service._has_uncertain_receipt('task', None))
        self.assertFalse(service._has_uncertain_receipt('other', None))
        self.assertFalse(service._has_uncertain_receipt('task', 'turn'))
        record['turn_id'] = 'turn'
        service._put('native_receipts', record)
        self.assertFalse(service._has_uncertain_receipt('task', None))
        self.assertTrue(service._has_uncertain_receipt('task', 'turn'))
        record['state'] = 'completed'
        service._put('native_receipts', record)
        self.assertFalse(service._has_uncertain_receipt('task', 'turn'))

    def test_completed_history_does_not_add_work_to_a_receipt_lookup(self):
        service = self.service
        with service.transaction():
            for index in range(5000):
                service._put('native_receipts', dict(id=str(index), task_id=str(index),
                    turn_id='old', state='completed', result='Saved tool output.'))
        # Bound database work without depending on the speed of the CI machine.
        service.db.set_progress_handler(lambda: 1, 2000)
        try:
            self.assertFalse(service._has_uncertain_receipt('current', 'current'))
        finally:
            service.db.set_progress_handler(None, 0)


if __name__ == '__main__':
    unittest.main()
