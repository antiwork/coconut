import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import ServiceError, now
from test_service_pr import PRServiceTests, URL
from test_background_processes import BackgroundProcessTests


class WorkStatusTests(unittest.TestCase):
    setUp = PRServiceTests.setUp
    tearDown = PRServiceTests.tearDown
    write = PRServiceTests.write
    run_task = PRServiceTests.run_task

    def artifact(self, activity='Reviewing with Fable', phase='working'):
        self.provider.payload.update(activity=activity, activity_state=phase)
        self.run_task()
        return self.service._all('artifacts')[0]

    def row(self):
        return self.service.snapshot()['artifacts'][0]

    def status(self, status):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = status
            self.service._put('tasks', task)

    def test_finished_turn_cannot_keep_claiming_review_runs(self):
        self.artifact()
        self.assertNotEqual(self.row()['activity'], 'Reviewing with Fable')
        self.assertEqual(self.row()['work_state'], 'needs_recovery')

    def test_bot_report_requires_execution_evidence(self):
        self.artifact()
        self.status('working')
        self.assertEqual(self.row()['activity'], 'Needs recovery')
        self.service.running[self.task['id']] = threading.current_thread()
        try:
            self.assertEqual(self.row()['activity'], 'Reviewing with Fable')
        finally:
            self.service.running.clear()

    def test_process_receipt_expires_without_observation(self):
        self.artifact()
        self.status('waiting')
        record = dict(id=str(uuid4()), task_id=self.task['id'], action={'name': 'terminal'}, state='completed',
                      result=json.dumps(dict(session_id='test-process', notify_on_complete=True)),
                      turn_id=self.service._get('tasks', self.task['id']).get('_native_turn_id'),
                      _process_status='running', _process_observed_at=now())
        with self.service.transaction(): self.service._put('native_receipts', record)
        self.assertEqual(self.row()['activity'], 'Reviewing with Fable')
        record['_process_observed_at'] = '2000-01-01T00:00:00Z'
        with self.service.transaction(): self.service._put('native_receipts', record)
        self.assertEqual(self.row()['work_state'], 'waiting')

    def test_verified_outcome_survives_turn_but_not_changed_head(self):
        artifact = self.artifact('Review needs changes', 'blocked')
        self.assertEqual(self.row()['activity'], 'Review needs changes')
        artifact['head_sha'] = 'b' * 40
        with self.service.transaction(): self.service._put('artifacts', artifact)
        self.assertNotEqual(self.row()['activity'], 'Review needs changes')

    def test_failed_turn_overrides_bot_ready_report(self):
        self.artifact('Ready to merge', 'ready')
        self.status('failed')
        self.assertEqual(self.row()['activity'], 'Needs recovery')

    def test_repeated_track_updates_activity_without_github_read(self):
        artifact = self.artifact()
        self.status('working')
        self.service._register_pr_artifact(self.task['id'], dict(url=URL, activity='Checking a finding'))
        self.assertEqual(self.service._get('artifacts', artifact['id'])['_activity'], 'Checking a finding')
        self.assertEqual(len(self.tracker.urls), 1)

    def test_dismiss_and_undo_preserve_chat_and_all_bot_copies(self):
        artifact = self.artifact()
        artifact.update(pr_state='merged', status='merged')
        with self.service.transaction():
            self.service._put('artifacts', artifact)
            self.service._put('artifacts', dict(artifact, id=str(uuid4())))
        messages = self.service._all('messages')
        path = f'/v1/work/pull-requests/{artifact["id"]}/dismiss'
        self.write('POST', path, dict(dismissal_id='first'))
        self.assertTrue(all(a['work_dismissal_id'] == 'first' for a in self.service.snapshot()['artifacts']))
        self.assertEqual(messages, self.service._all('messages'))
        self.write('POST', path, dict(dismissal_id='second'))
        with self.assertRaises(ServiceError): self.write('POST', path, dict(dismissal_id='first', undo=True))
        self.write('POST', path, dict(dismissal_id='second', undo=True))
        self.assertTrue(all(a['work_dismissal_id'] is None for a in self.service.snapshot()['artifacts']))

    def test_new_owner_does_not_inherit_previous_ready_report(self):
        artifact = self.artifact('Ready to merge', 'ready')
        next_task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Review the new change'})
        with self.service.transaction():
            task = self.service._get('tasks', next_task['id'])
            task.update(status='working', _native_turn_id='new-turn')
            self.service._put('tasks', task)
        self.service._bind_pr_review(artifact, task, transfer=True)
        self.service.running[task['id']] = threading.current_thread()
        try:
            self.assertEqual(self.row()['activity'], 'In progress')
            self.assertEqual(self.row()['activity_state'], 'working')
        finally:
            self.service.running.clear()

    def test_explicit_tracking_restores_a_reopened_pr(self):
        from dataclasses import replace
        from test_service_pr import observation
        artifact = self.artifact()
        artifact.update(pr_state='closed')
        with self.service.transaction(): self.service._put('artifacts', artifact)
        self.write('POST', f'/v1/work/pull-requests/{artifact["id"]}/dismiss', dict(dismissal_id='test'))
        self.tracker.observations = [replace(observation(), pr_state='open')]
        self.write('POST', '/v1/work/pull-requests', {'bot_id': self.bot['id'], 'url': URL})
        self.assertIsNone(self.row()['work_dismissal_id'])

    def test_open_work_cannot_be_dismissed(self):
        artifact = self.artifact()
        with self.assertRaises(ServiceError) as error:
            self.write('POST', f'/v1/work/pull-requests/{artifact["id"]}/dismiss', dict(dismissal_id='test'))
        self.assertEqual(error.exception.status, 409)


class ProcessObservationTests(unittest.TestCase):
    setUp = BackgroundProcessTests.setUp
    tearDown = BackgroundProcessTests.tearDown
    write = BackgroundProcessTests.write
    start = BackgroundProcessTests.start

    def test_running_observation_does_not_resume_or_consume(self):
        task_id = self.start()
        def observe(bot, processes):
            return [dict(id=p['id'], observation='running', session_id=p['session_id']) for p in processes]
        with patch.object(self.provider, 'process_completions', side_effect=observe):
            self.service._drain_process_completions()
        receipt = self.service._all('native_receipts')[0]
        self.assertEqual(receipt['_process_status'], 'running')
        self.assertTrue(receipt['_process_observed_at'])
        self.assertNotIn('_process_delivery', receipt)
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'waiting')
        self.provider.results['proc_test'] = 'The review finished.'
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'queued')
