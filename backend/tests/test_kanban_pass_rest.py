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
        clock = iter((100.0, 100.0 + seconds))
        fake = SimpleNamespace(monotonic=lambda: next(clock), time=time.time)
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


if __name__ == '__main__':
    unittest.main()
