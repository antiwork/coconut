import unittest

import test_message_drafts as drafts
from coconut.service import ServiceError, YieldForDecision


class DecisionCallRecoveryTests(unittest.TestCase):
    setUp = drafts.MessageDraftTests.setUp
    tearDown = drafts.MessageDraftTests.tearDown
    restart = drafts.MessageDraftTests.restart
    write = drafts.MessageDraftTests.write
    event = drafts.MessageDraftTests.event
    draft = drafts.MessageDraftTests.draft
    answer = drafts.MessageDraftTests.answer

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

    def test_draft_schema_requires_explicit_followup_link(self):
        from coconut.card_tools import SUGGEST_SCHEMA
        description = SUGGEST_SCHEMA['parameters']['properties']['previous_decision_id']['description']
        self.assertIn('For a draft, always supply previous_decision_id', description)
        self.assertIn('unrelated drafts must omit it', description)

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


if __name__ == '__main__':
    unittest.main()
