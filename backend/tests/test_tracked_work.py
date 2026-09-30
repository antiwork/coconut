import threading
import unittest

from coconut.service import ServiceError
from test_service_pr import PRServiceTests
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


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

    def test_recent_updates_read_only_matching_messages(self):
        from unittest.mock import patch
        from uuid import uuid4
        task = self.service._get('tasks', self.task['id'])
        task['_turn_started_at'] = '2026-01-02T00:00:00Z'
        self.service._put('tasks', task)
        update = dict(task, id=str(uuid4()), _work_status_only=True, _work_context_id='work')
        self.service._put('tasks', update)
        expected = []
        for i in range(5):
            message = self.service._message(self.bot['id'], str(i), task_id=update['id'])
            message['created_at'] = '2026-01-03T00:00:00Z'
            self.service._put('messages', message)
            expected.append(message['id'])
        current = dict(task, id=str(uuid4()))
        with patch.object(self.service, '_all', side_effect=AssertionError('Unbounded history read')):
            rows = self.service.recent_work_updates(current)
        self.assertEqual([row['message_id'] for row in rows], expected[-3:])
        task['_turn_started_at'] = '2026-01-04T00:00:00Z'
        self.service._put('tasks', task)
        self.assertEqual(self.service.recent_work_updates(current), [])

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
        self.assertEqual(self.row()['activity'], 'Needs recovery')
        self.service.running[self.task['id']] = threading.current_thread()
        try:
            self.assertEqual(self.row()['activity'], 'Preparing the export')
        finally:
            self.service.running.clear()
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'completed'
            self.service._put('tasks', task)
        self.assertEqual(self.row()['activity'], 'Needs recovery')
        self.assertNotEqual(self.row()['status'], 'completed')

    def test_completion_dismissal_and_undo_persist(self):
        item = self.track(status='completed', activity='Export saved', completion_evidence='Read the saved export; all records are present.')
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
        item = self.track(status='completed', activity='Export saved', completion_evidence='Read the saved export; all records are present.')
        self.service._register_pr_artifact(self.task['id'], dict(url='https://github.com/example/project/pull/42'))
        result = self.write('POST', f'/v1/work/items/{item["id"]}/dismiss', dict(dismissal_id='one'))
        self.assertTrue(result['ok'])


class DelegatedTrackedWorkTests(unittest.TestCase):
    setUp = TrackedWorkTests.setUp
    tearDown = TrackedWorkTests.tearDown
    write = TrackedWorkTests.write
    track = TrackedWorkTests.track
    row = TrackedWorkTests.row
    def delegated(self, **changes):
        self.track()
        with self.service.transaction():
            parent = self.service._get('tasks', self.task['id'])
            parent.update(status='completed', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', parent)
            child = dict(parent, id='delegated', status='waiting', source_task_id=parent['id'],
                         _native_relay={'envelope_id': 'delivery'}, _is_return=False)
            child.update(changes)
            self.service._put('tasks', child)
        return child

    def test_native_handoff_keeps_reported_work_waiting(self):
        self.delegated(status='queued')
        self.assertEqual(self.row()['status'], 'waiting')
        self.assertEqual(self.row()['activity'], 'Queued')

    def test_child_decision_reaches_reported_work(self):
        child = self.delegated(status='needs_you')
        self.service._put('decisions', dict(id='pending-card', task_id=child['id'], bot_id=self.bot['id'], status='pending'))
        self.assertEqual(self.row()['activity'], 'Waiting for your decision')

    def test_finished_or_unrelated_child_does_not_claim_progress(self):
        for changes in (dict(status='completed'), dict(_conversation_epoch='old'),
                        dict(exchange_id='other'), dict(_is_return=True)):
            with self.subTest(changes=changes):
                self.delegated(**changes)
                self.assertEqual(self.row()['activity'], 'Needs recovery')
