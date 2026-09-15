import threading
import unittest
from datetime import datetime, timedelta, timezone

import test_service_pr as fixtures


class AutomaticWorkTests(unittest.TestCase):
    setUp = fixtures.PRServiceTests.setUp
    def tearDown(self):
        self.service.running.clear()
        fixtures.PRServiceTests.tearDown(self)
    write = fixtures.PRServiceTests.write

    def start(self, **fields):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task.update(status='working', created_at=(datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(),
                        _native_turn_id='turn', _conversation_epoch=self.service._setting('conversation_epoch'))
            task.update(fields)
            self.service._put('tasks', task)
        self.service.running[task['id']] = threading.current_thread()
        return task

    def sync(self):
        self.service._sync_automatic_work()
        return [a for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work']

    def test_long_user_request_appears_without_bot_tracking_call(self):
        self.start()
        before = self.service._all('messages')
        item = self.sync()[0]
        self.assertEqual(item['title'], self.task['title'])
        self.assertEqual(item['status'], 'working')
        self.assertEqual(self.service._all('messages'), before)
        self.assertEqual(self.sync()[0]['id'], item['id'])

    def test_quick_reply_routine_and_reaction_do_not_create_work(self):
        self.start(created_at=datetime.now(timezone.utc).isoformat())
        self.assertEqual(self.sync(), [])
        self.start(_native_job='routine')
        self.assertEqual(self.sync(), [])
        self.start(_native_job=None)
        with self.service.transaction():
            for message in self.service._all('messages'):
                message['reaction_event'] = True
                self.service._put('messages', message)
        self.assertEqual(self.sync(), [])

    def test_wait_and_resume_keep_the_same_item(self):
        self.start()
        item = self.sync()[0]
        self.service.running.clear()
        self.start(status='waiting')
        self.service.running.clear()
        waiting = self.sync()[0]
        self.assertEqual(waiting['id'], item['id'])
        self.assertEqual(waiting['work_state'], 'needs_recovery')
        self.start(_native_turn_id='next-turn')
        self.assertEqual(self.sync()[0]['id'], item['id'])
        self.assertEqual(self.sync()[0]['status'], 'working')

    def test_completion_can_be_dismissed_and_stays_dismissed(self):
        self.start()
        item = self.sync()[0]
        self.start(status='completed', _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Verified the requested result.')]})
        self.service.running.clear()
        self.assertEqual(self.sync()[0]['status'], 'completed')
        self.write('POST', f'/v1/work/items/{item["id"]}/dismiss', dict(dismissal_id='dismiss'))
        self.assertEqual(self.sync()[0]['work_dismissal_id'], 'dismiss')

    def test_failure_stays_visible_as_needing_attention(self):
        self.start()
        self.sync()
        self.start(status='failed')
        self.service.running.clear()
        self.assertEqual(self.sync()[0]['status'], 'blocked')

    def test_report_adopts_automatic_item_without_duplicates(self):
        self.start()
        item = self.sync()[0]
        reported = self.service._register_work(self.task['id'], dict(key='investigation', title='Investigate missing logs',
            activity='Checking the logs', status='working'))
        self.assertEqual(reported['id'], item['id'])
        self.assertEqual(len(self.sync()), 1)
        self.assertEqual(self.sync()[0]['activity'], 'Checking the logs')

    def test_existing_pr_prevents_duplicate_automatic_work(self):
        self.start()
        self.service._register_pr_artifact(self.task['id'], dict(url=fixtures.URL))
        self.assertEqual(self.sync(), [])

    def test_handoff_keeps_work_visible_after_parent_turn_ends(self):
        parent = self.start()
        self.sync()
        with self.service.transaction():
            child = dict(parent, id='child', source_bot_id=parent['bot_id'], _assignment_task_id=parent['id'], status='queued')
            self.service._put('tasks', child)
            parent['status'] = 'completed'
            self.service._put('tasks', parent)
        self.service.running.clear()
        item = self.sync()[0]
        self.assertEqual(item['status'], 'waiting')
        self.assertEqual(item['activity'], 'Queued')

    def test_later_pr_replaces_automatic_entry(self):
        self.start()
        self.sync()
        self.service._register_pr_artifact(self.task['id'], dict(url=fixtures.URL))
        self.assertEqual(self.sync(), [])
        self.assertEqual(self.service._tracked_items_input(self.service._get('tasks', self.task['id'])), [])

    def test_old_session_does_not_create_work(self):
        self.start(_conversation_epoch='old-session')
        self.assertEqual(self.sync(), [])

    def test_explicit_tracking_restores_an_automatic_item_hidden_by_pr(self):
        self.start()
        item = self.sync()[0]
        self.service._register_pr_artifact(self.task['id'], dict(url=fixtures.URL))
        self.assertEqual(self.sync(), [])
        report = self.service._register_work(self.task['id'], dict(key='follow-up', title='Check the rollout',
            activity='Checking the rollout', status='working'))
        self.assertEqual(report['id'], item['id'])
        self.assertEqual(self.sync()[0]['title'], 'Check the rollout')

    def test_existing_key_replaces_new_automatic_entry(self):
        self.start()
        original = self.service._register_work(self.task['id'], dict(key='investigation', title='Investigate logs',
            activity='Findings ready', status='completed', completion_evidence='Verified the log findings.'))
        self.start(status='completed', _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Verified the requested result.')]})
        self.service.running.clear()
        self.task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Continue the investigation', 'mode': 'queue'})
        self.start()
        self.assertEqual(len(self.sync()), 2)
        reported = self.service._register_work(self.task['id'], dict(key='investigation', title='Investigate logs',
            activity='Checking more evidence', status='working'))
        self.assertEqual(reported['id'], original['id'])
        self.assertEqual(len(self.sync()), 1)
