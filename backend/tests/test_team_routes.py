"""The team routes on the real listeners: who can call them, on which listener, and the logs they write.

test_team_rooms.py covers the room steps through the service. This file goes through the listeners that the app and
scripts/team.py talk to, so the path prefix in server.py, the token of a device, the token of the bridge file and
the answers are covered. It also checks that no log line and no error text of a room operation carries a secret: the
invite link, a token, a key, a ticket, a welcome or a proof.
"""
import http.client
import json
import logging
from pathlib import Path
import threading
import unittest
from uuid import uuid4

from coconut import team_keys
from coconut.server import APIServer, TunnelServer
from coconut.team_relay import TeamRelayError
from team_fakes import RELAY, TeamMacs

# The static key of a paired phone, as the tunnel sidecar names it.
PHONE = {'X-Coconut-Peer': 'a' * 64}
PAIR = 'Pair this phone with the Mac to continue.'


class Mac:
    """The main listener and the tunnel listener of one service, the token of its bridge file, and two paired
    devices: one on the main listener and one through the tunnel."""

    def __init__(self, test, service):
        self.server = test.serve(APIServer(('127.0.0.1', 0), service))
        self.tunnel = test.serve(TunnelServer(0, service))
        service.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_port}')
        self.token = json.loads((Path(service.root) / 'gateway' / 'bridge.json').read_text())['token']
        self.device = service.pair({'code': service.pairing_code()})['token']
        self.phone = service.pair({'code': service.pairing_code(600, tunnel=True)}, PHONE['X-Coconut-Peer'])['token']


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

    def callers(self, mac: Mac) -> dict:
        """The three callers of the team routes on one Mac, each as the options of `call`."""
        return {'tool': dict(token=mac.token, server=mac.server), 'device': dict(token=mac.device, server=mac.server),
                'phone': dict(token=mac.phone, server=mac.tunnel, headers=PHONE)}

    def make_room(self) -> dict:
        status, answer = self.call('POST', '/v1/team/rooms', {'name': 'Example Team', 'member_name': 'Ada', 'relay': RELAY},
                                   self.mac_one.token)
        self.assertEqual(status, 200, answer)
        return answer['room']

    def test_each_caller_reaches_each_team_route(self):
        bot = self.first.mutate('POST', '/v1/bots', {'name': 'Reporter'}, str(uuid4()), 'phone')
        self.first._start_team()
        for name, one in self.callers(self.mac_one).items():
            two = self.callers(self.mac_two)[name]
            with self.subTest(caller=name):
                status, answer = self.call('POST', '/v1/team/rooms', {'name': 'Example Team', 'member_name': 'Ada',
                                                                      'relay': RELAY}, **one)
                self.assertEqual(status, 200, answer)
                room = answer['room']['room']
                status, invite = self.call('POST', f'/v1/team/rooms/{room}/invites', {}, **one)
                self.assertEqual((status, set(invite)), (200, {'link', 'expires', 'uses'}))
                status, joined = self.call('POST', '/v1/team/join', {'link': invite['link'], 'member_name': 'Grace'}, **two)
                self.assertEqual((status, joined['room']['state']), (200, 'active'), joined)
                status, answer = self.call('PATCH', f'/v1/team/rooms/{room}', {'member_name': 'Ada L.'}, **one)
                self.assertEqual((status, [member['name'] for member in answer['room']['members'] if member['this_mac']]),
                                 (200, ['Ada L.']))
                status, answer = self.call('POST', f'/v1/team/rooms/{room}/bots', {'bot_id': bot['id']}, **one)
                self.assertEqual((status, [row['bot_id'] for row in answer['room']['bots']]), (200, [bot['id']]))
                status, answer = self.call('POST', '/v1/team/sync', {}, **one)
                self.assertEqual((status, answer['complete']), (200, True))
                status, answer = self.call('GET', '/v1/team/rooms', None, **one)
                (view,) = (row for row in answer['rooms'] if row['room'] == room)
                self.assertEqual((status, [member['name'] for member in view['members']]), (200, ['Ada L.', 'Grace']))
                status, answer = self.call('DELETE', f'/v1/team/rooms/{room}/bots/{bot["id"]}', None, **one)
                self.assertEqual((status, answer['room']['bots']), (200, []))
                self.assertEqual(self.call('DELETE', f'/v1/team/rooms/{room}', None, **two), (200, {'room': room, 'left': True}))
                self.assertEqual(self.call('DELETE', f'/v1/team/rooms/{room}', None, **one), (200, {'room': room, 'left': True}))
                # An error of a room operation comes back as the JSON of the service.
                self.assertEqual(self.call('DELETE', f'/v1/team/rooms/{room}', None, **one),
                                 (404, {'error': 'This Mac is not in this team.'}))

    def test_each_caller_reaches_the_team_chat(self):
        room = self.make_room()['room']
        invite = self.call('POST', f'/v1/team/rooms/{room}/invites', {'uses': 1}, self.mac_one.token)[1]
        self.assertEqual(self.call('POST', '/v1/team/join', {'link': invite['link'], 'member_name': 'Grace'},
                                   self.mac_two.token, server=self.mac_two.server)[0], 200)
        messages = f'/v1/team/rooms/{room}/messages'
        sent = {}
        for name, one in self.callers(self.mac_one).items():
            with self.subTest(caller=name):
                status, answer = self.call('POST', messages, {'text': f'Hello from the {name}'}, **one)
                self.assertEqual((status, answer['message']['text'], answer['message']['this_mac']),
                                 (200, f'Hello from the {name}', True), answer)
                sent[name] = answer['message']
                identity = sent[name]['id']
                status, answer = self.call('PATCH', f'{messages}/{identity}', {'text': f'Hi from the {name}'}, **one)
                self.assertEqual((status, answer['message']['text']), (200, f'Hi from the {name}'), answer)
                # The query of a GET reaches the route.
                status, page = self.call('GET', f'{messages}?limit=1', None, **one)
                self.assertEqual((status, [message['id'] for message in page['messages']]), (200, [identity]), page)
                self.assertEqual(page['before'] is None, name == 'tool')
        # Each caller of the second Mac reads the chat, moves the read position, and cannot change a message of Ada.
        # This test starts no sync thread, so it runs the pass itself.
        self.second.team_sync_pass()
        for name, two in self.callers(self.mac_two).items():
            with self.subTest(reader=name):
                status, page = self.call('GET', messages, None, **two)
                self.assertEqual((status, [message['text'] for message in page['messages']]),
                                 (200, ['Hi from the phone', 'Hi from the device', 'Hi from the tool']), page)
                self.assertEqual(self.call('PATCH', f'{messages}/{sent["tool"]["id"]}', {'text': 'Mine'}, **two),
                                 (403, {'error': 'You can change only your own messages.'}))
                self.assertEqual(self.call('DELETE', f'{messages}/{sent["tool"]["id"]}', None, **two),
                                 (403, {'error': 'You can change only your own messages.'}))
        status, rooms = self.call('GET', '/v1/team/rooms', None, self.mac_two.token, server=self.mac_two.server)
        (view,) = rooms['rooms']
        self.assertEqual((status, view['unread'], view['last_message']['text'], view['last_message']['member_name']),
                         (200, 3, 'Hi from the phone', 'Ada'))
        status, answer = self.call('POST', f'/v1/team/rooms/{room}/read', {'first': sent['device']['first']},
                                   **self.callers(self.mac_two)['phone'])
        self.assertEqual((status, answer), (200, {'room': room, 'read': sent['device']['first'], 'unread': 1}))
        # A repeat of a send with the same key gives one message, and a repeat of a delete the same answer.
        phone = self.callers(self.mac_one)['phone']
        key = {'Idempotency-Key': str(uuid4())}
        repeats = [self.call('POST', messages, {'text': 'Once'}, phone['token'], server=phone['server'],
                             headers=dict(PHONE, **key)) for _ in range(2)]
        self.assertEqual((repeats[0][0], repeats[0]), (200, repeats[1]))
        key = {'Idempotency-Key': str(uuid4())}
        identity = repeats[0][1]['message']['id']
        repeats = [self.call('DELETE', f'{messages}/{identity}', None, phone['token'], server=phone['server'],
                             headers=dict(PHONE, **key)) for _ in range(2)]
        self.assertEqual(repeats, [(200, {'room': room, 'id': identity, 'deleted': True})] * 2)
        texts = [message['text'] for message in self.call('GET', messages, None, self.mac_one.token)[1]['messages']]
        self.assertEqual(texts, ['Hi from the phone', 'Hi from the device', 'Hi from the tool'])

    def test_a_request_without_a_valid_token_gets_401(self):
        messages = f'/v1/team/rooms/{"a" * 64}/messages'
        routes = (('GET', '/v1/team/rooms', None), ('POST', '/v1/team/rooms', {'name': 'Example Team'}),
                  ('POST', '/v1/team/join', {'link': 'coconut://team/x'}), ('PATCH', f'/v1/team/rooms/{"a" * 64}', {}),
                  ('GET', messages + '?limit=1', None), ('POST', messages, {'text': 'Hello'}),
                  ('PATCH', f'{messages}/{"b" * 32}', {'text': 'Hello'}), ('DELETE', f'{messages}/{"b" * 32}', None),
                  ('POST', f'/v1/team/rooms/{"a" * 64}/read', {'first': 1}))
        main = {'no token': {}, 'another token': {'token': 'not-the-token'},
                'the bridge token of another Mac': {'token': self.mac_two.token},
                'a device of another Mac': {'token': self.mac_two.device},
                # A device that paired through the tunnel must connect with its static key.
                'a phone without the tunnel': {'token': self.mac_one.phone}}
        tunnel = {'no peer': {'token': self.mac_one.phone},
                  'the bridge token': {'token': self.mac_one.token, 'headers': PHONE},
                  'another peer': {'token': self.mac_one.phone, 'headers': {'X-Coconut-Peer': 'b' * 64}},
                  'a device of the main listener': {'token': self.mac_one.device, 'headers': PHONE}}
        cases = [(name, dict(options, server=self.mac_one.server)) for name, options in main.items()]
        cases += [(name, dict(options, server=self.mac_one.tunnel)) for name, options in tunnel.items()]
        for name, options in cases:
            for method, path, data in routes:
                with self.subTest(caller=name, path=path, method=method):
                    self.assertEqual(self.call(method, path, data, **options), (401, {'error': PAIR}))
        self.assertEqual((self.relay.requests, self.first.team_store.rooms()), ([], []))

    def test_the_request_log_of_the_phone_has_routes_and_no_invite_link(self):
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        logger = logging.getLogger('coconut.server')
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        one, two = self.callers(self.mac_one)['phone'], self.callers(self.mac_two)['phone']
        room = self.call('POST', '/v1/team/rooms', {'name': 'Example Team', 'member_name': 'Ada', 'relay': RELAY}, **one)[1]['room']
        link = self.call('POST', f'/v1/team/rooms/{room["room"]}/invites', {}, **one)[1]['link']
        self.assertEqual(self.call('POST', '/v1/team/join', {'link': link, 'member_name': 'Grace'}, **two)[0], 200)
        # A join with a link that is not valid writes its route too, and the link stays out of the log.
        self.assertEqual(self.call('POST', '/v1/team/join', {'link': link + 'x'}, **two)[0], 400)
        output = '\n'.join(record.getMessage() for record in records)
        routes = [json.loads(line.split(' ', 1)[1])['route'] for line in output.splitlines()]
        self.assertEqual(routes, ['/v1/team/rooms', '/v1/team/rooms/:id/invites', '/v1/team/join', '/v1/team/join'])
        secrets = team_keys.read(self.first.root, room['room'])
        for secret in (link, link.removeprefix('coconut://team/'), secrets['token'], secrets['keys']['0']):
            self.assertNotIn(secret, output)

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
