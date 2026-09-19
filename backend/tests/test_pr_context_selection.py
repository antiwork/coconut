import unittest
from unittest.mock import patch
from uuid import uuid4

import test_pull_request_reviews as fixtures


class PRContextSelectionTests(unittest.TestCase):
    setUp = fixtures.PRReviewTests.setUp
    tearDown = fixtures.PRReviewTests.tearDown
    write = fixtures.PRReviewTests.write

    def test_context_selects_owner_tasks_without_loading_unrelated_records(self):
        task = self.service._get('tasks', self.child_id)
        expected = self.service._tracked_pr_observations(task, [])
        self.assertEqual([a['url'] for a in expected], [fixtures.URL])
        with self.service.transaction():
            for index in range(100):
                self.service._put('tasks', dict(task, id=str(uuid4()), title='Unrelated task ' + str(index)))
                self.service._put('artifacts', dict(id=str(uuid4()), kind='file', title='Unrelated file',
                                                  text='Saved output. ' * 1000))
        with patch.object(self.service, '_all', side_effect=AssertionError('PR context must not load whole tables')):
            self.assertEqual(self.service._tracked_pr_observations(task, []), expected)

    def test_return_keeps_reported_link_after_review_owner_changes(self):
        source = self.service._get('tasks', self.child_id)
        source['_reported_pr_urls'] = [fixtures.URL.lower()]
        self.service._put('tasks', source)
        artifact = self.service._get('artifacts', self.artifact_id)
        artifact.update(_review_task_id='different-owner', _activity_task_id='different-owner')
        self.service._put('artifacts', artifact)
        callback = dict(source, id='callback', _is_return=True, source_task_id=source['id'])
        with patch.object(self.service, '_all', side_effect=AssertionError('No whole-table decode')):
            result = self.service._tracked_pr_observations(callback, [])
        self.assertEqual([a['url'] for a in result], [fixtures.URL])
