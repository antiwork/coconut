import unittest

import test_automatic_work as automatic
import test_tracked_work as tracked
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class WorkAuditIsolationTests(unittest.TestCase):
    setUp = automatic.AutomaticWorkTests.setUp
    tearDown = automatic.AutomaticWorkTests.tearDown
    write = automatic.AutomaticWorkTests.write
    start = automatic.AutomaticWorkTests.start
    sync = automatic.AutomaticWorkTests.sync
    track = tracked.TrackedWorkTests.track

    def test_chat_does_not_steer_an_unrelated_background_audit(self):
        audit = self.start(status='waiting', _source_audit_prompt='Review assigned issue')
        result = self.write('POST', f'/v1/bots/{audit["bot_id"]}/messages',
                            {'text': 'What happens after the merge?', 'mode': 'auto'})
        self.assertNotEqual(result['id'], audit['id'])
        self.assertEqual(self.service._get('tasks', audit['id']), audit)

    def test_audit_does_not_become_automatic_work_after_a_reply(self):
        self.start(_source_audit_prompt='Review assigned issue')
        self.assertEqual(self.sync(), [])

    def test_old_automatic_audit_row_is_removed_without_stopping_its_task(self):
        task = self.start()
        item = self.sync()[0]
        task['_source_audit_prompt'] = 'Review assigned issue'
        self.service._put('tasks', task)
        self.assertEqual(self.sync(), [])
        self.assertTrue(self.service._get('artifacts', item['id'])['_hidden'])
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'working')

    def test_audit_report_preserves_the_execution_owner(self):
        owner = self.start()
        item = self.track()
        audit = self.write('POST', f'/v1/bots/{owner["bot_id"]}/messages',
                           {'text': 'Review assigned issue', 'mode': 'queue'})
        with self.service.transaction():
            audit = self.service._get('tasks', audit['id'])
            audit.update(status='working', _source_audit_prompt='Review assigned issue', _native_turn_id='audit-turn')
            self.service._put('tasks', audit)
        updated = self.service._register_work(audit['id'], dict(key='account-export', title='Export account data',
            activity='Preparing the export', status='working'))
        self.assertEqual(updated['task_id'], owner['id'])
        self.assertEqual(updated['id'], item['id'])
        with self.service.transaction():
            audit['status'] = 'failed'
            self.service._put('tasks', audit)
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == item['id'])
        self.assertEqual(row['activity_state'], 'working')
        self.assertEqual(row['activity'], 'Preparing the export')


if __name__ == '__main__':
    unittest.main()
