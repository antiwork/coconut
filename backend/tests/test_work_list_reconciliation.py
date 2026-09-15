import threading
import unittest

import test_automatic_work as automatic
import test_tracked_work as tracked
import test_decision_inbox as inbox


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


if __name__ == '__main__':
    unittest.main()
