import json
import threading
import unittest

import test_tracked_work as fixtures


class WorkScopeCompletionTests(unittest.TestCase):
    setUp = fixtures.TrackedWorkTests.setUp
    tearDown = fixtures.TrackedWorkTests.tearDown
    track = fixtures.TrackedWorkTests.track
    row = fixtures.TrackedWorkTests.row
    write = fixtures.TrackedWorkTests.write

    def completed_diagnosis(self):
        item = self.track(status='completed', activity='Diagnosis delivered',
            completion_evidence='Read the report and verified delivery to the implementation owner.')
        task = self.service._get('tasks', self.task['id'])
        task.update(status='waiting', _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Deliver diagnosis')]})
        self.service._put('tasks', task)
        return item, task

    def test_finished_diagnosis_does_not_wait_for_requesters_new_work(self):
        item, task = self.completed_diagnosis()
        task['status'] = 'working'
        self.service._put('tasks', task)
        bot = self.write('POST', '/v1/bots', dict(name='Developer', role='Develop'))
        reply = self.service._store_bot_message(task['id'], dict(target_bot_id=bot['id'], text='Diagnosis delivered', require_response=True))
        task['status'] = 'waiting'
        self.service._put('tasks', task)
        self.assertEqual(self.service._get('tasks', reply['task_id'])['status'], 'queued')
        self.assertEqual(self.row()['work_state'], 'completed')
        self.service._queue_work_recoveries()
        self.assertEqual(self.row()['work_state'], 'completed')
        self.assertEqual(self.service._all('decisions'), [])

    def test_finished_diagnosis_ignores_acknowledged_legacy_wait(self):
        _, task = self.completed_diagnosis()
        child = dict(task, id='acknowledgement', status='completed', _waiting_on=[])
        task['_waiting_on'] = [child['id']]
        self.service._put('tasks', child)
        self.service._put('tasks', task)
        self.assertEqual(self.row()['work_state'], 'completed')

    def test_unconfirmed_local_process_still_requires_its_result(self):
        _, task = self.completed_diagnosis()
        self.service._put('native_receipts', dict(id='process', task_id=task['id'], state='completed',
            action={'name': 'terminal'}, turn_id=task.get('_native_turn_id'),
            _process_session_id='proc-one', _process_status='running'))
        # Use the native result format to register a real background subscription.
        task['status'] = 'working'
        self.service._put('tasks', task)
        call = self.service.native_event(task['id'], 'native_call', dict(name='terminal', arguments={}))
        self.service.native_event(task['id'], 'native_result', dict(id=call['id'], result=json.dumps(
            dict(session_id='proc-two', status='running', notify_on_complete=True))))
        task['status'] = 'waiting'
        self.service._put('tasks', task)
        self.assertNotEqual(self.row()['work_state'], 'completed')

    def test_reopened_scope_does_not_keep_old_completion(self):
        _, task = self.completed_diagnosis()
        self.track(status='working', activity='Verify the implementation')
        self.assertNotEqual(self.row()['work_state'], 'completed')

    def test_running_work_does_not_reuse_a_blocked_label(self):
        self.track(status='blocked', activity='Blocked on an old dependency')
        self.service.running[self.task['id']] = threading.current_thread()
        try:
            row = self.row()
            self.assertEqual(row['work_state'], 'working')
            self.assertEqual(row['activity'], 'In progress')
        finally:
            self.service.running.clear()

    def test_reply_to_waiting_work_stays_in_the_queue_before_its_next_turn(self):
        self.service.db.execute("INSERT INTO settings(key,value) VALUES ('conversation_epoch', ?)", (json.dumps('current-epoch'),))
        self.track()
        task = self.service._get('tasks', self.task['id'])
        task.update(status='waiting', _native_turn_id='previous-turn',
                    _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', task)
        self.write('POST', '/v1/tasks/' + task['id'] + '/replies', dict(text='Continue the export', mode='queue'))
        row = self.row()
        self.assertEqual(row['work_state'], 'queued')
        self.assertEqual(row['work_queue_position'], 1)
        self.assertIn(task['id'], [a['id'] for a in row['work_attempts']])

    def test_queued_work_from_an_old_epoch_stays_excluded(self):
        self.track()
        task = self.service._get('tasks', self.task['id'])
        task.update(status='queued', _native_turn_id='previous-turn', _conversation_epoch='old-epoch')
        self.service._put('tasks', task)
        self.assertEqual(self.row()['work_attempts'], [])

    def test_old_failed_attempt_does_not_hide_current_execution(self):
        from coconut.service import now
        self.track()
        owner = self.service._get('tasks', self.task['id'])
        owner['status'] = 'waiting'
        old = dict(owner, id='old-failure', status='failed', _assignment_task_id=owner['id'])
        current = dict(owner, id='current-attempt', status='working', _assignment_task_id=owner['id'],
                       updated_at=now(), _turn_started_at=now())
        for task in (owner, old, current):
            self.service._put('tasks', task)
        self.service.running[current['id']] = threading.current_thread()
        try:
            self.assertEqual(self.row()['work_state'], 'working')
        finally:
            self.service.running.clear()
        current['status'] = 'queued'
        self.service._put('tasks', current)
        self.assertEqual(self.row()['work_state'], 'queued')
        current['status'] = 'completed'
        self.service._put('tasks', current)
        self.assertEqual(self.row()['work_state'], 'needs_recovery')

    def test_new_blocker_report_keeps_priority_over_a_running_sibling(self):
        from coconut.service import now
        item = self.track()
        owner = self.service._get('tasks', self.task['id'])
        owner['status'] = 'waiting'
        reporter = dict(owner, id='blocker-report', status='completed', _native_turn_id='report-turn',
                        _assignment_task_id=owner['id'])
        current = dict(owner, id='current-attempt', status='working', _assignment_task_id=owner['id'],
                       updated_at=now(), _turn_started_at=now())
        for task in (owner, reporter, current):
            self.service._put('tasks', task)
        self.service._set_work_activity(item, reporter, dict(activity='Export verification failed', activity_state='blocked'))
        self.service.running[current['id']] = threading.current_thread()
        try:
            self.assertEqual(self.row()['work_state'], 'needs_recovery')
            self.assertEqual(self.row()['activity'], 'Export verification failed')
        finally:
            self.service.running.clear()

    def test_later_callback_reopens_the_same_assignment(self):
        item, owner = self.completed_diagnosis()
        first = dict(owner, id='first-callback', status='completed', _assignment_task_id=owner['id'])
        self.service._put('tasks', first)
        saved = self.service._get('artifacts', item['id'])
        saved['task_id'] = first['id']
        self.service._put('artifacts', saved)
        second = dict(owner, id='second-callback', status='working', _assignment_task_id=owner['id'])
        self.service._put('tasks', second)
        self.service._save_work_progress(second, {'_work_todos': {'todos': [
            dict(id='coconut-assignment', status='in_progress', content='Verify the diagnosis')]}})
        call = self.service.native_event(second['id'], 'native_call', dict(name='terminal', arguments={}))
        self.service.native_event(second['id'], 'native_result', dict(id=call['id'], result=json.dumps(
            dict(session_id='verify-diagnosis', status='running', notify_on_complete=True))))
        second = self.service._get('tasks', second['id'])
        second['status'] = 'waiting'
        self.service._put('tasks', second)
        self.assertNotEqual(self.row()['work_state'], 'completed')

    def test_completed_report_keeps_an_explicit_process_wait(self):
        _, task = self.completed_diagnosis()
        self.service._put('native_receipts', dict(id='source', task_id=task['id'], state='completed',
            action={'name': 'terminal'}, turn_id=task.get('_native_turn_id')))
        self.service._put('native_receipts', dict(id='wait', task_id=task['id'], state='completed',
            action={'name': 'coconut_wait'}, turn_id=task.get('_native_turn_id'),
            _wait_process_receipt='source', _wait_session_id=task['session_id']))
        self.assertEqual(self.row()['work_state'], 'waiting')

    def test_completed_report_keeps_explicit_task_and_decision_waits(self):
        _, task = self.completed_diagnosis()
        target = dict(task, id='other-task', status='queued')
        self.service._put('tasks', target)
        self.service._put('decisions', dict(id='other-decision', task_id=target['id'], bot_id=target['bot_id'],
            status='pending', kind='question', title='Approve this result?'))
        for field, identity in [('_wait_task_id', target['id']), ('_wait_decision_id', 'other-decision')]:
            with self.subTest(field=field):
                self.service._put('native_receipts', dict(id='explicit-wait', task_id=task['id'], state='completed',
                    action={'name': 'coconut_wait'}, turn_id=task.get('_native_turn_id'),
                    _wait_session_id=target['session_id'], **{field: identity}))
                self.assertNotEqual(self.row()['work_state'], 'completed')

    def test_explicit_wait_reusing_a_handoff_keeps_the_outcome_open(self):
        _, task = self.completed_diagnosis()
        task['status'] = 'working'
        self.service._put('tasks', task)
        bot = self.write('POST', '/v1/bots', dict(name='Verifier', role='Verify'))
        sent = self.service._store_bot_message(task['id'], dict(target_bot_id=bot['id'], text='Verify the diagnosis', require_response=True))
        result = self.service.wait_for_task(task['id'], dict(task_id=sent['task_id'], reason='Waiting for verification'))
        self.assertEqual(result['status'], 'waiting')
        self.assertNotEqual(self.row()['work_state'], 'completed')
        task = self.service._get('tasks', task['id'])
        task['status'] = 'waiting'
        self.service._put('tasks', task)
        self.assertNotEqual(self.row()['work_state'], 'completed')
