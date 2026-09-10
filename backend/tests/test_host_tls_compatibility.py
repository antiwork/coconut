from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


HOST_PATH = Path(__file__).resolve().parents[2] / 'scripts' / 'host.py'
APPLE_PYTHON = Path('/usr/bin/python3')
spec = importlib.util.spec_from_file_location('host_tls_compatibility', HOST_PATH)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)

APPLE_CLIENT = r'''
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import socket
import ssl
import sys

host_path, source, trust_path, port, hostname, expected_pin, remove_partial = sys.argv[1:]
spec = importlib.util.spec_from_file_location('host_tls_client', host_path)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
arguments = {'cafile': trust_path} if source == 'cafile' else {'cadata': Path(trust_path).read_text()}
context = host.trusted_certificate_context(**arguments)
if remove_partial == 'true':
    context.verify_flags &= ~0x80000
result = {
    'ok': False,
    'check_hostname': context.check_hostname,
    'cert_required': context.verify_mode == ssl.CERT_REQUIRED,
    'pin_matches': False,
    'request_sent': False,
}
try:
    with socket.create_connection(('127.0.0.1', int(port)), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=hostname) as connection:
            actual_pin = hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest()
            result['pin_matches'] = hmac.compare_digest(actual_pin, expected_pin)
            if result['pin_matches']:
                connection.sendall(b'GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                result['request_sent'] = True
                response = b''
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                result['ok'] = response.startswith(b'HTTP/1.1 200 OK\r\n') and response.endswith(b'{"ok":true}')
except ssl.SSLCertVerificationError as error:
    result['verify_code'] = error.verify_code
except (ssl.SSLError, OSError):
    result['connection_error'] = True
print(json.dumps(result))
'''


@contextmanager
def temporary_tls_server(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    received = {'health': False}
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve():
            try:
                raw, _ = listener.accept()
                with raw:
                    raw.settimeout(5)
                    with context.wrap_socket(raw, server_side=True) as connection:
                        request = b''
                        while b'\r\n\r\n' not in request and len(request) < 4096:
                            chunk = connection.recv(4096)
                            if not chunk:
                                return
                            request += chunk
                        received['health'] = request.startswith(b'GET /health HTTP/1.1\r\n')
                        connection.sendall(
                            b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}'
                        )
            except (ssl.SSLError, OSError):
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1], received
        finally:
            thread.join(timeout=7)
            if thread.is_alive():
                raise AssertionError('The temporary TLS server did not stop.')


