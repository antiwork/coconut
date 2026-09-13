import unittest

import test_pull_request_reviews as fixtures


class WorkActivityOwnerTests(unittest.TestCase):
    setUp = fixtures.PRReviewTests.setUp
    tearDown = fixtures.PRReviewTests.tearDown
    write = fixtures.PRReviewTests.write
    green = fixtures.PRReviewTests.green

    def test_activity_from_later_task_preserves_review_return_route(self):
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
        self.assertEqual(artifact['_review_task_id'], self.child_id)
        self.assertEqual(artifact['_activity_task_id'], later['id'])
        self.green()
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'queued')
        self.assertEqual(self.service._get('tasks', later['id'])['status'], 'completed')
