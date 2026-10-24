import json
import copy
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from uuid import uuid4

from coconut import profiles
from coconut import native_routine_receipts as receipts
from coconut import native_routines
from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError


class ApprovalRecoveryTests(unittest.TestCase):
    def test_ambiguous_native_write_reconciles_without_repeating_or_overwriting(self):
        with tempfile.TemporaryDirectory() as root:
            service = Service(root, FixtureProvider(root), start_workers=False)
            self.addCleanup(service.close)
            state = {'default': 'off', 'modes': [{'id': 'off'}, {'id': 'manual'}]}
            writes = []
            def native(operation='read', **values):
                if operation == 'set_default_approval':
                    writes.append(values['mode'])
                    state['default'] = values['mode']
                    raise ConnectionError('Lost response after write.')
                return {'approvals': dict(state)}
            service.provider.native_settings = native
            key = str(uuid4())
            with self.assertRaises(ServiceError):
                service.mutate('PUT', '/v1/settings/approvals', {'mode': 'manual'}, key)
            result = service.mutate('PUT', '/v1/settings/approvals', {'mode': 'manual'}, key)
            self.assertEqual(result['default'], 'manual')
            state['default'] = 'off'
            service.mutate('PUT', '/v1/settings/approvals', {'mode': 'manual'}, key)
            self.assertEqual(state['default'], 'off')
            self.assertEqual(writes, ['manual'])


class RoutineClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Coconut keeps the receipts beside the profile home, in its own runs folder.
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()
        self.modules = patch.dict('sys.modules', {'profiles': profiles})
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.job = {'id': 'native-job', 'fire_claim': None}
        self.claims = 0
        self.dispatches = 0

    def get_job(self, identity):
        return self.job

    def claim_job_for_fire(self, identity, **values):
        self.claims += 1
        self.job['fire_claim'] = {'by': 'one-owner'}
        return self.job

    def claim_dispatch(self, identity):
        self.dispatches += 1
        return True

    def test_claim_before_database_commit_is_reused_after_restart(self):
        first = receipts.claim(self.home, 'native-job', self)
        second = receipts.claim(self.home, 'native-job', self)
        self.assertEqual(first, second)
        self.assertEqual((self.claims, self.dispatches), (1, 1))

    def test_lost_claim_does_not_dispatch_again(self):
        receipts.claim(self.home, 'native-job', self)
        self.job['fire_claim'] = {'by': 'another-owner'}
        with self.assertRaisesRegex(ValueError, 'lost'):
            receipts.claim(self.home, 'native-job', self)
        self.assertEqual(self.claims, 1)

    def test_crash_during_native_dispatch_does_not_repeat_unknown_operation(self):
        receipts.save(self.home, 'native-job', {'phase': 'dispatching'})
        with self.assertRaisesRegex(ValueError, 'unknown'):
            receipts.claim(self.home, 'native-job', self)
        self.assertEqual((self.claims, self.dispatches), (0, 0))
        self.assertIn('unknown', receipts.issue(self.home, self.job))

    def test_terminal_receipt_allows_a_new_occurrence(self):
        receipts.claim(self.home, 'native-job', self)
        receipts.save(self.home, 'native-job', {'phase': 'finished'})
        receipts.claim(self.home, 'native-job', self)
        self.assertEqual((self.claims, self.dispatches), (2, 2))


