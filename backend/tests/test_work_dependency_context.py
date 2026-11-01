"""Follow-up assignments use existing work instead of repeating status turns."""
import unittest

import test_work_waits as waits
from coconut.service import ServiceError, now
from coconut.work_progress import NativeWork
from test_work_progress import progress


class DependencyContextTests(unittest.TestCase):
    setUp = waits.WorkWaitTests.setUp
    tearDown = waits.WorkWaitTests.tearDown
    write = waits.WorkWaitTests.write
    target = waits.WorkWaitTests.target
    finish_target = waits.WorkWaitTests.finish_target

    def test_followup_receives_owner_and_waits_until_its_result(self):
        target = self.target()
        unrelated = self.write(f'/v1/bots/{self.desk["id"]}/messages', {'text': 'Unrelated request'})
        def start(task, bot, history, emit):
            owners = {row['id']: row for row in task['_work_dependencies']}
            self.assertIn(target, owners)
            self.assertNotIn(unrelated['id'], owners)
            self.assertNotIn(self.parent['id'], owners)  # The parent waits on this task.
            emit('work_wait', {'task_id': target, 'reason': 'Waiting for publication'})
            emit('completed', {'summary': 'Waiting for publication.', '_work_todos': progress()})
        self.provider.start_task = start
        self.service._run_task(self.child)
        task = self.service._get('tasks', self.child)
        self.assertEqual(task['status'], 'waiting')
        self.assertNotIn('_work_retry', task)
        self.finish_target(target)
        self.service._drain_task_completions()
        resumed = self.service._get('tasks', self.child)
        self.assertEqual(resumed['status'], 'queued')
        self.assertIn('Publication verified', resumed['_background_completion'][0])

    def test_failure_names_the_affected_assignment(self):
        for _ in range(3):
            self.service._run_task(self.child)
        task = self.service._get('tasks', self.child)
        self.assertEqual(task['status'], 'failed')
        self.assertIn(task['title'], task['summary'])
        self.assertNotIn('ended repeated turns', task['summary'])


class DependencyGateTests(unittest.TestCase):
    """The dependency prompt reaches exactly the turns that restore a native task list."""
    setUp = waits.WorkWaitTests.setUp
    tearDown = waits.WorkWaitTests.tearDown
    write = waits.WorkWaitTests.write
    target = waits.WorkWaitTests.target

    def started(self, task_id):
        self.service._run_task(task_id)
        task = self.provider.starts[-1][0]
        self.assertEqual(task['id'], task_id)
        return task

    def queued_return(self):
        self.service._drain_returns()
        return next(t for t in self.service._all('tasks') if t.get('_is_return') and t['status'] == 'queued')

    def test_return_turn_receives_no_dependency_context(self):
        target = self.target()
        self.provider.mode = 'done'
        self.service._run_task(self.child)
        returned = self.queued_return()
        started = self.started(returned['id'])
        self.assertTrue(started['_is_return'])
        self.assertFalse(NativeWork({'operation': 'start', 'task': started}).enabled)
        self.assertNotIn('_work_dependencies', started)
        # The peer exists in this exchange. Only the gate keeps it out of a turn without a task list.
        self.assertIn(target, {t['id'] for t in self.service.work_dependencies(self.service._get('tasks', returned['id']))})

    def test_pr_review_turn_receives_no_dependency_context(self):
        self.target()
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task.update(_pr_review={'url': 'https://github.com/a/b/pull/1', 'head_sha': 'abc123', 'checked_at': now()},
                        _work_todos=progress())
            self.service._put('tasks', task)
        started = self.started(self.child)
        self.assertEqual(started['_pr_review']['head_sha'], 'abc123')
        self.assertFalse(NativeWork({'operation': 'start', 'task': started}).enabled)
        self.assertNotIn('_work_dependencies', started)

    def test_context_turn_receives_no_dependency_context(self):
        self.target()
        self.provider.mode = 'decision'
        self.service._run_task(self.child)
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'needs_you')
        decision = self.service._all('decisions')[-1]
        self.write(f"/v1/decisions/{decision['id']}/context", {'question': 'Which patch is this?'})
        context = self.service._all('contexts')[-1]
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(5)
        started = self.provider.last_context_task
        self.assertEqual(started['id'], self.child)
        self.assertFalse(NativeWork({'operation': 'context', 'task': started}).enabled)
        self.assertNotIn('_work_dependencies', started)

    def test_handoff_return_keeps_dependency_context(self):
        target = self.target()
        ops = self.write('/v1/bots', {'name': 'Ops', 'role': 'Operations'})
        with self.service.transaction():
            child = self.service._get('tasks', self.child)
            child['status'] = 'working'; self.service._put('tasks', child)
            grandchild = self.service._store_bot_message(self.child, {'target_bot_id': ops['id'], 'text': 'Publish the validated patch'})
        self.service.emit(self.child, 'completed', {'summary': 'Delegated.'})
        self.provider.mode = 'done'
        self.service._run_task(grandchild['task_id'])
        returned = self.queued_return()
        self.assertEqual(returned['bot_id'], self.dev['id'])
        started = self.started(returned['id'])
        self.assertTrue(started['_is_return'])
        self.assertEqual(started['_handoff_assignment']['return_to_bot_id'], self.desk['id'])
        self.assertTrue(NativeWork({'operation': 'start', 'task': started}).enabled)
        self.assertIn(target, {t['id'] for t in started['_work_dependencies']})

    def test_saved_todos_keep_dependency_context_without_a_source_bot(self):
        target = self.target()
        with self.service.transaction():
            parent = self.service._get('tasks', self.parent['id'])
            parent.update(status='queued', _work_todos=progress(), _work_resume=True)
            self.service._put('tasks', parent)
        started = self.started(self.parent['id'])
        self.assertFalse(started.get('source_bot_id'))
        self.assertTrue(NativeWork({'operation': 'start', 'task': started}).enabled)
        self.assertIn(target, {t['id'] for t in started['_work_dependencies']})


class DependencyListingTests(unittest.TestCase):
    """Listing peers is read-only. Only an explicit wait needs a running, unpaused task."""
    setUp = waits.WorkWaitTests.setUp
    tearDown = waits.WorkWaitTests.tearDown
    write = waits.WorkWaitTests.write
    target = waits.WorkWaitTests.target

    def activate(self):
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', task)

    def check_listing_registers_nothing(self, target):
        task = self.service._get('tasks', self.child)
        before = self.service._all('native_receipts')
        listed = self.service.work_dependencies(task)
        self.assertIn(target, {t['id'] for t in listed})
        with self.assertRaises(ServiceError) as rejected:
            self.service.wait_for_task(self.child, {'task_id': target, 'reason': 'Waiting for publication'})
        self.assertEqual(rejected.exception.status, 409)
        self.assertEqual(self.service._all('native_receipts'), before)
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'working')

    def test_listing_during_shutdown_registers_nothing(self):
        target = self.target()
        self.activate()
        self.service.stopping.set()
        try:
            self.check_listing_registers_nothing(target)
        finally:
            self.service.stopping.clear()

    def test_listing_for_archived_bot_registers_nothing(self):
        target = self.target()
        self.activate()
        with self.service.transaction():
            bot = self.service._get('bots', self.dev['id'])
            bot['archived'] = True
            self.service._put('bots', bot)
        self.assertTrue(self.service._task_paused(self.service._get('tasks', self.child)))
        self.check_listing_registers_nothing(target)


if __name__ == '__main__':
    unittest.main()
