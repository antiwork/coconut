import json
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from coconut.native_bridge import NativeBridge
from uuid import uuid4

import test_decision_inbox as inbox_tests
import test_message_drafts as draft_tests
from coconut.hermes_worker import conversation_input
from coconut.service import ServiceError, YieldForDecision


class CardTextResponseTests(unittest.TestCase):
    setUp = inbox_tests.DecisionInboxTests.setUp
    tearDown = inbox_tests.DecisionInboxTests.tearDown
    write = inbox_tests.DecisionInboxTests.write
    event = inbox_tests.DecisionInboxTests.event
    restart = inbox_tests.DecisionInboxTests.restart
    question = inbox_tests.DecisionInboxTests.question
    approval = inbox_tests.DecisionInboxTests.approval
    draft = draft_tests.MessageDraftTests.draft

    def respond(self, decision, text='What did you verify?', key=None):
        return self.service.mutate('POST', f"/v1/decisions/{decision['id']}/answer",
            dict(choice='reply', correction=text, revision=decision['revision']), key or str(uuid4()))

    def test_reply_closes_card_and_resumes_once_after_restart(self):
        decision = self.question()
        key = str(uuid4())
        reply = self.respond(decision, key=key)
        self.assertEqual(reply['status'], 'replied')
        self.assertEqual(self.respond(decision, key=key), reply)
        self.assertFalse(self.service._all('contexts'))
        self.restart()
        state = self.service.snapshot()
        self.assertEqual(state['decisions'][0]['status'], 'replied')
        task = self.service._get('tasks', self.task['id'])
        self.assertEqual((task['status'], task['_resume_decision']), ('queued', decision['id']))
        replies = [m for m in state['messages'] if m['role'] == 'user' and m.get('decision_id') == decision['id']]
        self.assertEqual([m['text'] for m in replies], ['What did you verify?'])
        self.service.provider.resume_task = Mock()
        self.service._run_task(task['id'])
        delivered = self.service.provider.resume_task.call_args.args[3]
        self.assertEqual(delivered['correction'], 'What did you verify?')
        self.assertEqual(delivered['status'], 'replied')
        with self.assertRaises(ServiceError):
            self.write('POST', f"/v1/decisions/{decision['id']}/answer", dict(option_id='check', revision=1))

    def test_optional_reply_keeps_source_work_and_queues_one_continuation(self):
        decision = self.question(optional=True)
        key = str(uuid4())
        self.respond(decision, key=key)
        self.respond(decision, key=key)
        tasks = self.service._all('tasks')
        self.assertEqual(len(tasks), 2)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'working')
        child = next(t for t in tasks if t['id'] != self.task['id'])
        self.assertEqual((child['status'], child['_resume_decision']), ('queued', decision['id']))
        self.assertEqual(child['conversation_bot_id'], self.task['conversation_bot_id'])

    def test_native_question_receipt_carries_the_text_answer(self):
        call = self.event('native_call', dict(name='coconut_ask', arguments={'title': 'Which file?'}))
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Which file?', receipt_id=call['id'], options=[dict(id='one', label='First file')]))
        decision = self.service._all('decisions')[-1]
        self.respond(decision, 'Use the second file.')
        receipt = self.service._get('native_receipts', call['id'])
        self.assertEqual(receipt['state'], 'completed')
        self.assertEqual(json.loads(receipt['result'])['response'], 'Use the second file.')

    def test_reply_to_native_approval_does_not_approve_the_tool(self):
        decision = self.approval()
        reply = self.respond(decision)
        self.assertEqual(reply['status'], 'replied')
        self.assertEqual(self.service._all('native_receipts')[0]['state'], 'awaiting_approval')
        self.assertIsNone(reply['selected_option_id'])
        prompt, _ = conversation_input(dict(operation='resume', decision=reply, task={}, history=[]))
        self.assertIn('A native approval still requires its exact choice.', prompt)

    def test_draft_reply_closes_without_authorizing_a_send(self):
        decision = self.draft()
        reply = self.respond(decision, 'Make the time 3 PM.')
        self.assertEqual(reply['status'], 'replied')
        self.assertEqual(reply['draft'], decision['draft'])
        self.assertIsNone(reply['selected_option_id'])
        prompt, _ = conversation_input(dict(operation='resume', decision=reply, task={}, history=[]))
        self.assertIn('A message draft still requires Send on the current draft.', prompt)
        self.assertEqual(len(self.service._all('tasks')), 2)

    def test_followup_is_new_and_can_link_to_the_text_response(self):
        decision = self.question()
        self.respond(decision)
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Save the verified file?', context='The local check passed.',
                options=[dict(id='save', label='Save file')], previous_decision_id=decision['id']))
        cards = self.service.snapshot()['decisions']
        self.assertEqual([d['status'] for d in cards], ['replied', 'pending'])
        self.assertNotEqual(cards[0]['id'], cards[1]['id'])
        self.assertEqual(cards[1]['previous_decision_id'], decision['id'])

    def test_empty_or_stale_reply_keeps_card_pending(self):
        decision = self.question()
        with self.assertRaises(ServiceError):
            self.respond(decision, '  ')
        with self.assertRaises(ServiceError):
            self.respond(dict(decision, revision=0))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.assertFalse(self.service._all('contexts'))

    def test_exact_native_action_requests_new_permission_without_execution(self):
        decision = self.approval()
        self.respond(decision)
        decision = self.service._get('decisions', decision['id'])
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        modules = {
            'tools.approval': SimpleNamespace(register_gateway_notify=lambda *a: None, unregister_gateway_notify=lambda *a: None),
            'hermes_cli.plugins': SimpleNamespace(PluginContext=lambda *a: SimpleNamespace(register_middleware=lambda *a: SimpleNamespace(dispose=lambda: None)),
                PluginManifest=lambda **kw: None, get_plugin_manager=lambda: None),
            'hermes_cli.middleware': SimpleNamespace(TOOL_EXECUTION_MIDDLEWARE='tool'),
        }
        request = dict(operation='resume', task=self.task, decision=dict(decision, action=decision['_action']))
        with patch.dict('sys.modules', modules):
            bridge = NativeBridge(request, self.event, self.event)
            bridge.install(None)
            try:
                with self.assertRaises(YieldForDecision):
                    bridge.recorded(decision['_action']['name'], decision['_action']['arguments'],
                        lambda args: self.fail('The tool must wait for a fresh approval.'))
            finally:
                bridge.close()
        cards = self.service._all('decisions')
        self.assertEqual([d['status'] for d in cards], ['replied', 'pending'])
        self.assertEqual(cards[1]['_action'], decision['_action'])

    def test_text_pause_survives_automatic_recovery(self):
        work = self.service._register_work(self.task['id'], dict(key='report', title='Check the report', activity='Checking', status='working'))
        decision = self.question()
        self.respond(decision, 'Pause this work. Keep its saved files.')
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.service._register_work(task['id'], dict(key='report', title='Check the report', activity='Paused',
            status='blocked', paused_by_decision_id=decision['id']))
        self.event('completed', dict(summary='Paused as requested.'))
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        row = self.service._work_projection([self.service._get('artifacts', work['id'])])[0]
        self.assertEqual(row['activity'], 'Paused by your decision')
        self.assertEqual(len(self.service._all('tasks')), before)
