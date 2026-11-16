"""Direct messages: the private chat between two people of a team (coconut/team_directs.py, docs/team-log.md).

Ada (the first Mac) makes the room, and Grace (the second Mac) and Lin (the third Mac) join it. Each Mac is a Service
with FakeRelay and FakeHelper (team_fakes.py). A fake box opens only for its own pair, as a box of the real helper
does. The tests of the format check the real cryptography against the vectors of the contract, and
scripts/test_team_log.py runs the same path through the real relay in Miniflare and the real helper.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import unittest
from uuid import uuid4

from coconut import team_store
from coconut.push import DIRECT_BODY
from coconut.push_relay import group_text
from coconut.server import APIServer
from coconut.team import PERSON_ONLY
from coconut.team_directs import BOT_ONLY, LEFT, NOT_HERE, NOT_ON, NOT_YET, SELF, TEAM_ONLY
from coconut.team_relay import TeamRelayError
from team_fakes import DEVICE, TeamMacs
from test_push import RecordingTransport, pair_phone
from test_push_relay import EncryptedTransport
from test_team_only import TeamOnlyMacs
from test_team_push import ROUTE, flush, register


class DirectMacs(TeamMacs):
    def setUp(self):
        super().setUp()
        self.room = self.create()['room']
        for service, name in ((self.second, 'Grace'), (self.third, 'Lin')):
            self.join(service, self.invite({'room': self.room})['link'], name)
        self.macs = (self.first, self.second, self.third)
        self.sync()
        self.ada, self.grace, self.lin = (service.team_store.room(self.room)['member'] for service in self.macs)

    def sync(self, *services):
        """Two passes on each Mac: the first writes, the second reads what the others wrote."""
        for _ in range(2):
            for service in services or self.macs:
                service.team_sync_pass()

    def ask(self, service, device: str = DEVICE) -> dict:
        """The read of the rooms by an app that knows direct messages. The room of the test."""
        return service.team_request('GET', '/v1/team/rooms', {}, '', device, True)['rooms'][0]

    def turn_on(self, *services):
        """The app of each of these Macs asks for direct messages, and each Mac reads the new `member` entries."""
        for service in services:
            self.ask(service)
        self.sync()

    def path(self, member: str, rest: str = 'messages') -> str:
        return f'/v1/team/rooms/{self.room}/directs/{member}/{rest}'

    def send(self, service, member: str, text: str, key: str = '', **fields) -> dict:
        return self.phone(service)('POST', self.path(member), dict(fields, text=text), key)['message']

    def chat(self, service, member: str) -> list[tuple[str, bool]]:
        """The direct chat of a Mac with a member, newest first, as text and `this_mac`."""
        page = self.phone(service)('GET', self.path(member), {})
        return [(message['text'], message['this_mac']) for message in page['messages']]

    def member_entry(self, service, member: str) -> dict:
        """The payload and the `rev` of the `member` entry of a member in the store of a Mac."""
        row = service.team_store.member(self.room, member)
        entry = service.team_store.entry(self.room, row['entry'])
        return dict(json.loads(entry['payload']), rev=entry['rev'])

    def count(self, service, table: str) -> int:
        return service.db.execute(f'SELECT COUNT(*) FROM {table} WHERE room=?', (self.room,)).fetchone()[0]


class WhoCanGetADirectMessage(DirectMacs, unittest.TestCase):
    def test_no_mac_writes_direct_before_an_app_asks(self):
        (room,) = self.rooms(self.second)
        self.assertNotIn('directs', room)
        self.assertTrue(all('direct' not in member for member in room['members']))
        for service in self.macs:
            self.assertNotIn('direct', self.member_entry(service, service.team_store.room(self.room)['member']))
            self.assertFalse(service.team_store.direct_on())
        # A tool on this Mac sends the header too, and nothing changes: only a paired device is an app of the person.
        answer = self.first.team_request('GET', '/v1/team/rooms', {}, '', '', True)
        self.assertNotIn('directs', answer['rooms'][0])
        self.assertFalse(self.first.team_store.direct_on())
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.grace), {'text': 'Hello'}),
                         (409, NOT_ON))

    def test_the_first_ask_writes_the_member_entry_one_time(self):
        cursor = self.second._cursor()
        room = self.ask(self.first)
        self.assertEqual(room['directs'], [])
        self.assertTrue(self.first.team_store.direct_on())
        self.sync()
        self.assertEqual(self.member_entry(self.first, self.ada)['rev'], 2)
        self.assertIs(self.member_entry(self.second, self.ada)['direct'], True)
        # Grace's Team screen gets the change of the member.
        self.assertTrue(self.second.updates(cursor, 0, collection_deltas=True)['team_changed'])
        # More asks and passes write nothing.
        for _ in range(3):
            self.ask(self.first)
            self.sync()
        self.assertEqual(self.member_entry(self.first, self.ada)['rev'], 2)
        # A new name keeps the mark.
        self.phone(self.first)('PATCH', f'/v1/team/rooms/{self.room}', {'member_name': 'Ada L.'})
        self.assertEqual({name: value for name, value in self.member_entry(self.first, self.ada).items()
                          if name in ('name', 'direct', 'rev')}, {'name': 'Ada L.', 'direct': True, 'rev': 3})

    def test_an_update_writes_the_member_entry_within_the_wait_of_an_announce(self):
        # The service wrote its entry a moment ago, so a plain pass waits ANNOUNCE_SECONDS. The new mark does not.
        self.first._team_announced[self.room] = time.monotonic()
        self.ask(self.first)
        self.first.team_sync_pass()
        self.assertIs(self.member_entry(self.first, self.ada).get('direct'), True)

    def test_each_rule_of_who_can_get_a_message(self):
        self.turn_on(self.first, self.second)
        views = {member['member']: member['direct'] for member in self.ask(self.first)['members']}
        self.assertEqual(views, {self.ada: False, self.grace: True, self.lin: False})
        self.assertEqual(self.send(self.first, self.grace, 'Hello Grace')['text'], 'Hello Grace')
        for member, reason in ((self.lin, NOT_YET), (self.ada, SELF), ('9' * 64, LEFT)):
            with self.subTest(member=member[:4]):
                self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(member), {'text': 'Hello'}),
                                 (409, reason))
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path('Grace'), {'text': 'Hello'}),
                         (404, NOT_HERE))
        # Grace leaves. The chat keeps its history, and Ada can no longer send.
        self.leave(self.second, {'room': self.room})
        self.sync(self.first)
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.grace), {'text': 'Still there?'}),
                         (409, LEFT))
        self.assertEqual(self.chat(self.first, self.grace), [('Hello Grace', True)])
        (chat,) = self.ask(self.first)['directs']
        self.assertEqual((chat['member'], chat['member_name']), (self.grace, 'Grace'))


class TeamOnlyMacsHaveNoDirectMessages(TeamOnlyMacs, unittest.TestCase):
    def test_a_mac_for_a_team_only_never_reads_or_writes_direct(self):
        room = self.team()
        self.assertEqual(self.only.team_request('GET', '/v1/team/rooms', {}, '', '', True)['rooms'][0].get('directs'),
                         None)
        self.assertFalse(self.only._team_direct_asked())
        self.assertFalse(self.only.team_store.direct_on())
        self.sync()
        only = self.only.team_store.room(room)['member']
        payload = json.loads(self.first.team_store.entry(room, self.first.team_store.member(room, only)['entry'])['payload'])
        self.assertEqual((payload.get('bot_only'), payload.get('direct')), (True, None))
        # Each route of the direct messages answers with a sentence for a person.
        for method, rest in (('GET', 'messages'), ('POST', 'messages'), ('PATCH', 'messages/' + '1' * 32),
                             ('DELETE', 'messages/' + '1' * 32), ('POST', 'read')):
            with self.subTest(method=method, rest=rest):
                self.assertEqual(self.refusal(self.call, method, f'/v1/team/rooms/{room}/directs/{only}/{rest}',
                                              {'text': 'Hi', 'first': 1}), (409, TEAM_ONLY))
        # Ada cannot send to the Mac for a team only.
        self.first.team_request('GET', '/v1/team/rooms', {}, '', DEVICE, True)
        self.first.team_sync_pass()
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', f'/v1/team/rooms/{room}/directs/{only}/messages',
                                      {'text': 'Hello'}), (409, BOT_ONLY))


class ReadingADirectEntry(DirectMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.turn_on(*self.macs)

    def test_only_the_member_that_it_is_for_reads_it(self):
        sent = self.send(self.first, self.grace, 'Hello Grace')
        self.sync()
        self.assertEqual(self.chat(self.second, self.ada), [('Hello Grace', False)])
        self.assertEqual(self.chat(self.first, self.grace), [('Hello Grace', True)])
        (chat,) = self.ask(self.second)['directs']
        self.assertEqual(chat, {'member': self.ada, 'member_name': 'Ada', 'unread': 1,
                                'last_message': {'first': sent['first'], 'this_mac': False, 'text': 'Hello Grace',
                                                 'created_at': chat['last_message']['created_at']}})
        # Lin has the entry and shows nothing.
        self.assertEqual(self.third.team_store.entry(self.room, sent['id'])['kind'], 'direct')
        self.assertEqual((self.count(self.third, 'team_directs'), self.ask(self.third)['directs']), (0, []))
        self.assertEqual(self.chat(self.third, self.ada), [])
        # No Mac has it in the Team chat.
        for service in self.macs:
            room = self.ask(service)
            self.assertEqual((room['unread'], room['last_message']), (0, None), service.root.name)
            self.assertEqual(self.count(service, 'team_messages'), 0)
            # In the normal order no entry waits, also on Lin's Mac: it tried the entry with Ada's key.
            self.assertEqual(self.count(service, 'team_direct_waits'), 0)
            self.assertEqual(self.phone(service)('GET', f'/v1/team/rooms/{self.room}/messages', {})['messages'], [])
        # The answer has the fields of a message of the app.
        self.assertEqual(set(sent), {'id', 'first', 'this_mac', 'text', 'reply_to', 'reply', 'reactions', 'created_at',
                                     'edited_at', 'preview'})

    def test_a_mac_without_the_mark_stores_the_entry_and_opens_nothing(self):
        self.third.db.execute('DELETE FROM team_marks')
        sent = self.send(self.first, self.grace, 'Hello Grace')
        self.sync(self.third)
        self.assertEqual(self.third.team_store.entry(self.room, sent['id'])['kind'], 'direct')
        self.assertNotIn('open_direct', self.third.team_helper.calls)
        self.assertEqual(self.count(self.third, 'team_directs'), 0)

    def test_a_box_for_another_pair_or_another_member_shows_nothing(self):
        room = self.first.team_store.room(self.room)
        lin_key = self.first.team_store.member(self.room, self.lin)['agreement']
        grace_key = self.first.team_store.member(self.room, self.grace)['agreement']
        cases = {'a' * 32: (self.grace, grace_key, {'to': self.lin, 'text': 'For Lin, sealed for Grace'}),
                 'b' * 32: (self.lin, lin_key, {'to': self.grace, 'text': 'For Grace, sealed for Lin'}),
                 'c' * 32: (self.grace, grace_key, {'to': self.grace, 'text': ''}),
                 'd' * 32: (self.grace, grace_key, {'to': self.grace, 'text': 'Hi', 'reply_to': 'not an entry'})}
        secrets = json.loads((self.first.root / 'team' / f'{self.room}.json').read_text())
        with self.first._team_room_lock(self.room):
            for entry, (other, key, plaintext) in cases.items():
                box = self.first.team_helper.seal_direct(self.room, entry, secrets['agreement_key'], self.ada, other, key,
                                                         json.dumps(plaintext).encode())
                self.first._team_write(room, entry, False, {'kind': 'direct', 'box': box})
        self.sync()
        for service in (self.second, self.third):
            self.assertEqual(self.count(service, 'team_directs'), 0, service.root.name)

    def test_an_own_message_after_a_lost_store(self):
        self.send(self.first, self.grace, 'To Grace')
        self.send(self.first, self.lin, 'To Lin')
        self.send(self.second, self.ada, 'To Ada')
        self.sync()
        # The Mac loses the copy of the log, and reads it again from the start. Its member entries come in the same
        # answer as the direct entries.
        with self.first.transaction():
            for table in ('team_entries', 'team_members', 'team_directs'):
                self.first.db.execute(f'DELETE FROM {table} WHERE room=?', (self.room,))
            self.first.team_store.update_room(self.room, seq=0)
        self.first.team_sync_pass()
        self.assertEqual(self.chat(self.first, self.grace), [('To Ada', False), ('To Grace', True)])
        self.assertEqual(self.chat(self.first, self.lin), [('To Lin', True)])

    def test_a_writer_without_a_member_entry_shows_nothing(self):
        with self.second.transaction():
            self.second.db.execute('DELETE FROM team_members WHERE room=? AND member=?', (self.room, self.ada))
        calls = len(self.second.team_helper.calls)
        self.send(self.first, self.grace, 'Hello Grace')
        self.sync(self.second)
        self.assertEqual(self.count(self.second, 'team_directs'), 0)
        self.assertNotIn('open_direct', self.second.team_helper.calls[calls:])

    def test_an_edit_a_removal_and_a_write_that_no_longer_opens(self):
        sent = self.send(self.first, self.grace, 'Hello Grace')
        self.sync()
        message = f'messages/{sent["id"]}'
        edited = self.phone(self.first)('PATCH', self.path(self.grace, message), {'text': 'Hello, Grace'})['message']
        self.assertEqual((edited['first'], edited['text']), (sent['first'], 'Hello, Grace'))
        self.sync(self.second)
        (row,) = self.phone(self.second)('GET', self.path(self.ada), {})['messages']
        self.assertEqual((row['text'], row['first']), ('Hello, Grace', sent['first']))
        self.assertIsNotNone(row['edited_at'])
        # The same text writes nothing.
        writes = len([request for request in self.relay.requests if request[0] == 'write'])
        self.phone(self.first)('PATCH', self.path(self.grace, message), {'text': 'Hello, Grace'})
        self.assertEqual(len([request for request in self.relay.requests if request[0] == 'write']), writes)
        # Only the writer changes a message.
        self.assertEqual(self.refusal(self.phone(self.second), 'PATCH', self.path(self.ada, message), {'text': 'No'})[0],
                         403)
        self.assertEqual(self.refusal(self.phone(self.first), 'PATCH', self.path(self.grace, f'messages/{"e" * 32}'),
                                      {'text': 'No'})[0], 404)
        # The removal reaches Grace, and a repeat gives the same answer.
        removed = {'room': self.room, 'member': self.grace, 'id': sent['id'], 'deleted': True}
        self.assertEqual(self.phone(self.first)('DELETE', self.path(self.grace, message), {}), removed)
        self.assertEqual(self.phone(self.first)('DELETE', self.path(self.grace, message), {}), removed)
        self.sync(self.second)
        self.assertEqual(self.chat(self.second, self.ada), [])
        # A new write of an entry that no longer opens removes the message on each Mac.
        later = self.send(self.first, self.grace, 'Later')
        self.sync(self.second)
        self.assertEqual(self.chat(self.second, self.ada), [('Later', False)])
        room = self.first.team_store.room(self.room)
        with self.first._team_room_lock(self.room):
            self.first._team_write(room, later['id'], False, {'kind': 'direct', 'box': 'AAAA'})
        self.sync(self.second)
        self.assertEqual((self.chat(self.first, self.grace), self.chat(self.second, self.ada)), ([], []))

    def test_a_reply_quotes_a_message_of_the_same_chat(self):
        first = self.send(self.first, self.grace, 'Is the report ready?')
        self.sync()
        reply = self.send(self.second, self.ada, 'Yes', reply_to=first['id'])
        self.assertEqual(reply['reply'], {'id': first['id'], 'this_mac': False, 'text': 'Is the report ready?'})
        self.sync()
        (shown, _) = self.phone(self.first)('GET', self.path(self.grace), {})['messages']
        self.assertEqual((shown['reply_to'], shown['reply']['this_mac']), (first['id'], True))
        # A message of another chat is not a message that this chat can answer.
        other = self.send(self.second, self.lin, 'Hello Lin')
        self.assertEqual(self.refusal(self.phone(self.second), 'POST', self.path(self.ada),
                                      {'text': 'No', 'reply_to': other['id']})[0], 409)
        self.assertEqual(self.refusal(self.phone(self.second), 'POST', self.path(self.ada),
                                      {'text': 'No', 'reply_to': 'x'})[0], 400)


class AMemberEntryLaterInTheRead(DirectMacs, unittest.TestCase):
    """The relay gives a replaced `member` entry at the `seq` of its newest write. So a Mac that reads a room from an
    earlier position meets a direct entry before the `member` entry that its key needs. The entry waits, and the end
    of the read tries it one more time."""

    def setUp(self):
        super().setUp()
        self.turn_on(*self.macs)

    def rename(self, service, name):
        """A new name writes the `member` entry of the Mac again, at a new `seq` behind the earlier entries."""
        self.phone(service)('PATCH', f'/v1/team/rooms/{self.room}', {'member_name': name})

    def rewind(self, service):
        """The store of the Mac is behind the relay, for example after it was put back from an older copy."""
        with service.transaction():
            for table in ('team_entries', 'team_members', 'team_directs'):
                service.db.execute(f'DELETE FROM {table} WHERE room=?', (self.room,))
            service.team_store.update_room(self.room, seq=0)

    def read(self, service) -> bool:
        """One answer of the relay, without the end of the read."""
        with service._team_room_lock(self.room):
            return service._team_read(self.room)

    def test_a_direct_entry_before_the_member_entry_of_its_key_shows_after_the_read(self):
        phone = pair_phone(self.first)
        register(self.first, phone, level='every')
        self.send(self.first, self.grace, 'To Grace')
        self.send(self.second, self.ada, 'To Ada')
        self.rename(self.second, 'Grace H.')
        for page in (256, 1):
            with self.subTest(page=page):
                self.relay.page = page
                self.rewind(self.first)
                cursor = self.first._cursor()
                # During the read, both entries wait for the `member` entry of Grace.
                while self.read(self.first):
                    pass
                self.assertEqual(self.count(self.first, 'team_direct_waits'), 2)
                self.assertEqual(self.chat(self.first, self.grace), [])
                self.first.team_sync_pass()
                self.assertEqual(self.chat(self.first, self.grace), [('To Ada', False), ('To Grace', True)])
                self.assertEqual(self.count(self.first, 'team_direct_waits'), 0)
                self.assertEqual(self.first.updates(cursor, 0, collection_deltas=True)['team_directs_changed'],
                                 [{'team': self.room, 'member': self.grace}])
                # The message of Grace is history now, so it notifies nothing.
                self.assertEqual(self.first.db.execute("SELECT COUNT(*) FROM push_items WHERE kind='direct'").fetchone()[0],
                                 0)

    def test_an_entry_that_is_not_for_this_mac_does_not_wait_after_its_try(self):
        self.send(self.first, self.grace, 'To Grace')
        self.rename(self.first, 'Ada L.')
        self.rewind(self.third)
        while self.read(self.third):
            pass
        self.assertEqual(self.count(self.third, 'team_direct_waits'), 1)
        calls = self.third.team_helper.calls.count('open_direct')
        self.third.team_sync_pass()
        self.assertEqual(self.third.team_helper.calls.count('open_direct'), calls + 1)
        self.assertEqual((self.count(self.third, 'team_direct_waits'), self.count(self.third, 'team_directs')), (0, 0))
        # A later pass tries it no more.
        self.third.team_sync_pass()
        self.assertEqual(self.third.team_helper.calls.count('open_direct'), calls + 1)

    def test_a_writer_that_never_gets_a_member_entry_ends_the_wait(self):
        with self.third.transaction():
            self.third.db.execute('DELETE FROM team_members WHERE room=? AND member=?', (self.room, self.ada))
        self.send(self.first, self.grace, 'To Grace')
        self.read(self.third)
        self.assertEqual(self.count(self.third, 'team_direct_waits'), 1)
        self.third.team_sync_pass()
        self.assertEqual((self.count(self.third, 'team_direct_waits'), self.count(self.third, 'team_directs')), (0, 0))

    def test_a_delete_over_a_waiting_entry_ends_the_wait(self):
        with self.second.transaction():
            self.second.db.execute('DELETE FROM team_members WHERE room=? AND member=?', (self.room, self.ada))
        sent = self.send(self.first, self.grace, 'To Grace')
        self.read(self.second)
        self.assertEqual(self.count(self.second, 'team_direct_waits'), 1)
        self.phone(self.first)('DELETE', self.path(self.grace, f'messages/{sent["id"]}'), {})
        self.read(self.second)
        self.assertEqual(self.count(self.second, 'team_direct_waits'), 0)

    def test_a_leave_removes_the_waits(self):
        with self.second.transaction():
            self.second.db.execute('DELETE FROM team_members WHERE room=? AND member=?', (self.room, self.ada))
        self.send(self.first, self.grace, 'To Grace')
        self.read(self.second)
        self.leave(self.second, {'room': self.room})
        self.assertEqual(self.count(self.second, 'team_direct_waits'), 0)


class EventsAndReadPosition(DirectMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.turn_on(*self.macs)

    def changes(self, service, cursor) -> dict:
        return service.updates(cursor, 0, collection_deltas=True)

    def test_a_change_of_a_direct_chat_adds_its_own_event(self):
        cursor = self.second._cursor()
        sent = self.send(self.first, self.grace, 'Hello Grace')
        self.sync(self.second)
        answer = self.changes(self.second, cursor)
        # The type is known, so the app reads only what changed.
        self.assertNotIn('reset', answer)
        self.assertEqual(answer['team_directs_changed'], [{'team': self.room, 'member': self.ada}])
        self.assertEqual((answer['team_messages_changed'], answer['team_changed']), ([], False))
        # A pass that changes nothing adds no event.
        cursor = self.second._cursor()
        self.sync(self.second)
        self.assertEqual(self.second._cursor(), cursor)
        # The read position, a new name, an edit and a removal each add the event.
        for step in (lambda: self.phone(self.second)('POST', self.path(self.ada, 'read'), {'first': sent['first']}),
                     lambda: self.phone(self.first)('PATCH', f'/v1/team/rooms/{self.room}', {'member_name': 'Ada L.'}),
                     lambda: self.phone(self.first)('PATCH', self.path(self.grace, f'messages/{sent["id"]}'),
                                                    {'text': 'Hello, Grace'}),
                     lambda: self.phone(self.first)('DELETE', self.path(self.grace, f'messages/{sent["id"]}'), {})):
            cursor = self.second._cursor()
            step()
            self.sync(self.second)
            self.assertEqual(self.changes(self.second, cursor)['team_directs_changed'],
                             [{'team': self.room, 'member': self.ada}])
        # Lin's Mac had nothing to show, so it has no event.
        self.assertEqual(self.third.db.execute("SELECT COUNT(*) FROM events WHERE type='team_directs.updated'").fetchone()[0],
                         0)

    def test_the_unread_count_and_the_read_position(self):
        sent = [self.send(self.first, self.grace, f'Message {number}') for number in range(3)]
        self.send(self.second, self.ada, 'Mine')
        self.sync(self.second)
        read = self.path(self.ada, 'read')
        self.assertEqual(self.ask(self.second)['directs'][0]['unread'], 3)
        self.assertEqual(self.phone(self.second)('POST', read, {'first': sent[1]['first']}),
                         {'room': self.room, 'member': self.ada, 'read': sent[1]['first'], 'unread': 1})
        # It never moves back, and never past the newest message of the chat.
        self.assertEqual(self.phone(self.second)('POST', read, {'first': 1})['read'], sent[1]['first'])
        newest = self.second.team_store.directs(self.room, self.ada, None, 1)[0]['first']
        self.assertEqual(self.phone(self.second)('POST', read, {'first': 10 ** 9})['read'], newest)
        self.assertEqual(self.refusal(self.phone(self.second), 'POST', read, {'first': 'x'})[0], 400)
        # The position of another chat stays at 0, and the count stops at 99.
        self.assertEqual(self.second.team_store.direct_read_position(self.room, self.lin), 0)
        with self.second.transaction():
            for number in range(120):
                self.second.db.execute('''INSERT INTO team_directs (room, entry, first, writer, member, text, reply_to,
                    created_at, updated_at, edited) VALUES (?, ?, ?, ?, ?, 'x', NULL, 'now', 'now', 0)''',
                                       (self.room, f'{number:032x}', 1000 + number, self.lin, self.lin))
        self.assertEqual(self.second.team_store.direct_unread(self.room, self.lin), 99)

    def test_a_store_of_an_older_version_gets_the_column(self):
        db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(db.close)
        db.execute('''CREATE TABLE team_members (room TEXT NOT NULL, member TEXT NOT NULL, name TEXT NOT NULL,
            avatar TEXT NOT NULL, color TEXT NOT NULL, agreement TEXT NOT NULL, entry TEXT NOT NULL,
            bot_only INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (room, member))''')
        db.execute("INSERT INTO team_members VALUES ('r', 'm', 'Ada', '', '', 'k', 'e', 0)")
        db.executescript(team_store.SCHEMA)
        store = team_store.TeamStore(db)
        store.migrate()
        self.assertEqual(store.member('r', 'm')['direct'], 0)


class Routes(DirectMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.turn_on(self.first, self.second)

    def test_only_a_paired_device_reaches_the_routes_on_a_persons_mac(self):
        for method, rest in (('GET', 'messages'), ('POST', 'messages'), ('PATCH', 'messages/' + '1' * 32),
                             ('DELETE', 'messages/' + '1' * 32), ('POST', 'read')):
            with self.subTest(method=method, rest=rest):
                self.assertEqual(self.refusal(self.first.team_request, method, self.path(self.grace, rest),
                                              {'text': 'Hi'}, '', ''), (403, PERSON_ONLY))
        self.assertEqual(self.refusal(self.phone(self.first), 'PUT', self.path(self.grace), {})[0], 404)
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', f'/v1/team/rooms/{"f" * 64}/directs/'
                                      f'{self.grace}/messages', {'text': 'Hi'})[0], 404)
        for body in ({}, {'text': ' '}, {'text': 'x' * 16001}, {'text': '\ud800'}):
            self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.grace), body)[0], 400)
        for query in ({'limit': '0'}, {'limit': '101'}, {'before': 'x'}):
            self.assertEqual(self.refusal(self.phone(self.first), 'GET', self.path(self.grace), query)[0], 400)

    def test_a_page_of_the_chat(self):
        sent = [self.send(self.first, self.grace, f'Message {number}') for number in range(5)]
        page = self.phone(self.first)('GET', self.path(self.grace), {'limit': '2'})
        self.assertEqual((page['room'], page['member'], [row['id'] for row in page['messages']], page['before']),
                         (self.room, self.grace, [sent[4]['id'], sent[3]['id']], sent[3]['first']))
        page = self.phone(self.first)('GET', self.path(self.grace), {'before': str(sent[1]['first'])})
        self.assertEqual(([row['id'] for row in page['messages']], page['before']), ([sent[0]['id']], None))

    def test_a_repeat_with_the_same_key_sends_one_message(self):
        key = str(uuid4())
        first = self.send(self.first, self.grace, 'Once', key)
        self.assertEqual(self.send(self.first, self.grace, 'Once', key), first)
        # A write that reaches the relay without an answer: the repeat reads it back and sends nothing more.
        write = self.relay.write

        def lost(*arguments, **options):
            write(*arguments, **options)
            raise TeamRelayError(0, 'The relay did not answer.')
        self.relay.write = lost
        key = str(uuid4())
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.grace), {'text': 'Lost'}, key)[0], 503)
        self.relay.write = write
        again = self.send(self.first, self.grace, 'Lost', key)
        self.assertEqual(self.chat(self.first, self.grace), [('Lost', True), ('Once', True)])
        entries = self.relay.rooms[self.room]['entries']
        self.assertIn(again['id'], entries)
        self.sync(self.second)
        self.assertEqual(self.chat(self.second, self.ada), [('Lost', False), ('Once', False)])


class ListenerRoutes(DirectMacs, unittest.TestCase):
    """The header of an app and the token of a tool on the real main listener."""

    def setUp(self):
        super().setUp()
        self.server = APIServer(('127.0.0.1', 0), self.first)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.first.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_port}')
        self.tool = json.loads((self.first.root / 'gateway' / 'bridge.json').read_text())['token']
        self.device = self.first.pair({'code': self.first.pairing_code()})['token']

    def call(self, method, path, token, headers=None, data=None):
        import http.client
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = dict(headers or {}, Authorization=f'Bearer {token}')
        if data is not None:
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, json.load(response)

    def test_the_header_of_a_device_sets_the_mark_and_the_token_of_a_tool_does_not(self):
        direct = {'X-Coconut-Team': 'direct'}
        status, answer = self.call('GET', '/v1/team/rooms', self.tool, direct)
        self.assertEqual((status, 'directs' in answer['rooms'][0]), (200, False))
        self.assertFalse(self.first.team_store.direct_on())
        status, answer = self.call('GET', '/v1/team/rooms', self.device)
        self.assertEqual((status, 'directs' in answer['rooms'][0]), (200, False))
        self.assertFalse(self.first.team_store.direct_on())
        status, answer = self.call('GET', '/v1/team/rooms', self.device, direct)
        self.assertEqual((status, answer['rooms'][0]['directs']), (200, []))
        self.assertTrue(self.first.team_store.direct_on())
        # The token of the bridge file reaches no route of a direct chat on a person's Mac.
        self.assertEqual(self.call('GET', self.path(self.grace), self.tool), (403, {'error': PERSON_ONLY}))
        self.assertEqual(self.call('POST', self.path(self.grace), self.tool, data={'text': 'Hi'}),
                         (403, {'error': PERSON_ONLY}))
        self.assertEqual(self.call('GET', self.path(self.grace), self.device),
                         (200, {'room': self.room, 'member': self.grace, 'messages': [], 'before': None}))


class DirectPushMacs(DirectMacs):
    """Grace's Mac (`second`) has the phone."""

    def setUp(self):
        super().setUp()
        self.turn_on(*self.macs)
        self.phone_id = pair_phone(self.second)
        self.transport = RecordingTransport()
        self.second.push.transport = self.transport
        register(self.second, self.phone_id, level='grouped')

    def items(self):
        return self.second.db.execute('SELECT kind,message_id,team,status,reason FROM push_items ORDER BY created,rowid'
                                      ).fetchall()

    def payloads(self):
        return [call[2] for call in self.transport.calls]

    def seal(self):
        self.encrypted = EncryptedTransport()
        self.second.push.relay = self.encrypted
        register(self.second, self.phone_id, route=ROUTE)

    def arrive(self, text, service=None):
        """A direct message to Grace, and a pass of her Mac."""
        sent = self.send(service or self.first, self.grace, text)
        self.second.team_sync_pass()
        return sent


