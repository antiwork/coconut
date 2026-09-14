import unittest

from coconut.service import ServiceError
import test_decision_followups as followup_tests


class FollowupConversationScopeTests(unittest.TestCase):
    setUp = followup_tests.DecisionFollowupTests.setUp
    tearDown = followup_tests.DecisionFollowupTests.tearDown
    write = followup_tests.DecisionFollowupTests.write
    event = followup_tests.DecisionFollowupTests.event
    restart = followup_tests.DecisionFollowupTests.restart
    question = followup_tests.DecisionFollowupTests.question
    followup = followup_tests.DecisionFollowupTests.followup

    def finish_answer(self, outcome):
        original = self.question()
        self.write('POST', f"/v1/decisions/{original['id']}/answer", dict(option_id='check', revision=1))
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.event(outcome, dict(summary='The evidence check ended.'))
        return self.service._get('decisions', original['id'])

    def test_completed_answer_links_from_a_later_message(self):
        original = self.finish_answer('completed')
        self.assertEqual(original['status'], 'executed')
        self.task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", dict(text='What happens next?', mode='queue'))
        self.assertNotEqual(self.task['id'], original['task_id'])
        result = self.followup(previous_decision_id=original['id'])
        self.restart()
        saved = self.service._get('decisions', result['id'])
        self.assertEqual(saved['previous_decision_id'], original['id'])
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(self.service._get('decisions', original['id']), original)

    def test_failed_execution_does_not_erase_the_earlier_answer(self):
        original = self.finish_answer('failed')
        self.assertEqual(original['status'], 'failed')
        self.task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", dict(text='How can we recover?', mode='queue'))
        result = self.followup(previous_decision_id=original['id'])
        self.assertEqual(result['previous_decision_id'], original['id'])
        self.assertEqual(result['status'], 'pending')

    def test_an_answer_in_another_conversation_cannot_be_linked(self):
        original = self.finish_answer('completed')
        other = self.write('POST', '/v1/bots', dict(name='Other', role=''))
        self.task = self.write('POST', f"/v1/bots/{other['id']}/messages", dict(text='A separate request.', mode='queue'))
        with self.assertRaises(ServiceError):
            self.followup(previous_decision_id=original['id'])
        self.assertEqual(len(self.service._all('decisions')), 1)
