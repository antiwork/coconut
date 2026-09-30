import unittest
from uuid import uuid4
from coconut.service import now
from test_work_followups import WorkFollowupTests
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class WorkBlockerTests(unittest.TestCase):
    setUp = WorkFollowupTests.setUp
    tearDown = WorkFollowupTests.tearDown
    write = WorkFollowupTests.write
    run_task = WorkFollowupTests.run_task
    prepare = WorkFollowupTests.prepare
    row = WorkFollowupTests.row

    def blocker(self, reported=False):
        blocked = dict(self.followup, id=str(uuid4()), status='completed' if reported else 'failed',
                       _native_turn_id='blocked-turn', updated_at=now())
        with self.service.transaction():
            self.service._put('tasks', blocked)
            if reported:
                artifact = self.service._all('artifacts')[0]
                self.service._set_work_activity(artifact, blocked,
                    {'activity': 'Cleanup is ready for publication', 'activity_state': 'blocked'})
        return blocked

    def test_live_watcher_cannot_hide_failed_work(self):
        self.prepare(); blocked = self.blocker()
        self.assertEqual(self.row()['activity'], 'Needs recovery')
        self.assertEqual(self.row()['activity_state'], 'blocked')
        self.assertEqual(self.row()['activity_bot_id'], blocked['bot_id'])

    def test_live_watcher_cannot_hide_current_reported_blocker(self):
        self.prepare(); self.blocker(reported=True)
        self.assertEqual(self.row()['activity'], 'Cleanup is ready for publication')
        self.assertEqual(self.row()['activity_state'], 'blocked')

    def test_old_report_does_not_override_changed_head(self):
        self.prepare(); self.blocker(reported=True)
        with self.service.transaction():
            artifact = self.service._all('artifacts')[0]
            artifact['head_sha'] = 'b' * 40; self.service._put('artifacts', artifact)
        self.assertEqual(self.row()['activity_state'], 'working')

    def test_active_child_can_resolve_failed_parent(self):
        self.prepare()
        with self.service.transaction():
            self.root['status'] = 'failed'; self.service._put('tasks', self.root)
        self.assertEqual(self.row()['activity_state'], 'working')

    def test_new_activity_is_visible_without_reassigning_review_ownership(self):
        import threading
        self.prepare()
        next_task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Resume the remaining cleanup'})
        with self.service.transaction():
            task = self.service._get('tasks', next_task['id'])
            task.update(status='working', _native_turn_id='resume-turn',
                        _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', task)
            artifact = self.service._all('artifacts')[0]
            self.service._set_work_activity(artifact, task, {'activity': 'Checking the published cleanup'})
        self.service.running[task['id']] = threading.current_thread()
        try:
            self.assertEqual(self.row()['activity'], 'Checking the published cleanup')
            self.assertEqual(self.service._all('artifacts')[0]['_review_task_id'], self.root['id'])
        finally:
            self.service.running.clear()

    def test_recovery_routes_to_the_failed_bot_instead_of_the_watcher(self):
        self.prepare()
        bot = dict(self.service._get('bots', self.bot['id']), id=str(uuid4()))
        bot['profile_id'] = bot['id']
        self.service._put('bots', bot)
        blocked = self.blocker()
        blocked['bot_id'] = bot['id']
        self.service._put('tasks', blocked)
        self.assertEqual(self.row()['work_state'], 'needs_recovery')
        self.assertEqual(self.row()['activity_bot_id'], bot['id'])
