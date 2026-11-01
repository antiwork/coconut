"""Owner-created routine reports preserve original continuation ownership."""
import copy
import json
import unittest
from uuid import UUID, uuid4, uuid5

from coconut import routine_reports
from coconut.service import ServiceError, digest, encoded, now
import test_native_work_continuation as fixtures


class RoutineReportContinuationTests(unittest.TestCase):
    setUp = fixtures.NativeWorkContinuationTests.setUp
    tearDown = fixtures.NativeWorkContinuationTests.tearDown
    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    resume = fixtures.NativeWorkContinuationTests.resume

    def claim(self, task, native_id, status='done', run=20):
        task['_kanban_claim'] = dict(native_id=native_id, external_id=task['id'],
            run_id=run, input=task.get('_native_turn_id', task['id']), context='')
        self.service._put('tasks', task)
        self.service.kanban.tasks[task['id']] = dict(id=native_id, external_id=task['id'],
            status=status, input_id=task['_kanban_claim']['input'],
            current_run_id=run if status == 'running' else None, worker_pid=None)

    def receipt(self, task, name, arguments, result):
        saved = self.service._get('tasks', task['id'])
        self.service._put('tasks', dict(task, status='working'))
        call = self.service.emit(task['id'], 'native_call', dict(name=name, arguments=arguments))
        self.service.emit(task['id'], 'native_result', dict(id=call['id'], result=json.dumps(result)))
        self.service._put('tasks', saved)
        return call['id']

    def incident(self, gateway=False):
        owner, source = self.scheduled_native()
        self.claim(owner, 't_existing', run=10)
        create = self.receipt(owner, 'cronjob_manage', dict(action='create', schedule='1h'),
                              dict(success=True, job_id='job'))
        reporter = dict(owner, id=str(uuid4()), status='working', source_task_id=None,
            source_bot_id=None, parent_task_id=None, _reply_to_task_id=None,
            _native_turn_id='report-input', created_at=now(), updated_at=now(),
            routine_id=str(uuid5(UUID(owner['bot_id']), 'cron:job')),
            _native_job=dict(id='job', fire_claim={'by': 'occurrence'}))
        reporter['_execution_session_id'] = reporter['id']
        reporter.pop('_work_todos', None)
        reporter.pop('_waiting_on', None)
        self.claim(reporter, 't_report', run=11)
        self.service._event('routine.triggered', dict(routine_id=reporter['routine_id'],
                            task_id=reporter['id']), reporter['bot_id'], reporter['id'])
        completion = self.receipt(reporter, 'kanban_complete', {},
                                  dict(ok=True, task_id='t_report', run_id=11))
        # In the gateway, coconut_message_bot saves the delivery of the report under its own ID. Otherwise the saved
        # state of the report that an earlier Coconut delivered through its native relay: the delivery under its
        # envelope ID and the relay of the coordinator. Coconut no longer makes that state, but it reads it.
        envelope = 'a' * 32
        with self.service.transaction():
            relay = self.service._store_bot_message(reporter['id'], dict(target_bot_id=source['bot_id'], text='Capacity report'))
            delivery = next(d for d in reversed(self.service._all('deliveries')) if d['receipt']['task_id'] == relay['task_id'])
            if not gateway:
                self.service.db.execute('DELETE FROM deliveries WHERE id=?', (delivery['id'],))
                self.service._put('deliveries', dict(delivery, id=digest(encoded([reporter['id'], envelope]))))
        coordinator = self.service._get('tasks', relay['task_id'])
        if not gateway:
            coordinator['_native_relay'] = dict(sender_bot_id=reporter['bot_id'], envelope_id=envelope)
        coordinator.update(status='completed', _conversation_epoch=owner['_conversation_epoch'],
            _native_turn_id='coordinator-input', updated_at=now())
        coordinator['_execution_session_id'] = coordinator['id']
        self.claim(coordinator, 't_coord', run=12)
        reporter = self.service._get('tasks', reporter['id'])
        reporter.update(status='waiting')
        self.service._put('tasks', reporter)
        with self.service.transaction():
            result = self.service._store_bot_message(coordinator['id'], dict(
                target_bot_id=reporter['bot_id'], text='Completed report', result_status='completed'), returning=True)
        returned = self.service._get('tasks', result['task_id'])
        returned.pop('_conversation_epoch', None)
        self.service._put('tasks', returned)
        # The original requester is the authenticated foreground invocation.
        source['_native_turn_id'] = 'caller-input'
        source['_waiting_on'] = []  # Original owner already returned before this report.
        self.claim(source, 't_source', status='running', run=13)
        for identity, task, activity in (('owner-pr', owner, reporter), ('requester-pr', source, coordinator)):
            self.service._put('artifacts', dict(id=identity, kind='pr', task_id=task['id'],
                bot_id=task['bot_id'], title='Existing PR', status='merged', pr_state='merged',
                url='https://github.com/example/project/pull/1', _review_task_id=task['id'],
                _activity_task_id=activity['id'], created_at=now(), observed_at=now()))
        return owner, source, reporter, coordinator, returned, create, completion

    def test_exact_report_resumes_original_once_and_consumes_only_its_wait(self):
        owner, caller, reporter, coordinator, returned, _, _ = self.incident()
        snapshots = copy.deepcopy(self.service.kanban.tasks)
        result = self.resume(caller)
        self.assertEqual(result['task_id'], owner['id'])
        self.assertEqual(self.resume(caller), result)
        resumed = self.service._get('tasks', owner['id'])
        for field in ('id', '_execution_session_id', 'source_task_id', 'source_bot_id', '_kanban_claim'):
            self.assertEqual(resumed[field], owner[field])
        self.assertEqual(resumed['status'], 'queued')
        report = self.service._get('tasks', reporter['id'])
        self.assertEqual(report['_waiting_on'], [])
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(len(report['_routine_report_returns']), 1)
        self.assertEqual(self.service._get('tasks', returned['id'])['status'], 'completed')
        self.assertEqual(self.service.kanban.tasks, snapshots)

    def test_the_delivery_of_coconut_message_bot_proves_the_report(self):
        owner, caller, reporter, coordinator, returned, _, _ = self.incident(gateway=True)
        self.assertNotIn('_native_relay', coordinator)
        delivery, = [d for d in self.service._all('deliveries') if d['receipt']['task_id'] == coordinator['id']]
        # A progress note to the coordinator proves nothing.
        self.service._put('deliveries', dict(delivery, _progress_only=True))
        self.reject_unchanged(caller, owner, reporter, returned)
        self.service._put('deliveries', delivery)
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', reporter['id'])['status'], 'completed')

    def test_unproved_create_receipt_and_occurrence_fail_closed(self):
        for case in ('missing', 'wrong-job', 'wrong-owner', 'hash', 'duplicate', 'trigger', 'epoch'):
            with self.subTest(case=case):
                self.tearDown(); self.setUp()
                owner, caller, reporter, coordinator, returned, create, _ = self.incident()
                record = self.service._get('native_receipts', create)
                if case == 'missing':
                    self.service.db.execute('DELETE FROM native_receipts WHERE id=?', (create,))
                elif case == 'trigger':
                    self.service.db.execute("DELETE FROM events WHERE type='routine.triggered'")
                elif case == 'epoch':
                    self.service._put('tasks', dict(reporter, _conversation_epoch='earlier'))
                else:
                    if case == 'wrong-job': record['result'] = json.dumps(dict(success=True, job_id='other'))
                    if case == 'wrong-owner': record['task_id'] = caller['id']
                    if case == 'hash': record['action_hash'] = 'wrong'
                    if case == 'duplicate': record['id'] = str(uuid4())
                    self.service._put('native_receipts', record)
                self.reject_unchanged(caller, owner, reporter, returned)

    def reject_unchanged(self, caller, owner, reporter, returned):
        before = {t['id']: self.service._get('tasks', t['id']) for t in (owner, reporter, returned)}
        with self.assertRaises(ServiceError):
            self.resume(caller)
        self.assertEqual({i: self.service._get('tasks', i) for i in before}, before)

    def test_live_claim_wait_unknown_effect_and_decision_are_preserved(self):
        for case in ('claim', 'wait', 'unknown', 'decision', 'paused', 'return', 'context', 'steering'):
            with self.subTest(case=case):
                self.tearDown(); self.setUp()
                owner, caller, reporter, coordinator, returned, _, _ = self.incident()
                if case == 'claim': self.service.kanban.tasks[reporter['id']].update(status='running', current_run_id=99)
                elif case == 'wait': self.service._put('tasks', dict(reporter, _waiting_on=[coordinator['id'], 'independent']))
                elif case == 'unknown':
                    self.service._put('native_receipts', dict(id='unknown', task_id=reporter['id'],
                        turn_id='report-input', state='executing', action=dict(name='terminal', arguments={})))
                elif case == 'decision': self.service._put('decisions', dict(id='pending', task_id=reporter['id'], status='pending'))
                elif case == 'paused': self.service._put('tasks', dict(reporter, status='paused'))
                elif case == 'return': self.service._put('tasks', dict(coordinator, _return_pending={'text': 'undelivered'}))
                elif case == 'context': self.service._put('contexts', dict(id='context', task_id=reporter['id'], status='working'))
                elif case == 'steering': self.service._put('tasks', dict(reporter, _steering=['new input']))
                self.reject_unchanged(caller, owner, reporter, returned)

    def test_cross_bot_and_cross_task_callback_proof_is_rejected(self):
        for field, value in (('source_task_id', 'other'), ('source_bot_id', 'other'),
                             ('_assignment_task_id', 'other'), ('_resolved_dependency_id', 'other')):
            with self.subTest(field=field):
                self.tearDown(); self.setUp()
                owner, caller, reporter, coordinator, returned, _, _ = self.incident()
                self.service._put('tasks', dict(returned, **{field: value}))
                self.reject_unchanged(caller, owner, reporter, returned)

    def test_original_owner_unknown_tool_blocks_report_consumption(self):
        for name in ('terminal', 'delegate_task', 'cronjob_manage'):
            with self.subTest(name=name):
                self.tearDown(); self.setUp()
                owner, caller, reporter, coordinator, returned, _, _ = self.incident()
                self.service._put('tasks', dict(owner, status='working'))
                self.service.emit(owner['id'], 'native_call', dict(name=name, arguments={}))
                self.service._put('tasks', owner)
                with self.assertRaisesRegex(ServiceError, 'original owner to finish its tool call'):
                    self.resume(caller)
                self.reject_unchanged(caller, owner, reporter, returned)

    def test_legacy_claim_without_retained_input_accepts_only_same_native(self):
        owner, caller, reporter, coordinator, returned, _, _ = self.incident()
        for task in (reporter, coordinator, caller):
            self.service.kanban.tasks[task['id']]['input_id'] = None
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])

    def test_changed_retained_input_is_rejected(self):
        owner, caller, reporter, coordinator, returned, _, _ = self.incident()
        self.service.kanban.tasks[reporter['id']]['input_id'] = 'another-input'
        self.reject_unchanged(caller, owner, reporter, returned)

    def test_dispatch_capture_or_uncertainty_preserves_callback(self):
        for field in ('_kanban_claiming', '_kanban_dispatch_uncertain'):
            with self.subTest(field=field):
                self.tearDown(); self.setUp()
                owner, caller, reporter, coordinator, returned, _, _ = self.incident()
                setattr(self.service, field, 1)
                self.reject_unchanged(caller, owner, reporter, returned)

    def test_claimed_consumer_and_newer_producer_result_are_not_consumed(self):
        for case in ('consumer', 'new-result', 'failed-producer', 'missing-completion', 'reviewer'):
            with self.subTest(case=case):
                self.tearDown(); self.setUp()
                owner, caller, reporter, coordinator, returned, _, completion = self.incident()
                if case == 'consumer':
                    self.claim(returned, 't_consumer', status='running')
                elif case == 'new-result':
                    self.service._put('tasks', dict(coordinator, updated_at=now()))
                elif case == 'failed-producer':
                    self.service._put('tasks', dict(coordinator, status='failed'))
                elif case == 'missing-completion':
                    self.service.db.execute('DELETE FROM native_receipts WHERE id=?', (completion,))
                elif case == 'reviewer':
                    self.service.kanban.tasks[caller['id']]['claim_source_status'] = 'review'
                self.reject_unchanged(caller, owner, reporter, returned)

    def test_unrelated_same_bot_report_and_foreground_wait_remain_guarded(self):
        for case in ('peer', 'caller-wait', 'sibling'):
            with self.subTest(case=case):
                self.tearDown(); self.setUp()
                owner, caller, reporter, coordinator, returned, _, _ = self.incident()
                if case == 'caller-wait':
                    self.service._put('tasks', dict(caller, _waiting_on=['independent']))
                else:
                    other = dict(reporter, id=str(uuid4()), status='working')
                    other['_execution_session_id'] = other['id']
                    other.pop('_kanban_claim', None)
                    other.pop('_waiting_on', None)
                    if case == 'sibling': other['_assignment_task_id'] = coordinator['id']
                    self.service._put('tasks', other)
                    if case == 'peer':
                        peer = self.service._get('artifacts', 'owner-pr')
                        peer.update(id='independent-pr', task_id=other['id'], _review_task_id=other['id'],
                                    _activity_task_id=other['id'])
                        self.service._put('artifacts', peer)
                self.reject_unchanged(caller, owner, reporter, returned)

    def test_one_reporter_keeps_each_pr_requester_scope_distinct(self):
        owner, caller, reporter, coordinator, returned, _, _ = self.incident()
        work = dict(id='shared-work', kind='work')
        first = self.service._get('artifacts', 'owner-pr')
        first['work_outcome_id'] = work['id']
        second = dict(first, id='second-owner-pr', url='https://github.com/example/project/pull/2')
        requester = self.service._get('artifacts', 'requester-pr')
        other = dict(requester, id='second-requester-pr', url=second['url'])
        peers = [first, second, requester, other]
        tasks = {task['id']: task for task in self.service._all('tasks')}
        result = routine_reports.scopes(self.service, owner, work, peers, tasks,
                                       self.service._all('deliveries'))
        for peer in (first, second, requester, other):
            self.assertIsNotNone(result.get((peer['id'], peer['_activity_task_id'])))

    def test_overwritten_routine_report_and_its_coordinator_remain_guarded(self):
        for case in ('reporter', 'coordinator'):
            with self.subTest(case=case):
                self.tearDown(); self.setUp()
                owner, caller, reporter, coordinator, returned, _, _ = self.incident()
                self.receipt(owner, 'cronjob_manage', dict(action='create', schedule='1h'),
                             dict(success=True, job_id='earlier-job'))
                earlier = dict(reporter, id=str(uuid4()), status='working', created_at=now(),
                    _native_job=dict(id='earlier-job', fire_claim={'by': 'earlier-occurrence'}),
                    routine_id=str(uuid5(UUID(owner['bot_id']), 'cron:earlier-job')))
                earlier['_execution_session_id'] = earlier['id']
                earlier.pop('_waiting_on', None)
                self.claim(earlier, 't_earlier', status='running', run=30)
                self.service._event('routine.triggered', dict(routine_id=earlier['routine_id'],
                    task_id=earlier['id']), earlier['bot_id'], earlier['id'])
                if case == 'coordinator':
                    with self.service.transaction():
                        relay = self.service._store_bot_message(earlier['id'], dict(
                            target_bot_id=caller['bot_id'], text='Earlier report', native_envelope_id='b' * 32))
                    other = self.service._get('tasks', relay['task_id'])
                    other.update(status='working', _conversation_epoch=owner['_conversation_epoch'])
                    self.claim(other, 't_other', status='running', run=31)
                    earlier = self.service._get('tasks', earlier['id'])
                    earlier['status'] = 'completed'
                    self.claim(earlier, 't_earlier', run=30)
                # The current artifact names only the later reporter.
                self.assertEqual(self.service._get('artifacts', 'owner-pr')['_activity_task_id'], reporter['id'])
                self.reject_unchanged(caller, owner, reporter, returned)

    def test_journal_replay_after_database_reopen_is_idempotent(self):
        owner, caller, reporter, coordinator, returned, _, _ = self.incident()
        result = self.resume(caller)
        database = self.service.db.execute('PRAGMA database_list').fetchone()[2]
        import sqlite3
        self.service.db.close()
        self.service.db = sqlite3.connect(database, check_same_thread=False, isolation_level=None)
        self.assertEqual(self.resume(caller), result)
        self.assertEqual(len(self.service._get('tasks', reporter['id'])['_routine_report_returns']), 1)

    def test_native_claim_context_is_prompt_not_another_owner(self):
        owner, caller, reporter, coordinator, returned, _, _ = self.incident()
        caller['_kanban_claim']['context'] = '# Saved native worker context'
        self.service._put('tasks', caller)
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])

    def test_continuation_receipt_uses_actual_service_admission(self):
        owner, caller, reporter, coordinator, returned, _, _ = self.incident()
        payload = dict(task_id='t_existing', message='Restore the original owner.')
        # The turn's coconut_continue_work reaches the service as the work_continue event.
        result = self.service.emit(caller['id'], 'work_continue', payload)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['_execution_session_id'], owner['_execution_session_id'])


if __name__ == '__main__':
    unittest.main()
