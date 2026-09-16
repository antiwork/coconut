import unittest
from uuid import uuid4

import test_automatic_work as fixtures
from coconut.service import Service, ServiceError


class ExternalWorkWaitTests(unittest.TestCase):
    setUp = fixtures.AutomaticWorkTests.setUp
    tearDown = fixtures.AutomaticWorkTests.tearDown
    start = fixtures.AutomaticWorkTests.start
    sync = fixtures.AutomaticWorkTests.sync
    write = fixtures.AutomaticWorkTests.write

    def report_wait(self):
        self.start()
        self.service._put('routines', dict(id='intake', bot_id=self.bot['id'], _native_id='native-intake',
            name='Intake', enabled=True, next_run_at='2099-01-01T00:00:00Z'))
        self.payload = dict(key='payouts', title='Recover missing payouts', status='waiting',
            activity='Waiting for bank receipts', next_step='Check the existing issue for receipts',
            external_wait=dict(job_id='native-intake', source='Existing issue #2639'))
        item = self.service._register_work(self.task['id'], self.payload)
        self.start(status='completed', summary='Complete.',
            _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Verify the saved records')]})
        self.service.running.clear()
        return item

    def test_external_wait_survives_recovery_ticks_and_restart(self):
        item = self.report_wait()
        before = len(self.service._all('tasks'))
        for _ in range(3):
            self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)
        row = self.sync()[0]
        self.assertEqual((row['id'], row['work_state'], row['activity']),
                         (item['id'], 'waiting', 'Waiting for bank receipts'))
        self.assertIsNone(row['completion_evidence'])
        self.assertEqual(self.service._all('decisions'), [])
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.tracker)
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(self.sync()[0]['work_state'], 'waiting')

    def test_disabled_or_deleted_routine_does_not_hide_unfinished_work(self):
        item = self.report_wait()
        routine = self.service._get('routines', 'intake')
        routine['enabled'] = False
        self.service._put('routines', routine)
        self.assertEqual(self.sync()[0]['work_state'], 'needs_recovery')
        self.start()
        with self.assertRaises(ServiceError):
            self.service._register_work(self.task['id'], self.payload)
        self.service.db.execute('DELETE FROM routines')
        with self.assertRaises(ServiceError):
            self.service._register_work(self.task['id'], self.payload)

    def test_other_bot_routine_and_failed_worker_are_not_external_waits(self):
        self.report_wait()
        self.start()
        routine = self.service._get('routines', 'intake')
        routine['bot_id'] = 'another-bot'
        self.service._put('routines', routine)
        with self.assertRaises(ServiceError):
            self.service._register_work(self.task['id'], self.payload)
        routine['bot_id'] = self.bot['id']
        self.service._put('routines', routine)
        self.start(status='failed', summary='Tool result unknown')
        self.service.running.clear()
        self.assertEqual(self.sync()[0]['work_state'], 'needs_recovery')

    def test_routine_receives_bound_work_even_outside_the_recent_list(self):
        item = self.report_wait()
        for i in range(35):
            self.service._put('artifacts', dict(item, id=str(uuid4()), _work_key='other-' + str(i),
                external_wait=None, observed_at='2099-01-01T00:00:00Z'))
        task = dict(self.service._get('tasks', self.task['id']), _native_job={'id': 'native-intake'})
        rows = self.service._tracked_items_input(task)
        self.assertEqual(rows[0]['key'], 'payouts')
        self.assertTrue(rows[0]['routine_check'])
        self.assertEqual(rows[0]['external_wait']['source'], 'Existing issue #2639')
        self.assertEqual(sum(r['routine_check'] for r in rows), 1)

    def test_unchanged_routine_check_preserves_owner_and_new_evidence_completes_work(self):
        item = self.report_wait()
        task = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Scheduled intake', mode='queue'))
        stored = self.service._get('tasks', task['id'])
        stored.update(status='working', _native_job={'id': 'native-intake'}, _native_turn_id='routine-turn',
                      _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', stored)
        updated = self.service._register_work(task['id'], self.payload)
        self.assertEqual(updated['task_id'], self.task['id'])
        self.assertEqual(self.sync()[0]['work_state'], 'waiting')
        self.service._register_work(task['id'], dict(self.payload, status='completed', external_wait=None,
            completion_evidence='Bank receipt confirms all missing payouts arrived'))
        self.assertEqual(self.sync()[0]['work_state'], 'completed')
        self.assertNotIn('external_wait', self.service._get('artifacts', item['id']))

    def test_plain_wait_and_new_work_do_not_inherit_external_wait(self):
        item = self.report_wait()
        self.start()
        self.service._register_work(self.task['id'], dict(self.payload, status='working', external_wait=None))
        self.start(status='completed')
        self.service.running.clear()
        self.assertEqual(self.sync()[0]['work_state'], 'needs_recovery')
        self.assertNotIn('external_wait', self.service._get('artifacts', item['id']))

    def test_cancelled_owner_or_exchange_is_not_a_routine_check(self):
        self.report_wait()
        task = dict(self.service._get('tasks', self.task['id']), _native_job={'id': 'native-intake'})
        self.write('POST', '/v1/tasks/' + self.task['id'] + '/cancel', {})
        self.assertFalse(any(r['routine_check'] for r in self.service._tracked_items_input(task)))
        owner = self.service._get('tasks', self.task['id'])
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        exchange = self.service._get('exchanges', owner['exchange_id'])
        exchange['cancelled'] = True
        self.service._put('exchanges', exchange)
        self.assertFalse(any(r['routine_check'] for r in self.service._tracked_items_input(task)))

    def delegated_wait(self):
        parent = self.report_wait()
        self.start()
        bot = self.write('POST', '/v1/bots', dict(name='Helper', role='Check receipts'))
        sent = self.service._store_bot_message(self.task['id'],
            dict(target_bot_id=bot['id'], text='Check receipts', require_response=True))
        child = self.service._get('tasks', sent['task_id'])
        child.update(status='working', _native_relay=True, _native_turn_id='child-turn',
                     _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', child)
        self.service._put('routines', dict(id='child-intake', bot_id=bot['id'], _native_id='child-native',
            name='Check receipts', enabled=True, next_run_at='2099-01-01T00:00:00Z'))
        payload = dict(self.payload, external_wait=dict(job_id='child-native', source='Existing receipt thread'))
        item = self.service._register_work(child['id'], payload)
        self.assertTrue(self.service._get('artifacts', item['id'])['_hidden'])
        child['status'] = 'completed'
        self.service._put('tasks', child)
        self.start(status='completed', _waiting_on=[])
        self.service.running.clear()
        original = self.service._get('artifacts', parent['id'])
        original.pop('external_wait', None)
        self.service._put('artifacts', original)
        routine_task = dict(child, _native_job={'id': 'child-native'})
        return parent, item, child, routine_task, payload

    def test_hidden_delegated_wait_reaches_its_routine_and_keeps_parent_waiting(self):
        parent, item, child, routine_task, payload = self.delegated_wait()
        rows = self.service._tracked_items_input(routine_task)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['routine_check'])
        self.assertEqual(rows[0]['external_wait']['source'], 'Existing receipt thread')
        self.assertTrue(self.service._get('artifacts', item['id'])['_hidden'])
        self.assertEqual(self.sync()[0]['work_state'], 'waiting')
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)

    def test_routine_completion_keeps_delegated_work_hidden(self):
        parent, item, child, _, payload = self.delegated_wait()
        sent = self.write('POST', '/v1/bots/' + child['bot_id'] + '/messages', dict(text='Intake', mode='queue'))
        routine = self.service._get('tasks', sent['id'])
        routine.update(status='working', _native_job={'id': 'child-native'})
        self.service._put('tasks', routine)
        self.service._register_work(routine['id'], dict(payload, status='completed', external_wait=None,
            completion_evidence='The bank receipt confirms all funds arrived'))
        saved = self.service._get('artifacts', item['id'])
        self.assertEqual(saved['task_id'], child['id'])
        self.assertEqual(saved['_work_parent_id'], parent['id'])
        self.assertTrue(saved['_hidden'])
        self.assertNotIn('external_wait', saved)
        context = self.service.work_context_input(parent['id'])
        self.assertIn(routine['id'], {t['id'] for t in context['tasks']})
        related = next(a for a in context['related_work'] if a['id'] == item['id'])
        self.assertEqual(related['completion_evidence'], 'The bank receipt confirms all funds arrived')
        self.assertNotEqual(context['work_state'], 'completed')

    def test_parent_cancellation_excludes_hidden_wait_with_shared_owner(self):
        from coconut.service import encoded
        parent, item, child, routine_task, _ = self.delegated_wait()
        # A shared assignment can continue after one of its outcomes is cancelled.
        self.service.db.execute('INSERT INTO settings(key,value) VALUES (?,?)',
            ('work.cancelled.work:' + parent['id'], encoded('cancelled')))
        self.assertEqual(self.service._get('tasks', child['id'])['status'], 'completed')
        self.assertFalse(any(r['routine_check'] for r in self.service._tracked_items_input(routine_task)))

    def test_routine_progress_preserves_delegated_wait_until_the_check_finishes(self):
        import threading
        parent, item, child, _, payload = self.delegated_wait()
        sent = self.write('POST', '/v1/bots/' + child['bot_id'] + '/messages', dict(text='Intake', mode='queue'))
        routine = self.service._get('tasks', sent['id'])
        routine.update(status='working', _native_job={'id': 'child-native'}, _native_turn_id='routine-turn',
                       _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', routine)
        self.service.running[routine['id']] = threading.current_thread()
        report = dict(payload, status='working', activity='Reading new bank receipts')
        report.pop('external_wait')
        self.service._register_work(routine['id'], report)
        saved = self.service._get('artifacts', item['id'])
        self.assertEqual(saved['task_id'], child['id'])
        self.assertTrue(saved['_hidden'])
        self.assertEqual(saved['external_wait'], payload['external_wait'])
        self.assertEqual(self.sync()[0]['work_state'], 'working')
        self.service._register_work(routine['id'], payload)
        self.service.running.clear()
        self.assertEqual(self.sync()[0]['work_state'], 'waiting')
        self.assertTrue(self.service._get('artifacts', item['id'])['_hidden'])
        self.assertNotIn('_external_wait_task_id', self.service._get('artifacts', item['id']))

    def test_routine_callback_keeps_binding_and_routine_input(self):
        parent, item, child, _, payload = self.delegated_wait()
        sent = self.write('POST', '/v1/bots/' + child['bot_id'] + '/messages', dict(text='Intake', mode='queue'))
        routine = self.service._get('tasks', sent['id'])
        routine.update(status='waiting', _native_job={'id': 'child-native'})
        self.service._put('tasks', routine)
        callback = dict(routine, id=str(uuid4()), status='working', _assignment_task_id=routine['id'],
                        _is_return=True, source_task_id=child['id'])
        callback.pop('_native_job')
        self.service._put('tasks', callback)
        self.assertTrue(self.service._tracked_items_input(callback)[0]['routine_check'])
        report = dict(payload, status='working', activity='Checking receipts')
        report.pop('external_wait')
        self.service._register_work(callback['id'], report)
        rows = self.service._tracked_items_input(callback)
        self.assertEqual(rows[0]['key'], payload['key'])
        self.assertTrue(rows[0]['current_task'])
        self.assertEqual(rows[0]['external_wait'], payload['external_wait'])
        self.service._register_work(callback['id'], payload)
        saved = self.service._get('artifacts', item['id'])
        self.assertEqual(saved['task_id'], child['id'])
        self.assertTrue(saved['_hidden'])
        self.service._register_work(callback['id'], dict(payload, status='completed', external_wait=None,
            completion_evidence='Verified all funds arrived'))
        self.assertTrue(self.service._get('artifacts', item['id'])['_hidden'])
        context = self.service.work_context_input(parent['id'])
        self.assertTrue({routine['id'], callback['id']} <= {t['id'] for t in context['tasks']})

    def test_cancelling_parent_steers_active_routine_check_without_stopping_intake(self):
        import threading
        parent, item, child, _, payload = self.delegated_wait()
        sent = self.write('POST', '/v1/bots/' + child['bot_id'] + '/messages', dict(text='Intake', mode='queue'))
        routine = self.service._get('tasks', sent['id'])
        routine.update(status='working', _native_job={'id': 'child-native'}, _native_turn_id='routine-turn',
                       _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', routine)
        self.service.running[routine['id']] = threading.current_thread()
        report = dict(payload, status='working', activity='Reading receipts')
        report.pop('external_wait')
        self.service._register_work(routine['id'], report)
        report.update(status='waiting', activity='Waiting for the existing check')
        self.service._register_work(routine['id'], report)
        self.assertEqual(self.service._get('artifacts', item['id'])['_external_wait_task_id'], routine['id'])
        self.service._cancel_work(parent['id'], [])
        saved = self.service._get('tasks', routine['id'])
        self.assertEqual(saved['status'], 'working')
        self.assertIn(parent['id'], saved['_steering'][-1])
        self.assertIn('Continue the other requested work.', saved['_steering'][-1])
        self.assertEqual(self.sync()[0]['work_state'], 'cancelled')
        with self.assertRaises(ServiceError):
            self.service._register_work(routine['id'], payload)

    def test_completed_handoff_keeps_external_wait_after_parent_exchange_changes(self):
        parent, item, child, _, _ = self.delegated_wait()
        owner = self.service._get('tasks', parent['task_id'])
        owner.update(exchange_id=self.service._new_exchange(), _waiting_on=[])
        self.service._put('tasks', owner)
        self.assertEqual(self.sync()[0]['work_state'], 'waiting')
        self.assertTrue(self.service._get('artifacts', item['id'])['_hidden'])
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)

    def test_recovery_includes_failed_routine_receipts_without_repeating_them(self):
        item = self.report_wait()
        sent = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Intake', mode='queue'))
        routine = self.service._get('tasks', sent['id'])
        routine.update(status='working', _native_job={'id': 'native-intake'}, _native_turn_id='routine-turn',
                       _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', routine)
        report = dict(self.payload, status='working', activity='Reading receipts')
        report.pop('external_wait')
        self.service._register_work(routine['id'], report)
        receipt = self.service.native_event(routine['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'read existing receipt'}))
        routine.update(status='failed', summary='Receipt result was lost')
        self.service._put('tasks', routine)
        context = self.service.work_context_input(item['id'])
        self.assertIn(routine['id'], {t['id'] for t in context['tasks']})
        self.service._queue_work_recoveries()
        recovered = self.service._all('tasks')[-1]
        self.assertIn(receipt['id'], {r['id'] for r in recovered['_self_recovery']['unknown_tools']})
        self.assertIn(routine['id'], {t['id'] for t in recovered['_self_recovery']['saved_tasks']})
        saved = self.service._get('artifacts', item['id'])
        self.assertIn(routine['id'], saved['_work_previous_task_ids'])
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'executing')

    def test_parent_recovery_keeps_hidden_delegate_and_routine_assignment_context(self):
        parent, item, child, _, payload = self.delegated_wait()
        sent = self.write('POST', '/v1/bots/' + child['bot_id'] + '/messages', dict(text='Intake', mode='queue'))
        routine = self.service._get('tasks', sent['id'])
        routine.update(status='working', _native_job={'id': 'child-native'}, _native_turn_id='routine-turn',
                       _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', routine)
        receipt = self.service.native_event(routine['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'read saved receipts'}))
        routine['status'] = 'completed'
        self.service._put('tasks', routine)
        callback = dict(routine, id=str(uuid4()), status='working', _assignment_task_id=routine['id'], _is_return=True)
        callback.pop('_native_job')
        self.service._put('tasks', callback)
        report = dict(payload, status='working', activity='Reading receipt results')
        report.pop('external_wait')
        self.service._register_work(callback['id'], report)
        callback.update(status='failed', summary='The receipt result is missing')
        self.service._put('tasks', callback)
        self.service._queue_work_recoveries()
        recovery = self.service._all('tasks')[-1]
        self.assertIn(receipt['id'], {r['id'] for r in recovery['_self_recovery']['unknown_tools']})
        self.assertEqual(self.sync()[0]['work_state'], 'queued')
        self.assertTrue(self.service._get('artifacts', item['id'])['_hidden'])
        self.assertEqual(self.service._get('artifacts', item['id'])['_work_parent_id'], parent['id'])
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)

        recovery.update(status='working', _native_turn_id='verified-recovery')
        self.service._put('tasks', recovery)
        process = self.service.native_event(recovery['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'read saved verification'}))
        self.service.native_event(recovery['id'], 'native_result', dict(id=process['id'],
            result={'session_id': 'recovery-check', 'notify_on_complete': True}))
        recovery['status'] = 'waiting'
        self.service._put('tasks', recovery)
        self.assertEqual(self.sync()[0]['work_state'], 'waiting')
        for _ in range(3):
            self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)
        process = self.service._get('native_receipts', process['id'])
        process['_process_delivery'] = 'consumed'
        self.service._put('native_receipts', process)

        recovery['status'] = 'working'
        self.service._put('tasks', recovery)
        # An assignment can still own a result when one of its callbacks fails.
        receipt = self.service._get('native_receipts', receipt['id'])
        receipt.update(state='completed', result={'session_id': 'other-result', 'notify_on_complete': True})
        self.service._put('native_receipts', receipt)
        self.service._register_work(recovery['id'], self.payload)
        self.assertEqual(self.service._get('artifacts', item['id'])['_external_wait_task_id'], callback['id'])
        receipt['_process_delivery'] = 'consumed'
        self.service._put('native_receipts', receipt)
        self.service._register_work(recovery['id'], self.payload)
        recovery.update(status='completed', summary='The records check is complete')
        self.service._put('tasks', recovery)
        self.assertEqual(self.sync()[0]['work_state'], 'waiting')
        saved_child = self.service._get('artifacts', item['id'])
        self.assertNotIn('_external_wait_task_id', saved_child)
        self.assertTrue({routine['id'], callback['id']} <= set(saved_child['_work_previous_task_ids']))
        self.assertFalse(any(r['routine_check'] for r in self.service._tracked_items_input(dict(routine))))
        for _ in range(3):
            self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(self.service._all('decisions'), [])

    def repaired_recovery_card(self):
        item = self.report_wait()
        failed = self.start(status='failed', _self_recovery={'attempt': 2})
        self.service.running.clear()
        from test_work_self_recovery import legacy_recovery_card
        legacy_recovery_card(self.service, failed, [item])
        decision = self.service._all('decisions')[-1]
        self.assertEqual(decision['status'], 'pending')
        sent = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='Repair the existing wait record', mode='queue', work_artifact_id=item['id']))
        repair = self.service._get('tasks', sent['id'])
        repair.update(status='working', _native_turn_id='repair',
                      _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', repair)
        self.service._register_work(repair['id'], self.payload)
        repair['status'] = 'completed'
        self.service._put('tasks', repair)
        return item, decision, repair

    def test_new_external_wait_retires_obsolete_recovery_card_without_another_turn(self):
        item, decision, repair = self.repaired_recovery_card()
        before = len(self.service._all('tasks'))
        messages = self.service._all('messages')
        for _ in range(3):
            self.service._queue_work_recoveries()
        resolved = self.service._get('decisions', decision['id'])
        self.assertEqual(resolved['status'], 'resolved')
        self.assertEqual(resolved['resolution'], 'work_scope_changed')
        self.assertFalse(resolved.get('selected_option_id'))
        self.assertEqual(self.service._get('tasks', decision['task_id'])['status'], 'completed')
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(self.service._all('messages'), messages)
        self.assertEqual(self.sync()[0]['work_state'], 'waiting')
        self.assertIsNone(self.sync()[0]['completion_evidence'])

    def test_legacy_recovery_card_retires_even_without_an_external_wait(self):
        item, decision, repair = self.repaired_recovery_card()
        other = dict(self.service._get('artifacts', item['id']), id=str(uuid4()),
                     _work_key='another-outcome', external_wait=None, status='blocked')
        self.service._put('artifacts', other)
        decision['_recovery_work_ids'].append(other['id'])
        self.service._put('decisions', decision)
        self.service._retire_obsolete_recovery_questions()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'resolved')

    def test_ordinary_and_revised_questions_do_not_use_automatic_retirement(self):
        item, decision, repair = self.repaired_recovery_card()
        decision['revision'] += 1
        self.service._put('decisions', decision)
        self.service._retire_obsolete_recovery_questions()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        decision['revision'] = 1
        decision.pop('_recovery_work_ids')
        self.service._put('decisions', decision)
        self.service._retire_obsolete_recovery_questions()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_failed_human_reply_keeps_its_recovery_card(self):
        item, decision, repair = self.repaired_recovery_card()
        task = self.service._get('tasks', decision['task_id'])
        self.service._request_context(decision, task, 'Stop this work', [], reply_device='device')
        context = self.service._all('contexts')[-1]
        context['status'] = 'failed'
        self.service._put('contexts', context)
        self.service._retire_obsolete_recovery_questions()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'needs_you')
