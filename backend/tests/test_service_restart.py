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
from coconut.service_restart import DRAIN_REQUEST_FILE, DRAINED_FILE


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

    def test_an_ended_drain_keeps_the_pause_of_a_workspace_recovery(self):
        request, _ = self.drain_files()
        request.write_text(str(time.time() + 60))
        self.service.poll_service_drain()
        self.service._workspace_recovery_hold = True
        request.write_text(str(time.time() - 1))
        self.service.poll_service_drain()
        self.assertTrue(self.service.restart_pending)

    def test_startup_ends_an_open_checkout_recovery_of_an_earlier_version(self):
        with self.service.transaction():
            self.service._put('workspace_recoveries', dict(id='checkout', state='held', merge_commit='a' * 40,
                                                           request={}, created_at='2026-10-01T00:00:00Z'))
        self.service.close()
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        receipt = self.service._get('workspace_recoveries', 'checkout')
        self.assertEqual((receipt['state'], receipt['error']), ('superseded', 'Checkout recovery was removed.'))
        self.assertFalse(self.service._workspace_recovery_hold)
        self.assertFalse(self.service.restart_pending)

    def test_the_drain_waits_for_an_uncertain_dispatch(self):
        request, drained = self.drain_files()
        request.write_text(str(time.time() + 60))
        self.service._kanban_dispatch_uncertain = True
        self.service.poll_service_drain()
        self.assertFalse(drained.exists())
        self.service._kanban_dispatch_uncertain = False
        self.service.poll_service_drain()
        self.assertTrue(drained.exists())

    def test_the_dispatcher_polls_the_drain_request(self):
        request, drained = self.drain_files()
        request.write_text(str(time.time() + 60))
        self.service._dispatch_pending()
        self.assertTrue(self.service.restart_pending and drained.exists())
