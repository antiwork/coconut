import unittest

import test_work_audit_isolation as isolation


class WorkAuditReturnTests(unittest.TestCase):
    setUp = isolation.WorkAuditIsolationTests.setUp
    tearDown = isolation.WorkAuditIsolationTests.tearDown
    write = isolation.WorkAuditIsolationTests.write
    start = isolation.WorkAuditIsolationTests.start
    track = isolation.WorkAuditIsolationTests.track

    def test_audit_return_preserves_the_execution_owner(self):
        owner = self.start()
        item = self.track()
        audit = self.write('POST', f'/v1/bots/{owner["bot_id"]}/messages',
                           {'text': 'Review assigned issue', 'mode': 'queue'})
        reply = self.write('POST', f'/v1/bots/{owner["bot_id"]}/messages',
                           {'text': 'The audit result returned', 'mode': 'queue'})
        with self.service.transaction():
            audit['_source_audit_prompt'] = 'Review assigned issue'
            self.service._put('tasks', audit)
            reply.update(status='working', _assignment_task_id=audit['id'], _native_turn_id='audit-return')
            self.service._put('tasks', reply)
        updated = self.service._register_work(reply['id'], dict(key='account-export', title='Export account data',
            activity='Preparing the export', status='working'))
        self.assertEqual(updated['task_id'], owner['id'])
        self.assertEqual(updated['id'], item['id'])


if __name__ == '__main__':
    unittest.main()
