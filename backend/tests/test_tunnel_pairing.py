"""Pairing v2 and the tunnel listener: the phone's static key from the sidecar is bound to its device."""
import base64
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from coconut.providers import FixtureProvider
from coconut.server import APIServer, TunnelServer, create_tunnel_pairing
from coconut.service import Service, ServiceError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import pairing  # noqa: E402

PHONE = 'a' * 64
OTHER = 'b' * 64


class TunnelPairingTests(unittest.TestCase):
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

    def serve(self, tunnel):
        server = TunnelServer(0, self.service) if tunnel else APIServer(('127.0.0.1', 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return f'http://127.0.0.1:{server.server_port}'

    def call(self, root, path, *, body=None, token=None, peer=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        if peer:
            headers['X-Coconut-Peer'] = peer
        request = urllib.request.Request(root + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_a_tunnel_secret_binds_the_phone_key(self):
        secret = self.service.pairing_code(600, tunnel=True)
        self.assertEqual(len(base64.urlsafe_b64decode(secret + '=')), 32)
        stored = self.service.db.execute('SELECT hash FROM pairing WHERE tunnel=1').fetchall()
        self.assertEqual(len(stored), 1)
        self.assertNotIn(secret, stored[0][0])
        with self.assertRaises(ServiceError) as caught:
            self.service.pair({'code': secret})
        self.assertEqual(caught.exception.status, 401)
        token = self.service.pair({'code': secret}, PHONE)['token']
        device = self.service.authenticate(token, PHONE)
        self.assertEqual(self.service.db.execute('SELECT peer_key FROM devices WHERE id=?', (device,)).fetchone()[0], PHONE)
        for peer in (OTHER, None):
            with self.assertRaises(ServiceError):
                self.service.authenticate(token, peer)
        with self.assertRaises(ServiceError):
            self.service.pair({'code': secret}, PHONE)

    def test_a_tls_code_does_not_pair_through_the_tunnel(self):
        code = self.service.pairing_code()
        with self.assertRaises(ServiceError):
            self.service.pair({'code': code}, PHONE)
        token = self.service.pair({'code': code})['token']
        self.assertTrue(self.service.authenticate(token))
        with self.assertRaises(ServiceError):
            self.service.authenticate(token, PHONE)

    def test_the_rate_limit_covers_tunnel_secrets(self):
        self.service.pairing_code(600, tunnel=True)
        for _ in range(5):
            with self.assertRaises(ServiceError):
                self.service.pair({'code': 'wrong'}, PHONE)
        secret = self.service.pairing_code(600, tunnel=True)
        with self.assertRaises(ServiceError) as caught:
            self.service.pair({'code': secret}, PHONE)
        self.assertEqual(caught.exception.status, 429)

    def test_only_the_tunnel_listener_reads_the_peer_header(self):
        tls, tunnel = self.serve(False), self.serve(True)
        secret = self.service.pairing_code(600, tunnel=True)
        # The main listener ignores the header, so a TLS phone cannot claim a key.
        self.assertEqual(self.call(tls, '/v1/pair', body={'code': secret}, peer=PHONE)[0], 401)
        self.assertEqual(self.call(tunnel, '/v1/pair', body={'code': secret})[0], 401)
        status, paired = self.call(tunnel, '/v1/pair', body={'code': secret}, peer=PHONE)
        self.assertEqual(status, 200)
        token = paired['token']
        self.assertEqual(self.call(tunnel, '/v1/state', token=token, peer=PHONE)[0], 200)
        self.assertEqual(self.call(tunnel, '/v1/state', token=token, peer=OTHER)[0], 401)
        self.assertEqual(self.call(tunnel, '/v1/state', token=token)[0], 401)
        self.assertEqual(self.call(tunnel, '/v1/state', token=token, peer=PHONE.upper())[0], 401)
        self.assertEqual(self.call(tls, '/v1/state', token=token, peer=PHONE)[0], 401)
        # A phone that paired over TLS keeps working there, and not through the tunnel.
        old = self.service.pair({'code': self.service.pairing_code()})['token']
        self.assertEqual(self.call(tls, '/v1/state', token=old)[0], 200)
        self.assertEqual(self.call(tunnel, '/v1/state', token=old, peer=PHONE)[0], 401)
        with urllib.request.urlopen(tunnel + '/health') as response:
            self.assertEqual(response.status, 200)


class PairingStoreMigrationTests(unittest.TestCase):
    def test_an_older_store_gets_the_tunnel_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'coconut.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.executescript('''
                CREATE TABLE devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE pairing (hash TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                INSERT INTO devices VALUES ('old', 'iPhone', 'hash', 'now');''')
            secret, expires = create_tunnel_pairing(directory)
            self.assertAlmostEqual(expires, time.time() + 600, delta=5)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute('SELECT peer_key FROM devices').fetchall(), [(None,)])
                self.assertEqual(connection.execute('SELECT tunnel FROM pairing').fetchall(), [(1,)])
            service = Service(directory, FixtureProvider(directory), start_workers=False)
            try:
                self.assertTrue(service.pair({'code': secret}, PHONE)['token'])
            finally:
                service.close()


class PairingLinkTests(unittest.TestCase):
    def payload(self):
        return dict(version=2, relay='https://coconut-push.gumroad.workers.dev', route='c' * 64,
                    mac_key=base64.urlsafe_b64encode(bytes(32)).decode().rstrip('='),
                    secret=base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('='), expires=1_800_000_000, name='Mac mini')

    def test_a_version_2_setup_goes_in_a_version_3_link(self):
        link = pairing.link(self.payload())
        token, marker = link.removeprefix('coconut://pair/').split('?setup=')
        self.assertRegex(token, r'^[a-z2-7]+$')
        self.assertEqual(pairing.read_link(link), self.payload())
        self.assertEqual(pairing.read_link(link.replace('Mac mini', 'x')), self.payload())  # The token holds the name.
        # App builds before this format read only the setup field. Both kinds ask the person to update the app.
        old_build = json.loads(base64.urlsafe_b64decode(marker + '=' * (-len(marker) % 4)))
        self.assertEqual(old_build, dict(version=3, server_url='', certificate_sha256='', pairing_code=''))
        self.assertNotIn('eyJ', link)

    def test_a_version_3_link_holds_the_longest_relay_host_name(self):
        # A host name has at most 253 characters. With the scheme and a port, the relay is longer than 255 bytes.
        host = '.'.join(['a' * 63] * 3 + ['b' * 61])
        for relay in ['https://' + host, 'https://' + host + ':65535']:
            with self.subTest(relay=relay[-12:]):
                self.assertEqual(pairing.read_link(pairing.link(self.payload() | {'relay': relay})), self.payload() | {'relay': relay})

    def test_the_earlier_link_format_still_reads(self):
        data = json.dumps(self.payload(), separators=(',', ':')).encode()
        earlier = 'coconut://pair?setup=' + base64.urlsafe_b64encode(data).decode().rstrip('=')
        self.assertEqual(pairing.read_link(earlier), self.payload())

    def test_a_damaged_link_is_refused(self):
        link = pairing.link(self.payload())
        token = link.removeprefix('coconut://pair/').split('?')[0]
        for damaged in [link.replace(token, token[:40]), link.replace(token, token[:6] + '...' + token[-4:]),
                        link.replace(token, token.upper()), link.replace(token, 'b' + token[1:])]:
            with self.subTest(damaged=damaged[:60]), self.assertRaises(ValueError):
                pairing.read_link(damaged)

    def test_unsafe_version_2_payloads_are_refused(self):
        for changes in [{'relay': 'http://relay.example'}, {'relay': 'https://relay.example/path'}, {'route': 'C' * 64},
                        {'mac_key': 'abc'}, {'secret': self.payload()['secret'][:-2]}, {'expires': '1'}, {'name': ''},
                        {'server_url': 'https://mini:8960'}, {'version': True}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                pairing.validate(self.payload() | changes)

    def test_the_ticket_and_key_follow_the_documented_derivation(self):
        # HKDF-SHA256 with an empty salt (RFC 5869). The sidecar and the iPhone use CryptoKit's HKDF with the same labels.
        ticket_hash, psk = pairing.tunnel_ticket(self.payload()['secret'])
        import hashlib, hmac
        prk = hmac.new(b'', bytes(range(32)), hashlib.sha256).digest()
        ticket = hmac.new(prk, b'coconut pairing ticket\x01', hashlib.sha256).hexdigest()
        self.assertEqual(ticket_hash, hashlib.sha256(ticket.encode()).hexdigest())
        self.assertEqual(psk, hmac.new(prk, b'coconut pairing psk\x01', hashlib.sha256).hexdigest())


if __name__ == '__main__':
    unittest.main()
