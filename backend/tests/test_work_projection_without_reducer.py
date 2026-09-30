import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, now


class WorkProjectionWithoutReducerTests(unittest.TestCase):
    # This module does not install legacy_work_sync.scope(), so Service is what production runs.
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.service = Service(root, FixtureProvider(root), start_workers=False)

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def row(self, **fields):
        return dict(dict(id=str(uuid4()), bot_id=str(uuid4()), task_id=str(uuid4()), kind='work', title='A row', status='working',
                         activity='Still running', url=None, preview='', created_at=now(), observed_at=now()), **fields)

    def test_the_product_has_no_reducer_of_attempts_and_receipts(self):
        for name in ('_work_outcome', '_project_work_outcomes', '_project_work_outcomes_inner'):
            self.assertFalse(hasattr(Service, name), name)

    def test_a_row_without_a_native_outcome_shows_no_invented_state(self):
        projected = self.service._work_projection([self.row()])[0]
        self.assertEqual((projected['work_state'], projected['activity']), ('waiting', 'Status unavailable'))

    def test_a_row_with_a_saved_terminal_answer_keeps_it(self):
        pull = self.row(kind='pr', status='merged', pr_state='merged', url='https://github.com/acme/app/pull/1')
        projected = self.service._work_projection([pull])[0]
        self.assertEqual((projected['work_state'], projected['activity']), ('completed', 'Completed'))


if __name__ == '__main__':
    unittest.main()
