"""Regressions for durable, authenticated input arriving during a turn."""
import json
import os
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import encoded
import test_provider
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


class LiveBotSourceWorkerRegressions(unittest.TestCase):
    setUp = test_provider.ProviderTests.setUp
    tearDown = test_provider.ProviderTests.tearDown
    fake_hermes = test_provider.ProviderTests.fake_hermes
    creation_request = test_provider.ProviderTests.creation_request

    def run_live(self, *, authenticated=True, initial_sources=True):
        creation, created = self.creation_request()
        self.task['_bot_deletion_sources'] = self.task['_bot_creation_sources']
        self.task['_deletable_bots'] = [dict(id=created['bot_id'], name=created['name'])]
        if not initial_sources:
            self.task['_bot_creation_sources'] = []
            self.task['_bot_deletion_sources'] = []
        source = dict(id=str(uuid4()), text='Create Scout, then delete Scout.')
        creation['source_message_id'] = created['source_message_id'] = source['id']
        deletion = dict(source_message_id=source['id'], target_bot_id=created['bot_id'], expected_name=created['name'])
        deleted = dict(status='deleted', source_message_id=source['id'], bot_id=created['bot_id'],
                       profile_id=created['bot_id'], name=created['name'], cancelled_task_ids=[])
        marker, gate = self.root / 'ready', self.root / 'delivered'
        provider = self.fake_hermes(f'''
            import time
            from pathlib import Path
            Path({str(marker)!r}).touch()
            while not Path({str(gate)!r}).exists(): time.sleep(.01)
            text = self._drain_pending_redirect()
            if text:
                registry.dispatch('coconut_create_bot', {creation!r})
                registry.dispatch('coconut_delete_bot', {deletion!r})
            return {{'completed': True, 'final_response': 'Done.',
                    'messages': [{{'role': 'user', 'content': text}}]}}
        ''')
        task = self.task | {'_native_turn_id': str(uuid4())}
        events = []
        def emit(kind, data):
            events.append((kind, data))
            return {'bot_create': created, 'bot_delete': deleted}.get(kind)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            worker = threading.Thread(target=lambda: provider.start_task(task, self.bot, [], emit))
            worker.start()
            try:
                deadline = time.monotonic() + 4
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(marker.exists())
                message = dict(source)
                if authenticated:
                    message['bot_source'] = source
                provider.steer_task(task['id'], task['_native_turn_id'], [message])
                # A receipt-free pipe frame has no acknowledgement. The gate keeps
                # the synthetic model active while the worker accepts the frame.
                time.sleep(.1)
            finally:
                gate.touch()
                worker.join(6)
            self.assertFalse(worker.is_alive())
        return events, source

    def test_live_source_authorizes_both_local_payload_validators(self):
        events, source = self.run_live()
        self.assertEqual([kind for kind, _ in events],
                         ['bot_create', 'bot_delete', 'steering_consumed', 'text_delta', 'completed'])
        self.assertEqual(events[2][1]['message_ids'], [source['id']])

    def test_text_and_id_alone_do_not_authorize_bot_operations(self):
        events, _ = self.run_live(authenticated=False)
        self.assertNotIn('bot_create', [kind for kind, _ in events])
        self.assertNotIn('bot_delete', [kind for kind, _ in events])
        self.assertEqual(events[-1][0], 'failed')

    def test_turn_without_bot_tools_retains_authenticated_input_for_next_turn(self):
        events, _ = self.run_live(initial_sources=False)
        self.assertEqual([kind for kind, _ in events], ['text_delta', 'completed'])
