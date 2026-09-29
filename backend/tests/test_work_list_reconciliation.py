import threading
import unittest

import test_automatic_work as automatic
import test_tracked_work as tracked
import test_decision_inbox as inbox
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class WorkListReconciliationTests(unittest.TestCase):
    setUp = automatic.AutomaticWorkTests.setUp
    tearDown = automatic.AutomaticWorkTests.tearDown
    write = automatic.AutomaticWorkTests.write
    start = automatic.AutomaticWorkTests.start
    sync = automatic.AutomaticWorkTests.sync
    track = tracked.TrackedWorkTests.track
    row = tracked.TrackedWorkTests.row

    def callback_chain(self, status='queued'):
        parent = self.start()
        self.track(status='blocked', activity='Preparation stopped')
        parent['status'] = 'completed'
        self.service._put('tasks', parent)
        child = dict(parent, id='child', status='completed', source_task_id=parent['id'],
                     _native_relay={'envelope_id': 'handoff'})
        callback = dict(parent, id='callback', status=status, source_task_id='child',
                        _is_return=True, _assignment_task_id='earlier-assignment')
        self.service._put('tasks', child)
        self.service._put('tasks', callback)
        self.service.running.clear()
        return callback

    def test_result_callback_replaces_the_old_blocked_status(self):
        self.callback_chain()
        self.assertEqual(self.row()['activity'], 'Queued')
        self.assertEqual(self.row()['activity_state'], 'waiting')

    def test_callback_receives_the_existing_work_as_its_current_item(self):
        callback = self.callback_chain()
        items = self.service._tracked_items_input(callback)
        self.assertTrue(items[0]['current_task'])

    def test_live_callback_shows_progress_without_an_old_blocker(self):
        callback = self.callback_chain(status='working')
        self.service.running[callback['id']] = threading.current_thread()
        self.assertEqual(self.row()['activity'], 'In progress')
        self.assertEqual(self.row()['activity_state'], 'working')

    def test_finished_callback_does_not_complete_unverified_work(self):
        self.callback_chain(status='completed')
        self.assertEqual(self.row()['status'], 'blocked')
        self.assertEqual(self.row()['activity'], 'Preparation stopped')

    def test_old_session_callback_cannot_claim_progress(self):
        callback = self.callback_chain(status='working')
        callback['session_id'] = 'old-session'
        self.service._put('tasks', callback)
        self.service.running[callback['id']] = threading.current_thread()
        self.assertEqual(self.row()['status'], 'blocked')

    def test_saved_wait_keeps_pr_coverage_after_a_human_changes_exchange(self):
        parent = self.start()
        self.sync()
        self.service._register_pr_artifact(parent['id'], {'url': automatic.fixtures.URL})
        artifact = self.service._all('artifacts')[-1]
        child = dict(parent, id='child', source_task_id=parent['id'],
                     _native_relay={'envelope_id': 'handoff'})
        self.service._put('tasks', child)
        artifact['task_id'] = child['id']
        self.service._put('artifacts', artifact)
        parent.update(exchange_id=self.service._new_exchange(), _waiting_on=['child'])
        self.service._put('tasks', parent)
        self.assertEqual(self.sync(), [])

    def test_existing_proposal_row_uses_its_linked_title(self):
        parent = self.start(title='Continue this proposal using the recorded reply. Proposal: Fix country fields')
        item = self.sync()[0]
        self.service._put('decisions', dict(id='proposal', bot_id=parent['bot_id'], task_id='earlier',
            title='Fix country fields', status='approved', created_at=parent['created_at'],
            _execution_task_id=parent['id']))
        self.service._sync_automatic_work()
        self.assertEqual(self.service._get('artifacts', item['id'])['title'], 'Fix country fields')

    def test_different_exchange_without_a_saved_wait_stays_separate(self):
        parent = self.start()
        child = dict(parent, id='child', exchange_id='unrelated', source_task_id=parent['id'],
                     _native_relay={'envelope_id': 'handoff'})
        self.service._put('tasks', child)
        chain = self.service._work_chain(parent, {parent['id']: parent, 'child': child}, [], include_continuations=True)
        self.assertEqual(set(chain), {parent['id']})


class ProposalWorkTitleTests(unittest.TestCase):
    setUp = inbox.DecisionInboxTests.setUp
    tearDown = inbox.DecisionInboxTests.tearDown
    write = inbox.DecisionInboxTests.write
    event = inbox.DecisionInboxTests.event
    question = inbox.DecisionInboxTests.question

    def test_answer_keeps_context_but_uses_the_proposal_title(self):
        decision = self.question(optional=True)
        before = self.service._all('messages')
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'check', 'revision': 1})
        decision = self.service._get('decisions', decision['id'])
        task = self.service._get('tasks', decision['_execution_task_id'])
        self.assertEqual(task['title'], decision['title'])
        self.assertEqual(task['_resume_decision'], decision['id'])
        self.assertEqual(decision['selected_option_label'], 'Check references')
        self.assertEqual(self.service._all('messages'), before)


