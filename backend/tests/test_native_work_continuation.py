import copy
import os
import threading
import unittest
from unittest.mock import Mock, patch

from coconut.service import ServiceError
import test_targeted_bot_continuation as fixtures


class NativeReviewerRuntimeTests(unittest.TestCase):
    def board(self, status='running'):
        import sqlite3
        from types import SimpleNamespace
        from coconut.kanban_runtime import Board
        board = Board.__new__(Board)
        board.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(board.conn.close)
        board.conn.execute('CREATE TABLE coconut_bindings (external_id TEXT, native_id TEXT, intent TEXT)')
        board.conn.execute('CREATE TABLE coconut_outcomes (native_id TEXT PRIMARY KEY, input_id TEXT)')
        board.conn.execute("INSERT INTO coconut_bindings VALUES ('owner', 'native', 'old-intent')")
        board.conn.execute("INSERT INTO coconut_outcomes VALUES ('native', 'old-input')")
        task = SimpleNamespace(id='native', status=status, current_run_id=47)
        board.kb = Mock(get_task=Mock(return_value=task), _retry_status_for_run=Mock(return_value='review'))
        board.bind = Mock()
        board.dispatch = Mock()
        row = dict(id='owner', native_id='native', input='new-input', previous_input='old-input', state='ready')
        return board, task, row

    def test_running_reviewer_does_not_consume_pending_input(self):
        board, task, row = self.board()
        before = list(board.conn.iterdump())
        board.sync([row])
        self.assertEqual(list(board.conn.iterdump()), before)
        board.bind.assert_not_called()

    def test_answer_reopens_review_when_snapshot_missed_the_pause(self):
        from coconut.kanban_runtime import fingerprint
        board, task, row = self.board('review')
        # A decision resumes the same input. Its review pause can occur between
        # snapshots, leaving the previous ready intent in the binding.
        row['input'] = row['previous_input']
        intent = fingerprint({key: row[key] for key in ('state', 'input')})
        board.conn.execute('UPDATE coconut_bindings SET intent=?', (intent,))
        board.kb.reopen_review_task.return_value = True
        board.sync([row])
        board.kb.reopen_review_task.assert_called_once_with(board.conn, task.id)

    def test_explicit_cancellation_still_archives_running_task(self):
        board, task, row = self.board()
        row['state'] = 'archived'
        board.sync([row])
        board.kb.archive_task.assert_called_once_with(board.conn, task.id)

    def test_reviewer_claim_winning_reopen_does_not_consume_input(self):
        board, task, row = self.board('review')
        def claim(*args):
            task.status = 'running'
            return False
        board.kb.reopen_review_task.side_effect = claim
        before = list(board.conn.iterdump())
        board.sync([row])
        board.kb.reopen_review_task.assert_called_once()
        self.assertEqual(list(board.conn.iterdump()), before)

    def test_reviewer_claim_during_bind_does_not_consume_input(self):
        from types import SimpleNamespace
        board, task, row = self.board('review')
        running = SimpleNamespace(id=task.id, status='running', current_run_id=47)
        board.kb.get_task.side_effect = [task, running]
        before = list(board.conn.iterdump())
        board.sync([row])
        self.assertEqual(list(board.conn.iterdump()), before)

    def test_finish_cannot_end_native_reviewer_run(self):
        board, task, row = self.board()
        for state in ('ready', 'done', 'blocked', 'scheduled', 'review', 'archived'):
            with self.subTest(state=state):
                self.assertFalse(board.finish(task.id, task.current_run_id, state, 'Stale preparation'))
        board.dispatch._record_task_failure.assert_not_called()
        board.kb.schedule_task.assert_not_called()
        board.kb.archive_task.assert_not_called()
        board.kb.request_review.assert_not_called()

    def test_finish_still_recovers_implementation_preparation(self):
        board, task, row = self.board()
        board.kb._retry_status_for_run.return_value = 'ready'
        board.kb.unblock_task.return_value = True
        self.assertTrue(board.finish(task.id, task.current_run_id, 'ready', 'Interrupted preparation'))
        board.kb.schedule_task.assert_called_once_with(board.conn, task.id,
            reason='Interrupted preparation', expected_run_id=task.current_run_id)
        board.kb.unblock_task.assert_called_once_with(board.conn, task.id)


