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
        self.put(_work_status_only=False)
        self.put()
        second = self.put(_return_pending={'text': 'b'})
        self.assertEqual([t['id'] for t in self.service._marked_tasks('_return_pending')], [first, second])
        self.assertEqual(self.service._marked_tasks('_work_status_only'), [])
        rows = self.service.db.execute(f"SELECT COUNT(*) FROM tasks WHERE {marked('_work_status_only')}").fetchone()[0]
        self.assertEqual(rows, 0, 'A false marker is not indexed.')

    def test_a_start_drops_the_index_of_a_retired_marker(self):
        root = Path(self.temp.name) / 'earlier'
        earlier = Service(root, FixtureProvider(root), start_workers=False)
        # An earlier version indexed the reply of a worker's relay.
        earlier.db.execute("CREATE INDEX tasks_native_reply_pending ON tasks (id) WHERE "
                           "json_type(data,'$._native_reply_pending') IN ('object','true')")
        earlier.close()
        service = Service(root, FixtureProvider(root), start_workers=False)
        self.addCleanup(service.close)
        names = {row[0] for row in service.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertNotIn('tasks_native_reply_pending', names)
        with self.assertRaises(ValueError):
            service._marked_tasks('_native_reply_pending')

    def test_an_unknown_field_is_refused(self):
        with self.assertRaises(ValueError):
            self.service._marked_tasks("x') OR 1=1 --")

    def test_the_dispatcher_passes_read_no_full_table_when_nothing_is_pending(self):
        self.put()
        with patch.object(Service, '_all', side_effect=AssertionError('full table read')):
            self.service._dispatch_status_request()
            self.service._cancel_stale_decision_refreshes()

    def test_the_rows_of_many_recovering_tasks_decode_each_table_once(self):
        from collections import Counter
        from types import SimpleNamespace
        base = dict(thread_id='t', session_id='s', exchange_id='e', conversation_bot_id=self.bot['id'], title='Work',
                    created_at='2026-01-01T00:00:00Z', summary='')
        decision = dict(id=str(uuid4()), task_id='none', status='approved', decided_by='device', decided_at='2026-01-02T00:00:00Z',
                        created_at='2026-01-01T00:00:00Z')
        with self.service.transaction():
            self.service._put('decisions', decision)
        for number in range(6):
            identity = str(uuid4())
            with self.service.transaction():
                self.service._put('tasks', dict(base, id=identity, bot_id=self.bot['id'], status='queued',
                                                _self_recovery={'attempt': 1}, _execution_session_id='session'))
                self.service._put('artifacts', dict(id=str(uuid4()), kind='work', status='working', task_id=identity,
                                                    bot_id=self.bot['id'], title='Work', created_at='2026-01-01T00:00:00Z',
                                                    paused_by_decision_id=decision['id'], _work_pause_at='2026-01-02T00:00:00Z'))
        self.service.kanban = SimpleNamespace(tasks={}, close=lambda: None)
        reads, original = Counter(), Service._all
        def counted(service, table):
            reads[table] += 1
            return original(service, table)
        checks = []
        direction = Service._work_has_new_direction
        def watched(service, *arguments):
            checks.append(arguments[0]['id'])
            return direction(service, *arguments)
        with patch.object(Service, '_all', counted), patch.object(Service, '_work_has_new_direction', watched):
            rows = self.service._kanban_rows()
        self.assertEqual(len(rows), 6)
        self.assertEqual(len(checks), 6, 'Every task reaches the new direction check.')
        self.assertTrue(all(count <= 1 for count in reads.values()), reads)


if __name__ == '__main__':
    unittest.main()
