"""Completed routine provenance and active-claim fencing."""
import copy
from contextlib import contextmanager
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from coconut.native_bridge import NativeBridge, TurnPaused
from coconut import native_routine_receipts as receipts
import test_native_recovery as fixtures


class CompletedRoutineTests(unittest.TestCase):
    setUp = fixtures.NativeCompletionReceiptTests.setUp
    get_job = fixtures.NativeCompletionReceiptTests.get_job
    _hermes_now = fixtures.NativeCompletionReceiptTests._hermes_now
    _under_fire_fence = fixtures.NativeCompletionReceiptTests._under_fire_fence
    _with_job = fixtures.NativeCompletionReceiptTests._with_job
    save_job_output = fixtures.NativeCompletionReceiptTests.save_job_output
    save_jobs = fixtures.NativeCompletionReceiptTests.save_jobs
    _record_run_outcome = fixtures.NativeCompletionReceiptTests._record_run_outcome
    _advance_after_run = fixtures.NativeCompletionReceiptTests._advance_after_run
    finish = fixtures.NativeCompletionReceiptTests.finish

    def bridge(self, finished=True, owner='owner'):
        task = dict(id='original-execution', _native_job=copy.deepcopy(self.job), _native_job_finished=finished)
        task['_native_job']['fire_claim'] = {'by': owner}
        return NativeBridge({'task': task}, lambda *args: None, lambda *args: None)

    @contextmanager
    def fire_claim_fence(self, identity, *, expected_owner):
        self.fenced = True
        try:
            yield (self.job.get('fire_claim') or {}).get('by') == expected_owner
        finally:
            self.fenced = False

    def run_tool(self, bridge):
        modules = {'cron.jobs': self, 'hermes_constants': SimpleNamespace(get_hermes_home=lambda: self.home)}
        with patch.dict('sys.modules', modules), bridge.claim():
            self.assertTrue(self.fenced)
            return 'executed'

    def test_completed_occurrence_can_continue_with_original_provenance(self):
        bridge = self.bridge()
        original = copy.deepcopy(bridge.request)
        self.assertTrue(self.finish()['ok'])
        self.assertEqual(self.run_tool(bridge), 'executed')
        self.assertEqual(bridge.request, original)

    def test_legacy_completion_requires_exact_native_success(self):
        bridge = self.bridge()
        self.assertTrue(self.finish()['ok'])
        receipts.receipt_path(self.home, receipts.completion_id('job', 'owner')).unlink()
        self.job.pop('_coconut_completion')
        bridge.request['task']['_native_job_completion'] = dict(job_id='job', owner='owner', success=True)
        self.assertEqual(self.run_tool(bridge), 'executed')
        self.job['_coconut_completion_owner'] = 'other-owner'
        with self.assertRaises(TurnPaused): self.run_tool(bridge)

    def test_later_native_success_does_not_authorize_failed_owner(self):
        bridge = self.bridge()
        self.request['success'] = False
        self.assertTrue(self.finish()['ok'])
        self.job['fire_claim'] = {'by': 'native-owner'}
        self.assertTrue(fixtures.NativeCompletionReceiptTests.mark_job_run(
            self, 'job', True, expected_fire_owner='native-owner'))
        with self.assertRaises(TurnPaused): self.run_tool(bridge)

    def test_finish_retry_cannot_launder_later_native_success(self):
        self.request['success'] = False
        self.assertTrue(self.finish()['ok'])
        self.job['fire_claim'] = {'by': 'native-owner'}
        fixtures.NativeCompletionReceiptTests.mark_job_run(
            self, 'job', True, expected_fire_owner='native-owner')
        self.assertTrue(self.finish()['ok'])
        self.assertNotEqual(receipts.read(self.home, receipts.completion_id('job', 'owner')),
                            dict(job_id='job', owner='owner', success=True))

    def test_finished_flag_does_not_authorize_a_lost_claim(self):
        bridge = self.bridge()
        self.job['fire_claim'] = None
        with self.assertRaises(TurnPaused): self.run_tool(bridge)
        self.job['fire_claim'] = {'by': 'new-owner'}
        with self.assertRaises(TurnPaused): self.run_tool(bridge)

    def test_active_claim_stays_fenced_even_with_a_finished_flag(self):
        bridge = self.bridge()
        with patch.object(receipts, 'completed', side_effect=AssertionError('Active claim consulted completion')):
            self.assertEqual(self.run_tool(bridge), 'executed')

    def test_cancelled_or_failed_occurrence_cannot_bypass_a_released_claim(self):
        bridge = self.bridge()
        self.request['success'] = False
        self.assertTrue(self.finish()['ok'])
        with self.assertRaises(TurnPaused): self.run_tool(bridge)

    def test_later_occurrence_does_not_erase_completed_execution_proof(self):
        bridge = self.bridge()
        self.assertTrue(self.finish()['ok'])
        self.job['fire_claim'] = {'by': 'next-owner'}
        self.request.update(owner='next-owner', success=True)
        self.assertTrue(self.finish()['ok'])
        self.assertEqual(self.run_tool(bridge), 'executed')
        with self.assertRaises(TurnPaused): self.run_tool(self.bridge(owner='unrelated-owner'))

    def test_no_finished_flag_does_not_release_a_stale_turn(self):
        bridge = self.bridge(finished=False)
        self.assertTrue(self.finish()['ok'])
        with self.assertRaises(TurnPaused): self.run_tool(bridge)

    def test_cancelled_tool_scope_remains_paused_after_completion(self):
        bridge = self.bridge()
        self.assertTrue(self.finish()['ok'])
        bridge.paused.set()
        with self.assertRaises(TurnPaused): bridge.start_execution()

    def test_malformed_or_unavailable_completion_proof_fails_closed(self):
        bridge = self.bridge()
        self.job['fire_claim'] = None
        with patch.object(receipts, 'read', side_effect=OSError('unavailable')):
            with self.assertRaises(TurnPaused): self.run_tool(bridge)


