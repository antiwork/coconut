import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.hermes_worker import conversation_input
from coconut.providers import FixtureProvider
from coconut.service import Service
from coconut.work_recovery import BLOCKED_WORK_INSTRUCTIONS, block_check_delay


class BlockedWorkCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(self.tmp.name, FixtureProvider(self.tmp.name), start_workers=False)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)
        bot = self.service.mutate('POST', '/v1/bots', {'name': 'Builder', 'role': ''}, str(uuid4()))
        task = self.service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Build the record', 'mode': 'queue'}, str(uuid4()))
        with self.service.transaction():
            saved = self.service._get('tasks', task['id'])
            saved.update(status='completed', _native_outcome_id='t_build')
            self.service._put('tasks', saved)
        self.task_id = task['id']

    def check(self, native, clock):
        with patch('coconut.native_outcomes.time.time', return_value=clock), self.service.transaction():
            self.service._native_block_check(self.service._get('tasks', self.task_id), native)
        return self.service._get('tasks', self.task_id)

    def state(self, status):
        with self.service.transaction():
            task = self.service._get('tasks', self.task_id)
            task['status'] = status
            self.service._put('tasks', task)

    def blocked(self, kind='capability'):
        return dict(id='t_build', status='blocked', block_kind=kind)

    def test_blocked_outcome_gets_an_owner_check_then_backs_off(self):
        first = self.check(self.blocked(), 1000)
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['_block_check']['next_at'], 1000 + block_check_delay(0))
        self.assertEqual(self.check(self.blocked(), 1000 + block_check_delay(0) - 1)['status'], 'completed')
        queued = self.check(self.blocked(), 1000 + block_check_delay(0))
        self.assertEqual(queued['status'], 'queued')
        self.assertEqual(queued['summary'], 'Checking why this work is blocked')
        self.assertEqual(queued['_self_recovery']['blocked']['kind'], 'capability')
        self.assertEqual(queued['_block_check']['attempt'], 1)
        self.assertGreater(block_check_delay(2), block_check_delay(1))
        self.assertEqual(block_check_delay(20), 21600)
        with self.service.transaction():
            queued['status'] = 'completed'
            self.service._put('tasks', queued)
        again = self.check(self.blocked(), 1000 + block_check_delay(0) + block_check_delay(1) - 1)
        self.assertEqual(again['status'], 'completed')

    def test_pending_card_or_other_block_kind_stops_the_check(self):
        self.check(self.blocked(), 0)
        with self.service.transaction():
            self.service._put('decisions', dict(id=str(uuid4()), task_id=self.task_id, bot_id='b', status='pending'))
        self.assertEqual(self.check(self.blocked(), 10 ** 9)['status'], 'completed')
        for kind in ('dependency', 'transient'):
            self.assertNotIn('_self_recovery', self.check(self.blocked(kind), 10 ** 9))

    def test_waiting_dependency_or_background_process_stops_the_check(self):
        self.check(self.blocked(), 0)
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved['_waiting_on'] = ['dependency']
            self.service._put('tasks', saved)
        self.assertEqual(self.check(self.blocked(), 10 ** 9)['status'], 'completed')
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved.pop('_waiting_on')
            self.service._put('tasks', saved)
        with patch.object(self.service, '_background_pending', return_value=True):
            self.assertEqual(self.check(self.blocked(), 10 ** 9)['status'], 'completed')

    def test_unblocked_outcome_clears_the_check(self):
        self.check(self.blocked(), 0)
        self.assertEqual(self.check(self.blocked(), 10 ** 9)['status'], 'queued')
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved['status'] = 'completed'
            self.service._put('tasks', saved)
        # A running check turn or a scheduled recheck keeps the backoff.
        for status in ('running', 'scheduled'):
            self.assertEqual(self.check(dict(id='t_build', status=status), 10 ** 9 + 1)['_block_check']['attempt'], 1)
        cleared = self.check(dict(id='t_build', status='done'), 10 ** 9 + 1)
        self.assertNotIn('_block_check', cleared)
        self.assertNotIn('blocked', cleared.get('_self_recovery', {}))

    def test_repeated_block_in_native_triage_still_gets_checks(self):
        self.check(dict(self.blocked(), status='triage'), 0)
        self.assertEqual(self.check(dict(self.blocked(), status='triage'), 10 ** 9)['status'], 'queued')

    def test_deferred_check_moves_its_due_time(self):
        self.check(self.blocked(), 0)
        with patch.object(self.service, '_task_work_is_paused', return_value=True):
            deferred = self.check(self.blocked(), 10 ** 9)
        self.assertEqual(deferred['_block_check']['next_at'], 10 ** 9 + block_check_delay(1))

    def test_block_after_a_clear_starts_its_own_delay_and_keeps_the_backoff(self):
        self.check(self.blocked(), 0)
        queued = self.check(self.blocked(), 10 ** 9)
        with self.service.transaction():
            queued['status'] = 'completed'
            self.service._put('tasks', queued)
        cleared = self.check(dict(id='t_build', status='ready'), 10 ** 9 + 5)
        self.assertTrue(cleared['_block_check']['cleared'])
        self.assertNotIn('blocked', cleared.get('_self_recovery', {}))
        again = self.check(self.blocked(), 2 * 10 ** 9)['_block_check']
        self.assertEqual((again['since'], again['attempt']), (2 * 10 ** 9, 1))
        self.assertEqual(again['next_at'], 2 * 10 ** 9 + block_check_delay(1))

    def test_check_turn_takes_a_reply_held_from_a_failed_turn(self):
        self.check(self.blocked(), 0)
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved.update(status='failed', _steering=['Use the smaller fixture'], steering_status='blocked',
                         _blocked_followup=dict(source_ids=['message-1'], attachment_ids=[]))
            self.service._put('tasks', saved)
        queued = self.check(self.blocked(), 10 ** 9)
        self.assertEqual(queued['status'], 'queued')
        self.assertNotIn('_blocked_followup', queued)
        self.assertEqual(queued['steering_status'], 'continued')
        self.assertIn('message-1', queued['_preference_source_ids'])

    def test_human_reply_replaces_a_queued_check(self):
        self.check(self.blocked(), 0)
        queued = self.check(self.blocked(), 10 ** 9)
        self.assertIn('blocked', queued['_self_recovery'])
        with self.service.transaction():
            self.service._reply(self.service._get('tasks', self.task_id), {'text': 'Free space is back. Continue.'})
        self.assertNotIn('_self_recovery', self.service._get('tasks', self.task_id))

    def test_pending_card_of_another_outcome_does_not_defer_the_check(self):
        self.check(self.blocked(), 0)
        other = self.service.mutate('POST', f"/v1/bots/{self.service._get('tasks', self.task_id)['bot_id']}/messages",
                                    {'text': 'Unrelated question', 'mode': 'queue'}, str(uuid4()))
        with self.service.transaction():
            self.service._put('decisions', dict(id=str(uuid4()), task_id=other['id'], bot_id='b', status='pending'))
            saved = self.service._get('tasks', self.task_id)
            saved.pop('_execution_session_id', None)
            self.service._put('tasks', saved)
        queued = self.check(self.blocked(), 10 ** 9)
        self.assertEqual(queued['status'], 'queued')
        self.assertNotIn('attempt', queued['_self_recovery'])

    def test_live_native_run_stops_the_check(self):
        self.check(self.blocked(), 0)
        for field in ('current_run_id', 'worker_pid'):
            self.assertEqual(self.check(dict(self.blocked(), **{field: 7}), 10 ** 9)['status'], 'completed')

    def test_retry_after_a_cleared_block_drops_the_block_record(self):
        self.check(self.blocked(), 0)
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved['_self_recovery'] = dict(attempt=1, blocked=dict(kind='capability', turn='old'))
            self.service._put('tasks', saved)
        with self.service.transaction():
            self.service._native_retry(self.service._get('tasks', self.task_id), dict(status='ready'))
        self.assertNotIn('blocked', self.service._get('tasks', self.task_id)['_self_recovery'])

    def test_check_carries_earlier_answers_of_the_outcome(self):
        self.check(self.blocked(), 0)
        with self.service.transaction():
            self.service._put('decisions', dict(id='declined', task_id=self.task_id, bot_id='b', status='rejected',
                                                title='Free 8 GB?', options=[], revision=1))
        queued = self.check(self.blocked(), 10 ** 9)
        self.assertIn('declined', [d['id'] for d in queued['_self_recovery']['decisions']])

    def test_retry_turn_clears_the_check_so_the_next_block_settles(self):
        self.check(self.blocked(), 0)
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved['status'] = 'queued'
            self.service._put('tasks', saved)
        self.assertTrue(self.check(dict(id='t_build', status='ready'), 10 ** 9)['_block_check']['cleared'])
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved['status'] = 'completed'
            self.service._put('tasks', saved)
        again = self.check(self.blocked(), 2 * 10 ** 9)
        self.assertEqual(again['status'], 'completed')
        self.assertEqual(again['_block_check']['next_at'], 2 * 10 ** 9 + block_check_delay(0))

    def test_short_block_kind_keeps_the_pending_due_time(self):
        first = self.check(self.blocked(), 0)['_block_check']
        self.check(self.blocked('transient'), 5)
        self.assertEqual(self.service._get('tasks', self.task_id)['_block_check'], first)
        self.assertEqual(self.check(self.blocked(), block_check_delay(0))['status'], 'queued')

    def test_earlier_conversation_is_not_resumed(self):
        self.check(self.blocked(), 0)
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved['_conversation_epoch'] = 'earlier'
            self.service._put('tasks', saved)
        self.assertEqual(self.check(self.blocked(), 10 ** 9)['status'], 'completed')

    def test_failed_turn_keeps_its_unread_input(self):
        self.check(self.blocked(), 0)
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved.update(status='failed', _input_after_message_id='before-request', _turn_message_ids=['request'])
            self.service._put('tasks', saved)
        queued = self.check(self.blocked(), 10 ** 9)
        self.assertEqual((queued['status'], queued['_input_after_message_id']), ('queued', 'before-request'))

    def test_retry_owns_the_context_so_a_reply_keeps_it(self):
        self.check(self.blocked(), 0)
        queued = self.check(self.blocked(), 10 ** 9)
        self.assertTrue(queued['_self_recovery']['from_block_check'])
        with self.service.transaction():
            queued['status'] = 'completed'
            self.service._put('tasks', queued)
            self.service._native_retry(self.service._get('tasks', self.task_id), dict(status='ready', last_failure_error='boom'))
        with self.service.transaction():
            self.service._reply(self.service._get('tasks', self.task_id), {'text': 'Any update?'})
        recovery = self.service._get('tasks', self.task_id)['_self_recovery']
        self.assertNotIn('from_block_check', recovery)
        self.assertEqual(recovery['failure'], 'boom')

    def test_human_reply_keeps_tool_calls_without_a_result(self):
        self.check(self.blocked(), 0)
        with self.service.transaction():
            receipt = dict(id=str(uuid4()), task_id=self.task_id, state='executing', action={'name': 'terminal'})
            self.service._put('native_receipts', receipt)
        queued = self.check(self.blocked(), 10 ** 9)
        self.assertEqual(queued['_self_recovery']['unknown_tools'], [receipt])
        with self.service.transaction():
            self.service._reply(self.service._get('tasks', self.task_id), {'text': 'Free space is back. Continue.'})
        recovery = self.service._get('tasks', self.task_id)['_self_recovery']
        self.assertEqual(recovery['unknown_tools'], [receipt])
        self.assertNotIn('blocked', recovery)
        self.assertNotIn('from_block_check', recovery)

    def test_recorded_pause_stops_the_check(self):
        self.check(self.blocked(), 0)
        with patch.object(self.service, '_task_work_is_paused', return_value=True):
            self.assertEqual(self.check(self.blocked(), 10 ** 9)['status'], 'completed')

    def test_controlling_declined_answer_is_recorded_on_the_existing_work_item(self):
        # The check must link a controlling answer to Work before blocking, or Coconut queues the check again.
        for text in ('coconut_track_work', 'existing Work key', 'status blocked', 'paused_by_decision_id', 'Before calling kanban_block',
                     'does not control this block does not pause'):
            self.assertIn(text, BLOCKED_WORK_INSTRUCTIONS)
        payload = dict(key='build-record', title='Build the record', activity='Waiting for build space', status='waiting')
        self.state('working')
        item = self.service._register_work(self.task_id, payload)
        self.state('completed')
        with self.service.transaction():
            self.service._put('decisions', dict(id='declined', task_id=self.task_id, bot_id='b', status='rejected',
                                                decided_by='person', title='Free 8 GB?', options=[], revision=1))
        self.check(self.blocked(), 0)
        # A declined card alone does not pause the outcome.
        queued = self.check(self.blocked(), 10 ** 9)
        self.assertEqual(queued['status'], 'queued')
        self.state('working')
        self.service._register_work(self.task_id, payload | dict(status='blocked', activity='Paused by the declined request',
                                                                 paused_by_decision_id='declined'))
        self.assertEqual(self.service._get('artifacts', item['id'])['paused_by_decision_id'], 'declined')
        self.state('completed')
        self.assertEqual(self.check(self.blocked(), 2 * 10 ** 9)['status'], 'completed')

    def test_only_the_check_turn_requires_a_card_or_a_recheck(self):
        self.check(self.blocked(), 0)
        task = self.check(self.blocked(), 10 ** 9)
        prompt = conversation_input(dict(operation='start', task=task, history=[]))[0]
        self.assertIn(BLOCKED_WORK_INSTRUCTIONS, prompt)
        # A later message rotates the turn ID, so it is not treated as a check.
        later = dict(task, _native_turn_id='new-human-input')
        self.assertNotIn(BLOCKED_WORK_INSTRUCTIONS, conversation_input(dict(operation='start', task=later, history=[]))[0])
        # An answer to the check's card resumes the same turn without the check instructions.
        answer = dict(operation='resume', task=task, history=[], decision=dict(id='d', title='Free space?', status='approved', action_type='question', options=[]))
        self.assertNotIn(BLOCKED_WORK_INSTRUCTIONS, conversation_input(answer)[0])


if __name__ == '__main__':
    unittest.main()
