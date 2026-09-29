import unittest
import threading
from datetime import datetime, timezone

import test_tracked_work as fixtures
import legacy_work_sync  # noqa: F401  (adds Service._sync_automatic_work)


class DelegatedWorkDuplicatesTests(unittest.TestCase):
    setUp = fixtures.TrackedWorkTests.setUp
    tearDown = fixtures.TrackedWorkTests.tearDown
    write = fixtures.TrackedWorkTests.write
    track = fixtures.TrackedWorkTests.track

    def child(self, linked=True, key='account-export'):
        bot = self.write('POST', '/v1/bots', dict(name='Developer', role='Develop'))
        if linked:
            message = self.service._store_bot_message(self.task['id'], dict(target_bot_id=bot['id'], text='Prepare export', require_response=True))
            identity = message['task_id']
        else:
            identity = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Prepare export', mode='queue'))['id']
        task = self.service._get('tasks', identity)
        task.update(status='working')
        self.service._put('tasks', task)
        item = self.service._register_work(task['id'], dict(key=key, title='Prepare export data', activity='Checking export', status='working'))
        return task, item

    def test_handoff_with_same_key_has_one_visible_work_item(self):
        parent = self.track()
        task, child = self.child()
        rows = [a for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work']
        self.assertEqual([a['id'] for a in rows], [parent['id']])
        self.assertEqual(self.service._get('artifacts', child['id'])['_work_parent_id'], parent['id'])
        self.service._register_work(task['id'], dict(key='account-export', title='Prepare export data', activity='Verifying export', status='working'))
        self.assertTrue(self.service._get('artifacts', child['id'])['_hidden'])

    def test_similar_names_without_a_handoff_do_not_merge(self):
        self.track()
        _, child = self.child(linked=False)
        self.assertFalse(self.service._get('artifacts', child['id'])['_hidden'])

    def test_distinct_handoff_outcomes_do_not_merge(self):
        self.track()
        _, child = self.child(key='account-deletion')
        self.assertFalse(self.service._get('artifacts', child['id'])['_hidden'])

    def test_later_direct_request_reusing_the_key_is_visible(self):
        self.track()
        child, item = self.child()
        task = self.write('POST', '/v1/bots/' + child['bot_id'] + '/messages', dict(text='Export again', mode='queue'))
        stored = self.service._get('tasks', task['id'])
        stored['status'] = 'working'
        self.service._put('tasks', stored)
        self.service._register_work(task['id'], dict(key='account-export', title='Export new data', activity='Preparing export', status='working'))
        saved = self.service._get('artifacts', item['id'])
        self.assertFalse(saved['_hidden'])
        self.assertNotIn('_work_parent_id', saved)

    def test_nested_handoff_still_shows_only_the_outer_outcome(self):
        parent = self.track()
        child, _ = self.child()
        bot = self.write('POST', '/v1/bots', dict(name='Second developer', role='Develop'))
        message = self.service._store_bot_message(child['id'], dict(target_bot_id=bot['id'], text='Prepare nested export', require_response=True))
        grandchild = self.service._get('tasks', message['task_id'])
        grandchild['status'] = 'working'
        self.service._put('tasks', grandchild)
        item = self.service._register_work(grandchild['id'], dict(key='account-export', title='Prepare nested export', activity='Verifying export', status='working'))
        self.assertEqual(self.service._get('artifacts', item['id'])['_work_parent_id'], parent['id'])
        self.assertEqual([a['id'] for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work'], [parent['id']])

    def test_parent_callback_keeps_one_row_and_the_delegates_progress(self):
        parent = self.track()
        child, item = self.child()
        owner = self.service._get('tasks', self.task['id'])
        callback = dict(owner, id='parent-callback', _assignment_task_id=owner['id'],
                        source_task_id='helper-task', _is_return=True,
                        created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        self.service._register_work(callback['id'], dict(key='account-export', title='Export account data',
            activity='Read the progress report', status='working'))
        self.assertEqual(self.service._get('artifacts', item['id'])['_work_parent_id'], parent['id'])
        self.service.running[child['id']] = threading.current_thread()
        try:
            rows = [a for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work']
            self.assertEqual([r['id'] for r in rows], [parent['id']])
            self.assertEqual(rows[0]['work_state'], 'working')
            self.assertEqual(rows[0]['activity'], 'Checking export')
        finally:
            self.service.running.clear()

    def test_callback_in_an_older_exchange_keeps_its_live_work(self):
        parent = self.track()
        owner = self.service._get('tasks', self.task['id'])
        callback = dict(owner, id='older-callback', _assignment_task_id=owner['id'],
                        source_task_id='helper-task', _is_return=True, created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        self.service._register_work(callback['id'], dict(key='account-export', title='Export account data',
            activity='Checking the exported file', status='working'))
        newer = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Continue', mode='queue'))
        owner.update(exchange_id=newer['exchange_id'], _waiting_on=[], status='completed')
        self.service._put('tasks', owner)
        self.service.running[callback['id']] = threading.current_thread()
        try:
            row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == parent['id'])
            self.assertEqual(row['work_state'], 'working')
            self.assertEqual(row['activity'], 'Checking the exported file')
        finally:
            self.service.running.clear()

    def test_stop_keeps_an_older_callback_that_also_owns_other_work(self):
        first = self.track()
        owner = self.service._get('tasks', self.task['id'])
        callback = dict(owner, id='shared-callback', _assignment_task_id=owner['id'],
                        source_task_id='helper-task', _is_return=True, created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        for key, title in [('account-export', 'Export account data'), ('other-export', 'Export the other account')]:
            self.service._register_work(callback['id'], dict(key=key, title=title, activity='Checking the export', status='working'))
        newer = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Continue', mode='queue'))
        owner.update(exchange_id=newer['exchange_id'], _waiting_on=[], status='completed')
        self.service._put('tasks', owner)
        self.write('POST', '/v1/work/items/' + first['id'] + '/cancel', {})
        saved = self.service._get('tasks', callback['id'])
        self.assertEqual(saved['status'], 'working')
        self.assertIn('Continue the other requested work.', saved['_steering'][-1])

    def test_stop_after_parent_callback_cancels_original_assignment_and_delegate(self):
        parent = self.track()
        child, _ = self.child()
        owner = self.service._get('tasks', self.task['id'])
        callback = dict(owner, id='parent-callback', _assignment_task_id=owner['id'],
                        source_task_id='helper-task', _is_return=True,
                        created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        self.service._register_work(callback['id'], dict(key='account-export', title='Export account data',
            activity='Read the progress report', status='working'))
        self.write('POST', '/v1/work/items/' + parent['id'] + '/cancel', {})
        for identity in (owner['id'], callback['id'], child['id']):
            self.assertEqual(self.service._get('tasks', identity)['status'], 'cancelled')

    def test_cancelling_a_callback_preserves_other_completed_work(self):
        completed = self.track(key='saved-export', status='completed', activity='Export saved',
                               completion_evidence='Read the saved export; all records are present.')
        owner = self.service._get('tasks', self.task['id'])
        callback = dict(owner, id='later-callback', _assignment_task_id=owner['id'],
                        source_task_id='helper-task', _is_return=True,
                        created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        active = self.service._register_work(callback['id'], dict(key='another-export', title='Export another account',
            activity='Preparing another export', status='working'))
        self.write('POST', '/v1/work/items/' + active['id'] + '/cancel', {})
        rows = {a['id']: a for a in self.service.snapshot()['artifacts']}
        self.assertEqual(rows[active['id']]['work_state'], 'cancelled')
        self.assertEqual(rows[completed['id']]['work_state'], 'completed')
        self.assertEqual(self.service._get('tasks', callback['id'])['status'], 'cancelled')

    def test_reused_parent_key_does_not_hide_an_older_delegation(self):
        parent = self.track()
        _, item = self.child()
        task = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Export a different account', mode='queue'))
        stored = self.service._get('tasks', task['id'])
        stored['status'] = 'working'
        self.service._put('tasks', stored)
        updated = self.service._register_work(task['id'], dict(key='account-export', title='Export the other account',
            activity='Preparing another export', status='working'))
        self.assertEqual(updated['id'], parent['id'])
        self.assertFalse(self.service._get('artifacts', item['id'])['_hidden'])

    def test_helper_return_preserves_the_original_handoff(self):
        parent = self.track()
        child, item = self.child()
        callback = dict(child, id='helper-return', _assignment_task_id=child['id'],
                        source_task_id='helper-task', _is_return=True,
                        created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        self.service._register_work(callback['id'], dict(key='account-export', title='Prepare export data',
            activity='Read the helper result', status='working'))
        self.assertEqual(self.service._get('artifacts', item['id'])['_work_parent_id'], parent['id'])

    def test_completed_legacy_handoff_stays_linked_after_its_wait_clears(self):
        parent = self.track()
        child, item = self.child()
        owner = self.service._get('tasks', self.task['id'])
        owner.update(status='completed', _waiting_on=[])
        child['status'] = 'completed'
        self.service._put('tasks', owner)
        self.service._put('tasks', child)
        self.service._sync_automatic_work()
        self.assertEqual(self.service._get('artifacts', item['id'])['_work_parent_id'], parent['id'])

    def test_completed_handoff_stays_linked_after_a_followup_changes_the_exchange(self):
        parent = self.track()
        child, item = self.child()
        owner = self.service._get('tasks', self.task['id'])
        owner.update(status='completed', _waiting_on=[], exchange_id='followup-exchange')
        child['status'] = 'completed'
        self.service._put('tasks', owner)
        self.service._put('tasks', child)
        self.service._sync_automatic_work()
        self.assertEqual(self.service._get('artifacts', item['id'])['_work_parent_id'], parent['id'])

    def test_callback_handoff_uses_the_original_assignments_row(self):
        parent = self.track()
        owner = self.service._get('tasks', self.task['id'])
        callback = dict(owner, id='delegating-callback', _assignment_task_id=owner['id'],
                        _is_return=True, created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        bot = self.write('POST', '/v1/bots', dict(name='Helper', role='Verify'))
        message = self.service._store_bot_message(callback['id'], dict(target_bot_id=bot['id'], text='Prepare the export', require_response=True))
        child = self.service._get('tasks', message['task_id'])
        child['status'] = 'working'
        self.service._put('tasks', child)
        item = self.service._register_work(child['id'], dict(key='account-export', title='Prepare the export', activity='Checking data', status='working'))
        self.assertEqual(self.service._get('artifacts', item['id'])['_work_parent_id'], parent['id'])

    def test_completion_keeps_the_handoff_in_one_row(self):
        parent = self.track()
        _, child = self.child()
        stored = self.service._get('artifacts', parent['id'])
        stored['status'] = 'completed'
        self.service._put('artifacts', stored)
        self.service._sync_automatic_work()
        saved = self.service._get('artifacts', child['id'])
        self.assertTrue(saved['_hidden'])
        self.assertEqual(saved['_work_parent_id'], parent['id'])

    def test_existing_duplicate_is_reconciled_during_queue_maintenance(self):
        parent = self.track()
        _, child = self.child()
        stored = self.service._get('artifacts', child['id'])
        stored.pop('_work_parent_id')
        stored['_hidden'] = False
        self.service._put('artifacts', stored)
        self.service._sync_automatic_work()
        self.assertEqual(self.service._get('artifacts', child['id'])['_work_parent_id'], parent['id'])


if __name__ == '__main__':
    unittest.main()
