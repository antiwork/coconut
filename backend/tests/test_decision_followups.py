import unittest

from coconut.service import ServiceError, YieldForDecision
import test_decision_inbox as inbox_tests
from coconut.answer_undo import AnswerUndo
# These tests check what an answer does, so it applies at once. test_answer_undo.py tests the Undo window.
AnswerUndo.answer_undo_seconds = 0


class DecisionFollowupTests(unittest.TestCase):
    setUp = inbox_tests.DecisionInboxTests.setUp
    tearDown = inbox_tests.DecisionInboxTests.tearDown
    write = inbox_tests.DecisionInboxTests.write
    event = inbox_tests.DecisionInboxTests.event
    restart = inbox_tests.DecisionInboxTests.restart
    question = inbox_tests.DecisionInboxTests.question

    def followup(self, **extra):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Allow the report?', context='The original ruling remains unavailable.',
                uncertainty='Only a summary was found.', options=[dict(id='wait', label='Find the original ruling')], **extra))
        return self.service.snapshot()['decisions'][-1]

    def test_check_answer_links_the_followup_without_approving_it(self):
        original = self.question()
        self.write('POST', f"/v1/decisions/{original['id']}/answer", dict(option_id='check', revision=1))
        followup = self.followup()
        self.restart()
        saved = self.service._get('decisions', followup['id'])
        self.assertEqual(saved['previous_decision_id'], original['id'])
        self.assertEqual(saved['uncertainty'], 'Only a summary was found.')
        self.assertEqual(saved['status'], 'pending')
        self.assertIsNone(saved.get('selected_option_id'))
        self.assertEqual(self.service._get('decisions', original['id'])['selected_option_label'], 'Check references')

    def test_later_turn_can_link_an_answer_from_the_same_task(self):
        original = self.question()
        self.write('POST', f"/v1/decisions/{original['id']}/answer", dict(option_id='check', revision=1))
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task.pop('_resume_decision')
            self.service._put('tasks', task)
        followup = self.followup(previous_decision_id=original['id'])
        self.assertEqual(followup['previous_decision_id'], original['id'])

    def test_pending_missing_and_other_task_questions_cannot_be_linked(self):
        original = self.question()
        for previous_id in (original['id'], 'missing', 123, []):
            with self.subTest(previous_id=previous_id), self.assertRaises(ServiceError):
                self.followup(previous_decision_id=previous_id)
        self.write('POST', f"/v1/decisions/{original['id']}/answer", dict(option_id='check', revision=1))
        with self.service.transaction():
            saved = self.service._get('decisions', original['id'])
            saved['task_id'] = 'unrelated-task'
            self.service._put('decisions', saved)
        with self.assertRaises(ServiceError):
            self.followup(previous_decision_id=original['id'])
        self.assertEqual(len(self.service._all('decisions')), 1)

    def test_fresh_question_has_no_followup_reference(self):
        self.assertNotIn('previous_decision_id', self.question())
