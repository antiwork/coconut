import unittest
import json
import threading
from datetime import datetime, timezone

import test_delegated_work_duplicates as fixtures
from coconut.service import ServiceError
import legacy_work_sync  # noqa: F401  (adds Service._sync_automatic_work)


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

    def test_validated_alias_survives_a_new_parent_attempt(self):
        parent = self.track()
        task, item = self.child(key='worker-export')
        self.report(task, parent)
        old = self.service._get('tasks', parent['task_id'])
        newer = dict(old, id='later-verification', exchange_id=self.service._new_exchange(),
            status='completed', _waiting_on=[], _self_recovery={'previous_task_id': old['id']})
        self.service._put('tasks', newer)
        saved = self.service._get('artifacts', parent['id'])
        saved.update(task_id=newer['id'], _work_previous_task_ids=[old['id']])
        self.service._put('artifacts', saved)
        # Repair an alias that an earlier reconciliation exposed.
        alias = self.service._get('artifacts', item['id'])
        alias['_hidden'] = False
        alias.pop('_work_parent_id', None)
        alias.pop('_work_parent_task_id', None)
        self.service._put('artifacts', alias)
        for _ in range(2):
            self.service._sync_automatic_work()
            self.assertTrue(self.service._get('artifacts', item['id'])['_hidden'])
            rows = [a for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work']
            self.assertEqual([a['id'] for a in rows], [parent['id']])

    def test_work_discussion_keeps_the_existing_delegated_report(self):
        parent = self.track()
        child, report = self.child(key='worker-export')
        self.report(child, parent)
        sent = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='Update the title for this same work', mode='queue', work_artifact_id=parent['id']))
        discussion = self.service._get('tasks', sent['id'])
        discussion['status'] = 'working'
        self.service._put('tasks', discussion)
        self.service._register_work(discussion['id'], dict(key='account-export', title='Export the account records',
            activity='Waiting for the existing result', status='waiting'))
        self.service._sync_automatic_work()
        self.assertTrue(self.service._get('artifacts', report['id'])['_hidden'])
        rows = [a for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work']
        self.assertEqual([a['id'] for a in rows], [parent['id']])

    def test_linked_pr_follows_its_delegated_outcome_after_aliasing(self):
        from test_service_pr import URL
        parent = self.track()
        child, report = self.child(key='worker-export')
        pr = self.service._register_pr_artifact(child['id'], dict(url=URL))
        self.assertEqual(pr['work_outcome_id'], report['id'])
        self.report(child, parent)
        row = self.service._work_projection([self.service._get('artifacts', pr['id'])])[0]
        self.assertEqual(row['work_outcome_id'], parent['id'])
        self.write('POST', '/v1/work/items/' + parent['id'] + '/cancel', {})
        self.assertEqual(self.service._get('tasks', child['id'])['status'], 'cancelled')
        row = self.service._work_projection([self.service._get('artifacts', pr['id'])])[0]
        self.assertEqual(row['work_state'], 'cancelled')
        self.assertNotIn('checkout_recovery_stopped', row)
        canonical = self.service._work_projection([self.service._get('artifacts', parent['id'])])[0]
        self.assertIs(canonical['checkout_recovery_stopped'], True)

    def test_hidden_delegate_keeps_context_for_existing_discussions(self):
        parent = self.track()
        child, report = self.child(key='worker-export')
        self.report(child, parent)
        context = self.service.work_context_input(report['id'])
        self.assertEqual(context['id'], report['id'])
        self.assertEqual(context['title'], self.service._get('artifacts', report['id'])['title'])

    def test_resuming_delegate_pr_restores_monitoring_in_the_parent_bot(self):
        from test_service_pr import URL
        parent = self.track()
        child, report = self.child(key='worker-export')
        pr = self.service._register_pr_artifact(child['id'], dict(url=URL))
        self.report(child, parent)
        self.write('POST', '/v1/work/items/' + pr['id'] + '/cancel', {})
        self.write('POST', '/v1/work/items/' + pr['id'] + '/resume', {})
        owner = self.service._get('tasks', self.service._get('artifacts', parent['id'])['task_id'])
        owner.update(status='waiting', _waiting_on=[])
        self.service._put('tasks', owner)
        for peer in self.service._all('artifacts'):
            if peer['kind'] == 'pr':
                peer.update(pr_state='open', checks_state='passed', required_checks_state='passed')
                self.service._put('artifacts', peer)
        self.service._queue_pr_reviews()
        resumed = self.service._get('tasks', owner['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertEqual(resumed['_pr_review']['url'], URL)

    def test_resumed_delegate_pr_keeps_its_wait_after_the_turn_ends(self):
        self.check_retired_delegate_wait(failed=False)

    def test_recovered_delegate_pr_keeps_its_wait_after_the_turn_ends(self):
        self.check_retired_delegate_wait(failed=True)

    def check_retired_delegate_wait(self, *, failed):
        from test_service_pr import URL
        parent = self.track()
        child, report = self.child(key='worker-export')
        pr = self.service._register_pr_artifact(child['id'], dict(url=URL))
        self.report(child, parent)
        if failed:
            for identity in (parent['task_id'], child['id']):
                previous = self.service._get('tasks', identity)
                previous.update(status='failed', _waiting_on=[], _conversation_epoch=self.service._setting('conversation_epoch'))
                self.service._put('tasks', previous)
            self.service._queue_work_recoveries()
        else:
            self.write('POST', '/v1/work/items/' + pr['id'] + '/cancel', {})
            self.write('POST', '/v1/work/items/' + pr['id'] + '/resume', {})
        owner = self.service._get('tasks', self.service._get('artifacts', parent['id'])['task_id'])
        self.assertNotEqual(owner['id'], parent['task_id'])
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        self.service._register_pr_artifact(owner['id'],
            dict(url=URL, activity='Waiting for GitHub checks', activity_state='waiting'))
        owner = self.service._get('tasks', owner['id'])
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        row = self.service._work_projection([self.service._get('artifacts', parent['id'])])[0]
        self.assertEqual(row['work_state'], 'waiting')

    def test_recovery_uses_the_outcome_owner_when_the_pr_was_created_first(self):
        from test_automatic_work import AutomaticWorkTests
        from test_service_pr import URL
        AutomaticWorkTests.start(self)
        child, report = self.child(key='worker-export')
        self.service._register_pr_artifact(child['id'], dict(url=URL))
        parent = self.track()
        self.report(child, parent)
        AutomaticWorkTests.start(self, status='failed')
        child = self.service._get('tasks', child['id'])
        child.update(status='failed', _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', child)
        self.service.running.clear()
        self.service._queue_work_recoveries()
        owner = self.service._get('tasks', self.service._get('artifacts', parent['id'])['task_id'])
        self.assertNotEqual(owner['id'], self.task['id'])
        self.assertEqual(owner['bot_id'], self.bot['id'])
        self.assertEqual([w['id'] for w in owner['_self_recovery']['work']], [parent['id']])

    def test_parent_and_delegate_discussions_keep_the_original_execution(self):
        parent = self.track()
        child, report = self.child()
        owner = self.service._get('tasks', parent['task_id'])
        middle = dict(owner, id='delegating-discussion', _execution_session_id=owner['id'],
                      _work_context_id=parent['id'], parent_task_id=owner['id'])
        self.service._put('tasks', middle)
        owner['_waiting_on'] = []
        child.update(source_task_id=middle['id'], parent_task_id=middle['id'], _reply_to_task_id=middle['id'])
        for task, item, identity in ((owner, parent, 'parent-discussion'), (child, report, 'delegate-discussion')):
            task['_execution_session_id'] = task['id']
            self.service._put('tasks', task)
            discussion = dict(task, id=identity, source_task_id=None,
                parent_task_id=task['id'], exchange_id=self.service._new_exchange(),
                _work_context_id=item['id'], _waiting_on=[], status='completed')
            discussion.pop('_assignment_task_id', None)
            self.service._put('tasks', discussion)
            saved = self.service._get('artifacts', item['id'])
            history = [task['id'], middle['id']] if item['id'] == parent['id'] else [task['id']]
            saved.update(task_id=identity, _work_previous_task_ids=history, _hidden=False)
            for field in ('_work_parent_id', '_work_parent_task_id', '_same_outcome_as'):
                saved.pop(field, None)
            self.service._put('artifacts', saved)
        self.service.running[child['id']] = threading.current_thread()
        try:
            self.service._sync_automatic_work()
            rows = [a for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work']
            self.assertEqual([a['id'] for a in rows], [parent['id']])
            self.assertEqual(rows[0]['work_state'], 'working')
            tasks = {t['id']: t for t in self.service._all('tasks')}
            chain = self.service._work_report_chain(self.service._get('artifacts', parent['id']), tasks, [])
            self.assertIn(child['id'], chain)
        finally:
            self.service.running.clear()

    def test_execution_link_cannot_cross_bot_session_or_conversation(self):
        parent = self.track()
        owner = self.service._get('tasks', parent['task_id'])
        owner['_execution_session_id'] = owner['id']
        for field in ('bot_id', 'conversation_bot_id', 'session_id', '_conversation_epoch'):
            with self.subTest(field=field):
                other = dict(owner, id='other', **{field: 'another-value'})
                tasks = {owner['id']: owner, other['id']: other}
                self.assertEqual(self.service._work_execution_owner(other, tasks)['id'], other['id'])
                links, _ = self.service._work_links(tasks, [], True)
                self.assertNotIn(owner['id'], links.get(other['id'], set()))

    def test_original_worker_can_report_after_a_newer_work_discussion(self):
        parent = self.track()
        owner = self.service._get('tasks', parent['task_id'])
        owner['_execution_session_id'] = owner['id']
        self.service._put('tasks', owner)
        discussion = dict(owner, id='newer-discussion', parent_task_id=owner['id'],
            _work_context_id=parent['id'], created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', discussion)
        item = self.service._get('artifacts', parent['id'])
        item['task_id'] = discussion['id']
        self.service._put('artifacts', item)
        self.service._register_work(owner['id'], dict(key='account-export', title='Export account data',
            activity='Verifying the file', status='working'))
        saved = self.service._get('artifacts', parent['id'])
        self.assertEqual(saved['task_id'], discussion['id'])
        self.assertEqual(saved['activity'], 'Verifying the file')
        discussion['status'] = 'completed'
        self.service._put('tasks', discussion)
        self.service.running[owner['id']] = threading.current_thread()
        try:
            row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == parent['id'])
            self.assertEqual(row['activity'], 'Verifying the file')
        finally:
            self.service.running.clear()

    def test_old_worker_cannot_report_on_an_unrelated_newer_outcome(self):
        parent = self.track()
        request = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='Export a different account', mode='queue'))
        newer = self.service._get('tasks', request['id'])
        newer['status'] = 'working'
        self.service._put('tasks', newer)
        self.service._register_work(newer['id'], dict(key='account-export', title='Export another account',
            activity='Preparing another file', status='working'))
        with self.assertRaises(ServiceError):
            self.service._register_work(parent['task_id'], dict(key='account-export', title='Export account data',
                activity='Verifying the old file', status='working'))

    def test_explicit_alias_detaches_when_unrelated_work_reuses_parent_key(self):
        parent = self.track()
        task, item = self.child(key='worker-export')
        self.report(task, parent)
        request = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='Export a different account', mode='queue'))
        owner = self.service._get('tasks', request['id'])
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        self.service._register_work(owner['id'], dict(key='account-export', title='Export the other account',
            activity='Preparing another export', status='working'))
        self.assertFalse(self.service._get('artifacts', item['id'])['_hidden'])
        discussion = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='Update the other account export', mode='queue', work_artifact_id=parent['id']))
        current = self.service._get('tasks', discussion['id'])
        current['status'] = 'working'
        self.service._put('tasks', current)
        self.service._register_work(current['id'], dict(key='account-export', title='Export the other account',
            activity='Checking the other export', status='working'))
        self.assertFalse(self.service._get('artifacts', item['id'])['_hidden'])

    def test_alias_recovery_keeps_the_original_parent(self):
        parent = self.track()
        task, item = self.child(key='worker-export')
        self.report(task, parent)
        owner = self.service._get('tasks', parent['task_id'])
        owner.update(exchange_id=self.service._new_exchange(), _waiting_on=[])
        self.service._put('tasks', owner)
        attempt = dict(task, id='later-delegate', exchange_id=self.service._new_exchange(),
            status='waiting', source_task_id=None, _self_recovery={'previous_task_id': task['id']})
        self.service._put('tasks', attempt)
        alias = self.service._get('artifacts', item['id'])
        alias.update(task_id=attempt['id'], _hidden=False)
        alias.pop('_work_parent_id', None)
        self.service._put('artifacts', alias)
        self.service._sync_automatic_work()
        self.assertTrue(self.service._get('artifacts', item['id'])['_hidden'])

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

    def test_completed_alias_does_not_keep_an_acknowledged_outcome_open(self):
        parent, task = self.completed_parent_with_delegate()
        self.service._register_work(task['id'], dict(key='worker-export',
            title='Export account data', activity='Export verified', status='completed',
            completion_evidence='The exported file contains all records.'))
        task.update(status='completed', _waiting_on=[],
                    _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Export data')]})
        self.service._put('tasks', task)
        owner = self.service._get('tasks', parent['task_id'])
        owner['_waiting_on'] = [task['id']]
        self.service._put('tasks', owner)
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id']==parent['id'])
        self.assertEqual(row['work_state'], 'completed')

    def test_ready_failed_delegate_does_not_complete_the_outcome(self):
        parent, task = self.completed_parent_with_delegate()
        owner = self.service._get('tasks', parent['task_id'])
        owner['_waiting_on'] = [task['id']]
        self.service._put('tasks', owner)
        for status in ('failed', 'cancelled'):
            with self.subTest(status=status):
                task.update(status=status, _waiting_on=[])
                self.service._put('tasks', task)
                row = next(a for a in self.service.snapshot()['artifacts'] if a['id']==parent['id'])
                self.assertNotEqual(row['work_state'], 'completed')


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


class PullRequestOutcomeTests(unittest.TestCase):
    from test_tracked_work import TrackedWorkTests as fixtures
    setUp = fixtures.setUp
    tearDown = fixtures.tearDown
    track = fixtures.track
    write = fixtures.write

    def test_pr_reuses_its_outcome_and_cancellation(self):
        from test_service_pr import URL
        work = self.track()
        pr = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        self.assertEqual(pr['work_outcome_id'], work['id'])
        self.service.running[self.task['id']] = threading.current_thread()
        try:
            rows = self.service.snapshot()['artifacts']
            self.assertEqual({a['work_state'] for a in rows}, {'working'})
        finally:
            self.service.running.clear()
        result = self.write('POST', '/v1/work/items/' + pr['id'] + '/cancel', {})
        self.assertTrue(result['ok'])
        self.assertEqual({a['work_state'] for a in self.service.snapshot()['artifacts']}, {'cancelled'})

    def test_merge_card_from_another_task_stays_attached_to_the_outcome(self):
        from test_service_pr import URL
        work = self.track()
        self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        other = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='Review the completed export', mode='queue'))
        self.service._put('decisions', dict(id='merge-export', task_id=other['id'], bot_id=self.bot['id'],
            status='pending', kind='question', title='Merge the export fix?', merge_pr_url=URL))
        row = self.service._work_projection([self.service._get('artifacts', work['id'])])[0]
        self.assertEqual(row['work_state'], 'needs_decision')
        self.assertEqual(row['work_decision_id'], 'merge-export')

    def test_pr_cannot_attach_an_unrelated_outcome(self):
        from test_service_pr import URL
        work = self.track()
        request = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
                             dict(text='Fix another issue', mode='queue'))
        task = self.service._get('tasks', request['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        with self.assertRaises(ServiceError):
            self.service._register_pr_artifact(task['id'], dict(url=URL, work_id=work['id']))
        self.assertEqual(len(self.service._all('artifacts')), 1)

    def test_later_review_keeps_the_existing_pr_outcome(self):
        from test_service_pr import URL
        work = self.track()
        self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        sent = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='Review the existing PR', mode='queue'))
        reviewer = self.service._get('tasks', sent['id'])
        reviewer['status'] = 'working'
        self.service._put('tasks', reviewer)
        self.service._register_work(reviewer['id'], dict(key='review-export', title='Review the export',
            activity='Reading the PR', status='working'))
        reported = self.service._register_pr_artifact(reviewer['id'], dict(url=URL, activity='Reviewing the export'))
        self.assertEqual(reported['work_outcome_id'], work['id'])

    def test_second_pr_keeps_the_hidden_automatic_outcome(self):
        from test_automatic_work import AutomaticWorkTests
        from test_service_pr import URL, observation
        AutomaticWorkTests.start(self)
        self.service._sync_automatic_work()
        first = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        self.service._sync_automatic_work()
        self.service.running.clear()
        self.tracker.observations.append(observation())
        second = self.service._register_pr_artifact(self.task['id'], dict(url=URL.replace('/42', '/43')))
        self.assertEqual(second.get('work_outcome_id'), first['work_outcome_id'])

    def test_pr_refresh_restores_dismissed_automatic_work_only_when_active(self):
        from test_automatic_work import AutomaticWorkTests
        from test_service_pr import URL
        AutomaticWorkTests.start(self)
        self.service._sync_automatic_work()
        work = next(a for a in self.service._all('artifacts') if a['kind'] == 'work')
        pr = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        self.service._sync_automatic_work()
        AutomaticWorkTests.start(self, status='completed', _work_todos={'todos': [dict(id='coconut-assignment',
            status='completed', content='Export verified.')]})
        self.service.running.clear()
        self.write('POST', '/v1/work/items/' + pr['id'] + '/dismiss', dict(dismissal_id='finished'))
        saved = self.service._get('artifacts', pr['id'])
        saved['pr_state'] = 'open'
        self.service._put('artifacts', saved)
        self.service._restore_active_work(saved)
        key = 'work.dismissed.work:' + work['id']
        self.assertEqual(self.service._setting(key), 'finished')
        AutomaticWorkTests.start(self, _work_todos={})
        try:
            self.service._restore_active_work(saved)
            self.assertIsNone(self.service._setting(key))
        finally:
            self.service.running.clear()

    def test_pr_recovery_can_update_its_hidden_automatic_outcome(self):
        from test_automatic_work import AutomaticWorkTests
        from test_service_pr import URL
        AutomaticWorkTests.start(self)
        self.service._sync_automatic_work()
        work = next(a for a in self.service._all('artifacts') if a['kind'] == 'work')
        pr = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        self.service._sync_automatic_work()
        self.assertTrue(self.service._get('artifacts', work['id'])['_hidden'])
        AutomaticWorkTests.start(self, status='failed')
        self.service.running.clear()
        self.service._queue_work_recoveries()
        recovery = self.service._get('tasks', self.service._get('artifacts', pr['id'])['_review_task_id'])
        self.assertNotEqual(recovery['id'], self.task['id'])
        self.assertEqual(recovery['_work_context_id'], work['id'])
        recovery['status'] = 'working'
        self.service._put('tasks', recovery)
        reported = self.service._register_pr_artifact(recovery['id'],
            dict(url=URL, activity='Checking the saved test results'))
        self.assertEqual(reported['work_outcome_id'], work['id'])
        self.assertEqual(self.service._get('artifacts', pr['id'])['_activity_task_id'], recovery['id'])
        recovery = self.service._get('tasks', recovery['id'])
        recovery.update(status='completed', _work_todos={'todos': [dict(id='coconut-assignment',
            status='completed', content='Verified the completed export.')]})
        self.service._put('tasks', recovery)
        row = self.service._work_projection([self.service._get('artifacts', pr['id'])])[0]
        self.assertEqual(row['work_state'], 'completed')
        count = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), count)

    def test_reply_to_pr_progress_keeps_the_hidden_outcome_context(self):
        from test_automatic_work import AutomaticWorkTests
        from test_service_pr import URL
        AutomaticWorkTests.start(self)
        self.service._sync_automatic_work()
        work = next(a for a in self.service._all('artifacts') if a['kind'] == 'work')
        pr = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        self.service._sync_automatic_work()
        self.service.running.clear()
        status = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='How is this going?', mode='queue', work_artifact_id=pr['id'], work_status_only=True))
        reply = self.write('POST', '/v1/tasks/' + status['id'] + '/replies', dict(text='Please finish the fix'))
        self.assertEqual(self.service._get('tasks', reply['id'])['_work_context_id'], work['id'])

    def test_resuming_linked_work_restores_pr_monitoring(self):
        from test_service_pr import URL
        work = self.track()
        pr = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        self.write('POST', '/v1/work/items/' + pr['id'] + '/cancel', {})
        self.write('POST', '/v1/work/items/' + pr['id'] + '/resume', {})
        owner = self.service._get('artifacts', work['id'])['task_id']
        saved = self.service._get('artifacts', pr['id'])
        self.assertEqual(saved['_review_task_id'], owner)
        self.assertTrue(self.service._pr_review_source_current(saved))

    def test_visible_outcome_recovery_retains_its_pr_monitor(self):
        from test_automatic_work import AutomaticWorkTests
        from test_service_pr import URL
        work = self.track()
        pr = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        AutomaticWorkTests.start(self, status='failed')
        self.service.running.clear()
        self.service._queue_work_recoveries()
        owner = self.service._get('artifacts', work['id'])['task_id']
        self.assertNotEqual(owner, self.task['id'])
        saved = self.service._get('artifacts', pr['id'])
        self.assertEqual(saved['_review_task_id'], owner)
        self.assertTrue(self.service._pr_review_source_current(saved))

    def test_one_recovery_restores_each_independent_pr_monitor(self):
        from test_automatic_work import AutomaticWorkTests
        from test_service_pr import URL, observation
        AutomaticWorkTests.start(self)
        first = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        self.tracker.observations.append(observation())
        second = self.service._register_pr_artifact(self.task['id'], dict(url=URL.replace('/42', '/43')))
        AutomaticWorkTests.start(self, status='failed')
        self.service.running.clear()
        self.service._queue_work_recoveries()
        owners = {self.service._get('artifacts', p['id'])['_review_task_id'] for p in (first, second)}
        self.assertEqual(len(owners), 1)
        self.assertNotIn(self.task['id'], owners)
        count = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), count)

    def test_explicit_followup_can_report_after_the_previous_owner_fails(self):
        from test_automatic_work import AutomaticWorkTests
        from test_service_pr import URL
        work = self.track()
        pr = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        AutomaticWorkTests.start(self, status='failed')
        self.service.running.clear()
        sent = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='Continue fixing this PR', mode='queue', work_artifact_id=pr['id']))
        followup = self.service._get('tasks', sent['id'])
        followup.update(status='working')
        self.service._put('tasks', followup)
        reported = self.service._register_pr_artifact(followup['id'],
            dict(url=URL, activity='Fixing the failed export'))
        self.assertEqual(reported['work_outcome_id'], work['id'])
        self.service.running[followup['id']] = threading.current_thread()
        try:
            row = self.service._work_projection([self.service._get('artifacts', pr['id'])])[0]
            self.assertEqual(row['work_state'], 'working')
            self.assertEqual(row['activity'], 'Fixing the failed export')
        finally:
            self.service.running.clear()

    def test_human_followup_transfers_the_monitor_and_preserves_a_wait(self):
        from test_automatic_work import AutomaticWorkTests
        from test_service_pr import URL
        work = self.track()
        pr = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        AutomaticWorkTests.start(self, status='failed')
        self.service.running.clear()
        sent = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='Continue fixing this PR', mode='queue', work_artifact_id=pr['id']))
        followup = self.service._get('tasks', sent['id'])
        followup['status'] = 'working'
        self.service._put('tasks', followup)
        self.service._register_work(followup['id'], dict(key='account-export', title='Export account data',
            activity='Checking the corrected export', status='working'))
        self.service._register_pr_artifact(followup['id'],
            dict(url=URL, activity='Waiting for GitHub checks', activity_state='waiting'))
        saved = self.service._get('artifacts', pr['id'])
        self.assertEqual(saved['_review_task_id'], followup['id'])
        followup = self.service._get('tasks', followup['id'])
        followup['status'] = 'completed'
        self.service._put('tasks', followup)
        row = self.service._work_projection([self.service._get('artifacts', work['id'])])[0]
        self.assertEqual(row['work_state'], 'waiting')

    def test_replacement_outcome_keeps_pr_state_and_shared_dismissal(self):
        from test_service_pr import URL
        work = self.track()
        pr = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        replacement = dict(self.service._get('artifacts', work['id']), id='replacement',
                           status='completed', completion_evidence='The export contains all records.',
                           activity='Export verified')
        self.service._put('artifacts', replacement)
        previous = self.service._get('artifacts', work['id'])
        previous.update(_hidden=True, _work_replaced_by=replacement['id'])
        self.service._put('artifacts', previous)
        owner = self.service._get('tasks', self.task['id'])
        owner.update(status='completed', _work_todos={'todos': [dict(id='coconut-assignment', status='completed')]})
        self.service._put('tasks', owner)
        row = self.service._work_projection([self.service._get('artifacts', pr['id'])])[0]
        self.assertEqual(row['work_outcome_id'], replacement['id'])
        self.assertEqual(row['work_state'], 'completed')
        self.write('POST', '/v1/work/items/' + pr['id'] + '/dismiss', dict(dismissal_id='shared'))
        rows = self.service.snapshot()['artifacts']
        self.assertEqual({a['work_dismissal_id'] for a in rows}, {'shared'})
        self.write('POST', '/v1/work/items/' + pr['id'] + '/dismiss', dict(dismissal_id='shared', undo=True))
        self.assertEqual({a['work_dismissal_id'] for a in self.service.snapshot()['artifacts']}, {None})
        replacement.update(status='waiting', completion_evidence='')
        self.service._put('artifacts', replacement)
        owner.update(status='waiting', _work_todos={})
        self.service._put('tasks', owner)
        for _ in range(2):
            result = self.write('POST', '/v1/work/items/' + pr['id'] + '/cancel', {})
            self.assertNotIn(previous['id'], [a['id'] for a in result['artifacts']])

    def test_pr_activity_uses_its_own_head_in_the_shared_outcome(self):
        from test_service_pr import URL
        work = self.track()
        pr = self.service._register_pr_artifact(self.task['id'],
            dict(url=URL, activity='Checking export compatibility'))
        self.service.running[self.task['id']] = threading.current_thread()
        try:
            for identity in (work['id'], pr['id']):
                row = self.service._work_projection([self.service._get('artifacts', identity)])[0]
                self.assertEqual(row['work_state'], 'working')
                self.assertEqual(row['activity'], 'Checking export compatibility')
        finally:
            self.service.running.clear()
        self.service._register_pr_artifact(self.task['id'], dict(url=URL, activity='Missing export schema', activity_state='blocked'))
        row = self.service._work_projection([self.service._get('artifacts', work['id'])])[0]
        self.assertEqual(row['work_state'], 'needs_recovery')
        self.assertEqual(row['activity'], 'Missing export schema')

    def test_pr_metadata_stays_with_its_url_inside_one_outcome(self):
        from test_service_pr import URL
        self.track()
        first = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        from test_service_pr import observation
        from dataclasses import replace
        self.tracker.observations.append(replace(observation(), url=URL[:-2] + '43'))
        second = self.service._register_pr_artifact(self.task['id'], dict(url=URL[:-2] + '43'))
        other = self.service._get('artifacts', second['id'])
        other.update(head_sha='b' * 40, checks_state='failed', observed_at='2099-01-01T00:00:00Z')
        self.service._put('artifacts', other)
        context = self.service.work_context_input(first['id'])
        self.assertEqual(context['pull_request']['head_sha'], first['head_sha'])
        self.assertEqual({p['id'] for p in context['pull_requests']}, {first['id'], second['id']})

    def test_old_copy_cannot_restore_a_blocker_after_the_pr_head_changes(self):
        from test_service_pr import URL
        work = self.track()
        first = self.service._register_pr_artifact(self.task['id'],
            dict(url=URL, activity='Missing old schema', activity_state='blocked'))
        newer = dict(self.service._get('artifacts', first['id']), id='new-copy',
                     observed_at='2099-01-01T00:00:00Z', head_sha='b' * 40)
        newer.pop('_activity_task_id', None)
        self.service._put('artifacts', newer)
        row = self.service._work_projection([self.service._get('artifacts', work['id'])])[0]
        self.assertNotEqual(row['activity'], 'Missing old schema')

    def test_old_head_activity_cannot_reintroduce_a_failed_attempt(self):
        from test_service_pr import URL
        work = self.track()
        pr = self.service._register_pr_artifact(self.task['id'],
            dict(url=URL, activity='Waiting for GitHub checks', activity_state='waiting'))
        owner = self.service._get('tasks', self.task['id'])
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        failed = dict(owner, id='old-head-observer', status='failed', exchange_id=self.service._new_exchange())
        self.service._put('tasks', failed)
        previous = self.service._get('artifacts', pr['id'])
        previous.update(_activity_task_id=failed['id'], _activity_state='blocked')
        self.service._put('artifacts', previous)
        latest = dict(previous, id='current-head-report', head_sha='b' * 40, _activity_head='b' * 40,
            _activity_task_id=owner['id'], _activity_state='waiting', _activity='Waiting for GitHub checks',
            observed_at='2099-01-01T00:00:00Z', _activity_at='2099-01-01T00:00:00Z')
        self.service._put('artifacts', latest)
        row = self.service._work_projection([self.service._get('artifacts', work['id'])])[0]
        self.assertEqual(row['work_state'], 'waiting')

    def test_linked_pr_wait_survives_the_end_of_its_owner_turn(self):
        from test_service_pr import URL
        work = self.track()
        pr = self.service._register_pr_artifact(self.task['id'],
            dict(url=URL, activity='Waiting for GitHub checks', activity_state='waiting'))
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        for artifact in (work, pr):
            row = self.service._work_projection([self.service._get('artifacts', artifact['id'])])[0]
            self.assertEqual(row['work_state'], 'waiting')
        self.service._queue_work_recoveries()
        self.assertFalse(any(t.get('_self_recovery') for t in self.service._all('tasks')))

    def test_linked_pr_closure_queues_outcome_verification_after_a_wait(self):
        from test_service_pr import URL
        work = self.track()
        pr = self.service._register_pr_artifact(self.task['id'],
            dict(url=URL, activity='Waiting for GitHub checks', activity_state='waiting'))
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        newer = dict(self.service._get('artifacts', pr['id']), id='newer-closed-report',
            pr_state='merged', status='merged', observed_at='2099-01-01T00:00:00Z')
        newer.pop('_activity_task_id')
        self.service._put('artifacts', newer)
        row = self.service._work_projection([self.service._get('artifacts', work['id'])])[0]
        self.assertEqual(row['work_state'], 'needs_recovery')
        self.service._queue_work_recoveries()
        owner = self.service._get('tasks', self.service._get('artifacts', work['id'])['task_id'])
        self.assertNotEqual(owner['id'], self.task['id'])
        self.assertEqual(owner['_self_recovery']['work'][0]['id'], work['id'])

    def test_linking_cannot_restart_a_cancelled_standalone_pr(self):
        from test_service_pr import URL
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        pr = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        self.write('POST', '/v1/work/items/' + pr['id'] + '/cancel', {})
        self.task = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='Check the export records', mode='queue'))
        work = self.track()
        with self.assertRaises(ServiceError):
            self.service._register_pr_artifact(self.task['id'], dict(url=URL, work_id=work['id']))
        saved = self.service._get('artifacts', pr['id'])
        self.assertNotIn('work_outcome_id', saved)
        self.assertEqual(self.service._work_projection([saved])[0]['work_state'], 'cancelled')

    def test_resumed_pr_can_wait_after_its_original_attempt_was_cancelled(self):
        self.check_resumed_pr_wait(failed=False)

    def test_recovered_pr_can_wait_after_its_original_attempt_failed(self):
        self.check_resumed_pr_wait(failed=True)

    def check_resumed_pr_wait(self, *, failed):
        from test_service_pr import URL
        from test_automatic_work import AutomaticWorkTests
        work = self.track()
        pr = self.service._register_pr_artifact(self.task['id'],
            dict(url=URL, activity='Checking the previous attempt'))
        if failed:
            AutomaticWorkTests.start(self, status='failed')
            self.service.running.clear()
            self.service._queue_work_recoveries()
        else:
            self.write('POST', '/v1/work/items/' + pr['id'] + '/cancel', {})
            self.write('POST', '/v1/work/items/' + pr['id'] + '/resume', {})
        owner = self.service._get('tasks', self.service._get('artifacts', work['id'])['task_id'])
        self.assertNotEqual(owner['id'], self.task['id'])
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        self.service._register_pr_artifact(owner['id'],
            dict(url=URL, activity='Waiting for GitHub checks', activity_state='waiting'))
        owner = self.service._get('tasks', owner['id'])
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        row = self.service._work_projection([self.service._get('artifacts', pr['id'])])[0]
        self.assertEqual(row['work_state'], 'waiting')
        count = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), count)
