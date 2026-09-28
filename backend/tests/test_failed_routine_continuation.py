"""Failed occurrences require both native outcome and authenticated continuation."""
import copy
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid5

from coconut.native_bridge import NativeBridge, TurnPaused
from coconut import native_routine_receipts as receipts
import test_completed_routine_continuation as completion_fixtures
import test_native_work_continuation as continuation_fixtures
import test_worker_model_settings as worker_fixtures


class FailedRoutineContinuationTests(unittest.TestCase):
    setUp = continuation_fixtures.NativeWorkContinuationTests.setUp
    tearDown = continuation_fixtures.NativeWorkContinuationTests.tearDown
    write = continuation_fixtures.NativeWorkContinuationTests.write
    prepare = continuation_fixtures.NativeWorkContinuationTests.prepare
    native = continuation_fixtures.NativeWorkContinuationTests.native
    snapshot_result = continuation_fixtures.NativeWorkContinuationTests.snapshot_result
    resume = continuation_fixtures.NativeWorkContinuationTests.resume

    def occurrence(self, job=None):
        owner, source = self.native()
        job = copy.deepcopy(job or dict(id='job', fire_claim={'by': 'owner'}))
        owner.update(status='working', _native_job=job,
                     routine_id=str(uuid5(UUID(owner['bot_id']), 'cron:' + job['id'])))
        self.service._put('tasks', owner)
        self.service._event('routine.triggered', dict(routine_id=owner['routine_id'], task_id=owner['id']),
                            owner['bot_id'], owner['id'])
        self.service.emit(owner['id'], 'failed', dict(summary='Original occurrence failed.'))
        # This isolated occurrence has no native return transport. Discharge its
        # saved result through the service's normal return drain before recovery.
        self.service._drain_returns()
        owner = self.service._get('tasks', owner['id'])
        owner['_native_job_finished'] = True
        self.service._put('tasks', owner)
        return owner, source

    def test_authenticated_requester_retains_failed_outcome_and_execution(self):
        owner, source = self.occurrence()
        self.assertEqual(self.service.native_routine_completion_input(owner), {})
        receipt = self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        proof = self.service.native_routine_completion_input(continued)
        self.assertEqual(proof['completion'], {})
        self.assertEqual(proof['owner'], 'owner')
        self.assertEqual(proof['continuation']['task_id'], owner['id'])
        self.assertEqual(proof['continuation']['turn_id'], continued['_native_turn_id'])
        self.assertEqual(proof['continuation']['native_task_id'], receipt['native_task_id'])
        self.assertEqual(continued['_execution_session_id'], owner['_execution_session_id'])
        self.assertEqual(continued['_native_job'], owner['_native_job'])
        self.assertTrue(continued['_native_job_finished'])
        self.assertEqual(self.resume(source), receipt)
        self.assertEqual(self.service.native_routine_completion_input(self.service._get('tasks', owner['id'])), proof)

    def test_missing_foreign_or_wrong_target_delivery_does_not_authorize(self):
        owner, source = self.occurrence()
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        deliveries = [d for d in self.service._all('deliveries') if d.get('receipt', {}).get('native_task_id') == 't_existing']
        self.assertEqual(len(deliveries), 1)
        delivery = deliveries[0]
        for change in (dict(target_bot_id='foreign'), dict(source_task_id='missing'),
                       dict(receipt=dict(delivery['receipt'], task_id=source['id'])),
                       dict(receipt=dict(delivery['receipt'], native_task_id='foreign'))):
            with self.subTest(change=change):
                self.service._put('deliveries', delivery | change)
                self.assertEqual(self.service.native_routine_completion_input(continued), {})
        self.service.db.execute('DELETE FROM deliveries WHERE id=?', (delivery['id'],))
        self.assertEqual(self.service.native_routine_completion_input(continued), {})

    def test_native_failure_does_not_require_a_coconut_failed_event(self):
        owner, source = self.occurrence()
        self.resume(source)
        # The observed worker failure had no task.failed event, then its silent
        # TurnPaused exits produced misleading completed events.
        self.service.db.execute("DELETE FROM events WHERE task_id=? AND type='task.updated' AND json_extract(data,'$.status')='failed'",
                                (owner['id'],))
        continued = self.service._get('tasks', owner['id'])
        self.service._event('task.updated', dict(continued, status='completed'), owner['bot_id'], owner['id'])
        proof = self.service.native_routine_completion_input(continued)
        self.assertEqual(proof['continuation']['task_id'], owner['id'])
        native = FailedClaimTests()
        native.setUp()
        try:
            native.request['success'] = False
            self.assertTrue(native.finish()['ok'])
            bridge = NativeBridge({'task': dict(continued, _native_job_completion=proof)}, None, None)
            self.assertEqual(native.run_tool(bridge), 'executed')
            self.assertFalse(native.job['_coconut_completion']['success'])
        finally:
            native.doCleanups()

    def test_later_failure_requires_another_authenticated_delivery(self):
        owner, source = self.occurrence()
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.service._event('task.updated', dict(continued, status='failed'), owner['bot_id'], owner['id'])
        self.assertEqual(self.service.native_routine_completion_input(continued), {})

    def test_failed_completion_migrates_without_becoming_success(self):
        owner, source = self.occurrence()
        self.resume(source)
        expected = dict(job_id='job', owner='owner', success=False)
        identity = receipts.completion_id('job', 'owner')
        calls = []
        def native(operation, bots, **arguments):
            calls.append(operation)
            if operation == 'routines_read':
                return dict(routines=[], completions={owner['bot_id']: {identity: expected}})
            self.assertEqual(operation, 'routine_prune_completions')
            self.assertEqual(self.service._get('tasks', owner['id'])['_native_job_completion_receipt'], expected)
            return dict(ok=True)
        self.service.provider.native_settings = native
        self.service.refresh_native_routines()
        stored = self.service._get('tasks', owner['id'])
        proof = self.service.native_routine_completion_input(stored)
        self.assertEqual(proof['completion'], dict(expected, native_confirmed=True))
        self.assertEqual(calls, ['routines_read', 'routine_prune_completions'])

    def test_same_owner_and_reloaded_delivery_are_supported(self):
        from coconut.service import Service
        owner, source = self.occurrence()
        source['bot_id'] = owner['bot_id']
        self.service._put('tasks', source)
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        proof = self.service.native_routine_completion_input(continued)
        self.assertTrue(proof['continuation']['delivery_id'])
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.assertEqual(self.service.native_routine_completion_input(continued), proof)

    def test_real_pending_decision_and_foreign_requester_stay_rejected(self):
        from coconut.service import ServiceError
        owner, source = self.occurrence()
        self.service._put('decisions', dict(id='pause', task_id=owner['id'], status='pending'))
        with self.assertRaisesRegex(ServiceError, 'decision'):
            self.resume(source)
        self.service.db.execute("DELETE FROM decisions WHERE id='pause'")
        source['bot_id'] = self.write('/v1/bots', {'name': 'Foreign'})['id']
        self.service._put('tasks', source)
        with self.assertRaisesRegex(ServiceError, 'Only the owner'):
            self.resume(source)
        self.assertEqual(self.service.native_routine_completion_input(owner), {})


