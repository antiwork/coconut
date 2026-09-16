import unittest
import json
import threading
from datetime import datetime, timezone

import test_delegated_work_duplicates as fixtures
from coconut.service import ServiceError


class WorkOutcomeAliasTests(unittest.TestCase):
    setUp = fixtures.DelegatedWorkDuplicatesTests.setUp
    tearDown = fixtures.DelegatedWorkDuplicatesTests.tearDown
    track = fixtures.DelegatedWorkDuplicatesTests.track
    child = fixtures.DelegatedWorkDuplicatesTests.child
    write = fixtures.DelegatedWorkDuplicatesTests.write

    def report(self, task, parent):
        return self.service._register_work(task['id'], dict(key='worker-export', same_outcome_as=parent['id'],
            title='Export account data', activity='Verifying export', status='working'))

    def test_explicit_same_outcome_reuses_parent_despite_different_key(self):
        parent = self.track()
        task, child = self.child(key='worker-export')
        self.report(task, parent)
        self.service.running[task['id']] = threading.current_thread()
        try:
            rows = [a for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work']
            self.assertEqual([a['id'] for a in rows], [parent['id']])
            self.assertEqual(rows[0]['work_state'], 'working')
            self.assertEqual(rows[0]['activity'], 'Verifying export')
            self.assertEqual(self.service._get('artifacts', child['id'])['_work_parent_id'], parent['id'])
        finally:
            self.service.running.clear()

    def test_unrelated_task_cannot_hide_its_work_under_another_outcome(self):
        parent = self.track()
        task, _ = self.child(linked=False, key='worker-export')
        with self.assertRaises(ServiceError): self.report(task, parent)
        self.assertEqual(len([a for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work']), 2)

    def test_delegate_receives_parent_identity_and_completion_scope(self):
        parent = self.track(completion_condition='Verify the exported file contains all records.')
        task, _ = self.child(key='worker-export')
        items = self.service._tracked_items_input(task)
        inherited = next(a for a in items if a['id'] == parent['id'])
        self.assertEqual(inherited['completion_condition'], 'Verify the exported file contains all records.')
        self.assertTrue(inherited['current_task'])

    def test_delegate_keeps_parent_context_after_parent_callback(self):
        parent = self.track()
        task, _ = self.child(key='worker-export')
        self.report(task, parent)
        owner = self.service._get('tasks', parent['task_id'])
        callback = dict(owner, id='parent-callback', _assignment_task_id=owner['id'],
                        created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        self.service._register_work(callback['id'], dict(key='account-export', title='Export account data',
            activity='Checking the export', status='working'))
        inherited = next(a for a in self.service._tracked_items_input(task) if a['id'] == parent['id'])
        self.assertTrue(inherited['current_task'])


    def test_reporting_callback_keeps_ownership_across_an_exchange_change(self):
        parent = self.track()
        owner = self.service._get('tasks', parent['task_id'])
        callback = dict(owner, id='older-callback', _assignment_task_id=owner['id'],
                        created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        self.service._register_work(callback['id'], dict(key='account-export', title='Export account data',
            activity='Checking the export', status='working'))
        owner.update(exchange_id=self.service._new_exchange(), _waiting_on=[])
        self.service._put('tasks', owner)
        row = next(a for a in self.service._tracked_items_input(callback) if a['id'] == parent['id'])
        self.assertTrue(row['current_task'])

    def test_delegate_can_link_to_parent_reporting_in_an_older_exchange(self):
        parent = self.track()
        owner = self.service._get('tasks', parent['task_id'])
        callback = dict(owner, id='older-parent', _assignment_task_id=owner['id'],
                        created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        self.service._register_work(callback['id'], dict(key='account-export', title='Export account data',
            activity='Checking export', status='working'))
        bot = self.write('POST', '/v1/bots', dict(name='Delegate', role='Export'))
        message = self.service._store_bot_message(callback['id'], dict(target_bot_id=bot['id'], text='Prepare export', require_response=True))
        delegate = self.service._get('tasks', message['task_id'])
        delegate.update(status='working', _native_relay=True)
        self.service._put('tasks', delegate)
        owner.update(exchange_id=self.service._new_exchange(), _waiting_on=[])
        self.service._put('tasks', owner)
        child = self.report(delegate, parent)
        self.assertEqual(self.service._get('artifacts', child['id'])['_work_parent_id'], parent['id'])

    def test_new_delegation_drops_old_alias_and_reuses_its_parent_key(self):
        parent = self.track()
        task, item = self.child(key='worker-export')
        self.report(task, parent)
        request = self.write('POST', '/v1/bots/' + parent['bot_id'] + '/messages', dict(text='Export another account', mode='queue'))
        owner = self.service._get('tasks', request['id'])
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        new_parent = self.service._register_work(owner['id'], dict(key='worker-export', title='Export another account',
            activity='Preparing export', status='working'))
        message = self.service._store_bot_message(owner['id'], dict(target_bot_id=task['bot_id'], text='Export another account', require_response=True))
        delegate = self.service._get('tasks', message['task_id'])
        delegate.update(status='working', _native_relay=True)
        self.service._put('tasks', delegate)
        self.service._register_work(delegate['id'], dict(key='worker-export', title='Export another account',
            activity='Preparing export', status='working'))
        saved = self.service._get('artifacts', item['id'])
        self.assertNotIn('_same_outcome_as', saved)
        self.assertEqual(saved['_work_parent_id'], new_parent['id'])


    def test_canceled_parent_cannot_receive_a_new_alias(self):
        parent = self.track()
        self.track(key='separate-result', title='Prepare another export')
        task, child = self.child(key='worker-export')
        self.write('POST', '/v1/work/items/' + parent['id'] + '/cancel', {})
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'working')
        with self.assertRaises(ServiceError): self.report(task, parent)
        self.assertFalse(self.service._get('artifacts', child['id'])['_hidden'])

    def test_nested_handoff_cannot_inherit_a_canceled_parent(self):
        parent = self.track()
        self.track(key='another-result', title='Prepare another export')
        task, child = self.child(key='worker-export')
        self.report(task, parent)
        self.write('POST', '/v1/work/items/' + parent['id'] + '/cancel', {})
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'working')
        bot = self.write('POST', '/v1/bots', dict(name='Nested delegate', role='Export'))
        sent = self.service._store_bot_message(task['id'], dict(target_bot_id=bot['id'], text='Prepare export', require_response=True))
        delegate = self.service._get('tasks', sent['task_id'])
        delegate.update(status='working', _native_relay=True)
        self.service._put('tasks', delegate)
        report = self.service._register_work(delegate['id'], dict(key='worker-export', title='Export account data',
            activity='Verifying export', status='working'))
        self.assertFalse(self.service._get('artifacts', report['id'])['_hidden'])
        self.assertEqual(self.service._get('artifacts', child['id'])['_work_parent_id'], parent['id'])
        with self.assertRaises(ServiceError): self.report(task, parent)

    def test_nested_same_key_handoff_keeps_the_outer_alias(self):
        parent = self.track()
        task, _ = self.child(key='worker-export')
        self.report(task, parent)
        bot = self.write('POST', '/v1/bots', dict(name='Nested delegate', role='Export'))
        sent = self.service._store_bot_message(task['id'], dict(target_bot_id=bot['id'], text='Prepare export', require_response=True))
        delegate = self.service._get('tasks', sent['task_id'])
        delegate.update(status='working', _native_relay=True)
        self.service._put('tasks', delegate)
        child = self.service._register_work(delegate['id'], dict(key='worker-export', title='Export account data',
            activity='Verifying export', status='working'))
        saved = self.service._get('artifacts', child['id'])
        self.assertEqual(saved['_work_parent_id'], parent['id'])
        self.assertEqual(saved['_same_outcome_as'], parent['id'])
        self.assertEqual([a['id'] for a in self.service.snapshot()['artifacts'] if a['kind']=='work'], [parent['id']])


    def completed_parent_with_delegate(self):
        parent = self.track()
        task, _ = self.child(key='worker-export')
        self.report(task, parent)
        self.track(status='completed', completion_evidence='The export exists.', activity='Export saved')
        owner = self.service._get('tasks', parent['task_id'])
        owner.update(status='waiting', _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Export data')]})
        self.service._put('tasks', owner)
        return parent, task

    def test_existing_alias_accepts_progress_and_completion_while_parent_remains_active(self):
        parent, task = self.completed_parent_with_delegate()
        self.report(task, parent)
        result = self.service._register_work(task['id'], dict(key='worker-export', same_outcome_as=parent['id'],
            title='Export account data', activity='Export verified', status='completed',
            completion_evidence='The exported file contains all records.'))
        saved = self.service._get('artifacts', result['id'])
        self.assertEqual(saved['_work_parent_id'], parent['id'])
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['completion_evidence'], 'The exported file contains all records.')

    def test_parent_completion_keeps_the_aliased_process_open(self):
        parent, task = self.completed_parent_with_delegate()
        call = self.service.native_event(task['id'], 'native_call', dict(name='terminal', arguments={}))
        self.service.native_event(task['id'], 'native_result', dict(id=call['id'], result=json.dumps(
            dict(session_id='export-process', status='running', notify_on_complete=True))))
        task['status'] = 'waiting'
        self.service._put('tasks', task)
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id']==parent['id'])
        self.assertNotEqual(row['work_state'], 'completed')

    def test_parent_completion_keeps_the_aliased_decision_open(self):
        parent, task = self.completed_parent_with_delegate()
        self.service._put('decisions', dict(id='delegate-choice', task_id=task['id'], bot_id=task['bot_id'],
            status='pending', kind='question', title='Approve this export?'))
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id']==parent['id'])
        self.assertEqual(row['work_state'], 'needs_decision')
        self.assertEqual(row['work_decision_id'], 'delegate-choice')

    def test_parent_completion_keeps_a_queued_delegate_open(self):
        parent, task = self.completed_parent_with_delegate()
        task['status'] = 'queued'
        self.service._put('tasks', task)
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id']==parent['id'])
        self.assertEqual(row['work_state'], 'queued')


class AliasedExternalWaitTests(unittest.TestCase):
    import test_external_work_waits as external
    setUp = external.ExternalWorkWaitTests.setUp
    tearDown = external.ExternalWorkWaitTests.tearDown
    write = external.ExternalWorkWaitTests.write
    start = external.ExternalWorkWaitTests.start
    report_wait = external.ExternalWorkWaitTests.report_wait
    delegated_wait = external.ExternalWorkWaitTests.delegated_wait
    sync = external.ExternalWorkWaitTests.sync

    def test_different_keys_keep_external_wait_after_parent_exchange_changes(self):
        parent, item, child, _, _ = self.delegated_wait()
        report = self.service._get('artifacts', item['id'])
        report.update(_work_key='delegate-receipts', _same_outcome_as=parent['id'])
        self.service._put('artifacts', report)
        owner = self.service._get('tasks', parent['task_id'])
        owner.update(exchange_id=self.service._new_exchange(), _waiting_on=[])
        self.service._put('tasks', owner)
        self.assertEqual(self.sync()[0]['work_state'], 'waiting')
        self.assertTrue(self.service._get('artifacts', item['id'])['_hidden'])
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)

    def test_bound_routine_preserves_alias_before_recording_its_active_check(self):
        parent, item, child, _, payload = self.delegated_wait()
        report = self.service._get('artifacts', item['id'])
        report.update(_work_key='delegate-receipts', _same_outcome_as=parent['id'])
        self.service._put('artifacts', report)
        routine = dict(child, id='routine-check', status='working', _native_job={'id': 'child-native'},
                       created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', routine)
        payload.update(key='delegate-receipts', status='working', activity='Reading new receipts')
        payload.pop('external_wait')
        self.service._register_work(routine['id'], payload)
        saved = self.service._get('artifacts', item['id'])
        self.assertEqual(saved['_same_outcome_as'], parent['id'])
        self.assertEqual(saved['_work_parent_id'], parent['id'])
        self.assertTrue(saved['_hidden'])
