#!/usr/bin/env python3
"""End-to-end test of the team log: three Coconut services, the room routes of the relay in Miniflare, and the helper.

Each service has its own data folder, as three Macs. The first makes a room and an invite, and the second joins with
the link. The test then writes to the relay as a member would, to show what the other Mac does with each entry.
It needs macOS, the Xcode Command Line Tools, and `npm ci` in relay/.

    python3 scripts/test_team_log.py

scripts/test_tunnel_loopback.py loads this test too, so the Mac app job of CI runs it after its `npm ci`.
TeamLogVectorTests checks the test vectors of docs/team-log.md with the real helper.
"""
from __future__ import annotations

import base64
from pathlib import Path
import http.client
import json
import os
import secrets
import shutil
import signal
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
from coconut.team_helper import TeamFormatError, TeamHelper  # noqa: E402
from coconut.team_relay import TeamRelay, TeamRelayError  # noqa: E402


def compile_helper(output: Path) -> None:
    # DEBUG lets the link code accept the http relay on 127.0.0.1, as for the client of the tunnel test.
    subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-O', '-D', 'DEBUG', *map(str, team.SOURCES),
                    '-o', str(output)], check=True)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def start_failure(message: str, process: subprocess.Popen, log: Path) -> AssertionError:
    """The error for a service that did not answer /health, with its exit status and the end of its log.

    CI shows only this error. The service runs with -X faulthandler, so SIGABRT writes the stack of each thread into
    the log before the process stops. The log of a test service has no secret: its provider is the fixture.
    """
    status = process.poll()
    if status is None:
        process.send_signal(signal.SIGABRT)
        try:
            status = f'{process.wait(10)} after SIGABRT, so the process was still running'
        except subprocess.TimeoutExpired:
            status = 'none, the process still runs 10 seconds after SIGABRT'
    text = log.read_text(errors='replace') if log.exists() else '(no log file)'
    return AssertionError(f'{message} Exit status: {status}. The end of {log.name}:\n{text[-6000:]}')


