import unittest

import test_decision_inbox as fixtures


class DecisionVisibilityTests(unittest.TestCase):
    setUp = fixtures.DecisionInboxTests.setUp
    tearDown = fixtures.DecisionInboxTests.tearDown
    write = fixtures.DecisionInboxTests.write
    event = fixtures.DecisionInboxTests.event
    question = fixtures.DecisionInboxTests.question

    def test_delegate_does_not_receive_private_parent_decisions(self):
        decision = self.question()
        peer = self.write('POST', '/v1/bots', {'name': 'Delegate', 'role': ''})
        with self.service.transaction():
            root = self.service._get('tasks', self.task['id'])
            delegated = self.service._send(peer, {'text': 'Check the public report.'}, source_task=root,
                                           conversation_bot_id=self.bot['id'], exchange_id=root['exchange_id'])
            task = self.service._get('tasks', delegated['id'])
        self.assertEqual(self.service.decision_chat_input(task)['_pending_decisions'], [])
        self.assertEqual([d['id'] for d in self.service.decision_chat_input(root)['_pending_decisions']], [decision['id']])

    def test_delegate_receives_a_decision_only_after_its_message_is_shared(self):
        decision = self.question()
        peer = self.write('POST', '/v1/bots', {'name': 'Delegate', 'role': ''})
        with self.service.transaction():
            root = self.service._get('tasks', self.task['id'])
            delegated = self.service._send(peer, {'text': 'Check the report.'}, source_task=root,
                                           conversation_bot_id=self.bot['id'], exchange_id=root['exchange_id'])
            task = self.service._get('tasks', delegated['id'])
            message = next(m for m in self.service._all('messages') if m.get('decision_id') == decision['id'])
            message['recipient_bot_ids'].append(peer['id'])
            self.service._put('messages', message)
        self.assertEqual([d['id'] for d in self.service.decision_chat_input(task)['_pending_decisions']], [decision['id']])


class SharedVisibilityTests(DecisionVisibilityTests):
    def test_a_shared_pass_sees_a_newly_shared_message_and_ignores_a_hidden_one(self):
        decision = self.question()
        peer = self.write('POST', '/v1/bots', {'name': 'Delegate', 'role': ''})
        with self.service.transaction(), self.service._shared_reads():
            root = self.service._get('tasks', self.task['id'])
            delegated = self.service._send(peer, {'text': 'Check the report.'}, source_task=root,
                                           conversation_bot_id=self.bot['id'], exchange_id=root['exchange_id'])
            task = self.service._get('tasks', delegated['id'])
            self.assertFalse(self.service._decision_visible_to_task(decision, task))
            message = next(m for m in self.service._all('messages') if m.get('decision_id') == decision['id'])
            message['recipient_bot_ids'].append(peer['id'])
            self.service._put('messages', message)
            self.assertTrue(self.service._decision_visible_to_task(decision, task))
            message['_hidden'] = True
            self.service._put('messages', message)
            self.assertFalse(self.service._decision_visible_to_task(decision, task))
