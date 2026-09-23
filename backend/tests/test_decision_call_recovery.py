import json
import os
import unittest
from unittest.mock import patch

import test_message_drafts as drafts
import test_provider as providers
from coconut.service import ServiceError, YieldForDecision


class DecisionCallRecoveryTests(unittest.TestCase):
    setUp = drafts.MessageDraftTests.setUp
    tearDown = drafts.MessageDraftTests.tearDown
    restart = drafts.MessageDraftTests.restart
    write = drafts.MessageDraftTests.write
    event = drafts.MessageDraftTests.event
    draft = drafts.MessageDraftTests.draft
    answer = drafts.MessageDraftTests.answer
    fake_hermes = providers.ProviderTests.fake_hermes

    def question(self, previous):
        return dict(title='Proceed to the next step?', context='The earlier step has its own approval.',
                    previous_decision_id=previous, options=[dict(id='continue', label='Continue')])

    def test_approved_draft_links_as_context_without_approving_the_next_step(self):
        draft = self.draft()
        approved = self.answer(draft)
        with self.assertRaises(YieldForDecision):
            self.event('native_question', self.question(draft['id']))
        later = self.service.snapshot()['decisions'][-1]
        self.assertEqual(later['previous_decision_id'], draft['id'])
        self.assertEqual(later['status'], 'pending')
        self.assertIsNone(later.get('selected_option_id'))
        self.assertEqual(self.service._get('decisions', draft['id'])['draft_hash'], approved['draft_hash'])
        self.assertEqual(self.service._get('decisions', draft['id'])['status'], 'approved')

    def test_draft_that_carries_out_an_answer_links_to_that_answer(self):
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Ask the seller for more proof?',
                options=[dict(id='more_proof', label='Request more proof'), dict(id='decline', label='Decline the claim')]))
        ruling = self.service.snapshot()['decisions'][-1]
        self.write('POST', f"/v1/decisions/{ruling['id']}/answer", {'option_id': 'more_proof', 'revision': 1})
        # A later source audit task carries out the answer from its listed earlier answers.
        later = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Reconcile the issue', 'mode': 'queue'})
        with self.service.transaction():
            task = self.service._get('tasks', later['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        body = dict(channel='Gmail', sender='support@example.com', recipient='seller@example.com',
                    subject='Account recovery', body='Please send two more details.')
        self.service.emit(later['id'], 'native_proposal', dict(key='proof-request', title='Send the proof request to the seller?',
                                                              draft=body, previous_decision_id=ruling['id']))
        draft = self.service.snapshot()['decisions'][-1]
        self.assertEqual(draft['action_type'], 'draft_message')
        self.assertEqual(draft['previous_decision_id'], ruling['id'])
        self.assertEqual(draft['status'], 'pending')

    def test_unrelated_draft_does_not_inherit_resumed_question(self):
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Check ownership?',
                options=[dict(id='check', label='Check ownership')]))
        ruling = self.service.snapshot()['decisions'][-1]
        self.write('POST', f"/v1/decisions/{ruling['id']}/answer", {'option_id': 'check', 'revision': 1})
        self.assertEqual(self.service._get('tasks', self.task['id'])['_resume_decision'], ruling['id'])
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        draft = self.draft()
        self.assertIsNone(draft.get('previous_decision_id'))
        self.assertEqual(draft['status'], 'pending')
        body = draft['draft']
        self.event('native_proposal', dict(key='related', title='Send the proof request?',
            draft=body, previous_decision_id=ruling['id']))
        related = self.service.snapshot()['decisions'][-1]
        self.assertEqual(related['previous_decision_id'], ruling['id'])
        self.assertEqual(related['status'], 'pending')

    def test_pending_draft_still_cannot_be_used_as_an_answer(self):
        draft = self.draft()
        with self.assertRaises(ServiceError):
            self.event('native_question', self.question(draft['id']))
        self.assertEqual(len(self.service._all('decisions')), 1)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'working')

    def test_invalid_link_is_corrected_in_the_same_worker_turn(self):
        provider = self.fake_hermes('''
            args = {'title': 'Continue?', 'context': 'One new choice.',
                    'previous_decision_id': 'missing', 'options': [{'id': 'yes', 'label': 'Yes'}]}
            result = json.loads(registry.dispatch('coconut_ask', args))
            assert result['status'] == 'validation_failed', result
            assert 'error' in result
            del args['previous_decision_id']
            registry.dispatch('coconut_ask', args)
            raise AssertionError('Accepted decisions must stop the tool batch')
        ''')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            with self.assertRaises(YieldForDecision):
                provider.start_task(self.task, self.bot, [], self.event)
        decisions = self.service._all('decisions')
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]['status'], 'pending')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        self.assertFalse(any('Work stopped:' in m['text'] for m in self.service._all('messages')))

    def test_restart_collapses_old_link_failures_and_keeps_other_messages(self):
        text = 'Work stopped: Link an answered question from this conversation.'
        with self.service.transaction():
            self.service.db.execute("DELETE FROM settings WHERE key='decision_link_notices_migrated'")
            for _ in range(3):
                self.service._message(self.bot['id'], text, task_id=self.task['id'], kind='status')
            self.service._message(self.bot['id'], text, task_id=self.task['id'], role='user')
            self.service._message(self.bot['id'], 'A different failure.', task_id=self.task['id'], kind='status')
        self.restart()
        messages = self.service.snapshot()['messages']
        self.assertEqual(sum(m['kind'] == 'status' and m['text'] == text for m in messages), 1)
        self.assertTrue(any(m['role'] == 'user' and m['text'] == text for m in messages))
        self.assertTrue(any(m['text'] == 'A different failure.' for m in messages))
        self.assertEqual(sum(m['text'] == text for m in self.service._all('messages')), 4)
        self.restart()
        self.assertEqual(self.service.snapshot()['messages'], messages)

    def test_invalid_suggestion_returns_rejection_without_creating_a_card(self):
        provider = self.fake_hermes('''
            result = json.loads(registry.dispatch('coconut_suggest', {
                'key': 'next', 'title': 'Continue?', 'context': 'A new choice.',
                'previous_decision_id': 'missing', 'options': [{'id': 'yes', 'label': 'Yes'}]}))
            assert result['status'] == 'rejected', result
            return {'completed': True, 'final_response': 'I can correct that link.'}
        ''')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], self.event)
        self.assertEqual(self.service._all('decisions'), [])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
