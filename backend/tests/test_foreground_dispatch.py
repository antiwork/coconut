"""Direct replies must not wait for background synchronization."""
import threading
import time
import unittest
from unittest.mock import patch

import test_native_kanban as native
from hermes_env import needs_hermes


@needs_hermes
class ForegroundDispatchTests(unittest.TestCase):
    setUp = native.NativeKanbanTests.setUp
    write = native.NativeKanbanTests.write
    task = native.NativeKanbanTests.task

    def test_direct_reply_starts_while_maintenance_waits(self):
        self.provider.supports_native_outcomes = True
        blocked, release = threading.Event(), threading.Event()
        def maintenance():
            blocked.set()
            release.wait(10)
        self.addCleanup(release.set)
        with patch.object(self.service, 'tick_routines', side_effect=maintenance):
            self.service.start()
            self.assertTrue(blocked.wait(3))
            started = time.monotonic()
            task = self.task('Answer this direct question')
            deadline = time.monotonic() + 1
            while task['id'] not in self.provider.started and time.monotonic() < deadline:
                time.sleep(.01)
            latency = (time.monotonic() - started) * 1000
            release.set()
            self.assertIn(task['id'], self.provider.started, f'Direct reply waited {latency:.1f} ms for maintenance.')

    def test_inflight_native_admission_leaves_only_reserved_foreground_slot(self):
        self.provider.supports_native_outcomes = True
        tasks = [self.task('First reply')]
        second = self.write('/v1/bots', {'name': 'Second'})
        tasks.append(self.write('/v1/bots/' + second['id'] + '/messages', {'text': 'Second reply'}))
        self.provider.release.clear()
        with self.service.lock:
            self.service._kanban_dispatching = True
            self.service._dispatch_chat(2)
            self.assertEqual(set(self.service.running), {tasks[0]['id']})
        self.provider.release.set()