class WhichDirectMessageNotifies(DirectPushMacs, unittest.TestCase):
    def test_a_fresh_direct_message_of_another_member_makes_one_item(self):
        sent = self.arrive('Hello Grace')
        self.assertEqual(self.items(), [('direct', sent['id'], self.room, 'waiting', None)])
        # A pass that reads nothing new adds nothing.
        self.second.team_sync_pass()
        self.assertEqual(len(self.items()), 1)
        flush(self.second)
        (payload,) = self.payloads()
        self.assertEqual(payload['aps']['alert'], {'title': 'Coconut', 'body': DIRECT_BODY})
        self.assertEqual(payload['aps']['thread-id'], f'{self.room}:{self.ada}')
        # No `team` at the top: the app of today would open the team chat.
        self.assertEqual(payload['coconut'], {'version': 1, 'device_id': self.phone_id, 'kind': 'direct',
                                              'direct': {'team': self.room, 'member': self.ada}})
        for text in ('Hello Grace', 'Ada', 'Example Team'):
            self.assertNotIn(text, json.dumps(payload))

    def test_an_own_message_an_edit_a_removal_an_old_message_and_a_message_of_others_notify_nothing(self):
        self.send(self.second, self.ada, 'Mine')
        sent = self.arrive('First')
        self.phone(self.first)('PATCH', self.path(self.grace, f'messages/{sent["id"]}'), {'text': 'First, edited'})
        self.second.team_sync_pass()
        self.phone(self.first)('DELETE', self.path(self.grace, f'messages/{sent["id"]}'), {})
        self.second.team_sync_pass()
        self.send(self.first, self.lin, 'For Lin')
        self.second.team_sync_pass()
        self.assertEqual([item[1] for item in self.items()], [sent['id']])
        self.second.team_store.clock = lambda: time.time() + team_store.TRIGGER_SECONDS
        self.arrive('Late')
        self.assertEqual(len(self.items()), 1)

    def test_the_switch_of_the_team_does_not_decide_and_the_level_does(self):
        self.phone(self.second)('PUT', f'/v1/team/rooms/{self.room}/notifications', {'enabled': False})
        self.arrive('Still notifies')
        self.assertEqual(len(self.items()), 1)
        register(self.second, self.phone_id, level='urgent_only')
        self.assertEqual(self.items()[0][3:], ('dropped', 'Disabled'))
        self.arrive('Only urgent')
        old = pair_phone(self.second)
        register(self.second, old, token='d' * 64)
        self.arrive('Old app')
        self.assertEqual(len(self.items()), 1)

    def test_a_message_that_no_longer_needs_the_person_sends_nothing(self):
        def outcome():
            flush(self.second)
            return self.items()[-1][3:]
        sent = self.arrive('Read on the Mac')
        self.phone(self.second)('POST', self.path(self.ada, 'read'), {'first': sent['first']})
        self.assertEqual(outcome(), ('dropped', 'NoLongerRelevant'))
        sent = self.arrive('Removed')
        self.phone(self.first)('DELETE', self.path(self.grace, f'messages/{sent["id"]}'), {})
        self.second.team_sync_pass()
        self.assertEqual(outcome(), ('dropped', 'NoLongerRelevant'))
        self.arrive('Before the leave')
        self.leave(self.second, {'room': self.room})
        self.assertEqual(outcome(), ('dropped', 'NoLongerRelevant'))
        self.assertEqual(self.transport.calls, [])


