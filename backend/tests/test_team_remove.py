"""The admin removes a member of a team (coconut/team.py `team_remove`, docs/team-log.md, "Member removal").

Ada (the first Mac) makes the room and is its admin. Grace (the second Mac) and Lin (the third Mac) join it. Each Mac
is a Service with FakeRelay and FakeHelper (team_fakes.py). scripts/test_team_log.py runs a removal through the real
relay in Miniflare and the real helper.
"""
from __future__ import annotations

import base64
import json
import time
import unittest
from unittest import mock
from uuid import uuid4

from coconut.team import PERSON_ONLY, removal_entry
from coconut.team_directs import LEFT
from coconut.team_relay import TeamRelayError
from team_fakes import DEVICE, decode
from test_team_directs import DirectMacs
from test_team_only import TeamOnlyMacs
from test_team_turns import TeamTurnMacs


class RemovalMacs(DirectMacs):
    def remove(self, member: str, service=None, key: str = '', device: str = DEVICE) -> dict:
        return (service or self.first).team_request('DELETE', f'/v1/team/rooms/{self.room}/members/{member}', {}, key,
                                                    device)['room']

    def view(self, service) -> dict:
        return self.rooms(service)[0]

    def names_of(self, service) -> list[str]:
        return [member['name'] for member in self.view(service)['members']]

    def removal(self) -> dict | None:
        """The `remove` entry of Grace on the relay: its row and its payload."""
        token = json.loads(self.key_file(self.first, self.room).read_text())['token']
        row = self.relay.rooms[self.room]['entries'].get(removal_entry(token, self.room, self.grace))
        if row is None:
            return None
        return dict(row, payload=json.loads(base64.b64decode(decode(row['body'])['payload'])))

    def tickets(self) -> dict:
        return self.relay.rooms[self.room]['tickets']

    def changes(self, service, cursor) -> dict:
        return service.updates(cursor, 0, collection_deltas=True)


