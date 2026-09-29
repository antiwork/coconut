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
        return self.put_with(str(uuid4()), **fields)

    def put_with(self, identity, **fields):
        row = dict(id=identity, bot_id=self.bot['id'], status='queued', created_at='2026-01-01T00:00:00Z')
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

    def test_the_lookups_of_the_continuation_check_use_an_index_and_scan_no_table(self):
        from coconut import routine_coordinator
        # The statements that the routine pass runs, not copies of them.
        statements = {'events of a routine task': routine_coordinator.ROUTINE_EVENTS,
                      'events of a completion': routine_coordinator.COMPLETION_EVENTS,
                      'deliveries of a task': routine_coordinator.ROUTINE_DELIVERIES}
        for name, statement in statements.items():
            values = ('t', 'b')[:statement.count('?')]
            plan = ' | '.join(row[3] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + statement, values))
            self.assertIn('USING INDEX', plan, (name, plan))
            self.assertNotIn('SCAN', plan, (name, plan))

    def test_a_queued_continuation_keeps_its_routine_busy_as_the_scan_says(self):
        from uuid import UUID, uuid5
        bot_id, identity, source = self.bot['id'], str(uuid4()), self.put()
        job = dict(id='job1', fire_claim={'by': 'owner1'})
        routine_id = str(uuid5(UUID(bot_id), 'cron:job1'))
        text = 'Continue with the report.'
        self.put_with(identity, status='working', routine_id=routine_id, _native_job=job, _native_job_finished=True,
                      _native_outcome_id='n1', _native_turn_id='turn1', _self_recovery=dict(previous_task_id=identity),
                      _bot_messages=[dict(sender_bot_id=bot_id, text=text)])
        with self.service.transaction():
            self.service._event('routine.triggered', dict(routine_id=routine_id, task_id=identity), bot_id, identity)
            self.service._put('deliveries', dict(id=str(uuid4()), receipt=dict(task_id=identity, native_task_id='n1', status='queued'),
                                                 target_bot_id=bot_id, turn_id='turn1', created_at='2999-01-01T00:00:00Z',
                                                 source_task_id=source, text=text))
        task = self.service._get('tasks', identity)
        self.assertTrue(self.service.native_routine_completion_input(task).get('continuation'), 'The branch under test runs.')
        expected = reference(self.service, self.service._all('tasks'), routine_id)
        self.assertEqual(expected['id'], identity)
        self.assertEqual(self.service._active_routine_task(routine_id, self.service._routine_index())['id'], identity)
        # Once the continuation is gone, the closed occurrence releases the routine, in both rules.
        task['status'] = 'completed'
        with self.service.transaction():
            self.service._put('tasks', task)
        self.assertIsNone(reference(self.service, self.service._all('tasks'), routine_id))
        self.assertIsNone(self.service._active_routine_task(routine_id, self.service._routine_index()))

    def test_a_tick_with_no_due_routine_reads_no_task(self):
        with self.service.transaction():
            self.service._put('routines', dict(id='r1', bot_id=self.bot['id'], enabled=True, next_run_at='2999-01-01T00:00:00Z'))
        reads = []
        original = Service._all
        def counted(service, table):
            reads.append(table)
            return original(service, table)
        with patch.object(Service, '_all', counted):
            self.service.tick_routines()
        self.assertNotIn('tasks', reads)


if __name__ == '__main__':
    unittest.main()
