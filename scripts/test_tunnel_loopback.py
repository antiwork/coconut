#!/usr/bin/env python3
"""Loopback test of the relay transport: test client, Miniflare relay, tunnel sidecar, and backend on this Mac.

The test client (scripts/tunnel-test-client.swift) pairs with a version 2 link, then runs a real GET /v1/state, a
5 MB attachment upload, and a 3.9 MB download. It needs macOS, the Xcode Command Line Tools, and `npm ci` in relay/.

    python3 scripts/test_tunnel_loopback.py
"""
from pathlib import Path
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import pairing  # noqa: E402
from push import compile_swift  # noqa: E402
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
        compile_swift([ROOT / 'scripts/tunnel-test-client.swift', *tunnel.SOURCES[1:]], cls.client,
                      Path(cls.temp.name) / 'client.sha256')
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

    def run_client(self):
        link = pairing.link(pairing.tunnel_payload(self.data))
        result = subprocess.run([str(self.client), link], capture_output=True, text=True, timeout=120)
        steps = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        self.assertEqual(result.returncode, 0, f'{steps}\n{result.stderr}')
        return {step['step']: step for step in steps}

    def test_pairing_state_and_large_bodies_pass_through_the_tunnel(self):
        relay = self.start(['node', 'test/tunnel-server.mjs', '0'], cwd=ROOT / 'relay', stdout=subprocess.PIPE, text=True)
        url = relay.stdout.readline().strip()
        relay.stdout.close()
        self.assertTrue(url.startswith('http://127.0.0.1:'), url)
        port, tunnel_port = free_port(), free_port()
        self.start([sys.executable, '-m', 'coconut', '--data-dir', str(self.data), '--host', '127.0.0.1', '--port', str(port),
                    '--tunnel-port', str(tunnel_port), '--provider', 'fixture'], log='backend.log', cwd=ROOT / 'backend')
        wait(lambda: urllib.request.urlopen(f'http://127.0.0.1:{tunnel_port}/health', timeout=1).status == 200, 30,
             'The backend did not start.')

        route = json.loads(subprocess.run([str(self.sidecar), 'setup', '--data-dir', str(self.data), '--relay', url],
                                          check=True, capture_output=True, text=True).stdout)
        for name in ('identity.key', 'route.json'):
            self.assertEqual((self.data / 'tunnel' / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(route['relay'], url)
        self.start([str(self.sidecar), 'run', '--data-dir', str(self.data), '--backend', f'127.0.0.1:{tunnel_port}'], log='tunnel.log')
        wait(lambda: self.status()['state'] == 'connected', 20, 'The sidecar did not connect to the relay.')

        steps = self.run_client()
        self.assertEqual(steps['state']['status'], 200)
        self.assertGreater(steps['upload']['bytes'], 5_000_000)
        self.assertEqual(steps['download']['bytes'], 3_900_000)
        self.assertIn('closed', steps['other key']['result'])
        self.assertIn('closed', steps['header injection']['result'])
        self.assertEqual(steps['spent ticket']['result'], 'refused')
        self.assertEqual(list((self.data / 'tunnel/tickets').glob('*.json')), [])
        self.assertEqual(len(json.loads((self.data / 'tunnel/peers.json').read_text())), 1)
        print(json.dumps(steps, indent=1), file=sys.stderr)

        # Another host socket replaces the sidecar's socket. The sidecar connects again, and a new link pairs.
        before = self.status()['connected_since']
        host = socket.create_connection(('127.0.0.1', int(url.rsplit(':', 1)[1])))
        host.sendall((f'GET /v1/tunnels/{route["route"]}/host HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n'
                      'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
                      f'Authorization: Bearer {json.loads((self.data / "tunnel/route.json").read_text())["host_token"]}\r\n\r\n').encode())
        self.assertIn(b' 101 ', host.recv(1024))
        wait(lambda: self.status()['state'] == 'connected' and self.status()['connected_since'] != before, 30,
             'The sidecar did not reconnect.')
        host.close()
        self.assertEqual(self.run_client()['state']['status'], 200)
        self.assertEqual(len(json.loads((self.data / 'tunnel/peers.json').read_text())), 2)


if __name__ == '__main__':
    unittest.main()