class RemovalTests(RemovalMacs, unittest.TestCase):
    def test_the_admin_removes_a_member_and_each_other_mac_drops_it(self):
        helper = self.second.mutate('POST', '/v1/bots', {'name': 'Helper'}, str(uuid4()), 'phone')
        self.share_page(self.second, self.room, helper)
        self.phone(self.second)('POST', f'/v1/team/rooms/{self.room}/messages', {'text': 'Hello team'})
        self.sync()
        library = self.third.documents()
        self.assertEqual(([page['title'] for page in library['documents'] if 'team' in page],
                          [bot['name'] for bot in library['team_bots']]), (['Weekly report'], ['Helper']))
        self.assertEqual([bot['name'] for bot in self.view(self.third)['bots']], ['Helper'])
        cursor = self.third._cursor()

        room = self.remove(self.grace)
        self.assertEqual([member['name'] for member in room['members']], ['Ada', 'Lin'])
        self.assertEqual(room['bots'], [])
        # The relay has no token of Grace, and one kept `remove` entry of the admin with her ID and her last name.
        self.assertNotIn(self.grace, self.relay.rooms[self.room]['members'])
        removal = self.removal()
        self.assertEqual((removal['member'], removal['kept']), (self.ada, True))
        self.assertEqual({name: removal['payload'][name] for name in ('kind', 'rev', 'member', 'name')},
                         dict(kind='remove', rev=1, member=self.grace, name='Grace'))

        # Lin's Mac drops Grace, her bots and her pages, and keeps the chat with her name.
        self.sync(self.third)
        self.assertEqual(self.names_of(self.third), ['Ada', 'Lin'])
        self.assertEqual(self.view(self.third)['bots'], [])
        library = self.third.documents()
        self.assertEqual(([page for page in library['documents'] if 'team' in page], library['team_bots']), ([], []))
        (message,) = self.phone(self.third)('GET', f'/v1/team/rooms/{self.room}/messages', {})['messages']
        self.assertEqual((message['text'], message['member_name']), ('Hello team', 'Grace'))
        # The events of a member that leaves: the Team screen and the Library.
        changes = self.changes(self.third, cursor)
        self.assertNotIn('reset', changes)
        self.assertTrue(changes['team_changed'])
        self.assertTrue(changes['documents_changed'])

        # Grace's Mac learns of the removal from its next request, and can read and write nothing.
        view = self.view(self.second)
        self.assertEqual((view['state'], view['can_invite'], view['bots']), ('removed', False, []))
        self.assertEqual(self.refusal(self.phone(self.second), 'POST', f'/v1/team/rooms/{self.room}/messages',
                                      {'text': 'Still here?'}), (409, 'This Mac is no longer in the team.'))
        self.assertEqual([member['can_remove'] for member in view['members']], [False] * 3)

    def test_the_admin_can_remove_each_other_member_and_nobody_else_can(self):
        admin, lin = self.view(self.first), self.view(self.third)
        self.assertEqual({member['name']: member['can_remove'] for member in admin['members']},
                         {'Ada': False, 'Grace': True, 'Lin': True})
        self.assertEqual({member['name']: member['can_remove'] for member in lin['members']},
                         {'Ada': False, 'Grace': False, 'Lin': False})
        self.assertEqual(self.refusal(lambda: self.remove(self.grace, self.third)),
                         (403, 'Only the admin of the team can remove a member.'))
        self.assertEqual(self.refusal(lambda: self.remove(self.ada)),
                         (409, 'This is your own Mac. To go out of the team, leave the team.'))
        for member in ('9' * 64, 'Grace', self.grace.upper()):
            with self.subTest(member=member[:8]):
                self.assertEqual(self.refusal(lambda: self.remove(member)), (404, 'This member is not in the team.'))
        self.assertEqual(self.names_of(self.first), ['Ada', 'Grace', 'Lin'])
        self.assertIn(self.grace, self.relay.rooms[self.room]['members'])

    def test_the_token_of_the_bridge_cannot_remove_a_member_on_a_persons_mac(self):
        self.assertEqual(self.refusal(lambda: self.remove(self.grace, device='')), (403, PERSON_ONLY))
        self.assertIn(self.grace, self.relay.rooms[self.room]['members'])

    def test_a_room_that_is_not_active_answers_409_and_shows_no_action(self):
        del self.relay.rooms[self.room]
        view = self.view(self.first)
        self.assertEqual((view['state'], [member['can_remove'] for member in view['members']]), ('gone', [False] * 3))
        self.assertEqual(self.refusal(lambda: self.remove(self.grace)), (409, 'This team no longer exists.'))

    def test_a_member_that_the_relay_no_longer_has_is_removed(self):
        # Grace left without her `delete` entry: the relay forgot her, and each Mac still shows her.
        del self.relay.rooms[self.room]['members'][self.grace]
        self.sync(self.first, self.third)
        self.assertEqual(self.names_of(self.third), ['Ada', 'Grace', 'Lin'])
        self.assertEqual([member['name'] for member in self.remove(self.grace)['members']], ['Ada', 'Lin'])
        self.sync(self.third)
        self.assertEqual(self.names_of(self.third), ['Ada', 'Lin'])

    def test_a_repeat_with_the_same_key_gets_the_same_answer(self):
        key = str(uuid4())
        first = self.remove(self.grace, key=key)
        writes = len([request for request in self.relay.requests if request[0] == 'write'])
        self.assertEqual(self.remove(self.grace, key=key), first)
        self.assertEqual(len([request for request in self.relay.requests if request[0] == 'write']), writes)

    def test_a_direct_chat_with_a_removed_member_keeps_its_history_and_takes_no_message(self):
        self.turn_on(*self.macs)
        self.send(self.first, self.grace, 'Hello Grace')
        self.sync()
        self.remove(self.grace)
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.grace), {'text': 'Still there?'}),
                         (409, LEFT))
        self.assertEqual(self.chat(self.first, self.grace), [('Hello Grace', True)])
        (chat,) = self.ask(self.first)['directs']
        self.assertEqual((chat['member'], chat['member_name']), (self.grace, 'Grace'))


