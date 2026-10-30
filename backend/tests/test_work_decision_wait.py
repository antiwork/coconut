import unittest

from coconut.service import Service, ServiceError
import test_work_progress as fixtures
from test_work_progress import progress
from coconut.answer_undo import AnswerUndo
# These tests check what an answer does, so it applies at once. test_answer_undo.py tests the Undo window.
AnswerUndo.answer_undo_seconds = 0


class WorkDecisionWaitTests(unittest.TestCase):
    setUp = fixtures.WorkProgressTests.setUp
    tearDown = fixtures.WorkProgressTests.tearDown
    write = fixtures.WorkProgressTests.write

    def card(self):
        with self.service.transaction():
            parent = self.service._get('tasks', self.parent['id'])
            parent['status'] = 'queued'
            self.service._put('tasks', parent)
        original = self.provider.start_task
        def ask(task, bot, history, emit):
            emit('native_question', {'title': 'Approve the appearance?', 'options': [
                {'id': 'publish', 'label': 'Approve appearance', 'explanation': 'Continue the approved publication.'},
                {'id': 'revise', 'label': 'Request changes', 'explanation': 'Keep publication paused.'}]})
        self.provider.start_task = ask
        self.service._run_task(self.parent['id'])
        self.provider.start_task = original
        return self.service._all('decisions')[-1]

    def wait(self, card):
        original = self.provider.start_task
        def start(task, bot, history, emit):
            self.listing = emit('work_wait', {})
            self.result = emit('work_wait', {'decision_id': card['id'], 'reason': 'Waiting for appearance feedback'})
            emit('completed', {'summary': 'Waiting for feedback.', '_work_todos': progress()})
        self.provider.start_task = start
        self.service._run_task(self.child)
        self.provider.start_task = original
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        self.assertNotIn('_work_retry', self.service._get('tasks', self.child))

    def test_child_waits_for_parent_card_and_receives_requested_change_once_after_restart(self):
        card = self.card()
        self.wait(card)
        self.assertIn(card['id'], {d['id'] for d in self.listing['decisions']})
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.write(f"/v1/decisions/{card['id']}/answer", {'option_id': 'revise', 'revision': card['revision']})
        self.service._drain_process_completions()
        task = self.service._get('tasks', self.child)
        self.assertEqual(task['status'], 'queued')
        self.assertIn('revise', task['_background_completion'][0])
        self.assertIn('does not approve publication', task['_background_completion'][0])
        after = task['_background_return_id']
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['_background_return_id'], after)

    def test_cancelled_waiter_stays_cancelled(self):
        card = self.card(); self.wait(card)
        self.write(f'/v1/tasks/{self.child}/cancel', {})
        self.assertNotEqual(self.service._get('decisions', card['id'])['status'], 'pending')
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'cancelled')

    def test_dismissal_undo_preserves_delivery_of_the_later_answer(self):
        card = self.card()
        path = f"/v1/decisions/{card['id']}/inbox"
        dismissed = self.write(path, {'action': 'dismiss', 'revision': card['revision']})
        self.wait(card)
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        restored = self.write(path, {'action': 'restore', 'revision': card['revision'],
                                     'swipe_id': dismissed['inbox_swipe_id']})
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        self.write(f"/v1/decisions/{card['id']}/answer", {'option_id': 'revise', 'revision': restored['revision']})
        self.service._drain_process_completions()
        self.assertIn('revise', self.service._get('tasks', self.child)['_background_completion'][0])

    def test_wait_rejects_other_exchange_and_multiple_targets(self):
        card = self.card()
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', task)
        with self.assertRaises(ServiceError):
            self.service.wait_for_task(self.child, {'decision_id': card['id'], 'task_id': self.parent['id'], 'reason': 'Waiting'})
        with self.service.transaction():
            owner = self.service._get('tasks', self.parent['id'])
            owner['exchange_id'] = 'other-exchange'
            self.service._put('tasks', owner)
        with self.assertRaises(ServiceError):
            self.service.wait_for_task(self.child, {'decision_id': card['id'], 'reason': 'Waiting'})


if __name__ == '__main__':
    unittest.main()
