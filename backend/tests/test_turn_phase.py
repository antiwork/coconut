import tempfile
import unittest
from uuid import uuid4

from coconut.service import Service
from test_service import ControlledProvider


class RecordedPhaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.device = self.service.pair({'code': self.service.pairing_code()})['device_id']
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Ada'}, str(uuid4()), self.device)
        self.task = self.service.mutate('POST', f'/v1/bots/{self.bot["id"]}/messages',
                                        dict(text='Check the test', mode='queue'), str(uuid4()), self.device)
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def stored(self):
        with self.service.transaction():
            return self.service._get('tasks', self.task['id'])

    def public(self):
        return next(task for task in self.service.snapshot()['tasks'] if task['id'] == self.task['id'])

    def test_working_task_publishes_its_phase_and_keeps_the_turn_start(self):
        self.service.emit(self.task['id'], 'phase', {'phase': 'thinking'})
        started = self.stored()['turn_started_at']
        self.service.emit(self.task['id'], 'phase', {'phase': 'testing'})
        self.assertEqual(self.public()['phase'], 'testing')
        self.assertEqual(self.public()['turn_started_at'], started)

    def test_unknown_phase_is_ignored(self):
        self.service.emit(self.task['id'], 'phase', {'phase': 'rm -rf /'})
        self.assertNotIn('phase', self.stored())

    def test_phase_ends_with_the_turn(self):
        self.service.emit(self.task['id'], 'phase', {'phase': 'reading'})
        self.service.emit(self.task['id'], 'text_delta', {'text': 'Done.'})
        self.service.emit(self.task['id'], 'completed', {'summary': 'Complete.'})
        self.assertNotIn('phase', self.stored())
        self.assertNotIn('turn_started_at', self.stored())

    def test_finished_task_does_not_take_a_late_phase(self):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'completed'
            self.service._put('tasks', task)
        self.service.emit(self.task['id'], 'phase', {'phase': 'writing'})
        self.assertNotIn('phase', self.stored())


if __name__ == '__main__':
    unittest.main()
