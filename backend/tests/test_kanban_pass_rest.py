"""A pass of the Kanban dispatcher rests for twice its own time, so that commits cannot make passes run back to back."""
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from coconut import kanban_scheduler
from coconut.providers import FixtureProvider
from coconut.service import Service


class KanbanPassRestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / 'coconut'
        self.service = Service(root, FixtureProvider(root), start_workers=False)
        self.addCleanup(self.service.close)
        self.service.kanban = SimpleNamespace(tasks={}, call=lambda request, spawn=None: {'tasks': []}, close=lambda: None)
        self.service.provider.trim_kanban_workers = lambda keep: None
        self.rests, self.waits = [], []

    def run_pass(self, seconds):
        reads = []
        # The first reading starts the pass. Every later reading is the end of the pass.
        fake = SimpleNamespace(monotonic=lambda: 100.0 if not reads.append(1) and len(reads) == 1 else 100.0 + seconds, time=time.time)
        self.service.changed.wait = lambda timeout=None: self.waits.append(timeout)
        with patch.object(kanban_scheduler, 'time', fake), patch.object(self.service.stopping, 'wait', self.rests.append):
            self.service._dispatch_kanban()

    def test_a_slow_pass_rests_for_twice_its_time_up_to_one_second(self):
        self.run_pass(0.3)
        self.assertAlmostEqual(self.rests[-1], 0.6)
        self.assertAlmostEqual(self.waits[-1], 0.4)
        self.run_pass(2.0)
        self.assertEqual(self.rests[-1], kanban_scheduler.KANBAN_PASS_REST_SECONDS)
        self.assertEqual(self.waits[-1], 0.0)

    def test_a_quick_pass_rests_briefly_and_still_waits_for_a_wakeup(self):
        self.run_pass(0.01)
        self.assertAlmostEqual(self.rests[-1], 0.02)
        self.assertAlmostEqual(self.waits[-1], 0.98)

    def test_the_work_migration_runs_once_in_its_interval(self):
        calls, clock = [], [1000.0]
        self.service._migrate_native_work = lambda: calls.append(clock[0])
        fake = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
        self.service.changed.wait = lambda timeout=None: None
        with patch.object(kanban_scheduler, 'time', fake), patch.object(self.service.stopping, 'wait', lambda rest: None):
            for now in (1000.0, 1003.0, 1009.0, 1010.5, 1011.0):
                clock[0] = now
                self.service._dispatch_kanban()
        self.assertEqual(calls, [1000.0, 1010.5])

    def test_a_failed_migration_runs_again_on_the_next_pass(self):
        attempts = []
        def migrate():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError('boom')
        self.service._migrate_native_work = migrate
        self.service.changed.wait = lambda timeout=None: None
        with patch.object(self.service.stopping, 'wait', lambda rest: None):
            with self.assertRaises(RuntimeError):
                self.service._dispatch_kanban()
            self.service._dispatch_kanban()
        self.assertEqual(len(attempts), 2)


if __name__ == '__main__':
    unittest.main()