class NativeWorkContinuationTests(unittest.TestCase):
    setUp = fixtures.TargetedBotContinuationTests.setUp
    tearDown = fixtures.TargetedBotContinuationTests.tearDown
    write = fixtures.TargetedBotContinuationTests.write
    prepare = fixtures.TargetedBotContinuationTests.prepare

    def snapshot_result(self):
        return {'tasks': [dict(row, external_id=identity) for identity, row in self.service.kanban.tasks.items()]}

    def native(self):
        owner, source = self.prepare()
        owner.update(status='completed', _native_outcome_id='t_existing')
        self.service._put('tasks', owner)
        self.service.kanban = Mock(tasks={owner['id']: dict(id='t_existing', status='triage')})
        self.service.kanban.call.side_effect = lambda request: self.snapshot_result()
        return owner, source

    def resume(self, source):
        return self.service.emit(source['id'], 'work_continue',
            dict(task_id='t_existing', message='The corrected commit passed review. Finish its acceptance check.'))

    def test_context_identity_resumes_its_original_outcome(self):
        owner, source = self.native()
        self.service._put('contexts', dict(id='context', task_id=owner['id'],
            bot_id=owner['bot_id'], status='completed',
            _kanban_claim={'native_id': 't_context'}))
        self.service.kanban.tasks['context'] = dict(id='t_context', status='done')
        receipt = self.service.emit(source['id'], 'work_continue',
            dict(task_id='t_context', message='Continue the authorized acceptance check.'))
        self.assertEqual(receipt['task_id'], owner['id'])
        self.assertEqual(receipt['native_task_id'], 't_existing')
        self.assertEqual(self.service._get('tasks', owner['id'])['_native_outcome_id'], 't_existing')

    def test_context_identity_preserves_pending_decision(self):
        owner, source = self.native()
        self.service._put('contexts', dict(id='context', task_id=owner['id'],
            bot_id=owner['bot_id'], status='completed',
            _kanban_claim={'native_id': 't_context'}))
        self.service.kanban.tasks['context'] = dict(id='t_context', status='done')
        self.service._put('decisions', dict(id='pending', task_id=owner['id'], status='pending'))
        with self.assertRaisesRegex(ServiceError, 'decision must be answered'):
            self.service.emit(source['id'], 'work_continue',
                dict(task_id='t_context', message='Continue the acceptance check.'))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('decisions', 'pending')['status'], 'pending')

    def test_context_native_claim_prevents_resuming_its_parent(self):
        owner, source = self.native()
        self.service._put('contexts', dict(id='context', task_id=owner['id'],
            bot_id=owner['bot_id'], status='completed',
            _kanban_claim={'native_id': 't_context'}))
        self.service.kanban.tasks['context'] = dict(id='t_context', status='running', current_run_id=42)
        with self.assertRaisesRegex(ServiceError, 'context task to finish'):
            self.service.emit(source['id'], 'work_continue',
                dict(task_id='t_context', message='Continue the acceptance check.'))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def scheduled_native(self):
        owner, source = self.prepare()
        owner.update(status='completed', _native_turn_id='completed-input',
                     _kanban_claim={'native_id': 't_existing', 'input': 'completed-input',
                                    'run_id': 'completed-run'})
        owner.pop('_native_outcome_id', None)
        self.service._put('tasks', owner)
        self.service.kanban = Mock(tasks={owner['id']: dict(
            external_id=owner['id'], id='t_existing', status='scheduled', current_run_id=None, worker_pid=None)})
        self.service.kanban.call.side_effect = lambda request: self.snapshot_result()
        return owner, source

    def ready_native(self):
        owner, source = self.scheduled_native()
        owner['status'] = 'queued'
        self.service._put('tasks', owner)
        self.service.kanban.tasks[owner['id']].update(status='ready', dispatch_blocker='active_pr')
        return owner, source

    def review_native(self):
        owner, source = self.scheduled_native()
        self.service.kanban.tasks[owner['id']].update(
            status='review', outcome_summary='Saved review evidence')
        return owner, source

    def test_review_accepts_requester_input_once_preserving_identity_and_evidence(self):
        owner, source = self.review_native()
        snapshot = copy.deepcopy(self.service.kanban.tasks)
        self.service._put('decisions', dict(id='answered', task_id=owner['id'], status='rejected'))
        receipt = self.resume(source)
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual(receipt['task_id'], owner['id'])
        self.assertEqual(self.resume(source), receipt)
        self.assertEqual(self.service._get('tasks', owner['id']), queued)
        self.assertEqual(self.service.kanban.tasks, snapshot)
        self.assertNotEqual(queued['_native_turn_id'], owner['_native_turn_id'])
        for key in ('id', '_execution_session_id', 'source_bot_id', 'source_task_id', 'thread_id', '_kanban_claim'):
            self.assertEqual(queued[key], owner[key])
        self.assertEqual(queued['_self_recovery']['decisions'][0]['status'], 'rejected')

    def test_review_stale_idle_snapshot_refreshes_external_claim(self):
        owner, source = self.review_native()
        adapter = self.service.kanban
        def call(request):
            self.assertEqual(request, {'op': 'snapshot', 'external_ids': [owner['id']]})
            adapter.tasks[owner['id']].update(status='running', current_run_id=47)
            return self.snapshot_result()
        adapter.call.side_effect = call
        with self.assertRaises(ServiceError):
            self.resume(source)
        adapter.call.assert_called_once()
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_review_claim_after_refresh_is_not_reclaimed_as_preparation(self):
        owner, source = self.review_native()
        self.resume(source)
        adapter = self.service.kanban
        adapter.tasks[owner['id']].update(status='running', current_run_id=47,
            worker_pid=None, claim_source_status='review')
        adapter.call.reset_mock()
        self.service._reconcile_kanban()
        adapter.call.assert_not_called()
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual(queued['status'], 'queued')
        self.assertNotEqual(queued['_native_turn_id'], owner['_kanban_claim']['input'])

    def test_review_accepts_owner_input(self):
        owner, source = self.review_native()
        self.service._put('tasks', dict(source, bot_id=owner['bot_id']))
        self.assertEqual(self.resume(source)['task_id'], owner['id'])

    def test_review_explicit_owner_continues_same_execution(self):
        owner, source = self.review_native()
        owner['_native_outcome_id'] = 't_existing'
        self.service._put('tasks', owner)
        self.assertEqual(self.resume(source)['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['_execution_session_id'], owner['_execution_session_id'])

    def test_review_preserves_explicit_human_work_pause(self):
        owner, source = self.review_native()
        item = dict(id='paused-work', task_id=owner['id'], bot_id=owner['bot_id'], kind='work',
                    title=owner['title'], status='waiting', paused_by_decision_id='human-pause')
        decision = dict(id='human-pause', task_id=owner['id'], status='rejected', decided_by='human')
        self.service._put('artifacts', item)
        self.service._put('decisions', decision)
        with self.assertRaisesRegex(ServiceError, 'paused'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('artifacts', item['id']), item)
        self.assertEqual(self.service._get('decisions', decision['id']), decision)

    def test_review_rejects_native_and_local_workers(self):
        self.ready_native = self.review_native
        self.test_ready_queued_rejects_active_workers()

    def test_review_preserves_decision_dependency_and_authority(self):
        self.ready_native = self.review_native
        self.test_ready_queued_preserves_decision_dependency_and_authority()

    def test_review_preserves_human_pause_and_needs_you(self):
        owner, source = self.review_native()
        for state in ('needs_you', 'paused', 'working', 'queued'):
            changed = dict(owner, status=state)
            self.service._put('tasks', changed)
            with self.subTest(state=state), self.assertRaises(ServiceError):
                self.resume(source)
            self.assertEqual(self.service._get('tasks', owner['id']), changed)

    def test_review_continuation_cannot_race_captured_dispatch_input(self):
        self.ready_native = self.review_native
        self.check_ready_dispatch_race()

    def test_review_dispatch_failure_retains_uncertain_response_fence(self):
        self.ready_native = self.review_native
        self.check_ready_dispatch_race(fail_tick=True)

    def test_review_snapshot_revalidates_worker(self):
        def change(owner, source, peer, items, snapshots):
            snapshots[owner['id']].update(status='review', current_run_id=123)
        owner, _, _, result = self.concurrent_snapshot(missing_owner=True, change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('active work', str(result))
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], owner['status'])

    def test_review_concurrent_distinct_requests_accept_only_one_input(self):
        owner, source = self.review_native()
        barrier = threading.Barrier(3)
        results = []
        def resume(message):
            barrier.wait(5)
            try:
                results.append(self.service.emit(source['id'], 'work_continue',
                    dict(task_id='t_existing', message=message)))
            except ServiceError as error:
                results.append(error)
        threads = [threading.Thread(target=resume, args=(message,)) for message in ('First input', 'Second input')]
        for thread in threads:
            thread.start()
        barrier.wait(5)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, ServiceError) for result in results), 1)
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'queued')

    def test_ready_queued_accepts_requester_input_once(self):
        owner, source = self.ready_native()
        receipt = self.resume(source)
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual(self.resume(source), receipt)
        self.assertEqual(self.service._get('tasks', owner['id']), queued)
        row = next(r for r in self.service._kanban_rows() if r['id'] == owner['id'])
        self.assertEqual(row['previous_input'], 'completed-input')
        self.assertNotEqual(row['input'], row['previous_input'])
        for key in ('id', '_execution_session_id', 'source_bot_id', 'source_task_id', 'thread_id'):
            self.assertEqual(queued[key], owner[key])

    def test_ready_queued_rejects_active_workers(self):
        owner, source = self.ready_native()
        native = self.service.kanban.tasks[owner['id']]
        for field in ('current_run_id', 'worker_pid'):
            native[field] = 123
            with self.subTest(field=field), self.assertRaisesRegex(ServiceError, 'active work'):
                self.resume(source)
            native[field] = None
        self.service.running[owner['id']] = Mock(is_alive=lambda: True)
        with self.assertRaisesRegex(ServiceError, 'active work'):
            self.resume(source)
        self.service.running.clear()
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_ready_queued_preserves_other_respawn_guards(self):
        owner, source = self.ready_native()
        for guard in ('blocker_auth', 'rate_limit_cooldown', 'recent_success'):
            self.service.kanban.tasks[owner['id']]['dispatch_blocker'] = guard
            with self.subTest(guard=guard), self.assertRaisesRegex(ServiceError, 'guarded'):
                self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_ready_queued_preserves_decision_dependency_and_authority(self):
        owner, source = self.ready_native()
        self.service._put('decisions', dict(id='pending', task_id=owner['id'], status='pending'))
        with self.assertRaisesRegex(ServiceError, 'existing decision'):
            self.resume(source)
        self.service._put('decisions', dict(id='pending', task_id=owner['id'], status='rejected'))
        owner['_waiting_on'] = ['dependency']
        self.service._put('tasks', owner)
        with self.assertRaisesRegex(ServiceError, 'dependencies'):
            self.resume(source)
        other = self.write('/v1/bots', {'name': 'Unrelated'})
        self.service._put('tasks', dict(source, bot_id=other['id']))
        with self.assertRaisesRegex(ServiceError, 'owner or its requesting bot'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_ready_snapshot_wait_revalidates_live_worker_without_deadlock(self):
        def change(owner, source, peer, items, snapshots):
            self.service._put('tasks', dict(owner, status='queued'))
            snapshots[owner['id']].update(status='ready', current_run_id=123)
        owner, _, _, result = self.concurrent_snapshot(missing_owner=True, change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('active work', str(result))
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'queued')

    def check_ready_dispatch_race(self, fail_tick=False):
        from contextlib import ExitStack
        owner, source = self.ready_native()
        adapter = self.service.kanban
        review = adapter.tasks[owner['id']]['status'] == 'review'
        adapter.lock = threading.Lock()
        entered, release = threading.Event(), threading.Event()
        errors, captured = [], []

        def call(request, spawn=None):
            with adapter.lock:
                if request['op'] != 'tick':
                    return {}
                captured.extend(r for r in request['rows'] if r['id'] == owner['id'])
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Continuation did not release the native tick')
                if fail_tick:
                    raise RuntimeError('Lost tick response')
                if review:
                    return {}
                # The real callback takes the service lock under the adapter lock.
                spawn(dict(external_id=owner['id'], native_id='t_existing',
                           run_id='new-run', workspace='/tmp/retained-native-workspace'))
                adapter.tasks[owner['id']].update(status='running', current_run_id='new-run')
                return {}

        adapter.call.side_effect = call
        def dispatch():
            try:
                self.service._dispatch_pending()
            except BaseException as error:
                errors.append(error)

        with ExitStack() as stack:
            for name in ('_migrate_native_work', '_adopt_native_outcomes', '_reconcile_kanban',
                         '_dispatch_chat', '_publish_kanban_changes'):
                stack.enter_context(patch.object(self.service, name))
            stack.enter_context(patch.object(self.service.provider, 'prepare_kanban_worker',
                                            return_value=123, create=True))
            tick = threading.Thread(target=dispatch, daemon=True)
            tick.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(ServiceError, 'dispatch is in progress'):
                    self.resume(source)
                self.assertEqual(self.service._get('tasks', owner['id']), owner)
            finally:
                release.set()
                tick.join(5)
            self.assertFalse(tick.is_alive(), 'Adapter -> service callback deadlocked')
        self.assertEqual(self.service._kanban_claiming, 0)
        self.assertEqual(len(captured), 1)
        if fail_tick:
            self.assertEqual([str(error) for error in errors], ['Lost tick response'])
            self.assertTrue(self.service._kanban_dispatching)
            with self.assertRaisesRegex(ServiceError, 'dispatch is in progress'):
                self.resume(source)
        else:
            self.assertFalse(self.service._kanban_dispatching)
            self.assertEqual(errors, [])
            saved = self.service._get('tasks', owner['id'])
            if review:
                self.assertEqual(saved, owner)
            else:
                self.assertEqual(saved['_kanban_claim']['input'], captured[0]['input'])
            self.assertEqual(saved['_native_turn_id'], owner['_native_turn_id'])

    def test_ready_continuation_cannot_race_captured_dispatch_input(self):
        self.check_ready_dispatch_race()

    def test_ready_dispatch_failure_retains_uncertain_response_fence(self):
        self.check_ready_dispatch_race(fail_tick=True)

    def check_ready_dispatch_idle(self, failure=None, restart=False):
        from contextlib import ExitStack
        owner, source = self.ready_native()
        adapter = self.service.kanban
        adapter.lock = threading.Lock()
        idle, release = threading.Event(), threading.Event()
        errors, rows = [], []
        phase = {'failure': failure}
        reconcile = self.service._reconcile_kanban
        wait = self.service.changed.wait

        def call(request, spawn=None):
            with adapter.lock:
                if request['op'] == 'tick':
                    rows.extend(r for r in request['rows'] if r['id'] == owner['id'])
                if request['op'] == phase['failure']:
                    raise RuntimeError('Lost native response')
                return {}

        def reconcile_snapshot():
            if phase['failure'] == 'reconcile' and rows:
                raise RuntimeError('Reconciliation failed')
            return reconcile()

        def idle_wait(timeout=None):
            idle.set()
            # Use the real condition so admission must acquire the service lock.
            while not release.is_set():
                if not wait(5):
                    raise AssertionError('Continuation did not release dispatch idle wait')

        def dispatch():
            try:
                self.service._dispatch_pending()
            except BaseException as error:
                errors.append(error)

        adapter.call.side_effect = call
        with ExitStack() as stack:
            for name in ('_migrate_native_work', '_adopt_native_outcomes',
                         '_dispatch_chat', '_publish_kanban_changes'):
                stack.enter_context(patch.object(self.service, name))
            stack.enter_context(patch.object(self.service, '_reconcile_kanban', side_effect=reconcile_snapshot))
            stack.enter_context(patch.object(self.service.changed, 'wait', side_effect=idle_wait))
            if failure:
                with self.assertRaisesRegex(RuntimeError, 'Lost native response|Reconciliation failed'):
                    self.service._dispatch_pending()
                with self.assertRaisesRegex(ServiceError, 'dispatch is in progress'):
                    self.resume(source)
                self.assertEqual(self.service._get('tasks', owner['id']), owner)
                # A failed recovery snapshot must not clear the retained fence.
                phase['failure'] = 'snapshot'
                with self.assertRaisesRegex(RuntimeError, 'Lost native response'):
                    self.service._dispatch_pending()
                with self.assertRaisesRegex(ServiceError, 'dispatch is in progress'):
                    self.resume(source)
            phase['failure'] = None
            self.service.restart_pending = restart
            tick = threading.Thread(target=dispatch, daemon=True)
            tick.start()
            try:
                self.assertTrue(idle.wait(5), errors)
                self.assertTrue(self.service._kanban_dispatching)
                receipt = self.resume(source)
                self.assertEqual(receipt['task_id'], owner['id'])
                self.assertEqual(self.resume(source), receipt)
                continued = self.service._get('tasks', owner['id'])
                self.assertNotEqual(continued['_native_turn_id'], owner['_native_turn_id'])
                for key in ('id', '_execution_session_id', 'source_bot_id', 'source_task_id', 'thread_id'):
                    self.assertEqual(continued[key], owner[key])
                self.assertTrue(all(r['input'] == owner['_native_turn_id'] for r in rows))
            finally:
                release.set()
                with self.service.changed:
                    self.service.changed.notify_all()
                tick.join(5)
            self.assertFalse(tick.is_alive(), 'Service dispatch did not leave its idle wait')
            self.assertEqual(errors, [])
        self.assertFalse(self.service._kanban_dispatching)
        self.assertEqual(self.service._kanban_claiming, 0)
        self.assertFalse(self.service._kanban_dispatch_uncertain)

    def test_ready_continuation_admitted_during_service_dispatch_idle(self):
        self.check_ready_dispatch_idle()

    def test_ready_continuation_after_lost_tick_waits_for_reconciliation(self):
        self.check_ready_dispatch_idle(failure='tick')

    def test_ready_continuation_after_failed_reconciliation_recovers(self):
        self.check_ready_dispatch_idle(failure='reconcile')

    def test_ready_continuation_after_drain_reconciliation_recovers(self):
        self.check_ready_dispatch_idle(failure='tick', restart=True)

    def test_requester_resumes_same_outcome_and_preserves_its_result_route(self):
        owner, source = self.native()
        before = len(self.service._all('tasks'))
        result = self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(result['task_id'], owner['id'])
        self.assertEqual(continued['status'], 'queued')
        self.assertNotEqual(continued.get('_native_turn_id'), owner.get('_native_turn_id'))
        for key in ('_native_outcome_id', '_execution_session_id', '_work_todos',
                    'source_task_id', 'source_bot_id', 'thread_id', '_preference_source_ids'):
            self.assertEqual(continued.get(key), owner.get(key))
        self.assertEqual(self.resume(source), result)
        self.assertEqual(self.service._get('tasks', owner['id']), continued)

    def test_resumed_requester_receives_the_native_result_after_its_old_turn_failed(self):
        owner, original = self.native()
        self.service._put('tasks', dict(original, status='failed'))
        reply = self.write('/v1/tasks/' + original['id'] + '/replies', {'text': 'Continue the status check.'})
        source = self.service._get('tasks', reply['id'])
        source['status'] = 'working'
        self.service._put('tasks', source)
        self.assertEqual(source['_execution_session_id'], original['_execution_session_id'])
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['source_task_id'], original['id'])
        self.assertEqual(continued['_reply_to_task_id'], source['id'])
        continued['status'] = 'completed'
        self.service.kanban.tasks[owner['id']].update(status='done', input_id=continued['_native_turn_id'])
        self.service._prepare_return(continued, 'Assessment complete.', 'completed')
        self.service._put('tasks', continued)
        self.service._put('tasks', dict(source, status='completed'))
        self.service._drain_returns()
        returned = [t for t in self.service._all('tasks') if t.get('_is_return')]
        self.assertEqual(len(returned), 1)
        self.assertEqual(returned[0]['_assignment_task_id'], source['id'])
        self.service._run_task(returned[0]['id'])
        self.assertEqual(self.service._get('tasks', returned[0]['id'])['status'], 'completed')
        self.assertEqual(self.service._get('tasks', original['id'])['status'], 'failed')

    def test_new_chat_can_continue_work_without_replacing_the_original_requester(self):
        owner, original = self.native()
        new = self.write('/v1/bots/' + original['bot_id'] + '/messages', {'text': 'Continue the original correction.'})
        source = self.service._get('tasks', new['id'])
        source['status'] = 'working'
        self.service._put('tasks', source)
        self.assertNotEqual(source['exchange_id'], owner['exchange_id'])
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['source_task_id'], original['id'])
        self.assertEqual(continued['exchange_id'], owner['exchange_id'])
        self.assertEqual(continued['source_bot_id'], original['bot_id'])

    def test_another_bot_cannot_resume_the_outcome(self):
        owner, source = self.native()
        other = self.write('/v1/bots', {'name': 'Unrelated'})
        self.service._put('tasks', dict(source, bot_id=other['id']))
        with self.assertRaisesRegex(ServiceError, 'owner or its requesting bot'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_cannot_resume_active_or_archived_native_work(self):
        owner, source = self.native()
        for state in ('running', 'ready', 'archived'):
            self.service.kanban.tasks[owner['id']]['status'] = state
            with self.subTest(state=state), self.assertRaisesRegex(ServiceError, 'not blocked'):
                self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_completed_outcome_accepts_new_facts_without_changing_execution(self):
        owner, source = self.native()
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        result = self.resume(source)
        continued = self.service._get('tasks', result['task_id'])
        self.assertEqual(continued['id'], owner['id'])
        self.assertEqual(continued['_execution_session_id'], owner['_execution_session_id'])
        self.assertEqual(continued['source_task_id'], owner['source_task_id'])
        self.assertEqual(continued['status'], 'queued')
        self.assertNotEqual(continued['_native_turn_id'], owner.get('_native_turn_id'))

    def test_completed_successor_can_keep_its_previous_claim(self):
        owner, source = self.native()
        owner['_kanban_claim'] = {'native_id': 't_previous', 'input': 'previous-input'}
        self.service._put('tasks', owner)
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        self.assertEqual(self.resume(source)['task_id'], owner['id'])

    def test_requester_continues_quiescent_scheduled_outcome_from_durable_claim(self):
        owner, source = self.scheduled_native()
        task_count = len(self.service._all('tasks'))
        original_execution = owner['_execution_session_id']
        original_workspace = '/tmp/retained-native-workspace'
        self.service.kanban.tasks[owner['id']]['workspace_path'] = original_workspace
        result = self.resume(source)
        continued = self.service._get('tasks', result['task_id'])
        row = next(row for row in self.service._kanban_rows() if row['id'] == owner['id'])
        self.assertEqual(continued['id'], owner['id'])
        self.assertEqual(continued['_native_outcome_id'], 't_existing')
        self.assertEqual(continued['_execution_session_id'], original_execution)
        self.assertEqual(continued['status'], 'queued')
        self.assertEqual(row['state'], 'ready')
        self.assertEqual(row['native_id'], 't_existing')
        self.assertEqual(row['previous_input'], 'completed-input')
        self.assertNotEqual(row['input'], row['previous_input'])
        self.assertEqual(row['workspace'], original_workspace)
        self.assertEqual(self.resume(source), result)
        self.assertEqual(len(self.service._all('tasks')), task_count)
        self.assertEqual(len(continued['_bot_messages']),
                         len(self.service._get('tasks', owner['id'])['_bot_messages']))

    def test_owner_can_continue_quiescent_scheduled_outcome(self):
        owner, source = self.scheduled_native()
        self.service._put('tasks', dict(source, bot_id=owner['bot_id']))
        self.assertEqual(self.resume(source)['task_id'], owner['id'])

    def test_promoted_claim_retains_work_for_a_human_reply_after_completion(self):
        owner, source = self.scheduled_native()
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        continued['status'] = 'completed'
        self.service._put('tasks', continued)
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        result = self.write('/v1/tasks/' + owner['id'] + '/replies',
                            {'text': 'Check the additional review finding in this same work.'})
        next_task = self.service._get('tasks', result['id'])
        self.assertEqual(next_task['_execution_session_id'], owner['_execution_session_id'])
        item = self.service._get('artifacts', next_task['_work_context_id'])
        self.assertEqual(item['_native_outcome_id'], 't_existing')
        self.assertEqual(len([a for a in self.service._all('artifacts') if a['kind'] == 'work']), 1)

    def test_promoted_claim_reuses_its_existing_work_item(self):
        owner, source = self.scheduled_native()
        self.service._put('artifacts', dict(id='retained-work', task_id=owner['id'],
            bot_id=owner['bot_id'], kind='work', title='The original outcome', status='waiting'))
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['_work_context_id'], 'retained-work')
        item = self.service._get('artifacts', 'retained-work')
        self.assertEqual(item['title'], 'The original outcome')
        self.assertEqual(item['_native_outcome_id'], 't_existing')
        self.assertEqual(len(self.service._all('artifacts')), 1)

    def test_promoted_claim_binds_an_existing_context_before_a_human_reply(self):
        owner, source = self.scheduled_native()
        owner['_work_context_id'] = 'retained-work'
        self.service._put('tasks', owner)
        self.service._put('artifacts', dict(id='retained-work', task_id=owner['id'],
            bot_id=owner['bot_id'], kind='work', title='The original outcome', status='waiting'))
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        continued['status'] = 'completed'
        self.service._put('tasks', continued)
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        result = self.write('/v1/tasks/' + owner['id'] + '/replies',
                            {'text': 'Continue this work with the new review finding.'})
        followup = self.service._get('tasks', result['id'])
        self.assertEqual(followup['_native_outcome_id'], 't_existing')
        self.assertEqual(followup['_work_context_id'], 'retained-work')
        self.assertEqual(followup['_execution_session_id'], owner['_execution_session_id'])

    def test_promoted_claim_retains_a_standalone_pr(self):
        owner, source = self.scheduled_native()
        self.service._put('artifacts', dict(id='retained-pr', task_id=owner['id'],
            bot_id=owner['bot_id'], kind='pr', title='The original PR', status='open',
            url='https://github.com/example/project/pull/1', pr_state='open'))
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['_work_context_id'], 'retained-pr')
        self.assertEqual(self.service._get('artifacts', 'retained-pr')['_native_outcome_id'], 't_existing')
        self.assertEqual(len(self.service._all('artifacts')), 1)

    def test_claim_promotion_cannot_take_a_pr_from_another_execution(self):
        owner, source = self.scheduled_native()
        from uuid import uuid4
        other = dict(owner, id=str(uuid4()), _execution_session_id=str(uuid4()), status='working')
        self.service._put('tasks', other)
        item = dict(id='retained-pr', task_id=owner['id'], bot_id=owner['bot_id'],
                    kind='pr', title='The original PR', status='open',
                    url='https://github.com/example/project/pull/1', pr_state='open')
        for field in ('_review_task_id', '_activity_task_id'):
            self.service._put('artifacts', dict(item, **{field: other['id']}))
            with self.subTest(field=field), self.assertRaisesRegex(ServiceError, 'another execution'):
                self.resume(source)
            self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def completed_pr_peer(self):
        from uuid import uuid4
        owner, source = self.scheduled_native()
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        peer = self.write('/v1/bots/' + source['bot_id'] + '/messages', {'text': 'Track this PR.'})
        peer = self.service._get('tasks', peer['id'])
        peer.update(status='completed', _execution_session_id=peer['id'],
                    _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', peer)
        items = []
        for task in (owner, peer):
            item = dict(id=str(uuid4()), task_id=task['id'], bot_id=task['bot_id'],
                kind='pr', title='Prepared draft', status='open', pr_state='open',
                created_at=task['created_at'],
                url='https://github.com/example/repo/pull/15',
                _review_task_id=task['id'], _activity_task_id=task['id'])
            self.service._put('artifacts', item)
            items.append(item)
        return owner, source, peer, items

    def concurrent_snapshot(self, missing_owner=False, change=None):
        owner, source, peer, items = self.completed_pr_peer()
        peer['_kanban_claim'] = dict(native_id='t_peer', input='peer-input')
        self.service._put('tasks', peer)
        snapshots = {owner['id']: dict(self.service.kanban.tasks[owner['id']]),
                     peer['id']: dict(id='t_peer', status='done')}
        adapter = Mock(tasks={} if missing_owner else {owner['id']: snapshots[owner['id']]})
        adapter.lock = threading.Lock()
        locked, requested = threading.Event(), threading.Event()
        errors, results, held = [], [], []

        def call(request, spawn=None):
            if request['op'] == 'snapshot':
                held.append(self.service.lock._is_owned())
                requested.set()
            with adapter.lock:
                if request['op'] == 'tick':
                    locked.set()
                    if not requested.wait(5):
                        raise AssertionError('Continuation never requested its missing snapshot')
                    spawn()
                else:
                    # Match the real adapter: replace, rather than merge, its cache.
                    adapter.tasks = {i: dict(snapshots[i]) for i in request['external_ids'] if i in snapshots}
                return self.snapshot_result()

        adapter.call.side_effect = call
        self.service.kanban = adapter

        def spawn():
            # A bounded acquire lets the negative control unwind both real threads.
            if not self.service.lock.acquire(timeout=2):
                raise AssertionError('Adapter -> service lock deadlocked with continuation')
            try:
                if change:
                    change(owner, source, peer, items, snapshots)
            finally:
                self.service.lock.release()

        def run_tick():
            try:
                adapter.call({'op': 'tick'}, spawn=spawn)
            except BaseException as error:
                errors.append(error)

        def run_continue():
            try:
                results.append(self.resume(source))
            except BaseException as error:
                results.append(error)

        tick = threading.Thread(target=run_tick, daemon=True)
        continuation = threading.Thread(target=run_continue, daemon=True)
        tick.start()
        self.assertTrue(locked.wait(5))
        continuation.start()
        continuation.join(10)
        tick.join(10)
        self.assertFalse(continuation.is_alive(), 'Continuation did not finish')
        self.assertFalse(tick.is_alive(), 'Scheduler tick did not finish')
        self.assertEqual(errors, [])
        self.assertTrue(held)
        self.assertFalse(any(held), 'Snapshot called under the service transaction lock')
        self.assertEqual(len(results), 1)
        return owner, peer, items, results[0]

    def test_uncached_peer_snapshot_does_not_deadlock_scheduler_spawn(self):
        owner, peer, items, receipt = self.concurrent_snapshot()
        self.assertIsInstance(receipt, dict)
        self.assertEqual(receipt['task_id'], owner['id'])
        self.assertEqual(self.service._get('artifacts', items[1]['id']), items[1])
        self.assertEqual(self.service._get('tasks', peer['id']), peer)

    def test_aged_owner_and_peer_snapshots_do_not_deadlock_scheduler_spawn(self):
        owner, _, _, receipt = self.concurrent_snapshot(missing_owner=True)
        self.assertIsInstance(receipt, dict)
        self.assertEqual(receipt['task_id'], owner['id'])

    def test_snapshot_wait_revalidates_changed_peer_state(self):
        def change(owner, source, peer, items, snapshots):
            self.service._put('tasks', dict(peer, _return_pending={'text': 'New result'}))
        owner, _, items, result = self.concurrent_snapshot(change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('another execution', str(result))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('artifacts', items[0]['id']), items[0])

    def test_snapshot_wait_revalidates_owner_mapping(self):
        def change(owner, source, peer, items, snapshots):
            self.service._put('tasks', dict(owner, _native_outcome_id='t_rebound'))
        owner, _, _, result = self.concurrent_snapshot(missing_owner=True, change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('mapping does not match', str(result))
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')

    def test_snapshot_wait_revalidates_human_pause(self):
        def change(owner, source, peer, items, snapshots):
            self.service._put('decisions', dict(id='new-pause', task_id=owner['id'], status='pending'))
        owner, _, _, result = self.concurrent_snapshot(change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('existing decision', str(result))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_snapshot_wait_revalidates_source(self):
        def change(owner, source, peer, items, snapshots):
            self.service._put('tasks', dict(source, status='completed'))
        owner, _, _, result = self.concurrent_snapshot(change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('cannot continue work now', str(result))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_snapshot_wait_revalidates_peer_links(self):
        def change(owner, source, peer, items, snapshots):
            self.service._put('artifacts', dict(items[1], _activity_task_id=source['id']))
        owner, _, _, result = self.concurrent_snapshot(change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('another execution', str(result))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_snapshot_wait_fails_closed_for_missing_native_peer(self):
        def change(owner, source, peer, items, snapshots):
            snapshots.pop(peer['id'])
        owner, _, _, result = self.concurrent_snapshot(change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('another execution', str(result))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_snapshot_wait_fails_closed_for_rebound_native_peer(self):
        def change(owner, source, peer, items, snapshots):
            snapshots[peer['id']] = dict(id='t_rebound', status='done')
        owner, _, _, result = self.concurrent_snapshot(change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('another execution', str(result))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_owner_lookup_without_deferred_callback_keeps_aged_binding(self):
        owner, _ = self.scheduled_native()
        snapshot = self.service.kanban.tasks.pop(owner['id'])
        def refresh(request):
            self.assertEqual(request, {'op': 'snapshot', 'external_ids': [owner['id']]})
            self.service.kanban.tasks[owner['id']] = snapshot
            return self.snapshot_result()
        self.service.kanban.call.side_effect = refresh
        resolved, native = self.service._native_continuation_owner('t_existing')
        self.assertEqual(resolved, owner)
        self.assertEqual(native, snapshot)
        self.service.kanban.call.assert_called_once()

    def test_aged_owner_batches_all_missing_peer_chains(self):
        from uuid import uuid4
        owner, source, peer, items = self.completed_pr_peer()
        snapshots = {owner['id']: dict(self.service.kanban.tasks[owner['id']])}
        for index in range(8):
            identity = str(uuid4())
            task = dict(peer, id=identity, _execution_session_id=identity,
                        _kanban_claim=dict(native_id='t_peer_' + str(index), input='finished'))
            self.service._put('tasks', task)
            snapshots[identity] = dict(id=task['_kanban_claim']['native_id'], status='done')
            self.service._put('artifacts', dict(items[1], id=str(uuid4()), task_id=identity,
                _review_task_id=identity, _activity_task_id=identity))
        self.service.kanban.tasks = {}
        def snapshot(request):
            self.assertFalse(self.service.lock._is_owned())
            self.service.kanban.tasks = {i: snapshots[i] for i in request['external_ids']}
            return self.snapshot_result()
        self.service.kanban.call.side_effect = snapshot
        self.assertEqual(self.resume(source)['task_id'], owner['id'])
        self.assertEqual(self.service.kanban.call.call_count, 2)
        self.assertEqual(set(self.service.kanban.call.call_args.args[0]['external_ids']), set(snapshots))

    def test_completed_cross_bot_pr_peer_retains_original_owner(self):
        owner, source, peer, items = self.completed_pr_peer()
        receipt = self.resume(source)
        self.assertEqual(receipt['task_id'], owner['id'])
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['_execution_session_id'], owner['_execution_session_id'])
        self.assertEqual(continued['_work_context_id'], items[0]['id'])
        self.assertEqual(self.service._get('artifacts', items[0]['id'])['task_id'], owner['id'])
        self.assertEqual(self.service._get('artifacts', items[1]['id']), items[1])
        self.assertEqual(self.service._get('tasks', peer['id']), peer)
        self.assertEqual(self.resume(source), receipt)

    def test_completed_peer_with_unresolved_work_still_blocks(self):
        owner, source, peer, items = self.completed_pr_peer()
        cases = [dict(status=s) for s in ('queued', 'working', 'waiting', 'needs_you', 'paused', 'cancelled', 'failed')]
        cases += [dict(_waiting_on=['undelivered']), dict(_return_pending={'text': 'Result'}),
                  dict(_work_todos={'todos': [{'id': 'pending', 'status': 'pending'}]}),
                  dict(bot_id=owner['bot_id'])]
        for changes in cases:
            with self.subTest(changes=changes):
                self.service._put('tasks', dict(peer, **changes))
                with self.assertRaisesRegex(ServiceError, 'another execution'):
                    self.resume(source)
                self.assertEqual(self.service._get('tasks', owner['id']), owner)
                self.assertEqual(self.service._get('artifacts', items[0]['id']), items[0])

    def test_completed_peer_keeps_pending_callbacks_decisions_and_processes(self):
        owner, source, peer, items = self.completed_pr_peer()
        for table, value in (
                ('decisions', dict(id='pending', task_id=peer['id'], status='pending')),
                ('contexts', dict(id='callback', task_id=peer['id'], status='queued')),
                ('native_receipts', dict(id='tool', task_id=peer['id'], state='executing',
                                         action={'name': 'terminal', 'arguments': {}}, created_at=peer['created_at'])),
                ('native_receipts', dict(id='wait', task_id=peer['id'], state='completed',
                                         action={'name': 'coconut_wait', 'arguments': {}},
                                         _wait_task_id=owner['id'], created_at=peer['created_at']))):
            with self.subTest(table=table, identity=value['id']):
                self.service._put(table, value)
                with self.assertRaisesRegex(ServiceError, 'another execution'):
                    self.resume(source)
                self.service.db.execute('DELETE FROM ' + table + ' WHERE id=?', (value['id'],))
                self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_completed_peer_requires_terminal_native_binding(self):
        owner, source, peer, items = self.completed_pr_peer()
        for retained in ({'_native_outcome_id': 't_peer'},
                         {'_kanban_claim': {'native_id': 't_peer', 'input': 'peer-input'}}):
            self.service._put('tasks', dict(peer, **retained))
            for snapshot in ({'id': 't_peer', 'status': s} for s in
                             ('scheduled', 'running', 'ready', 'blocked', 'triage', 'archived')):
                self.service.kanban.tasks[peer['id']] = snapshot
                with self.subTest(retained=retained, snapshot=snapshot):
                    with self.assertRaisesRegex(ServiceError, 'another execution'):
                        self.resume(source)
            for snapshot in ({'id': 'different', 'status': 'done'},
                             {'id': 't_peer', 'status': 'done', 'worker_pid': 123},
                             {'id': 't_peer', 'status': 'done', 'current_run_id': 'run'}):
                self.service.kanban.tasks[peer['id']] = snapshot
                with self.assertRaisesRegex(ServiceError, 'another execution'):
                    self.resume(source)
            del self.service.kanban.tasks[peer['id']]
            with self.assertRaisesRegex(ServiceError, 'another execution'):
                self.resume(source)
            self.service.kanban.call.assert_called_with({'op': 'snapshot', 'external_ids': [peer['id']]})
        self.service.kanban.tasks[peer['id']] = {'id': 't_peer', 'status': 'done'}
        self.assertEqual(self.resume(source)['task_id'], owner['id'])

    def test_completed_peer_reporter_keeps_active_execution_owner(self):
        from uuid import uuid4
        owner, source, peer, items = self.completed_pr_peer()
        reporter = dict(peer, id=str(uuid4()), _assignment_task_id=peer['id'])
        self.service._put('tasks', reporter)
        self.service._put('artifacts', dict(items[1], _review_task_id=reporter['id'],
                                          _activity_task_id=reporter['id']))
        for changes in ({'status': 'working'}, {'_return_pending': {'text': 'Undelivered result'}}):
            with self.subTest(changes=changes):
                self.service._put('tasks', dict(peer, **changes))
                with self.assertRaisesRegex(ServiceError, 'another execution'):
                    self.resume(source)
                self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_completed_peer_keeps_active_continuation_and_human_pause(self):
        from uuid import uuid4
        owner, source, peer, items = self.completed_pr_peer()
        child = dict(peer, id=str(uuid4()), status='working', _assignment_task_id=peer['id'])
        self.service._put('tasks', child)
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(source)
        self.service._put('tasks', dict(child, status='completed'))
        decision = dict(id='pause', task_id=peer['id'], status='rejected', decided_by='human')
        self.service._put('decisions', decision)
        self.service._put('artifacts', dict(items[1], paused_by_decision_id=decision['id']))
        with self.assertRaisesRegex(ServiceError, 'paused'):
            self.resume(source)
        self.service._put('artifacts', items[1])
        self.service.db.execute('INSERT INTO settings VALUES (?,?)',
                                ('work.cancelled.' + items[1]['url'], '"cancelled"'))
        with self.assertRaisesRegex(ServiceError, 'cancelled'):
            self.resume(source)

    def test_claim_promotion_preserves_cancellation_of_its_retained_pr(self):
        owner, source = self.scheduled_native()
        item = dict(id='retained-pr', task_id=owner['id'], bot_id=owner['bot_id'],
                    kind='pr', title='The original PR', status='open',
                    url='https://github.com/example/project/pull/1', pr_state='open')
        self.service._put('artifacts', item)
        self.service.db.execute('INSERT INTO settings VALUES (?,?)',
                                ('work.cancelled.' + item['url'], '"2026-09-18T00:00:00Z"'))
        with self.assertRaisesRegex(ServiceError, 'cancelled'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('artifacts', item['id']), item)

    def test_claim_promotion_checks_linked_pr_owners_behind_a_work_context(self):
        owner, source = self.scheduled_native()
        from uuid import uuid4
        other = dict(owner, id=str(uuid4()), _execution_session_id=str(uuid4()), status='working')
        self.service._put('tasks', other)
        owner['_work_context_id'] = 'retained-work'
        self.service._put('tasks', owner)
        self.service._put('artifacts', dict(id='retained-work', task_id=owner['id'],
            bot_id=owner['bot_id'], kind='work', title='The original outcome', status='waiting'))
        pr = dict(id='linked-pr', task_id=other['id'], bot_id=owner['bot_id'],
                  kind='pr', title='The linked PR', status='open', pr_state='open',
                  url='https://github.com/example/project/pull/1', work_outcome_id='retained-work')
        for field in ('_review_task_id', '_activity_task_id'):
            self.service._put('artifacts', dict(pr, **{field: other['id']}))
            with self.subTest(field=field), self.assertRaisesRegex(ServiceError, 'another execution'):
                self.resume(source)
            self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_promoted_claim_resolves_and_deduplicates_retained_aliases(self):
        owner, source = self.scheduled_native()
        root = dict(id='retained-work', task_id=owner['id'], bot_id=owner['bot_id'],
                    kind='work', title='The original outcome', status='waiting')
        for item in (root, dict(root, id='hidden', _hidden=True, _work_parent_id='retained-work'),
                     dict(root, id='replaced', _work_replaced_by='hidden'),
                     dict(root, id='pr-link', kind='pr', work_outcome_id='replaced')):
            self.service._put('artifacts', item)
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['_work_context_id'], 'retained-work')
        self.assertEqual(self.service._get('artifacts', 'retained-work')['_native_outcome_id'], 't_existing')
        self.assertEqual(len(self.service._all('artifacts')), 4)

    def test_promoted_claim_resolves_a_retained_context_alias(self):
        owner, source = self.scheduled_native()
        owner['_work_context_id'] = 'alias'
        self.service._put('tasks', owner)
        root = dict(id='retained-work', task_id=owner['id'], bot_id=owner['bot_id'],
                    kind='work', title='The original outcome', status='waiting')
        self.service._put('artifacts', root)
        self.service._put('artifacts', dict(root, id='alias', _work_replaced_by=root['id']))
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['_work_context_id'], root['id'])
        self.assertEqual(self.service._get('artifacts', root['id'])['_native_outcome_id'], 't_existing')

    def test_scheduled_continuation_rejects_foreign_caller(self):
        owner, source = self.scheduled_native()
        other = self.write('/v1/bots', {'name': 'Unrelated'})
        self.service._put('tasks', dict(source, bot_id=other['id']))
        with self.assertRaisesRegex(ServiceError, 'owner or its requesting bot'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_scheduled_continuation_preserves_human_work_pause(self):
        owner, source = self.scheduled_native()
        with patch.object(self.service, '_task_work_is_paused', return_value=True), \
                self.assertRaisesRegex(ServiceError, 'paused, cancelled'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_scheduled_native_mapping_must_be_unambiguous_and_consistent(self):
        owner, source = self.scheduled_native()
        cases = (
            {'_native_outcome_id': 't_conflicting'},
            {'_kanban_claim': dict(owner['_kanban_claim'], native_id='t_conflicting')},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.service._put('tasks', dict(owner, **changes))
                with self.assertRaisesRegex(ServiceError, 'mapping does not match'):
                    self.resume(source)
                self.assertEqual(self.service._get('tasks', owner['id']), dict(owner, **changes))

    def test_scheduled_continuation_rejects_active_native_work(self):
        owner, source = self.scheduled_native()
        native = self.service.kanban.tasks[owner['id']]
        for changes in ({'current_run_id': 'active-run'}, {'worker_pid': 1234}):
            with self.subTest(changes=changes):
                native.update(current_run_id=None, worker_pid=None)
                native.update(changes)
                with self.assertRaisesRegex(ServiceError, 'active work'):
                    self.resume(source)
        native.update(current_run_id=None, worker_pid=None)
        worker = Mock()
        worker.is_alive.return_value = True
        self.service.running[owner['id']] = worker
        with self.assertRaisesRegex(ServiceError, 'active work'):
            self.resume(source)
        self.service.running.clear()
        with patch.object(self.service, '_background_pending', return_value=True), \
                self.assertRaisesRegex(ServiceError, 'active work'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_scheduled_continuation_fails_closed_for_cancelled_deleted_or_missing_work(self):
        owner, source = self.scheduled_native()
        exchange = self.service._get('exchanges', owner['exchange_id'])
        self.service._put('exchanges', dict(exchange, cancelled=True))
        with self.assertRaisesRegex(ServiceError, 'paused, cancelled'):
            self.resume(source)
        self.service._put('exchanges', exchange)
        bot = self.service._get('bots', owner['bot_id'])
        self.service._put('bots', dict(bot, _deleted=True))
        with self.assertRaisesRegex(ServiceError, 'paused, cancelled'):
            self.resume(source)
        self.service._put('bots', bot)
        self.service.kanban.tasks.clear()
        with self.assertRaisesRegex(ServiceError, 'existing native outcome'):
            self.resume(source)

    def reported_work(self):
        from uuid import uuid4
        owner, source = self.native()
        reporter = dict(owner, id=str(uuid4()), status='waiting', _native_outcome_id=None,
                        _work_context_id=None, created_at='2099-01-01T00:00:00Z')
        reporter['_execution_session_id'] = reporter['id']
        self.service._put('tasks', reporter)
        item = dict(id=str(uuid4()), kind='work', bot_id=owner['bot_id'], task_id=reporter['id'],
            _work_key='retained-work', title='Continue the saved correction', status='waiting',
            activity='Waiting for the original worker', created_at=owner['created_at'], observed_at=owner['created_at'],
            _work_origin_task_id=owner['id'], _work_previous_task_ids=[owner['id']], _native_outcome_id='old-report')
        self.service._put('artifacts', item)
        owner['_work_context_id'] = item['id']
        self.service._put('tasks', owner)
        return owner, source, reporter, item

    def test_continuation_restores_an_owner_displaced_by_a_progress_report(self):
        owner, source, reporter, item = self.reported_work()
        self.resume(source)
        restored = self.service._get('artifacts', item['id'])
        self.assertEqual(restored['task_id'], owner['id'])
        self.assertEqual(restored['_native_outcome_id'], owner['_native_outcome_id'])
        self.assertIn(reporter['id'], restored['_work_previous_task_ids'])
        self.assertEqual(self.service._get('tasks', reporter['id']), reporter)

    def test_continuation_does_not_take_work_from_another_execution(self):
        owner, source, reporter, item = self.reported_work()
        cases = ({'_native_outcome_id': 'another'}, {'_work_context_id': item['id']},
                 {'status': 'working'}, {'status': 'paused'}, {'status': 'cancelled'}, {'session_id': 'old'})
        for changes in cases:
            with self.subTest(changes=changes):
                self.service._put('tasks', dict(reporter, **changes))
                with self.assertRaisesRegex(ServiceError, 'another execution'):
                    self.resume(source)
                self.assertEqual(self.service._get('artifacts', item['id']), item)
                self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.service._put('tasks', reporter)
        self.service._put('decisions', dict(id='pending-report', task_id=reporter['id'], status='pending'))
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(source)

    def test_reporter_wait_does_not_block_restoring_the_native_owner(self):
        owner, source, reporter, item = self.reported_work()
        self.service._put('tasks', dict(reporter, status='working'))
        self.service.emit(reporter['id'], 'work_wait', dict(task_id=source['id'], reason='Wait for the coordinator'))
        self.service._put('tasks', reporter)
        receipts = copy.deepcopy(self.service._native_task_receipts(reporter['id']))
        self.assertTrue(self.service._background_pending(reporter['id']))
        self.resume(source)
        self.assertEqual(self.service._get('artifacts', item['id'])['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', reporter['id']), reporter)
        self.assertEqual(self.service._native_task_receipts(reporter['id']), receipts)

    def test_reporter_process_or_unknown_action_still_blocks_restoration(self):
        import json
        owner, source, reporter, item = self.reported_work()
        self.service._put('tasks', dict(reporter, status='working'))
        record = self.service.emit(reporter['id'], 'native_call', dict(name='terminal', arguments={'command': 'run tests'}))
        self.service._put('tasks', reporter)
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(source)
        self.service._put('tasks', dict(reporter, status='working'))
        self.service.emit(reporter['id'], 'native_result', dict(id=record['id'],
            result=json.dumps(dict(notify_on_complete=True, session_id='running-process'))))
        self.service._put('tasks', reporter)
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(source)
        self.assertEqual(self.service._get('artifacts', item['id']), item)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_reporter_wait_for_another_tasks_decision_blocks_restoration(self):
        owner, source, reporter, item = self.reported_work()
        decision = dict(id='other-decision', task_id=source['id'], status='pending')
        self.service._put('decisions', decision)
        self.service._put('tasks', dict(reporter, status='working'))
        self.service.emit(reporter['id'], 'work_wait', dict(decision_id=decision['id'], reason='Wait for the human'))
        self.service._put('tasks', reporter)
        for changes in ({}, {'status': 'dismissed', 'inbox_undo_until': '2099-01-01T00:00:00Z'}):
            self.service._put('decisions', dict(decision, **changes))
            with self.subTest(changes=changes), self.assertRaisesRegex(ServiceError, 'another execution'):
                self.resume(source)
            self.assertEqual(self.service._get('artifacts', item['id']), item)
        self.service._put('decisions', dict(decision, status='approved'))
        self.resume(source)
        self.assertEqual(self.service._get('artifacts', item['id'])['task_id'], owner['id'])

    def test_old_chat_execution_does_not_block_current_reporter(self):
        from uuid import uuid4
        owner, source, reporter, item = self.reported_work()
        reporter.pop('_execution_session_id')
        reporter['_chat_session'] = True
        self.service._put('tasks', reporter)
        old = dict(reporter, id=str(uuid4()), status='working', _conversation_epoch='old-conversation')
        self.service._put('tasks', old)
        self.service.emit(old['id'], 'native_call', dict(name='terminal', arguments={'command': 'old process'}))
        self.resume(source)
        self.assertEqual(self.service._get('artifacts', item['id'])['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', old['id'])['status'], 'working')

    def test_queued_reporter_callback_blocks_before_its_epoch_is_assigned(self):
        from uuid import uuid4
        owner, source, reporter, item = self.reported_work()
        callback = dict(reporter, id=str(uuid4()), status='queued', _assignment_task_id=reporter['id'])
        callback.pop('_conversation_epoch', None)
        self.service._put('tasks', callback)
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(source)
        self.assertEqual(self.service._get('artifacts', item['id']), item)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_progress_report_keeps_native_work_owner_and_binding(self):
        owner, source, reporter, item = self.reported_work()
        item.update(task_id=owner['id'], _native_outcome_id=owner['_native_outcome_id'])
        self.service._put('artifacts', item)
        reporter['status'] = 'working'
        self.service._put('tasks', reporter)
        self.service.emit(reporter['id'], 'artifact', dict(kind='work', key=item['_work_key'],
            title=item['title'], activity='The human revised the local scope', status='waiting',
            completion_condition='Verify the revised scope in the original worktree'))
        saved = self.service._get('artifacts', item['id'])
        self.assertEqual(saved['task_id'], owner['id'])
        self.assertEqual(saved['_native_outcome_id'], owner['_native_outcome_id'])
        self.assertEqual(saved['_work_report_task_id'], reporter['id'])
        self.assertEqual(saved['completion_condition'], 'Verify the revised scope in the original worktree')

    def test_cannot_resume_paused_work_or_pending_dependencies(self):
        owner, source = self.native()
        for changes in ({'status': 'paused'}, {'_waiting_on': [source['id']]}):
            self.service._put('tasks', dict(owner, **changes))
            with self.subTest(changes=changes), self.assertRaises(ServiceError):
                self.resume(source)
        self.service._put('tasks', owner)
        with patch.object(self.service, '_background_pending', return_value=True), self.assertRaisesRegex(ServiceError, 'active work'):
            self.resume(source)

    def test_pending_human_decision_remains_pending(self):
        owner, source = self.native()
        self.service._put('decisions', dict(id='decision', task_id=owner['id'], status='pending'))
        before = copy.deepcopy(self.service._all('decisions'))
        with self.assertRaisesRegex(ServiceError, 'existing decision'):
            self.resume(source)
        self.assertEqual(self.service._all('decisions'), before)

    def test_unconfirmed_actions_and_saved_results_reach_the_continued_worker(self):
        from coconut.hermes_worker import conversation_input
        owner, source = self.native()
        self.service._put('tasks', dict(owner, status='working', _native_turn_id='previous-turn'))
        unknown = self.service.emit(owner['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'git push'}))
        finished = self.service.emit(owner['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'git status'}))
        self.service.emit(owner['id'], 'native_result', dict(id=finished['id'], result='The commit is saved.'))
        owner.update(status='failed', _native_turn_id='previous-turn',
            _self_recovery={'unknown_tools': [dict(id='older', action={'name': 'publish'})]})
        self.service._put('tasks', owner)
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual({r['id'] for r in continued['_self_recovery']['unknown_tools']}, {'older', unknown['id']})
        self.assertEqual(continued['_continuation_receipt_ids'], [finished['id']])
        prompt = conversation_input(dict(operation='start', task=continued, history=[]))[0]
        self.assertIn('Never repeat a write while its effect remains unknown', prompt)
        self.assertIn('git push', prompt)
        self.assertEqual(self.service._get('native_receipts', unknown['id'])['state'], 'executing')

    def test_completed_turn_does_not_replay_old_observations_for_new_facts(self):
        owner, source = self.native()
        self.service._put('tasks', dict(owner, status='working', _native_turn_id='completed-turn'))
        record = self.service.emit(owner['id'], 'native_call', dict(name='terminal', arguments={'command': 'gh pr view'}))
        self.service.emit(owner['id'], 'native_result', dict(id=record['id'], result='Checks are pending.'))
        self.service._put('tasks', dict(owner, _native_turn_id='completed-turn'))
        self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id'])['_continuation_receipt_ids'], [])

    def test_continued_result_reaches_requester_after_an_earlier_return(self):
        owner, source = self.native()
        self.service.kanban.tasks[owner['id']].update(status='done', input_id=owner['id'])
        self.service._prepare_return(owner, 'Acceptance is blocked.', 'completed')
        self.service._put('tasks', owner)
        with self.assertRaisesRegex(ServiceError, 'previous result'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')
        self.service._drain_returns()
        returned = [t for t in self.service._all('tasks') if t.get('_is_return')]
        self.assertEqual(len(returned), 1)
        self.service._run_task(returned[0]['id'])
        self.service._put('tasks', dict(source, status='working'))
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.service.kanban.tasks[owner['id']].update(status='done', input_id=continued['_native_turn_id'])
        self.service._prepare_return(continued, 'Acceptance passed.', 'completed')
        continued['status'] = 'completed'
        self.service._put('tasks', continued)
        self.service._drain_returns()
        self.service._drain_returns()
        deliveries = [d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']]
        self.assertEqual([d['text'] for d in deliveries], ['Acceptance is blocked.', 'Acceptance passed.'])
        self.assertEqual({d['target_bot_id'] for d in deliveries}, {source['bot_id']})

    def test_explicit_work_pause_blocks_bot_continuation(self):
        owner, source = self.native()
        with patch.object(self.service, '_task_work_is_paused', return_value=True), self.assertRaisesRegex(ServiceError, 'paused'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_saved_human_followup_keeps_the_continuation_receipt_idempotent(self):
        owner, source = self.native()
        self.service._put('tasks', dict(owner, status='working', _turn_message_ids=[]))
        self.write('/v1/tasks/' + owner['id'] + '/replies', {'text': 'Keep the correction local.'})
        self.service.emit(owner['id'], 'failed', {'summary': 'Provider stopped before receiving the follow-up.'})
        self.assertTrue(self.service._get('tasks', owner['id'])['_blocked_followup'])
        self.service._drain_returns()
        self.service._put('tasks', dict(source, status='working'))
        result = self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertNotIn('_blocked_followup', continued)
        self.assertEqual(self.resume(source), result)
        self.assertEqual(self.service._get('tasks', owner['id']), continued)

    def test_human_restriction_survives_failure_before_provider_input(self):
        from coconut.hermes_worker import conversation_input
        owner, source = self.native()
        owner.update(status='queued', _kanban_claim={})
        self.service._put('tasks', owner)
        self.write('/v1/tasks/' + owner['id'] + '/replies', {'text': 'Do not publish. Keep the change local.'})
        start = self.provider.start_task
        def fail_before_input(*args):
            raise RuntimeError('Provider unavailable before input')
        self.provider.start_task = fail_before_input
        self.service._run_task(owner['id'])
        failed = self.service._get('tasks', owner['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertTrue(failed['_turn_message_ids'])
        self.provider.start_task = start
        self.service._drain_returns()
        self.service._put('tasks', dict(source, status='working'))
        self.resume(source)
        captured = []
        def verify(task, bot, history, emit):
            captured.append(conversation_input(dict(operation='start', task=task, history=history))[0])
            emit('completed', {'summary': 'The change remains local.'})
        self.provider.start_task = verify
        self.service._run_task(owner['id'])
        self.assertEqual(len(captured), 1)
        self.assertIn('Do not publish. Keep the change local.', captured[0])


class NativeContinuationProviderTests(unittest.TestCase):
    import test_provider as provider_fixtures
    setUp = provider_fixtures.ProviderTests.setUp
    tearDown = provider_fixtures.ProviderTests.tearDown
    fake_hermes = provider_fixtures.ProviderTests.fake_hermes

    def test_tool_receives_rejection_and_can_continue_another_native_task(self):
        provider = self.fake_hermes(
            "import json\n"
            "rejected = json.loads(registry.dispatch('coconut_continue_work', {'task_id': 't_active', 'message': 'Continue.'}))\n"
            "assert rejected == {'status': 'rejected', 'error': 'The worker is still active.'}\n"
            "receipt = json.loads(registry.dispatch('coconut_continue_work', {'task_id': 't_blocked', 'message': 'The checks passed.'}))\n"
            "assert receipt == {'status': 'queued', 'task_id': 'existing', 'native_task_id': 't_blocked'}\n"
            "return {'completed': True, 'final_response': 'The existing work will continue.'}\n")
        events = []
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'work_continue':
                if data['task_id'] == 't_active':
                    raise ServiceError(409, 'The worker is still active.')
                return dict(status='queued', task_id='existing', native_task_id='t_blocked')
        with patch.dict(os.environ, ANTHROPIC_API_KEY='test-only'):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(len([e for e in events if e[0] == 'work_continue']), 2)
        self.assertEqual(events[-1][0], 'completed', events)

    def test_premature_merge_validation_reaches_the_worker_without_failing(self):
        provider = self.fake_hermes(
            "import json\n"
            "result = json.loads(registry.dispatch('coconut_ask', {'title': 'Merge?', 'options': [{'id': 'merge', 'label': 'Merge'}]}))\n"
            "assert result['status'] == 'validation_failed'\n"
            "assert 'pending worker' in result['error']\n"
            "return {'completed': True, 'final_response': 'I will read the review result first.'}\n")
        events = []
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'native_question':
                raise ServiceError(400, 'The PR review still has a pending worker or delegated result.')
        with patch.dict(os.environ, ANTHROPIC_API_KEY='test-only'):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(events[-1][0], 'completed', events)
        self.assertNotIn('failed', [kind for kind, _ in events])


if __name__ == '__main__':
    unittest.main()