class Mac:
    """One Coconut service with its own data folder, and the calls of a tool on that Mac."""

    def __init__(self, folder: Path):
        self.data = folder
        self.port = free_port()
        self.log = folder.with_name(f'{folder.name}.log')
        self.process = None
        self.device_token = None

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        token = json.loads((self.data / 'gateway/bridge.json').read_text())['token']
        return self.request(method, path, body, {'Authorization': f'Bearer {token}'})

    def phone(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        """A request of a paired phone on the main listener. The first call pairs with a 10-digit code, as a tool on
        this Mac does."""
        if self.device_token is None:
            from coconut.server import create_pairing_code
            status, answer = self.request('POST', '/v1/pair', {'code': create_pairing_code(self.data),
                                                               'device_name': 'Test phone'})
            assert status == 200, answer
            self.device_token = answer['token']
        return self.request(method, path, body, {'Authorization': f'Bearer {self.device_token}',
                                                 'Idempotency-Key': secrets.token_hex(16)})

    def request(self, method: str, path: str, body: dict | None, headers: dict | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=90)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None,
                               dict(headers or {}, **{'Content-Type': 'application/json'}))
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
        cls.helper_path = folder / 'team-helper'
        compile_helper(cls.helper_path)
        relay = subprocess.Popen(['node', 'test/room-server.mjs', '0'], cwd=ROOT / 'relay', stdout=subprocess.PIPE, text=True)
        cls.processes.append(relay)
        cls.url = relay.stdout.readline().strip()
        relay.stdout.close()
        assert cls.url.startswith('http://127.0.0.1:'), cls.url
        cls.macs = [Mac(folder / name) for name in ('first', 'second', 'third')]
        environment = dict(os.environ, COCONUT_TEAM_HELPER=str(cls.helper_path))
        for mac in cls.macs:
            with mac.log.open('w') as output:
                mac.process = subprocess.Popen(
                    [sys.executable, '-X', 'faulthandler', '-m', 'coconut', '--data-dir', str(mac.data), '--port',
                     str(mac.port), '--provider', 'fixture'], cwd=ROOT / 'backend', env=environment, stdout=output,
                    stderr=subprocess.STDOUT)
                cls.processes.append(mac.process)
        try:
            for mac in cls.macs:
                cls.wait_for_health(mac)
        except AssertionError:
            # unittest does not call tearDownClass when setUpClass fails, so stop the relay and the services here.
            cls.tearDownClass()
            raise
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
        raise start_failure(f'The service of the {mac.data.name} Mac did not start.', mac.process, mac.log)

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
        return mac.log.read_text()

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
        # No answer has the public agreement key, so the test reads it from the store.
        with self.database(first) as database:
            agreement = database.execute('SELECT agreement FROM team_members WHERE room=? AND member=?',
                                         (room_id, room['member'])).fetchone()[0]
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

    def test_a_mac_shares_a_bot_and_its_page_with_the_team(self):
        first, second, _ = self.macs
        # The second Mac makes the room, so the first Mac can leave it at the end.
        status, answer = second.call('POST', '/v1/team/rooms', {'name': 'Example Team', 'member_name': 'Grace',
                                                                'relay': self.url})
        self.assertEqual(status, 200, answer)
        room_id = answer['room']['room']
        link = second.call('POST', f'/v1/team/rooms/{room_id}/invites', {})[1]['link']
        self.assertEqual(first.call('POST', '/v1/team/join', {'link': link, 'member_name': 'Ada'})[0], 200)

        # The first Mac runs a bot, shares it, and its tooling saves a page for the team.
        status, bot = first.phone('POST', '/v1/bots', {'name': 'Reporter'})
        self.assertEqual(status, 200, bot)
        status, answer = first.call('POST', f'/v1/team/rooms/{room_id}/bots', {'bot_id': bot['id']})
        self.assertEqual((status, [row['name'] for row in answer['room']['bots']]), (200, ['Reporter']), answer)
        with self.database(first) as database:
            home = database.execute('SELECT home FROM profiles WHERE bot_id=?', (bot['id'],)).fetchone()[0]
        page = {'home': home, 'key': 'weekly-report', 'title': 'Weekly report', 'format': 'markdown',
                'content': '3 tasks are open.', 'audience': 'team'}
        status, answer = first.call('POST', '/v1/gateway/document', page)
        self.assertEqual((status, answer['version']), (200, 1), answer)

        # The second Mac lists the page with the fields of a local document, opens it and pins it.
        listed = self.team_library(first, second)
        (shared,) = listed['documents']
        (team_bot,) = listed['team_bots']
        self.assertEqual((shared['title'], shared['version'], shared['bot_id'], shared['team']['name']),
                         ('Weekly report', 1, team_bot['id'], 'Example Team'))
        self.assertEqual((team_bot['name'], team_bot['member']['name']), ('Reporter', 'Ada'))
        for field, kind in (('id', str), ('bot_id', str), ('key', str), ('title', str), ('format', str),
                            ('version', int), ('created_at', str), ('updated_at', str)):
            self.assertIsInstance(shared[field], kind, field)
        status, opened = second.phone('GET', f'/v1/documents/{shared["id"]}')
        self.assertEqual((status, opened.get('content')), (200, '3 tasks are open.'), opened)
        status, pinned = second.phone('PATCH', f'/v1/documents/{shared["id"]}', {'pinned': True})
        self.assertEqual((status, pinned.get('pinned')), (200, True), pinned)
        status, answer = second.phone('DELETE', f'/v1/documents/{shared["id"]}', {})
        self.assertEqual(status, 403, answer)
        # The first Mac shows its own page once, as a local document.
        status, local = first.phone('GET', '/v1/documents')
        self.assertEqual(([row['audience'] for row in local['documents']], local['team_bots']), (['team'], []))

        # A new version reaches the second Mac, and the pin stays.
        self.assertEqual(first.call('POST', '/v1/gateway/document', dict(page, content='4 tasks are open.'))[0], 200)
        (shared,) = self.team_library(first, second)['documents']
        self.assertEqual((shared['version'], shared['pinned']), (2, True))
        self.assertEqual(second.phone('GET', f'/v1/documents/{shared["id"]}')[1]['content'], '4 tasks are open.')

        # A private page goes from the second Mac. The bot stays.
        self.assertEqual(first.call('POST', '/v1/gateway/document', dict(page, audience='private'))[0], 200)
        listed = self.team_library(first, second)
        self.assertEqual((listed['documents'], [row['name'] for row in listed['team_bots']]), ([], ['Reporter']))

        # The first Mac leaves the room, and its bot and its pages go from the second Mac.
        self.assertEqual(first.call('POST', '/v1/gateway/document', page)[0], 200)
        self.assertEqual(len(self.team_library(first, second)['documents']), 1)
        self.assertEqual(first.call('DELETE', f'/v1/team/rooms/{room_id}')[0], 200)
        self.assertEqual(self.team_library(first, second), {'documents': [], 'team_bots': []})
        self.assertEqual(second.call('DELETE', f'/v1/team/rooms/{room_id}')[0], 200)
        for mac in (first, second):
            self.assertNotIn('coconut.team_pages', self.log(mac))

    def test_the_app_makes_a_team_and_a_second_mac_joins_it(self):
        first, second, _ = self.macs
        # The Team screen of a paired phone calls the routes with the token of its device.
        status, answer = first.phone('POST', '/v1/team/rooms', {'name': 'Example Team', 'member_name': 'Ada',
                                                                'relay': self.url})
        self.assertEqual(status, 200, answer)
        room_id = answer['room']['room']
        self.assertEqual((answer['room']['is_admin'], answer['room']['can_invite']), (True, True))
        status, invite = first.phone('POST', f'/v1/team/rooms/{room_id}/invites', {})
        self.assertEqual(status, 200, invite)
        cursor = second.phone('GET', '/v1/updates')[1]['cursor']
        status, answer = second.phone('POST', '/v1/team/join', {'link': invite['link'], 'member_name': 'Grace'})
        self.assertEqual((status, answer['room']['can_invite']), (200, False), answer)
        self.assertTrue(second.phone('GET', f'/v1/updates?after={cursor}')[1]['team_changed'])

        # The first Mac adds a bot, and each Mac lists both members and the bot.
        status, bot = first.phone('POST', '/v1/bots', {'name': 'Reporter'})
        self.assertEqual(status, 200, bot)
        status, answer = first.phone('POST', f'/v1/team/rooms/{room_id}/bots', {'bot_id': bot['id']})
        self.assertEqual((status, [(row['name'], row['this_mac'], row['bot_id']) for row in answer['room']['bots']]),
                         (200, [('Reporter', True, bot['id'])]), answer)
        # The pass of the first Mac writes the bot entry, and the pass of the second Mac reads it.
        views = {mac: self.team_view(mac, room_id) for mac in (first, second)}
        for mac, view in views.items():
            self.assertEqual([(member['name'], member['this_mac']) for member in view['members']],
                             [('Ada', mac is first), ('Grace', mac is second)], mac.data.name)
            self.assertEqual([(row['name'], row['member_name'], row['this_mac']) for row in view['bots']],
                             [('Reporter', 'Ada', mac is first)], mac.data.name)
        self.assertEqual(views[first]['bots'][0]['id'], views[second]['bots'][0]['id'])

        # The first Mac takes a name with an emoji sequence, and the second Mac shows it after the real seal.
        name = 'Ada \U0001f469\u200d\U0001f4bb'
        status, answer = first.phone('PATCH', f'/v1/team/rooms/{room_id}', {'member_name': name})
        self.assertEqual(status, 200, answer)
        self.assertEqual([member['name'] for member in self.team_view(second, room_id)['members']], [name, 'Grace'])
        self.assertEqual(self.team_view(second, room_id)['bots'][0]['member_name'], name)

        # The second Mac leaves, then the admin leaves the room without other members.
        self.assertEqual(second.phone('DELETE', f'/v1/team/rooms/{room_id}', {}), (200, {'room': room_id, 'left': True}))
        self.assertEqual([member['name'] for member in self.team_view(first, room_id)['members']], [name])
        self.assertEqual(first.phone('DELETE', f'/v1/team/rooms/{room_id}', {}), (200, {'room': room_id, 'left': True}))
        for mac in (first, second):
            status, answer = mac.phone('GET', '/v1/team/rooms')
            self.assertEqual((status, [row['room'] for row in answer['rooms']]), (200, []))
            self.assertNotIn(invite['link'], self.log(mac))

    def test_two_macs_share_the_team_chat(self):
        first, second, _ = self.macs
        status, answer = first.phone('POST', '/v1/team/rooms', {'name': 'Example Team', 'member_name': 'Ada',
                                                                'relay': self.url})
        self.assertEqual(status, 200, answer)
        room_id = answer['room']['room']
        link = first.phone('POST', f'/v1/team/rooms/{room_id}/invites', {})[1]['link']
        self.assertEqual(second.phone('POST', '/v1/team/join', {'link': link, 'member_name': 'Grace'})[0], 200)
        messages = f'/v1/team/rooms/{room_id}/messages'

        # Ada sends, and Grace reads it after a sync of her Mac.
        status, answer = first.phone('POST', messages, {'text': 'Hello **Grace**'})
        self.assertEqual(status, 200, answer)
        hello = answer['message']
        view = self.team_view(second, room_id)
        self.assertEqual((view['unread'], view['last_message']['text'], view['last_message']['member_name']),
                         (1, 'Hello **Grace**', 'Ada'))
        (received,) = second.phone('GET', messages)[1]['messages']
        self.assertEqual((received['id'], received['first'], received['this_mac']), (hello['id'], hello['first'], False))
        self.assertEqual(second.phone('POST', f'/v1/team/rooms/{room_id}/read', {'first': hello['first']})[1],
                         {'room': room_id, 'read': hello['first'], 'unread': 0})

        # Grace answers. Ada sees the answer as unread, and reads it.
        status, answer = second.phone('POST', messages, {'text': 'Hello Ada', 'reply_to': hello['id']})
        self.assertEqual((status, answer['message']['reply_to']), (200, hello['id']), answer)
        reply = answer['message']
        self.assertEqual(self.team_view(first, room_id)['unread'], 1)
        self.assertEqual(first.phone('POST', f'/v1/team/rooms/{room_id}/read', {'first': reply['first']})[1]['unread'], 0)

        # Ada edits, then deletes her message. Each Mac ends with the same chat.
        status, answer = first.phone('PATCH', f'{messages}/{hello["id"]}', {'text': 'Hello, Grace'})
        self.assertEqual((status, answer['message']['first']), (200, hello['first']), answer)
        self.team_view(second, room_id)
        (edited,) = [row for row in second.phone('GET', messages)[1]['messages'] if row['id'] == hello['id']]
        self.assertEqual(edited['text'], 'Hello, Grace')
        self.assertIsNotNone(edited['edited_at'])
        self.assertEqual(first.phone('DELETE', f'{messages}/{hello["id"]}', {}),
                         (200, {'room': room_id, 'id': hello['id'], 'deleted': True}))
        chats = []
        for mac in (first, second):
            view = self.team_view(mac, room_id)
            self.assertEqual(view['unread'], 0, mac.data.name)
            page = mac.phone('GET', messages)[1]
            chats.append([{name: value for name, value in row.items() if name != 'this_mac'} for row in page['messages']])
        self.assertEqual(chats[0], chats[1])
        self.assertEqual([(row['text'], row['reply_to'], row['member_name']) for row in chats[0]],
                         [('Hello Ada', hello['id'], 'Grace')])
        # The read position of each Mac stays, and a position from before does not move it back.
        self.assertEqual(second.phone('POST', f'/v1/team/rooms/{room_id}/read', {'first': 1})[1]['read'], hello['first'])
        self.assertEqual(first.phone('POST', f'/v1/team/rooms/{room_id}/read', {'first': 1})[1]['read'], reply['first'])

        self.assertEqual(second.phone('DELETE', f'/v1/team/rooms/{room_id}', {})[0], 200)
        self.assertEqual(first.phone('DELETE', f'/v1/team/rooms/{room_id}', {})[0], 200)
        # The text of a message is private, so no log line has it.
        for mac in (first, second):
            for text in ('Hello **Grace**', 'Hello, Grace', 'Hello Ada'):
                self.assertNotIn(text, self.log(mac))

    def test_a_mac_without_a_person_joins_with_the_coconut_tool(self):
        first, second, _ = self.macs
        status, answer = first.call('POST', '/v1/team/rooms', {'name': 'Example Team', 'member_name': 'Ada',
                                                               'relay': self.url})
        self.assertEqual(status, 200, answer)
        room_id = answer['room']['room']
        link = first.call('POST', f'/v1/team/rooms/{room_id}/invites', {})[1]['link']
        status, bot = second.phone('POST', '/v1/bots', {'name': 'Night Bot'})
        self.assertEqual(status, 200, bot)
        tool = self.coconut_tool()
        outputs = []

        def run(mac: Mac, *arguments: str, link_input: str = '') -> tuple[int, list[str]]:
            # The tool finds the data of a Mac in <install>/.runtime/host, so each Mac gets an install folder.
            install = Path(self.temp.name) / f'install-{mac.data.name}'
            if not (install / '.runtime/host').exists():
                (install / '.runtime').mkdir(parents=True)
                (install / '.runtime/host').symlink_to(mac.data)
            result = subprocess.run([str(tool), 'team', *arguments], input=link_input, capture_output=True, text=True,
                                    env={'PATH': os.environ['PATH'], 'HOME': self.temp.name,
                                         'COCONUT_INSTALL_DIR': str(install)}, timeout=240)
            outputs.append(result.stdout + result.stderr)
            return result.returncode, (result.stdout or result.stderr).splitlines()

        # The second Mac joins with the link on standard input, then joins again.
        code, lines = run(second, 'join', '--name', 'Grace', link_input=link + '\n')
        self.assertEqual((code, lines[:3]), (0, ['This Mac joined the team Example Team.', 'Team: Example Team',
                                                 f'ID: {room_id}']), lines)
        self.assertEqual(run(second, 'join', link_input=link), (0, ['This Mac is already in this team.']))
        # The link is never an argument, and the tool does not repeat it.
        self.assertEqual(run(second, 'join', link)[0], 64)

        # The second Mac adds its bot by its name, in another case.
        self.assertEqual(run(second, 'add-bot', 'night bot'), (0, ['The bot Night Bot is in the team Example Team now.']))
        self.assertEqual(run(second, 'add-bot', 'Day Bot')[0], 1)
        # A pass of the second Mac writes the bot entry, and a pass of the first Mac reads it.
        second.rooms()
        first.rooms()
        code, lines = run(first)
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines, ['Team: Example Team', f'ID: {room_id}', 'State: active', 'This Mac is the admin: yes',
                                 'Member: Ada (admin, this Mac)', 'Member: Grace', 'Bot: Night Bot (Mac of Grace)'])
        code, lines = run(second)
        self.assertEqual(lines, ['Team: Example Team', f'ID: {room_id}', 'State: active', 'This Mac is the admin: no',
                                 'Member: Ada (admin)', 'Member: Grace (this Mac)',
                                 f'Bot: Night Bot (this Mac, ID {bot["id"]})'])
        code, lines = run(second, '--json')
        self.assertEqual(json.loads('\n'.join(lines))['rooms'][0]['room'], room_id)

        # A leave needs the name of the team.
        self.assertEqual(run(second, 'leave')[0], 1)
        self.assertEqual(run(second, 'leave', '--confirm', 'Example Team'), (0, ['This Mac left the team Example Team.']))
        self.assertEqual(run(second), (0, ['This Mac is in no team.']))
        self.assertEqual(first.call('DELETE', f'/v1/team/rooms/{room_id}')[0], 200)
        # No output of the tool and no log of a service has the link.
        for text in outputs + [self.log(mac) for mac in self.macs]:
            self.assertNotIn(link, text)
        self.assertNotIn(link.removeprefix('coconut://team/'), ''.join(outputs))

    @staticmethod
    def coconut_tool() -> Path:
        """The `coconut` tool of the Swift package. The Mac app job of CI ran `swift test` before, so this build is quick."""
        command = ['swift', 'build', '--package-path', str(ROOT / 'mac'), '--product', 'CoconutTool']
        subprocess.run(command, check=True, capture_output=True)
        folder = subprocess.run([*command, '--show-bin-path'], check=True, capture_output=True, text=True).stdout.strip()
        return Path(folder) / 'CoconutTool'

    def team_view(self, mac: Mac, room_id: str) -> dict:
        """A pass of the Mac, started by the phone, then its room as the Team screen gets it."""
        status, answer = mac.phone('POST', '/v1/team/sync', {})
        self.assertTrue(status == 200 and answer['complete'], answer)
        (view,) = (row for row in answer['rooms'] if row['room'] == room_id)
        return view

    def team_library(self, writer: Mac, reader: Mac) -> dict:
        """A pass on the Mac that writes, one on the Mac that reads, then the Library of the reader."""
        writer.rooms()
        reader.rooms()
        status, answer = reader.phone('GET', '/v1/documents')
        self.assertEqual(status, 200, answer)
        return answer

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