class TrustedCertificateInputTests(unittest.TestCase):
    def test_trust_requires_one_nonempty_explicit_source(self):
        for arguments in ({}, {'cafile': ''}, {'cadata': ''}, {'cadata': ' \n'},
                          {'cafile': 'cert.pem', 'cadata': 'certificate'}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                host.trusted_certificate_context(**arguments)


class AppleTLSCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != 'darwin' or not APPLE_PYTHON.is_file() or not shutil.which('openssl'):
            raise unittest.SkipTest('Apple Python 3.9 and OpenSSL are required for this TLS regression.')
        probe = subprocess.run(
            [str(APPLE_PYTHON), '-c', 'import json,ssl,sys; print(json.dumps([list(sys.version_info[:2]),ssl.OPENSSL_VERSION]))'],
            capture_output=True, text=True, timeout=10,
        )
        if probe.returncode != 0:
            raise unittest.SkipTest('Apple Python is unavailable.')
        version, library = json.loads(probe.stdout)
        if version != [3, 9] or not library.startswith('LibreSSL'):
            raise unittest.SkipTest('This regression requires Apple Python 3.9 with LibreSSL.')
        cls.directory = tempfile.TemporaryDirectory(prefix='coconut-tls-test-')
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        with patch.object(host.socket, 'gethostname', return_value='test-mini'), \
             patch.object(host.socket, 'getaddrinfo', return_value=[]):
            cls.cert, cls.key = host.certificate(root / 'server', 'localhost')
            cls.unrelated_cert, _ = host.certificate(root / 'unrelated', 'localhost')
        cls.expired_cert = root / 'expired.pem'
        request = root / 'expired.csr'
        config = root / 'expired.cnf'
        (root / 'index').write_text('')
        (root / 'serial').write_text('01\n')
        config.write_text(f'''[ca]
default_ca = local
[local]
database = {root / 'index'}
serial = {root / 'serial'}
new_certs_dir = {root}
certificate = {cls.cert}
private_key = {cls.key}
default_md = sha256
policy = subject
x509_extensions = server
[subject]
commonName = supplied
[server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost,IP:127.0.0.1
''')
        subprocess.run(
            [shutil.which('openssl'), 'req', '-new', '-key', str(cls.key),
             '-subj', '/CN=Coconut Local Host', '-out', str(request)],
            check=True, capture_output=True,
        )
        subprocess.run(
            [shutil.which('openssl'), 'ca', '-selfsign', '-batch', '-notext', '-config', str(config),
             '-in', str(request), '-out', str(cls.expired_cert),
             '-startdate', '20200101000000Z', '-enddate', '20200102000000Z'],
            check=True, capture_output=True,
        )

    def connect(self, *, source='cafile', cert=None, trust=None, hostname='localhost', pin=None, remove_partial=False):
        cert = cert or self.cert
        trust = trust or cert
        if pin is None:
            pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        with temporary_tls_server(cert, self.key) as (port, received):
            process = subprocess.run(
                [str(APPLE_PYTHON), '-c', APPLE_CLIENT, str(HOST_PATH), source, str(trust), str(port),
                 hostname, pin, 'true' if remove_partial else 'false'],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(process.returncode, 0, 'The Apple TLS client failed before returning a result.')
            result = json.loads(process.stdout)
        self.assertTrue(result['check_hostname'])
        self.assertTrue(result['cert_required'])
        self.assertEqual(received['health'], result['request_sent'])
        return result

    def assert_rejected(self, result, *, code=None):
        self.assertFalse(result['ok'])
        self.assertFalse(result['request_sent'])
        self.assertFalse(result['pin_matches'])
        if code is not None:
            self.assertEqual(result.get('verify_code'), code)

    def test_cafile_accepts_the_exact_valid_leaf_before_health_request(self):
        result = self.connect(source='cafile')
        self.assertTrue(result['ok'])
        self.assertTrue(result['pin_matches'])
        self.assertTrue(result['request_sent'])

    def test_cadata_accepts_the_exact_valid_leaf_before_health_request(self):
        result = self.connect(source='cadata')
        self.assertTrue(result['ok'])
        self.assertTrue(result['pin_matches'])
        self.assertTrue(result['request_sent'])

    def test_wrong_hostname_prevents_the_health_request(self):
        for source in ('cafile', 'cadata'):
            with self.subTest(source=source):
                result = self.connect(source=source, hostname='wrong-host.invalid')
                self.assert_rejected(result)
                self.assertIn('verify_code', result)

    def test_unrelated_trust_prevents_the_health_request(self):
        for source in ('cafile', 'cadata'):
            with self.subTest(source=source):
                result = self.connect(source=source, trust=self.unrelated_cert)
                self.assert_rejected(result)
                self.assertIn('verify_code', result)

    def test_expired_leaf_prevents_the_health_request(self):
        for source in ('cafile', 'cadata'):
            with self.subTest(source=source):
                self.assert_rejected(self.connect(source=source, cert=self.expired_cert), code=10)

    def test_wrong_pin_prevents_the_health_request(self):
        for source in ('cafile', 'cadata'):
            with self.subTest(source=source):
                result = self.connect(source=source, pin='0' * 64)
                self.assert_rejected(result)
                self.assertNotIn('verify_code', result)
                self.assertNotIn('connection_error', result)

    def test_removing_partial_chain_reproduces_apple_verification_failure(self):
        for source in ('cafile', 'cadata'):
            with self.subTest(source=source):
                self.assert_rejected(self.connect(source=source, remove_partial=True), code=20)


if __name__ == '__main__':
    unittest.main()
