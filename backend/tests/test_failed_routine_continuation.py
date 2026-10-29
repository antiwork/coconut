"""Failed occurrences require both native outcome and authenticated continuation."""
import copy
import json
import threading
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

    def reconcile_before_continuation(self, after_native_finish):
        from coconut.service import ServiceError
        native = FailedClaimTests()
        native.setUp()
        self.addCleanup(native.doCleanups)
        owner, source = self.occurrence(native.job)
        owner['_native_job_finished'] = False
        self.service._put('tasks', owner)
        entered, release = threading.Event(), threading.Event()
        errors, finishes = [], []

        def settings(operation, bots, **arguments):
            self.assertEqual(operation, 'routine_finish')
            finishes.append(arguments)
            def finish():
                return receipts.finish(native.home, arguments['native_id'], native,
                    owner=arguments['owner'], success=arguments['success'],
                    output=arguments['output'], failure=arguments['failure'])
            result = finish() if after_native_finish else None
            entered.set()
            if not release.wait(5):
                raise AssertionError('Continuation did not release reconciliation')
            return result if after_native_finish else finish()

        self.service.provider.native_settings = settings
        self.service.refresh_native_routines = lambda: None
        def tick():
            try:
                self.service.tick_native_routines()
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=tick)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(ServiceError, 'original routine occurrence'):
                self.resume(source)
            self.assertEqual(self.service._get('tasks', owner['id']), owner)
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.service._native_cron_error)
        closed = self.service._get('tasks', owner['id'])
        self.assertTrue(closed['_native_job_finished'])
        self.assertEqual(closed['status'], 'failed')
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        proof = self.service.native_routine_completion_input(continued)
        bridge = NativeBridge({'task': dict(continued, _native_job_completion=proof)}, None, None)
        self.assertEqual(native.run_tool(bridge), 'executed')
        with self.service.transaction():
            continued['status'] = 'working'
            self.service._put('tasks', continued)
        self.service.emit(owner['id'], 'completed', dict(summary='Recovery succeeded.'))
        self.service._native_cron_next = 0
        self.service.tick_native_routines()
        self.assertEqual(len(finishes), 1)
        self.assertFalse(finishes[0]['success'])
        self.assertEqual(native.job['_coconut_completion'], dict(job_id='job', owner='owner', success=False))
        self.assertEqual(native.job['repeat']['completed'], 1)
        self.assertEqual(native.outputs, ['Original occurrence failed.'])

    def test_continuation_waits_for_captured_failure_to_reach_native_finish(self):
        self.reconcile_before_continuation(after_native_finish=False)

    def test_continuation_waits_for_native_failure_to_commit_locally(self):
        self.reconcile_before_continuation(after_native_finish=True)

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

    def finished_native(self):
        native = FailedClaimTests()
        native.setUp()
        self.addCleanup(native.doCleanups)
        owner, source = self.occurrence(native.job)
        native.request['success'] = False
        self.assertTrue(native.finish()['ok'])
        return native, owner, source

    def continued_bridge(self, identity):
        task = self.service._get('tasks', identity)
        return NativeBridge({'task': dict(task, _native_job_completion=
            self.service.native_routine_completion_input(task))}, None, None)

    def between_tools(self, route):
        native, owner, source = self.finished_native()
        self.resume(source)
        # The pass of the service claims only a claim of its own that it recovers. The Hermes gateway fires the rest.
        routine = dict(id=owner['routine_id'], bot_id=owner['bot_id'], name='Next occurrence',
                       enabled=True, _native_id='job', next_run_at='2000-01-01T00:00:00Z',
                       _recover_claim=route == 'scheduler')
        self.service._put('routines', routine)
        self.service.refresh_native_routines = lambda: None
        claims, dispatches = [], []
        def settings(operation, bots, **kw):
            self.assertEqual(operation, 'routine_claim')
            return receipts.claim(native.home, kw['native_id'], native, manual=kw['manual'])
        self.service.provider.native_settings = settings
        def claim(identity, **kw):
            claims.append(kw)
            native.job['fire_claim'] = {'by': 'next-owner'}
            return copy.deepcopy(native.job)
        def dispatch(identity):
            dispatches.append(identity)
            return True
        caller = self.write('/v1/bots/' + owner['bot_id'] + '/messages', {'text': 'Run routine', 'mode': 'queue'})
        caller = self.service._get('tasks', caller['id'])
        caller['status'] = 'working'
        self.service._put('tasks', caller)
        def attempt():
            if route == 'scheduler':
                self.service._native_cron_next = 0
                self.service.tick_native_routines()
                self.assertFalse(self.service._native_cron_error)
            elif route == 'manual':
                return self.write('/v1/routines/' + routine['id'] + '/run', {})
            else:
                result = self.service.emit(caller['id'], 'native_call',
                    dict(name='cronjob_manage', arguments=dict(action='run', job_id='job')))
                return json.loads(result['routine_result'])
        with patch.object(native, 'claim_job_for_fire', claim, create=True), \
                patch.object(native, 'claim_dispatch', dispatch, create=True):
            for stage in ('queued', 'working', 'between tools'):
                with self.subTest(stage=stage, route=route):
                    task = self.service._get('tasks', owner['id'])
                    if stage == 'working':
                        task['status'] = 'working'
                        self.service._put('tasks', task)
                    self.assertEqual(receipts.read(native.home, receipts.continuation_id('job')), {})
                    result = attempt()
                    self.assertEqual(claims, [], 'A supported claim stole the admitted continuation')
                    self.assertEqual(dispatches, [])
                    if result:
                        self.assertEqual(result.get('task_id', result.get('id')), owner['id'])
                    if stage != 'queued':
                        self.assertEqual(native.run_tool(self.continued_bridge(owner['id'])), 'executed')
            self.service.emit(owner['id'], 'completed', dict(summary='Recovery succeeded.'))
            attempt()
            self.assertEqual(len(claims), 1)
            self.assertEqual(dispatches, ['job'])
            self.assertFalse(native.job['_coconut_completion']['success'])
            self.assertEqual(native.job['repeat']['completed'], 1)

    def test_a_recovered_claim_cannot_claim_before_or_between_recovery_tools(self):
        self.between_tools('scheduler')

    def test_manual_run_cannot_claim_before_or_between_recovery_tools(self):
        self.between_tools('manual')

    def test_manual_tool_cannot_claim_before_or_between_recovery_tools(self):
        self.between_tools('tool')

    def test_native_retry_cannot_borrow_the_completed_recovery_delivery(self):
        native, owner, source = self.finished_native()
        original = self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        proof = self.service.native_routine_completion_input(continued)
        self.assertEqual(native.run_tool(self.continued_bridge(owner['id'])), 'executed')
        continued['status'] = 'working'
        self.service._put('tasks', continued)
        self.service.emit(owner['id'], 'completed', dict(summary='Recovery succeeded.'))
        completed = self.service._get('tasks', owner['id'])
        with self.service.transaction():
            self.service._native_retry(completed, dict(status='ready'))
        retried = self.service._get('tasks', owner['id'])
        self.assertEqual(retried['status'], 'queued')
        self.assertNotEqual(retried['_native_turn_id'], proof['continuation']['turn_id'])
        self.assertEqual(retried['_bot_messages'], continued['_bot_messages'])
        self.assertEqual(self.resume(source), original)  # Idempotency cannot authorize the new input.
        self.assertEqual(self.service.native_routine_completion_input(retried), {})
        with self.assertRaises(TurnPaused):
            native.run_tool(self.continued_bridge(owner['id']))
        self.assertIsNone(self.service._active_routine_task(owner['routine_id']))
        # A new explicit delivery, through the same supported route, authorizes
        # a fresh input. Historical delivery and failed native outcome stay intact.
        self.service._drain_returns()
        self.service.kanban.tasks[owner['id']].update(status='ready')
        self.service.emit(source['id'], 'work_continue', dict(task_id='t_existing',
            message='Authorize another acceptance check after the native retry.'))
        fresh = self.service.native_routine_completion_input(self.service._get('tasks', owner['id']))
        self.assertNotEqual(fresh['continuation']['delivery_id'], proof['continuation']['delivery_id'])
        self.assertNotEqual(fresh['continuation']['turn_id'], retried['_native_turn_id'])
        self.assertEqual(native.run_tool(self.continued_bridge(owner['id'])), 'executed')
        self.assertFalse(native.job['_coconut_completion']['success'])

    def test_legacy_unbound_delivery_requires_new_explicit_continuation(self):
        native, owner, source = self.finished_native()
        self.resume(source)
        task = self.service._get('tasks', owner['id'])
        proof = self.service.native_routine_completion_input(task)
        delivery = self.service._get('deliveries', proof['continuation']['delivery_id'])
        delivery.pop('turn_id', None)
        self.service._put('deliveries', delivery)
        self.assertEqual(self.service.native_routine_completion_input(task), {})
        with self.assertRaises(TurnPaused):
            native.run_tool(self.continued_bridge(owner['id']))
        self.service.kanban.tasks[owner['id']].update(status='ready')
        self.service.emit(source['id'], 'work_continue', dict(task_id='t_existing',
            message='Explicitly authorize this saved legacy continuation.'))
        self.assertEqual(native.run_tool(self.continued_bridge(owner['id'])), 'executed')

    def test_missing_foreign_or_wrong_target_delivery_does_not_authorize(self):
        owner, source = self.occurrence()
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        deliveries = [d for d in self.service._all('deliveries') if d.get('receipt', {}).get('native_task_id') == 't_existing']
        self.assertEqual(len(deliveries), 1)
        delivery = deliveries[0]
        for change in (dict(turn_id='foreign'), dict(target_bot_id='foreign'), dict(source_task_id='missing'),
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
        stored = self.service._get('tasks', owner['id'])
        self.assertEqual(self.service.native_routine_completion_input(stored), proof)
        delivery = self.service._get('deliveries', proof['continuation']['delivery_id'])
        self.assertEqual(delivery['turn_id'], stored['_native_turn_id'])
        self.assertEqual(self.service._active_routine_task(stored['routine_id'])['id'], stored['id'])

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

    def reserve_during_service_claim(self, manual):
        import fcntl
        from coconut.providers import FixtureProvider
        from coconut.service import Service, ServiceError
        from uuid import uuid4
        bridge = self.failed()
        service = Service(self.home / 'service', FixtureProvider(self.home / 'service'), start_workers=False)
        self.addCleanup(service.close)
        bot = service.mutate('POST', '/v1/bots', {'name': 'Routine'}, str(uuid4()))
        task = service.mutate('POST', '/v1/bots/' + bot['id'] + '/messages', {'text': 'Continue'}, str(uuid4()))
        task = service._get('tasks', task['id'])
        task.update(bridge.request['task'], id=task['id'], status='working', routine_id='routine')
        task['_native_job_completion']['continuation']['task_id'] = task['id']
        service._put('tasks', task)
        bridge.request['task'] = task
        # The pass of the service claims only a claim of its own that it recovers. The Hermes gateway fires the rest.
        routine = dict(id='routine', bot_id=bot['id'], name='Next occurrence', enabled=True,
                       _native_id='job', next_run_at='2000-01-01T00:00:00Z', _recover_claim=not manual)
        service._put('routines', routine)
        service.refresh_native_routines = lambda: None
        service.provider.native_settings = lambda operation, bots, **kw: receipts.claim(
            self.home, kw['native_id'], self, manual=kw['manual'])
        claimed, dispatched, errors = [], [], []
        def claim_job(identity, **kw):
            with native_lock(identity):
                claimed.append(kw)
                self.job['fire_claim'] = {'by': 'next-owner'}
                return copy.deepcopy(self.job)
        def dispatch(identity):
            dispatched.append(identity)
            return True
        @contextmanager
        def native_lock(identity):
            with (self.home / 'native-fire.lock').open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                yield True
        tool_entered, attempt_done, tool_receipt, release = (threading.Event() for _ in range(4))
        def tool():
            try:
                with bridge.claim():
                    tool_entered.set()
                    self.assertTrue(attempt_done.wait(5), 'Claim held the service lock waiting on this tool')
                    # Exercise the actual service receipt while the reservation is held.
                    result = service.emit(task['id'], 'native_call', dict(name='read_file', arguments={'path': 'proof'}))
                    self.assertEqual(result['status'], 'recorded')
                    tool_receipt.set()
                    self.assertTrue(release.wait(5))
            except BaseException as error:
                errors.append(error)
        def attempt():
            try:
                if manual == 'tool':
                    result = service.emit(task['id'], 'native_call',
                        dict(name='cronjob_manage', arguments=dict(action='run', job_id='job')))
                    self.assertFalse(json.loads(result['routine_result'])['success'])
                elif manual:
                    with self.assertRaises(ServiceError):
                        service.mutate('POST', '/v1/routines/routine/run', {}, str(uuid4()))
                else:
                    service.tick_native_routines()
                    self.assertFalse(service._native_cron_error)
            except BaseException as error:
                errors.append(error)
            finally:
                attempt_done.set()
        modules = {'cron.jobs': self, 'hermes_constants': SimpleNamespace(get_hermes_home=lambda: self.home)}
        with patch.dict('sys.modules', modules), patch.object(self, '_fire_job_lock', native_lock), \
                patch.object(self, 'claim_job_for_fire', claim_job, create=True), \
                patch.object(self, 'claim_dispatch', dispatch, create=True):
            worker = threading.Thread(target=tool)
            worker.start()
            contender = None
            try:
                self.assertTrue(tool_entered.wait(5))
                contender = threading.Thread(target=attempt)
                contender.start()
                self.assertTrue(attempt_done.wait(5))
                self.assertTrue(tool_receipt.wait(5))
                self.assertEqual(errors, [])
                self.assertEqual(claimed, [])
                self.assertEqual(dispatched, [])
                self.assertEqual(len(service._all('tasks')), 1)
            finally:
                release.set()
                attempt_done.set()
                worker.join(5)
                if contender:
                    contender.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(receipts.read(self.home, receipts.continuation_id('job')), {})
            # The next intake is now allowed; original failure proof stays immutable.
            if manual:
                queued = service.mutate('POST', '/v1/routines/routine/run', {}, str(uuid4()))
                self.assertEqual(queued['status'], 'queued')
            else:
                service._native_cron_next = 0
                service.tick_native_routines()
                self.assertFalse(service._native_cron_error)
            self.assertEqual(len(claimed), 1)
            self.assertEqual(dispatched, ['job'])
            self.assertEqual(len(service._all('tasks')), 2)
            self.assertFalse(self.job['_coconut_completion']['success'])
            with self.assertRaises(TurnPaused):
                self.run_tool(bridge)

    def test_a_recovered_claim_cannot_dispatch_during_continuation_tool(self):
        self.reserve_during_service_claim(manual=False)

    def test_manual_claim_cannot_dispatch_during_continuation_tool(self):
        self.reserve_during_service_claim(manual=True)

    def test_manual_tool_cannot_dispatch_during_continuation_tool(self):
        self.reserve_during_service_claim(manual='tool')

    def test_parallel_scopes_release_only_their_exact_reservation(self):
        bridge = self.failed()
        modules = {'cron.jobs': self, 'hermes_constants': SimpleNamespace(get_hermes_home=lambda: self.home)}
        key = receipts.continuation_id('job')
        with patch.dict('sys.modules', modules), bridge.claim():
            first = receipts.read(self.home, key)
            self.assertEqual(len(first), 1)
            with bridge.claim():
                self.assertEqual(len(receipts.read(self.home, key)), 2)
            self.assertEqual(receipts.read(self.home, key), first)
            other = copy.deepcopy(bridge.request)
            other['task']['_native_turn_id'] = 'another-input'
            other['task']['_native_job_completion']['continuation']['turn_id'] = 'another-input'
            with self.assertRaises(TurnPaused):
                self.run_tool(NativeBridge(other, None, None))
            self.assertEqual(receipts.read(self.home, key), first)
        self.assertEqual(receipts.read(self.home, key), {})

    def test_interrupted_scope_keeps_next_fire_fenced_across_processes(self):
        import json
        import os
        import subprocess
        import sys
        bridge = self.failed()
        code = """
import json, os, sys
from contextlib import nullcontext
from types import SimpleNamespace
from coconut.native_routine_receipts import continued_failure
from coconut import profiles
sys.modules['profiles'] = profiles
home, proof = json.loads(sys.stdin.read())
jobs = SimpleNamespace(_fire_job_lock=lambda identity: nullcontext(True), get_job=lambda identity: {})
with continued_failure(home, 'job', 'owner', jobs,
        dict(job_id='job', owner='owner', success=False, native_confirmed=True), proof) as admitted:
    assert admitted
    os._exit(0)
"""
        result = subprocess.run([sys.executable, '-c', code], input=json.dumps([
            str(self.home), bridge.request['task']['_native_job_completion']['continuation']]),
            text=True, capture_output=True, env=os.environ.copy(), timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        key = receipts.continuation_id('job')
        interrupted = receipts.read(self.home, key)
        self.assertEqual(len(interrupted), 1)
        self.assertIn('interrupted tool receipts', receipts.issue(self.home, self.job))
        # A restarted bridge can release only its own scope, not the lost one.
        self.assertEqual(self.run_tool(bridge), 'executed')
        self.assertEqual(receipts.read(self.home, key), interrupted)
        for manual in (False, True):
            self.assertIsNone(receipts.claim(self.home, 'job', self, manual=manual)['job'])
        self.assertFalse(self.job['_coconut_completion']['success'])

    def test_exception_releases_reservation_without_changing_failure(self):
        bridge = self.failed()
        before = copy.deepcopy(self.job)
        modules = {'cron.jobs': self, 'hermes_constants': SimpleNamespace(get_hermes_home=lambda: self.home)}
        with patch.dict('sys.modules', modules), self.assertRaisesRegex(RuntimeError, 'tool failed'):
            with bridge.claim():
                raise RuntimeError('tool failed')
        self.assertEqual(receipts.read(self.home, receipts.continuation_id('job')), {})
        self.assertEqual(self.job, before)

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
