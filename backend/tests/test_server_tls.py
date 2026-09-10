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


if __name__ == '__main__':
    unittest.main()