class RepeatTests(RemovalMacs, unittest.TestCase):
    """Each step of a removal is safe to repeat, and a removal that stopped finishes when the admin asks again."""

    def broken(self, name: str):
        """A patch that makes one call of the relay fail with 503."""
        def failing(*arguments, **options):
            raise TeamRelayError(503, 'The relay answered 503.')
        return mock.patch.object(self.relay, name, failing)

    def test_a_removal_that_stops_after_a_step_finishes_with_a_repeat(self):
        self.invite({'room': self.room})
        tickets = dict(self.tickets())
        # Step 1 fails: nothing changed.
        with self.broken('remove_ticket'):
            self.assertEqual(self.refusal(lambda: self.remove(self.grace))[0], 503)
        self.assertEqual((self.tickets(), self.removal()), (tickets, None))
        self.assertIn(self.grace, self.relay.rooms[self.room]['members'])
        # Step 2 fails: the invite is gone, and Grace is still a member.
        with self.broken('remove_member'):
            self.assertEqual(self.refusal(lambda: self.remove(self.grace))[0], 503)
        self.assertEqual(self.tickets(), {})
        self.assertEqual(self.first.team_store.invites(self.room), [])
        self.assertIn(self.grace, self.relay.rooms[self.room]['members'])
        # Step 3 fails: the relay has no Grace, and the admin still shows her with the action.
        with self.broken('write'):
            self.assertEqual(self.refusal(lambda: self.remove(self.grace))[0], 503)
        self.assertNotIn(self.grace, self.relay.rooms[self.room]['members'])
        self.assertIsNone(self.removal())
        (grace,) = [member for member in self.view(self.first)['members'] if member['name'] == 'Grace']
        self.assertTrue(grace['can_remove'])
        # The repeat finishes.
        self.assertEqual([member['name'] for member in self.remove(self.grace)['members']], ['Ada', 'Lin'])
        self.assertEqual(self.removal()['payload']['rev'], 1)
        # A repeat after the end writes nothing new and gives the same view.
        writes = len([request for request in self.relay.requests if request[0] == 'write'])
        self.assertEqual([member['name'] for member in self.remove(self.grace)['members']], ['Ada', 'Lin'])
        self.assertEqual(len([request for request in self.relay.requests if request[0] == 'write']), writes)

    def test_a_remove_entry_that_the_relay_took_without_an_answer_is_not_written_again(self):
        write = self.relay.write

        def lost(*arguments, **options):
            write(*arguments, **options)
            raise TeamRelayError(0, 'The relay did not answer.')
        with mock.patch.object(self.relay, 'write', lost):
            self.assertEqual(self.refusal(lambda: self.remove(self.grace))[0], 503)
        self.assertEqual(self.removal()['payload']['rev'], 1)
        # The repeat reads the room first and finds the entry.
        self.assertEqual([member['name'] for member in self.remove(self.grace)['members']], ['Ada', 'Lin'])
        self.assertEqual(self.removal()['payload']['rev'], 1)

    def test_the_entry_id_of_a_removal_needs_the_token_of_the_admin(self):
        # Another member cannot know the ID, so it cannot write an entry with it first.
        token = json.loads(self.key_file(self.first, self.room).read_text())['token']
        other = json.loads(self.key_file(self.third, self.room).read_text())['token']
        self.assertRegex(removal_entry(token, self.room, self.grace), r'^[0-9a-f]{32}$')
        self.assertEqual(removal_entry(token, self.room, self.grace), removal_entry(token, self.room, self.grace))
        self.assertNotEqual(removal_entry(token, self.room, self.grace), removal_entry(other, self.room, self.grace))
        self.assertNotEqual(removal_entry(token, self.room, self.grace), removal_entry(token, self.room, self.lin))