class FailedClaimTests(unittest.TestCase):
    setUp = completion_fixtures.CompletedRoutineTests.setUp
    get_job = completion_fixtures.CompletedRoutineTests.get_job
    _hermes_now = completion_fixtures.CompletedRoutineTests._hermes_now
    _under_fire_fence = completion_fixtures.CompletedRoutineTests._under_fire_fence
    _with_job = completion_fixtures.CompletedRoutineTests._with_job
    save_job_output = completion_fixtures.CompletedRoutineTests.save_job_output
    save_jobs = completion_fixtures.CompletedRoutineTests.save_jobs
    _record_run_outcome = completion_fixtures.CompletedRoutineTests._record_run_outcome
    _advance_after_run = completion_fixtures.CompletedRoutineTests._advance_after_run
    finish = completion_fixtures.CompletedRoutineTests.finish
    fire_claim_fence = completion_fixtures.CompletedRoutineTests.fire_claim_fence
    bridge = completion_fixtures.CompletedRoutineTests.bridge
    run_tool = completion_fixtures.CompletedRoutineTests.run_tool

    @contextmanager
    def _fire_job_lock(self, identity):
        self.fenced = True
        try:
            yield True
        finally:
            self.fenced = False

    def failed(self):
        bridge = self.bridge()
        self.request['success'] = False
        self.assertTrue(self.finish()['ok'])
        bridge.request['task'].update(_native_turn_id='continued-input', _native_outcome_id='t_existing',
            _native_job_completion=dict(job_id='job', owner='owner', completion={},
                continuation=dict(task_id='original-execution', turn_id='continued-input',
                                  native_task_id='t_existing', delivery_id='persisted-delivery')))
        return bridge

    def test_failed_occurrence_continues_without_refiring_or_rewriting_result(self):
        bridge = self.failed()
        before = copy.deepcopy(self.job), self.writes, copy.deepcopy(bridge.request)
        self.assertEqual(self.run_tool(bridge), 'executed')
        self.assertEqual((self.job, self.writes, bridge.request), before)
        self.assertFalse(self.job['_coconut_completion']['success'])
        self.assertEqual(self.job['repeat']['completed'], 1)

    def test_failed_proof_does_not_authorize_other_task_turn_or_native_outcome(self):
        for field in ('id', '_native_turn_id', '_native_outcome_id'):
            with self.subTest(field=field):
                bridge = self.failed()
                bridge.request['task'][field] = 'foreign'
                with self.assertRaises(TurnPaused): self.run_tool(bridge)

    def test_native_fence_failure_rejects_even_exact_completion(self):
        from contextlib import nullcontext
        bridge = self.failed()
        with patch.object(self, '_fire_job_lock', return_value=nullcontext(False)):
            with self.assertRaises(TurnPaused): self.run_tool(bridge)

    def test_failed_recovery_releases_native_locks_before_service_receipts(self):
        import fcntl
        bridge = self.failed()
        original = self.get_job
        def get_job(identity):
            # completed() also reads the snapshot before the recovery check.
            if self.fenced:
                self.checked_under_fence = True
            return original(identity)
        self.checked_under_fence = False
        modules = {'cron.jobs': self, 'hermes_constants': SimpleNamespace(get_hermes_home=lambda: self.home)}
        with patch.dict('sys.modules', modules), patch.object(self, 'get_job', get_job), bridge.claim():
            self.assertTrue(self.checked_under_fence)
            self.assertFalse(self.fenced)
            with receipts.receipt_path(self.home, 'job').with_suffix('.lock').open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)

    def test_missing_continuation_input_and_foreign_completion_reject(self):
        bridge = self.failed()
        for field in ('turn_id', 'task_id', 'native_task_id', 'delivery_id'):
            with self.subTest(field=field):
                wrong = copy.deepcopy(bridge.request)
                wrong['task']['_native_job_completion']['continuation'].pop(field)
                with self.assertRaises(TurnPaused): self.run_tool(NativeBridge(wrong, None, None))
        proof = bridge.request['task']['_native_job_completion']
        proof['owner'] = 'foreign'
        with self.assertRaises(TurnPaused): self.run_tool(bridge)

    def test_failed_result_is_retained_but_does_not_grant_success(self):
        bridge = self.failed()
        expected = dict(job_id='job', owner='owner', success=False)
        candidates = receipts.completion_receipts(self.home)
        self.assertEqual(candidates, {receipts.completion_id('job', 'owner'): expected})
        self.assertFalse(receipts.completed(self.home, 'job', 'owner', self))
        self.job['_coconut_completion'] = dict(job_id='job', owner='later', success=True)
        self.assertEqual(self.run_tool(bridge), 'executed')
        receipts.prune_completions(self.home, candidates)
        bridge.request['task']['_native_job_completion']['completion'] = dict(expected, native_confirmed=True)
        self.assertEqual(self.run_tool(bridge), 'executed')
        self.assertFalse(receipts.completed(self.home, 'job', 'owner', self))

    def test_unknown_completion_and_active_other_occurrence_remain_fenced(self):
        bridge = self.failed()
        self.job['fire_claim'] = {'by': 'active-other-owner'}
        with self.assertRaises(TurnPaused): self.run_tool(bridge)
        self.job['fire_claim'] = None
        self.job.pop('_coconut_completion')
        receipts.receipt_path(self.home, receipts.completion_id('job', 'owner')).unlink(missing_ok=True)
        with self.assertRaises(TurnPaused): self.run_tool(bridge)


class PausedWorkerTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerModelProcessTests.setUp
    tearDown = worker_fixtures.WorkerModelProcessTests.tearDown
    run_worker = worker_fixtures.WorkerModelProcessTests.run_worker

    def paused_agent(self, reason):
        agent = self.source / 'run_agent.py'
        source = agent.read_text()
        source = source.replace("kwargs['stream_callback']('Ready.')", reason)
        agent.write_text(source)

    def test_unexplained_pause_emits_failed_lifecycle(self):
        self.paused_agent("from native_bridge import TurnPaused; raise TurnPaused('private diagnostic')")
        _, events, _, _ = self.run_worker('start', effort=None, extract=False)
        failures = [e['data'] for e in events if e['type'] == 'failed']
        self.assertEqual(len(failures), 1, events)
        self.assertEqual(failures[0]['error_code'], 'native_turn_failed')
        self.assertNotIn('private diagnostic', str(events))
        self.assertFalse(any(e['type'] == 'completed' for e in events))

    def test_unexplained_pause_cannot_become_ready_to_review(self):
        from coconut.providers import FixtureProvider
        from coconut.service import Service
        from uuid import uuid4
        self.paused_agent("from native_bridge import TurnPaused; raise TurnPaused()")
        service = Service(self.root / 'service', FixtureProvider(self.root / 'service'), start_workers=False)
        self.addCleanup(service.close)
        bot = service.mutate('POST', '/v1/bots', {'name': 'Worker'}, str(uuid4()))
        task = service.mutate('POST', '/v1/bots/' + bot['id'] + '/messages', {'text': 'Check the file'}, str(uuid4()))
        def worker(task, bot, history, emit):
            _, events, _, _ = self.run_worker('start', effort=None, extract=False)
            for event in events:
                if event['type'] in ('failed', 'completed'):
                    emit(event['type'], event['data'])
        service.provider.start_task = worker
        service._run_task(task['id'])
        stored = service._get('tasks', task['id'])
        self.assertEqual(stored['status'], 'failed', stored)
        self.assertEqual(stored['error_code'], 'native_turn_failed')
        self.assertNotEqual(stored['summary'], 'Ready to review.')

    def test_real_bridge_decision_pause_remains_a_pause(self):
        self.paused_agent("bridge = next(cell.cell_contents for cell in self.tool_start_callback.__closure__ "
                          "if hasattr(cell.cell_contents, 'paused')); "
                          "bridge.pause('native_approval', {'title': 'Allow this action?'})")
        _, events, _, _ = self.run_worker('start', effort=None, extract=False)
        self.assertEqual(len([e for e in events if e['type'] == 'native_approval']), 1, events)
        self.assertFalse(any(e['type'] in ('failed', 'completed') for e in events), events)


if __name__ == '__main__':
    unittest.main()
