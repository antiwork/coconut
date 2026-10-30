import json
import threading
import time
import unittest
from unittest.mock import patch

from coconut import turn_timings
from coconut.turn_timings import timed_lock


def logged(records):
    return [json.loads(record.getMessage().partition('coconut.transaction_timing ')[2]) for record in records]


def busy_work(stop):
    """Run Python until stop is set, so the thread competes for the GIL."""
    total = 0
    while not stop.is_set():
        total += sum(range(1000))
    return total


class LockHoldTimingTests(unittest.TestCase):
    def test_a_short_hold_is_not_logged(self):
        with self.assertNoLogs('coconut.turn_timings', level='WARNING'):
            with timed_lock(threading.RLock(), 'short'):
                pass

    def test_a_slow_hold_logs_its_own_cpu_time(self):
        with patch.object(turn_timings, 'LOG_AFTER', 0.05), patch.object(turn_timings, 'SAMPLE_AFTER', 10):
            with self.assertLogs('coconut.turn_timings', level='WARNING') as logs:
                with timed_lock(threading.RLock(), 'sleeping'):
                    time.sleep(0.1)
        [data] = logged(logs.records)
        self.assertEqual(data['operation'], 'sleeping')
        self.assertGreaterEqual(data['hold_ms'], 90)
        # A sleep holds the lock but uses no CPU, so the hold is mostly a wait.
        self.assertLess(data['cpu_ms'], data['hold_ms'] / 2)
        self.assertNotIn('samples', data)

    def test_a_slow_hold_samples_the_holder_and_the_busy_threads(self):
        stop = threading.Event()
        worker = threading.Thread(target=busy_work, args=(stop,), name='busy-worker')
        worker.start()
        try:
            with patch.object(turn_timings, 'LOG_AFTER', 0.05), patch.object(turn_timings, 'SAMPLE_AFTER', 0.05), \
                    patch.object(turn_timings, 'SAMPLE_EVERY', 0.02):
                with self.assertLogs('coconut.turn_timings', level='WARNING') as logs:
                    with timed_lock(threading.RLock(), 'sampled'):
                        end = time.perf_counter() + 0.5
                        while time.perf_counter() < end:
                            time.sleep(0.01)
        finally:
            stop.set()
            worker.join()
        [data] = logged(logs.records)
        self.assertGreater(data['samples'], 0)
        self.assertLessEqual(data['samples'], turn_timings.SAMPLE_LIMIT)
        self.assertTrue(any('test_lock_hold_timing.py' in stack for stack, _ in data['holder']))
        self.assertTrue(any(stack.startswith('busy-worker: ') and 'busy_work' in stack for stack, _ in data['others']))

    def test_an_idle_thread_is_not_counted(self):
        waiting = threading.Event()
        idle = threading.Thread(target=waiting.wait, name='idle-worker')
        idle.start()
        try:
            with patch.object(turn_timings, 'LOG_AFTER', 0.05), patch.object(turn_timings, 'SAMPLE_AFTER', 0.05), \
                    patch.object(turn_timings, 'SAMPLE_EVERY', 0.02):
                with self.assertLogs('coconut.turn_timings', level='WARNING') as logs:
                    with timed_lock(threading.RLock(), 'idle'):
                        time.sleep(0.3)
        finally:
            waiting.set()
            idle.join()
        [data] = logged(logs.records)
        self.assertFalse(any(stack.startswith('idle-worker: ') for stack, _ in data['others']))

    def test_a_sample_after_the_hold_ended_changes_nothing(self):
        sampler = turn_timings.HoldSampler()
        token = sampler.start('ended')
        sampler.holds[token]['started'] -= 10
        sampler.sample()
        summary = sampler.stop(token)
        self.assertEqual(summary['samples'], 1)
        # The summary holds plain lists. A later sample cannot change it or fail on the ended hold.
        before = json.dumps(summary)
        sampler.sample()
        self.assertEqual(json.dumps(summary), before)
        self.assertEqual(sampler.holds, {})

    def test_a_failing_diagnostic_never_fails_the_hold(self):
        ran = []
        with patch.object(turn_timings, 'LOG_AFTER', 0.0), \
                patch.object(turn_timings._sampler, 'stop', side_effect=RuntimeError('dictionary changed size')):
            with self.assertLogs('coconut.turn_timings', level='WARNING') as logs:
                with timed_lock(threading.RLock(), 'committed'):
                    ran.append(True)
        self.assertEqual(ran, [True])
        self.assertTrue(any('A lock hold diagnostic failed.' in record.getMessage() for record in logs.records))
        [data] = [json.loads(r.getMessage().partition('coconut.transaction_timing ')[2]) for r in logs.records
                  if 'transaction_timing' in r.getMessage()]
        self.assertEqual(data['operation'], 'committed')
        self.assertNotIn('samples', data)

    def test_an_error_in_the_hold_still_logs_and_raises(self):
        with patch.object(turn_timings, 'LOG_AFTER', 0.0):
            with self.assertLogs('coconut.turn_timings', level='WARNING') as logs, self.assertRaises(ValueError):
                with timed_lock(threading.RLock(), 'failing'):
                    raise ValueError('stop')
        [data] = logged(logs.records)
        self.assertEqual(data['operation'], 'failing')
        self.assertIsNotNone(data['cpu_ms'])


if __name__ == '__main__':
    unittest.main()
