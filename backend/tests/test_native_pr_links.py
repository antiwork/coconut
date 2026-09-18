"""A PR reference must not become the deliverable for unrelated work."""
import unittest
import test_native_work_display as display
import test_native_kanban as native
from test_service_pr import Tracker, observation, URL


@unittest.skipUnless(native.SOURCE and native.PYTHON, 'Set Coconut Hermes paths for native integration checks.')
class NativePRLinksTests(unittest.TestCase):
    setUp = native.NativeKanbanTests.setUp
    write = native.NativeKanbanTests.write
    task = native.NativeKanbanTests.task
    native_program = native.NativeKanbanTests.native_program
    outcome = display.NativeWorkDisplayTests.outcome

    def report(self, payload):
        identity, item = self.outcome()
        owner = self.service._get('tasks', item['task_id'])
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        self.service.pr_tracker = Tracker(observation())
        return identity, item, owner, self.service._register_pr_artifact(owner['id'], dict(url=URL, **payload(identity)))

    def test_reference_does_not_inherit_the_current_native_outcome(self):
        identity, item, owner, pr = self.report(lambda native: {})
        self.assertNotIn('work_outcome_id', pr)

    def test_explicit_native_deliverable_keeps_one_outcome(self):
        identity, item, owner, pr = self.report(lambda native: dict(work_id=native))
        self.assertEqual(pr['work_outcome_id'], item['id'])
        repeated = self.service._register_pr_artifact(owner['id'], dict(url=URL))
        self.assertEqual(repeated['work_outcome_id'], item['id'])

    def test_admin_can_remove_a_wrong_reference_without_changing_native_execution(self):
        identity, item, owner, pr = self.report(lambda native: dict(work_id=native))
        before = dict(self.service.kanban.tasks[owner['id']])
        cleared = self.write('/v1/work/pull-requests', dict(bot_id=self.bot['id'], url=URL, work_id=None))
        self.assertNotIn('work_outcome_id', cleared)
        self.assertNotIn('work_outcome_id', self.service._get('artifacts', pr['id']))
        self.assertEqual(self.service.kanban.tasks[owner['id']], before)

    def test_worker_cannot_remove_an_existing_outcome_link(self):
        from coconut.service import ServiceError
        identity, item, owner, pr = self.report(lambda native: dict(work_id=native))
        with self.assertRaises(ServiceError):
            self.service._register_pr_artifact(owner['id'], dict(url=URL, work_id=None))
        self.assertEqual(self.service._get('artifacts', pr['id'])['work_outcome_id'], item['id'])
