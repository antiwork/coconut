"""Regressions for durable, authenticated input arriving during a turn."""
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import encoded
import test_service


class LiveInputServiceRegressions(unittest.TestCase):
    setUp = test_service.ServiceTests.setUp
    tearDown = test_service.ServiceTests.tearDown
    bot = test_service.ServiceTests.bot
    send = test_service.ServiceTests.send
    write = test_service.ServiceTests.write

    def working(self):
        bot = self.bot()
        task = self.send(bot, 'Investigate the issue')
        with self.service.transaction():
            task = self.service._get('tasks', task['id'])
            task.update(status='working', _native_turn_id=str(uuid4()),
                        _turn_message_ids=[m['id'] for m in self.service._task_messages(task['id'])],
                        _turn_preference_source_ids=list(task['_preference_source_ids']))
            self.service._put('tasks', task)
        return bot, task

    def test_json_roundtrip_preserves_delivery_order_and_unconsumed_followup(self):
        bot, task = self.working()
        first, second = 'ffffffff-ffff-4fff-8fff-ffffffffffff', '00000000-0000-4000-8000-000000000000'
        for identity, text in [(first, 'First request'), (second, 'Correction to the first request')]:
            with (patch('coconut.service.uid', return_value=identity),
                  patch('coconut.service.now', return_value='2026-09-22T12:00:00Z')):
                self.write('POST', f'/v1/bots/{bot["id"]}/messages', dict(text=text, mode='auto'))
        saved = json.loads(encoded(self.service._get('tasks', task['id'])))
        self.assertEqual(list(saved['_live_messages']), [second, first])
        received = []
        self.provider.steer_task = lambda *args: received.extend(args[-1])
        self.service.emit(task['id'], 'steering_ready', dict(turn_id=task['_native_turn_id']))
        self.assertEqual([m['id'] for m in received], [first, second])
        self.service.emit(task['id'], 'steering_consumed', dict(
            turn_id=task['_native_turn_id'], message_ids=[received[0]['id']]))
        self.service.emit(task['id'], 'completed', dict(summary='First input handled.'))
        self.service._run_task(task['id'])
        followup = self.provider.starts[-1][0]
        self.assertEqual(followup['_turn_message_ids'], [second])

    def test_live_bot_source_is_authenticated_by_the_service(self):
        bot, task = self.working()
        received = []
        self.provider.steer_task = lambda *args: received.extend(args[-1])
        self.write('POST', f'/v1/bots/{bot["id"]}/messages',
                   dict(text='Create Scout, then delete Lin.', mode='auto'))
        message = received[-1]
        self.assertEqual(message.get('bot_source'), dict(id=message['id'], text=message['text']))
        # Being in the live queue alone is insufficient: every service delivery
        # must still validate the stored human provenance and audience.
        for changes in ({'_human_device_id': None}, {'_human_source_kind': 'settings'},
                        {'sender_bot_id': str(uuid4())}, {'recipient_bot_ids': []},
                        {'thread_id': str(uuid4())}, {'role': 'assistant'}):
            with self.subTest(changes=changes), self.service.transaction():
                original = self.service._get('messages', message['id'])
                self.service._put('messages', original | changes)
                received.clear()
                self.service._deliver_live_messages(task['id'])
                self.assertNotIn('bot_source', received[-1])
                self.service._put('messages', original)
