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
