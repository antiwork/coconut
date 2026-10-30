import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import test_service_pr as pr_fixtures
import test_delegated_work_duplicates as delegation_fixtures
from coconut.service import ServiceError, YieldForDecision
from test_service_pr import URL, SHA, observation
import legacy_work_sync
from coconut.answer_undo import AnswerUndo
# These tests check what an answer does, so it applies at once. test_answer_undo.py tests the Undo window.
AnswerUndo.answer_undo_seconds = 0

setUpModule, tearDownModule = legacy_work_sync.scope()


class WorkPauseTests(unittest.TestCase):
    setUp = pr_fixtures.PRServiceTests.setUp
    tearDown = pr_fixtures.PRServiceTests.tearDown
    write = pr_fixtures.PRServiceTests.write
    run_task = pr_fixtures.PRServiceTests.run_task

    def task_state(self, state):
        task = self.service._get('tasks', self.task['id'])
        task['status'] = state
        self.service._put('tasks', task)
        return task

    def decision(self, choice='modify'):
        self.task_state('working')
        try:
            self.service.native_event(self.task['id'], 'native_question', {
                'title': 'Continue this change?',
                'options': [dict(id='yes', label='Continue'), dict(id='no', label='Stop')],
            })
        except YieldForDecision:
            pass
        decision = self.service._all('decisions')[-1]
        answer = (dict(revision=1, option_id='no') if choice == 'approve' else
                  dict(revision=1, choice=choice, correction='Pause this work. Keep its saved files.'))
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer',
                   answer)
        self.task_state('working')
        return decision['id']

    def test_corrected_answer_can_pause_work_without_recovery(self):
        self.task_state('working')
        payload = dict(key='fix', title='Fix account export', activity='Checking export', status='working')
        item = self.service._register_work(self.task['id'], payload)
        identity = self.decision()
        self.assertEqual(self.service._get('decisions', identity)['status'], 'modified')
        self.service._register_work(self.task['id'], payload | dict(status='blocked', paused_by_decision_id=identity))
        self.task_state('completed')
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        row = self.service._work_projection([self.service._get('artifacts', item['id'])])[0]
        self.assertEqual(row['work_state'], 'waiting')
        self.assertEqual(row['activity'], 'Paused by your decision')
        self.assertEqual(len(self.service._all('tasks')), before)

    def test_corrected_answer_can_pause_a_pr_and_active_report_clears_it(self):
        self.run_task()
        item = self.service._all('artifacts')[0]
        identity = self.decision()
        self.service._register_pr_artifact(self.task['id'], dict(url=URL,
            activity='Waiting for requested scope', activity_state='blocked', paused_by_decision_id=identity))
        self.task_state('completed')
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        row = self.service._work_projection([self.service._get('artifacts', item['id'])])[0]
        self.assertEqual(row['activity'], 'Paused by your decision')
        self.assertEqual(len(self.service._all('tasks')), before)
        self.task_state('working')
        self.write('POST', '/v1/tasks/' + self.task['id'] + '/replies', dict(text='Resume this work.'))
        self.service._register_pr_artifact(self.task['id'], dict(url=URL, activity='Implementing the new scope', activity_state='working'))
        self.assertNotIn('paused_by_decision_id', self.service._get('artifacts', item['id']))

    def test_passing_checks_cannot_resume_a_paused_pr(self):
        self.run_task()
        identity = self.decision()
        self.service._register_pr_artifact(self.task['id'], dict(url=URL,
            activity='Waiting for requested scope', activity_state='blocked', paused_by_decision_id=identity))
        self.task_state('completed')
        for reviewed in (False, True):
            with self.subTest(reviewed=reviewed):
                item = self.service._all('artifacts')[0]
                if reviewed:
                    item.update(_reviewed_heads=[SHA], _review_checked_at='2026-09-01T00:00:00Z')
                    self.service._put('artifacts', item)
                self.tracker.observations.append(replace(observation('ready_for_review'),
                    pr_state='open', draft=False, checks_state='passed'))
                self.service.refresh_pull_requests()
                self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')
        self.task_state('working')
        self.write('POST', '/v1/tasks/' + self.task['id'] + '/replies', dict(text='Resume this work.'))
        self.service._register_pr_artifact(self.task['id'], dict(url=URL,
            activity='Implementing the new scope', activity_state='working'))
        self.task_state('completed')
        item = self.service._all('artifacts')[0]
        item['head_sha'] = 'b' * 40
        self.service._put('artifacts', item)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')

    def test_new_bot_observation_keeps_pause_and_resume_clears_all_records(self):
        self.run_task()
        identity = self.decision()
        self.service._register_pr_artifact(self.task['id'], dict(url=URL,
            activity='Waiting for requested scope', activity_state='blocked', paused_by_decision_id=identity))
        self.task_state('completed')
        bot = self.write('POST', '/v1/bots', dict(name='Second bot', role='Review'))
        task = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Track this PR', mode='queue'))
        self.tracker.observations.append(observation(date='2026-09-17T00:00:00Z'))
        self.service._run_task(task['id'])
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        rows = self.service._work_projection(self.service._all('artifacts'))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['activity'] == 'Paused by your decision' for row in rows))
        self.assertEqual(len(self.service._all('tasks')), before)
        self.task_state('working')
        self.write('POST', '/v1/tasks/' + self.task['id'] + '/replies', dict(text='Resume this work.'))
        self.service._register_pr_artifact(self.task['id'], dict(url=URL,
            activity='Implementing the new scope', activity_state='working'))
        self.assertTrue(all(not self.service._work_pause(row) for row in self.service._all('artifacts')))

    def test_background_report_preserves_pause_until_later_human_direction(self):
        self.run_task()
        identity = self.decision()
        self.write('POST', '/v1/tasks/' + self.task['id'] + '/replies', dict(text='Keep this work paused.'))
        self.service._register_pr_artifact(self.task['id'], dict(url=URL,
            activity='Waiting for requested scope', activity_state='blocked', paused_by_decision_id=identity))
        self.service._register_pr_artifact(self.task['id'], dict(url=URL,
            activity='Checking CI', activity_state='working'))
        self.assertEqual(self.service._work_pause(self.service._all('artifacts')[0])['id'], identity)
        bot = self.write('POST', '/v1/bots', dict(name='Observer', role='Review'))
        task = self.service._send(self.service._get('bots', bot['id']),
            dict(text='Observe this PR', mode='queue'), human_authored=False)
        self.tracker.observations.append(observation())
        self.service._run_task(task['id'])
        task = self.service._get('tasks', task['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        self.service._register_pr_artifact(task['id'], dict(url=URL, activity='Checking CI', activity_state='working'))
        self.assertTrue(all(self.service._work_pause(row) for row in self.service._all('artifacts')))

    def test_completion_keeps_the_pause_after_an_option_answer(self):
        for event in ('completed', 'failed'):
            with self.subTest(event=event):
                self.task_state('working')
                payload = dict(key='fix', title='Fix account export', activity='Checking export', status='working')
                item = self.service._register_work(self.task['id'], payload)
                identity = self.decision('approve')
                # Native question options use approved, even when the chosen action pauses work.
                self.assertEqual(self.service._get('decisions', identity)['status'], 'approved')
                self.service._register_work(self.task['id'], payload | dict(status='blocked', paused_by_decision_id=identity))
                self.service.emit(self.task['id'], event, dict(summary='The requested pause is recorded.',
                    _work_todos=dict(revision=10, todos=[dict(id='coconut-assignment', content='Fix account export', status='pending')])))
                self.assertEqual(self.service._get('tasks', self.task['id'])['status'], event)
                self.assertEqual(self.service._get('decisions', identity)['status'], 'executed' if event == 'completed' else 'failed')
                before = len(self.service._all('tasks'))
                self.service._queue_work_recoveries()
                row = self.service._work_projection([self.service._get('artifacts', item['id'])])[0]
                self.assertEqual(row['activity'], 'Paused by your decision')
                self.assertEqual(len(self.service._all('tasks')), before)
                self.task_state('working')
                self.service._register_work(self.task['id'], payload | dict(status='blocked', paused_by_decision_id=identity))

    def test_completion_continues_other_unpaused_work(self):
        self.task_state('working')
        payload = dict(key='fix', title='Fix account export', activity='Checking export', status='working')
        self.service._register_work(self.task['id'], payload)
        identity = self.decision()
        self.service._register_work(self.task['id'], payload | dict(status='blocked', paused_by_decision_id=identity))
        self.service._register_work(self.task['id'], payload | dict(key='another-fix', title='Fix account import'))
        self.service.emit(self.task['id'], 'completed', dict(summary='Export is paused; import still needs work.',
            _work_todos=dict(revision=10, todos=[dict(id='coconut-assignment', content='Fix export and import', status='pending')])))
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')

    def test_finished_work_does_not_restart_the_remaining_paused_work(self):
        self.task_state('working')
        payload = dict(key='fix', title='Fix account export', activity='Checking export', status='working')
        self.service._register_work(self.task['id'], payload)
        identity = self.decision()
        self.service._register_work(self.task['id'], payload | dict(status='blocked', paused_by_decision_id=identity))
        self.service._register_work(self.task['id'], payload | dict(key='another-fix', title='Fix account import',
            status='completed', completion_evidence='Verified the imported account.'))
        self.service.emit(self.task['id'], 'completed', dict(summary='Import is complete; export remains paused.',
            _work_todos=dict(revision=10, todos=[dict(id='coconut-assignment', content='Fix export and import', status='pending')])))
        task = self.service._get('tasks', self.task['id'])
        self.assertEqual(task['status'], 'completed')
        task.update(status='queued', _work_resume=True)
        self.service._put('tasks', task)
        self.assertFalse(self.service._dispatch_eligible(task, datetime.now(timezone.utc).timestamp()))

    def test_status_question_does_not_resume_the_pr(self):
        self.run_task()
        identity = self.decision()
        item = self.service._register_pr_artifact(self.task['id'], dict(url=URL,
            activity='Waiting for requested scope', activity_state='blocked', paused_by_decision_id=identity))
        self.task_state('completed')
        task = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='How is this going?', mode='queue', work_artifact_id=item['id'], work_status_only=True))
        task = self.service._get('tasks', task['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        self.service._register_pr_artifact(task['id'], dict(url=URL, activity='Checking CI', activity_state='working'))
        self.assertEqual(self.service._work_pause(self.service._get('artifacts', item['id']))['id'], identity)

    def test_pr_pause_requires_activity_text(self):
        self.run_task()
        identity = self.decision()
        with self.assertRaisesRegex(ServiceError, 'Include activity text'):
            self.service._register_pr_artifact(self.task['id'], dict(url=URL,
                activity_state='blocked', paused_by_decision_id=identity))

    def test_scheduled_recovery_cannot_dispatch_until_human_direction(self):
        self.task_state('working')
        payload = dict(key='fix', title='Fix account export', activity='Checking export', status='working')
        item = self.service._register_work(self.task['id'], payload)
        identity = self.decision()
        self.service._register_work(self.task['id'], payload | dict(status='blocked', paused_by_decision_id=identity))
        task = self.service._get('tasks', self.task['id'])
        task.update(status='scheduled', _self_recovery=dict(attempt=3), _work_context_id=item['id'],
                    scheduled_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
        self.service._put('tasks', task)
        after_delay = datetime.now(timezone.utc).timestamp() + 600
        self.assertFalse(self.service._dispatch_eligible(task, after_delay))
        self.service._run_task(task['id'])
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'scheduled')
        row = self.service._work_projection([self.service._get('artifacts', item['id'])])[0]
        self.assertEqual(row['activity'], 'Paused by your decision')
        self.write('POST', '/v1/tasks/' + task['id'] + '/replies', dict(text='Resume this work.'))
        self.assertTrue(self.service._dispatch_eligible(self.service._get('tasks', task['id']), after_delay))

    def test_repeated_pause_report_preserves_later_human_direction(self):
        self.run_task()
        identity = self.decision()
        payload = dict(url=URL, activity='Waiting for requested scope', activity_state='blocked', paused_by_decision_id=identity)
        item = self.service._register_pr_artifact(self.task['id'], payload)
        timestamp = self.service._get('artifacts', item['id'])['_work_pause_at']
        self.write('POST', '/v1/tasks/' + self.task['id'] + '/replies', dict(text='Resume this work.'))
        self.service._register_pr_artifact(self.task['id'], payload)
        self.assertEqual(self.service._get('artifacts', item['id'])['_work_pause_at'], timestamp)
        self.service._register_pr_artifact(self.task['id'], dict(url=URL, activity='Implementing the new scope', activity_state='working'))
        self.assertIsNone(self.service._work_pause(self.service._get('artifacts', item['id'])))

    def test_pause_rejects_unrelated_and_unanswered_decisions(self):
        self.task_state('working')
        payload = dict(key='fix', title='Fix account export', activity='Waiting for scope', status='blocked')
        item = self.service._register_work(self.task['id'], payload)
        identity = self.decision('reject')
        unrelated = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Unrelated work', mode='queue'))
        decision = self.service._get('decisions', identity)
        decision.update(id=str(uuid4()), task_id=unrelated['id'])
        identity = decision['id']
        self.service._put('decisions', decision)
        with self.assertRaises(ServiceError):
            self.service._register_work(self.task['id'], payload | dict(paused_by_decision_id=identity))
        decision.update(task_id=self.task['id'], status='pending', decided_by=None)
        self.service._put('decisions', decision)
        with self.assertRaises(ServiceError):
            self.service._register_work(self.task['id'], payload | dict(paused_by_decision_id=identity))
        self.assertNotIn('paused_by_decision_id', self.service._get('artifacts', item['id']))


class DelegatedPauseTests(unittest.TestCase):
    setUp = delegation_fixtures.DelegatedWorkDuplicatesTests.setUp
    tearDown = delegation_fixtures.DelegatedWorkDuplicatesTests.tearDown
    write = delegation_fixtures.DelegatedWorkDuplicatesTests.write
    track = delegation_fixtures.DelegatedWorkDuplicatesTests.track
    child = delegation_fixtures.DelegatedWorkDuplicatesTests.child

    def test_human_resume_clears_pause_across_parent_and_delegate(self):
        parent = self.track()
        task, child = self.child()
        try:
            self.service.native_event(task['id'], 'native_question', {
                'title': 'Continue the export?',
                'options': [dict(id='yes', label='Continue'), dict(id='no', label='Stop')],
            })
        except YieldForDecision:
            pass
        decision = self.service._all('decisions')[-1]
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, choice='reject'))
        task = self.service._get('tasks', task['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        self.service._register_work(task['id'], dict(key='account-export', title='Prepare export data',
            activity='Export paused', status='blocked', paused_by_decision_id=decision['id']))
        task['status'] = 'completed'
        self.service._put('tasks', task)
        self.assertEqual(self.service._work_pause(parent)['id'], decision['id'])
        self.track()
        self.assertEqual(self.service._work_pause(self.service._get('artifacts', parent['id']))['id'], decision['id'])
        self.write('POST', '/v1/tasks/' + self.task['id'] + '/replies', dict(text='Resume the export.'))
        self.track()
        for identity in (parent['id'], child['id']):
            self.assertIsNone(self.service._work_pause(self.service._get('artifacts', identity)))
