import json
import unittest
from unittest.mock import patch

import test_decision_inbox as inbox_tests
from coconut.hermes_worker import conversation_input
from coconut.answer_undo import AnswerUndo
# These tests check what an answer does, so it applies at once. test_answer_undo.py tests the Undo window.
AnswerUndo.answer_undo_seconds = 0


class ContextCompletionTests(unittest.TestCase):
    setUp = inbox_tests.DecisionInboxTests.setUp
    tearDown = inbox_tests.DecisionInboxTests.tearDown
    write = inbox_tests.DecisionInboxTests.write
    event = inbox_tests.DecisionInboxTests.event
    question = inbox_tests.DecisionInboxTests.question
    restart = inbox_tests.DecisionInboxTests.restart

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

    def start_research(self):
        self.decision = self.question()
        self.write('POST', f"/v1/decisions/{self.decision['id']}/reply",
                   dict(text='Ask Gumclaw for advice.', revision=1))
        context = self.service._all('contexts')[-1]
        def answer(task, bot, history, question, emit):
            receipt = emit('native_call', dict(name='delegate_task', arguments={'goal': 'Read the existing reply'}))
            emit('native_result', dict(id=receipt['id'], result=json.dumps(
                dict(status='dispatched', delegation_id='deleg-advice'))))
            emit('completed', dict(summary='Gumclaw is checking.'))
        with patch.object(self.service.provider, 'answer_context', answer):
            self.run_context(context)
        return context

    def run_context(self, context):
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=5)
        self.assertFalse(self.service.context_threads[context['id']].is_alive())

    def drain(self):
        def completed(bot, processes):
            return [dict(id=p['id'], acknowledged=True) if p.get('acknowledge') else
                    dict(id=p['id'], text='Gumclaw found the local test workflow.') for p in processes]
        with patch.object(self.service.provider, 'process_completions', completed, create=True):
            self.service._drain_process_completions()

    def test_advice_resumes_card_once_across_restart_without_approving_work(self):
        original = self.start_research()
        before = len(self.service._all('messages'))
        self.drain()
        contexts = self.service._all('contexts')
        self.assertEqual(len(contexts), 2)
        continuation = contexts[-1]
        self.assertEqual(continuation['status'], 'queued')
        self.assertEqual(continuation['_message_id'], original['_message_id'])
        self.assertEqual(len(self.service._all('messages')), before)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        self.restart()
        self.drain()
        self.assertEqual(len(self.service._all('contexts')), 2)
        def answer(task, bot, history, question, emit):
            self.assertIn('local test workflow', task['_background_completion'][0])
            blocked = emit('decision_response', dict(choice='approve', option_id='check', quote=question))
            self.assertEqual(blocked['status'], 'pending')
            emit('decision_response', dict(choice='modify', quote=question, revision=dict(
                title='Run local tests?', context='Gumclaw supplied the workflow.', options=self.decision['options'])))
            emit('completed', dict(summary='The card includes Gumclaw’s result.'))
        with patch.object(self.service.provider, 'answer_context', answer):
            self.run_context(continuation)
        saved = self.service._get('decisions', self.decision['id'])
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(saved['context'], 'Gumclaw supplied the workflow.')
        self.assertEqual(saved['revision'], 2)
        self.drain()
        self.assertEqual(len(self.service._all('contexts')), 2)
        self.assertEqual(len(self.service._all('decisions')), 1)

    def test_cancelled_or_answered_card_does_not_resume_research(self):
        context = self.start_research()
        with self.service.transaction():
            saved = self.service._get('contexts', context['id'])
            saved['status'] = 'cancelled'
            self.service._put('contexts', saved)
        self.drain()
        self.assertEqual(len(self.service._all('contexts')), 1)
        with self.service.transaction():
            saved['status'] = 'completed'
            self.service._put('contexts', saved)
        self.write('POST', f"/v1/decisions/{self.decision['id']}/answer", dict(option_id='leave', revision=1))
        self.drain()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_cached_result_from_before_restart_reaches_the_current_card(self):
        self.start_research()
        with self.service.transaction():
            receipt = self.service._all('native_receipts')[-1]
            receipt['_completion_text'] = 'Already saved advice.'
            self.service._put('native_receipts', receipt)
            decision = self.service._get('decisions', self.decision['id'])
            decision['revision'] = 2
            self.service._put('decisions', decision)
        self.restart()
        self.drain()
        context = self.service._all('contexts')[-1]
        self.assertEqual(context['status'], 'queued')
        self.assertEqual(context['_decision_revision'], 2)
        self.assertEqual(context['_background_completion'], ['Already saved advice.'])

    def test_answer_between_delivery_and_dispatch_cancels_the_continuation(self):
        self.start_research()
        self.drain()
        context = self.service._all('contexts')[-1]
        self.write('POST', f"/v1/decisions/{self.decision['id']}/answer", dict(option_id='leave', revision=1))
        with patch.object(self.service.provider, 'answer_context') as answer:
            self.run_context(context)
        answer.assert_not_called()
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'cancelled')

    def test_queued_result_updates_the_latest_card_revision(self):
        self.start_research()
        self.drain()
        continuation = self.service._all('contexts')[-1]
        other = inbox_tests.DecisionInboxTests.reply(self, self.decision, 'Clarify this title.')
        self.service.resolve_decision_reply(other['id'], dict(choice='modify', quote=other['question'],
            revision=dict(title='Clarify references?', options=self.decision['options'])))
        def answer(task, bot, history, question, emit):
            self.assertEqual(task['pending_decision']['revision'], 2)
            emit('decision_response', dict(choice='modify', quote=question, revision=dict(
                title='Run local tests?', context='The requested research is ready.', options=self.decision['options'])))
            emit('completed', dict(summary='Updated.'))
        with patch.object(self.service.provider, 'answer_context', answer):
            self.run_context(continuation)
        decision = self.service._get('decisions', self.decision['id'])
        self.assertEqual(decision['revision'], 3)
        self.assertEqual(decision['context'], 'The requested research is ready.')

    def test_prompt_keeps_results_separate_from_the_user_instruction(self):
        prompt, _ = conversation_input(dict(operation='context', history=[], question='Ask Gumclaw.', task=dict(
            _decision_reply=True, _background_completion=['The workflow exists.'], pending_decision={})))
        self.assertIn('The workflow exists.', prompt)
        self.assertIn('not a new instruction or approval', prompt)
