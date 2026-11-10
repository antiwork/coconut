"""The room operations of a Mac: make a room, invite, join, leave and list (coconut/team.py).

Each Mac of a test is a Service with its own data folder. The Macs share one relay in memory, and the helper seals
nothing (team_fakes.py). scripts/test_team_log.py runs the same steps with the real relay code and the real helper.
"""
import base64
import json
import time
import unittest

from coconut import team_keys
from coconut.team_relay import TeamRelayError
from team_fakes import RELAY, TeamMacs, decode, digest, encode


class TeamRoomTests(TeamMacs, unittest.TestCase):
    # Make a room

    def test_the_mac_that_makes_a_room_is_its_admin_and_writes_its_member_entry(self):
        room = self.create(avatar='coconut', color='green')
        self.assertEqual((room['name'], room['relay'], room['state'], room['is_admin'], room['admin'], room['epoch']),
                         ('Example Team', RELAY, 'active', True, room['member'], 0))
        self.assertEqual(room['members'], [dict(member=room['member'], name='Ada', avatar='coconut', color='green',
                                                agreement=room['members'][0]['agreement'], admin=True, this_mac=True)])
        state = self.relay.rooms[room['room']]
        self.assertEqual(list(state['members']), [room['member']])
        # The `member` entry is a kept entry in the first epoch, signed by the member key of this Mac.
        (entry,) = state['entries'].values()
        body = decode(entry['body'])
        self.assertEqual((entry['seq'], entry['kept'], body['epoch'], body['signer']), (1, True, 0, room['member']))
        payload = json.loads(base64.b64decode(body['payload']))
        self.assertEqual({name: payload[name] for name in ('kind', 'rev', 'name', 'avatar', 'color', 'agreement')},
                         dict(kind='member', rev=1, name='Ada', avatar='coconut', color='green',
                              agreement=room['members'][0]['agreement']))
        self.assertRegex(payload['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$')
        self.assertEqual(room['sync']['entries'], 1)

    def test_the_secrets_of_a_room_are_in_one_private_file_and_in_no_answer(self):
        room = self.create()
        folder = self.first.root / 'team'
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.key_file(self.first, room['room']).stat().st_mode & 0o777, 0o600)
        secrets = team_keys.read(self.first.root, room['room'])
        self.assertEqual(set(secrets), {'version', 'room', 'member_key', 'agreement_key', 'token', 'keys'})
        self.assertEqual(list(secrets['keys']), ['0'])
        self.assertEqual(self.relay.rooms[room['room']]['members'][room['member']]['hash'], digest(secrets['token']))
        answers = json.dumps([room, self.rooms(self.first)])
        for secret in (secrets['member_key'], secrets['agreement_key'], secrets['token'], secrets['keys']['0']):
            self.assertNotIn(secret, answers)
        # The store has no secret either.
        stored = json.dumps([list(row) for table in ('team_rooms', 'team_entries', 'team_members')
                             for row in self.first.db.execute(f'SELECT * FROM {table}')])
        for secret in (secrets['member_key'], secrets['agreement_key'], secrets['token']):
            self.assertNotIn(secret, stored)

    def test_the_member_name_has_a_neutral_default(self):
        room = self.first.team_request('POST', '/v1/team/rooms', {'name': 'Example Team', 'relay': RELAY})['room']
        self.assertEqual([member['name'] for member in room['members']], ['Member'])

    def test_a_room_needs_a_name_and_a_relay_that_fit_the_rules_of_a_link(self):
        for name in (None, '', ' Example Team', 'Example Team\n', 'Example\u202eTeam', 'n' * 81, 7):
            with self.subTest(name=name):
                self.assertEqual(self.refusal(lambda: self.create(name=name))[0], 400)
        for relay in ('http://relay.example', 'https://relay.example/path', 'https://user@relay.example',
                      'https://10.0.0.5', 'https://localhost', 'relay.example', 'https://relay.example?x=1'):
            with self.subTest(relay=relay):
                self.assertEqual(self.refusal(lambda: self.create(relay=relay))[0], 400)
        for fields in ({'member_name': ''}, {'member_name': 'n' * 81}, {'avatar': 'a' * 81}, {'color': 'c' * 21}, {'color': 7}):
            with self.subTest(fields=fields):
                self.assertEqual(self.refusal(lambda: self.create(**fields))[0], 400)
        self.assertEqual((self.relay.rooms, self.rooms(self.first)), ({}, []))

    def test_a_room_that_the_relay_refuses_leaves_nothing_on_the_mac(self):
        self.relay.down = True
        self.assertEqual(self.refusal(self.create), (503, 'The relay did not answer. Try again later.'))
        self.assertEqual(self.rooms(self.first, sync=False), [])
        self.assertEqual(list((self.first.root / 'team').glob('*.json')), [])

    def test_without_the_helper_a_room_operation_says_how_to_build_it(self):
        self.first.team_helper.available = False
        status, text = self.refusal(self.create)
        self.assertEqual(status, 503)
        self.assertIn('scripts/team.py build', text)
        self.assertEqual(self.first.team_request('GET', '/v1/team/rooms', {}), {'rooms': [], 'helper': False})

    def test_a_mac_can_be_in_more_than_one_room(self):
        one, two = self.create(name='Example Team'), self.create(name='Other Team', member_name='Ada L.')
        self.assertNotEqual(one['member'], two['member'])
        self.assertEqual([(room['name'], [member['name'] for member in room['members']]) for room in self.rooms(self.first)],
                         [('Example Team', ['Ada']), ('Other Team', ['Ada L.'])])

    # Invite

    def test_an_invite_puts_a_ticket_and_a_sealed_welcome_on_the_relay_and_gives_a_link(self):
        room = self.create()
        before = int(time.time())
        invite = self.invite(room, ttl=600, uses=3)
        self.assertEqual((invite['uses'], set(invite)), (3, {'link', 'expires', 'uses'}))
        self.assertTrue(before + 600 <= invite['expires'] <= time.time() + 600)
        link = self.first.team_helper.invite(invite['link'])
        self.assertEqual((link['room'], link['relay'], link['name'], link['expires']),
                         (room['room'], RELAY, 'Example Team', invite['expires']))
        (ticket_hash, ticket), = self.relay.rooms[room['room']]['tickets'].items()
        self.assertEqual(ticket_hash, self.first.team_helper.ticket(link['secret'])['hash'])
        self.assertEqual((ticket['uses'], ticket['ttl']), (3, 600))
        # The welcome names the room key of the epoch, the admin and the team.
        welcome = self.first.team_helper.open_welcome(room['room'], link['secret'], ticket['welcome'])
        self.assertEqual(welcome, dict(epoch=0, key=team_keys.read(self.first.root, room['room'])['keys']['0'],
                                       admin=room['member'], name='Example Team'))

    def test_an_invite_has_the_limits_of_a_ticket(self):
        room = self.create()
        for fields in ({'ttl': 59}, {'ttl': 7 * 86400 + 1}, {'ttl': '600'}, {'uses': 0}, {'uses': 65}, {'uses': True}):
            with self.subTest(fields=fields):
                self.assertEqual(self.refusal(lambda: self.invite(room, **fields))[0], 400)
        self.assertEqual(self.relay.rooms[room['room']]['tickets'], {})
        # Without values, an invite lasts one day and admits one Mac.
        self.invite(room)
        (ticket,) = self.relay.rooms[room['room']]['tickets'].values()
        self.assertEqual((ticket['uses'], ticket['ttl']), (1, 86400))

    def test_only_the_admin_makes_an_invite(self):
        room = self.create()
        self.join(self.second, self.invite(room)['link'])
        self.assertEqual(self.refusal(lambda: self.invite(room, self.second)), (403, 'Only the admin of the room makes an invite.'))
        self.assertEqual(self.refusal(lambda: self.invite(room, self.third))[0], 404)
        self.assertEqual(self.refusal(lambda: self.invite({'room': 'not a room'}))[0], 404)

    def test_the_relay_holds_a_limit_of_open_invites(self):
        room = self.create()
        for _ in range(8):
            self.invite(room)
        status, text = self.refusal(lambda: self.invite(room))
        self.assertEqual(status, 409)
        self.assertIn('open invites', text)

    # Join

    def test_a_mac_joins_with_a_link_and_each_mac_sees_the_member_entry_of_the_other(self):
        room = self.create()
        joined = self.join(self.second, self.invite(room)['link'])
        self.assertEqual((joined['room'], joined['state'], joined['name'], joined['admin'], joined['is_admin'], joined['epoch']),
                         (room['room'], 'active', 'Example Team', room['member'], False, 0))
        self.assertNotEqual(joined['member'], room['member'])
        # The joined Mac has the room key of the welcome, and its own keys and token.
        own, admin = (team_keys.read(service.root, room['room']) for service in (self.second, self.first))
        self.assertEqual(own['keys'], admin['keys'])
        for name in ('member_key', 'agreement_key', 'token'):
            self.assertNotEqual(own[name], admin[name])
        self.assertEqual(self.key_file(self.second, room['room']).stat().st_mode & 0o777, 0o600)
        for service in (self.first, self.second):
            view = self.rooms(service)[0]
            self.assertEqual([(member['name'], member['admin'], member['this_mac']) for member in view['members']],
                             [('Ada', True, service is self.first), ('Grace', False, service is self.second)])
            self.assertEqual((view['sync']['seq'], view['sync']['entries'], view['sync']['waiting'], view['sync']['error']),
                             (2, 2, 0, None))
            self.assertIsNotNone(view['sync']['synced_at'])

    def test_the_room_takes_the_name_that_the_admin_sealed_in_the_welcome(self):
        room = self.create()
        link = self.first.team_helper.invite(self.invite(room)['link'])
        # Someone changed the name in the link on its way. The welcome is sealed, so its name is the one of the admin.
        changed = 'coconut://team/' + encode(dict(link, version=1, name='Another Name'))
        self.assertEqual(self.join(self.second, changed)['name'], 'Example Team')

    def test_a_second_join_with_a_used_ticket_fails_and_keeps_nothing(self):
        room = self.create()
        link = self.invite(room, uses=1)['link']
        self.join(self.second, link)
        status, text = self.refusal(self.join, self.third, link)
        self.assertEqual(status, 409)
        self.assertIn('invite is used', text)
        self.assertEqual(self.rooms(self.third, sync=False), [])
        self.assertFalse(self.key_file(self.third, room['room']).exists())
        self.assertEqual(len(self.relay.rooms[room['room']]['members']), 2)

    def test_a_mac_joins_a_room_only_once(self):
        room = self.create()
        link = self.invite(room, uses=2)['link']
        self.join(self.second, link)
        self.assertEqual(self.refusal(self.join, self.second, link)[0], 409)
        self.assertEqual(self.refusal(self.join, self.first, link)[0], 409)
        self.assertEqual(len(self.relay.rooms[room['room']]['members']), 2)

    def test_a_link_that_is_not_an_invite_or_is_past_its_end_is_refused_before_the_relay(self):
        room = self.create()
        link = self.first.team_helper.invite(self.invite(room)['link'])
        requests = len(self.relay.requests)
        cases = {
            'coconut://pair/abc': (400, 'This is not the invite link of a team.'),
            'coconut://team/' + encode(dict(link, version=2)): (400, 'A newer Coconut made this invite link. Update Coconut, then join again.'),
            'coconut://team/' + encode(dict(link, version=1, expires=int(time.time()) - 1)): (410, 'This invite link is past its end. Ask the admin for a new one.'),
            'coconut://team/' + encode(dict(link, version=1, relay='http://relay.example')): (400, 'This is not the invite link of a team.'),
        }
        for text, expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(self.refusal(self.join, self.second, text), expected)
        for value in (None, '', 7, 'x' * 5000):
            self.assertEqual(self.refusal(lambda: self.second.team_request('POST', '/v1/team/join', {'link': value}))[0], 400)
        self.assertEqual((len(self.relay.requests), self.rooms(self.second, sync=False)), (requests, []))
        self.assertFalse(self.key_file(self.second, room['room']).exists())

    def test_a_join_without_an_answer_of_the_relay_asks_again_with_the_same_member_and_token(self):
        room = self.create()
        link = self.invite(room)['link']
        self.relay.down = True
        self.assertEqual(self.refusal(self.join, self.second, link),
                         (503, 'The relay did not answer. Join again with the same link.'))
        (waiting,) = self.rooms(self.second, sync=False)
        self.assertEqual((waiting['state'], waiting['members'], waiting['admin']), ('joining', [], ''))
        token = team_keys.read(self.second.root, room['room'])['token']
        # The sync does not read a room that the Mac did not join yet.
        self.second.team_sync_pass()
        self.assertNotIn('read', [request[0] for request in self.relay.requests])
        self.relay.down = False
        joined = self.join(self.second, link, 'Grace H.')
        self.assertEqual((joined['state'], joined['member']), ('active', waiting['member']))
        self.assertEqual(self.relay.rooms[room['room']]['members'][waiting['member']]['hash'], digest(token))
        self.assertEqual(self.names(self.first), ['Ada', 'Grace H.'])

    def test_a_mac_that_cannot_open_the_welcome_leaves_the_room(self):
        room = self.create()
        link = self.invite(room)['link']
        (ticket,) = self.relay.rooms[room['room']]['tickets'].values()
        for welcome, reason in ((dict(decode(ticket['welcome']), secret='another secret'), 'The welcome of the room did not open'),
                                (dict(decode(ticket['welcome']), version=2), 'A newer Coconut made this room')):
            with self.subTest(reason=reason):
                ticket['welcome'], ticket['uses'] = encode(welcome), 1
                status, text = self.refusal(self.join, self.second, link)
                self.assertEqual(status, 409)
                self.assertIn(reason, text)
                # The Mac is no member at the relay, and it keeps no row and no key.
                self.assertEqual(list(self.relay.rooms[room['room']]['members']), [room['member']])
                self.assertEqual(self.rooms(self.second, sync=False), [])
                self.assertFalse(self.key_file(self.second, room['room']).exists())

    # Leave

    def test_a_member_that_leaves_tells_the_others_and_keeps_nothing(self):
        room = self.create()
        self.join(self.second, self.invite(room)['link'])
        self.assertEqual(self.names(self.first), ['Ada', 'Grace'])
        token = team_keys.read(self.second.root, room['room'])['token']
        answer = self.second.team_request('DELETE', f'/v1/team/rooms/{room["room"]}', {})
        self.assertEqual(answer, {'room': room['room'], 'left': True})
        # The relay no longer knows the token, and the Mac has no row and no key of the room.
        self.assertEqual(list(self.relay.rooms[room['room']]['members']), [room['member']])
        with self.assertRaises(TeamRelayError) as refused:
            self.relay.read(RELAY, room['room'], token, 0)
        self.assertEqual(refused.exception.status, 401)
        self.assertEqual(self.rooms(self.second), [])
        self.assertFalse(self.key_file(self.second, room['room']).exists())
        for table in ('team_rooms', 'team_entries', 'team_members', 'team_pending'):
            self.assertEqual(self.second.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0, table)
        # The `member` entry of the Mac is now a `delete` entry that is not kept, so it outlives the leave.
        deleted = [entry for entry in self.relay.rooms[room['room']]['entries'].values() if entry['seq'] == 3]
        self.assertEqual([(entry['kept'], entry['first']) for entry in deleted], [(False, 2)])
        self.assertEqual(self.names(self.first), ['Ada'])

    def test_a_member_leaves_also_without_a_working_helper(self):
        room = self.create()
        self.join(self.second, self.invite(room)['link'])
        self.assertEqual(self.names(self.first), ['Ada', 'Grace'])
        self.second.team_helper.available = False
        with self.assertLogs('coconut.team', 'WARNING') as logs:
            self.assertEqual(self.leave(self.second, room), {'room': room['room'], 'left': True})
        self.assertIn('leaves without a delete entry', logs.output[0])
        self.assertEqual(list(self.relay.rooms[room['room']]['members']), [room['member']])
        self.assertEqual(self.rooms(self.second, sync=False), [])
        self.assertFalse(self.key_file(self.second, room['room']).exists())
        # Without the `delete` entry, the first Mac keeps the copy of the member entry (docs/team-log.md, "To leave").
        self.assertEqual(self.names(self.first), ['Ada', 'Grace'])

    def test_the_admin_leaves_only_a_room_without_other_members(self):
        room = self.create()
        self.join(self.second, self.invite(room)['link'])
        path = f'/v1/team/rooms/{room["room"]}'
        self.assertEqual(self.refusal(self.first.team_request, 'DELETE', path, {}),
                         (409, 'The admin cannot leave a room that has other members.'))
        # Nothing changed: the admin is still in the list of each Mac.
        self.assertEqual((self.names(self.first), self.names(self.second)), (['Ada', 'Grace'], ['Ada', 'Grace']))
        self.second.team_request('DELETE', path, {})
        self.assertEqual(self.first.team_request('DELETE', path, {}), {'room': room['room'], 'left': True})
        # The relay deleted the room with its admin.
        self.assertEqual((self.relay.rooms, self.rooms(self.first)), ({}, []))
        self.assertFalse(self.key_file(self.first, room['room']).exists())

    def test_a_leave_without_an_answer_of_the_relay_keeps_the_room(self):
        room = self.create()
        self.join(self.second, self.invite(room)['link'])
        self.relay.down = True
        path = f'/v1/team/rooms/{room["room"]}'
        self.assertEqual(self.refusal(self.second.team_request, 'DELETE', path, {})[0], 503)
        self.assertEqual(self.rooms(self.second, sync=False)[0]['state'], 'active')
        self.assertTrue(self.key_file(self.second, room['room']).exists())
        self.relay.down = False
        self.assertEqual(self.second.team_request('DELETE', path, {})['left'], True)

    def test_a_mac_that_the_relay_no_longer_knows_deletes_its_copy_with_a_leave(self):
        room = self.create()
        self.join(self.second, self.invite(room)['link'])
        del self.relay.rooms[room['room']]['members'][self.rooms(self.second)[0]['member']]
        self.assertEqual(self.rooms(self.second)[0]['state'], 'removed')
        self.assertEqual(self.second.team_request('DELETE', f'/v1/team/rooms/{room["room"]}', {})['left'], True)
        self.assertEqual(self.rooms(self.second), [])
        self.assertEqual(self.refusal(self.second.team_request, 'DELETE', f'/v1/team/rooms/{room["room"]}', {})[0], 404)

    def test_an_unknown_team_route_is_not_found(self):
        for method, path in (('GET', '/v1/team/unknown'), ('PUT', '/v1/team/rooms'), ('GET', '/v1/team/join'),
                             ('POST', '/v1/team/rooms/' + 'a' * 64), ('GET', '/v1/team/rooms/' + 'a' * 64 + '/invites')):
            with self.subTest(path=path):
                self.assertEqual(self.refusal(self.first.team_request, method, path, {}), (404, 'This route was not found.'))


if __name__ == '__main__':
    unittest.main()
