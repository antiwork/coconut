"""Completed routine provenance."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from coconut import native_routine_receipts as receipts


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

    def test_inflight_turn_keeps_receipt_until_it_ends(self):
        # The turn that runs holds its own copy of the task.
        held = copy.deepcopy(self.task)
        self.service.running[self.task['id']] = SimpleNamespace(is_alive=lambda: True)
        self.service.refresh_native_routines()
        self.assertEqual(receipts.read(self.home, self.identity), self.expected)
        jobs = SimpleNamespace(get_job=lambda _: {'_coconut_completion': dict(job_id='job', owner='next', success=True)})
        self.assertTrue(receipts.completed(self.home, 'job', 'owner', jobs,
                                          held.get('_native_job_completion')))
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
