import threading
import unittest

from coconut.service import ServiceError
from test_service_pr import PRServiceTests


class TrackedWorkTests(unittest.TestCase):
    setUp = PRServiceTests.setUp
    tearDown = PRServiceTests.tearDown
    write = PRServiceTests.write

    def track(self, **changes):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        payload = dict(key='account-export', title='Export account data', activity='Preparing the export', status='working')
        payload.update(changes)
        return self.service._register_work(self.task['id'], payload)

    def row(self):
        return self.service.snapshot()['artifacts'][0]

    def test_updates_reuse_item_without_messages_or_github_calls(self):
        before = len(self.service._all('messages'))
        first = self.track()
        second = self.track(activity='Checking the export', status='ready')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.service._all('artifacts')), 1)
        self.assertEqual(self.row()['activity'], 'Checking the export')
        self.assertEqual(self.tracker.urls, [])
        self.assertEqual(len(self.service._all('messages')), before)

    def test_working_label_requires_live_execution(self):
        self.track()
        self.assertEqual(self.row()['activity'], 'Activity unconfirmed')
        self.service.running[self.task['id']] = threading.current_thread()
        try:
            self.assertEqual(self.row()['activity'], 'Preparing the export')
        finally:
            self.service.running.clear()
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'completed'
            self.service._put('tasks', task)
        self.assertEqual(self.row()['activity'], 'Needs attention')
        self.assertNotEqual(self.row()['status'], 'completed')

    def test_completion_dismissal_and_undo_persist(self):
        item = self.track(status='completed', activity='Export saved')
        path = f'/v1/work/items/{item["id"]}/dismiss'
        self.write('POST', path, dict(dismissal_id='first'))
        self.assertEqual(self.row()['work_dismissal_id'], 'first')
        self.write('POST', path, dict(dismissal_id='second'))
        with self.assertRaises(ServiceError):
            self.write('POST', path, dict(dismissal_id='first', undo=True))
        self.write('POST', path, dict(dismissal_id='second', undo=True))
        self.assertIsNone(self.row()['work_dismissal_id'])
        self.assertEqual(self.row()['status'], 'completed')

    def test_active_work_cannot_be_dismissed(self):
        item = self.track()
        with self.assertRaises(ServiceError):
            self.write('POST', f'/v1/work/items/{item["id"]}/dismiss', dict(dismissal_id='one'))

    def test_invalid_input_does_not_create_item(self):
        for fields in [dict(status='merged'), dict(activity=''), dict(key='bad key'), dict(title='a\nb'), dict(activity='a'*81)]:
            with self.assertRaises(ServiceError): self.track(**fields)
        self.assertEqual(self.service._all('artifacts'), [])

    def test_old_task_cannot_overwrite_new_owner(self):
        item = self.track()
        newer = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Continue the export', 'mode': 'queue'})
        original = self.task
        self.task = newer
        self.track(activity='Saving the export')
        self.task = original
        with self.assertRaises(ServiceError): self.track(status='completed')
        self.assertEqual(self.service._get('artifacts', item['id'])['activity'], 'Saving the export')

    def test_generic_dismissal_with_prs_in_the_same_list(self):
        item = self.track(status='completed', activity='Export saved')
        self.service._register_pr_artifact(self.task['id'], dict(url='https://github.com/example/project/pull/42'))
        result = self.write('POST', f'/v1/work/items/{item["id"]}/dismiss', dict(dismissal_id='one'))
        self.assertTrue(result['ok'])
