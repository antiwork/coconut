import time
import unittest

import test_tracked_work as fixtures
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class DispatchOrderTests(unittest.TestCase):
    setUp = fixtures.TrackedWorkTests.setUp
    tearDown = fixtures.TrackedWorkTests.tearDown
    track = fixtures.TrackedWorkTests.track
    row = fixtures.TrackedWorkTests.row
    write = fixtures.TrackedWorkTests.write

    def update(self, task, **fields):
        task = self.service._get('tasks', task['id'])
        task.update(fields)
        self.service._put('tasks', task)
        return task

    def positions(self):
        """Number the tasks that the dispatcher can start now, in its order."""
        tasks = [t for t in self.service._dispatch_order(self.service._all('tasks'))
                 if not t.get('_work_status_only') and self.service._dispatch_eligible(t, time.time())]
        return {t['id']: index + 1 for index, t in enumerate(tasks)}

    def send(self, bot=None, **fields):
        return self.write('POST', f'/v1/bots/{(bot or self.bot)["id"]}/messages',
                          dict(text='Inspect the next issue', mode='queue', **fields))

    def test_position_matches_global_dispatch_order_and_excludes_future_work(self):
        self.update(self.task, status='queued')
        future = self.send(scheduled_at='2099-01-01T00:00:00Z')
        other = self.write('POST', '/v1/bots', dict(name='Other'))
        independent = self.send(other)
        next_task = self.send()
        self.assertEqual(self.positions(), {self.task['id']: 1, independent['id']: 2, next_task['id']: 3})
        self.update(self.task, status='working')
        self.assertEqual(self.positions()[next_task['id']], 2)
        self.assertNotIn(future['id'], self.positions())
        self.update(next_task, _work_status_only=True)
        self.assertNotIn(next_task['id'], self.positions())

    def test_returned_result_runs_before_routine_source_audits(self):
        audit = self.update(self.task, status='queued', _source_audit_prompt='Check source coverage')
        result = self.update(self.send(), _is_return=True)
        self.assertEqual(self.positions(), {result['id']: 1, audit['id']: 2})
        tasks = self.service._dispatch_order(self.service._all('tasks'))
        self.assertEqual([t['id'] for t in tasks], [result['id'], audit['id']])

    def test_result_and_decision_continuations_run_before_new_work(self):
        ordinary = self.update(self.task, status='queued')
        returned = self.update(self.send(), _is_return=True)
        process = self.update(self.send(), _background_completion=['Tests passed'])
        decision = self.update(self.send(), _resume_decision='approved-card')
        self.assertEqual(self.positions(), {returned['id']: 1, process['id']: 2,
                                            decision['id']: 3, ordinary['id']: 4})

    def test_blocked_continuations_do_not_claim_a_queue_position(self):
        self.update(self.task, status='needs_you')
        child = self.send()
        self.update(child, _assignment_task_id=self.task['id'])
        other = self.send()
        self.assertEqual(self.positions(), {other['id']: 1})
        self.update(self.task, status='completed')
        self.assertEqual(self.positions(), {child['id']: 1, other['id']: 2})

    def test_recovery_reply_keeps_the_original_outcome_title(self):
        original = self.update(self.task, status='failed')
        reply = self.write('POST', f'/v1/tasks/{original["id"]}/replies',
                           dict(text='Resume this interrupted task after the deployment. Inspect saved results first.'))
        self.assertEqual(reply['title'], original['title'])

    def test_task_cancel_does_not_project_explicit_work_stop(self):
        item = self.track()
        stopped = self.write('POST', f'/v1/tasks/{self.task["id"]}/cancel', {})
        self.assertEqual(stopped['status'], 'cancelled')
        self.assertEqual(self.row()['work_state'], 'cancelled')
        self.assertIsNone(self.service._setting('work.cancelled.work:' + item['id']))


if __name__ == '__main__':
    unittest.main()
