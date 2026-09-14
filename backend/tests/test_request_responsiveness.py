import http.client
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.server import APIServer
from coconut.service import Service


class RequestResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)

    def test_slow_settings_read_does_not_block_message_acceptance(self):
        service = self.service
        bot = service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        reading, release, accepted = threading.Event(), threading.Event(), threading.Event()

        def settings(operation='read', bots=()):
            if operation == 'routines_read':
                return {'routines': []}
            reading.set()
            release.wait(3)
            return {'approvals': {}, 'mcp_servers': []}

        service.provider.native_settings = settings
        def send():
            result = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Check the report.'}, str(uuid4()))
            accepted.set()
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            snapshot = pool.submit(service.snapshot)
            self.assertTrue(reading.wait(2))
            message = pool.submit(send)
            try:
                self.assertTrue(accepted.wait(.3), 'Reading settings must not hold the queue lock.')
            finally:
                release.set()
            self.assertEqual(message.result()['status'], 'queued')
            self.assertIn('approvals', snapshot.result())

    def test_snapshot_retries_when_settings_change_during_the_read(self):
        service = self.service
        reading, release = threading.Event(), threading.Event()
        servers = []
        reads = []
        def settings(operation='read', bots=(), **payload):
            if operation == 'routines_read':
                return {'routines': []}
            if operation == 'mcp':
                servers.append({'name': 'Reports'})
                return {'ok': True}
            result = {'approvals': {}, 'mcp_servers': list(servers)}
            reads.append(result)
            if len(reads) == 1:
                reading.set()
                release.wait(3)
            return result
        service.provider.native_settings = settings
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(service.snapshot)
            try:
                self.assertTrue(reading.wait(2))
                service.mutate('POST', '/v1/settings/mcp', {'action': 'add', 'name': 'Reports'}, str(uuid4()))
            finally:
                release.set()
            snapshot = pending.result()
        self.assertEqual(snapshot['mcp_servers'], [{'name': 'Reports'}])
        self.assertEqual(snapshot['cursor'], service._cursor())

    def test_busy_queue_does_not_block_http_acceptance(self):
        service = self.service
        checking = threading.Event()
        original = service.take_service_restart
        def check_restart():
            checking.set()
            return original()
        service.take_service_restart = check_restart
        server = APIServer(('127.0.0.1', 0), service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        try:
            with service.lock:
                thread.start()
                self.assertTrue(checking.wait(1))
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=.3)
                try:
                    connection.request('GET', '/health')
                    self.assertEqual(connection.getresponse().status, 200)
                finally:
                    connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
