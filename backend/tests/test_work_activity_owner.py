import unittest

import test_pull_request_reviews as fixtures


class WorkActivityOwnerTests(unittest.TestCase):
    setUp = fixtures.PRReviewTests.setUp
    tearDown = fixtures.PRReviewTests.tearDown
    write = fixtures.PRReviewTests.write
    green = fixtures.PRReviewTests.green

    def test_activity_from_a_later_task_takes_over_a_finished_review(self):
        # Since "Keep PR ownership with the current execution", a new execution continues a review that finished, and the
        # status callbacks return to it. The old reviewer stays finished. This is the rule, and not a loss of the route.
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')
        original = self.provider.start_task
        def start(task, bot, history, emit):
            def report(kind, payload):
                if kind == 'artifact':
                    payload = dict(payload, activity='Checking the pull request')
                return emit(kind, payload)
            return original(task, bot, history, report)
        self.provider.start_task = start
        later = self.write(f'/v1/bots/{self.dev["id"]}/messages', {'text': 'Check this PR again'})
        self.service._run_task(later['id'])
        artifact = self.service._get('artifacts', self.artifact_id)
        self.assertEqual(artifact['_review_task_id'], later['id'])
        self.assertEqual(artifact['_activity_task_id'], later['id'])
        self.assertTrue(artifact.get('_review_ownership_at'))
        self.green()
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')
        self.assertEqual(self.service._get('tasks', later['id'])['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
