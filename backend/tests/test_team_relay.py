"""The client of the relay routes of a team room (coconut/team_relay.py), against a local HTTP server.

relay/test/room.test.js tests the routes themselves, and scripts/test_team_log.py runs this client against them. This
file checks what the client sends, how it reads each answer, and the rule for the address of a relay.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest import mock

from coconut import team_relay
from coconut.team_relay import TeamRelay, TeamRelayError, relay_origin

ROOM = 'a' * 64
MEMBER = 'b' * 64
TOKEN = 'c' * 64
ENTRY = {'seq': 3, 'first': 1, 'id': 'd' * 32, 'member': MEMBER, 'at': 1800000000000, 'kept': True, 'body': 'Ym9keQ=='}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def answer(self):
        server = self.server
        length = int(self.headers.get('Content-Length') or 0)
        server.seen.append({'method': self.command, 'path': self.path, 'authorization': self.headers.get('Authorization'),
                            'type': self.headers.get('Content-Type'),
                            'body': json.loads(self.rfile.read(length)) if length else None})
        status, value, headers = server.answers.pop(0) if server.answers else (204, None, {})
        if status == 0:
            # No answer: the connection closes before a status line.
            self.close_connection = True
            return
        content = value if isinstance(value, bytes) else (json.dumps(value).encode() if value is not None else b'')
        self.send_response(status)
        for name, text in headers.items():
            self.send_header(name, text)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    do_GET = do_POST = do_PUT = do_DELETE = answer


class TeamRelayTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.seen, self.server.answers = [], []
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.relay = TeamRelay(timeout=2)
        sleep = mock.patch.object(team_relay.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def answers(self, *answers):
        self.server.answers.extend((status, value, {}) for status, value in answers)

    def failure(self, call, *arguments) -> TeamRelayError:
        with self.assertRaises(TeamRelayError) as raised:
            call(*arguments)
        return raised.exception

    def test_each_route_has_its_method_path_authorization_and_body(self):
        claim = {'member': MEMBER, 'hash': 'e' * 64, 'proof': 'cHJvb2Y='}
        self.answers((201, {}), (204, None), (204, None), (200, {'welcome': 'd2VsY29tZQ=='}),
                     (200, {'members': [{'member': MEMBER, 'admin': True, 'joined': 1}]}), (204, None),
                     (200, {'seq': 3, 'first': 1}), (200, {'entries': [ENTRY], 'last': 3}), (204, None))
        self.relay.create(self.url, ROOM, claim)
        self.relay.add_ticket(self.url, ROOM, TOKEN, 'f' * 64, 600, 2, 'd2VsY29tZQ==')
        self.relay.remove_ticket(self.url, ROOM, TOKEN, 'f' * 64)
        self.assertEqual(self.relay.join(self.url, ROOM, 'ticket', claim), 'd2VsY29tZQ==')
        self.assertEqual(self.relay.members(self.url, ROOM, TOKEN), [{'member': MEMBER, 'admin': True, 'joined': 1}])
        self.relay.remove_member(self.url, ROOM, TOKEN, MEMBER)
        self.assertEqual(self.relay.write(self.url, ROOM, TOKEN, ENTRY['id'], True, 'Ym9keQ=='), {'seq': 3, 'first': 1})
        self.assertEqual(self.relay.read(self.url, ROOM, TOKEN, 2), {'entries': [ENTRY], 'last': 3})
        self.relay.destroy(self.url, ROOM, TOKEN)
        bearer = f'Bearer {TOKEN}'
        self.assertEqual([(seen['method'], seen['path'], seen['authorization'], seen['body']) for seen in self.server.seen], [
            ('PUT', f'/v1/rooms/{ROOM}', None, claim),
            ('POST', f'/v1/rooms/{ROOM}/tickets', bearer, {'hash': 'f' * 64, 'ttl': 600, 'uses': 2, 'welcome': 'd2VsY29tZQ=='}),
            ('DELETE', f'/v1/rooms/{ROOM}/tickets/{"f" * 64}', bearer, None),
            ('POST', f'/v1/rooms/{ROOM}/members', 'Ticket ticket', claim),
            ('GET', f'/v1/rooms/{ROOM}/members', bearer, None),
            ('DELETE', f'/v1/rooms/{ROOM}/members/{MEMBER}', bearer, None),
            ('POST', f'/v1/rooms/{ROOM}/entries', bearer, {'id': ENTRY['id'], 'kept': True, 'body': 'Ym9keQ=='}),
            ('GET', f'/v1/rooms/{ROOM}/entries?after=2', bearer, None),
            ('DELETE', f'/v1/rooms/{ROOM}', bearer, None)])
        self.assertEqual({seen['type'] for seen in self.server.seen if seen['body']}, {'application/json'})

    def test_a_status_that_the_route_does_not_give_is_an_error_with_that_status(self):
        for status, temporary in ((401, False), (404, False), (409, False), (413, False), (429, True), (503, True)):
            with self.subTest(status=status):
                self.answers((status, {'error': 'no'}))
                error = self.failure(self.relay.write, self.url, ROOM, TOKEN, ENTRY['id'], False, 'Ym9keQ==')
                self.assertEqual((error.status, error.temporary, str(error)), (status, temporary, f'The relay answered {status}.'))

    def test_an_answer_without_the_fields_of_the_route_is_no_answer(self):
        cases = [
            (self.relay.join, (self.url, ROOM, 'ticket', {}), {'welcome': 'not base64!'}),
            (self.relay.join, (self.url, ROOM, 'ticket', {}), b'not json'),
            (self.relay.members, (self.url, ROOM, TOKEN), {'members': [{'member': 'B' * 64}]}),
            (self.relay.write, (self.url, ROOM, TOKEN, ENTRY['id'], True, 'Ym9keQ=='), {'seq': 2, 'first': 3}),
            (self.relay.write, (self.url, ROOM, TOKEN, ENTRY['id'], True, 'Ym9keQ=='), {'seq': True, 'first': 1}),
            (self.relay.read, (self.url, ROOM, TOKEN, 3), {'entries': [ENTRY], 'last': 3}),
            (self.relay.read, (self.url, ROOM, TOKEN, 0), {'entries': [ENTRY, ENTRY], 'last': 3}),
            (self.relay.read, (self.url, ROOM, TOKEN, 0), {'entries': [dict(ENTRY, kept=1)], 'last': 3}),
            (self.relay.read, (self.url, ROOM, TOKEN, 0), {'entries': [dict(ENTRY, body='')], 'last': 3}),
            (self.relay.read, (self.url, ROOM, TOKEN, 0), {'entries': [ENTRY]}),
        ]
        for call, arguments, value in cases:
            with self.subTest(call=call.__name__, value=value):
                self.answers((200, value))
                self.assertEqual(self.failure(call, *arguments).status, 0)

    def test_a_request_without_an_answer_goes_again_only_as_often_as_the_caller_says(self):
        self.answers((0, None))
        self.assertEqual(self.failure(self.relay.remove_member, self.url, ROOM, TOKEN, MEMBER).status, 0)
        self.assertEqual(len(self.server.seen), 1)
        self.answers((0, None), (204, None))
        self.relay.remove_member(self.url, ROOM, TOKEN, MEMBER, tries=2)
        self.assertEqual(len(self.server.seen), 3)

    def test_an_answer_over_the_limit_is_no_answer(self):
        with mock.patch.object(team_relay, 'MAX_ANSWER', 64):
            self.answers((200, {'entries': [], 'last': 0, 'padding': 'x' * 100}))
            error = self.failure(self.relay.read, self.url, ROOM, TOKEN, 0)
        self.assertEqual((error.status, str(error)), (0, 'The answer of the relay is too large.'))

    def test_the_client_follows_no_redirect(self):
        self.server.answers.append((307, None, {'Location': 'https://other.example/v1/rooms'}))
        self.assertEqual(self.failure(self.relay.destroy, self.url, ROOM, TOKEN).status, 307)
        self.assertEqual(len(self.server.seen), 1)

    def test_no_error_text_has_the_token_or_the_ticket(self):
        for status in (0, 401, 503):
            self.answers((status, {'token': TOKEN}))
            error = self.failure(self.relay.join, self.url, ROOM, 'ticket-' + TOKEN, {})
            self.assertNotIn(TOKEN, str(error))

    def test_the_address_of_a_relay_is_an_https_origin_with_a_host_name(self):
        for value in ('https://relay.example', 'https://coconut-push.example.workers.dev', 'https://relay.example:8443',
                      'http://127.0.0.1:8787'):
            with self.subTest(value=value):
                self.assertEqual(relay_origin(value), value)
        for value in ('http://relay.example', 'https://relay.example/', 'https://relay.example/v1', 'https://relay.example?x',
                      'https://relay.example#x', 'https://user@relay.example', 'https://user:pass@relay.example',
                      'https://10.0.0.5', 'https://localhost', 'https://relay.123', 'https://relay..example',
                      'https://relay_name.example', 'http://127.0.0.1', 'http://localhost:8787', 'https://[::1]:8787',
                      'HTTPS://relay.example', 'relay.example', '', None, 7, 'https://relay.example:99999'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    relay_origin(value)

    def test_a_host_with_capital_letters_becomes_lowercase(self):
        # The rule of the link in ios/Tunnel/TeamLog.swift accepts such a host, so a link can carry one.
        self.assertEqual(relay_origin('https://Relay.Example'), 'https://relay.example')
        self.assertEqual(relay_origin('https://RELAY.EXAMPLE:8443'), 'https://relay.example:8443')


if __name__ == '__main__':
    unittest.main()
