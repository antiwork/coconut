import threading
import unittest
from uuid import uuid4

from coconut.service import now
import test_work_status as fixtures


class WorkReportContinuationTests(unittest.TestCase):
    setUp = fixtures.WorkStatusTests.setUp
    tearDown = fixtures.WorkStatusTests.tearDown
    write = fixtures.WorkStatusTests.write
    run_task = fixtures.WorkStatusTests.run_task
    artifact = fixtures.WorkStatusTests.artifact
    row = fixtures.WorkStatusTests.row

    def setup_continuation(self):
        artifact = self.artifact()
        later = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Report the latest finding'})
        with self.service.transaction():
            original = self.service._get('tasks', self.task['id'])
            original.update(status='waiting', _native_turn_id='resumed-turn')
            self.service._put('tasks', original)
            later = self.service._get('tasks', later['id'])
            later.update(status='completed', _native_turn_id='report-turn',
                         _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', later)
            self.service._put('artifacts', dict(artifact, id=str(uuid4()), _activity='Reported the latest finding',
                _activity_task_id=later['id'], _activity_turn='report-turn', _activity_at=now()))
        return original

    def test_completed_report_does_not_hide_waiting_work(self):
        self.setup_continuation()
        self.assertEqual(self.row()['activity_state'], 'waiting')
        self.assertNotEqual(self.row()['activity'], 'Work finished')
        self.assertNotEqual(self.row()['activity'], 'Reviewing with Fable')

    def test_resumed_execution_uses_fresh_evidence_without_old_label(self):
        original = self.setup_continuation()
        self.service.running[original['id']] = threading.current_thread()
        try:
            self.assertEqual(self.row()['activity_state'], 'working')
            self.assertEqual(self.row()['activity'], 'Working')
        finally:
            self.service.running.clear()
