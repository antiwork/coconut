"""A mention identifies a bot without sharing the chat or assigning work."""
from test_service_team import TeamServiceTests
import unittest
from coconut.hermes_worker import conversation_input
from coconut.service import ServiceError


class BotReferenceTests(unittest.TestCase):
    setUp = TeamServiceTests.setUp
    tearDown = TeamServiceTests.tearDown
    write = TeamServiceTests.write
    send = TeamServiceTests.send
    working = TeamServiceTests.working
    def test_reference_keeps_current_bot_and_active_turn(self):
        task = self.working(self.send(self.a, 'Continue the setup.'))
        result = self.send(self.a, 'Update @Lin settings.', mode='auto',
                           mentioned_bot_ids=[self.b['id'], self.b['id']])
        self.assertEqual(result['id'], task['id'])
        self.assertEqual(result['bot_id'], self.a['id'])
        message = self.service.snapshot()['messages'][-1]
        self.assertEqual(message['recipient_bot_ids'], [self.a['id']])
        self.assertEqual(message['bot_mentions'], [dict(id=self.b['id'], name='Lin')])
        self.assertEqual(self.service._history(self.b['id']), [])
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)
        prompt, _ = conversation_input(dict(operation='start', task=task,
                                            history=self.service._history(self.a['id'])))
        self.assertIn(self.b['id'], prompt)
        self.assertIn('The person is addressing this chat', prompt)

    def test_reference_keeps_quoted_message_and_reaction_with_current_bot(self):
        original = self.send(self.a, 'Original question')
        source = self.service.snapshot()['messages'][-1]
        result = self.send(self.a, 'Tell me about @Lin.', mentioned_bot_ids=[self.b['id']],
                           reply_to_message_id=source['id'])
        message = self.service.snapshot()['messages'][-1]
        self.assertEqual(message['reply_to']['message_id'], source['id'])
        self.assertEqual(message['bot_mentions'][0]['id'], self.b['id'])
        self.write('POST', f'/v1/messages/{message["id"]}/reactions', dict(emoji='👍', active=True))
        self.assertTrue(all(t['bot_id'] == self.a['id'] for t in self.service.snapshot()['tasks']))
        self.assertEqual(self.service._history(self.b['id']), [])

    def test_reference_in_card_question_preserves_pending_action(self):
        task = self.send(self.a, 'Write the report')
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][0]
        self.write('POST', f'/v1/tasks/{task["id"]}/replies',
                   dict(text='Would @Lin know?', decision_id=decision['id'], mentioned_bot_ids=[self.b['id']]))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.assertEqual(self.service._get('decisions', decision['id'])['revision'], decision['revision'])
        message = self.service.snapshot()['messages'][-1]
        self.assertEqual(message['bot_mentions'][0]['id'], self.b['id'])
        self.assertEqual(message['decision_id'], decision['id'])
        context = self.service._all('contexts')[-1]
        prompt, _ = conversation_input(dict(operation='context', question=context['question'],
            task=dict(pending_decision=decision, _context_message_id=context['_message_id']),
            history=self.service._history(self.a['id'])))
        self.assertIn(self.b['id'], prompt)
        self.assertEqual(self.service._history(self.b['id']), [])

    def test_reference_validates_before_recording_any_message(self):
        before = self.service.snapshot()
        with self.assertRaises(ServiceError):
            self.send(self.a, mentioned_bot_ids=['missing'])
        self.assertEqual(self.service.snapshot()['messages'], before['messages'])
        self.assertEqual(self.service.snapshot()['tasks'], before['tasks'])
