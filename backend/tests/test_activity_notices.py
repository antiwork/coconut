import tempfile
import unittest
from uuid import uuid4
from coconut.service import Service
from test_service import ControlledProvider


class ActivityNoticeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'test')
        self.task = self.service.mutate('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Check files', 'mode': 'queue'}, str(uuid4()), 'test')

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def test_failure_uses_status_and_progress_stays_in_history(self):
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        self.service.emit(task['id'], 'activity', {'text': 'Read 3 files'})
        self.service.emit(task['id'], 'failed', {'summary': 'The test database is unavailable.'})
        messages = self.service._all('messages')
        self.assertEqual(next(m for m in messages if m['text'] == 'Read 3 files')['kind'], 'activity')
        self.assertEqual(next(m for m in messages if m['text'] == 'The test database is unavailable.')['kind'], 'status')

    def test_migration_preserves_old_failure_without_exposing_progress_or_settings(self):
        with self.service.transaction():
            self.service.db.execute("DELETE FROM settings WHERE key='activity_notices_migrated'")
            failure = self.service._message(self.bot['id'], 'Tests failed.', task_id=self.task['id'], kind='activity')
            progress = self.service._message(self.bot['id'], 'Ran 6 focused checks · All passed', task_id=self.task['id'], kind='activity')
            settings = self.service._message(self.bot['id'], 'New actions use this permission mode. An action already executing may finish.', role='system', kind='activity')
            old = dict(self.task, status='failed', summary='Tests failed.')
            self.service._event('task.updated', old, self.bot['id'], self.task['id'])
        self.service.close()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.assertEqual(self.service._get('messages', failure['id'])['kind'], 'status')
        self.assertEqual(self.service._get('messages', progress['id'])['kind'], 'activity')
        self.assertEqual(self.service._get('messages', settings['id'])['kind'], 'activity')
        self.assertEqual(self.service._get('messages', failure['id'])['text'], failure['text'])
