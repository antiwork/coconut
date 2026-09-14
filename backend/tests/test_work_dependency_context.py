"""Follow-up assignments use existing work instead of repeating status turns."""
import unittest

import test_work_waits as waits
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
        self.service._drain_process_completions()
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


if __name__ == '__main__':
    unittest.main()
