import tempfile
import threading
import unittest
from unittest.mock import Mock
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
from coconut.server import APIServer


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
