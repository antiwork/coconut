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
            if (self.job.get('fire_claim') or {}).get('by') == bridge.request['task']['_native_job']['fire_claim']['by']:
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
        self.assertEqual(self.run_tool(bridge), 'executed')
        with self.assertRaises(TurnPaused): self.run_tool(self.bridge(owner='other-owner'))

    def legacy_bridge(self):
        bridge = self.bridge()
        self.assertTrue(self.finish()['ok'])
        receipts.receipt_path(self.home, receipts.completion_id('job', 'owner')).unlink()
        self.job.pop('_coconut_completion')
        bridge.request['task']['_native_job_completion'] = dict(job_id='job', owner='owner', success=True)
        return bridge

    def claim_job_for_fire(self, identity, **kwargs):
        self.job['fire_claim'] = {'by': 'later-owner'}
        return copy.deepcopy(self.job)

    def claim_dispatch(self, identity): return True

    def test_validated_legacy_continuation_survives_later_claim(self):
        bridge = self.legacy_bridge()
        self.assertEqual(self.run_tool(bridge), 'executed')
        receipts.claim(self.home, 'job', self, manual=True)
        self.assertEqual(self.run_tool(bridge), 'executed')
        self.assertEqual(receipts.read(self.home, receipts.completion_id('job', 'owner')),
                         bridge.request['task']['_native_job_completion'])

    def test_legacy_validation_rechecks_after_taking_receipt_lock(self):
        bridge = self.legacy_bridge()
        original = receipts.locked
        @contextmanager
        def later_claim(home, identity):
            self.assertFalse(self.fenced, 'Native fence held while taking receipt lock')
            with patch.object(receipts, 'locked', original):
                receipts.claim(home, identity, self, manual=True)
            with original(home, identity):
                yield
        with patch.object(receipts, 'locked', side_effect=later_claim):
            with self.assertRaises(TurnPaused): self.run_tool(bridge)
        self.assertFalse(receipts.receipt_path(self.home, receipts.completion_id('job', 'owner')).exists())

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