class DirectAlerts(DirectPushMacs, unittest.TestCase):
    def test_the_sealed_alert_names_the_writer(self):
        self.seal()
        self.arrive('**Hello** Grace, the [report](https://example.com) is ready.')
        flush(self.second)
        (payload,) = self.encrypted.sealed
        self.assertEqual(payload['aps']['alert'], {'title': 'Ada', 'body': 'Hello Grace, the report is ready.'})
        self.assertEqual(payload['aps']['thread-id'], f'{self.room}:{self.ada}')
        self.assertNotIn('Hello', json.dumps(self.encrypted.sent))

    def test_without_a_name_for_the_writer_the_title_is_the_team(self):
        self.seal()
        self.arrive('Who wrote this?')
        with self.second.transaction():
            self.second.db.execute("DELETE FROM team_names WHERE kind='member'")
        flush(self.second)
        self.assertEqual(self.encrypted.sealed[0]['aps']['alert'], {'title': 'Example Team', 'body': 'Who wrote this?'})

    def test_a_group_of_one_direct_chat_opens_that_chat(self):
        self.arrive('One')
        self.arrive('Two')
        flush(self.second)
        (payload,) = self.payloads()
        self.assertEqual(payload['aps']['alert'], {'title': 'Coconut', 'body': '2 direct messages are waiting'})
        self.assertEqual(payload['aps']['thread-id'], f'{self.room}:{self.ada}')
        self.assertEqual(payload['coconut'], {'version': 1, 'device_id': self.phone_id, 'kind': 'group', 'count': 2,
                                              'direct': {'team': self.room, 'member': self.ada}})

    def test_a_group_of_a_team_message_and_a_direct_message_of_one_room_opens_inbox(self):
        self.phone(self.first)('POST', f'/v1/team/rooms/{self.room}/messages', {'text': 'For the team'})
        self.arrive('For Grace')
        flush(self.second)
        (payload,) = self.payloads()
        self.assertEqual(payload['aps']['alert']['body'], '1 team message and 1 direct message are waiting')
        self.assertEqual(payload['aps']['thread-id'], 'coconut-inbox')
        self.assertEqual({name: payload['coconut'][name] for name in ('kind', 'count', 'bot_id', 'decision_id')},
                         {'kind': 'group', 'count': 2, 'bot_id': None, 'decision_id': None})
        self.assertNotIn('team', payload['coconut'])
        self.assertNotIn('direct', payload['coconut'])

    def test_a_group_of_two_direct_chats_opens_inbox(self):
        self.arrive('From Ada')
        self.arrive('From Lin', service=self.third)
        flush(self.second)
        (payload,) = self.payloads()
        self.assertEqual((payload['aps']['thread-id'], payload['coconut']['count']), ('coconut-inbox', 2))
        self.assertNotIn('direct', payload['coconut'])

    def test_the_text_of_a_group(self):
        def item(kind):
            return {'kind': kind}
        self.assertEqual(group_text([item('direct')]), '1 direct message is waiting')
        self.assertEqual(group_text([item('message'), item('team'), item('direct'), item('direct')]),
                         '1 message, 1 team message and 2 direct messages are waiting')

    def test_no_log_line_has_the_text_or_a_name(self):
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, handler)
        sent = self.arrive('The private words 7d2e')
        self.phone(self.first)('PATCH', self.path(self.grace, f'messages/{sent["id"]}'), {'text': 'The private words, edited'})
        self.sync()
        flush(self.second)
        self.seal()
        self.arrive('The private words again')
        self.encrypted.result = (500, 'InternalServerError')
        flush(self.second)
        # A box that does not open writes a log line too, without its text.
        room = self.first.team_store.room(self.room)
        with self.first._team_room_lock(self.room):
            self.first._team_write(room, 'f' * 32, False, {'kind': 'direct', 'box': 'not a box'})
        self.sync()
        output = '\n'.join(logging.Formatter().format(record) for record in records)
        for text in ('private words', 'Grace', 'Ada', 'Example Team'):
            self.assertNotIn(text, output)


if __name__ == '__main__':
    unittest.main()
