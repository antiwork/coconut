import json
import unittest
from uuid import uuid4

import test_service_pr as fixtures
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class WorkFollowupTests(unittest.TestCase):
    setUp = fixtures.PRServiceTests.setUp
    tearDown = fixtures.PRServiceTests.tearDown
    write = fixtures.PRServiceTests.write
    run_task = fixtures.PRServiceTests.run_task

    def prepare(self):
        with self.service.transaction():
            self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                                    ('conversation_epoch', json.dumps('current-epoch')))
        self.run_task()
        self.root = self.service._get('tasks', self.task['id'])
        self.root.update(status='waiting', _native_turn_id='review-turn')
        self.callback = dict(self.root, id=str(uuid4()), status='completed')
        self.followup = dict(self.root, id=str(uuid4()), _native_turn_id='fix-turn',
                             _requester_reply_task_id=self.callback['id'])
        # The root waits for the callback (coconut_wait).
        self.wait = dict(id=str(uuid4()), task_id=self.root['id'],
                         _wait_task_id=self.callback['id'], _wait_session_id=self.root['session_id'],
                         state='completed', action={'name': 'coconut_wait'}, result='{}')
        with self.service.transaction():
            for task in (self.root, self.callback, self.followup):
                self.service._put('tasks', task)
            self.service._put('native_receipts', self.wait)

    def row(self):
        return self.service.snapshot()['artifacts'][0]

    def running_state(self, task):
        """The activity state of the Work row while the turn of `task` runs."""
        import threading
        self.service.running[task['id']] = threading.current_thread()
        try:
            return self.row()['activity_state']
        finally:
            self.service.running.clear()

    def test_live_followup_without_activity_report(self):
        self.prepare()
        self.assertEqual(self.running_state(self.followup), 'working')
        self.assertEqual(self.service._tracked_work_input(self.followup), [fixtures.URL])

    def test_unrelated_and_reset_tasks_do_not_supply_activity_or_context(self):
        self.prepare()
        for change in ({'exchange_id': 'another-exchange'}, {'_conversation_epoch': 'old'},
                       {'session_id': 'old'}, {'_requester_reply_task_id': 'unrelated'}):
            with self.subTest(change=change):
                task = dict(self.followup, **change)
                with self.service.transaction():
                    self.service._put('tasks', task)
                self.assertNotEqual(self.row()['activity_state'], 'working')
                self.assertEqual(self.service._tracked_work_input(task), [])

    def test_cycle_terminates_and_keeps_live_dependency(self):
        self.prepare()
        self.followup['_waiting_on'] = [self.root['id']]
        with self.service.transaction():
            self.service._put('tasks', self.followup)
        self.assertEqual(self.running_state(self.followup), 'working')

    def test_completed_followup_needs_outcome_evidence(self):
        self.prepare()
        self.root['status'] = 'completed'
        self.followup['status'] = 'completed'
        self.wait['_process_delivery'] = 'delivered'
        with self.service.transaction():
            self.service._put('native_receipts', self.wait)
            self.service._put('tasks', self.root)
            self.service._put('tasks', self.followup)
        self.assertEqual(self.row()['work_state'], 'needs_recovery')

    def test_new_queued_task_has_no_epoch_until_execution(self):
        self.prepare()
        with self.service.transaction():
            task = self.service._send(self.bot, {'text': 'Fix the finding', 'mode': 'queue'},
                                      exchange_id=self.root['exchange_id'], parent_task_id=self.callback['id'])
            task['_requester_reply_task_id'] = self.callback['id']
            self.service._put('tasks', task)
            self.followup['status'] = 'completed'
            self.service._put('tasks', self.followup)
        self.assertNotIn('_conversation_epoch', task)
        self.assertEqual(self.row()['activity'], 'Queued')
        self.assertEqual(self.service._tracked_work_input(task), [fixtures.URL])
        task['session_id'] = 'old-session'
        with self.service.transaction():
            self.service._put('tasks', task)
        self.assertNotEqual(self.row()['activity'], 'Queued')

    def test_old_deeper_completion_cannot_hide_a_pending_decision(self):
        self.prepare()
        self.callback['status'] = 'needs_you'
        self.service._put('decisions', dict(id='pending-card', task_id=self.callback['id'], bot_id=self.bot['id'], status='pending'))
        self.followup['status'] = 'completed'
        with self.service.transaction():
            self.service._put('tasks', self.callback)
            self.service._put('tasks', self.followup)
        self.assertEqual(self.row()['activity'], 'Waiting for your decision')

    def test_completed_callback_cannot_hide_a_failed_assignment(self):
        self.prepare()
        self.root['status'] = 'failed'
        self.followup['status'] = 'completed'
        with self.service.transaction():
            self.service._put('tasks', self.root)
            self.service._put('tasks', self.followup)
        self.assertEqual(self.row()['activity'], 'Needs recovery')

    def test_next_provider_turn_gets_tracked_pr_context(self):
        self.prepare()
        seen = []
        def run(task, bot, history, emit):
            seen.extend(task.get('_tracked_prs', []))
            emit('completed', {'summary': 'Done'})
        self.provider.start_task = run
        self.followup['status'] = 'queued'
        with self.service.transaction():
            self.service._put('tasks', self.followup)
        self.service._run_task(self.followup['id'])
        self.assertEqual(seen, [fixtures.URL])
