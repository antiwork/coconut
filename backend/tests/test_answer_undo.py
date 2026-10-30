"""The Undo window of an answer: the Mac holds the answer, Undo returns the card, and the window end applies it."""
from datetime import datetime, timedelta, timezone
import unittest

import test_native_execution as native_tests
from coconut.answer_undo import ANSWER_UNDO_SECONDS, AnswerUndo
from coconut.service import ServiceError, YieldForDecision

# test_native_execution applies answers at once. These tests check the window.
AnswerUndo.answer_undo_seconds = ANSWER_UNDO_SECONDS


def later(seconds=ANSWER_UNDO_SECONDS + 1):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class AnswerUndoTests(unittest.TestCase):
    setUp = native_tests.NativeExecutionTests.setUp
    tearDown = native_tests.NativeExecutionTests.tearDown
    write = native_tests.NativeExecutionTests.write
    event = native_tests.NativeExecutionTests.event
    restart = native_tests.NativeExecutionTests.restart

    def question(self):
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Publish the notes?', options=[
                dict(id='publish', label='Publish'), dict(id='wait', label='Wait')]))
        return self.service.snapshot()['decisions'][-1]

    def answer(self, decision, **data):
        return self.write('POST', f"/v1/decisions/{decision['id']}/answer",
                          dict(dict(option_id='publish', revision=decision['revision']), **data))

    def undo(self, held, **data):
        return self.write('POST', f"/v1/decisions/{held['id']}/inbox",
                          dict(dict(action='undo_answer', revision=held['revision'], undo_id=held['answer_undo_id']), **data))

    def stored(self, decision):
        return self.service._get('decisions', decision['id'])

    def task_status(self):
        return self.service._get('tasks', self.task['id'])['status']

    def test_the_mac_holds_an_answer_for_the_window_and_the_task_waits(self):
        decision = self.question()
        held = self.answer(decision)
        self.assertEqual(held['status'], 'held')
        self.assertEqual(held['revision'], decision['revision'])
        self.assertEqual(held['held_answer_label'], 'Publish')
        deadline = datetime.fromisoformat(held['answer_undo_until'].replace('Z', '+00:00'))
        self.assertAlmostEqual((deadline - datetime.now(timezone.utc)).total_seconds(), ANSWER_UNDO_SECONDS, delta=2)
        self.assertNotIn('_held_answer', held)
        self.assertEqual(self.task_status(), 'needs_you')
        stored = self.stored(decision)
        self.assertEqual(stored['_answers'], [])
        # The stored card stays pending, so every part of the Mac that waits for it still waits.
        self.assertEqual(stored['status'], 'pending')
        self.assertTrue(self.service._decision_wait_pending(stored))
        self.assertEqual([d['status'] for d in self.service.snapshot()['decisions']], ['held'])
        # Nothing else can act on the card in the window: no second answer, no reply, no dismissal.
        for path, data in (('answer', dict(option_id='wait', revision=decision['revision'])),
                           ('reply', dict(text='Wait', revision=decision['revision'])),
                           ('inbox', dict(action='dismiss', revision=decision['revision']))):
            with self.subTest(path=path), self.assertRaises(ServiceError) as raised:
                self.write('POST', f"/v1/decisions/{decision['id']}/{path}", data)
            self.assertEqual(raised.exception.status, 409)
        # The window has not ended, so the sweep changes nothing.
        self.service.finish_held_answers()
        self.assertIn('_held_answer', self.stored(decision))

    def test_undo_returns_the_card_to_pending_with_the_same_revision_and_records_it(self):
        decision = self.question()
        held = self.answer(decision)
        restored = self.undo(held)
        self.assertEqual(restored['status'], 'pending')
        self.assertEqual(restored['revision'], decision['revision'])
        self.assertIsNone(restored['answer_undo_until'])
        stored = self.stored(decision)
        self.assertEqual(stored['_answers'], [])
        self.assertEqual(stored['_undone_answers'][0]['data']['option_id'], 'publish')
        self.assertEqual(self.task_status(), 'needs_you')
        self.service.finish_held_answers(later())
        self.assertEqual(self.stored(decision)['status'], 'pending')
        # The card takes a new answer as before.
        held_again = self.answer(decision, option_id='wait')
        self.service.finish_held_answers(later())
        self.assertEqual(self.stored(held_again)['selected_option_id'], 'wait')

    def test_an_undo_with_another_id_or_after_the_window_is_refused(self):
        decision = self.question()
        held = self.answer(decision)
        with self.assertRaises(ServiceError) as raised:
            self.undo(held, undo_id='another')
        self.assertEqual(raised.exception.status, 409)
        with self.service.transaction():
            stored = self.stored(decision)
            stored['answer_undo_until'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            self.service._put('decisions', stored)
        with self.assertRaises(ServiceError) as raised:
            self.undo(held)
        self.assertIn('The Undo window ended', str(raised.exception))
        self.assertIn('_held_answer', self.stored(decision))

    def test_the_window_end_applies_the_answer_and_a_later_undo_is_refused(self):
        decision = self.question()
        held = self.answer(decision)
        self.service.finish_held_answers(later())
        answered = self.stored(decision)
        self.assertEqual(answered['status'], 'approved')
        self.assertEqual(answered['selected_option_id'], 'publish')
        self.assertIsNone(answered['answer_undo_until'])
        self.assertNotIn('_held_answer', answered)
        self.assertEqual(self.task_status(), 'queued')
        with self.assertRaises(ServiceError) as raised:
            self.undo(held)
        self.assertIn('no longer waiting', str(raised.exception))

    def test_a_typed_reply_waits_for_the_window_too(self):
        decision = self.question()
        held = self.answer(decision, option_id=None, choice='reply', correction='Publish after the review.')
        self.assertEqual(held['status'], 'held')
        self.assertEqual(held['held_answer_label'], 'Publish after the review.')
        self.assertFalse([m for m in self.service._all('messages') if m.get('decision_id') == decision['id'] and m['role'] == 'user'])
        self.service.finish_held_answers(later())
        self.assertEqual(self.stored(decision)['status'], 'replied')
        self.assertTrue([m for m in self.service._all('messages') if m.get('decision_id') == decision['id'] and m['role'] == 'user'])

    def test_a_restart_in_the_window_keeps_the_answer_and_the_mac_applies_it_at_the_end(self):
        decision = self.question()
        self.answer(decision)
        self.restart()
        self.assertIn('_held_answer', self.stored(decision))
        self.service.finish_held_answers()
        self.assertIn('_held_answer', self.stored(decision))
        self.service.finish_held_answers(later())
        self.assertEqual(self.stored(decision)['status'], 'approved')
        self.assertEqual(self.task_status(), 'queued')

    def test_the_dispatch_pass_applies_due_answers(self):
        decision = self.question()
        self.answer(decision)
        with self.service.transaction():
            stored = self.stored(decision)
            stored['answer_undo_until'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            self.service._put('decisions', stored)
        self.service.finish_inbox_swipes()
        self.assertEqual(self.stored(decision)['status'], 'approved')

    def test_an_answer_that_no_longer_fits_returns_the_card_to_inbox(self):
        decision = self.question()
        self.answer(decision)
        with self.service.transaction():
            stored = self.stored(decision)
            stored['revision'] += 1
            self.service._put('decisions', stored)
        with self.assertLogs('coconut.answer_undo', 'WARNING'):
            self.service.finish_held_answers(later())
        stored = self.stored(decision)
        self.assertEqual(stored['status'], 'pending')
        self.assertIsNone(stored['answer_undo_until'])
        self.assertEqual(stored['_answers'], [])
        self.assertEqual(self.task_status(), 'needs_you')

    def test_a_task_cancelled_in_the_window_keeps_its_cancellation(self):
        decision = self.question()
        self.answer(decision)
        self.write('POST', f"/v1/tasks/{self.task['id']}/cancel", {})
        self.service.finish_held_answers(later())
        stored = self.stored(decision)
        self.assertNotIn('_held_answer', stored)
        self.assertEqual(stored['_answers'], [])
        self.assertEqual(self.task_status(), 'cancelled')

    def test_a_wrong_answer_fails_at_once(self):
        decision = self.question()
        for data, status in ((dict(option_id='missing'), 400), (dict(option_id=None, choice='reply', correction=' '), 400),
                             (dict(revision=decision['revision'] + 1), 409), (dict(option='publish'), 400)):
            with self.subTest(data=data), self.assertRaises(ServiceError) as raised:
                self.answer(decision, **data)
            self.assertEqual(raised.exception.status, status)
        self.assertNotIn('_held_answer', self.stored(decision))

    def test_a_reply_that_finishes_in_the_window_does_not_replace_the_held_answer(self):
        decision = self.question()
        self.write('POST', f"/v1/decisions/{decision['id']}/reply", {'text': 'Wait for the review.', 'revision': decision['revision']})
        context = self.service._all('contexts')[-1]
        with self.service.transaction():
            context['status'] = 'working'
            self.service._put('contexts', context)
        self.answer(decision)
        with self.assertRaises(ServiceError) as raised:
            self.service.resolve_decision_reply(context['id'], dict(choice='reject', quote='Wait for the review.'))
        self.assertEqual(raised.exception.status, 409)
        stored = self.stored(decision)
        self.assertIn('_held_answer', stored)
        self.assertEqual(stored['_answers'], [])
        self.assertEqual(self.task_status(), 'needs_you')
        self.service.finish_held_answers(later())
        self.assertEqual(self.stored(decision)['selected_option_id'], 'publish')

    def test_a_revision_of_the_card_waits_for_the_held_answer(self):
        decision = self.question()
        self.answer(decision)
        task = self.service._get('tasks', self.task['id'])
        with self.service.transaction(), self.assertRaises(ServiceError):
            self.service._revise_question(self.stored(decision), task, dict(
                title='Publish later?', options=[dict(id='later', label='Later')]), 'A check changed the question.')
        self.assertEqual(self.stored(decision)['revision'], decision['revision'])

    def test_an_archived_bot_wins_and_keeps_the_held_answer_as_superseded(self):
        decision = self.question()
        held = self.answer(decision)
        with self.service.transaction():
            stored = self.stored(decision)
            stored['status'] = 'rejected'
            self.service._put('decisions', stored)
        with self.assertRaises(ServiceError):
            self.undo(held)
        self.service.finish_held_answers(later())
        stored = self.stored(decision)
        self.assertEqual(stored['status'], 'rejected')
        self.assertEqual(stored['_superseded_answers'][0]['data']['option_id'], 'publish')

    def test_undo_replays_as_a_card_change(self):
        decision = self.question()
        cursor = self.service._cursor()
        self.undo(self.answer(decision))
        changes = self.service._replay_changes(cursor, collection_deltas=True)
        self.assertIsNotNone(changes)

    def test_a_retried_answer_request_returns_the_same_hold(self):
        decision = self.question()
        key = '4c2e7f5e-8c2a-4d3a-9f0e-2a6b1c9d7e11'
        data = dict(option_id='publish', revision=decision['revision'])
        first = self.service.mutate('POST', f"/v1/decisions/{decision['id']}/answer", data, key)
        second = self.service.mutate('POST', f"/v1/decisions/{decision['id']}/answer", data, key)
        self.assertEqual(first['answer_undo_id'], second['answer_undo_id'])


if __name__ == '__main__':
    unittest.main()
