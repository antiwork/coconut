"""A read that waits for new events answers at the commit of the writer, not at its next look at the cursor."""
import statistics
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from coconut.commit_signal import CommitSignal
from coconut.providers import FixtureProvider
from coconut.service import Service
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class CommitSignalTests(unittest.TestCase):
    def test_a_wait_ends_at_the_next_commit(self):
        signal = CommitSignal()
        threading.Timer(.05, signal.notify).start()
        started = time.monotonic()
        self.assertEqual(signal.wait(0, 5), 1)
        self.assertLess(time.monotonic() - started, 2)

    def test_a_commit_before_the_wait_is_not_lost(self):
        signal = CommitSignal()
        seen = signal.count
        signal.notify()
        started = time.monotonic()
        self.assertEqual(signal.wait(seen, 5), 1)
        self.assertLess(time.monotonic() - started, 1)

    def test_a_wait_without_a_commit_ends_after_its_time(self):
        signal = CommitSignal()
        started = time.monotonic()
        self.assertEqual(signal.wait(0, .05), 0)
        self.assertGreaterEqual(time.monotonic() - started, .04)


class WaitingReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'phone')

    def add_message(self):
        with self.service.transaction():
            # The service saves the message and writes its event.
            message = self.service._message(self.bot['id'], 'A streamed word', streaming=True)
        return message['id'], time.perf_counter()

    def delays(self, read, runs=12):
        """The time from each commit to the answer of a read that waited for it."""
        result = []
        for number in range(runs):
            after = self.service._cursor()
            answers = []
            waiter = threading.Thread(target=lambda: answers.append((read(after), time.perf_counter())))
            waiter.start()
            # The commit comes at a different point of the 0.1 s between two looks at the cursor.
            time.sleep(.12 + number * .009)
            identity, committed = self.add_message()
            waiter.join(5)
            self.assertFalse(waiter.is_alive())
            answer, answered = answers[0]
            self.assertGreater(answer['cursor'], after)
            result.append((answer, identity, answered - committed))
        return result

    def test_a_waiting_update_answers_at_the_commit(self):
        delays = self.delays(lambda after: self.service.updates(after, 5, collection_deltas=True))
        for answer, identity, _ in delays:
            self.assertEqual([m['id'] for m in answer['messages']], [identity])
        # A look at the cursor every 0.1 s gave a median of 0.05 s.
        self.assertLess(statistics.median(delay for _, _, delay in delays), .025)

    def test_a_waiting_event_read_answers_at_the_commit(self):
        delays = self.delays(lambda after: self.service.events(after, 5))
        for answer, identity, _ in delays:
            self.assertIn(identity, [e['data'].get('id') for e in answer['events']])
        self.assertLess(statistics.median(delay for _, _, delay in delays), .025)

    def test_a_write_outside_a_transaction_still_ends_the_wait(self):
        after = self.service._cursor()
        answers = []
        waiter = threading.Thread(target=lambda: answers.append(self.service.updates(after, 5, collection_deltas=True)))
        waiter.start()
        time.sleep(.05)
        # This commit tells nobody. The wait finds it at its next look at the cursor.
        self.service.db.execute("INSERT INTO events(type,bot_id,task_id,data,created_at) VALUES ('bot.read',?,NULL,'{}',?)",
                                (self.bot['id'], '2026-10-29T00:00:00Z'))
        waiter.join(2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(answers[0]['cursor'], after + 1)

    def test_a_wait_without_an_event_ends_after_its_time_and_at_the_end_of_the_service(self):
        after = self.service._cursor()
        started = time.monotonic()
        self.assertEqual(self.service.updates(after, .3, collection_deltas=True)['cursor'], after)
        self.assertGreaterEqual(time.monotonic() - started, .25)
        answers = []
        waiter = threading.Thread(target=lambda: answers.append(self.service.updates(after, 20, collection_deltas=True)))
        waiter.start()
        time.sleep(.05)
        self.service.stopping.set()
        self.service.commits.notify()
        waiter.join(2)
        self.assertFalse(waiter.is_alive())


if __name__ == '__main__':
    unittest.main()
