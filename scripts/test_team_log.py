#!/usr/bin/env python3
"""End-to-end test of the team log: three Coconut services, the room routes of the relay in Miniflare, and the helper.

Each service has its own data folder, as three Macs. The first makes a room and an invite, and the second joins with
the link. The test then writes to the relay as a member would, to show what the other Mac does with each entry.
It needs macOS, the Xcode Command Line Tools, and `npm ci` in relay/.

    python3 scripts/test_team_log.py

scripts/test_tunnel_loopback.py loads this test too, so the Mac app job of CI runs it after its `npm ci`.
"""
from pathlib import Path
import http.client
import json
import os
import secrets
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
sys.path.insert(0, str(ROOT / 'backend'))

import team  # noqa: E402
from coconut.team_helper import TeamHelper  # noqa: E402
from coconut.team_relay import TeamRelay, TeamRelayError  # noqa: E402


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class Mac:
    """One Coconut service with its own data folder, and the calls of a tool on that Mac."""

    def __init__(self, folder: Path):
        self.data = folder
        self.port = free_port()

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        token = json.loads((self.data / 'gateway/bridge.json').read_text())['token']
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=90)
        try:
            headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def rooms(self, *, sync: bool = True) -> list[dict]:
        status, answer = self.call('POST', '/v1/team/sync', {}) if sync else self.call('GET', '/v1/team/rooms')
        assert status == 200 and answer.get('complete', True), answer
        return answer['rooms']

    def keys(self, room: str) -> dict:
        """The key file of a room. The test uses it to write to the relay as this Mac."""
        return json.loads((self.data / f'team/{room}.json').read_text())


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun') and shutil.which('node')
                     and (ROOT / 'relay/node_modules/miniflare').is_dir(),
                     'The team log test needs macOS, the Xcode Command Line Tools, Node, and npm ci in relay/.')
class TeamLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        folder = Path(cls.temp.name)
        cls.processes = []
        # DEBUG lets the link code accept the http relay on 127.0.0.1, as for the client of the tunnel test.
        cls.helper_path = folder / 'team-helper'
        subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-O', '-D', 'DEBUG', *map(str, team.SOURCES),
                        '-o', str(cls.helper_path)], check=True)
        relay = subprocess.Popen(['node', 'test/room-server.mjs', '0'], cwd=ROOT / 'relay', stdout=subprocess.PIPE, text=True)
        cls.processes.append(relay)
        cls.url = relay.stdout.readline().strip()
        relay.stdout.close()
        assert cls.url.startswith('http://127.0.0.1:'), cls.url
        cls.macs = [Mac(folder / name) for name in ('first', 'second', 'third')]
        environment = dict(os.environ, COCONUT_TEAM_HELPER=str(cls.helper_path))
        for mac in cls.macs:
            with (folder / f'{mac.data.name}.log').open('w') as output:
                cls.processes.append(subprocess.Popen(
                    [sys.executable, '-m', 'coconut', '--data-dir', str(mac.data), '--port', str(mac.port),
                     '--provider', 'fixture'], cwd=ROOT / 'backend', env=environment, stdout=output, stderr=subprocess.STDOUT))
        for mac in cls.macs:
            cls.wait_for_health(mac)
        os.environ['COCONUT_TEAM_HELPER'] = str(cls.helper_path)
        cls.helper = TeamHelper(folder)
        cls.relay = TeamRelay()

    @classmethod
    def wait_for_health(cls, mac: Mac) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if urllib.request.urlopen(f'http://127.0.0.1:{mac.port}/health', timeout=1).status == 200:
                    return
            except OSError:
                time.sleep(.2)
        raise AssertionError(f'The service of the {mac.data.name} Mac did not start.')

    @classmethod
    def tearDownClass(cls):
        os.environ.pop('COCONUT_TEAM_HELPER', None)
        for process in cls.processes:
            process.terminate()
        for process in cls.processes:
            try:
                process.wait(30)
            except subprocess.TimeoutExpired:
                process.kill()
        cls.temp.cleanup()

    def log(self, mac: Mac) -> str:
        return (Path(self.temp.name) / f'{mac.data.name}.log').read_text()

    def write(self, mac: Mac, room: str, entry: str, fields: dict, *, kept: bool, signer: str | None = None) -> dict:
        """Write an entry to the relay with the token of a Mac. `signer` is another member key for its signature."""
        keys = mac.keys(room)
        body = self.helper.seal(room, entry, 0, keys['keys']['0'], signer or keys['member_key'],
                                json.dumps(dict(fields, at='2026-11-10T12:00:00Z')).encode())
        return self.relay.write(self.url, room, keys['token'], entry, kept, body)

    def names(self, room: dict) -> list[str]:
        return [member['name'] for member in room['members']]

    def test_two_macs_share_one_log(self):
        first, second, third = self.macs

        # The first Mac makes a room. It is the admin, and the log shows it.
        status, answer = first.call('POST', '/v1/team/rooms', {'name': 'Example Team', 'member_name': 'Ada', 'relay': self.url})
        self.assertEqual(status, 200, answer)
        room = answer['room']
        room_id = room['room']
        self.assertEqual((room['state'], room['is_admin'], room['name'], self.names(room)), ('active', True, 'Example Team', ['Ada']))
        self.assertEqual((first.data / 'team').stat().st_mode & 0o777, 0o700)
        self.assertEqual((first.data / f'team/{room_id}.json').stat().st_mode & 0o777, 0o600)
        # No answer of a tool route has a key or a token of the room.
        secrets_of_first = first.keys(room_id)
        for secret in (secrets_of_first['token'], secrets_of_first['member_key'], secrets_of_first['keys']['0']):
            self.assertNotIn(secret, json.dumps(answer))

        # A Mac that is not in the room cannot make an invite, and the admin can.
        self.assertEqual(second.call('POST', f'/v1/team/rooms/{room_id}/invites', {})[0], 404)
        status, invite = first.call('POST', f'/v1/team/rooms/{room_id}/invites', {'uses': 1})
        self.assertEqual(status, 200, invite)
        self.assertTrue(invite['link'].startswith('coconut://team/'), invite)

        # The second Mac joins with the link, and each Mac sees the `member` entry of the other.
        status, answer = second.call('POST', '/v1/team/join', {'link': invite['link'], 'member_name': 'Grace'})
        self.assertEqual(status, 200, answer)
        joined = answer['room']
        self.assertEqual((joined['state'], joined['name'], joined['admin'], joined['is_admin']),
                         ('active', 'Example Team', room['member'], False))
        self.assertEqual((second.data / f'team/{room_id}.json').stat().st_mode & 0o777, 0o600)
        for mac in (first, second):
            view = mac.rooms()[0]
            self.assertEqual(self.names(view), ['Ada', 'Grace'], mac.data.name)
            self.assertEqual((view['sync']['seq'], view['sync']['entries'], view['sync']['error']), (2, 2, None))
        self.assertEqual([member['admin'] for member in second.rooms()[0]['members']], [True, False])

        # A second join with the used ticket fails, and the third Mac keeps nothing of the room.
        status, answer = third.call('POST', '/v1/team/join', {'link': invite['link'], 'member_name': 'Edsger'})
        self.assertEqual(status, 409, answer)
        self.assertIn('invite', answer['error'])
        self.assertEqual(third.rooms(sync=False), [])
        self.assertFalse((third.data / f'team/{room_id}.json').exists())
        self.assertEqual(len(self.relay.members(self.url, room_id, secrets_of_first['token'])), 2)

        # The first Mac replaces its `member` entry. The second Mac takes the write with the higher `rev`.
        agreement = next(member['agreement'] for member in first.rooms(sync=False)[0]['members'] if member['this_mac'])
        own_entry = self.own_entry(first, room_id)
        replaced = {'kind': 'member', 'name': 'Ada L.', 'avatar': '', 'color': '', 'agreement': agreement}
        position = self.write(first, room_id, own_entry, dict(replaced, rev=2), kept=True)
        self.assertEqual((position['seq'], position['first']), (3, 1))
        for mac in (first, second):
            self.assertEqual(self.names(mac.rooms()[0]), ['Ada L.', 'Grace'], mac.data.name)
            # The entry keeps its place in the log: `first` stays, and `seq` is the one of the new write.
            self.assertEqual(self.row(mac, room_id, own_entry), (3, 1, 2), mac.data.name)
        # A write of the same entry without a higher `rev` does not replace it.
        self.write(first, room_id, own_entry, dict(replaced, name='Ada of before', rev=2), kept=True)
        for mac in (first, second):
            self.assertEqual(self.names(mac.rooms()[0]), ['Ada L.', 'Grace'], mac.data.name)
            self.assertEqual(self.row(mac, room_id, own_entry), (3, 1, 2), mac.data.name)
            self.assertIn(f'refused the entry {own_entry} at seq 4', self.log(mac))

        # A body that a third key signed. The relay takes it from the second Mac, and the first Mac drops it.
        outsider = self.helper.member()
        forged = secrets.token_hex(16)
        self.write(second, room_id, forged, {'kind': 'member', 'rev': 1, 'name': 'Mallory', 'avatar': '', 'color': '',
                                             'agreement': outsider['agreement']}, kept=False, signer=outsider['member_key'])
        view = first.rooms()[0]
        self.assertEqual((self.names(view), view['sync']['seq'], view['sync']['entries']), (['Ada L.', 'Grace'], 5, 2))
        self.assertIsNone(self.row(first, room_id, forged))
        self.assertIn(f'dropped the entry {forged} at seq 5', self.log(first))
        # An entry of a kind that this version does not know stays in the store, and it changes no member.
        unknown = secrets.token_hex(16)
        self.write(second, room_id, unknown, {'kind': 'later', 'rev': 1, 'text': 'for a later version'}, kept=False)
        view = first.rooms()[0]
        self.assertEqual((self.names(view), view['sync']['entries']), (['Ada L.', 'Grace'], 3))

        # The admin cannot leave a room that has another member.
        status, answer = first.call('DELETE', f'/v1/team/rooms/{room_id}')
        self.assertEqual(status, 409, answer)
        # The second Mac leaves. It can no longer read, it keeps nothing, and the first Mac no longer shows it.
        secrets_of_second = second.keys(room_id)
        self.assertEqual(second.call('DELETE', f'/v1/team/rooms/{room_id}'), (200, {'room': room_id, 'left': True}))
        with self.assertRaises(TeamRelayError) as refused:
            self.relay.read(self.url, room_id, secrets_of_second['token'], 0)
        self.assertEqual(refused.exception.status, 401)
        self.assertEqual(second.rooms(), [])
        self.assertFalse((second.data / f'team/{room_id}.json').exists())
        self.assertEqual(self.names(first.rooms()[0]), ['Ada L.'])

        # The admin leaves the room without other members, and the relay deletes the room.
        self.assertEqual(first.call('DELETE', f'/v1/team/rooms/{room_id}')[0], 200)
        self.assertEqual(first.rooms(), [])
        with self.assertRaises(TeamRelayError) as missing:
            self.relay.read(self.url, room_id, secrets_of_first['token'], 0)
        self.assertEqual(missing.exception.status, 404)
        # No log of a service has a secret of a room.
        for mac in self.macs:
            for secret in (*secrets_of_first['keys'].values(), secrets_of_first['member_key'], secrets_of_first['token'],
                           secrets_of_second['member_key'], secrets_of_second['token'], invite['link']):
                self.assertNotIn(secret, self.log(mac))

    def database(self, mac: Mac):
        import sqlite3
        return sqlite3.connect(f'file:{mac.data / "coconut.sqlite3"}?mode=ro', uri=True)

    def own_entry(self, mac: Mac, room: str) -> str:
        """The entry ID of the `member` entry that this Mac wrote."""
        with self.database(mac) as database:
            return json.loads(database.execute('SELECT profile FROM team_rooms WHERE room=?', (room,)).fetchone()[0])['entry']

    def row(self, mac: Mac, room: str, entry: str):
        """The seq, first and rev of an entry in the store of a Mac, or None."""
        with self.database(mac) as database:
            return database.execute('SELECT seq, first, rev FROM team_entries WHERE room=? AND id=?', (room, entry)).fetchone()


if __name__ == '__main__':
    unittest.main()
