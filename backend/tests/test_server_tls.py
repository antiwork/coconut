import http.client
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import unittest

from coconut.server import APIServer


SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import host


class TLSAcceptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        cert, key = host.certificate(Path(self.temporary.name), 'localhost')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        self.client_context = host.trusted_certificate_context(cafile=cert)
        self.server = APIServer(('127.0.0.1', 0), None, tls_context=context)
        self.accepting = threading.Event()
        get_request = self.server.get_request

        def observed_accept():
            self.accepting.set()
            return get_request()

        self.server.get_request = observed_accept
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def health(self):
        connection = http.client.HTTPSConnection('localhost', self.server.server_port, timeout=1,
                                                 context=self.client_context)
        try:
            connection.request('GET', '/health')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {'ok': True, 'api_version': 1})
        finally:
            connection.close()

    def test_silent_tls_peer_does_not_block_another_https_request(self):
        silent = socket.create_connection(self.server.server_address, timeout=1)
        self.addCleanup(silent.close)
        self.assertTrue(self.accepting.wait(1))
        self.health()

    def test_incomplete_tls_handshake_times_out_and_releases_connection(self):
        self.server.tls_handshake_timeout = .2
        silent = socket.create_connection(self.server.server_address, timeout=1)
        self.addCleanup(silent.close)
        self.assertTrue(self.accepting.wait(1))
        self.assertEqual(silent.recv(1), b'')
        self.health()

    def connection(self):
        connection = http.client.HTTPSConnection('localhost', self.server.server_port, timeout=1,
                                                 context=self.client_context)
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
