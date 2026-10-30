import itertools
import json
import unittest

import test_decision_freshness as fixtures


setUpModule, tearDownModule = fixtures.legacy_work_sync.scope()


class TurnContextReadTests(unittest.TestCase):
    """The turn context answers its chain questions with one backward walk and reads its rows through indexes."""

    setUp = fixtures.DecisionFreshnessTests.setUp
    tearDown = fixtures.DecisionFreshnessTests.tearDown
    write = fixtures.DecisionFreshnessTests.write

    def add(self, identity, **fields):
        task = dict(self.base, id=identity, thread_id=identity, **fields)
        self.service._put('tasks', task)
        return task

    def graph(self):
        """A small work graph with every kind of link, a second exchange, a stale epoch and a failed owner."""
        self.base = {k: v for k, v in self.service._get('tasks', self.task['id']).items()
                     if k not in ('_waiting_on', '_assignment_task_id', '_execution_session_id')}
        self.base['status'] = 'completed'
        with self.service.transaction():
            self.add('root', _waiting_on=['waited'])
            self.add('waited', exchange_id='other-exchange')
            self.add('turn', _assignment_task_id='root')
            self.add('owner', _execution_session_id='owner')
            self.add('member', _execution_session_id='owner')
            self.add('relay', source_task_id='member', _native_relay=True)
            self.add('reply', _requester_reply_task_id='relay')
            self.add('stale', _conversation_epoch='old', _waiting_on=['root'])
            self.add('returned', source_task_id='turn', _is_return=True, _assignment_task_id='turn')
            self.add('callback', _relay_callback_owner='returned', exchange_id='third-exchange')
            self.add('loop', _waiting_on=['loop', 'root'])
            self.add('failed-owner', status='failed', _execution_session_id='failed-owner')
            self.add('failed-member', _execution_session_id='failed-owner')
            self.service._put('native_receipts', dict(id='wait-receipt', task_id='reply', turn_id=None, state='completed',
                                                      action={'name': 'coconut_wait'}, _wait_task_id='callback'))

    def test_the_backward_walk_finds_the_roots_of_every_forward_chain(self):
        self.graph()
        with self.service.lock, self.service._shared_reads():
            tasks = self.service._task_map()
            for records, flag in itertools.product(
                    (self.service._shared_work_receipts(), self.service._shared_pending_receipts()), (False, True)):
                contains = {}
                for root in tasks.values():
                    for member in self.service._work_chain(root, tasks, records, include_continuations=flag):
                        contains.setdefault(member, set()).add(root['id'])
                for identity in tasks:
                    with self.subTest(target=identity, continuations=flag):
                        self.assertEqual(self.service._work_reach(identity, tasks, records, include_continuations=flag),
                                         contains.get(identity, set()))
            # The graph has real chains: the turn reaches its assignment, and the stale task is in no chain.
            records = self.service._shared_work_receipts()
            self.assertIn('root', self.service._work_reach('turn', tasks, records, include_continuations=True))
            self.assertEqual(self.service._work_reach('stale', tasks, records, include_continuations=True), set())

    def test_one_pass_builds_the_task_links_with_and_without_continuations(self):
        self.graph()
        from coconut.work_status import WorkStatus
        tasks = {t['id']: t for t in self.service._all('tasks')}
        pairs = WorkStatus._work_task_link_pairs(tasks)
        for flag in (False, True):
            with self.subTest(continuations=flag):
                linked, waits = WorkStatus._work_links(tasks, [], flag)
                self.assertEqual(pairs[flag], (linked, waits))
        self.assertIn('turn', pairs[True][0]['root'])
        self.assertNotIn('turn', pairs[False][0].get('root', set()))

    def plan(self, statement, parameters):
        return ' '.join(row[-1] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + statement, parameters))

    def test_the_recent_work_updates_read_through_their_indexes(self):
        plans = {
            "SELECT id, json_extract(data,'$._turn_started_at') FROM tasks INDEXED BY tasks_turn_started "
            "WHERE json_extract(data,'$.bot_id')=? AND json_extract(data,'$.session_id')=? "
            "AND NOT coalesce(json_extract(data,'$._work_status_only'),0) "
            "AND json_extract(data,'$._turn_started_at') IS NOT NULL "
            "ORDER BY json_extract(data,'$._turn_started_at') DESC LIMIT 2": ('a', 'b'),
            "SELECT m.data FROM tasks t INDEXED BY tasks_status_only "
            "CROSS JOIN messages m ON json_extract(m.data, '$.task_id')=+t.id "
            "WHERE json_extract(t.data,'$.bot_id')=? AND json_extract(t.data,'$.session_id')=? "
            "AND json_extract(t.data,'$._work_status_only')=1": ('a', 'b'),
            "SELECT id FROM tasks INDEXED BY tasks_execution "
            "WHERE json_extract(data,'$._execution_session_id')=? AND json_extract(data,'$.bot_id')=?": ('a', 'b'),
        }
        for statement, parameters in plans.items():
            with self.subTest(statement=statement[:60]):
                detail = self.plan(statement, parameters)
                self.assertIn('SEARCH', detail)
                self.assertNotIn('SCAN m', detail)
        self.assertIn('SEARCH m USING INDEX messages_task', self.plan(list(plans)[1], ('a', 'b')))

    def test_the_recent_work_updates_match_a_scan(self):
        self.base = {k: v for k, v in self.service._get('tasks', self.task['id']).items()}
        with self.service.transaction():
            self.add('earlier', _turn_started_at='2026-10-29T10:00:00Z')
            self.add('later', _turn_started_at='2026-10-29T12:00:00Z')
            self.add('status-check', _work_status_only=True, _work_context_id='work-1',
                     _turn_started_at='2026-10-29T13:00:00Z')
            for index, created in enumerate(('2026-10-29T11:00:00Z', '2026-10-29T12:30:00Z', '2026-10-29T12:40:00Z')):
                self.service._put('messages', dict(id=f'update-{index}', bot_id=self.task['bot_id'], task_id='status-check',
                                                   role='assistant', kind='text', text=f'Update {index}', created_at=created,
                                                   streaming=False, recipient_bot_ids=[], thread_id='status-check'))
        for task_id in ('later', 'earlier', 'status-check'):
            task = self.service._get('tasks', task_id)
            with self.subTest(task=task_id):
                before = self.service.db.execute(
                    "SELECT coalesce(max(json_extract(data,'$._turn_started_at')),'') FROM tasks NOT INDEXED "
                    "WHERE json_extract(data,'$.bot_id')=? AND json_extract(data,'$.session_id')=? AND id!=? "
                    "AND NOT coalesce(json_extract(data,'$._work_status_only'),0)",
                    (task['bot_id'], task['session_id'], task['id'])).fetchone()[0]
                expected = [m for m in (json.loads(row[0]) for row in self.service.db.execute(
                    "SELECT data FROM messages WHERE json_extract(data,'$.task_id')='status-check' ORDER BY rowid"))
                    if m['created_at'] > before][-3:]
                self.assertEqual([u['message_id'] for u in self.service.recent_work_updates(task)],
                                 [m['id'] for m in expected])
        self.assertEqual([u['work_id'] for u in self.service.recent_work_updates(self.service._get('tasks', 'earlier'))],
                         ['work-1', 'work-1'])


if __name__ == '__main__':
    unittest.main()
