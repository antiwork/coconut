import unittest

import test_work_progress as fixtures


class SavedDelegationResultTests(unittest.TestCase):
    setUp = fixtures.WorkProgressTests.setUp
    tearDown = fixtures.WorkProgressTests.tearDown
    write = fixtures.WorkProgressTests.write

    def interrupted_return(self):
        self.provider.mode = 'progress'
        self.service._run_task(self.child)
        update = next(t for t in self.service._all('tasks') if t.get('_is_return'))
        self.service._run_task(update['id'])
        self.provider.mode = 'done'
        self.service._run_task(self.child)
        child = self.service._get('tasks', self.child)
        child.pop('_return_pending', None)
        child['_native_relay_result'] = dict(text='Published the validated patch.', failed=False,
                                           sender_bot_id=self.desk['id'], envelope_id='saved-envelope')
        self.service._put('tasks', child)
        parent = self.service._get('tasks', self.parent['id'])
        parent['_conversation_epoch'] = self.service._setting('conversation_epoch')
        self.service._put('tasks', parent)
        return child

    def test_saved_final_result_releases_a_parent_after_an_earlier_progress_reply(self):
        self.interrupted_return()
        self.service._drain_returns()
        replies = [t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == self.child]
        self.assertEqual(len(replies), 1)
        self.service._run_task(replies[0]['id'])
        self.assertEqual(self.service._get('tasks', self.parent['id'])['status'], 'completed')
        self.service._saved_return_check_at = 0
        self.service._drain_returns()
        self.assertEqual(len([t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == self.child]), 1)

    def test_cancelled_parent_does_not_receive_the_saved_result(self):
        self.interrupted_return()
        parent = self.service._get('tasks', self.parent['id'])
        parent['status'] = 'cancelled'
        self.service._put('tasks', parent)
        self.service._drain_returns()
        self.assertFalse(any(t.get('_resolved_dependency_id') == self.child for t in self.service._all('tasks')))

    def test_an_old_child_session_cannot_resume_the_parent(self):
        child = self.interrupted_return()
        child['session_id'] = 'old-session'
        self.service._put('tasks', child)
        self.service._drain_returns()
        self.assertFalse(any(t.get('_resolved_dependency_id') == self.child for t in self.service._all('tasks')))
