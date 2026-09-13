import tempfile
import unittest
from uuid import uuid4

from coconut.service import Service, ServiceError
from coconut.hermes_worker import conversation_input
from coconut.native_conversation import current_message, seed_messages
from test_service import ControlledProvider


class MessageInteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.bot = self.write('/v1/bots', dict(name='Desk', role='Assistant'))
        self.task = self.send('First question')
        self.service._run_task(self.task['id'])
        self.original = next(m for m in self.service.snapshot()['messages'] if m['role'] == 'assistant' and m['kind'] == 'text')

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, path, data, key=None):
        return self.service.mutate('POST', path, data, key or str(uuid4()), 'phone')

    def send(self, text, **fields):
        return self.write(f'/v1/bots/{self.bot["id"]}/messages', dict(text=text, mode='auto', **fields))

    def react(self, active=True, emoji='❤️', key=None):
        return self.write(f'/v1/messages/{self.original["id"]}/reactions', dict(emoji=emoji, active=active), key)

    def test_reply_keeps_plain_display_and_complete_native_context(self):
        original = self.service._get('messages', self.original['id'])
        original['text'] = 'A long answer.\n' * 1500 + 'Last item: use the blue folder.'
        self.service._put('messages', original)
        task = self.send('Use that last item.', reply_to_message_id=original['id'])
        self.service._run_task(task['id'])
        task, bot, history = self.provider.starts[-1]
        request = dict(operation='start', task=task, bot=bot, history=history)
        message = next(m for m in history if m['task_id'] == task['id'] and m['role'] == 'user')
        self.assertEqual(message['text'], 'Use that last item.')
        self.assertEqual(message['reply_to']['message_id'], original['id'])
        prompt, _ = conversation_input(request)
        self.assertIn(original['text'], prompt)
        self.assertIn('your previous message', prompt)
        self.assertTrue(prompt.endswith('Use that last item.'))
        self.assertEqual(current_message(request)['text'], prompt)
        self.assertIn(original['text'], seed_messages(dict(request, operation='resume'))[-2]['content'])

    def test_reply_to_confirmed_user_message(self):
        original = next(m for m in self.service.snapshot()['messages'] if m['role'] == 'user')
        self.send('Correction.', reply_to_message_id=original['id'])
        reply = self.service.snapshot()['messages'][-1]
        self.assertEqual(reply['reply_to']['author'], 'You')
        self.assertEqual(reply['reply_to']['role'], 'user')

    def test_cross_conversation_and_unfinished_targets_rejected(self):
        other = self.write('/v1/bots', dict(name='Tuner', role='Assistant'))
        with self.assertRaises(ServiceError):
            self.write(f'/v1/bots/{other["id"]}/messages', dict(text='Reply', reply_to_message_id=self.original['id']))
        for flag in ('streaming', '_hidden', '_deleted'):
            target = self.service._get('messages', self.original['id'])
            target[flag] = True
            self.service._put('messages', target)
            with self.assertRaises(ServiceError): self.react()
            target[flag] = False
            self.service._put('messages', target)

    def test_reaction_is_idempotent_and_reaches_native_turn_with_reference(self):
        key = str(uuid4())
        first = self.react(key=key)
        self.assertEqual(first['reactions'], ['❤️'])
        self.assertEqual(self.react(key=key), first)
        self.react()
        events = [m for m in self.service.snapshot()['messages'] if m.get('reaction_event')]
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]['task_id'])
        followup = self.send('Continue with the next item.')
        self.service._run_task(followup['id'])
        task, bot, history = self.provider.starts[-1]
        prompt, _ = conversation_input(dict(operation='start', task=task, bot=bot, history=history))
        self.assertIn(self.original['text'], prompt)
        self.assertIn('❤️', prompt)
        self.assertIn('Earlier emoji feedback', prompt)
        self.assertIn('does not approve, reject, or select a pending decision', prompt)
        self.assertEqual(events[0]['reply_to']['message_id'], self.original['id'])
        self.assertEqual(self.react(False)['reactions'], [])
        self.react(False)
        self.assertEqual(len([m for m in self.service.snapshot()['messages'] if m.get('reaction_event')]), 2)

    def test_active_turn_keeps_reaction_for_the_next_requested_turn(self):
        task = self.send('Continue work')
        stored = self.service._get('tasks', task['id'])
        stored['status'] = 'working'
        stored['_turn_message_ids'] = [self.service.snapshot()['messages'][-1]['id']]
        self.service._put('tasks', stored)
        self.react()
        updated = self.service._get('tasks', task['id'])
        self.assertEqual(updated['status'], 'working')
        self.assertEqual(len(updated['_turn_message_ids']), 1)
        self.assertFalse(updated.get('_steering'))
        self.service.emit(task['id'], 'completed', {'summary': 'Complete.'})
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')
        followup = self.send('What is next?')
        self.service._run_task(followup['id'])
        current, bot, history = self.provider.starts[-1]
        prompt, _ = conversation_input(dict(operation='start', task=current, bot=bot, history=history))
        self.assertIn('Earlier emoji feedback', prompt)
        self.assertIn('What is next?', prompt)
        self.assertNotIn('Continue work', prompt)

    def test_reaction_cannot_answer_pending_decision(self):
        task = self.send('Write a report')
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][-1]
        self.react()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.assertEqual(len(self.provider.resumes), 0)

    def test_reaction_source_cannot_resolve_a_decision(self):
        task = self.send('Write a report')
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][-1]
        self.react()
        reaction = next(m for m in reversed(self.service.snapshot()['messages']) if m.get('reaction_event'))
        turn = self.service._get('tasks', task['id'])
        self.assertIsNone(reaction['task_id'])
        turn.update(status='working', _turn_message_ids=[reaction['id']])
        self.service._put('tasks', turn)
        with self.assertRaises(ServiceError):
            self.service.resolve_chat_decision(turn['id'], dict(source_message_id=reaction['id'],
                decision_id=decision['id'], decision_revision=decision['revision'], choice='reject', quote=reaction['text']))
        self.assertEqual(self.service.decision_chat_input(turn)['_decision_reply_sources'], [])
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_reaction_to_mentioned_bot_records_feedback_for_author_in_same_chat(self):
        tuner = self.write('/v1/bots', dict(name='Tuner', role='Assistant'))
        task = self.send('Check this', recipient_bot_ids=[tuner['id']])
        self.service._run_task(task['id'])
        self.original = next(m for m in self.service.snapshot()['messages'] if m['task_id'] == task['id'] and m['role'] == 'assistant')
        self.assertEqual(self.original['bot_id'], self.bot['id'])
        self.assertEqual(self.original['sender_bot_id'], tuner['id'])
        self.react()
        feedback = next(m for m in reversed(self.service.snapshot()['messages']) if m.get('reaction_event'))
        self.assertIsNone(feedback['task_id'])
        self.assertEqual(feedback['bot_id'], self.bot['id'])
        self.assertEqual(feedback['recipient_bot_ids'], [tuner['id']])
        self.assertIn(self.original['id'], self.service._get('bots', tuner['id'])['_pending_reactions'])
        self.assertFalse(self.service._get('bots', self.bot['id']).get('_pending_reactions'))

    def test_invalid_reactions_and_normal_message_metadata(self):
        for emoji, active in [('✅', True), ('❤️', 'yes'), (None, True)]:
            with self.assertRaises(ServiceError): self.react(active, emoji)
        self.react()
        self.send('Normal message')
        last = self.service.snapshot()['messages'][-1]
        self.assertNotIn('reply_to', last)
        self.assertNotIn('reaction_event', last)

    def test_reaction_survives_restart_without_another_delivery(self):
        key = str(uuid4())
        self.react(key=key)
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.assertEqual(self.react(key=key)['reactions'], ['❤️'])
        self.assertEqual(len([m for m in self.service.snapshot()['messages'] if m.get('reaction_event')]), 1)
