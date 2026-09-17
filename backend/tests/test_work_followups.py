import json
import unittest
from uuid import uuid4

from coconut.service import now
import test_service_pr as fixtures


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
        self.relay = dict(id=str(uuid4()), task_id=self.root['id'],
                          _relay_task_id=self.callback['id'], state='completed',
                          action={'name': 'coconut_message_bot'}, result='{}')
        self.process = dict(id=str(uuid4()), task_id=self.followup['id'],
                            action={'name': 'terminal'}, state='completed',
                            result=json.dumps(dict(session_id='fix-process', notify_on_complete=True)),
                            turn_id='fix-turn', _process_status='running', _process_observed_at=now())
        with self.service.transaction():
            for task in (self.root, self.callback, self.followup):
                self.service._put('tasks', task)
            for record in (self.relay, self.process):
                self.service._put('native_receipts', record)

    def row(self):
        return self.service.snapshot()['artifacts'][0]

    def test_live_followup_without_activity_report(self):
        self.prepare()
        self.assertEqual(self.row()['activity'], 'Waiting for worker output')
        self.assertEqual(self.row()['activity_state'], 'working')
        self.assertEqual(self.service._tracked_work_input(self.followup), [fixtures.URL])

    def test_original_process_without_activity_report(self):
        self.prepare()
        self.process.update(task_id=self.root['id'], turn_id='review-turn')
        with self.service.transaction():
            self.service._put('native_receipts', self.process)
        self.assertEqual(self.row()['activity'], 'Waiting for worker output')

    def test_stale_and_delivered_processes_are_not_live(self):
        self.prepare()
        for change in ({'_process_observed_at': '2000-01-01T00:00:00Z'},
                       {'_process_delivery': 'delivered'}):
            with self.subTest(change=change):
                with self.service.transaction():
                    self.service._put('native_receipts', dict(self.process, **change))
                self.assertNotEqual(self.row()['activity_state'], 'working')

    def test_live_process_survives_a_later_turn(self):
        self.prepare()
        self.process['turn_id'] = 'previous-turn'
        self.service._put('native_receipts', self.process)
        self.assertEqual(self.row()['activity_state'], 'working')

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
        self.assertEqual(self.row()['activity'], 'Waiting for worker output')

    def test_completed_followup_needs_outcome_evidence(self):
        self.prepare()
        self.root['status'] = 'completed'
        self.followup['status'] = 'completed'
        self.process['_process_delivery'] = 'delivered'
        self.relay['_process_delivery'] = 'delivered'
        with self.service.transaction():
            self.service._put('native_receipts', self.relay)
            self.service._put('tasks', self.root)
            self.service._put('tasks', self.followup)
            self.service._put('native_receipts', self.process)
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
