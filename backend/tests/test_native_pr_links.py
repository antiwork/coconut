"""A PR reference must not become the deliverable for unrelated work."""
import unittest
import test_native_work_display as display
import test_native_kanban as native
from test_service_pr import Tracker, observation, URL
from hermes_env import needs_hermes


@needs_hermes
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

    def test_check_signal_resumes_original_native_task_after_retry(self):
        from dataclasses import replace
        from uuid import uuid4
        from unittest.mock import patch
        identity, item, owner, pr = self.report(lambda native: dict(work_id=native))
        owner.update(status='queued', _execution_session_id=owner['id'])
        self.service._put('tasks', owner)
        self.service.kanban.call({'op': 'tick', 'rows': self.service._kanban_rows(), 'capacity': 0})
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        retry = dict(owner, id=str(uuid4()), parent_task_id=owner['id'],
                     source_task_id=None, source_bot_id=None, exchange_depth=0)
        retry.pop('_native_outcome_id')
        self.service._put('tasks', retry)
        pr['_review_task_id'] = retry['id']
        self.service._put('artifacts', pr)
        self.native_program("board.kb.block_task(board.conn, sys.argv[2], reason='Worker stopped before recording its outcome')", identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.service.pr_tracker.observations.append(replace(observation(), pr_state='open', draft=False, checks_state='passed'))
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('artifacts', pr['id'])['_review_task_id'], owner['id'])
        with patch.object(self.service, '_run_task') as start:
            self.service._dispatch_kanban()
        start.assert_any_call(owner['id'])
        self.assertEqual(self.service.kanban.tasks[owner['id']]['id'], identity)
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'running')