class ProposalWorkOwnershipTests(unittest.TestCase):
    setUp = tracked.TrackedWorkTests.setUp
    tearDown = tracked.TrackedWorkTests.tearDown
    write = tracked.TrackedWorkTests.write

    def pending_followup(self):
        from coconut.service import YieldForDecision
        parent = self.service._get('tasks', self.task['id'])
        parent.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', parent)
        pr = self.service._register_pr_artifact(parent['id'], dict(url=automatic.fixtures.URL))
        self.service.emit(parent['id'], 'native_proposal', dict(key='fix-export', title='Fix the export?',
            options=[dict(id='fix', label='Fix and update the PR'), dict(id='leave', label='Leave unchanged')]))
        proposal = self.service._all('decisions')[-1]
        self.write('POST', '/v1/decisions/' + proposal['id'] + '/answer', dict(option_id='fix', revision=1))
        proposal = self.service._get('decisions', proposal['id'])
        followup = self.service._get('tasks', proposal['_execution_task_id'])
        followup.update(status='working', _conversation_epoch=parent['_conversation_epoch'], _native_turn_id='proposal-turn')
        self.service._put('tasks', followup)
        bot = self.write('POST', '/v1/bots', dict(name='Developer'))
        message = self.service._store_bot_message(followup['id'], dict(target_bot_id=bot['id'],
            text='Prepare the approved export fix', require_response=True))
        child = self.service._get('tasks', message['task_id'])
        child.update(status='working', _conversation_epoch=parent['_conversation_epoch'], _native_turn_id='display-turn')
        self.service._put('tasks', child)
        with self.assertRaises(YieldForDecision):
            self.service.emit(child['id'], 'native_question', dict(title='Approve the display?',
                options=[dict(id='approve', label='Approve display'), dict(id='revise', label='Revise display')]))
        card = self.service._all('decisions')[-1]
        self.service.wait_for_task(followup['id'], dict(task_id=child['id'], reason='Waiting for the prepared fix'))
        followup = self.service._get('tasks', followup['id'])
        followup.update(status='waiting')
        self.service._put('tasks', followup)
        parent.update(status='completed')
        self.service._put('tasks', parent)
        return parent, pr, proposal, followup, child, card

    def row(self, pr):
        return next(a for a in self.service.snapshot()['artifacts'] if a['id'] == pr['id'])

    def test_approved_proposal_keeps_its_followup_card_and_prevents_recovery(self):
        parent, pr, _, followup, child, card = self.pending_followup()
        row = self.row(pr)
        self.assertEqual(row['work_state'], 'needs_decision')
        self.assertEqual(row['work_decision_id'], card['id'])
        before = len(self.service._all('tasks'))
        for _ in range(3): self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)
        parent.update(status='working')
        self.service._put('tasks', parent)
        listing = self.service.wait_for_task(parent['id'], {})
        self.assertIn(card['id'], {d['id'] for d in listing['decisions']})
        self.service.wait_for_task(parent['id'], dict(decision_id=card['id'], reason='Waiting for display feedback'))
        self.write('POST', '/v1/decisions/' + card['id'] + '/answer', dict(option_id='revise', revision=card['revision']))
        self.assertEqual(self.service._get('tasks', child['id'])['status'], 'queued')
        self.assertNotEqual(self.service._get('tasks', followup['id'])['status'], 'queued')

    def test_new_coordinator_report_keeps_the_proposal_card(self):
        from coconut.service import now
        parent, pr, _, _, _, card = self.pending_followup()
        callback = dict(parent, id='status-callback', status='waiting', exchange_id=self.service._new_exchange(),
            _is_return=True, _assignment_task_id=parent['id'], created_at=now(), _native_turn_id='status-turn')
        self.service._put('tasks', callback)
        self.service._set_work_activity(self.service._get('artifacts', pr['id']), callback,
            dict(activity='Waiting for display feedback', activity_state='blocked'))
        self.assertEqual(self.row(pr)['work_decision_id'], card['id'])

    def test_existing_recovery_keeps_the_saved_proposal_continuation(self):
        parent, pr, proposal, _, _, card = self.pending_followup()
        # Reproduce the old service state where recovery could not see this link.
        unlinked = dict(proposal)
        unlinked.pop('_execution_task_id')
        self.service._put('decisions', unlinked)
        self.service._queue_work_recoveries()
        owner = self.service._get('artifacts', pr['id'])['_review_task_id']
        self.assertNotEqual(owner, parent['id'])
        self.service._put('decisions', proposal)
        self.assertEqual(self.row(pr)['work_decision_id'], card['id'])
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)

    def test_cancellation_stops_the_approved_proposal_followup(self):
        _, pr, _, followup, child, card = self.pending_followup()
        self.write('POST', '/v1/work/items/' + pr['id'] + '/cancel', {})
        self.assertEqual(self.service._get('tasks', followup['id'])['status'], 'cancelled')
        self.assertEqual(self.service._get('tasks', child['id'])['status'], 'cancelled')
        self.assertNotEqual(self.service._get('decisions', card['id'])['status'], 'pending')

    def test_proposal_link_cannot_claim_an_unrelated_or_old_session_task(self):
        parent, pr, proposal, followup, child, card = self.pending_followup()
        original = dict(followup)
        self.service._put('tasks', dict(parent, id='unrelated', parent_task_id=None))
        for changes in (dict(parent_task_id='unrelated'), dict(session_id='old-session')):
            self.service._put('tasks', dict(original, **changes))
            self.assertNotEqual(self.row(pr)['work_decision_id'], card['id'])
        self.service._put('tasks', original)
        self.assertEqual(self.row(pr)['work_decision_id'], card['id'])


if __name__ == '__main__':
    unittest.main()
