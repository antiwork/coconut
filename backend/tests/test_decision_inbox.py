import base64
import unittest
from unittest.mock import Mock
from uuid import uuid4

import test_native_execution as native_tests
from coconut.service import ServiceError, YieldForDecision


class DecisionInboxTests(unittest.TestCase):
    setUp = native_tests.NativeExecutionTests.setUp
    tearDown = native_tests.NativeExecutionTests.tearDown
    write = native_tests.NativeExecutionTests.write
    event = native_tests.NativeExecutionTests.event
    restart = native_tests.NativeExecutionTests.restart
    approval = native_tests.NativeExecutionTests.approval
    request_context = native_tests.NativeExecutionTests.request_context

    def question(self, optional=False):
        payload = dict(key='one-proposal', title='Investigate references?', explanation='The hashes match.',
                       evidence='Compared hashes.', uncertainty='References are unknown.', risk='Deletion could break a caller.',
                       options=[{'id': 'check', 'label': 'Check references'}, {'id': 'leave', 'label': 'Leave it'}])
        if optional:
            self.event('native_proposal', payload)
        else:
            with self.assertRaises(YieldForDecision):
                self.event('native_question', payload)
        return self.service.snapshot()['decisions'][-1]

    def reply(self, decision, text):
        self.write('POST', f"/v1/decisions/{decision['id']}/reply", {'text': text, 'revision': decision['revision']})
        context = self.service._all('contexts')[-1]
        with self.service.transaction():
            context['status'] = 'working'
            self.service._put('contexts', context)
        return context

    def test_recommendation_is_optional_and_survives_revision(self):
        decision = self.question()
        self.assertIsNone(decision.get('recommended_option_id'))
        context = self.reply(decision, 'Compare the references first')
        self.service.resolve_decision_reply(context['id'], dict(choice='modify', quote=context['question'],
            revision=dict(title='Check references?', recommended_option_id='check', options=[
                dict(id='check', label='Check references', explanation='Find callers before changing files.'),
                dict(id='leave', label='Leave files', explanation='Keep all files unchanged.')])))
        self.restart()
        decision = self.service.snapshot()['decisions'][-1]
        self.assertEqual(decision['recommended_option_id'], 'check')
        context = self.reply(decision, 'Give me neutral choices')
        self.service.resolve_decision_reply(context['id'], dict(choice='modify', quote=context['question'],
            revision=dict(title='Which next step?', options=decision['options'])))
        self.assertIsNone(self.service.snapshot()['decisions'][-1].get('recommended_option_id'))

    def test_option_effects_survive_creation_revision_and_restart(self):
        options = [dict(id=str(index), label=f'Choice {index}', explanation=f'Effect {index}.') for index in range(4)]
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Which action?', options=options))
        decision = self.service.snapshot()['decisions'][-1]
        self.assertEqual([o['explanation'] for o in decision['options']], [o['explanation'] for o in options])
        context = self.reply(decision, 'Revise these choices')
        options[3]['explanation'] = 'Leave the release page unchanged.'
        self.service.resolve_decision_reply(context['id'], dict(choice='modify', quote=context['question'],
            revision=dict(title='Which revised action?', options=options)))
        self.restart()
        saved = self.service.snapshot()['decisions'][-1]
        self.assertEqual(saved['options'][3]['explanation'], options[3]['explanation'])
        self.assertEqual(saved['revision'], 2)

    def test_revised_option_requires_the_current_revision_even_when_id_is_reused(self):
        decision = self.question()
        context = self.reply(decision, 'Change the scope to all projects')
        self.service.resolve_decision_reply(context['id'], dict(choice='modify', quote=context['question'],
            revision=dict(title='Check all projects?', options=[dict(id='check', label='Check all projects',
                explanation='Read references in every project.')])))
        path = f"/v1/decisions/{decision['id']}/answer"
        for payload in ({'option_id': 'check'}, {'option_id': 'check', 'revision': 1}):
            with self.assertRaises(ServiceError):
                self.write('POST', path, payload)
            self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        result = self.write('POST', path, {'option_id': 'check', 'revision': 2})
        self.assertEqual(result['status'], 'approved')

    def test_invalid_option_effects_do_not_create_a_decision(self):
        for explanation in (42, 'x' * 501):
            with self.subTest(explanation_type=type(explanation).__name__):
                with self.assertRaises(ServiceError):
                    self.event('native_question', dict(title='Choose?',
                        options=[dict(id='one', label='One', explanation=explanation)]))
        self.assertEqual(self.service.snapshot()['decisions'], [])

    def file_proposal(self):
        with self.assertRaises(YieldForDecision):
            self.event('decision', dict(title='Save report?', action_type='create_file',
                action=dict(type='create_file', filename='report.md', content='Report.')))
        return self.service.snapshot()['decisions'][-1]

    def test_reply_can_reject_a_file_without_creating_it(self):
        decision = self.file_proposal()
        context = self.reply(decision, 'Do not create this file')
        result = self.service.resolve_decision_reply(context['id'], dict(choice='reject', quote=context['question']))
        self.assertEqual(result['decision']['status'], 'rejected')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')
        self.assertFalse(self.service.snapshot()['artifacts'])

    def test_reply_can_request_a_file_revision_without_approving_it(self):
        decision = self.file_proposal()
        context = self.reply(decision, 'Use notes.md instead')
        result = self.service.resolve_decision_reply(context['id'], dict(choice='modify', quote=context['question']))
        self.assertEqual(result['decision']['status'], 'modified')
        self.assertEqual(result['decision']['correction'], context['question'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')
        self.assertFalse(self.service.snapshot()['artifacts'])

    def test_file_approval_still_requires_a_visible_choice(self):
        decision = self.file_proposal()
        context = self.reply(decision, 'Save the file')
        result = self.service.resolve_decision_reply(context['id'], dict(choice='approve', quote=context['question']))
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.assertFalse(self.service.snapshot()['artifacts'])

    def test_optional_proposal_does_not_pause_task_and_deduplicates(self):
        first = self.question(optional=True)
        second = self.question(optional=True)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.service.snapshot()['decisions']), 1)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'working')
        self.assertEqual(first['evidence'], 'Compared hashes.')
        self.assertEqual(first['inbox_kind'], 'proposal')

    def test_required_later_survives_reopen_without_resolving(self):
        decision = self.question()
        result = self.write('POST', f"/v1/decisions/{decision['id']}/inbox", {'action': 'later', 'revision': 1})
        self.assertEqual(result['status'], 'pending')
        self.restart()
        self.assertEqual(self.service.snapshot()['decisions'][0]['inbox_deferred_at'], result['inbox_deferred_at'])

    def expire_swipe(self, decision):
        with self.service.transaction():
            current = self.service._get('decisions', decision['id'])
            current['inbox_undo_until'] = '2000-01-01T00:00:00Z'
            self.service._put('decisions', current)
        self.service.finish_inbox_swipes()

    def test_old_undo_cannot_reverse_a_newer_swipe(self):
        decision = self.question()
        path = f"/v1/decisions/{decision['id']}/inbox"
        kept = self.write('POST', path, dict(action='keep', revision=1))
        dismissed = self.write('POST', path, dict(action='dismiss', revision=1))
        self.assertNotEqual(kept['inbox_swipe_id'], dismissed['inbox_swipe_id'])
        with self.assertRaises(ServiceError):
            self.write('POST', path, dict(action='restore', revision=1, swipe_id=kept['inbox_swipe_id']))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'dismissed')
        restored = self.write('POST', path, dict(action='restore', revision=1, swipe_id=dismissed['inbox_swipe_id']))
        self.assertEqual(restored['status'], 'pending')

    def test_required_dismissal_can_undo_before_hermes_resumes(self):
        decision = self.question()
        path = f"/v1/decisions/{decision['id']}/inbox"
        dismissed = self.write('POST', path, dict(action='dismiss', revision=1))
        self.assertEqual(dismissed['status'], 'dismissed')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        self.service.finish_inbox_swipes()
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        restored = self.write('POST', path, dict(action='restore', revision=1, swipe_id=dismissed['inbox_swipe_id']))
        self.assertEqual(restored['status'], 'pending')
        self.assertEqual(restored['revision'], 2)
        self.assertNotIn('_inbox_reject_device', self.service._get('decisions', decision['id']))
        with self.assertRaises(ServiceError):
            self.write('POST', f"/v1/decisions/{decision['id']}/answer", dict(option_id='check', revision=1))

    def test_required_dismissal_rejects_after_window_and_restart(self):
        decision = self.question()
        self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='dismiss', revision=1))
        self.restart()
        self.expire_swipe(decision)
        current = self.service._get('decisions', decision['id'])
        self.assertEqual(current['status'], 'rejected')
        self.assertIsNone(current['selected_option_id'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')
        self.assertFalse(self.service.snapshot()['artifacts'])
        with self.assertRaises(ServiceError):
            self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='restore', revision=1))
        count = len(current['_answers'])
        self.service.finish_inbox_swipes()
        self.assertEqual(len(self.service._get('decisions', decision['id'])['_answers']), count)

    def test_file_dismissal_never_creates_the_file(self):
        decision = self.file_proposal()
        self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='dismiss', revision=1))
        self.expire_swipe(decision)
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'rejected')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')
        self.assertFalse(self.service.snapshot()['artifacts'])

    def test_plain_chat_keeps_pending_decision_and_bot_can_resolve_it(self):
        decision = self.question()
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", dict(text='Leave it', mode='auto'))
        self.assertNotEqual(task['id'], decision['task_id'])
        message = self.service.snapshot()['messages'][-1]
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored.update(status='working', _turn_message_ids=[message['id']])
            self.service._put('tasks', stored)
        result = self.service.resolve_chat_decision(task['id'], dict(choice='approve', option_id='leave',
            quote='Leave it', decision_id=decision['id'], decision_revision=1, source_message_id=message['id']))
        self.assertEqual(result['decision']['selected_option_id'], 'leave')

    def test_chat_owner_can_resolve_another_bots_decision_in_its_chat(self):
        owner = self.write('POST', '/v1/bots', {'name': 'Desk', 'role': ''})
        with self.service.transaction():
            target = self.service._get('tasks', self.task['id'])
            target['conversation_bot_id'] = owner['id']
            self.service._put('tasks', target)
        decision = self.question()
        foreign = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", dict(text='Leave it', mode='auto'))
        foreign_message = self.service.snapshot()['messages'][-1]
        with self.service.transaction():
            outside = self.service._get('tasks', foreign['id'])
            outside.update(status='working', _turn_message_ids=[foreign_message['id']])
            self.service._put('tasks', outside)
        self.assertFalse(self.service.decision_chat_input(outside)['_pending_decisions'])
        with self.assertRaises(ServiceError):
            self.service.resolve_chat_decision(outside['id'], dict(choice='approve', option_id='leave',
                quote='Leave it', decision_id=decision['id'], decision_revision=1, source_message_id=foreign_message['id']))
        task = self.write('POST', f"/v1/bots/{owner['id']}/messages", dict(text='Leave it', mode='auto'))
        message = self.service.snapshot()['messages'][-1]
        with self.service.transaction():
            source = self.service._get('tasks', task['id'])
            source.update(status='working', _turn_message_ids=[message['id']])
            self.service._put('tasks', source)
        supplied = self.service.decision_chat_input(source)
        self.assertEqual([d['id'] for d in supplied['_pending_decisions']], [decision['id']])
        result = self.service.resolve_chat_decision(task['id'], dict(choice='approve', option_id='leave',
            quote='Leave it', decision_id=decision['id'], decision_revision=1, source_message_id=message['id']))
        self.assertEqual(result['decision']['selected_option_id'], 'leave')
        self.assertEqual(result['decision']['bot_id'], self.bot['id'])

    def test_normal_turn_supplies_verified_human_sources_to_hermes(self):
        decision = self.question()
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", dict(text='Leave it', mode='auto'))
        message = self.service.snapshot()['messages'][-1]
        self.service.provider.start_task = Mock(wraps=self.service.provider.start_task)
        self.service._run_task(task['id'])
        sent = self.service.provider.start_task.call_args.args[0]
        self.assertEqual(sent['_decision_reply_sources'], [dict(id=message['id'], text='Leave it')])
        self.assertEqual([d['id'] for d in sent['_pending_decisions']], [decision['id']])
        self.assertNotIn('_human_device_id', str(sent['_decision_reply_sources']))

    def test_expired_optional_undo_cannot_reopen_the_proposal(self):
        decision = self.question(optional=True)
        self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='dismiss', revision=1))
        self.expire_swipe(decision)
        self.restart()
        with self.assertRaises(ServiceError):
            self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='restore', revision=1))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'dismissed')

    def test_chat_resolution_rejects_stale_revision_and_foreign_source(self):
        decision = self.question()
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", dict(text='Leave it', mode='auto'))
        message = self.service.snapshot()['messages'][-1]
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored.update(status='working', _turn_message_ids=[message['id']])
            self.service._put('tasks', stored)
        payload = dict(choice='approve', option_id='leave', quote='Leave it', decision_id=decision['id'],
                       decision_revision=1, source_message_id=message['id'])
        for change in (dict(decision_revision=0), dict(source_message_id='foreign-message'), dict(quote='Delete everything')):
            with self.subTest(change=change), self.assertRaises(ServiceError):
                self.service.resolve_chat_decision(task['id'], dict(payload, **change))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.assertFalse(self.service._all('contexts'))

    def test_native_approval_dismissal_records_only_rejection(self):
        decision = self.approval()
        self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='dismiss', revision=1))
        self.expire_swipe(decision)
        current = self.service._get('decisions', decision['id'])
        self.assertEqual(current['status'], 'rejected')
        self.assertIsNone(current['selected_option_id'])
        self.assertFalse(self.service.snapshot()['artifacts'])

    def test_proposal_dismissal_and_undo_share_the_same_record(self):
        decision = self.question(optional=True)
        result = decision
        for action, expected in [('dismiss', 'dismissed'), ('restore', 'pending')]:
            result = self.write('POST', f"/v1/decisions/{decision['id']}/inbox", {'action': action, 'revision': 1, 'swipe_id': result.get('inbox_swipe_id')})
            self.assertEqual(result['status'], expected)
            self.assertEqual(result['id'], decision['id'])
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)

    def test_optional_answer_creates_continuation_without_restarting_original(self):
        decision = self.question(optional=True)
        result = self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'check', 'revision': 1})
        self.assertEqual(result['status'], 'approved')
        tasks = self.service.snapshot()['tasks']
        self.assertEqual(len(tasks), 2)
        self.assertEqual(next(t for t in tasks if t['id'] == self.task['id'])['status'], 'working')
        continuation = next(t for t in tasks if t['id'] != self.task['id'])
        self.assertEqual(continuation['parent_task_id'], self.task['id'])
        self.assertEqual(continuation['thread_id'], self.task['thread_id'])

    def test_question_reply_stays_pending_and_appears_in_original_chat(self):
        decision = self.question()
        context = self.reply(decision, 'Why is that needed?')
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        message = self.service.snapshot()['messages'][-1]
        self.assertEqual(message['decision_id'], decision['id'])
        self.assertEqual(message['bot_id'], self.bot['id'])
        self.assertEqual(message['text'], context['question'])

    def test_explicit_reply_resolves_once_and_queues_native_continuation(self):
        decision = self.question()
        context = self.reply(decision, 'Check references')
        response = dict(choice='approve', option_id='check', quote='Check references')
        result = self.service.resolve_decision_reply(context['id'], response)
        self.assertEqual(result['decision']['status'], 'approved')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')
        with self.assertRaises(ServiceError):
            self.service.resolve_decision_reply(context['id'], response)

    def test_scope_change_revises_one_card_and_invalidates_old_choices(self):
        decision = self.question()
        context = self.reply(decision, 'Check only the export folder')
        result = self.service.resolve_decision_reply(context['id'], dict(choice='modify', quote=context['question'],
            revision=dict(title='Check the export folder?', options=[dict(id='exports', label='Check exports')],
                          explanation='Limit the check to this folder.')))
        self.assertEqual(result['decision']['id'], decision['id'])
        self.assertEqual(result['decision']['status'], 'pending')
        self.assertEqual(result['decision']['revision'], 2)
        self.assertEqual(len(self.service.snapshot()['decisions']), 1)
        with self.assertRaises(ServiceError):
            self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'check', 'revision': 1})

    def test_context_cannot_invent_authorization_or_approve_native_action(self):
        decision = self.approval()
        context = self.reply(decision, 'Go ahead')
        with self.assertRaises(ServiceError):
            self.service.resolve_decision_reply(context['id'], dict(choice='approve', option_id='once', quote='I approve'))
        result = self.service.resolve_decision_reply(context['id'], dict(choice='approve', option_id='once', quote='Go ahead'))
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_resolution_after_other_device_answers_is_rejected(self):
        decision = self.question()
        context = self.reply(decision, 'Leave it')
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'choice': 'reject', 'revision': 1})
        with self.assertRaises(ServiceError):
            self.service.resolve_decision_reply(context['id'], dict(choice='approve', option_id='leave', quote='Leave it'))

    def test_context_only_request_cannot_be_used_as_a_reply(self):
        decision = self.question()
        context = self.request_context(decision)
        with self.assertRaises(ServiceError):
            self.service.resolve_decision_reply(context['id'], dict(choice='reject', quote='Check the file again.'))


    def test_long_proposal_answer_reaches_the_native_continuation(self):
        decision = self.question(optional=True)
        with self.service.transaction():
            stored = self.service._get('decisions', decision['id'])
            stored['title'] = 'Investigate the duplicate export and references in the production report folder before changing any file'
            self.service._put('decisions', stored)
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'check', 'revision': 1})
        continuation = next(t for t in self.service.snapshot()['tasks'] if t['id'] != self.task['id'])
        self.service.provider.resume_task = Mock()
        self.service._run_task(continuation['id'])
        self.service.provider.resume_task.assert_called_once()
        delivered = self.service.provider.resume_task.call_args.args[3]
        self.assertEqual(delivered['selected_option_label'], 'Check references')
        self.assertEqual(delivered['title'], stored['title'])
        self.assertEqual(delivered['evidence'], 'Compared hashes.')

    def test_failed_context_recovery_allows_another_reply(self):
        decision = self.question()
        context = self.reply(decision, 'What did you check?')
        self.service.native_event(self.task['id'], 'native_call',
            {'name': 'read_file', 'arguments': {'path': 'report'}}, context_id=context['id'])
        self.assertTrue(self.service.snapshot()['decisions'][0]['reply_pending'])
        self.restart()
        self.assertFalse(self.service.snapshot()['decisions'][0]['reply_pending'])
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'pending')
        self.reply(decision, 'Can you explain the uncertainty?')

    def test_source_context_comes_from_original_message(self):
        decision = self.question()
        request = next(m for m in self.service.snapshot()['messages'] if m['role'] == 'user')
        self.assertEqual(decision['request_text'], request['text'])
        self.assertEqual(decision['request_message_id'], request['id'])
        self.assertEqual(decision['request_bot_id'], self.bot['id'])
        self.assertEqual(decision['request_created_at'], request['created_at'])

    def test_optional_proposal_does_not_block_steering_current_work(self):
        self.question(optional=True)
        reply = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Also check the summary.', 'mode': 'auto'})
        self.assertEqual(reply['id'], self.task['id'])
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)

    def test_proposal_outcome_follows_continuation_before_original_finishes(self):
        decision = self.question(optional=True)
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'check', 'revision': 1})
        continuation = next(t for t in self.service.snapshot()['tasks'] if t['id'] != self.task['id'])
        self.event('completed', {'summary': 'Original work completed.'})
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'approved')
        self.service.provider.resume_task = Mock()
        self.service._run_task(continuation['id'])
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'executed')

    def test_proposal_outcome_follows_continuation_after_original_finishes(self):
        decision = self.question(optional=True)
        self.event('completed', {'summary': 'Original work completed.'})
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'check', 'revision': 1})
        continuation = next(t for t in self.service.snapshot()['tasks'] if t['id'] != self.task['id'])
        self.service.provider.resume_task = Mock(side_effect=lambda task, bot, history, decision, emit: emit('failed', {'summary': 'The check failed.'}))
        self.service._run_task(continuation['id'])
        result = self.service._get('decisions', decision['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['outcome'], 'The check failed.')

    def test_shared_request_is_used_before_an_older_parent_request(self):
        other = self.write('POST', '/v1/bots', {'name': 'Second', 'role': ''})
        with self.service.transaction():
            dispatched = self.service._send(self.bot, {'text': 'Check this new shared request.', 'recipient_bot_ids': [self.bot['id'], other['id']]}, parent_task_id=self.task['id'])
            task = self.service._get('tasks', dispatched['dispatched_task_ids'][1])
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.task = task
        decision = self.question()
        self.assertEqual(decision['request_text'], 'Check this new shared request.')
        self.assertEqual(decision['request_message_id'], task['_shared_message_ids'][0])
        self.assertEqual(decision['request_bot_id'], self.bot['id'])

    def test_cancelling_proposal_continuation_records_the_outcome(self):
        decision = self.question(optional=True)
        self.event('completed', {'summary': 'Original work completed.'})
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'check', 'revision': 1})
        continuation = next(t for t in self.service.snapshot()['tasks'] if t['id'] != self.task['id'])
        self.write('POST', f"/v1/tasks/{continuation['id']}/cancel", {})
        result = self.service._get('decisions', decision['id'])
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['outcome'], 'Task cancelled.')

    def test_delegation_does_not_copy_private_source_messages_to_another_bot(self):
        other = self.write('POST', '/v1/bots', {'name': 'Delegate', 'role': ''})
        with self.service.transaction():
            source = self.service._get('tasks', self.task['id'])
            delegated = self.service._send(other, {'text': 'Check only the public report.'}, source_task=source,
                                           parent_task_id=source['id'], record_message=False)
            task = self.service._get('tasks', delegated['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.task = task
        decision = self.question()
        self.assertNotIn('request_text', decision)
        self.assertNotIn('request_message_id', decision)

    def test_proposal_continuation_receives_the_original_attachment_content(self):
        content = base64.b64encode(b'The report references export-final.csv.').decode()
        artifact = self.write('POST', '/v1/attachments', {'bot_id': self.bot['id'], 'filename': 'notes.txt', 'content_base64': content})
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['_attachment_ids'] = [artifact['id']]
            self.service._put('tasks', task)
        decision = self.question(optional=True)
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'check', 'revision': 1})
        continuation = next(t for t in self.service.snapshot()['tasks'] if t['id'] != self.task['id'])
        self.service.provider.resume_task = Mock()
        self.service._run_task(continuation['id'])
        delivered = self.service.provider.resume_task.call_args.args[0]
        self.assertEqual(delivered['attachments'][0]['content_base64'], content)
