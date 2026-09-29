"""One grouping of the tasks gives every routine the answer that a scan of all tasks gave."""
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service

STATES = ('queued', 'working', 'waiting', 'needs_you', 'paused', 'scheduled', 'completed', 'failed', 'cancelled')


def reference(service, tasks, routine_id):
    """The earlier rule: read every task again for each routine."""
    related = {task['id'] for task in tasks if task.get('routine_id') == routine_id
               and (not task.get('_native_job_finished') or (
                   task['status'] in ('queued', 'working')
                   and service.native_routine_completion_input(task).get('continuation')))}
    while True:
        expanded = related | {task['id'] for task in tasks if task.get('source_task_id') in related}
        if expanded == related:
            break
        related = expanded
    active = [task for task in tasks if task['id'] in related and (task['status'] in service.ACTIVE or task.get('_return_pending'))]
    return next((task for task in active if task['status'] != 'waiting'), active[0] if active else None)


class RoutineIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / 'coconut'
        self.service = Service(root, FixtureProvider(root), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'One', 'role': ''}, str(uuid4()))

    def put(self, **fields):
        row = dict(id=str(uuid4()), bot_id=self.bot['id'], status='queued', created_at='2026-01-01T00:00:00Z')
        row.update(fields)
        with self.service.transaction():
            self.service._put('tasks', row)
        return row['id']

    def test_random_task_graphs_give_the_answer_of_the_scan(self):
        rng = random.Random(7)
        routines = ['r1', 'r2', 'r3']
        ids = []
        for _ in range(60):
            fields = {'status': rng.choice(STATES)}
            if rng.random() < .4:
                fields['routine_id'] = rng.choice(routines)
                if rng.random() < .5:
                    fields['_native_job_finished'] = True
            if ids and rng.random() < .5:
                fields['source_task_id'] = rng.choice(ids)
            if rng.random() < .1:
                fields['_return_pending'] = {'text': 'x'}
            ids.append(self.put(**fields))
        tasks = self.service._all('tasks')
        index = self.service._routine_index()
        for routine in routines + ['none']:
            expected = reference(self.service, tasks, routine)
            for given in (index, None):
                actual = self.service._active_routine_task(routine, given)
                self.assertEqual(actual and actual['id'], expected and expected['id'], routine)

    def test_a_pass_reads_the_tasks_once_for_all_routines(self):
        self.put(routine_id='r1', status='working')
        reads = []
        original = Service._all
        def counted(service, table):
            reads.append(table)
            return original(service, table)
        with patch.object(Service, '_all', counted):
            index = self.service._routine_index()
            for routine in ('r1', 'r2', 'r3', 'r4'):
                self.service._active_routine_task(routine, index)
        self.assertEqual(reads, ['tasks'])

    def test_the_routine_lookups_use_an_index_and_scan_no_table(self):
        statements = {
            'events by task': ('SELECT type,data,created_at FROM events WHERE task_id=? AND bot_id=? ORDER BY seq', ('t', 'b')),
            'deliveries by receipt': ("SELECT data FROM deliveries WHERE json_extract(data,'$.receipt.task_id')=? ORDER BY rowid DESC", ('t',)),
        }
        for name, (statement, values) in statements.items():
            plan = ' | '.join(row[3] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + statement, values))
            self.assertIn('USING INDEX', plan, (name, plan))
            self.assertNotIn('SCAN', plan, (name, plan))


if __name__ == '__main__':
    unittest.main()
