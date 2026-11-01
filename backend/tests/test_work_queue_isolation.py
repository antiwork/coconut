import tempfile
import unittest
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service


class WorkQueueIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(self.tmp.name, FixtureProvider(self.tmp.name), start_workers=False)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))

    def task(self, title):
        return self.service.mutate('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': title, 'mode': 'queue'}, str(uuid4()))

    def test_interrupted_callback_does_not_fail_other_pending_work(self):
        intake = self.task('Check assigned work')
        interrupted = self.task('Inspect runtime')
        publication = self.task('Publish reviewed fix')
        with self.service.transaction():
            intake = self.service._get('tasks', intake['id'])
            intake.update(status='waiting', _waiting_on=[publication['id']])
            self.service._put('tasks', intake)
            interrupted = self.service._get('tasks', interrupted['id'])
            interrupted.update(status='working', _assignment_task_id=intake['id'], _native_turn_id='interrupted-turn')
            self.service._put('tasks', interrupted)
            publication = self.service._get('tasks', publication['id'])
            publication['_assignment_task_id'] = intake['id']
            self.service._put('tasks', publication)
        self.service.emit(interrupted['id'], 'native_call', {'name': 'unknown_tool', 'arguments': {}})
        with self.service.transaction():
            self.service._recover()
        self.assertEqual(self.service._get('tasks', interrupted['id'])['status'], 'failed')
        self.assertEqual(self.service._get('tasks', intake['id'])['status'], 'waiting')
        self.service._run_task(publication['id'])
        self.assertNotIn('original assignment stopped', self.service._get('tasks', publication['id'])['summary'])
        self.assertEqual(self.service._all('native_receipts')[0]['state'], 'executing')

    def test_closed_native_run_does_not_block_next_intake(self):
        intake = self.task('Old intake')
        review = self.task('Review the assigned PR')
        with self.service.transaction():
            intake = self.service._get('tasks', intake['id'])
            intake.update(status='failed', routine_id='intake', _native_job_finished=True)
            self.service._put('tasks', intake)
            review = self.service._get('tasks', review['id'])
            review.update(status='waiting', source_task_id=intake['id'])
            self.service._put('tasks', review)
        self.assertIsNone(self.service._active_routine_task('intake'))
        with self.service.transaction():
            intake.update(status='working', _native_job_finished=False)
            self.service._put('tasks', intake)
        self.assertEqual(self.service._active_routine_task('intake')['id'], intake['id'])
