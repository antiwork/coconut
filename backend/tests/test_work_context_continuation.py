import unittest

import test_work_self_recovery as fixtures


class WorkContextContinuationTests(unittest.TestCase):
    setUp = fixtures.WorkSelfRecoveryTests.setUp
    tearDown = fixtures.WorkSelfRecoveryTests.tearDown
    start = fixtures.WorkSelfRecoveryTests.start
    sync = fixtures.WorkSelfRecoveryTests.sync
    write = fixtures.WorkSelfRecoveryTests.write
    stranded = fixtures.WorkSelfRecoveryTests.stranded
    recover = fixtures.WorkSelfRecoveryTests.recover
    recovery_process = fixtures.WorkSelfRecoveryTests.recovery_process

    def test_followup_can_wait_on_its_assignments_original_process(self):
        original, recovery, receipt = self.recovery_process()
        followup = dict(recovery, id='followup', _assignment_task_id=recovery['id'], _native_turn_id='followup-turn')
        followup.pop('_self_recovery', None)
        followup.pop('_work_context_id', None)
        self.service._put('tasks', followup)
        listed = self.service.wait_for_task(followup['id'], {})
        self.assertIn('existing-test', [p['process_id'] for p in listed['processes']])
        result = self.service.wait_for_task(followup['id'], dict(process_id='existing-test', reason='Waiting for existing tests'))
        self.assertEqual(result['status'], 'waiting')

    def test_unrelated_followup_cannot_attach_to_another_work_item(self):
        original, recovery, receipt = self.recovery_process()
        unrelated = dict(recovery, id='unrelated', exchange_id='unrelated-exchange', _native_turn_id='unrelated-turn')
        unrelated.pop('_self_recovery', None)
        unrelated.pop('_work_context_id', None)
        self.service._put('tasks', unrelated)
        self.service._put('exchanges', dict(id='unrelated-exchange', cancelled=False))
        listed = self.service.wait_for_task(unrelated['id'], {})
        self.assertEqual(listed['processes'], [])


if __name__ == '__main__':
    unittest.main()
