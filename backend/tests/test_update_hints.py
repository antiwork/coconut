"""The event cursor route of the tunnel listener, which the sidecar long-polls for its update hints."""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from coconut.providers import FixtureProvider
from coconut.server import APIServer, TunnelServer
from coconut.service import Service


class TunnelCursorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.servers = []

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join()
        self.service.close()
        self.temp.cleanup()

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return f'http://127.0.0.1:{server.server_port}'

    def get(self, url, headers=None):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def change(self):
        with self.service.transaction():
            self.service._event('settings.updated', {})

    def test_the_cursor_answers_at_once_when_it_differs(self):
        root = self.serve(TunnelServer(0, self.service))
        cursor = self.service._cursor()
        self.assertEqual(self.get(f'{root}/v1/tunnel/cursor?after={cursor + 5}&wait=20'), (200, {'cursor': cursor}))

    def test_a_long_poll_returns_soon_after_a_change(self):
        root = self.serve(TunnelServer(0, self.service))
        cursor = self.service._cursor()
        timer = threading.Timer(.3, self.change)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        status, body = self.get(f'{root}/v1/tunnel/cursor?after={cursor}&wait=20')
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertGreater(body['cursor'], cursor)
        self.assertLess(elapsed, 2)
        self.assertEqual(set(body), {'cursor'}, 'The answer holds only the cursor.')

    def test_a_long_poll_without_a_change_ends_after_the_wait(self):
        cursor = self.service._cursor()
        started = time.monotonic()
        self.assertEqual(self.service.tunnel_cursor(cursor, .3), {'cursor': cursor})
        self.assertGreaterEqual(time.monotonic() - started, .25)

    def test_only_the_sidecar_on_the_tunnel_listener_gets_the_cursor(self):
        tunnel = self.serve(TunnelServer(0, self.service))
        # A phone request always has the peer key, and then the route needs a device token like any other.
        status, _ = self.get(f'{tunnel}/v1/tunnel/cursor?after=0', {'X-Coconut-Peer': 'a' * 64})
        self.assertEqual(status, 401)
        main = self.serve(APIServer(('127.0.0.1', 0), self.service))
        status, _ = self.get(f'{main}/v1/tunnel/cursor?after=0')
        self.assertEqual(status, 401)
        status, body = self.get(f'{tunnel}/v1/tunnel/cursor?after=x')
        self.assertEqual(status, 400, body)


if __name__ == '__main__':
    unittest.main()
