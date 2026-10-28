#!/usr/bin/env python3
"""Loopback test of the relay transport: test client, Miniflare relay, tunnel sidecar, and backend on this Mac.

The test client (scripts/tunnel-test-client.swift) pairs with a version 2 link, then runs a real GET /v1/state, a
5 MB attachment upload, and a 3.9 MB download. It needs macOS, the Xcode Command Line Tools, and `npm ci` in relay/.

    python3 scripts/test_tunnel_loopback.py
"""
from pathlib import Path
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import pairing  # noqa: E402
import tunnel  # noqa: E402


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def wait(condition, seconds: float, message: str):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = condition()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(.2)
    raise AssertionError(message)


def coalescing_proxy(backend_port: int) -> int:
    """Forward each request to the backend, and answer with the whole response in one write and a close.

    The sidecar then reads the head, the body, and the end of the stream together, as from a fast backend.
    """
    listener = socket.create_server(('127.0.0.1', 0))

    def handle(connection):
        with connection:
            data = b''
            while b'\r\n\r\n' not in data:
                data += connection.recv(65536)
            head, _, body = data.partition(b'\r\n\r\n')
            length = next(int(line.split(b':', 1)[1]) for line in head.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            while len(body) < length:
                body += connection.recv(65536)
            with socket.create_connection(('127.0.0.1', backend_port)) as backend:
                backend.sendall(head + b'\r\n\r\n' + body)
                response = b''.join(iter(lambda: backend.recv(1 << 20), b''))
            connection.sendall(response)

    def serve():
        while True:
            connection, _ = listener.accept()
            threading.Thread(target=handle, args=(connection,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1]


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun') and (ROOT / 'relay/node_modules/miniflare').is_dir(),
                     'The loopback test needs macOS, the Xcode Command Line Tools, and npm ci in relay/.')
class TunnelLoopbackTests(unittest.TestCase):
    processes: list

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.temp.name) / 'host'
        cls.processes = []
        cls.client = Path(cls.temp.name) / 'tunnel-test-client'
        # The client runs the app's own transport, so this test also covers the iPhone end (R4).
        # It reads the link with the app's parser. DEBUG lets that parser accept the http relay on 127.0.0.1.
        sources = [ROOT / 'scripts/tunnel-test-client.swift', ROOT / 'ios/Coconut/TunnelConnection.swift',
                   ROOT / 'ios/Coconut/PairingSetup.swift', ROOT / 'ios/Tunnel/Noise.swift', ROOT / 'ios/Tunnel/TunnelFrames.swift']
        subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-O', '-D', 'DEBUG', *map(str, sources), '-o', str(cls.client)],
                       check=True)
        cls.sidecar = tunnel.build(cls.data)

    @classmethod
    def tearDownClass(cls):
        for process in cls.processes:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        cls.temp.cleanup()

    @classmethod
    def start(cls, command, log=None, **options):
        if log:
            with (Path(cls.temp.name) / log).open('w') as output:
                process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, **options)
        else:
            process = subprocess.Popen(command, **options)
        cls.processes.append(process)
        return process

    def status(self):
        return json.loads((self.data / 'tunnel/status.json').read_text())

    def run_client(self, payload=None, *, fails=False):
        link = pairing.link(payload or pairing.tunnel_payload(self.data))
        result = subprocess.run([str(self.client), link], capture_output=True, text=True, timeout=120)
        steps = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        self.assertEqual(result.returncode, 1 if fails else 0, f'{steps}\n{result.stderr}')
        return {step.get('step', 'failure'): step for step in steps}

    def peers(self):
        return json.loads((self.data / 'tunnel/peers.json').read_text())

    def upgrade(self, role, authorization):
        """Open a relay WebSocket with a raw HTTP upgrade, and return the socket after the 101 answer."""
        connection = socket.create_connection(('127.0.0.1', int(self.url.rsplit(':', 1)[1])))
        connection.sendall((f'GET /v1/tunnels/{self.route["route"]}/{role} HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                            'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                            f'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nAuthorization: {authorization}\r\n\r\n').encode())
        self.assertIn(b' 101 ', connection.recv(1024))
        return connection

    def test_pairing_state_and_large_bodies_pass_through_the_tunnel(self):
        relay = self.start(['node', 'test/tunnel-server.mjs', '0'], cwd=ROOT / 'relay', stdout=subprocess.PIPE, text=True)
        url = self.url = relay.stdout.readline().strip()
        relay.stdout.close()
        self.assertTrue(url.startswith('http://127.0.0.1:'), url)
        port, tunnel_port = free_port(), free_port()
        self.start([sys.executable, '-m', 'coconut', '--data-dir', str(self.data), '--port', str(port),
                    '--tunnel-port', str(tunnel_port), '--provider', 'fixture'], log='backend.log', cwd=ROOT / 'backend')
        wait(lambda: urllib.request.urlopen(f'http://127.0.0.1:{tunnel_port}/health', timeout=1).status == 200, 30,
             'The backend did not start.')

        route = self.route = json.loads(subprocess.run([str(self.sidecar), 'setup', '--data-dir', str(self.data), '--relay', url],
                                          check=True, capture_output=True, text=True).stdout)
        for name in ('identity.key', 'route.json'):
            self.assertEqual((self.data / 'tunnel' / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(route['relay'], url)
        sidecar = [str(self.sidecar), 'run', '--data-dir', str(self.data), '--backend', f'127.0.0.1:{coalescing_proxy(tunnel_port)}']
        running = self.start(sidecar, log='tunnel.log')
        wait(lambda: self.status()['state'] == 'connected', 20, 'The sidecar did not connect to the relay.')

        steps = self.run_client()
        self.assertEqual(steps['state']['status'], 200)
        self.assertGreater(steps['upload']['bytes'], 5_000_000)
        self.assertEqual(steps['download']['bytes'], 3_900_000)
        self.assertIn('closed', steps['other key']['result'])
        self.assertIn('closed', steps['header injection']['result'])
        self.assertEqual(steps['spent ticket']['result'], 'refused')
        self.assertEqual(list((self.data / 'tunnel/tickets').glob('*.json')), [])
        self.assertEqual(len(self.peers()), 1)
        print(json.dumps(steps, indent=1), file=sys.stderr)

        # Another host socket replaces the sidecar's socket. The sidecar connects again, and a new link pairs.
        before = self.status()['connected_since']
        host_token = json.loads((self.data / 'tunnel/route.json').read_text())['host_token']
        host = self.upgrade('host', f'Bearer {host_token}')
        wait(lambda: self.status()['state'] == 'connected' and self.status()['connected_since'] != before, 30,
             'The sidecar did not reconnect.')
        host.close()
        # The sidecar records the cause from the relay's close frame, and its event log holds no secret.
        last = wait(lambda: self.status().get('last_disconnect'), 10, 'The status has no last disconnect.')
        self.assertEqual((last['cause'], last['code'], last['reason']), ('relay_closed', 1000, 'replaced'))
        self.assertEqual(self.status()['reconnects'], 1)
        # A queue writes the log after the status, so wait for the second connected event.
        expected = {'start', 'connect_start', 'channel_open', 'disconnected', 'backoff'}

        def logged():
            text = (self.data / 'tunnel/events.log').read_text()
            events = [json.loads(line)['event'] for line in text.splitlines()]
            return text if expected <= set(events) and events.count('connected') >= 2 else None

        log = wait(logged, 10, 'The sidecar did not log the reconnect.')
        self.assertEqual((self.data / 'tunnel/events.log').stat().st_mode & 0o777, 0o600)
        for secret in (host_token, route['route'], route['mac_key']):
            self.assertNotIn(secret, log)
        self.assertEqual(self.run_client()['state']['status'], 200)
        self.assertEqual(len(self.peers()), 2)

        # When the sidecar cannot save the phone key, the phone gets an error, not a pairing that a restart would lose.
        (self.data / 'tunnel').chmod(0o500)
        try:
            failure = self.run_client(fails=True)['failure']['failure']
        finally:
            (self.data / 'tunnel').chmod(0o700)
        self.assertIn('backendDown', failure)
        self.assertEqual(len(self.peers()), 2)

        # The relay holds 8 tickets. A ninth link waits on the Mac, and goes to the relay when a ticket is used.
        payloads = [pairing.tunnel_payload(self.data) for _ in range(8)]
        time.sleep(1.1)
        payloads.append(pairing.tunnel_payload(self.data))
        time.sleep(1)
        ticket = pairing.hkdf(pairing.unpadded(payloads[0]['secret'], 32), 'coconut pairing ticket').hex()
        self.upgrade('client', f'Ticket {ticket}').close()
        time.sleep(1)
        self.assertEqual(self.run_client(payloads[-1])['state']['status'], 200)
        self.assertEqual(len(self.peers()), 3)

        # Cloudflare resets the host socket from time to time. Here the sidecar stops without a close frame during a
        # session, and a new sidecar starts a second later. The phone's next request waits in the relay for it.
        link = pairing.link(pairing.tunnel_payload(self.data))
        errors = (Path(self.temp.name) / 'reset-client.err').open('w+')
        self.addCleanup(errors.close)
        client = subprocess.Popen([str(self.client), link], stdout=subprocess.PIPE, stderr=errors, text=True,
                                  env={**os.environ, 'COCONUT_TEST_HOST_RESET': '1'})
        self.processes.append(client)
        # A thread reads the output, so every wait below has a deadline, also when the client stalls.
        output = queue.Queue()
        threading.Thread(target=lambda: [*map(output.put, client.stdout), output.put(None)], daemon=True).start()
        lines = []
        deadline = time.monotonic() + 120
        while True:
            try:
                line = output.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                client.kill()
                errors.seek(0)
                self.fail(f'The reset client did not finish in 120 seconds.\n{"".join(lines)}{errors.read()}')
            if line is None:
                break
            lines.append(line)
            if '"before reset"' in line:
                running.kill()
                running.wait(10)
                time.sleep(1)
                running = self.start(sidecar, log='tunnel-after-reset.log')
        try:
            code = client.wait(10)
        except subprocess.TimeoutExpired:
            client.kill()
            code = None
        errors.seek(0)
        self.assertEqual(code, 0, ''.join(lines) + errors.read())
        reset = next(json.loads(line) for line in lines if '"host reset"' in line)
        self.assertEqual(reset['status'], 200)
        self.assertLess(reset['seconds'], 6)
        print(json.dumps(reset), file=sys.stderr)


if __name__ == '__main__':
    unittest.main()