class ServiceCompletionTests(unittest.TestCase):
    def setUp(self):
        from uuid import UUID, uuid4, uuid5
        import tempfile
        from coconut.service import Service
        from coconut.providers import FixtureProvider
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(self.tmp.name, FixtureProvider(self.tmp.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Cron'}, str(uuid4()))
        routine = dict(id=str(uuid5(UUID(self.bot['id']), 'cron:job')), bot_id=self.bot['id'],
                       name='Probe', _native_id='job')
        self.service.provider.native_settings = lambda *a, **kw: dict(job=dict(id='job', fire_claim={'by': 'owner'}))
        with self.service.transaction():
            task = self.service.claim_native_routine(routine)
            self.task = self.service._get('tasks', task['id'])
            self.task['status'] = 'working'
            self.service._put('tasks', self.task)
        self.service.emit(task['id'], 'completed', {'summary': 'Successful routine'})
        self.task = self.service._get('tasks', task['id'])
        self.task['_native_job_finished'] = True
        self.expected = dict(job_id='job', owner='owner', success=True)

    def test_durable_service_completion_recovers_legacy_owner(self):
        self.assertEqual(self.service.native_routine_completion_input(self.task), self.expected)
        self.task['status'] = 'working'
        self.assertEqual(self.service.native_routine_completion_input(self.task), self.expected)

    def test_failed_history_cannot_be_replaced_by_later_success(self):
        with self.service.transaction():
            self.service._event('task.updated', dict(self.task, status='failed'), self.bot['id'], self.task['id'])
            self.service._event('task.updated', dict(self.task, status='completed'), self.bot['id'], self.task['id'])
        self.assertEqual(self.service.native_routine_completion_input(self.task), {})

    def test_missing_completion_or_wrong_routine_or_cancelled_fails_closed(self):
        for changes in ({'routine_id': 'other'}, {'status': 'cancelled'}, {'_native_job_finished': False}):
            with self.subTest(changes=changes):
                self.assertEqual(self.service.native_routine_completion_input(dict(self.task, **changes)), {})
        with self.service.transaction():
            self.service.db.execute("DELETE FROM events WHERE type='task.updated'")
        self.assertEqual(self.service.native_routine_completion_input(self.task), {})


if __name__ == '__main__':
    unittest.main()
