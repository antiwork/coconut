"""The team routes on the real listener: the token they need, the listener that serves them, and the logs they write.

test_team_rooms.py covers the room steps through the service. This file goes through the listener that
scripts/team.py talks to, so the path prefix in server.py, the token of the bridge file and the answers are covered.
It also checks that no log line and no error text of a room operation carries a secret: the invite link, a token, a
key, a ticket, a welcome or a proof.
"""
import http.client
import json
import logging
from pathlib import Path
import threading
import unittest

from coconut import team_keys
from coconut.server import APIServer, TunnelServer
from coconut.team_relay import TeamRelayError
from team_fakes import RELAY, TeamMacs

# The static key of a paired phone, as the tunnel sidecar names it.
PHONE = {'X-Coconut-Peer': 'a' * 64}


class Mac:
    """The main listener of one service, and the token of its bridge file."""

    def __init__(self, test, service):
        self.server = test.serve(APIServer(('127.0.0.1', 0), service))
        service.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_port}')
        self.token = json.loads((Path(service.root) / 'gateway' / 'bridge.json').read_text())['token']


class TeamRouteTests(TeamMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.mac_one, self.mac_two = Mac(self, self.first), Mac(self, self.second)

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        self.addCleanup(self.close, server, thread)
        return server

    def close(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def call(self, method, path, data=None, token=None, *, server=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', (server or self.mac_one.server).server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = dict(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data)
            headers['Content-Type'] = 'application/json'
        if token:
            headers['Authorization'] = f'Bearer {token}'
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.load(response)

    def make_room(self) -> dict:
        status, answer = self.call('POST', '/v1/team/rooms', {'name': 'Example Team', 'member_name': 'Ada', 'relay': RELAY},
                                   self.mac_one.token)
        self.assertEqual(status, 200, answer)
        return answer['room']

    def test_the_token_of_the_bridge_opens_the_routes(self):
        room = self.make_room()
        status, invite = self.call('POST', f'/v1/team/rooms/{room["room"]}/invites', {}, self.mac_one.token)
        self.assertEqual((status, set(invite)), (200, {'link', 'expires', 'uses'}))
        status, joined = self.call('POST', '/v1/team/join', {'link': invite['link'], 'member_name': 'Grace'},
                                   self.mac_two.token, server=self.mac_two.server)
        self.assertEqual((status, joined['room']['state']), (200, 'active'))
        # The sync route waits for a pass of the thread.
        self.first._start_team()
        status, answer = self.call('POST', '/v1/team/sync', {}, self.mac_one.token)
        self.assertTrue(answer['complete'])
        self.assertEqual((status, [member['name'] for member in answer['rooms'][0]['members']]), (200, ['Ada', 'Grace']))
        status, answer = self.call('GET', '/v1/team/rooms', None, self.mac_one.token)
        self.assertEqual((status, answer['helper'], len(answer['rooms'])), (200, True, 1))
        status, answer = self.call('DELETE', f'/v1/team/rooms/{room["room"]}', None, self.mac_two.token,
                                   server=self.mac_two.server)
        self.assertEqual((status, answer), (200, {'room': room['room'], 'left': True}))
        # An error of a room operation comes back as the JSON of the service.
        status, answer = self.call('DELETE', f'/v1/team/rooms/{room["room"]}', None, self.mac_two.token,
                                   server=self.mac_two.server)
        self.assertEqual((status, answer), (404, {'error': 'This Mac is not in this room.'}))

    def test_no_token_another_token_and_a_device_token_get_401(self):
        device = self.first.pair({'code': self.first.pairing_code()})['token']
        for token in (None, 'not-the-token', device, self.mac_two.token):
            with self.subTest(token=token and token[:4]):
                for method, path, data in (('GET', '/v1/team/rooms', None), ('POST', '/v1/team/rooms', {'name': 'Example Team'}),
                                           ('POST', '/v1/team/join', {'link': 'coconut://team/x'})):
                    status, answer = self.call(method, path, data, token)
                    self.assertEqual(status, 401)
                    self.assertIn('bridge.json', answer['error'])
        self.assertEqual((self.relay.requests, self.first.team_store.rooms()), ([], []))

    def test_the_tunnel_listener_does_not_serve_the_routes(self):
        tunnel = self.serve(TunnelServer(0, self.first))
        secret = self.first.pairing_code(600, tunnel=True)
        device = self.first.pair({'code': secret}, PHONE['X-Coconut-Peer'])['token']
        for headers in ({}, PHONE):
            with self.subTest(headers=headers):
                status, answer = self.call('GET', '/v1/team/rooms', None, self.mac_one.token, server=tunnel, headers=headers)
                self.assertEqual((status, answer['error']), (401, 'Pair this phone with the Mac to continue.'))
        # The phone has no team route yet.
        status, answer = self.call('GET', '/v1/team/rooms', None, device, server=tunnel, headers=PHONE)
        self.assertEqual((status, answer['error']), (404, 'This route was not found.'))
        self.assertEqual(self.relay.requests, [])

    def test_no_log_line_and_no_error_has_a_secret(self):
        records, texts = [], []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, handler)

        room = self.make_room()
        room_id = room['room']
        _, invite = self.call('POST', f'/v1/team/rooms/{room_id}/invites', {'uses': 1}, self.mac_one.token)
        # A join that the relay does not answer, then the join, then a join with the used link on a third Mac.
        self.relay.down = True
        texts.append(self.call('POST', '/v1/team/join', {'link': invite['link']}, self.mac_two.token,
                               server=self.mac_two.server)[1]['error'])
        self.relay.down = False
        self.call('POST', '/v1/team/join', {'link': invite['link'], 'member_name': 'Grace'}, self.mac_two.token,
                  server=self.mac_two.server)
        texts.append(self.refusal(self.join, self.third, invite['link'])[1])
        texts.append(self.refusal(self.join, self.third, invite['link'] + 'x')[1])
        # A write that gets no answer, a pass that fails, and a leave.
        def lose_the_write():
            if self.relay.requests[-1][0] == 'write':
                raise TeamRelayError(0, 'The relay did not answer.')
        self.relay.before = lose_the_write
        texts.append(self.refusal(self.leave, self.second, room)[1])
        self.relay.before = None
        self.relay.down = True
        self.first.team_wake(retry=True)
        self.first.team_sync_pass()
        self.relay.down = False
        self.first.team_wake(retry=True)
        self.first.team_sync_pass()
        self.leave(self.second, room)

        link = self.first.team_helper.invite(invite['link'])
        ticket = self.first.team_helper.ticket(link['secret'])
        (welcome,) = (row['welcome'] for row in self.relay.rooms[room_id]['tickets'].values())
        admin = team_keys.read(self.first.root, room_id)
        secrets = {'link': invite['link'], 'invite secret': link['secret'], 'ticket': ticket['ticket'],
                   'welcome': welcome, 'room key': admin['keys']['0'], 'token': admin['token'],
                   'member key': admin['member_key'], 'agreement key': admin['agreement_key'],
                   'proof': self.first.team_helper.proof(room_id, admin['token'], admin['member_key'])['proof']}
        output = '\n'.join([*(logging.Formatter().format(record) for record in records), *texts])
        self.assertIn('coconut.team_log', output)
        for name, value in secrets.items():
            with self.subTest(secret=name):
                self.assertNotIn(value, output)


if __name__ == '__main__':
    unittest.main()
