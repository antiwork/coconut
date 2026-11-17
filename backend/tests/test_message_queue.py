"""The queue of a running task: the messages that wait for its next turn (message_queue.py)."""
import base64
import unittest
from uuid import uuid4

from coconut.service import ServiceError
import test_service


class MessageQueueTests(unittest.TestCase):
    setUp = test_service.ServiceTests.setUp
    tearDown = test_service.ServiceTests.tearDown
    bot = test_service.ServiceTests.bot
    send = test_service.ServiceTests.send
    write = test_service.ServiceTests.write
    wait = test_service.ServiceTests.wait

    def working(self):
        bot = self.bot()
        task = self.send(bot, 'Hold this task')
        self.service.start()
        self.assertTrue(self.provider.started.wait(2))
        return bot, task

    def say(self, bot, text, **values):
        return self.write('POST', f'/v1/bots/{bot["id"]}/messages', dict(text=text, mode='auto', **values))

    def queue(self, task):
        return self.service._public(self.service._get('tasks', task['id'])).get('queued_messages', [])

    def path(self, task, *rest):
        return '/'.join([f'/v1/tasks/{task["id"]}/queue', *rest])

    def test_messages_to_a_working_task_show_as_its_queue_in_order(self):
        bot, task = self.working()
        self.assertEqual(self.queue(task), [])
        answer = self.say(bot, 'Check the refund totals.')
        self.say(bot, 'Then draft the reply.')
        self.assertEqual([m['text'] for m in answer['queued_messages']], ['Check the refund totals.'])
        queue = self.queue(task)
        self.assertEqual([m['text'] for m in queue], ['Check the refund totals.', 'Then draft the reply.'])
        self.assertEqual({m['state'] for m in queue}, {'queued'})
        self.assertFalse(any(m['can_steer'] for m in queue))
        # The state that the apps read has the queue too.
        self.assertEqual([m['text'] for m in self.service.snapshot()['tasks'][0]['queued_messages']],
                         ['Check the refund totals.', 'Then draft the reply.'])
        self.provider.hold.set()
        state = self.wait(lambda s: len(self.provider.starts) == 2 and s['tasks'][0]['status'] == 'completed')
        # The next turn took the messages, so the queue is empty.
        self.assertNotIn('queued_messages', state['tasks'][0])

    def test_an_attachment_counts_with_its_message(self):
        bot, task = self.working()
        artifact = self.write('POST', '/v1/attachments', dict(
            bot_id=bot['id'], filename='notes.txt', content_base64=base64.b64encode(b'notes').decode()))
        self.say(bot, 'Read these notes.', attachment_ids=[artifact['id']])
        self.say(bot, 'Then answer.')
        queue = self.queue(task)
        self.assertEqual([m['attachment_ids'] for m in queue], [[artifact['id']], []])
        self.provider.hold.set()

    def test_edit_changes_the_text_that_the_next_turn_reads(self):
        bot, task = self.working()
        self.say(bot, 'Check the refund totals.')
        self.say(bot, 'Then draft the reply.')
        first = self.queue(task)[0]
        answer = self.write('PATCH', self.path(task, first['id']), dict(text='Check the payout totals.'))
        self.assertEqual([m['text'] for m in answer['queued_messages']], ['Check the payout totals.', 'Then draft the reply.'])
        self.assertEqual(self.service._get('tasks', task['id'])['_steering'], ['Check the payout totals.', 'Then draft the reply.'])
        with self.assertRaises(ServiceError):
            self.write('PATCH', self.path(task, first['id']), dict(text=' '))
        self.provider.hold.set()
        self.wait(lambda s: len(self.provider.starts) == 2 and s['tasks'][0]['status'] == 'completed')
        texts = [m['text'] for m in self.provider.starts[1][2]]
        self.assertIn('Check the payout totals.', texts)
        self.assertNotIn('Check the refund totals.', texts)

    def test_remove_takes_the_message_out_of_the_chat_and_the_turn(self):
        bot, task = self.working()
        artifact = self.write('POST', '/v1/attachments', dict(
            bot_id=bot['id'], filename='notes.txt', content_base64=base64.b64encode(b'notes').decode()))
        self.say(bot, 'Read these notes.', attachment_ids=[artifact['id']])
        self.say(bot, 'Then answer.')
        first = self.queue(task)[0]
        answer = self.write('DELETE', self.path(task, first['id']))
        self.assertEqual([m['text'] for m in answer['queued_messages']], ['Then answer.'])
        saved = self.service._get('tasks', task['id'])
        self.assertEqual(saved['_steering'], ['Then answer.'])
        self.assertEqual(saved['_attachment_ids'], [])
        shown = [m['text'] for m in self.service.snapshot()['messages']]
        self.assertNotIn('Read these notes.', shown)
        self.assertNotIn('notes.txt', shown)
        with self.assertRaises(ServiceError) as gone:
            self.write('DELETE', self.path(task, first['id']))
        self.assertEqual(gone.exception.status, 409)
        self.write('DELETE', self.path(task, self.queue(task)[0]['id']))
        self.assertEqual(self.queue(task), [])
        self.provider.hold.set()
        # Nothing waits, so no second turn starts.
        self.wait(lambda s: s['tasks'][0]['status'] == 'completed')
        self.assertEqual(len(self.provider.starts), 1)

    def test_order_moves_the_messages_for_the_turn_and_for_the_apps(self):
        bot, task = self.working()
        for text in ('One', 'Two', 'Three'):
            self.say(bot, text)
        ids = [m['id'] for m in self.queue(task)]
        answer = self.write('POST', self.path(task, 'order'), dict(message_ids=[ids[2], ids[0], ids[1]]))
        self.assertEqual([m['text'] for m in answer['queued_messages']], ['Three', 'One', 'Two'])
        self.assertEqual(self.service._get('tasks', task['id'])['_steering'], ['Three', 'One', 'Two'])
        times = [m['created_at'] for m in answer['queued_messages']]
        self.assertEqual(times, sorted(times))
        for wrong in ([ids[0]], ids + [str(uuid4())], 'no'):
            with self.assertRaises(ServiceError):
                self.write('POST', self.path(task, 'order'), dict(message_ids=wrong))
        self.provider.hold.set()
        self.wait(lambda s: len(self.provider.starts) == 2 and s['tasks'][0]['status'] == 'completed')
        texts = [m['text'] for m in self.provider.starts[1][2] if m['text'] in ('One', 'Two', 'Three')]
        self.assertEqual(texts, ['Three', 'One', 'Two'])

    def test_steer_needs_a_provider_that_takes_input_in_a_running_turn(self):
        bot, task = self.working()
        artifact = self.write('POST', '/v1/attachments', dict(
            bot_id=bot['id'], filename='notes.txt', content_base64=base64.b64encode(b'notes').decode()))
        # A message after one with an attachment does not go to the running turn by itself.
        self.say(bot, 'One', attachment_ids=[artifact['id']])
        self.say(bot, 'Two')
        first, second = self.queue(task)
        with self.assertRaises(ServiceError) as refused:
            self.write('POST', self.path(task, second['id'], 'steer'))
        self.assertEqual(refused.exception.status, 409)
        received = []
        self.provider.steer_task = lambda task_id, turn_id, messages: received.extend(messages)
        # The task holds the queue of its last write. This reads it again with the new provider.
        queue = self.service._queued_messages(self.service._get('tasks', task['id']))
        self.assertEqual([(m['state'], m['can_steer']) for m in queue], [('queued', False), ('queued', True)])
        with self.assertRaises(ServiceError):
            self.write('POST', self.path(task, first['id'], 'steer'))
        answer = self.write('POST', self.path(task, second['id'], 'steer'))
        self.assertEqual([m['state'] for m in answer['queued_messages']], ['queued', 'steering'])
        self.assertEqual([m['text'] for m in received], ['Two'])
        # The running turn can have the message already, so it stays as it is.
        for method in ('DELETE', 'PATCH'):
            with self.assertRaises(ServiceError):
                self.write(method, self.path(task, second['id']), dict(text='Other'))
        turn = self.service._get('tasks', task['id'])['_native_turn_id']
        self.service.emit(task['id'], 'steering_consumed', dict(turn_id=turn, message_ids=[second['id']]))
        self.assertEqual([m['text'] for m in self.queue(task)], ['One'])
        self.provider.hold.set()


if __name__ == '__main__':
    unittest.main()
