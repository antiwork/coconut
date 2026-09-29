"""The dispatcher finds the tasks with a marker field through a partial index and reads no other task."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import MARKS, Service, marked


class MarkedTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / 'coconut'
        self.service = Service(root, FixtureProvider(root), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'One', 'role': ''}, str(uuid4()))

    def put(self, **fields):
        with self.service.transaction():
            task = dict(id=str(uuid4()), bot_id=self.bot['id'], status='queued', **fields)
            self.service._put('tasks', task)
        return task['id']

    def test_each_marker_query_uses_its_partial_index_and_no_scan(self):
        for field in MARKS:
            plan = ' | '.join(row[3] for row in self.service.db.execute(
                f'EXPLAIN QUERY PLAN SELECT rowid, data FROM tasks WHERE {marked(field)}'))
            self.assertIn(f'USING INDEX tasks{field}', plan, plan)

    def test_only_tasks_with_a_true_value_come_back_in_row_order(self):
        first = self.put(_return_pending={'text': 'a'})
        self.put(_return_pending=None)
        self.put(_return_pending={})
        self.put()
        second = self.put(_return_pending={'text': 'b'})
        self.put(_native_reply_pending={'text': 'c'})
        self.assertEqual([t['id'] for t in self.service._marked_tasks('_return_pending')], [first, second])
        self.assertEqual(len(self.service._marked_tasks('_native_reply_pending')), 1)
        self.assertEqual(self.service._marked_tasks('_work_status_only'), [])

    def test_an_unknown_field_is_refused(self):
        with self.assertRaises(ValueError):
            self.service._marked_tasks("x') OR 1=1 --")

    def test_the_dispatcher_passes_read_no_full_table_when_nothing_is_pending(self):
        self.put()
        with patch.object(Service, '_all', side_effect=AssertionError('full table read')):
            self.service._dispatch_status_request()
            self.service._cancel_stale_decision_refreshes()


if __name__ == '__main__':
    unittest.main()
