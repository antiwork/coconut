from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
from coconut.server import APIServer
from coconut.service_restart import DRAIN_REQUEST_FILE, DRAINED_FILE, RESTART_DRAIN_SECONDS


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

    def drain_files(self):
        return self.service.root / DRAIN_REQUEST_FILE, self.service.root / DRAINED_FILE

    def test_a_drain_request_pauses_new_turns_and_says_when_no_turn_runs(self):
        request, drained = self.drain_files()
        request.write_text(str(time.time() + 60))
        worker = Mock()
        worker.is_alive.return_value = True
        self.service.running['active'] = worker
        self.service.poll_service_drain()
        # A turn is running: dispatch is paused, and the service does not say it is idle.
        self.assertTrue(self.service.restart_pending)
        self.assertFalse(drained.exists())
        worker.is_alive.return_value = False
        self.service.poll_service_drain()
        self.assertTrue(drained.exists())
        self.assertTrue(self.service.restart_pending)

    def test_a_claim_in_flight_is_a_running_turn_for_the_drain(self):
        request, drained = self.drain_files()
        request.write_text(str(time.time() + 60))
        self.service._kanban_claiming = 1
        self.service.poll_service_drain()
        self.assertFalse(drained.exists())
        self.service._kanban_claiming = 0
        self.service.poll_service_drain()
        self.assertTrue(drained.exists())

    def test_an_ended_drain_request_resumes_dispatch_and_clears_its_files(self):
        request, drained = self.drain_files()
        request.write_text(str(time.time() + 60))
        self.service.poll_service_drain()
        self.assertTrue(self.service.restart_pending and drained.exists())
        # The updater failed and never removed the request. It ends by itself.
        request.write_text(str(time.time() - 1))
        self.service.poll_service_drain()
        self.assertFalse(self.service.restart_pending)
        self.assertFalse(request.exists() or drained.exists())

    def test_an_ended_drain_request_leaves_a_restart_that_a_bot_asked_for(self):
        request, _ = self.drain_files()
        self.assertEqual(self.request()['status'], 'queued')
        request.write_text(str(time.time() - 1))
        self.service.poll_service_drain()
        self.assertTrue(self.service.restart_pending)

    def test_the_server_clears_an_old_drain_request_before_it_announces_drain_support(self):
        from coconut import server
        request, drained = self.drain_files()
        request.write_text(str(time.time() + 600))
        drained.write_text('1')
        record = self.service.root / 'process.json'
        record.unlink(missing_ok=True)
        seen = []
        real = self.service.clear_service_drain
        def clear():
            # The record that announces the support does not exist yet when the old request goes.
            seen.append(record.exists())
            real()
        with patch.object(self.service, 'clear_service_drain', clear):
            server.write_process_record(self.service)
        self.assertEqual(seen, [False])
        self.assertFalse(request.exists() or drained.exists())
        self.assertIs(json.loads(record.read_text())['drain'], True)

    def test_start_does_not_remove_a_drain_request(self):
        request, _ = self.drain_files()
        request.write_text(str(time.time() + 600))
        self.service.start()
        self.assertTrue(request.exists())

    def test_a_bot_restart_that_is_deferred_during_a_drain_does_not_release_the_pause(self):
        request, _ = self.drain_files()
        request.write_text(str(time.time() + 600))
        self.service.poll_service_drain()
        self.assertEqual(self.request()['status'], 'queued')
        worker = Mock()
        worker.is_alive.return_value = True
        self.service.running['active'] = worker
        # The restart deadline passes while a turn still runs.
        self.service.restart_deadline = time.monotonic() - 1
        self.assertFalse(self.service.take_service_restart())
        # The deferral would have released the pause and admitted turns. The drain keeps it.
        self.assertTrue(self.service.restart_pending)

    def test_a_bot_restart_does_not_restart_the_service_while_an_update_drains_it(self):
        request, drained = self.drain_files()
        request.write_text(str(time.time() + 600))
        self.assertEqual(self.request()['status'], 'queued')
        self.service.poll_service_drain()
        # No turn runs, so the drain is complete and the restart would take effect. The update goes first.
        self.assertTrue(drained.exists())
        self.assertFalse(self.service.take_service_restart())
        self.assertFalse(self.service.stopping.is_set())
        # When the request ends, the restart that the bot asked for goes on.
        request.write_text(str(time.time() - 1))
        self.service.poll_service_drain()
        self.assertTrue(self.service.take_service_restart())

    def test_an_ended_drain_keeps_the_pause_of_a_workspace_recovery(self):
        request, _ = self.drain_files()
        request.write_text(str(time.time() + 60))
        self.service.poll_service_drain()
        self.service._workspace_recovery_hold = True
        request.write_text(str(time.time() - 1))
        self.service.poll_service_drain()
        self.assertTrue(self.service.restart_pending)

    def test_the_drain_waits_for_an_uncertain_dispatch(self):
        request, drained = self.drain_files()
        request.write_text(str(time.time() + 60))
        self.service._kanban_dispatch_uncertain = True
        self.service.poll_service_drain()
        self.assertFalse(drained.exists())
        self.service._kanban_dispatch_uncertain = False
        self.service.poll_service_drain()
        self.assertTrue(drained.exists())

    def test_an_ended_drain_keeps_the_pause_of_a_restart_that_a_bot_asked_for_during_it(self):
        request, _ = self.drain_files()
        request.write_text(str(time.time() + 60))
        self.service.poll_service_drain()
        self.assertTrue(self.service.restart_pending)
        # A running turn asks for a restart while the drain holds dispatch.
        self.assertEqual(self.request()['status'], 'queued')
        self.assertIsNotNone(self.service.restart_deadline)
        request.write_text(str(time.time() - 1))
        self.service.poll_service_drain()
        self.assertTrue(self.service.restart_pending)

    def test_the_dispatcher_polls_the_drain_request(self):
        request, drained = self.drain_files()
        request.write_text(str(time.time() + 60))
        self.service._dispatch_pending()
        self.assertTrue(self.service.restart_pending and drained.exists())

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
        # The runner has few processors, and the active turn would fill an automatic limit of one.
        with patch('coconut.work_capacity.automatic_limit', return_value=5), \
                patch('coconut.service_restart.time.monotonic', return_value=100) as clock:
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