class InviteRecordTests(RemovalMacs, unittest.TestCase):
    def test_an_invite_keeps_the_hash_of_its_ticket_and_its_end(self):
        before = int(time.time())
        invite = self.invite({'room': self.room}, ttl=600)
        records = self.first.team_store.invites(self.room)
        ticket = self.first.team_helper.ticket(self.first.team_helper.invite(invite['link'])['secret'])['hash']
        self.assertIn((ticket, invite['expires']), records)
        self.assertTrue(before + 600 <= invite['expires'] <= time.time() + 600)
        # DirectMacs made two invites for the joins of Grace and Lin. Each record names a ticket of the relay.
        self.assertEqual(len(records), 3)
        self.assertEqual({hash_ for hash_, _ in records}, set(self.tickets()))
        # A leave forgets them with the room.
        self.leave(self.second, {'room': self.room})
        self.assertEqual(self.second.team_store.invites(self.room), [])

    def test_a_removal_removes_each_open_invite_and_forgets_the_old_ones(self):
        link = self.invite({'room': self.room}, uses=3)['link']
        with self.first.transaction():
            self.first.team_store.add_invite(self.room, 'e' * 64, int(time.time()) - 1)
            # A ticket that the relay no longer has is no error.
            self.first.team_store.add_invite(self.room, 'f' * 64, int(time.time()) + 600)
        self.relay.requests.clear()
        self.remove(self.grace)
        self.assertEqual(self.tickets(), {})
        self.assertEqual(self.first.team_store.invites(self.room), [])
        # Three open tickets: the two of the joins and the new one. The old record calls no relay.
        self.assertEqual(len([request for request in self.relay.requests if request[0] == 'remove_ticket']), 4)
        # A link of before the removal no longer admits a Mac.
        fourth = self.mac('fourth')
        self.assertEqual(self.refusal(lambda: self.join(fourth, link, 'Grace')),
                         (409, 'The invite link is used, past its end or removed. Ask for a new link.'))


class TeamOnlyRemovalTests(TeamOnlyMacs, unittest.TestCase):
    def test_the_admin_removes_a_mac_for_a_team_only_with_its_bots(self):
        room = self.team()
        bot = self.declare()
        self.share(room, bot)
        self.save()
        self.sync()
        self.assertEqual(self.library()['bots'], ['Reporter'])
        (only,) = [member for member in self.rooms(self.first)[0]['members'] if member['bot_only']]
        self.assertTrue(only['can_remove'])
        view = self.first.team_request('DELETE', f'/v1/team/rooms/{room}/members/{only["member"]}', {}, '', DEVICE)['room']
        self.assertEqual(([member['name'] for member in view['members']], view['bots']), (['Ada'], []))
        self.assertEqual(self.library(), {'pages': [], 'bots': []})
        self.only.team_sync_pass()
        self.assertEqual(self.call('GET', '/v1/team/rooms')['rooms'][0]['state'], 'removed')

    def test_a_mac_for_a_team_only_that_is_an_admin_removes_with_its_token(self):
        # No person makes a team there in practice, but the route answers as on a person's Mac.
        room = self.call('POST', '/v1/team/rooms', {'name': 'Example Team', 'member_name': 'Spare Mac',
                                                     'relay': 'https://relay.example'})['room']
        link = self.call('POST', f'/v1/team/rooms/{room["room"]}/invites', {})['link']
        grace = self.join(self.second, link)['member']
        self.only.team_sync_pass()
        view = self.call('DELETE', f'/v1/team/rooms/{room["room"]}/members/{grace}')['room']
        self.assertEqual([member['name'] for member in view['members']], ['Spare Mac'])
        self.assertEqual(self.refusal(lambda: self.call('DELETE', f'/v1/team/rooms/{room["room"]}/members/{room["member"]}')),
                         (409, 'This is your own Mac. To go out of the team, leave the team.'))


class TriggerRemovalTests(TeamTurnMacs, unittest.TestCase):
    def test_a_removal_drops_the_waiting_turns_of_the_member(self):
        grace = self.join(self.second, self.invite({'room': self.room})['link'])['member']
        self.second.team_sync_pass()
        self.sync()
        self.send('@Reporter what is open?', service=self.second, mentions=[self.mention])
        self.sync()
        self.assertEqual(len(self.triggers()), 1)
        self.first.team_request('DELETE', f'/v1/team/rooms/{self.room}/members/{grace}', {}, '', DEVICE)
        self.sync()
        self.assertEqual(self.triggers(), [])


if __name__ == '__main__':
    unittest.main()
