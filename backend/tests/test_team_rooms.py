"""The room operations of a Mac: make a room, invite, join, leave and list (coconut/team.py).

Each Mac of a test is a Service with its own data folder. The Macs share one relay in memory, and the helper seals
nothing (team_fakes.py). scripts/test_team_log.py runs the same steps with the real relay code and the real helper.
"""
import base64
import json
import time
import unittest
from unittest import mock
from uuid import uuid4

from coconut import team_keys
from coconut.team_relay import TeamRelayError
from team_fakes import RELAY, TeamMacs, decode, digest, encode

# A name with an emoji sequence: a woman, the zero width joiner, and a laptop.
CODER = 'Ada \U0001f469\u200d\U0001f4bb'


class TeamRoomTests(TeamMacs, unittest.TestCase):
    # Make a room

    def test_the_mac_that_makes_a_room_is_its_admin_and_writes_its_member_entry(self):
        room = self.create(avatar='coconut', color='green')
        self.assertEqual((room['name'], room['relay'], room['state'], room['is_admin'], room['admin'], room['epoch']),
                         ('Example Team', RELAY, 'active', True, room['member'], 0))
        self.assertEqual(room['members'], [dict(member=room['member'], name='Ada', avatar='coconut', color='green',
                                                admin=True, this_mac=True)])
        # The public agreement key is a key, so no answer has it. The store has it from the `member` entry.
        (stored,) = self.first.team_store.members(room['room'])
        state = self.relay.rooms[room['room']]
        self.assertEqual(list(state['members']), [room['member']])
        # The `member` entry is a kept entry in the first epoch, signed by the member key of this Mac.
        (entry,) = state['entries'].values()
        body = decode(entry['body'])
        self.assertEqual((entry['seq'], entry['kept'], body['epoch'], body['signer']), (1, True, 0, room['member']))
        payload = json.loads(base64.b64decode(body['payload']))
        self.assertEqual({name: payload[name] for name in ('kind', 'rev', 'name', 'avatar', 'color', 'agreement')},
                         dict(kind='member', rev=1, name='Ada', avatar='coconut', color='green',
                              agreement=stored['agreement']))
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
        self.assertEqual(self.refusal(self.create), (503, 'Coconut could not reach its server. Try again later.'))
        self.assertEqual(self.rooms(self.first, sync=False), [])
        self.assertEqual(list((self.first.root / 'team').glob('*.json')), [])

    def test_without_the_helper_a_room_operation_asks_for_an_update(self):
        self.first.team_helper.available = False
        self.assertEqual(self.refusal(self.create), (503, 'Update Coconut on this Mac, then try again.'))
        answer = self.first.team_request('GET', '/v1/team/rooms', {})
        self.assertEqual((answer['rooms'], answer['helper']), ([], False))

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
        self.assertEqual(self.refusal(lambda: self.invite(room, self.second)), (403, 'Only the admin of the team can invite a person.'))
        self.assertEqual(self.refusal(lambda: self.invite(room, self.third))[0], 404)
        self.assertEqual(self.refusal(lambda: self.invite({'room': 'not a room'}))[0], 404)

    def test_the_relay_holds_a_limit_of_open_invites(self):
        room = self.create()
        for _ in range(8):
            self.invite(room)
        status, text = self.refusal(lambda: self.invite(room))
        self.assertEqual(status, 409)
        self.assertEqual(text, 'The team has 8 open invite links. Wait until one ends, then try again.')

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
        self.assertEqual(text, 'The invite link is used, past its end or removed. Ask for a new link.')
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
            'coconut://pair/abc': (400, 'This is not the invite link of a team. Check the link, then try again.'),
            'coconut://team/' + encode(dict(link, version=2)): (400, 'A newer Coconut made this invite link. Update Coconut, then join again.'),
            'coconut://team/' + encode(dict(link, version=1, expires=int(time.time()) - 1)): (410, 'The invite link is past its end. Ask for a new link.'),
            'coconut://team/' + encode(dict(link, version=1, relay='http://relay.example')): (400, 'This is not the invite link of a team. Check the link, then try again.'),
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
                         (503, 'Coconut could not reach its server. Join again with the same link.'))
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

    def test_a_second_join_of_a_joining_room_must_name_the_relay_of_the_first(self):
        room = self.create()
        link = self.invite(room)['link']
        self.relay.down = True
        self.assertEqual(self.refusal(self.join, self.second, link)[0], 503)
        self.relay.down = False
        # The first relay can have taken the join, so the member token of this Mac goes to no other relay.
        other = 'coconut://team/' + encode(dict(self.first.team_helper.invite(link), version=1, relay='https://other.example'))
        self.assertEqual(self.refusal(self.join, self.second, other), (
            409, 'This Mac already tries to join this team with a link of another server. Leave the team on this Mac, '
                 'then join again.'))
        self.assertEqual([request for request in self.relay.requests if request[1] != RELAY], [])
        self.assertEqual(self.rooms(self.second, sync=False)[0]['relay'], RELAY)
        self.assertEqual(self.join(self.second, link)['state'], 'active')

    def test_a_mac_that_cannot_open_the_welcome_leaves_the_room(self):
        room = self.create()
        link = self.invite(room)['link']
        (ticket,) = self.relay.rooms[room['room']]['tickets'].values()
        for welcome, reason in ((dict(decode(ticket['welcome']), secret='another secret'), 'This Mac could not open the invite'),
                                (dict(decode(ticket['welcome']), version=2), 'A newer Coconut made this team')):
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

    def test_a_member_leaves_also_when_the_read_back_of_a_write_without_an_answer_fails(self):
        room = self.create()
        write = self.relay.write

        def lose_the_answer(*arguments, **options):
            write(*arguments, **options)
            raise TeamRelayError(0, 'The relay did not answer.')
        with mock.patch.object(self.relay, 'write', lose_the_answer), self.assertLogs('coconut.team', 'WARNING'):
            joined = self.join(self.second, self.invite(room)['link'])
        # The relay has the `member` entry of the second Mac, so the leave must read the room before its `delete`.
        self.assertIn(room['room'], self.second._team_unsure)
        self.assertEqual(self.names(self.first), ['Ada', 'Grace'])
        self.second.team_helper.available = False
        with self.assertLogs('coconut.team', 'WARNING') as logs:
            self.assertEqual(self.leave(self.second, room), {'room': room['room'], 'left': True})
        self.assertIn('leaves without a delete entry', '\n'.join(logs.output))
        self.assertNotIn(joined['member'], self.relay.rooms[room['room']]['members'])
        self.assertEqual(self.rooms(self.second, sync=False), [])
        self.assertFalse(self.key_file(self.second, room['room']).exists())

    def test_the_admin_leaves_only_a_room_without_other_members(self):
        room = self.create()
        self.join(self.second, self.invite(room)['link'])
        path = f'/v1/team/rooms/{room["room"]}'
        self.assertEqual(self.refusal(self.first.team_request, 'DELETE', path, {}),
                         (409, 'The admin can leave the team only when no other member is in it.'))
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

    # The Team screen

    def bot(self, service, name: str) -> dict:
        return service.mutate('POST', '/v1/bots', {'name': name}, str(uuid4()), 'phone')

    def share(self, service, room: dict, bot: dict) -> dict:
        """Share a page of the bot with the room, so the bot is the author of a page there."""
        return self.share_page(service, room['room'], bot)

    def rename(self, service, room: dict, **fields) -> dict:
        return service.team_request('PATCH', f'/v1/team/rooms/{room["room"]}', fields)['room']

    def test_the_answer_has_what_the_team_screen_shows_for_an_admin_and_a_member(self):
        room = self.create(avatar='coconut', color='green')
        joined = self.join(self.second, self.invite(room)['link'])
        reporter, helper, archived = self.bot(self.first, 'Reporter'), self.bot(self.second, 'Helper'), self.bot(self.first, 'Old')
        for service, bot in ((self.first, reporter), (self.second, helper), (self.first, archived)):
            self.share(service, room, bot)
        self.first.mutate('PATCH', f'/v1/bots/{archived["id"]}', {'archived': True}, str(uuid4()), 'phone')
        for service in (self.first, self.second, self.first):
            service.team_sync_pass()
        admin, member = self.rooms(self.first)[0], self.rooms(self.second)[0]
        entries = {bot['id']: share for service, bot in ((self.first, reporter), (self.second, helper))
                   for bot_id, share in service.team_store.shares(room['room']).items() if bot_id == bot['id']}
        ada = dict(member=room['member'], name='Ada', avatar='coconut', color='green', admin=True)
        grace = dict(member=joined['member'], name='Grace', avatar='', color='', admin=False)
        bots = [dict(id=f't-{room["room"]}-{entries[reporter["id"]]}', name='Reporter', avatar=reporter['avatar'],
                     color=reporter['color'], member=room['member'], member_name='Ada'),
                dict(id=f't-{room["room"]}-{entries[helper["id"]]}', name='Helper', avatar=helper['avatar'],
                     color=helper['color'], member=joined['member'], member_name='Grace')]
        for view, this_mac, own in ((admin, room['member'], reporter), (member, joined['member'], helper)):
            with self.subTest(this_mac=view is admin and 'admin' or 'member'):
                self.assertEqual((view['room'], view['name'], view['state'], view['is_admin'], view['can_invite']),
                                 (room['room'], 'Example Team', 'active', view is admin, view is admin))
                service = self.first if view is admin else self.second
                self.assertEqual(view['created_at'], service.team_store.room(room['room'])['created_at'])
                self.assertEqual(view['members'], [dict(ada, this_mac=this_mac == room['member']),
                                                   dict(grace, this_mac=this_mac == joined['member'])])
                # The bots follow the order of the members. A bot of this Mac also has its local ID. The archived
                # bot is not in the list.
                expected = [dict(bot, this_mac=bot['member'] == this_mac) for bot in bots]
                for bot in expected:
                    if bot['this_mac']:
                        bot['bot_id'] = own['id']
                self.assertEqual(view['bots'], expected)
                self.assertEqual((view['sync']['error'], type(view['sync']['synced_at'])), (None, str))
                self.assertNotIn('agreement', json.dumps(view))
        # The ID of a team bot of another Mac is the one of the Library.
        self.assertEqual([bot['id'] for bot in self.second.documents()['team_bots']], [bots[0]['id']])

    def test_a_room_that_is_removed_shows_its_members_and_no_bots(self):
        room = self.create()
        joined = self.join(self.second, self.invite(room)['link'])
        self.share(self.second, room, self.bot(self.second, 'Helper'))
        self.second.team_sync_pass()
        self.first.team_sync_pass()
        del self.relay.rooms[room['room']]['members'][joined['member']]
        view = self.rooms(self.second)[0]
        self.assertEqual((view['state'], view['is_admin'], view['can_invite'], view['bots']), ('removed', False, False, []))
        self.assertEqual([member['name'] for member in view['members']], ['Ada', 'Grace'])
        # An admin of a room that is gone cannot invite either.
        del self.relay.rooms[room['room']]
        view = self.rooms(self.first)[0]
        self.assertEqual((view['state'], view['is_admin'], view['can_invite'], view['bots']), ('gone', True, False, []))
        for service, text in ((self.second, 'This Mac is no longer in the team.'), (self.first, 'This team no longer exists.')):
            self.assertEqual(self.refusal(lambda: self.rename(service, room, member_name='Ada L.')), (409, text))
            self.assertEqual(self.refusal(lambda: self.invite(room, service))[0], 409)

    def test_a_new_name_writes_the_member_entry_again_and_the_other_mac_shows_it(self):
        room = self.create(avatar='coconut', color='green')
        self.join(self.second, self.invite(room)['link'])
        entry = json.loads(self.first.team_store.room(room['room'])['profile'])['entry']
        answer = self.rename(self.first, room, member_name=CODER, color='blue')
        self.assertEqual([member['name'] for member in answer['members'] if member['this_mac']], [CODER])
        state = self.relay.rooms[room['room']]['entries'][entry]
        payload = json.loads(base64.b64decode(decode(state['body'])['payload']))
        self.assertEqual((state['kept'], payload['rev'], payload['name'], payload['avatar'], payload['color']),
                         (True, 2, CODER, 'coconut', 'blue'))
        view = self.rooms(self.second)[0]
        self.assertEqual([(member['name'], member['color']) for member in view['members']],
                         [(CODER, 'blue'), ('Grace', '')])
        # The profile follows, so a later `member` entry of the sync has the new name too.
        profile = json.loads(self.first.team_store.room(room['room'])['profile'])
        self.assertEqual((profile['name'], profile['avatar'], profile['color'], profile['entry']),
                         (CODER, 'coconut', 'blue', entry))
        # A write that the relay does not take is an error for the caller, and it changes nothing.
        self.relay.down = True
        self.assertEqual(self.refusal(lambda: self.rename(self.first, room, member_name='Ada L.')),
                         (503, 'Coconut could not reach its server. Try again later.'))
        self.relay.down = False
        self.assertEqual(json.loads(self.first.team_store.room(room['room'])['profile'])['name'], CODER)
        self.assertEqual(self.refusal(lambda: self.rename(self.first, room, member_name=' Ada'))[0], 400)
        self.assertEqual(self.refusal(lambda: self.rename(self.first, {'room': 'a' * 64}, member_name='Ada'))[0], 404)
        self.assertEqual(self.names(self.second), [CODER, 'Grace'])

    def test_a_change_after_a_name_without_an_answer_keeps_the_new_name(self):
        room = self.create(avatar='coconut', color='green')
        self.join(self.second, self.invite(room)['link'])
        write = self.relay.write

        def lose_the_answer(*arguments, **options):
            write(*arguments, **options)
            raise TeamRelayError(0, 'The relay did not answer.')
        with mock.patch.object(self.relay, 'write', lose_the_answer):
            self.assertEqual(self.refusal(lambda: self.rename(self.first, room, member_name='Ada L.'))[0], 503)
        # The relay has the new name. A change of only the color reads it back first, so the name stays.
        answer = self.rename(self.first, room, color='blue')
        self.assertEqual([(member['name'], member['color']) for member in answer['members'] if member['this_mac']],
                         [('Ada L.', 'blue')])
        entry = json.loads(self.first.team_store.room(room['room'])['profile'])['entry']
        payload = json.loads(base64.b64decode(decode(self.relay.rooms[room['room']]['entries'][entry]['body'])['payload']))
        self.assertEqual((payload['rev'], payload['name'], payload['color']), (3, 'Ada L.', 'blue'))
        self.assertEqual([(member['name'], member['color']) for member in self.rooms(self.second)[0]['members']],
                         [('Ada L.', 'blue'), ('Grace', '')])

    def test_the_saved_profile_follows_a_member_entry_that_the_sync_reads_back(self):
        room = self.create()
        write = self.relay.write

        def lose_the_answer(*arguments, **options):
            write(*arguments, **options)
            raise TeamRelayError(0, 'The relay did not answer.')
        with mock.patch.object(self.relay, 'write', lose_the_answer):
            self.refusal(lambda: self.rename(self.first, room, member_name='Ada L.'))
        self.first.team_sync_pass()
        # A `member` entry that the sync writes again after a loss has the newest name too.
        self.assertEqual(json.loads(self.first.team_store.room(room['room'])['profile'])['name'], 'Ada L.')

    def test_a_link_that_is_not_exactly_an_invite_link_is_refused(self):
        link = self.invite(self.create())['link']
        for changed in (link + 'x', link + 'xx', link + 'xxx', link + '=', link + '==', link[:-1], link + '!', link + ' x'):
            with self.subTest(changed=changed[-4:]):
                self.assertEqual(self.refusal(self.join, self.second, changed),
                                 (400, 'This is not the invite link of a team. Check the link, then try again.'))
        self.assertEqual(self.rooms(self.second, sync=False), [])

    def test_a_name_has_one_text_for_each_problem(self):
        noun = {'member_name': 'your name', 'name': 'the name of the team'}
        texts = {'': 'Give {}.', 'n' * 81: 'Use at most 80 characters for {}.', '\ud800': 'Use only valid text for {}.',
                 'Ada\nL.': 'Put {} on one line.', 'Ada\u2028L.': 'Put {} on one line.',
                 'Ada\x00': 'Remove the hidden characters from {}.', 'Ada\tL.': 'Remove the hidden characters from {}.',
                 'Ada\u202eL.': 'Remove the hidden characters from {}.', 'Ada\u200b': 'Remove the hidden characters from {}.',
                 ' Ada': 'Remove the space at the start or end of {}.', 'Ada\u00a0': 'Remove the space at the start or end of {}.'}
        for field in noun:
            for value, text in texts.items():
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.refusal(lambda: self.create(**{field: value})), (400, text.format(noun[field])))
        self.assertEqual(self.refusal(lambda: self.create(name=7)), (400, 'Give the name of the team.'))
        # A person can have an emoji with a joiner, a flag with tag characters, and a character that can be new to
        # the Python of the Mac. The name of a team follows the rule of a link, which has no format character.
        for name in (CODER, 'Ada \U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f',
                     'Ada \U0001fae9', 'n' * 80):
            with self.subTest(name=name):
                self.assertEqual([member['name'] for member in self.create(member_name=name)['members']], [name])
        self.assertEqual(self.refusal(lambda: self.create(name='Example \U0001f469\u200d\U0001f4bb Team')),
                         (400, 'Remove the hidden characters from the name of the team.'))
        self.assertEqual(self.create(name='Example Team \U0001f965')['name'], 'Example Team \U0001f965')
        for fields, text in (({'avatar': 'a' * 81}, 'Use text of at most 80 characters for the avatar.'),
                             ({'color': '\ud800'}, 'Use text of at most 20 characters for the color.')):
            self.assertEqual(self.refusal(lambda: self.create(**fields)), (400, text))

    def changed(self, service, step) -> bool:
        """Run a step, and say whether GET /v1/updates then has `team_changed`."""
        cursor = service._cursor()
        step()
        return service.updates(cursor, 0, collection_deltas=True).get('team_changed', False)

    def test_the_app_learns_each_change_of_the_team_screen(self):
        room = {}
        bot = self.bot(self.first, 'Reporter')
        steps = [
            ('this Mac makes a room', self.first, lambda: room.update(self.create()), True),
            ('nothing changed', self.first, self.first.team_sync_pass, False),
            ('this Mac joins', self.second, lambda: self.join(self.second, self.invite(room)['link']), True),
            ('a member joins', self.first, self.first.team_sync_pass, True),
            ('this Mac reads the members of the team', self.second, self.second.team_sync_pass, True),
            ('nothing changed', self.first, self.first.team_sync_pass, False),
            ('nothing changed', self.second, self.second.team_sync_pass, False),
            ('this Mac changes its name', self.first, lambda: self.rename(self.first, room, member_name='Ada L.'), True),
            ('a member changes its name', self.second, self.second.team_sync_pass, True),
            ('the same name again', self.first, lambda: self.rename(self.first, room, member_name='Ada L.'), False),
            ('this Mac shares the first page of a bot', self.first, lambda: self.share(self.first, room, bot), True),
            ('the same page again', self.first, lambda: self.share(self.first, room, bot), False),
            ('a second page of the bot', self.first, lambda: self.share_page(self.first, room['room'], bot,
                                                                           'second-page'), False),
            ('the pass writes the bot', self.first, self.first.team_sync_pass, False),
            ('a bot enters the team', self.second, self.second.team_sync_pass, True),
            ('this Mac stops one page', self.first, lambda: self.stop_page(self.first, room['room'], bot,
                                                                          'second-page'), False),
            ('this Mac stops the last page of the bot', self.first, lambda: self.stop_page(
                self.first, room['room'], bot), True),
            ('the pass deletes the bot entry', self.first, self.first.team_sync_pass, False),
            ('a bot leaves the team', self.second, self.second.team_sync_pass, True),
            ('this Mac leaves', self.second, lambda: self.leave(self.second, room), True),
            ('a member leaves', self.first, self.first.team_sync_pass, True),
            ('the room becomes gone', self.first, lambda: (self.relay.rooms.clear(), self.first.team_sync_pass()), True),
            ('nothing changed', self.first, self.first.team_sync_pass, False),
        ]
        for name, service, step, expected in steps:
            with self.subTest(step=name):
                self.assertEqual(self.changed(service, step), expected)
        # A room becomes removed, and a join that the relay does not answer adds a room that is joining.
        room.update(self.create(self.third))
        link = self.invite(room, self.third)['link']
        self.relay.down = True
        self.assertTrue(self.changed(self.second, lambda: self.refusal(self.join, self.second, link)))
        self.relay.down = False
        self.assertTrue(self.changed(self.second, lambda: self.join(self.second, link)))
        del self.relay.rooms[room['room']]['members'][self.rooms(self.second, sync=False)[0]['member']]
        self.assertTrue(self.changed(self.second, self.second.team_sync_pass))
        self.assertEqual(self.rooms(self.second, sync=False)[0]['state'], 'removed')

    def test_an_unknown_team_route_is_not_found(self):
        for method, path in (('GET', '/v1/team/unknown'), ('PUT', '/v1/team/rooms'), ('GET', '/v1/team/join'),
                             ('POST', '/v1/team/rooms/' + 'a' * 64), ('GET', '/v1/team/rooms/' + 'a' * 64 + '/invites')):
            with self.subTest(path=path):
                self.assertEqual(self.refusal(self.first.team_request, method, path, {}), (404, 'This route was not found.'))


if __name__ == '__main__':
    unittest.main()
