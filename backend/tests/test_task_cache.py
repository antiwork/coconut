import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from coconut.providers import FixtureProvider
from coconut.service import Service


class TaskCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(Path(self.temp.name), FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)
        with self.service.transaction():
            for index in range(5):
                self.service._put('tasks', dict(id=f'task-{index}', status='completed', title=f'Task {index}'))

    def stored(self):
        return [(row[0], json.loads(row[1])) for row in self.service.db.execute('SELECT id, data FROM tasks ORDER BY rowid')]

    def cached(self):
        with self.service._shared_reads():
            return list(self.service._task_map().items())

    def test_changes_keep_the_row_order_of_the_table(self):
        self.assertEqual(self.cached(), self.stored())
        with self.service.transaction():
            self.service._put('tasks', dict(id='task-1', status='working', title='Changed'))
            self.service._put('tasks', dict(id='task-new', status='queued', title='New'))
            self.service.db.execute("DELETE FROM tasks WHERE id='task-3'")
        self.service.db.execute("UPDATE tasks SET data=json_set(data,'$.title','Raw') WHERE id='task-0'")
        cached = self.cached()
        self.assertEqual(cached, self.stored())
        self.assertEqual([identity for identity, _ in cached], ['task-0', 'task-1', 'task-2', 'task-4', 'task-new'])
        self.assertEqual(dict(cached)['task-0']['title'], 'Raw')

    def test_a_write_in_the_same_operation_is_visible(self):
        with self.service.transaction(), self.service._shared_reads():
            self.assertEqual(self.service._task_map()['task-2']['status'], 'completed')
            self.service._put('tasks', dict(id='task-2', status='working', title='Task 2'))
            self.assertEqual(self.service._task_map()['task-2']['status'], 'working')
            self.assertEqual([t['id'] for t in self.service._all_shared('tasks')], [i for i, _ in self.stored()])

    def test_a_rollback_drops_its_rows(self):
        self.cached()
        with self.assertRaises(RuntimeError):
            with self.service.transaction(), self.service._shared_reads():
                self.service._put('tasks', dict(id='task-2', status='working', title='Rolled back'))
                self.service._put('tasks', dict(id='task-rolled', status='queued', title='Rolled back'))
                self.assertIn('task-rolled', self.service._task_map())
                raise RuntimeError('stop')
        self.assertEqual(self.cached(), self.stored())
        with self.service.transaction():
            self.service._put('tasks', dict(id='task-after', status='queued', title='After'))
        self.assertEqual(self.cached(), self.stored())

    def full_reads(self):
        return patch.object(self.service, '_read_all_tasks', wraps=self.service._read_all_tasks)

    def test_new_rows_that_change_again_need_no_full_read(self):
        self.cached()
        with self.full_reads() as full:
            with self.service.transaction():
                self.service._put('tasks', dict(id='task-x', status='queued', title='X'))
                self.service._put('tasks', dict(id='task-y', status='queued', title='Y'))
                self.service._put('tasks', dict(id='task-x', status='working', title='X'))
                self.service._put('tasks', dict(id='task-0', status='working', title='Task 0'))
            self.assertEqual(self.cached(), self.stored())
        self.assertEqual(full.call_count, 0)

    def test_a_reused_rowid_reads_the_table_again(self):
        self.cached()
        self.service.db.execute("DELETE FROM tasks WHERE id='task-4'")
        with self.full_reads() as full:
            with self.service.transaction():
                self.service._put('tasks', dict(id='task-late', status='queued', title='Late'))
                self.service._put('tasks', dict(id='task-4', status='queued', title='Back'))
            self.assertEqual(self.cached(), self.stored())
        self.assertEqual(full.call_count, 1)

    def test_a_failed_rollback_still_drops_the_kept_tasks(self):
        class FailingRollback:
            def __init__(self, db):
                self.db = db
            def execute(self, statement, *args):
                result = self.db.execute(statement, *args)
                if statement == 'ROLLBACK':
                    raise sqlite3.OperationalError('cannot rollback')
                return result
            def __getattr__(self, name):
                return getattr(self.db, name)
        self.cached()
        real = self.service.db
        self.service.db = FailingRollback(real)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with self.service.transaction(), self.service._shared_reads():
                    self.service._put('tasks', dict(id='task-ghost', status='queued', title='Ghost'))
                    self.assertIn('task-ghost', self.service._task_map())
                    raise RuntimeError('stop')
        finally:
            self.service.db = real
        with self.service.transaction():
            self.service._put('tasks', dict(id='task-2', status='working', title='Task 2'))
        self.assertEqual(self.cached(), self.stored())

    def test_numbers_that_go_back_start_again_from_a_committed_state(self):
        self.cached()
        seq, tasks, rowids, last = self.service._task_rows
        self.service._task_rows = (seq + 10, tasks, rowids, last)
        # The shared committed state replaces the writer's state. It needs no full read.
        with self.full_reads() as full:
            self.assertEqual(self.cached(), self.stored())
        self.assertEqual(full.call_count, 0)
        # Without a shared state, the writer reads the table again.
        self.service._task_rows = (seq + 10, tasks, rowids, last)
        self.service._task_states.clear()
        with self.full_reads() as full:
            self.assertEqual(self.cached(), self.stored())
        self.assertEqual(full.call_count, 1)

    def test_the_check_finds_a_shared_task_that_a_reader_changed(self):
        self.cached()
        with self.service._shared_reads():
            self.service._task_map()['task-1']['status'] = 'changed without a write'
        with patch.dict(os.environ, COCONUT_VERIFY_TASK_CACHE='1'), self.assertRaises(AssertionError):
            self.cached()

    def test_each_operation_gets_its_own_map(self):
        with self.service._shared_reads():
            first = self.service._task_map()
            first.pop('task-0')
        self.assertIn('task-0', dict(self.cached()))

    def test_a_read_view_does_not_use_the_writer_cache(self):
        self.cached()
        with self.service._read_view() as view:
            self.assertIsNone(view._task_rows)
            with view._shared_reads():
                self.assertEqual(list(view._task_map()), [i for i, _ in self.stored()])
        self.assertIsNotNone(self.service._task_rows)

    def view_tasks(self):
        with self.service._read_view() as view, view._read_transaction(), view._shared_reads():
            return view._task_map(), view._task_rows

    def test_a_read_view_starts_from_the_shared_state_and_shares_its_tasks(self):
        self.cached()
        writer = self.service._task_rows
        with patch.object(Service, '_read_all_tasks', side_effect=AssertionError('A view must not read every task.')):
            tasks, state = self.view_tasks()
        self.assertEqual(list(tasks.items()), self.stored())
        # The view holds the same task objects as the writer, not a second parse.
        self.assertTrue(all(tasks[identity] is writer[1][identity] for identity in tasks))
        with self.service.transaction():
            self.service._put('tasks', dict(id='task-1', status='working', title='Changed'))
        tasks, later = self.view_tasks()
        self.assertEqual(list(tasks.items()), self.stored())
        self.assertIsNot(tasks['task-1'], writer[1]['task-1'])
        self.assertIs(tasks['task-2'], writer[1]['task-2'])
        # The older states did not change.
        self.assertEqual(state[1]['task-1']['status'], 'completed')
        self.assertEqual(writer[1]['task-1']['status'], 'completed')

    def test_a_state_with_uncommitted_rows_is_not_shared(self):
        self.cached()
        with self.service.transaction(), self.service._shared_reads():
            self.service._put('tasks', dict(id='task-uncommitted', status='queued', title='Not yet'))
            self.assertIn('task-uncommitted', self.service._task_map())
            tasks, _ = self.view_tasks()
            self.assertNotIn('task-uncommitted', tasks)
            self.assertTrue(all(state[0] < self.service._latest_task_change() for state in self.service._task_states.states))
        tasks, _ = self.view_tasks()
        self.assertIn('task-uncommitted', tasks)

    def test_a_rolled_back_state_never_reaches_a_view(self):
        self.cached()
        with self.assertRaises(RuntimeError):
            with self.service.transaction(), self.service._shared_reads():
                self.service._put('tasks', dict(id='task-ghost', status='queued', title='Ghost'))
                self.service._task_map()
                raise RuntimeError('stop')
        tasks, _ = self.view_tasks()
        self.assertNotIn('task-ghost', tasks)
        self.assertEqual(self.cached(), self.stored())

    def test_a_renumbering_vacuum_starts_a_new_generation(self):
        self.cached()
        with self.service._read_view() as view, view._read_transaction():
            self.service._forget_cached_tasks(renumbered=True)
            self.assertEqual(self.service._task_states.states, [])
            with self.full_reads(), patch.object(Service, '_read_all_tasks', wraps=view._read_all_tasks) as full:
                with view._shared_reads():
                    view._task_map()
            # The view started before the new generation, so it neither uses nor adds a state.
            self.assertEqual(full.call_count, 1)
            self.assertEqual(self.service._task_states.states, [])
        self.cached()
        self.assertEqual(len(self.service._task_states.states), 1)


if __name__ == '__main__':
    unittest.main()
