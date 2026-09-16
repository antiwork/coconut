import unittest
from datetime import datetime, timedelta, timezone

import test_pull_request_reviews as fixtures


class PRReviewFreshnessTests(unittest.TestCase):
    setUp = fixtures.PRReviewTests.setUp
    tearDown = fixtures.PRReviewTests.tearDown
    write = fixtures.PRReviewTests.write

    def prepare(self):
        artifact = self.service._get('artifacts', self.artifact_id)
        old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        artifact.update(checks_state='passed', pr_state='open', draft=False,
                        _reviewed_head=artifact['head_sha'], _review_checked_at=old)
        self.service._put('artifacts', artifact)
        task = self.service._get('tasks', self.child_id)
        task.update(status='completed', updated_at=old,
                    _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Review complete')]})
        self.service._put('tasks', task)
        return artifact, task

    def test_old_passing_review_without_a_card_gets_a_continuation(self):
        artifact, task = self.prepare()
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')
        self.assertEqual(self.service._get('tasks', task['id'])['_pr_review']['head_sha'], artifact['head_sha'])
        task = self.service._get('tasks', task['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')

    def test_rejected_merge_is_not_offered_again_for_the_same_head(self):
        artifact, task = self.prepare()
        self.service._put('decisions', dict(id='declined', bot_id=task['bot_id'], task_id=task['id'],
            status='rejected', merge_pr_url=artifact['url'], created_at=task['created_at']))
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')

    def test_old_head_rejection_does_not_suppress_a_new_heads_freshness_check(self):
        artifact, task = self.prepare()
        self.service._put('decisions', dict(id='declined-old-head', bot_id=task['bot_id'], task_id=task['id'],
            status='rejected', merge_pr_url=artifact['url'], _merge_pr_head='previous-head', created_at=task['created_at']))
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')

    def test_legacy_rejection_before_the_new_review_does_not_suppress_it(self):
        artifact, task = self.prepare()
        self.service._put('decisions', dict(id='declined-before-review', bot_id=task['bot_id'], task_id=task['id'],
            status='rejected', merge_pr_url=artifact['url'], created_at='2000-01-01T00:00:00Z'))
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')

    def test_legacy_rejection_survives_the_resumed_tasks_later_completion(self):
        artifact, task = self.prepare()
        artifact.pop('_review_checked_at')
        self.service._put('artifacts', artifact)
        task['updated_at'] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.service._put('tasks', task)
        self.service._put('decisions', dict(id='legacy-decline', bot_id=task['bot_id'], task_id=task['id'],
            status='rejected', merge_pr_url=artifact['url'],
            created_at=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()))
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')

    def test_dismissed_merge_proposal_stays_dismissed_for_the_same_head(self):
        artifact, task = self.prepare()
        self.service._put('decisions', dict(id='dismissed-proposal', bot_id=task['bot_id'], task_id=task['id'],
            status='dismissed', inbox_kind='proposal', merge_pr_url=artifact['url'],
            _merge_pr_head=artifact['head_sha'], created_at=task['created_at']))
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')

    def test_new_activity_on_another_peer_prevents_duplicate_review(self):
        artifact, task = self.prepare()
        newer = self.write('/v1/bots/' + self.desk['id'] + '/messages', {'text': 'Finish the latest PR changes'})
        peer = dict(artifact, id='new-peer', bot_id=self.desk['id'], task_id=newer['id'],
                    _activity_task_id=newer['id'], _activity_head=artifact['head_sha'],
                    _activity_at=datetime.now(timezone.utc).isoformat())
        peer.pop('_review_task_id', None)
        self.service._put('artifacts', peer)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')

    def test_recent_review_does_not_loop(self):
        artifact, task = self.prepare()
        artifact['_review_checked_at'] = datetime.now(timezone.utc).isoformat()
        self.service._put('artifacts', artifact)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
