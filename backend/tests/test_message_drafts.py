import json
import unittest
from uuid import uuid4

import test_native_execution as native_tests
from coconut.service import ServiceError
from coconut.hermes_worker import conversation_input


class MessageDraftTests(unittest.TestCase):
    setUp = native_tests.NativeExecutionTests.setUp
    tearDown = native_tests.NativeExecutionTests.tearDown
    write = native_tests.NativeExecutionTests.write
    event = native_tests.NativeExecutionTests.event
    restart = native_tests.NativeExecutionTests.restart

    def draft(self, **fields):
        body = dict(channel='Gmail', sender='me@example.com', recipient='sarah@example.com',
                    subject='Friday review', body='Hi Sarah,\n\nCan we meet at 2 PM?\n\nThanks!')
        body.update(fields)
        self.event('native_proposal', dict(key='friday-email', title='Move Friday’s review?', draft=body))
        return self.service.snapshot()['decisions'][-1]

    def answer(self, decision, option='send', **fields):
        return self.write('POST', '/v1/decisions/' + decision['id'] + '/answer',
            dict(option_id=option, revision=decision['revision'], draft_hash=decision['draft_hash'], **fields))

    def test_draft_is_shared_persistent_and_never_sends_on_creation(self):
        draft = self.draft()
        self.assertEqual(draft['action_type'], 'draft_message')
        self.assertEqual(draft['options'][1]['answer'], 'reject')
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)
        self.assertEqual(self.service.snapshot()['tasks'][0]['status'], 'working')
        self.assertEqual([m['decision_id'] for m in self.service.snapshot()['messages'] if m['kind'] == 'decision'], [draft['id']])
        self.assertEqual(self.draft()['id'], draft['id'])
        self.restart()
        self.assertEqual(self.service.snapshot()['decisions'][0]['draft'], draft['draft'])
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'pending')

    def test_edit_stays_pending_and_old_approval_is_rejected(self):
        old = self.draft()
        content = dict(old['draft'], body='Hi Sarah,\nCan we meet at 3 PM?', recipient='sarah.new@example.com')
        updated = self.write('POST', '/v1/decisions/' + old['id'] + '/draft', dict(revision=1, draft=content))
        self.assertEqual(updated['status'], 'pending')
        self.assertEqual(updated['revision'], 2)
        self.assertNotEqual(updated['draft_hash'], old['draft_hash'])
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)
        with self.assertRaises(ServiceError): self.answer(old)
        with self.assertRaises(ServiceError):
            self.write('POST', '/v1/decisions/' + old['id'] + '/draft', dict(revision=1, draft=old['draft']))
        approved = self.answer(updated)
        self.assertEqual(approved['draft'], content)
        continuation = self.service._get('tasks', self.service._get('decisions', old['id'])['_execution_task_id'])
        self.assertEqual(continuation['_resume_decision'], old['id'])
        self.assertEqual(self.service._get('decisions', old['id'])['_answers'][-1]['draft'], content)
        with self.assertRaises(ServiceError):
            self.write('POST', '/v1/decisions/' + old['id'] + '/draft', dict(revision=2, draft=old['draft']))

    def test_repeated_send_is_one_continuation_and_not_a_send_receipt(self):
        draft = self.draft()
        path = '/v1/decisions/' + draft['id'] + '/answer'
        payload = dict(option_id='send', revision=1, draft_hash=draft['draft_hash'])
        key = str(uuid4())
        first = self.service.mutate('POST', path, payload, key)
        self.assertEqual(self.service.mutate('POST', path, payload, key), first)
        self.assertEqual(first['status'], 'approved')
        self.assertEqual(first['outcome'], 'Approved for sending.')
        self.assertEqual(len(self.service.snapshot()['tasks']), 2)
        with self.assertRaises(ServiceError): self.answer(draft)

    def test_old_client_cannot_approve_a_draft_without_rendering_it(self):
        draft = self.draft()
        for extra in ({}, {'draft_hash': 'stale'}):
            with self.assertRaises(ServiceError):
                self.write('POST', '/v1/decisions/' + draft['id'] + '/answer', dict(option_id='send', revision=1, **extra))
        self.assertEqual(self.service._get('decisions', draft['id'])['status'], 'pending')

    def test_discard_and_swipe_never_resume_a_send(self):
        draft = self.draft()
        self.assertEqual(self.answer(draft, 'discard')['status'], 'rejected')
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)

    def test_invalid_or_hidden_draft_fields_cannot_create_cards(self):
        for fields in ({'bcc': 'hidden@example.com'}, {'body': ''}, {'recipient': 'x\nBcc: hidden@example.com'}, {'body': 'x' * 20001}):
            with self.subTest(fields=list(fields)):
                with self.assertRaises(ServiceError): self.draft(**fields)
        self.assertEqual(self.service.snapshot()['decisions'], [])

    def test_chat_approval_requires_visible_send_and_revision_edits_stay_pending(self):
        draft = self.draft()
        self.write('POST', '/v1/decisions/' + draft['id'] + '/reply', dict(text='Send it', revision=1))
        context = self.service._all('contexts')[-1]
        context['status'] = 'working'
        self.service._put('contexts', context)
        result = self.service.resolve_decision_reply(context['id'], dict(choice='approve', quote='Send it', option_id='send'))
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)
        result = self.service.resolve_decision_reply(context['id'], dict(choice='modify', quote='Send it',
            revision=dict(draft=dict(draft['draft'], body='Revised text.'))))
        self.assertEqual(result['decision']['status'], 'pending')
        self.assertEqual(result['decision']['draft']['body'], 'Revised text.')

    def test_resume_receives_exact_draft_after_context_compaction(self):
        draft = self.draft()
        approved = self.answer(draft)
        request = dict(operation='resume', decision=approved, bot=self.bot, task=self.task, history=[])
        prompt, history = conversation_input(request)
        self.assertIn('Send its body, subject, and destination exactly as recorded', prompt)
        self.assertIn(json.dumps(approved['draft']['body']), prompt)
        self.assertIn('sarah@example.com', prompt)
