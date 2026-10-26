import http.client
import json
import threading
import unittest

from coconut.server import APIServer


class LoopbackHTTPTests(unittest.TestCase):
    """The connection behavior of the plain HTTP listener on loopback."""

    def setUp(self):
        self.server = APIServer(('127.0.0.1', 0), None)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def health(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=1)
        try:
            connection.request('GET', '/health')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {'ok': True, 'api_version': 1})
        finally:
            connection.close()

    def connection(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=1)
        self.addCleanup(connection.close)
        return connection

    def test_requests_reuse_one_connection(self):
        accepted = []
        get_request = self.server.get_request
        self.server.get_request = lambda: accepted.append(1) or get_request()
        connection = self.connection()
        for _ in range(3):
            connection.request('GET', '/health')
            response = connection.getresponse()
            self.assertEqual(json.load(response), {'ok': True, 'api_version': 1})
            self.assertFalse(response.will_close)
        self.assertEqual(len(accepted), 1)

    def test_large_json_is_compressed_only_when_the_client_accepts_gzip(self):
        import gzip
        from unittest.mock import patch
        from coconut.server import Handler
        value = {'items': ['message'] * 2000}
        original = Handler._handle
        def answer(handler, method):
            handler._json(200, value)
        with patch.object(Handler, '_handle', answer):
            for encoding, expected in (('gzip, deflate', 'gzip'), ('identity', None), ('gzip;q=0, identity', None), ('br;q=1, GZIP;q=0.5', 'gzip')):
                connection = self.connection()
                connection.request('GET', '/health', headers={'Accept-Encoding': encoding})
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.getheader('Content-Encoding'), expected)
                self.assertEqual(json.loads(gzip.decompress(body) if expected else body), value)
        self.assertIs(Handler._handle, original)
        self.health()

    def test_get_with_a_body_is_rejected_and_closes_the_connection(self):
        connection = self.connection()
        connection.request('GET', '/health', b'GET /health HTTP/1.1', {'Content-Type': 'application/json'})
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertTrue(response.will_close)
        response.read()
        self.health()

    def test_error_before_the_body_is_read_closes_the_connection(self):
        connection = self.connection()
        body = json.dumps({'text': 'GET /health HTTP/1.1'}).encode()
        connection.request('POST', '/v1/bots/example/messages', body, {'Content-Type': 'application/json'})
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        self.assertTrue(response.will_close)
        response.read()
        self.health()


if __name__ == '__main__':
    unittest.main()
