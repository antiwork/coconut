"""Each Kanban pass reads the few rows it needs through an index, not every decision or context."""
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from coconut.decision_inbox import SWIPED_DECISIONS
from coconut.providers import FixtureProvider
from coconut.pull_request_reviews import MERGE_DECISIONS
from coconut.service import Service


class KanbanPassReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / 'coconut'
        self.service = Service(root, FixtureProvider(root), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'One', 'role': ''}, str(uuid4()))

    def plan(self, statement, values=()):
        return ' | '.join(row[3] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + statement, values))

    def test_swiped_and_merge_cards_use_their_indexes(self):
        self.assertIn('USING INDEX decisions_undo', self.plan(SWIPED_DECISIONS))
        merge = self.plan(MERGE_DECISIONS, ('https://github.com/o/r/pull/1',))
        self.assertIn('USING INDEX decisions_merge_url', merge)
        self.assertNotIn('SCAN', merge)

    def test_a_finished_swipe_is_the_only_card_that_changes(self):
        with self.service.transaction():
            task = dict(id=str(uuid4()), bot_id=self.bot['id'], status='completed')
            self.service._put('tasks', task)
            for identity, undo in (('expired', '2026-01-01T00:00:00+00:00'), ('open', '2999-01-01T00:00:00+00:00'),
                                   ('plain', None)):
                self.service._put('decisions', dict(id=identity, bot_id=self.bot['id'], task_id=task['id'], status='pending',
                                                    revision=1, inbox_undo_until=undo, inbox_swipe_id='swipe' if undo else None))
        self.service.finish_inbox_swipes()
        self.assertIsNone(self.service._get('decisions', 'expired')['inbox_undo_until'])
        self.assertEqual(self.service._get('decisions', 'open')['inbox_undo_until'], '2999-01-01T00:00:00+00:00')
        self.assertEqual(self.service._get('decisions', 'plain')['revision'], 1)

    def test_a_merge_card_is_found_without_regard_to_case(self):
        url = 'https://github.com/Owner/Repo/pull/7'
        with self.service.transaction():
            self.service._put('decisions', dict(id='merge', bot_id=self.bot['id'], task_id='t', status='executed',
                                                merge_pr_url=url, created_at='2026-01-01T00:00:00Z'))
        artifact = dict(url=url.lower(), head_sha='a' * 40)
        self.assertTrue(self.service._pr_has_recorded_merge_decision(artifact, [artifact]))


if __name__ == '__main__':
    unittest.main()
