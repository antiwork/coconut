import unittest

import test_service_team


class ExchangeTranscriptTests(unittest.TestCase):
    setUp = test_service_team.TeamServiceTests.setUp
    tearDown = test_service_team.TeamServiceTests.tearDown
    write = test_service_team.TeamServiceTests.write
    send = test_service_team.TeamServiceTests.send
    working = test_service_team.TeamServiceTests.working
    deliver = test_service_team.TeamServiceTests.deliver
    restart = test_service_team.TeamServiceTests.restart
    def test_delegated_reply_is_an_exchange_before_the_first_stream_event(self):
        root = self.working(self.send(self.a))
        delivery = self.deliver(root, self.b, 'Check the report')
        with self.service.transaction():
            reply = self.service._message(self.b['id'], '', task_id=delivery['task_id'], streaming=True)
        self.assertEqual(reply.get('exchange_recipient_bot_ids'), [self.a['id']])
        self.assertEqual(reply['sender_bot_id'], self.b['id'])
        with self.service.transaction():
            stored = self.service._get('messages', reply['id'])
            stored.pop('exchange_recipient_bot_ids')
            self.service._put('messages', stored)
        self.restart()
        restored = next(m for m in self.service.snapshot()['messages'] if m['id'] == reply['id'])
        self.assertEqual(restored['exchange_recipient_bot_ids'], [self.a['id']])

    def test_decision_context_stays_in_the_bot_conversation(self):
        root = self.working(self.send(self.a))
        delivery = self.deliver(root, self.b, 'Check the report')
        with self.service.transaction():
            reply = self.service._message(self.b['id'], 'This needs your choice.',
                task_id=delivery['task_id'], decision_id='pending-decision')
        self.assertNotIn('exchange_recipient_bot_ids', reply)
        self.restart()
        restored = next(m for m in self.service.snapshot()['messages'] if m['id'] == reply['id'])
        self.assertNotIn('exchange_recipient_bot_ids', restored)

    def test_return_to_origin_keeps_its_user_summary_as_a_normal_message(self):
        root = self.working(self.send(self.a))
        delivery = self.deliver(root, self.b, 'Check the report')
        child = self.working({'id': delivery['task_id']})
        returned = self.service._deliver_bot_message(child['id'],
            {'target_bot_id': self.a['id'], 'text': 'Report checked'}, returning=True)
        with self.service.transaction():
            reply = self.service._message(self.a['id'], 'The report is ready.', task_id=returned['task_id'])
        self.assertNotIn('exchange_recipient_bot_ids', reply)
        self.assertEqual(reply['kind'], 'text')

    def test_worker_updates_stay_with_worker_and_exchange_has_both_directions(self):
        root = self.working(self.send(self.a))
        delivery = self.deliver(root, self.b, 'Check the report')
        self.service._run_task(delivery['task_id'])
        self.service._drain_returns()
        messages = self.service.snapshot()['messages']
        updates = [m for m in messages if m['task_id'] == delivery['task_id'] and m['kind'] == 'text']
        self.assertTrue(updates)
        self.assertTrue(all(m['bot_id'] == self.b['id'] for m in updates))
        exchange = [m for m in messages if m['kind'] == 'bot_exchange' or m.get('exchange_recipient_bot_ids')]
        self.assertEqual(len(exchange), 2)
        self.assertEqual(exchange[0]['sender_bot_id'], self.a['id'])
        self.assertEqual(exchange[1]['sender_bot_id'], self.b['id'])
        self.assertEqual(exchange[1]['exchange_recipient_bot_ids'], [self.a['id']])
        identities = [m['id'] for m in exchange]
        self.restart()
        self.service._drain_returns()
        self.assertEqual([m['id'] for m in self.service.snapshot()['messages'] if m['kind'] == 'bot_exchange' or m.get('exchange_recipient_bot_ids')], identities)


if __name__ == '__main__':
    unittest.main()
