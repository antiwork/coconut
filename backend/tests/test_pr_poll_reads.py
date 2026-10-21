"""The PR poll reads pending cards through an index. It runs every 0.2 s while a refresh waits for GitHub."""
from pathlib import Path
import tempfile
import unittest

from coconut.providers import FixtureProvider
from coconut.pull_request_reviews import PENDING_CARDS, PENDING_MERGE_CARDS
from coconut.service import Service


class PRPollReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / 'coconut'
        self.service = Service(root, FixtureProvider(root), start_workers=False)
        self.addCleanup(self.service.close)

    def test_pending_card_reads_use_the_status_index_in_row_order(self):
        for statement in (PENDING_CARDS, PENDING_MERGE_CARDS):
            plan = ' | '.join(row[3] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + statement))
            self.assertIn('USING INDEX decisions_status', plan)
            self.assertNotIn('TEMP B-TREE', plan)

    def test_pending_cards_come_back_in_row_order(self):
        with self.service.transaction():
            for identity, status in (('c', 'pending'), ('a', 'executed'), ('b', 'pending')):
                self.service._put('decisions', dict(id=identity, status=status))
        self.assertEqual([row[0] for row in self.service.db.execute(PENDING_CARDS.replace('SELECT data', 'SELECT id'))],
                         ['c', 'b'])


if __name__ == '__main__':
    unittest.main()
