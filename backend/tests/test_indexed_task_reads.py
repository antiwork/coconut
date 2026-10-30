import unittest

from coconut.work_capacity import execution_key
from coconut.work_status import PROPOSAL_EXECUTION
import test_decision_freshness as fixtures


setUpModule, tearDownModule = fixtures.legacy_work_sync.scope()


class IndexedTaskReadTests(unittest.TestCase):
    """Turn completion and work continuation read their few tasks through indexes, not the whole task table."""

    setUp = fixtures.DecisionFreshnessTests.setUp
    tearDown = fixtures.DecisionFreshnessTests.tearDown
    write = fixtures.DecisionFreshnessTests.write

    def add(self, identity, **fields):
        self.service._put('tasks', dict(self.task, id=identity, **fields))

    def plan(self, statement, parameters):
        return ' '.join(row[-1] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + statement, parameters))

    def test_execution_tasks_match_a_scan_of_every_task(self):
        bot_id = self.task['bot_id']
        with self.service.transaction():
            self.add('session-owner', _execution_session_id='session-owner')
            self.add('session-turn', _execution_session_id='session-owner')
            self.add('empty-session', _execution_session_id='')
            self.add('status-only', _work_status_only=True, _execution_session_id='session-owner')
            self.add('other-bot', bot_id='other-bot-id')
        tasks = self.service._all('tasks')
        for key in ('session-owner', bot_id, 'status-only', 'other-bot-id'):
            with self.subTest(key=key):
                expected = [task for task in tasks if execution_key(task) == key]
                self.assertTrue(expected)
                self.assertEqual(self.service._execution_tasks(key), expected)
        self.assertEqual([t['id'] for t in self.service._execution_tasks('session-owner')], ['session-owner', 'session-turn'])

    def test_task_reads_use_indexes(self):
        plans = {
            "SELECT data FROM tasks WHERE json_extract(data,'$._execution_session_id')=? OR json_extract(data,'$.bot_id')=? "
            "OR id=? ORDER BY rowid": ('tasks_execution', 'tasks_bot'),
            "SELECT data FROM tasks WHERE id=? OR json_extract(data,'$._relay_callback_owner')=? ORDER BY rowid":
                ('tasks_relay_callback_owner',),
            "SELECT id FROM tasks WHERE id=? OR json_extract(data, '$._assignment_task_id')=?": ('tasks_assignment',),
            "SELECT data FROM deliveries WHERE json_extract(data,'$.source_task_id') IN (SELECT value FROM json_each(?))":
                ('deliveries_source',),
        }
        for statement, indexes in plans.items():
            with self.subTest(statement=statement):
                detail = self.plan(statement, ('[]',) if 'json_each' in statement else ('x',) * statement.count('?'))
                for index in indexes:
                    self.assertIn(index, detail)
                self.assertNotIn('SCAN tasks', detail)

    def test_callback_turns_of_an_owner_come_from_the_index(self):
        with self.service.transaction():
            self.add('owner')
            self.add('callback-a', _relay_callback_owner='owner')
            self.add('unrelated', _relay_callback_owner='someone-else')
            self.add('callback-b', _relay_callback_owner='owner')
        rows = self.service.db.execute(
            "SELECT id FROM tasks WHERE id=? OR json_extract(data,'$._relay_callback_owner')=? ORDER BY rowid", ('owner', 'owner'))
        self.assertEqual([row[0] for row in rows], ['owner', 'callback-a', 'callback-b'])

    def test_executed_proposals_come_from_their_partial_index(self):
        decision = dict(id='proposal', bot_id=self.task['bot_id'], task_id=self.task['id'], status='executed',
                        inbox_kind='proposal', _execution_task_id='execution')
        with self.service.transaction():
            self.service._put('decisions', decision)
            self.service._put('decisions', dict(decision, id='question', inbox_kind=None))
            self.service._put('decisions', dict(decision, id='unstarted', _execution_task_id=None))
        statement = 'SELECT id FROM decisions INDEXED BY decisions_proposal_execution WHERE ' + PROPOSAL_EXECUTION
        self.assertEqual([row[0] for row in self.service.db.execute(statement)], ['proposal'])
        details = [row[-1] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + statement)]
        # SQLite scans the small partial index, never the whole card table.
        self.assertEqual(details, ['SCAN decisions USING INDEX decisions_proposal_execution'])


if __name__ == '__main__':
    unittest.main()
