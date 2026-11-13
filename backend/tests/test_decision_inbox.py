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
                       uncertainty='References are unknown.', risk='Deletion could break a caller.', risk_level='medium',
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

    def test_risk_levels_require_reasons_and_reject_numeric_confidence(self):
        for fields in (dict(risk_level='low'), dict(risk_level='medium', risk='  '),
                       dict(risk_level='safe', risk='Looks fine'), dict(risk_level=99, risk='Tests pass'),
                       dict(risk_level='high', risk=42), dict(risk='A reason without its level')):
            for on_option in (False, True):
                with self.subTest(fields=fields, on_option=on_option), self.assertRaises(ServiceError):
                    option = dict(id='one', label='Proceed', **(fields if on_option else {}))
                    self.event('native_question', dict(title='Proceed?', options=[option],
                                                      **({} if on_option else fields)))
        self.assertEqual(self.service.snapshot()['decisions'], [])

    def test_assessments_survive_creation_revision_and_restart_without_answering(self):
        options = [dict(id='publish', label='Publish', risk_level='medium', risk='Customers can read an incorrect date.'),
                   dict(id='wait', label='Wait', risk_level='low', risk='Only the release note is delayed.')]
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Publish?', options=options,
                risk_level='not_assessed', risk='The release date needs confirmation.'))
        self.restart()
        decision = self.service.snapshot()['decisions'][-1]
        self.assertEqual(decision['risk_level'], 'not_assessed')
        self.assertEqual([o['risk_level'] for o in decision['options']], ['medium', 'low'])
        task = self.service._get('tasks', decision['task_id'])
        self.service._revise_question(decision, task, dict(title='Publish the checked notes?', options=options,
            risk_level='low', risk='Only the release note changes.'), 'Checked the date.')
        self.restart()
        revised = self.service.snapshot()['decisions'][-1]
        self.assertEqual(revised['status'], 'pending')
        self.assertEqual(revised['risk_level'], 'low')
        self.assertEqual(revised['revision'], 2)
        self.service._revise_question(revised, task, dict(title='Which next step?',
            options=[dict(id='wait', label='Wait')]), 'Remove the old assessment.')
        saved = self.service.snapshot()['decisions'][-1]
        self.assertIsNone(saved['risk_level'])
        self.assertEqual(saved['risk'], '')
        self.assertFalse(saved['options'][0].get('risk'))

    def test_cards_do_not_store_supporting_evidence(self):
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Publish?', evidence='Compared the draft with the release.',
                options=[dict(id='publish', label='Publish')]))
        decision = self.service.snapshot()['decisions'][-1]
        self.assertNotIn('evidence', decision)
        stored = self.service._get('decisions', decision['id'])
        stored['evidence'] = 'An older card kept its checks here.'
        self.service._put('decisions', stored)
        task = self.service._get('tasks', decision['task_id'])
        self.service._revise_question(stored, task, dict(title='Publish the notes?',
            options=[dict(id='publish', label='Publish')]), 'Checked the notes.')
        self.assertNotIn('evidence', self.service._get('decisions', decision['id']))

    def test_card_text_over_the_limits_is_rejected_with_a_shortening_hint(self):
        from coconut.decision_inbox import CARD_TEXT_LIMITS, OPTION_EXPLANATION_LIMIT, RISK_LIMIT
        option = dict(id='one', label='Publish')
        for fields, hint in ((dict(context='x' * (CARD_TEXT_LIMITS['context'] + 1)), 'Shorten the context'),
                             (dict(uncertainty='x' * (CARD_TEXT_LIMITS['uncertainty'] + 1)), 'Shorten the uncertainty'),
                             (dict(risk_level='low', risk='x' * (RISK_LIMIT + 1)), 'State the risk in one sentence'),
                             (dict(options=[dict(option, explanation='x' * (OPTION_EXPLANATION_LIMIT + 1))]), 'one short sentence')):
            with self.subTest(fields=list(fields)), self.assertRaisesRegex(ServiceError, hint):
                self.event('native_question', dict(dict(title='Publish?', options=[option]), **fields))
        self.assertEqual(self.service.snapshot()['decisions'], [])
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Publish?', options=[option], context='x' * CARD_TEXT_LIMITS['context'],
                                               uncertainty='x' * CARD_TEXT_LIMITS['uncertainty']))
        decision = self.service.snapshot()['decisions'][-1]
        task = self.service._get('tasks', decision['task_id'])
        with self.assertRaisesRegex(ServiceError, 'Shorten the context'):
            self.service._revise_question(decision, task, dict(title='Publish now?', options=[option],
                context='x' * (CARD_TEXT_LIMITS['context'] + 1)), 'Add detail.')
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['title'], saved['revision']), ('Publish?', 1))

    def test_card_rules_match_titles_to_scope_and_offer_the_next_step(self):
        from coconut.decision_inbox import DECISION_INSTRUCTIONS
        self.assertIn('The title names exactly what approval authorizes', DECISION_INSTRUCTIONS)
        self.assertIn('offer the next step in a card', DECISION_INSTRUCTIONS)

    def test_all_card_tools_expose_the_same_optional_risk_contract(self):
        from coconut.card_tools import QUESTION_SCHEMA, SUGGEST_SCHEMA, REPLY_SCHEMA
        common = QUESTION_SCHEMA['parameters']['properties']
        for properties in (common, SUGGEST_SCHEMA['parameters']['properties'],
                           REPLY_SCHEMA['parameters']['properties']['revision']['properties']):
            self.assertEqual(properties['risk_level']['enum'], ['low', 'medium', 'high', 'not_assessed'])
            option = properties['options']['items']['properties']
            self.assertEqual(option['risk_level']['enum'], common['risk_level']['enum'])
            self.assertEqual(option['risk']['maxLength'], common['risk']['maxLength'])
        self.assertNotIn('risk_level', QUESTION_SCHEMA['parameters']['required'])

    def test_card_tools_give_each_card_rule_once(self):
        # coconut_ask carries the rules of the card fields. The other card tools refer to them, so each model call
        # sends each rule once (the gateway offers every Coconut tool in each call).
        import json
        from coconut.card_tools import CHAT_REPLY_SCHEMA, QUESTION_SCHEMA, SUGGEST_SCHEMA, refresh_schema
        rule = QUESTION_SCHEMA['parameters']['properties']['context']['description']
        risk = QUESTION_SCHEMA['parameters']['properties']['risk_level']['description']
        self.assertEqual(json.dumps(QUESTION_SCHEMA).count(json.dumps(risk)), 1)
        for schema in (SUGGEST_SCHEMA, CHAT_REPLY_SCHEMA, refresh_schema()):
            text = json.dumps(schema)
            self.assertNotIn(json.dumps(rule), text, schema['name'])
            self.assertNotIn(json.dumps(risk), text, schema['name'])
            self.assertIn('coconut_ask', text, schema['name'])
        self.assertLess(sum(len(json.dumps(schema)) for schema in (
            QUESTION_SCHEMA, SUGGEST_SCHEMA, CHAT_REPLY_SCHEMA, refresh_schema())), 17000)

    def test_card_tools_expose_needs_attention_now_apart_from_risk(self):
        from coconut.card_tools import QUESTION_SCHEMA, SUGGEST_SCHEMA, REPLY_SCHEMA
        self.assertIn('next work hours', QUESTION_SCHEMA['parameters']['properties']['needs_attention_now']['description'])
        for schema in (QUESTION_SCHEMA, SUGGEST_SCHEMA):
            properties = schema['parameters']['properties']
            self.assertEqual(properties['needs_attention_now']['type'], 'boolean')
            self.assertNotIn('notification_importance', properties)
            self.assertNotIn('needs_attention_now', schema['parameters']['required'])
            self.assertNotIn('needs_attention_now', properties['options']['items']['properties'])
        self.assertNotIn('needs_attention_now', REPLY_SCHEMA['parameters']['properties']['revision']['properties'])

    def test_native_cards_store_needs_attention_now_and_default_to_false(self):
        self.assertIs(self.question(optional=True)['needs_attention_now'], False)
        for value in (True, False):
            self.event('native_proposal', dict(key=f'k-{value}', title=f'{value}?', needs_attention_now=value,
                                               options=[dict(id='a', label='Do it')]))
            self.assertIs(self.service.snapshot()['decisions'][-1]['needs_attention_now'], value)
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Restart now?', needs_attention_now=True,
                                               options=[dict(id='a', label='Restart')]))
        self.assertIs(self.service.snapshot()['decisions'][-1]['needs_attention_now'], True)

    def test_native_cards_reject_a_needs_attention_now_that_is_not_a_boolean(self):
        before = len(self.service.snapshot()['decisions'])
        for value in ('true', 'urgent', 1, 0, None, ['true'], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ServiceError, 'needs_attention_now'):
                self.event('native_question', dict(title='Publish?', needs_attention_now=value, options=[dict(id='a', label='Do it')]))
        self.assertEqual(len(self.service.snapshot()['decisions']), before)

    def test_needs_attention_now_reaches_the_push_queue(self):
        self.service.push.record = Mock()
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Server is down. Restart it?', needs_attention_now=True,
                                               options=[dict(id='a', label='Restart')]))
        created = [c.args[1] for c in self.service.push.record.call_args_list if c.args[0] == 'decision.created']
        self.assertEqual([d['needs_attention_now'] for d in created], [True])

    def test_stored_urgent_importance_becomes_needs_attention_now_and_the_rest_is_dropped(self):
        from coconut.decision_inbox import migrate_notification_importance
        decision_id = self.question(optional=True)['id']
        for level in ('urgent', 'important', 'routine', None):
            stored = self.service._get('decisions', decision_id)
            stored.pop('needs_attention_now', None)
            stored['notification_importance'] = level
            self.service._put('decisions', stored)
            migrate_notification_importance(self.service.db)
            migrate_notification_importance(self.service.db)
            stored = self.service._get('decisions', decision_id)
            self.assertNotIn('notification_importance', stored, level)
            self.assertEqual(stored.get('needs_attention_now'), True if level == 'urgent' else None, level)

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
        for explanation in (42, 'x' * 201):
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
        self.assertEqual(first['uncertainty'], 'References are unknown.')
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

    def assert_dismissed_without_answer(self, decision):
        current = self.service._get('decisions', decision['id'])
        self.assertEqual(current['status'], 'dismissed')
        self.assertEqual(current['_answers'], [])
        self.assertIsNone(current['decided_at'])
        self.assertIsNone(current.get('selected_option_id'))
        self.assertFalse(current.get('_inbox_reject_device'))
        self.assertEqual(current['outcome'], 'Dismissed. No answer was sent.')
        # The task ends without a follow-up turn. Nothing resumes it.
        task = self.service._get('tasks', self.task['id'])
        self.assertEqual(task['status'], 'completed')
        self.assertNotIn('_resume_decision', task)
        self.assertFalse([t for t in self.service._all('tasks') if t.get('parent_task_id') == self.task['id']])
        self.assertFalse(self.service.snapshot()['artifacts'])
        return current

    def test_required_dismissal_records_no_answer_after_window_and_restart(self):
        decision = self.question()
        self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='dismiss', revision=1))
        self.restart()
        self.expire_swipe(decision)
        current = self.assert_dismissed_without_answer(decision)
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'dismissed')
        with self.assertRaises(ServiceError):
            self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='restore', revision=1))
        for answer in (dict(choice='reject'), dict(option_id='check')):
            with self.subTest(answer=answer), self.assertRaises(ServiceError):
                self.write('POST', f"/v1/decisions/{decision['id']}/answer", dict(answer, revision=current['revision']))
        self.service.finish_inbox_swipes()
        self.assertEqual(self.service._get('decisions', decision['id']), current)

    def test_dismissal_keeps_a_task_that_waits_for_another_card(self):
        decision = self.question()
        with self.service.transaction():
            other = dict(self.service._get('decisions', decision['id']), id=str(uuid4()))
            self.service._put('decisions', other)
        self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='dismiss', revision=1))
        self.expire_swipe(decision)
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'dismissed')
        self.assertEqual(self.service._get('decisions', other['id'])['status'], 'pending')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')

    def asked_with_receipt(self):
        receipt = self.event('native_call', {'name': 'coconut_ask', 'arguments': {'title': 'Investigate references?'}})
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Investigate references?', receipt_id=receipt['id'],
                                               options=[{'id': 'check', 'label': 'Check references'}]))
        return self.service.snapshot()['decisions'][-1], receipt

    def test_dismissal_takes_effect_only_after_the_undo_window(self):
        decision, receipt = self.asked_with_receipt()
        path = f"/v1/decisions/{decision['id']}/inbox"
        # Dismiss, then Undo inside the window: nothing finished, and the card is whole again.
        dismissed = self.write('POST', path, dict(action='dismiss', revision=1))
        self.service.finish_inbox_swipes()
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'awaiting_answer')
        restored = self.write('POST', path, dict(action='restore', revision=1, swipe_id=dismissed['inbox_swipe_id']))
        self.service.finish_inbox_swipes()
        current = self.service._get('decisions', decision['id'])
        self.assertEqual((current['status'], current['_answers'], current.get('outcome')), ('pending', [], None))
        self.assertNotIn('_inbox_dismissed_at', current)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'awaiting_answer')
        # Dismiss again and let the window end: now the dismissal takes effect.
        self.write('POST', path, dict(action='dismiss', revision=restored['revision']))
        self.expire_swipe(decision)
        self.assert_dismissed_without_answer(decision)
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'completed')

    def test_staggered_dismissals_keep_the_task_open_while_another_undo_window_is_open(self):
        decision = self.question()
        with self.service.transaction():
            other = dict(self.service._get('decisions', decision['id']), id=str(uuid4()))
            self.service._put('decisions', other)
        self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='dismiss', revision=1))
        second = self.write('POST', f"/v1/decisions/{other['id']}/inbox", dict(action='dismiss', revision=1))
        # The first window ends while the second card can still come back.
        self.expire_swipe(decision)
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'dismissed')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        restored = self.write('POST', f"/v1/decisions/{other['id']}/inbox",
                              dict(action='restore', revision=1, swipe_id=second['inbox_swipe_id']))
        self.assertEqual(restored['status'], 'pending')
        self.service.finish_inbox_swipes()
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        # When the second card is dismissed and its window ends, the task ends.
        self.write('POST', f"/v1/decisions/{other['id']}/inbox", dict(action='dismiss', revision=restored['revision']))
        self.expire_swipe(other)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')

    def test_dismissal_tells_the_asking_tool_call_that_the_card_has_no_answer(self):
        import json
        receipt = self.event('native_call', {'name': 'coconut_ask', 'arguments': {'title': 'Investigate references?'}})
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Investigate references?', receipt_id=receipt['id'],
                                               options=[{'id': 'check', 'label': 'Check references'}]))
        decision = self.service.snapshot()['decisions'][-1]
        self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='dismiss', revision=1))
        self.expire_swipe(decision)
        self.assert_dismissed_without_answer(decision)
        record = self.service._get('native_receipts', receipt['id'])
        self.assertEqual(record['state'], 'completed')
        result = json.loads(record['result'])
        self.assertEqual(result['resolution'], 'dismissed')
        self.assertIn('not a rejection', result['response'])

    def test_file_dismissal_never_creates_the_file(self):
        decision = self.file_proposal()
        self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='dismiss', revision=1))
        self.expire_swipe(decision)
        self.assert_dismissed_without_answer(decision)

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

    def test_native_approval_dismissal_records_no_answer_and_never_runs_the_tool(self):
        import json
        decision = self.approval()
        self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='dismiss', revision=1))
        self.expire_swipe(decision)
        current = self.assert_dismissed_without_answer(decision)
        record = self.service._get('native_receipts', current['_receipt_id'])
        self.assertEqual(record['state'], 'completed')
        result = json.loads(record['result'])
        self.assertEqual(result['resolution'], 'dismissed')
        self.assertIn('The tool did not run.', result['error'])

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

    def test_a_chat_reply_answers_a_card_with_an_old_instruction_revision(self):
        decision = self.question()
        saved = self.service._get('decisions', decision['id'])
        saved['_profile_revision'] = 'an-older-revision'
        self.service._put('decisions', saved)
        context = self.reply(decision, 'Check references')
        result = self.service.resolve_decision_reply(context['id'], dict(choice='approve', option_id='check',
                                                                         quote='Check references'))
        self.assertEqual(result['decision']['status'], 'approved')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')

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
        self.assertEqual(delivered['uncertainty'], 'References are unknown.')

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

    def refreshing_question(self):
        """A card with the lock of a service before this release. That service locked a card whose linked PR ended."""
        from test_service_pr import URL
        decision = self.question()
        saved = self.service._get('decisions', decision['id'])
        saved.update(sources=[dict(kind='pull_request', title='Synthetic PR', url=URL)], _source_recheck=True,
                     revision=saved['revision'] + 1)
        self.service._put('decisions', saved)
        return saved

    def test_refreshing_required_dismissal_records_no_answer(self):
        decision = self.refreshing_question()
        self.write('POST', f"/v1/decisions/{decision['id']}/inbox", dict(action='dismiss', revision=decision['revision']))
        self.service.finish_inbox_swipes()
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        self.expire_swipe(decision)
        self.service.finish_inbox_swipes()
        saved = self.assert_dismissed_without_answer(decision)
        self.assertNotIn('source_refreshing', self.service._public(saved))

    def test_preexisting_refreshing_dismissal_does_not_block_dispatch(self):
        import threading
        decision = self.refreshing_question()
        # Durable state saved by the old /inbox endpoint; no new swipe needed.
        decision.update(status='dismissed', _inbox_reject_device='synthetic-phone',
                        inbox_swipe_id='old-swipe', inbox_undo_until='2000-01-01T00:00:00Z')
        self.service._put('decisions', decision)
        self.restart()
        # Admit both the dismissal resume and unrelated work in this dispatch,
        # even when the host's automatic capacity is only one worker.
        self.write('PUT', '/v1/settings/work-capacity', {'limit': 2})
        bot = self.write('POST', '/v1/bots', dict(name='Independent synthetic worker'))
        other = self.write('POST', f"/v1/bots/{bot['id']}/messages", dict(text='Independent fixture work', mode='queue'))
        progressed = threading.Event()
        original = self.service.provider.start_task
        def start(task, *args):
            if task['id'] == other['id']:
                progressed.set()
            return original(task, *args)
        self.service.provider.start_task = start
        self.service._dispatch_pending()
        self.assertTrue(progressed.wait(3), 'The unrelated task must reach its fixture provider.')
        self.service._dispatch_pending()
        # A dismissal saved before this release closes as a dismissal too. It rejects nothing.
        self.assert_dismissed_without_answer(decision)

    def test_refreshing_dismissal_undo_gives_a_card_that_accepts_an_answer(self):
        decision = self.refreshing_question()
        path = f"/v1/decisions/{decision['id']}/inbox"
        dismissed = self.write('POST', path, dict(action='dismiss', revision=decision['revision']))
        restored = self.write('POST', path, dict(action='restore', revision=decision['revision'], swipe_id=dismissed['inbox_swipe_id']))
        saved = self.service._get('decisions', decision['id'])
        self.assertNotIn('source_refreshing', restored)
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(saved['_answers'], [])
        self.assertEqual(restored['revision'], decision['revision'] + 1)
        self.assertFalse(saved.get('_inbox_reject_device'))
        self.service.finish_inbox_swipes()
        # Nothing locks a card now. An answer to the revision before the Undo fails; the current revision is answerable.
        with self.assertRaises(ServiceError):
            self.write('POST', f"/v1/decisions/{decision['id']}/answer", dict(
                choice='approve', option_id='check', revision=decision['revision']))
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        answered = self.write('POST', f"/v1/decisions/{decision['id']}/answer", dict(
            choice='approve', option_id='check', revision=restored['revision']))
        self.assertEqual(answered['status'], 'approved')
