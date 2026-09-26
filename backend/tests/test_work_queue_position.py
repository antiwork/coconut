import threading
import time
import unittest

import test_tracked_work as fixtures


class WorkQueuePositionTests(unittest.TestCase):
    setUp = fixtures.TrackedWorkTests.setUp
    tearDown = fixtures.TrackedWorkTests.tearDown
    track = fixtures.TrackedWorkTests.track
    row = fixtures.TrackedWorkTests.row
    write = fixtures.TrackedWorkTests.write

    def update(self, task, **fields):
        task = self.service._get('tasks', task['id'])
        task.update(fields)
        self.service._put('tasks', task)
        return task

    def positions(self):
        return self.service._queue_positions({t['id']: t for t in self.service._all('tasks')}, time.time())

    def send(self, bot=None, **fields):
        return self.write('POST', f'/v1/bots/{(bot or self.bot)["id"]}/messages',
                          dict(text='Inspect the next issue', mode='queue', **fields))

    def test_position_matches_global_dispatch_order_and_excludes_future_work(self):
        self.update(self.task, status='queued')
        future = self.send(scheduled_at='2099-01-01T00:00:00Z')
        other = self.write('POST', '/v1/bots', dict(name='Other'))
        independent = self.send(other)
        next_task = self.send()
        self.assertEqual(self.positions(), {self.task['id']: 1, independent['id']: 2, next_task['id']: 3})
        self.update(self.task, status='working')
        self.assertEqual(self.positions()[next_task['id']], 2)
        self.assertNotIn(future['id'], self.positions())
        self.update(next_task, _work_status_only=True)
        self.assertNotIn(next_task['id'], self.positions())

    def test_returned_result_runs_before_routine_source_audits(self):
        audit = self.update(self.task, status='queued', _source_audit_prompt='Check source coverage')
        result = self.update(self.send(), _is_return=True)
        self.assertEqual(self.positions(), {result['id']: 1, audit['id']: 2})
        tasks = self.service._dispatch_order(self.service._all('tasks'))
        self.assertEqual([t['id'] for t in tasks], [result['id'], audit['id']])

    def test_result_and_decision_continuations_run_before_new_work(self):
        ordinary = self.update(self.task, status='queued')
        returned = self.update(self.send(), _is_return=True)
        process = self.update(self.send(), _background_completion=['Tests passed'])
        decision = self.update(self.send(), _resume_decision='approved-card')
        self.assertEqual(self.positions(), {returned['id']: 1, process['id']: 2,
                                            decision['id']: 3, ordinary['id']: 4})

    def test_blocked_continuations_do_not_claim_a_queue_position(self):
        self.update(self.task, status='needs_you')
        child = self.send()
        self.update(child, _assignment_task_id=self.task['id'])
        other = self.send()
        self.assertEqual(self.positions(), {other['id']: 1})
        self.update(self.task, status='completed')
        self.assertEqual(self.positions(), {child['id']: 1, other['id']: 2})

    def test_work_exposes_position_and_clears_it_when_execution_starts(self):
        self.track()
        self.update(self.task, status='queued')
        self.assertEqual(self.row()['work_queue_position'], 1)
        self.update(self.task, status='working')
        self.service.running[self.task['id']] = threading.current_thread()
        try:
            self.assertIsNone(self.row()['work_queue_position'])
        finally:
            self.service.running.clear()

    def test_recovery_reply_keeps_the_original_outcome_title(self):
        original = self.update(self.task, status='failed')
        reply = self.write('POST', f'/v1/tasks/{original["id"]}/replies',
                           dict(text='Resume this interrupted task after the deployment. Inspect saved results first.'))
        self.assertEqual(reply['title'], original['title'])

    def test_cancel_stops_owned_attempts_but_preserves_other_work_and_wait_targets(self):
        item = self.track()
        child = self.send()
        child = self.update(child, source_task_id=self.task['id'])
        unrelated = self.send()
        self.update(unrelated, exchange_id=self.task['exchange_id'])
        dependency = self.send()
        self.update(self.task, _waiting_on=[dependency['id']])
        stopped = []
        self.provider.cancel_task = stopped.append
        path = f'/v1/work/items/{item["id"]}/cancel'
        self.write('POST', path, {})
        self.assertEqual(set(stopped), {self.task['id'], child['id']})
        self.assertEqual(self.service._get('tasks', unrelated['id'])['status'], 'queued')
        self.assertEqual(self.service._get('tasks', dependency['id'])['status'], 'queued')
        self.assertEqual(self.row()['work_state'], 'cancelled')
        self.assertFalse(self.service._get('exchanges', self.task['exchange_id']).get('cancelled'))
        self.write('POST', path, {})
        self.assertEqual(len(stopped), 2)
        self.assertNotIn(child['id'], self.positions())

    def test_cancelled_pr_does_not_restart_review_and_can_be_dismissed(self):
        from test_service_pr import URL
        self.update(self.task, status='working')
        item = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        self.update(self.task, status='completed')
        self.write('POST', f'/v1/work/items/{item["id"]}/cancel', {})
        saved = self.service._get('artifacts', item['id'])
        saved.update(pr_state='open', draft=False, checks_state='passed', head_sha='b' * 40)
        self.service._put('artifacts', saved)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'cancelled')
        self.assertEqual(self.row()['work_state'], 'cancelled')
        self.assertEqual(self.row()['pr_state'], 'open')
        self.write('POST', f'/v1/work/items/{item["id"]}/dismiss', dict(dismissal_id='cancelled'))
        self.service._restore_active_work(saved)
        self.assertEqual(self.row()['work_dismissal_id'], 'cancelled')

    def test_cancelled_work_rejects_late_reports_and_restart_recovery(self):
        from coconut.service import ServiceError
        item = self.track()
        self.update(self.task, status='failed')
        self.write('POST', f'/v1/work/items/{item["id"]}/cancel', {})
        self.service._recover()
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'cancelled')
        self.task = self.send()
        with self.assertRaises(ServiceError):
            self.track()
        self.assertEqual(self.row()['work_state'], 'cancelled')

    def test_cancel_receipt_contains_confirmed_state_and_replays_without_stopping_again(self):
        item = self.track()
        self.update(self.task, status='failed')
        stopped = []
        self.provider.cancel_task = stopped.append
        path = f'/v1/work/items/{item["id"]}/cancel'
        result = self.write('POST', path, {})
        self.assertTrue(result['ok'])
        self.assertEqual(result['artifacts'][0]['id'], item['id'])
        self.assertEqual(result['artifacts'][0]['work_state'], 'cancelled')
        self.assertIs(result['artifacts'][0]['checkout_recovery_stopped'], True)
        self.assertEqual(result['artifacts'][0]['activity'], 'Stopped')
        self.assertFalse(any(k.startswith('_') for k in result['artifacts'][0]))
        self.assertEqual(result['cursor'], self.service._cursor())
        again = self.write('POST', path, {})
        self.assertEqual(again['artifacts'][0]['work_state'], 'cancelled')
        self.assertEqual(again['cursor'], result['cursor'])
        self.assertEqual(stopped, [self.task['id']])
        self.assertNotIn('checkout_recovery_stopped', self.service._get('artifacts', item['id']))
        self.service.db.execute('DELETE FROM settings WHERE key=?', ('work.cancelled.work:' + item['id'],))
        self.assertNotIn('checkout_recovery_stopped', self.row())
        self.assertNotIn('checkout_recovery_stopped', self.service._work_projection([dict(result['artifacts'][0])])[0])

    def test_task_cancel_does_not_project_explicit_work_stop(self):
        item = self.track()
        stopped = self.write('POST', f'/v1/tasks/{self.task["id"]}/cancel', {})
        self.assertEqual(stopped['status'], 'cancelled')
        self.assertEqual(self.row()['work_state'], 'cancelled')
        self.assertNotIn('checkout_recovery_stopped', self.row())
        self.assertIsNone(self.service._setting('work.cancelled.work:' + item['id']))

    def test_shared_task_keeps_other_work_and_receives_scoped_stop(self):
        first = self.track()
        second = self.track(key='other-export', title='Export another account')
        self.write('POST', f'/v1/work/items/{first["id"]}/cancel', {})
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'working')
        rows = {a['id']: a for a in self.service.snapshot()['artifacts']}
        self.assertEqual(rows[first['id']]['work_state'], 'cancelled')
        self.assertNotEqual(rows[second['id']]['work_state'], 'cancelled')
        self.assertIn(first['id'], self.service._get('tasks', self.task['id'])['_steering'][-1])

    def test_explicit_resume_reopens_cancelled_work_with_new_attempt(self):
        first = self.track()
        self.write('POST', f'/v1/work/items/{first["id"]}/cancel', {})
        self.write('POST', f'/v1/work/items/{first["id"]}/resume', {})
        row = self.row()
        self.assertEqual(row['work_state'], 'queued')
        self.assertNotEqual(row['task_id'], self.task['id'])
        self.assertEqual(row['title'], first['title'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'cancelled')

    def test_shared_pr_owner_survives_and_explicit_resume_restores_review(self):
        from test_service_pr import URL, observation
        self.update(self.task, status='working')
        first = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        self.tracker.observations.append(observation())
        second = self.service._register_pr_artifact(self.task['id'], dict(url=URL.replace('/42', '/43')))
        self.update(self.task, status='completed')
        self.write('POST', f'/v1/work/items/{first["id"]}/cancel', {})
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')
        self.assertTrue(self.service._pr_review_source_current(self.service._get('artifacts', second['id'])))
        self.write('POST', f'/v1/work/items/{first["id"]}/resume', {})
        self.assertTrue(self.service._pr_review_source_current(self.service._get('artifacts', first['id'])))
        rows = {a['id']:a for a in self.service.snapshot()['artifacts']}
        self.assertEqual(rows[first['id']]['work_state'], 'queued')

    def test_resume_supports_legacy_task_cancellation(self):
        first = self.track()
        self.write('POST', f'/v1/tasks/{self.task["id"]}/cancel', {})
        self.assertEqual(self.row()['work_state'], 'cancelled')
        self.write('POST', f'/v1/work/items/{first["id"]}/resume', {})
        self.assertEqual(self.row()['work_state'], 'queued')

    def test_resume_transfers_duplicate_pr_owners(self):
        from test_service_pr import URL
        from coconut.service import uid
        self.update(self.task, status='working')
        first = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        other = self.write('POST', '/v1/bots', dict(name='Other'))
        peer = dict(self.service._get('artifacts', first['id']), id=uid(), bot_id=other['id'])
        self.service._put('artifacts', peer)
        self.write('POST', f'/v1/work/items/{first["id"]}/cancel', {})
        self.write('POST', f'/v1/work/items/{first["id"]}/resume', {})
        saved = [self.service._get('artifacts', i) for i in (first['id'], peer['id'])]
        self.assertIsNone(saved[1]['_review_task_id'])
        self.assertEqual(self.service._pr_review_owner(saved)['id'], first['id'])
        self.service._bind_pr_review(saved[1], self.service._get('tasks', self.task['id']))
        self.assertIsNone(self.service._get('artifacts', peer['id'])['_review_task_id'])
        self.service._migrate_pr_review_sources()
        self.assertIsNone(self.service._get('artifacts', peer['id'])['_review_task_id'])
        old = self.update(self.task, status='working')
        self.service._set_work_activity(saved[0], old, dict(activity='Old report', activity_state='blocked'))
        self.assertNotEqual(self.service._get('artifacts', first['id']).get('_activity'), 'Old report')
        self.assertNotEqual(saved[0]['_review_task_id'], self.task['id'])
        self.assertTrue(all(a['work_state'] == 'queued' for a in self.service.snapshot()['artifacts']))

    def test_cancelling_one_pr_resolves_its_card_and_unblocks_shared_task(self):
        from coconut.service import YieldForDecision
        from test_service_pr import URL, observation
        self.update(self.task, status='working')
        first = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        self.tracker.observations.append(observation())
        self.service._register_pr_artifact(self.task['id'], dict(url=URL.replace('/42', '/43')))
        revision = '2026-09-23T11:46:31Z'
        self.tracker.observe_issue = lambda url: dict(updated_at=revision)
        try:
            self.service.emit(self.task['id'], 'native_question', dict(title='Merge PR #42?', merge_pr_url=URL, source_revisions={URL: revision},
                options=[dict(id='merge', label='Merge', explanation='Merge this PR.'),
                         dict(id='leave', label='Leave open', explanation='Keep this PR open.')]))
        except YieldForDecision:
            pass
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        from coconut.service import uid
        unrelated = dict(self.service._all('decisions')[0], id=uid(), task_id=self.send()['id'], merge_pr_url=None)
        self.service._put('decisions', unrelated)
        self.write('POST', f'/v1/work/items/{first["id"]}/cancel', {})
        card = self.service.snapshot()['decisions'][0]
        self.assertEqual(card['status'], 'resolved')
        self.assertEqual(card['outcome'], 'Work cancelled.')
        from coconut.hermes_worker import conversation_input
        prompt, _ = conversation_input(dict(operation='resume', task=self.task, decision=card, history=[]))
        self.assertIn('Do not execute the cancelled action', prompt)
        self.assertNotIn('GitHub confirms', prompt)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')

    def test_shared_unlinked_decision_is_rechecked_after_work_scope_changes(self):
        from coconut.service import YieldForDecision
        from coconut.hermes_worker import conversation_input
        first = self.track()
        second = self.track(key='second', title='Export a second account')
        try:
            self.service.emit(self.task['id'], 'native_question', dict(title='Save these exports?',
                options=[dict(id='save', label='Save', explanation='Save the exports.'),
                         dict(id='leave', label='Leave', explanation='Do not save the exports.')]))
        except YieldForDecision:
            pass
        self.write('POST', f'/v1/work/items/{first["id"]}/cancel', {})
        card = self.service.snapshot()['decisions'][0]
        self.assertEqual(card['resolution'], 'work_scope_changed')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')
        rows = {a['id']:a for a in self.service.snapshot()['artifacts']}
        self.assertEqual(rows[first['id']]['work_state'], 'cancelled')
        self.assertEqual(rows[second['id']]['work_state'], 'queued')
        prompt, _ = conversation_input(dict(operation='resume', task=self.task, decision=card, history=[]))
        self.assertIn('Continue the other requested work', prompt)
        self.assertIn('Withdrawal is not approval', prompt)


if __name__ == '__main__':
    unittest.main()
