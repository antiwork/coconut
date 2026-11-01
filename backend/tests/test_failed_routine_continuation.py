"""A failed routine occurrence continues only with an authenticated continuation."""
import copy
import json
import threading
import unittest
from unittest.mock import patch
from uuid import UUID, uuid5

from coconut import native_routine_receipts as receipts
import test_native_work_continuation as continuation_fixtures
from test_waiting_occurrence_release import NativeJob


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
        native = NativeJob()
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
        self.assertEqual(self.service.native_routine_completion_input(continued)['continuation']['task_id'], owner['id'])
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
        native = NativeJob()
        native.setUp()
        self.addCleanup(native.doCleanups)
        owner, source = self.occurrence(native.job)
        native.request['success'] = False
        self.assertTrue(native.finish()['ok'])
        return native, owner, source

    def continuation(self, identity):
        return self.service.native_routine_completion_input(self.service._get('tasks', identity)).get('continuation')

    def between_tools(self, route):
        native, owner, source = self.finished_native()
        self.resume(source)
        routine = dict(id=owner['routine_id'], bot_id=owner['bot_id'], name='Next occurrence',
                       enabled=True, _native_id='job', next_run_at='2000-01-01T00:00:00Z')
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
            for stage in ('queued', 'working'):
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
                    self.assertEqual(self.continuation(owner['id'])['task_id'], owner['id'])
            self.service.emit(owner['id'], 'completed', dict(summary='Recovery succeeded.'))
            attempt()
            self.assertEqual(len(claims), 1)
            self.assertEqual(dispatches, ['job'])
            self.assertFalse(native.job['_coconut_completion']['success'])
            self.assertEqual(native.job['repeat']['completed'], 1)

    def test_scheduler_cannot_claim_before_or_between_recovery_tools(self):
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
        self.assertTrue(proof['continuation'])
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
        self.service.kanban.tasks[owner['id']].update(status='ready')
        self.service.emit(source['id'], 'work_continue', dict(task_id='t_existing',
            message='Explicitly authorize this saved legacy continuation.'))
        self.assertEqual(self.continuation(owner['id'])['task_id'], owner['id'])

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
        # An observed failure had no task.failed event, and then completed events followed.
        self.service.db.execute("DELETE FROM events WHERE task_id=? AND type='task.updated' AND json_extract(data,'$.status')='failed'",
                                (owner['id'],))
        continued = self.service._get('tasks', owner['id'])
        self.service._event('task.updated', dict(continued, status='completed'), owner['bot_id'], owner['id'])
        proof = self.service.native_routine_completion_input(continued)
        self.assertEqual(proof['continuation']['task_id'], owner['id'])

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


if __name__ == '__main__':
    unittest.main()
