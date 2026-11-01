import unittest
from unittest.mock import patch

import test_decision_inbox as inbox_tests
from turn_text import turn_input


class ContextCompletionTests(unittest.TestCase):
    setUp = inbox_tests.DecisionInboxTests.setUp
    tearDown = inbox_tests.DecisionInboxTests.tearDown
    write = inbox_tests.DecisionInboxTests.write
    event = inbox_tests.DecisionInboxTests.event
    question = inbox_tests.DecisionInboxTests.question

    def test_context_uses_the_reconciled_final_reply(self):
        decision = self.question()
        self.write('POST', f"/v1/decisions/{decision['id']}/reply", dict(text='Explain this.', revision=1))
        context = self.service._all('contexts')[-1]
        def answer(task, bot, history, question, emit):
            emit('text_delta', {'text': 'Draft explanation'})
            emit('text_delta', {'text': 'Verified explanation.', 'replace': True})
        with patch.object(self.service.provider, 'answer_context', answer):
            self.run_context(context)
        self.assertEqual(self.service._all('messages')[-1]['text'], 'Verified explanation.')

    def run_context(self, context):
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=5)
        self.assertFalse(self.service.context_threads[context['id']].is_alive())

    def test_prompt_keeps_results_separate_from_the_user_instruction(self):
        prompt, _ = turn_input(dict(operation='context', history=[], question='Ask Gumclaw.', task=dict(
            _decision_reply=True, _background_completion=['The workflow exists.'], pending_decision={})))
        self.assertIn('The workflow exists.', prompt)
        self.assertIn('not a new instruction or approval', prompt)
