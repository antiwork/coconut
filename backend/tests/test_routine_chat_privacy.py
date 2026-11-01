import tempfile
import unittest
from uuid import uuid4
from coconut.providers import FixtureProvider
from coconut.service import Service


class RoutineChatPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(self.tmp.name, FixtureProvider(self.tmp.name), start_workers=False)
        self.addCleanup(lambda: self.service.close())
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        self.prompt = 'Internal routine instructions. ' * 30
        self.job = dict(id='native-job', prompt=self.prompt, fire_claim={'by': 'owner'})
        self.routine = dict(id=str(uuid4()), bot_id=self.bot['id'], name='Work refresh', _native_id='native-job')

    def test_restart_hides_only_generated_routine_instructions(self):
        task = self.service.mutate('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'My real request'}, str(uuid4()))
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored.update(routine_id=self.routine['id'], _native_job=self.job)
            self.service._put('tasks', stored)
            generated = self.service._message(self.bot['id'], self.prompt, task_id=task['id'], role='user', human_authored=False)
            human = self.service._message(self.bot['id'], self.prompt, task_id=task['id'], role='user')
            human['_human_device_id'] = 'person'
            self.service._put('messages', human)
            failure = self.service._message(self.bot['id'], 'The routine failed.', task_id=task['id'], kind='status')
        self.service.close()
        self.service = Service(self.tmp.name, FixtureProvider(self.tmp.name), start_workers=False)
        visible = {m['id'] for m in self.service.snapshot()['messages']}
        self.assertNotIn(generated['id'], visible)
        self.assertIn(human['id'], visible)
        self.assertIn(failure['id'], visible)
        self.assertTrue(self.service._get('messages', generated['id'])['_hidden'])
        self.assertEqual(self.service._get('tasks', task['id'])['_native_job']['prompt'], self.prompt)