def run_of(start: int, count: int) -> bytes:
    """The bytes from `start`, one higher each time, as the inputs of the test vectors name them."""
    return bytes(range(start, start + count))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# The inputs and the results of "Test vectors" in docs/team-log.md.
SECRET, ROOM, ENTRY, MEMBER_KEY, ROOM_KEY = (b64(run_of(0x00, 32)), run_of(0x20, 32).hex(), run_of(0x40, 16).hex(),
                                             b64(run_of(0x50, 32)), b64(run_of(0x70, 32)))
TOKEN = run_of(0xb0, 32).hex()
PAYLOAD = b'{"kind":"member","rev":1,"name":"Ada"}'
RELAY, NAME, EXPIRES = 'https://relay.example', 'Example Team', 1800000000
MEMBER = '3f7708d5f5cc2bc633b59d2b3a2ed92e7479220c6f08ade208bebcd8580ab93b'
TICKET = 'c1bcb375f34955ef6b9e75cc15059d55bf46a85b0e61fa98e4d7a9de16e3b5e1'
TICKET_HASH = 'f22e44d35d61359d57e918b9e46b805a3591a804b845080fd7416cdc2a612eb0'
TOKEN_HASH = '796b2e4cd51a1e5c8c5346d578100dd667264b6c26b5a1dfca876f2e0fb104a5'
BODY = bytes.fromhex(
    '0100000000909192939495969798999a9b3dc6466e523666ef6e740edf8ec9e63d7cb603f63968e207c181b8a81597764cc292af10ed9bd3b5'
    '8b46badbf645e0b0a2bb6315ccba94c7b089151d14927bf4494f5ccec298f0d72467ae0d11cca33a42029f69407325375b2dc8da1e906baa'
    '85545c47a14ef9ab7c4a29555af5b8ffd519c73fd373')
