import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
from coconut.server import APIServer
from coconut.service_restart import RESTART_DRAIN_SECONDS


class ServiceRestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        self.task = self.service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Restart Coconut', 'mode': 'queue'}, str(uuid4()))
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.service.restart_enabled = True

    def tearDown(self):
        self.service.running.clear()
        self.service.context_threads.clear()
        self.service.close()
        self.temp.cleanup()

    def request(self):
        return self.service.emit(self.task['id'], 'service_restart', {})

    def test_restart_waits_for_reply_and_context_then_exits_once(self):
        self.assertEqual(self.request()['status'], 'queued')
        worker = Mock()
        worker.is_alive.return_value = True
        self.service.running['active'] = worker
        self.assertFalse(self.service.take_service_restart())
        self.service.running.clear()
        self.service.context_threads['context'] = worker
        self.assertFalse(self.service.take_service_restart())
        worker.is_alive.return_value = False
        server = Mock(service=self.service)
        with self.assertRaises(KeyboardInterrupt):
            APIServer.service_actions(server)
        self.assertTrue(self.service.stopping.is_set())
        self.assertFalse(self.service.take_service_restart())

    def test_retry_after_restart_does_not_request_another_restart(self):
        self.request()
        self.service.close()
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.service.restart_enabled = True
        self.assertEqual(self.request()['status'], 'restarted')
        self.assertFalse(self.service.restart_pending)
        self.assertFalse(self.service.take_service_restart())

    def test_long_turn_releases_dispatch_without_interrupting_the_worker(self):
        active = Mock()
        active.is_alive.return_value = True
        self.service.running[self.task['id']] = active
        bot = self.service.mutate('POST', '/v1/bots', {'name': 'Dev'}, str(uuid4()))
        queued = self.service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Review the PR'}, str(uuid4()))
        with patch('coconut.service_restart.time.monotonic', return_value=100) as clock:
            self.request()
            self.service._dispatch_pending()
            self.assertNotIn(queued['id'], self.service.running)
            clock.return_value += RESTART_DRAIN_SECONDS
            self.assertFalse(self.service.take_service_restart())
            self.assertFalse(self.service.stopping.is_set())
            started = []
            real_start = threading.Thread.start
            def record(thread):
                started.append(thread)
                real_start(thread)
            # The worker can finish and leave service.running before this test reads it, so record the thread itself.
            with patch.object(threading.Thread, 'start', record):
                self.service._dispatch_pending()
        self.assertTrue(started)
        for worker in started:
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.service._get('tasks', queued['id'])['status'], 'completed')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'working')
        self.assertIs(self.service.running[self.task['id']], active)
        self.assertFalse(self.service.take_service_restart())
        active.is_alive.return_value = False
        self.assertTrue(self.service.take_service_restart())
        self.assertFalse(self.service.take_service_restart())

    def check_trim_drain_deadline(self, active_turn):
        active = Mock()
        active.is_alive.return_value = active_turn
        self.service.running[self.task['id']] = active
        self.service._kanban_trimming = True
        with patch('coconut.service_restart.time.monotonic', return_value=100) as clock:
            self.request()
            clock.return_value += RESTART_DRAIN_SECONDS
            self.assertFalse(self.service.take_service_restart())
            self.assertFalse(self.service.restart_pending)
            self.assertFalse(self.service.stopping.is_set())
            active.is_alive.return_value = False
            self.assertFalse(self.service.take_service_restart())
            self.service._kanban_trimming = False
            self.assertTrue(self.service.take_service_restart())

    def test_trim_does_not_extend_the_active_turn_drain_deadline(self):
        self.check_trim_drain_deadline(True)

    def test_trim_alone_does_not_extend_the_drain_deadline(self):
        self.check_trim_drain_deadline(False)

    def test_context_defers_once_and_reports_deferral_on_repeated_requests(self):
        active = Mock()
        active.is_alive.return_value = True
        self.service.context_threads['context'] = active
        with patch('coconut.service_restart.time.monotonic', return_value=100) as clock:
            self.request()
            clock.return_value += RESTART_DRAIN_SECONDS - 1
            self.assertFalse(self.service.take_service_restart())
            self.assertTrue(self.service.restart_pending)
            clock.return_value += 1
            self.assertFalse(self.service.take_service_restart())
            self.assertEqual(self.request()['status'], 'deferred')
            for _ in range(3):
                self.assertFalse(self.service.take_service_restart())
        events = self.service.db.execute("SELECT count(*) FROM events WHERE type='service.restart_deferred'").fetchone()[0]
        self.assertEqual(events, 1)
        self.assertFalse(self.service.restart_pending)
        active.is_alive.return_value = False
        self.assertTrue(self.service.take_service_restart())

    def test_another_request_cannot_extend_or_restart_the_dispatch_pause(self):
        active = Mock()
        active.is_alive.return_value = True
        self.service.running[self.task['id']] = active
        with patch('coconut.service_restart.time.monotonic', return_value=100) as clock:
            self.request()
            clock.return_value += RESTART_DRAIN_SECONDS - 1
            with self.service.transaction():
                task = self.service._get('tasks', self.task['id'])
                self.service._advance_input(task)
                self.service._put('tasks', task)
            self.assertEqual(self.request()['status'], 'queued')
            clock.return_value += 1
            self.assertFalse(self.service.take_service_restart())
            self.assertFalse(self.service.restart_pending)
            with self.service.transaction():
                task = self.service._get('tasks', self.task['id'])
                self.service._advance_input(task)
                self.service._put('tasks', task)
            self.assertEqual(self.request()['status'], 'deferred')
            self.assertFalse(self.service.restart_pending)

    def test_new_turn_can_request_another_restart(self):
        self.request()
        self.service.close()
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.service.restart_enabled = True
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            self.service._advance_input(task)
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.assertEqual(self.request()['status'], 'queued')
        self.assertTrue(self.service.restart_pending)

    def test_unmanaged_service_does_not_exit(self):
        self.service.restart_enabled = False
        self.assertEqual(self.request()['status'], 'unavailable')
        self.assertFalse(self.service.restart_pending)

    def test_invalid_request_does_not_restart(self):
        with self.assertRaises(ServiceError):
            self.service.emit(self.task['id'], 'service_restart', {'command': 'anything'})
        self.assertFalse(self.service.restart_pending)
