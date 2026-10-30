import threading
import unittest
from unittest.mock import patch

from coconut.decision_freshness import HUMAN_MESSAGE, PLAN_ATTEMPTS
import test_decision_freshness as fixtures


setUpModule, tearDownModule = fixtures.legacy_work_sync.scope()


class CardPassLockTests(unittest.TestCase):
    """A card pass reads a snapshot without the service lock. It holds the lock only to write."""

    setUp = fixtures.DecisionFreshnessTests.setUp
    tearDown = fixtures.DecisionFreshnessTests.tearDown
    start = fixtures.DecisionFreshnessTests.start
    write = fixtures.DecisionFreshnessTests.write
    question = fixtures.DecisionFreshnessTests.question

    def plans(self, during_plan):
        """Run one card pass. during_plan(number) runs at the start of each plan, before the plan reads."""
        service_type = type(self.service)
        original = service_type._plan_decision_refreshes
        calls = []

        def plan(service, writes):
            calls.append(service)
            during_plan(len(calls))
            return original(service, writes)

        with patch.object(service_type, '_plan_decision_refreshes', plan):
            self.service._queue_decision_refreshes()
        return calls

    def queued_checks(self):
        return [c for c in self.service._all('contexts') if c.get('_decision_refresh') and c['status'] == 'queued']

    def test_another_thread_takes_the_lock_while_a_card_pass_plans(self):
        decision = self.question()
        acquired = []

        def take_lock(number):
            def worker():
                if self.service.lock.acquire(timeout=5):
                    acquired.append(True)
                    self.service.lock.release()
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(10)

        calls = self.plans(take_lock)
        self.assertEqual(acquired, [True])
        self.assertIsNot(calls[0], self.service, 'The plan must read a view, not the writer.')
        self.assertEqual([c['decision_id'] for c in self.queued_checks()], [decision['id']])

    def test_a_card_that_changes_during_the_plan_is_planned_again(self):
        decision = self.question()

        def change_card(number):
            if number == 1:
                with self.service.transaction():
                    saved = self.service._get('decisions', decision['id'])
                    saved['title'] = 'Install the new test dependencies?'
                    self.service._put('decisions', saved)

        calls = self.plans(change_card)
        self.assertEqual(len(calls), 2)
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['title'], 'Install the new test dependencies?', 'The pass must not write over a newer card.')
        [check] = self.queued_checks()
        self.assertEqual(saved['_freshness']['context_id'], check['id'])
        self.assertIn('Install the new test dependencies?', check['question'])

    def test_evidence_that_changes_during_the_plan_is_planned_again(self):
        decision = self.question()

        def change_task(number):
            if number == 1:
                with self.service.transaction():
                    task = self.service._get('tasks', decision['task_id'])
                    task['summary'] = 'The pull request got a new commit.'
                    self.service._put('tasks', task)

        calls = self.plans(change_task)
        self.assertEqual(len(calls), 2)
        [check] = self.queued_checks()
        with self.service.lock, self.service._shared_reads():
            tasks = self.service._task_map()
            artifacts = self.service._all('artifacts')
            current = self.service._get('decisions', decision['id'])
            records = self.service._decision_records(current, tasks, artifacts)
            fingerprint, _ = self.service._decision_evidence(current, tasks, records, artifacts,
                                                             profile_revision=current['_profile_revision'])
        self.assertEqual(check['_evidence_fingerprint'], fingerprint, 'A check must start with the current evidence.')

    def test_a_card_that_keeps_changing_is_planned_and_written_under_the_lock(self):
        decision = self.question()

        def change_card(number):
            if number <= PLAN_ATTEMPTS:
                with self.service.transaction():
                    saved = self.service._get('decisions', decision['id'])
                    saved['title'] = f'Change {number}'
                    self.service._put('decisions', saved)
            else:
                self.assertTrue(self.service.lock._is_owned())

        with self.assertLogs('coconut.decision_freshness', level='WARNING'):
            calls = self.plans(change_card)
        self.assertEqual(len(calls), PLAN_ATTEMPTS + 1)
        self.assertIs(calls[-1], self.service)
        [check] = self.queued_checks()
        self.assertIn(f'Change {PLAN_ATTEMPTS}', check['question'])

    def test_a_check_queued_meanwhile_stops_a_second_check(self):
        decision = self.question()

        def queue_other_check(number):
            if number == 1:
                with self.service.transaction():
                    self.service._put('contexts', dict(
                        id='other-check', bot_id=decision['bot_id'], task_id=decision['task_id'],
                        decision_id='another-card', status='queued', _decision_refresh=True))

        calls = self.plans(queue_other_check)
        # The second plan sees the other check and queues nothing.
        self.assertEqual(len(calls), 2)
        self.assertEqual([c['id'] for c in self.queued_checks()], ['other-check'])

    def test_a_caller_that_holds_the_lock_gets_the_direct_pass(self):
        decision = self.question()
        with self.service.lock, patch.object(type(self.service), '_card_pass_view',
                                             side_effect=AssertionError('A view cannot see the caller writes.')):
            self.service._queue_decision_refreshes()
        self.assertEqual([c['decision_id'] for c in self.queued_checks()], [decision['id']])

    def test_a_pass_without_changes_does_not_take_the_lock_to_write(self):
        self.question()
        self.service._queue_decision_refreshes()
        with patch.object(type(self.service), 'transaction', side_effect=AssertionError('Nothing to write.')):
            self.service._queue_decision_refreshes()
        self.assertEqual(len(self.queued_checks()), 1)

    def test_a_new_card_and_its_follow_up_read_no_whole_task_table(self):
        decision = self.question()
        original = type(self.service)._all

        forbidden = {'tasks'}

        def guarded(service, table):
            if table in forbidden:
                raise AssertionError(f'A card event must not parse every row of {table}.')
            return original(service, table)

        task = self.service._get('tasks', decision['task_id'])
        saved = self.service._get('decisions', decision['id'])

        def initialize():
            with self.service.transaction():
                self.service._initialize_decision_freshness(dict(saved), profile_revision=saved['_profile_revision'])
            return self.service._get('decisions', decision['id'])['_freshness']['fingerprint']

        expected = initialize()
        with patch.object(type(self.service), '_all', guarded):
            # The evidence reads the kept task map. It still reads the cards once, as the card pass does.
            self.assertEqual(initialize(), expected)
            forbidden.add('decisions')
            # An unanswered card is not a valid follow-up. The check reads the card and its task by ID.
            with self.assertRaisesRegex(Exception, 'Link an answered question'):
                self.service.decision_followup(task, {'previous_decision_id': decision['id']})
            self.assertEqual(self.service.decision_followup(task, {}), {})

    def test_card_reads_use_indexes(self):
        plans = {
            'SELECT rowid, data FROM messages INDEXED BY messages_human WHERE ' + HUMAN_MESSAGE: 'messages_human',
            "SELECT data FROM tasks WHERE json_extract(data,'$.bot_id')=? "
            "AND json_extract(data,'$.status') IN ('queued','working','waiting')": 'tasks_bot_status',
            "SELECT data FROM messages WHERE json_extract(data,'$.task_id')=? AND " + HUMAN_MESSAGE: 'messages_task',
        }
        for statement, index in plans.items():
            with self.subTest(index=index):
                parameters = (None,) * statement.count('?')
                detail = ' '.join(row[-1] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + statement, parameters))
                self.assertIn(index, detail)

    def test_human_messages_come_from_the_partial_index_in_row_order(self):
        self.question()
        with self.service.transaction():
            for number in range(3):
                self.service._put('messages', dict(
                    id=f'z-human-{number}', bot_id=self.bot['id'], task_id=self.task['id'], thread_id=self.task['thread_id'],
                    role='user', kind='text', text=f'Direction {number}', created_at='2999-01-01T00:00:0%dZ' % number,
                    recipient_bot_ids=[self.bot['id']], _human_device_id='device'))
            self.service._put('messages', dict(
                id='a-device-less', bot_id=self.bot['id'], task_id=self.task['id'], thread_id=self.task['thread_id'],
                role='user', kind='text', text='No device', created_at='2999-01-01T00:00:09Z',
                recipient_bot_ids=[self.bot['id']]))
        indexed = [row[0] for row in self.service.db.execute(
            'SELECT id FROM messages INDEXED BY messages_human WHERE ' + HUMAN_MESSAGE)]
        scanned = [m['id'] for m in self.service._all('messages') if m.get('_human_device_id') is not None]
        self.assertEqual(sorted(indexed), sorted(scanned))
        decision = self.service._all('decisions')[-1]
        task = self.service._get('tasks', decision['task_id'])
        found = [m['id'] for m in self.service._decision_human_messages(task, decision)]
        self.assertEqual(found[-3:], ['z-human-0', 'z-human-1', 'z-human-2'])
        self.assertNotIn('a-device-less', found)


if __name__ == '__main__':
    unittest.main()