class CompletionRetentionTests(ServiceCompletionTests):
    def setUp(self):
        super().setUp()
        from pathlib import Path
        import tempfile
        from coconut import profiles
        self.home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.home_tmp.cleanup)
        # Coconut keeps the receipts beside the profile home, in its own runs folder.
        self.home = Path(self.home_tmp.name) / 'home'
        self.home.mkdir()
        self.modules = patch.dict('sys.modules', {'profiles': profiles})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        with self.service.transaction():
            self.task.update(created_at='2000-01-01T00:00:00+00:00')
            self.service._put('tasks', self.task)
        self.identity = receipts.completion_id('job', 'owner')
        receipts.save(self.home, self.identity, self.expected)
        self.candidates = {self.identity: self.expected}
        self.after_snapshot = lambda: None
        self.before_prune = lambda: None
        def native(operation, bots, **kwargs):
            if operation == 'routines_read':
                snapshot = copy.deepcopy(self.candidates)
                self.after_snapshot()
                return dict(routines=[], completions={self.bot['id']: snapshot})
            if operation == 'routine_prune_completions':
                self.before_prune()
                return receipts.prune_completions(self.home, kwargs['completions'])
            raise AssertionError(operation)
        self.service.provider.native_settings = native

    def test_old_saved_task_proof_moves_before_unlink_and_survives_reload(self):
        def committed():
            import sqlite3, json
            connection = sqlite3.connect(self.service.db.execute('PRAGMA database_list').fetchone()[2])
            try:
                row = connection.execute('SELECT data FROM tasks WHERE id=?', (self.task['id'],)).fetchone()
                self.assertEqual(json.loads(row[0])['_native_job_completion_receipt'], self.expected)
            finally:
                connection.close()
        self.before_prune = committed
        self.service.refresh_native_routines()
        self.assertFalse(receipts.receipt_path(self.home, self.identity).exists())
        stored = self.service._get('tasks', self.task['id'])
        proof = self.service.native_routine_completion_input(stored)
        self.assertEqual(proof, dict(self.expected, native_confirmed=True))
        # Even a deleted routine and removed event history cannot erase native proof.
        with self.service.transaction():
            self.service.db.execute('DELETE FROM events')
        self.assertTrue(receipts.completed(self.home, 'job', 'owner', SimpleNamespace(get_job=lambda _: None), proof))
        for changes in ({'status': 'cancelled'}, {'status': 'failed'}, {'_native_job_finished': False}):
            self.assertEqual(self.service.native_routine_completion_input(dict(stored, **changes)), {})
        self.assertFalse(receipts.completed(self.home, 'job', 'lost', SimpleNamespace(get_job=lambda _: None), proof))

    def test_unowned_receipts_removed_unrelated_and_later_receipts_preserved(self):
        orphan = dict(job_id='deleted-job', owner='gone', success=True)
        orphan_id = receipts.completion_id('deleted-job', 'gone')
        receipts.save(self.home, orphan_id, orphan)
        self.candidates[orphan_id] = orphan
        receipts.save(self.home, 'job', dict(phase='claimed'))
        receipts.save(self.home, 'completed-unrelated', dict(unrelated=True))
        later_id = receipts.completion_id('job', 'later')
        self.after_snapshot = lambda: receipts.save(self.home, later_id, dict(job_id='job', owner='later', success=True))
        self.service.refresh_native_routines()
        self.assertFalse(receipts.receipt_path(self.home, orphan_id).exists())
        self.assertEqual(receipts.read(self.home, 'job'), dict(phase='claimed'))
        self.assertEqual(receipts.read(self.home, 'completed-unrelated'), dict(unrelated=True))
        self.assertTrue(receipts.receipt_path(self.home, later_id).exists())

    def test_inflight_request_keeps_receipt_until_worker_exits(self):
        old_request = NativeBridge({'task': copy.deepcopy(self.task)}, lambda *a: None, lambda *a: None)
        self.service.running[self.task['id']] = SimpleNamespace(is_alive=lambda: True)
        self.service.refresh_native_routines()
        self.assertEqual(receipts.read(self.home, self.identity), self.expected)
        jobs = SimpleNamespace(get_job=lambda _: {'_coconut_completion': dict(job_id='job', owner='next', success=True)})
        self.assertTrue(receipts.completed(self.home, 'job', 'owner', jobs,
                                          old_request.request['task'].get('_native_job_completion')))
        self.service.running.clear()
        self.service.refresh_native_routines()
        self.assertFalse(receipts.receipt_path(self.home, self.identity).exists())
        proof = self.service.native_routine_completion_input(self.service._get('tasks', self.task['id']))
        self.assertTrue(receipts.completed(self.home, 'job', 'owner', jobs, proof))

    def test_failed_task_commit_keeps_native_proof_for_retry(self):
        original = self.service._put
        def fail(table, value):
            if table == 'tasks' and value.get('_native_job_completion_receipt'):
                raise OSError('task persistence failed')
            return original(table, value)
        with patch.object(self.service, '_put', side_effect=fail):
            with self.assertRaises(OSError): self.service.refresh_native_routines()
        self.assertEqual(receipts.read(self.home, self.identity), self.expected)
        self.assertNotIn('_native_job_completion_receipt', self.service._get('tasks', self.task['id']))
        self.service.refresh_native_routines()
        self.assertFalse(receipts.receipt_path(self.home, self.identity).exists())

    def test_cleanup_retry_is_idempotent(self):
        self.before_prune = lambda: (_ for _ in ()).throw(OSError('cleanup interrupted'))
        with self.assertRaises(OSError): self.service.refresh_native_routines()
        self.assertEqual(receipts.read(self.home, self.identity), self.expected)
        self.assertEqual(self.service._get('tasks', self.task['id'])['_native_job_completion_receipt'], self.expected)
        self.before_prune = lambda: None
        self.service.refresh_native_routines()
        self.service.refresh_native_routines()
        self.assertFalse(receipts.receipt_path(self.home, self.identity).exists())

    def test_frequent_long_lived_routine_keeps_proof_with_each_task(self):
        from uuid import uuid4
        original_id = self.task['id']
        for index in range(40):
            task = copy.deepcopy(self.task)
            task['id'] = str(uuid4())
            task['_native_job']['fire_claim']['by'] = f'owner-{index}'
            proof = dict(job_id='job', owner=f'owner-{index}', success=True)
            identity = receipts.completion_id('job', proof['owner'])
            receipts.save(self.home, identity, proof)
            self.candidates[identity] = proof
            with self.service.transaction(): self.service._put('tasks', task)
            self.service.refresh_native_routines()
            self.assertEqual(list((self.home.parent / 'runs').glob('completed-*.json')), [])
            self.candidates = {}
        old = self.service._get('tasks', original_id)
        self.assertEqual(self.service.native_routine_completion_input(old), dict(self.expected, native_confirmed=True))


if __name__ == '__main__':
    unittest.main()