class RoutineCompletionTests(unittest.TestCase):
    def test_native_heartbeat_does_not_block_an_active_tool_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            service = Service(root, FixtureProvider(root), start_workers=False)
            self.addCleanup(service.close)
            bot = service.mutate('POST', '/v1/bots', {'name': 'Cron'}, str(uuid4()))
            task = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Check'}, str(uuid4()))
            with service.transaction():
                stored = service._get('tasks', task['id'])
                stored.update(status='working', routine_id='routine', _native_job={'id': 'job', 'fire_claim': {'by': 'owner'}})
                service._put('tasks', stored)
            receipt = service.native_event(task['id'], 'native_call', {'name': 'read_file', 'arguments': {}})
            finished = threading.Event()
            errors, threads = [], []
            def native(*args, **kwargs):
                def complete():
                    try:
                        service.native_event(task['id'], 'native_result', {'id': receipt['id'], 'result': 'done'})
                        finished.set()
                    except Exception as error:
                        errors.append(error)
                thread = threading.Thread(target=complete)
                threads.append(thread)
                thread.start()
                return {'ok': finished.wait(1)}
            service.provider.native_settings = native
            service.refresh_native_routines = lambda: None
            service.tick_native_routines()
            for thread in threads: thread.join(timeout=2)
            self.assertTrue(finished.is_set())
            self.assertEqual(errors, [])
            self.assertEqual(service._get('tasks', task['id'])['status'], 'working')

    def test_finishing_a_claim_does_not_refire_its_stale_projection(self):
        with tempfile.TemporaryDirectory() as root:
            service = Service(root, FixtureProvider(root), start_workers=False)
            self.addCleanup(service.close)
            bot = service.mutate('POST', '/v1/bots', {'name': 'Cron'}, str(uuid4()))
            task = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Check', 'mode': 'queue'}, str(uuid4()))
            identity = str(uuid4())
            with service.transaction():
                service._put('routines', dict(id=identity, bot_id=bot['id'], name='Cron', enabled=True,
                             next_run_at='2099-01-01T00:00:00Z', _recover_claim=True, _native_id='job'))
                task = service._get('tasks', task['id'])
                task.update(status='completed', routine_id=identity, _native_job={'id': 'job', 'fire_claim': {'by': 'owner'}})
                service._put('tasks', task)
                service._message(bot['id'], 'Actual native output.', task_id=task['id'], role='assistant')
            calls = []
            def native(operation='read', *args, **values):
                calls.append((operation, values))
                if operation == 'routine_claim':
                    self.fail('The stale projection must not trigger another native claim.')
                return {'ok': True}
            service.provider.native_settings = native
            service.refresh_native_routines = lambda: None
            service.tick_native_routines()
            self.assertEqual([name for name, _ in calls], ['routine_finish'])
            self.assertEqual(calls[0][1]['output'], 'Actual native output.')
            self.assertTrue(service._get('tasks', task['id'])['_native_job_finished'])

    def test_unconfirmed_completion_remains_available_for_retry(self):
        with tempfile.TemporaryDirectory() as root:
            service = Service(root, FixtureProvider(root), start_workers=False)
            self.addCleanup(service.close)
            bot = service.mutate('POST', '/v1/bots', {'name': 'Cron'}, str(uuid4()))
            task = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Check'}, str(uuid4()))
            with service.transaction():
                stored = service._get('tasks', task['id'])
                stored.update(status='completed', routine_id='routine', _native_job={'id': 'job', 'fire_claim': {'by': 'owner'}})
                service._put('tasks', stored)
            results = iter([{'ok': False}, {'ok': True}])
            service.provider.native_settings = lambda *args, **kwargs: next(results)
            service.refresh_native_routines = lambda: None
            service.tick_native_routines()
            self.assertFalse(service._get('tasks', task['id']).get('_native_job_finished', False))
            service._native_cron_next = 0
            service.tick_native_routines()
            self.assertTrue(service._get('tasks', task['id'])['_native_job_finished'])


class NativeCompletionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Coconut keeps the receipts beside the profile home, in its own runs folder.
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()
        self.job = {'id': 'job', 'fire_claim': {'by': 'owner'}, 'repeat': {'completed': 0}}
        self.outputs, self.writes = [], 0
        self.request = dict(bot_id='bot', native_id='job', owner='owner', success=True, output='Done')
        self.modules = patch.dict('sys.modules', {'profiles': profiles, 'native_routine_receipts': receipts,
                                                 'cron': SimpleNamespace(jobs=self)})
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.scope = patch.object(native_routines, 'profile', lambda home: nullcontext())
        self.scope.start(); self.addCleanup(self.scope.stop)
        receipts.save(self.home, 'job', {'phase': 'claimed', 'job': copy.deepcopy(self.job)})

    def get_job(self, identity): return copy.deepcopy(self.job)
    def _hermes_now(self): return datetime.now(timezone.utc)
    def _under_fire_fence(self, identity, callback): return callback()
    def _with_job(self, identity, callback, missing=False):
        jobs = [copy.deepcopy(self.job)]
        return callback(jobs, 0, jobs[0])

    def save_job_output(self, identity, output): self.outputs.append(output)
    def save_jobs(self, jobs):
        self.job = copy.deepcopy(jobs[0])
        self.writes += 1

    def _record_run_outcome(self, job, success, error, delivery_error, status, now):
        job.update(last_run_at=now, last_status='ok' if success else 'error', fire_claim=None)

    def _advance_after_run(self, job, now): job['repeat']['completed'] += 1

    def mark_job_run(self, identity, success, error=None, expected_fire_owner=None):
        if (self.job.get('fire_claim') or {}).get('by') != expected_fire_owner:
            return False
        job = copy.deepcopy(self.job)
        self._record_run_outcome(job, success, error, None, None, self._hermes_now().isoformat())
        self._advance_after_run(job, '')
        self.save_jobs([job])
        return True

    def finish(self):
        return native_routines.worker('routine_finish', self.request, {'bot': self.home})

    def test_native_completion_reconciles_after_the_local_receipt_write_fails(self):
        with patch.object(receipts, 'save', side_effect=OSError('Interrupted local receipt')):
            with self.assertRaises(OSError): self.finish()
        self.assertTrue(self.finish()['ok'])
        self.assertTrue(self.finish()['ok'])
        self.assertEqual(receipts.read(self.home, 'job')['phase'], 'finished')
        self.assertIsNone(receipts.issue(self.home, self.job))
        self.assertEqual(self.job['repeat']['completed'], 1)
        self.assertEqual(self.writes, 1)
        self.assertEqual(self.outputs, ['Done'])

    def test_another_owner_does_not_confirm_or_write_this_completion(self):
        self.job['fire_claim'] = {'by': 'another-owner'}
        self.assertFalse(self.finish()['ok'])
        self.assertEqual(self.writes, 0)
        self.assertEqual(self.outputs, [])
