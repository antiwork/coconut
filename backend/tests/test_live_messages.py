import unittest
from unittest.mock import Mock
from coconut.live_messages import LiveMessages
import test_service


class Agent:
    def __init__(self):
        self.pending = ''
        self.active = True

    def redirect(self, text):
        if not self.active:
            return False
        self.pending += '\n' + text
        return True

    def _drain_pending_redirect(self):
        result, self.pending = self.pending, ''
        return result

    def _drain_pending_steer(self):
        return None


class DeliveryTests(unittest.TestCase):
    def test_startup_preserves_multimodal_input_and_only_acknowledges_success(self):
        consumed = []
        live = LiveMessages(None, consumed.append)
        message = dict(id='correction', text='Use this correction.')
        live.deliver([message, message])
        agent = Mock()
        live.bind(agent)
        prompt = [{'type': 'text', 'text': 'Read this image.'},
                  {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fixture'}}]
        agent.run_conversation.side_effect = lambda text, **kw: dict(
            completed=True, messages=[dict(role='user', content=text)])
        result = live.run_conversation(prompt, persist_user_message='Read this image.')
        received = agent.run_conversation.call_args.args[0]
        self.assertEqual(received[:2], prompt)
        self.assertEqual(len(received), 3)
        self.assertEqual(consumed, [])
        live.finish(result | {'failed': True})
        self.assertEqual(consumed, [])
        live.finish(result)
        self.assertEqual(consumed, [['correction']])
        agent.redirect.assert_not_called()

    def test_deferred_startup_input_cannot_be_skipped_by_later_live_delivery(self):
        live = LiveMessages(None, lambda identities: self.fail('Deferred input was consumed'))
        live.accept = lambda message: False
        live.deliver([dict(id='first', text='Needs a fresh turn.')])
        agent = Mock()
        live.bind(agent)
        live.run_conversation('Original input')
        live.accept = lambda message: True
        live.deliver([dict(id='later', text='Must follow the first input.')])
        agent.redirect.assert_not_called()
        self.assertEqual(live.pending, {})

    def test_acknowledge_only_consumed_input_and_deduplicate_frames(self):
        agent, consumed = Agent(), []
        live = LiveMessages(agent, consumed.append)
        messages = [dict(id='one', text='Thanks, that answers it.'), dict(id='two', text='Unrelated: draft a note.')]
        live.deliver(messages)
        live.deliver(messages)
        self.assertEqual(consumed, [])
        text = agent._drain_pending_redirect()
        self.assertEqual(text.count('Thanks, that answers it.'), 1)
        self.assertIn('Unrelated: draft a note.', text)
        self.assertEqual(consumed, [])
        live.finish(dict(completed=True, messages=[dict(role='user', content=text)]))
        self.assertEqual(consumed, [['one', 'two']])
        live.deliver(messages)
        self.assertEqual(agent._drain_pending_redirect(), '')

    def test_idle_native_turn_leaves_message_available_for_retry(self):
        agent, consumed = Agent(), []
        live = LiveMessages(agent, consumed.append)
        agent.active = False
        live.deliver([dict(id='one', text='Stop the investigation.')])
        self.assertEqual(live.pending, {})
        self.assertEqual(consumed, [])

    def test_unapplied_or_failed_input_remains_durable(self):
        agent, consumed = Agent(), []
        live = LiveMessages(agent, consumed.append)
        live.deliver([dict(id='one', text='Stop this investigation.')])
        text = agent._drain_pending_redirect()
        live.finish(dict(completed=True, pending_steer=text, messages=[]))
        live.finish(dict(completed=False, messages=[dict(role='user', content=text)]))
        live.finish(dict(completed=True, messages=[dict(role='assistant', content=text)]))
        self.assertEqual(consumed, [])

    def test_rejected_earlier_message_blocks_later_delivery(self):
        agent, consumed = Agent(), []
        calls = []
        def redirect(text):
            calls.append(text)
            return len(calls) > 1
        agent.redirect = redirect
        live = LiveMessages(agent, consumed.append)
        live.deliver([dict(id='first', text='First'), dict(id='second', text='Second')])
        self.assertEqual(len(calls), 1)
        self.assertEqual(live.pending, {})

    def test_consumption_cannot_skip_unapplied_earlier_input(self):
        agent, consumed = Agent(), []
        live = LiveMessages(agent, consumed.append)
        live.deliver([dict(id='first', text='First'), dict(id='second', text='Second')])
        live.finish(dict(completed=True, messages=[dict(role='user', content=live.pending['second'])]))
        self.assertEqual(consumed, [])


class LiveServiceTests(unittest.TestCase):
    setUp = test_service.ServiceTests.setUp
    tearDown = test_service.ServiceTests.tearDown
    bot = test_service.ServiceTests.bot
    send = test_service.ServiceTests.send
    write = test_service.ServiceTests.write
    wait = test_service.ServiceTests.wait

    def test_worker_ready_retries_input_sent_before_process_started(self):
        bot = self.bot()
        task = self.send(bot, 'Hold this task')
        self.service.start()
        self.assertTrue(self.provider.started.wait(2))
        self.write('POST', f'/v1/bots/{bot["id"]}/messages', dict(text='Answer this first.', mode='auto'))
        received = []
        self.provider.steer_task = lambda task_id, turn_id, messages: received.extend(messages)
        turn_id = self.service._get('tasks', task['id'])['_native_turn_id']
        self.service.emit(task['id'], 'steering_ready', dict(turn_id='old-turn'))
        self.assertEqual(received, [])
        self.service.emit(task['id'], 'steering_ready', dict(turn_id=turn_id))
        self.assertEqual([row['text'] for row in received], ['Answer this first.'])
        self.provider.hold.set()

    def test_new_topic_reaches_live_turn_without_waiting_for_completion(self):
        bot = self.bot()
        task = self.send(bot, 'Hold this task')
        self.service.start()
        self.assertTrue(self.provider.started.wait(2))
        received = []
        def steer(task_id, turn_id, messages):
            received.extend(messages)
            self.service.emit(task_id, 'steering_consumed', dict(turn_id=turn_id, message_ids=[m['id'] for m in messages]))
        self.provider.steer_task = steer
        response = self.write('POST', f'/v1/bots/{bot["id"]}/messages', dict(text='Unrelated: what is two plus two?', mode='auto'))
        self.assertEqual(response['id'], task['id'])
        self.assertEqual(received[0]['text'], 'Unrelated: what is two plus two?')
        saved = self.service._get('tasks', task['id'])
        self.assertFalse(saved['_steering'])
        self.assertIn(received[0]['id'], saved['_turn_message_ids'])
        self.provider.hold.set()
        self.wait(lambda s: s['tasks'][0]['status'] == 'completed')
        self.assertEqual(len(self.provider.starts), 1)

    def test_unconsumed_input_survives_unavailable_runtime(self):
        bot = self.bot()
        task = self.send(bot, 'Hold this task')
        self.service.start()
        self.assertTrue(self.provider.started.wait(2))
        self.provider.steer_task = lambda *args: None
        self.write('POST', f'/v1/bots/{bot["id"]}/messages', dict(text='Stop this investigation.', mode='auto'))
        saved = self.service._get('tasks', task['id'])
        self.assertEqual(saved['_steering'], ['Stop this investigation.'])
        self.provider.hold.set()
        self.wait(lambda s: len(self.provider.starts) == 2 and s['tasks'][0]['status'] == 'completed')

    def test_live_text_cannot_skip_an_earlier_attachment(self):
        import base64
        bot = self.bot()
        task = self.send(bot, 'Hold this task')
        self.service.start()
        self.assertTrue(self.provider.started.wait(2))
        delivered = []
        self.provider.steer_task = lambda *args: delivered.extend(args[-1])
        artifact = self.write('POST', '/v1/attachments', dict(
            bot_id=bot['id'], filename='notes.txt',
            content_base64=base64.b64encode(b'important request').decode()))
        self.write('POST', f'/v1/bots/{bot["id"]}/messages', dict(
            text='Read these notes.', mode='auto', attachment_ids=[artifact['id']]))
        self.write('POST', f'/v1/bots/{bot["id"]}/messages', dict(text='Then answer this.', mode='auto'))
        self.assertEqual(delivered, [])
        self.provider.hold.set()
        self.wait(lambda s: len(self.provider.starts) == 2 and s['tasks'][0]['status'] == 'completed')
        followup = self.provider.starts[1]
        self.assertTrue(any(m['text'] == 'Read these notes.' for m in followup[2]))
        self.assertTrue(any(m['text'] == 'Then answer this.' for m in followup[2]))
        self.assertEqual(base64.b64decode(followup[0]['attachments'][0]['content_base64']), b'important request')

    def test_live_reply_keeps_context_from_parallel_work(self):
        bot = self.bot()
        task = self.send(bot, 'Hold this task')
        self.service.start()
        self.assertTrue(self.provider.started.wait(2))
        received = []
        self.provider.steer_task = lambda *args: received.extend(args[-1])
        with self.service.transaction():
            current = self.service._get('tasks', task['id'])
            current['_chat_session'] = True
            current.pop('_execution_session_id', None)
            self.service._put('tasks', current)
            self.service._message(bot['id'], 'The separate audit found a missing index.')
        self.write('POST', f'/v1/bots/{bot["id"]}/messages', dict(text='Explain that finding.', mode='auto'))
        self.assertIn('The separate audit found a missing index.', received[0]['text'])
        self.assertIn('Explain that finding.', received[0]['text'])
        self.provider.hold.set()

    def test_consumed_human_input_keeps_direct_reply_routing(self):
        bot, requester = self.bot(), self.bot('Requester')
        task = self.send(bot, 'Hold this task')
        self.service.start()
        self.assertTrue(self.provider.started.wait(2))
        self.provider.steer_task = lambda *args: None
        self.write('POST', f'/v1/bots/{bot["id"]}/messages', dict(text='Please answer me here.', mode='auto'))
        with self.service.transaction():
            saved = self.service._get('tasks', task['id'])
            saved['source_bot_id'] = requester['id']
            self.service._put('tasks', saved)
            identity = next(iter(saved['_live_messages']))
            message = self.service._get('messages', identity)
            message['recipient_bot_ids'] = [bot['id']]
            self.service._put('messages', message)
        self.service.emit(task['id'], 'steering_consumed', dict(
            turn_id=saved['_native_turn_id'], message_ids=[identity]))
        self.service.emit(task['id'], 'text_delta', dict(text='Here is your answer.'))
        reply = next(m for m in reversed(self.service._task_messages(task['id'])) if m['text'] == 'Here is your answer.')
        self.assertEqual(reply['_direct_reply_message_id'], identity)
        self.assertFalse(reply.get('exchange_recipient_bot_ids'))
        self.provider.hold.set()