WELCOME = bytes.fromhex(
    'a0a1a2a3a4a5a6a7a8a9aaabda8922b84e01f1b512a2cf53cff6685c32a45a60612033abab0e79079016c0888927292eeeb3cc2d461f9c01ff'
    'fdba1769b9351e70d02f7fb5d2111569322ca3b369c82d5c3fc250b9dac19e1b908c7bb52d8528191575c5337909dedb65a86dddba2d3940'
    '7cce1a90e75318e7825291e5f0543b4085f777e121daa5157c498170b6cee22dc9d7c8f4444acd196b02877565f016aefc07ba26506462f1'
    'ae40e555299323d31f25e0ef44f5563707469473e4f402151b7e88aff20ed3')
LINK = ('coconut://team/aeqccirdeqssmjzifevcwlbnfyxtamjsgm2dknrxha4tuoz4hu7d6aabaibqibiga4eascqlbqgq4dyqcejbgfavcylrqgi'
        '2dmob2hq7nne5eaaacvuhi5dqom5c6l3smvwgc6jomv4gc3lqnrsuk6dbnvygyzjakrswc3i')


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun'),
                     'The test vectors need macOS and the Xcode Command Line Tools.')
class TeamLogVectorTests(unittest.TestCase):
    """The helper that the Mac ships gives the values of the contract. The relay tests check the same values."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        folder = Path(cls.temp.name) / 'team'
        folder.mkdir(mode=0o700)
        compile_helper(folder / 'team-helper')
        # The service takes a helper in <data>/team only when it is private, as scripts/team.py build makes it.
        (folder / 'team-helper').chmod(0o700)
        cls.helper = TeamHelper(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def open(self, body: bytes, *, entry: str = ENTRY, member: str = MEMBER, keys: dict | None = None) -> dict:
        (result,) = self.helper.open(ROOM, keys or {'0': ROOM_KEY}, [{'id': entry, 'member': member, 'body': b64(body)}])
        return result

    def test_the_body_opens_and_is_bound_to_its_entry_member_and_epoch(self):
        self.assertEqual(len(BODY), 135)
        self.assertEqual(self.open(BODY), {'epoch': 0, 'payload': PAYLOAD})
        self.assertEqual(self.open(BODY, entry='41' + ENTRY[2:])['error'], 'seal')
        self.assertEqual(self.open(BODY, member='40' + MEMBER[2:])['error'], 'seal')
        # Another epoch in the header changes the additional data, also when the Mac has the same key for it.
        other_epoch = BODY[:4] + b'\x01' + BODY[5:]
        self.assertEqual(self.open(other_epoch, keys={'0': ROOM_KEY, '1': ROOM_KEY})['error'], 'seal')
        self.assertEqual(self.open(other_epoch), {'error': 'unknown_epoch', 'epoch': 1})
        for index in (0, 5, 20, len(BODY) - 1):
            with self.subTest(index=index):
                changed = bytearray(BODY)
                changed[index] ^= 1
                self.assertIn('error', self.open(bytes(changed)))
        self.assertEqual(self.open(b'\x02' + BODY[1:])['error'], 'unknown_version')

    def test_a_body_that_the_helper_seals_opens_with_the_same_payload(self):
        body = base64.b64decode(self.helper.seal(ROOM, ENTRY, 0, ROOM_KEY, MEMBER_KEY, PAYLOAD))
        self.assertEqual(len(body), len(BODY))
        self.assertNotEqual(body, BODY)
        self.assertEqual(self.open(body), {'epoch': 0, 'payload': PAYLOAD})

    def test_the_welcome_opens_to_the_plaintext_of_the_contract(self):
        self.assertEqual(len(WELCOME), 200)
        self.assertEqual(self.helper.open_welcome(ROOM, SECRET, b64(WELCOME)),
                         {'epoch': 0, 'key': ROOM_KEY, 'admin': MEMBER, 'name': NAME})
        with self.assertRaises(TeamFormatError):
            self.helper.open_welcome('21' + ROOM[2:], SECRET, b64(WELCOME))
        sealed = self.helper.seal_welcome(ROOM, SECRET, 0, ROOM_KEY, MEMBER, NAME)
        self.assertEqual(self.helper.open_welcome(ROOM, SECRET, sealed)['name'], NAME)

    def test_the_link_the_ticket_and_the_proof(self):
        self.assertEqual(self.helper.link(ROOM, SECRET, EXPIRES, RELAY, NAME), LINK)
        self.assertEqual(self.helper.invite(LINK),
                         {'room': ROOM, 'secret': SECRET, 'expires': EXPIRES, 'relay': RELAY, 'name': NAME})
        self.assertEqual(self.helper.ticket(SECRET), {'ticket': TICKET, 'hash': TICKET_HASH})
        # CryptoKit adds random bytes to each signature, so only the hash of the token is a fixed value.
        proof = self.helper.proof(ROOM, TOKEN, MEMBER_KEY)
        self.assertEqual((proof['hash'], len(base64.b64decode(proof['proof']))), (TOKEN_HASH, 64))


if __name__ == '__main__':
    unittest.main()
