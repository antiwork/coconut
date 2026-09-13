import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError, YieldForDecision
from coconut.pr_tracking import PRObservation

URL = 'https://github.com/example/project/pull/42'


class MergeDecisionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(Path(self.tmp.name), FixtureProvider(self.tmp.name), start_workers=False)
        self.bot = self.write('/v1/bots', {'name': 'Desk', 'role': ''})
        self.task = self.write(f"/v1/bots/{self.bot['id']}/messages", {'text': 'Review this PR'})
        self.observation = self.observed('open')
        self.service.pr_tracker = type('Tracker', (), {'observe': lambda _, url: self.observation})()
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.artifact = self.service._register_pr_artifact(self.task['id'], {'url': URL})

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def write(self, path, data):
        return self.service.mutate('POST', path, data, str(uuid4()))

    def observed(self, state):
        return PRObservation('merged' if state == 'merged' else 'pending', 'a' * 40, 'Change', URL,
                             state, '2026-09-13T10:00:00Z', True, state, False, 'pending')

    def question(self, **extra):
        payload = dict(title='Merge this PR?', options=[dict(id='merge', label='Merge', explanation='Merge the reviewed change.')], **extra)
        receipt = self.service.emit(self.task['id'], 'native_call', {'name': 'coconut_ask', 'arguments': payload})
        with self.assertRaises(YieldForDecision):
            self.service.emit(self.task['id'], 'native_question', dict(payload, receipt_id=receipt['id']))
        return self.service._all('decisions')[-1], receipt['id']

    def test_verified_merge_resolves_and_resumes_once_without_approval(self):
        decision, receipt_id = self.question(merge_pr_url=URL)
        self.observation = self.observed('merged')
        self.service.refresh_pull_requests()
        resolved = self.service._get('decisions', decision['id'])
        self.assertEqual(resolved['status'], 'resolved')
        self.assertEqual(resolved['revision'], 2)
        self.assertEqual(resolved['_answers'], [])
        self.assertIsNone(resolved['decided_by'])
        self.assertNotIn('selected_option_id', resolved)
        task = self.service._get('tasks', self.task['id'])
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['_resume_decision'], decision['id'])
        receipt = self.service._get('native_receipts', receipt_id)
        self.assertEqual(receipt['state'], 'completed')
        self.assertEqual(json.loads(receipt['result'])['resolution'], 'merged')
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('decisions', decision['id']), resolved)
        with self.assertRaises(ServiceError) as error:
            self.write(f"/v1/decisions/{decision['id']}/answer", {'option_id': 'merge', 'revision': 1})
        self.assertEqual(error.exception.status, 409)

    def test_restart_reconciles_saved_observation(self):
        decision, _ = self.question(merge_pr_url=URL)
        self.observation = self.observed('merged')
        self.service._refresh_pull_requests()
        self.service.close()
        self.service = Service(Path(self.tmp.name), FixtureProvider(self.tmp.name), start_workers=False)
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'resolved')

    def test_text_alone_does_not_link_or_resolve_a_card(self):
        decision, _ = self.question()
        self.observation = self.observed('merged')
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_failed_observation_and_closed_pr_do_not_resolve(self):
        decision, _ = self.question(merge_pr_url=URL)
        self.observation = PRObservation(None, None, None, URL, 'Unavailable', '2026-09-13T10:00:00Z', False)
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.observation = self.observed('closed')
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_untracked_url_is_rejected(self):
        with self.assertRaises(ServiceError):
            self.question(merge_pr_url=URL.replace('/42', '/43'))
        self.assertEqual(self.service._all('decisions'), [])

    def test_optional_card_resolution_does_not_restart_finished_work(self):
        self.service.emit(self.task['id'], 'native_proposal', dict(key='merge', title='Merge?', merge_pr_url=URL,
                            options=[dict(id='merge', label='Merge', explanation='Merge it.')]))
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'completed'
            self.service._put('tasks', task)
        self.observation = self.observed('merged')
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._all('decisions')[0]['status'], 'resolved')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')
