"""The named chats of a team (coconut/team_chats.py, docs/team-log.md "Chats of a team").

ChatStoreTests gives TeamStore.accept entries as the relay sends them, also entries that another member made to break
the rules. ChatRouteTests runs Ada's Mac (the admin), Grace's Mac and Lin's Mac with FakeRelay and FakeHelper, as the
app calls them. ChatPushTests checks the notification of a message in a named chat, and ChatBotTests a bot of a Mac for
a team only that answers in a named chat. scripts/test_team_log.py runs the real helper and relay.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from coconut import team_store, team_turns
from coconut.team_chats import ADMIN_ONLY, GONE_CHAT, SAME_NAME, TOO_MANY
from coconut.team import switch_route
from coconut.team_store import MAX_CHATS, REFUSED, STORED, TeamStore
from test_team_key_change import KeyChangeMacs
from test_team_messages import ADMIN, AGREEMENT, MEMBER, OTHER, ROOM, START, entry_id
from test_push import RecordingTransport, pair_phone
from test_team_push import TeamPushMacs, flush, register
from test_team_turns import TeamTurnMacs, reply

DESIGN, LAUNCH = entry_id(50), entry_id(51)


class ChatStoreTests(unittest.TestCase):
    """The reading rules of a `chat` entry and of a message in a named chat. MEMBER is this Mac, ADMIN the admin."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.executescript(team_store.SCHEMA)
        self.store = TeamStore(self.db)
        self.store.migrate()
        self.store.add_room(room=ROOM, relay='https://relay.example', name='Example Team', member=MEMBER, admin=ADMIN,
                            state=team_store.ACTIVE, epoch=0, profile='{}', created_at='2026-11-10T12:00:00Z')
        self.seq = 0

    def write(self, number: int, payload: dict, member: str = ADMIN):
        """Accept an entry whose body opens with this payload. Each write gets the next `seq`, and the `at` of the
        relay is START plus the `seq`."""
        self.seq += 1
        existing = self.store.entry(ROOM, entry_id(number))
        entry = dict(seq=self.seq, first=existing['first'] if existing else self.seq, id=entry_id(number),
                     member=member, at=START + self.seq, kept=False, body=b'')
        opened = team_store.read_entry({'epoch': 0, 'payload': json.dumps(payload).encode()})
        return self.store.accept(self.store.room(ROOM), entry, opened)

    def message(self, text: str = 'Hello', **fields) -> dict:
        return {'kind': 'message', 'rev': 1, 'at': '2026-11-10T12:00:00Z', 'text': text, **fields}

    def member(self, name: str) -> dict:
        return {'kind': 'member', 'rev': 1, 'at': '2026-11-10T12:00:00Z', 'name': name, 'avatar': '', 'color': '',
                'agreement': AGREEMENT}

    def chat(self, name: str = 'Design', rev: int = 1, **fields) -> dict:
        return {'kind': 'chat', 'rev': rev, 'at': '2026-11-10T12:00:00Z', 'name': name, **fields}

    def names(self) -> list[str]:
        return [row['name'] for row in self.store.chats(ROOM)]

    def texts(self, chat: str | None = None) -> list[str]:
        return [row['text'] for row in self.store.messages(ROOM, chat=chat)]

    def test_a_chat_entry_of_the_admin_makes_a_chat_and_one_of_another_member_does_not(self):
        self.assertEqual(self.write(50, self.chat()), (STORED, None))
        self.assertEqual(self.write(52, self.chat('Other'), OTHER), (REFUSED, 'admin'))
        (row,) = self.store.chats(ROOM)
        self.assertEqual((row['entry'], row['name'], row['created_at']),
                         (DESIGN, 'Design', team_store.relay_time(START + 1)))
        self.assertIn(ROOM, self.store.changed)

    def test_a_name_must_be_text_of_1_to_50_code_points(self):
        for rev, name in enumerate(('', 'x' * 51, 5, None, '\ud800'), start=1):
            with self.subTest(name=name):
                self.assertEqual(self.write(50, self.chat(name, rev)), (STORED, None))
                self.assertEqual(self.names(), [])
        # A reader takes each valid text, also one that the writer of this Mac would refuse.
        self.write(50, self.chat(' x' * 25, 10))
        self.assertEqual(self.names(), [' x' * 25])

    def test_a_rename_keeps_the_place_and_the_time(self):
        self.write(50, self.chat('Design'))
        self.write(51, self.chat('Launch'))
        created = self.store.chat(ROOM, DESIGN)['created_at']
        self.write(50, self.chat('Brand', rev=2))
        self.assertEqual(self.names(), ['Brand', 'Launch'])
        self.assertEqual(self.store.chat(ROOM, DESIGN)['created_at'], created)

    def test_a_message_shows_only_in_its_chat_and_only_while_the_chat_shows(self):
        self.write(1, self.message('Before the chat', chat=DESIGN), OTHER)
        self.assertEqual((self.texts(), self.texts(DESIGN)), ([], []), 'The chat entry comes later.')
        self.write(50, self.chat())
        self.write(2, self.message('In the Team chat'), OTHER)
        self.write(3, self.message('No such chat', chat=LAUNCH), OTHER)
        self.assertEqual(self.texts(), ['In the Team chat'])
        self.assertEqual(self.texts(DESIGN), ['Before the chat'])
        self.assertEqual(self.texts(LAUNCH), [])
        # A `chat` that is not an entry ID makes the message not valid.
        self.write(4, self.message('Bad', chat='design'), OTHER)
        self.assertIsNone(self.store.message(ROOM, entry_id(4)))
        self.assertEqual(self.store.message(ROOM, entry_id(1))['chat'], DESIGN)

    def test_a_removal_takes_the_chat_its_messages_its_read_position_and_its_triggers(self):
        self.store.answers = True
        self.write(50, self.chat())
        self.write(1, self.message('Kept', chat=DESIGN), OTHER)
        self.write(2, self.message('Team chat'), OTHER)
        self.store.mark_read(ROOM, 1, DESIGN)
        self.db.execute('INSERT INTO team_triggers (room, bot, entry) VALUES (?, ?, ?)', (ROOM, 'bot', entry_id(1)))
        self.store.changed.clear()
        self.write(50, {'kind': 'delete', 'rev': 2, 'at': '2026-11-10T12:00:00Z'})
        self.assertEqual(self.names(), [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM team_messages WHERE chat=?', (DESIGN,)).fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM team_chat_reads').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM team_triggers').fetchone()[0], 0)
        self.assertEqual(self.texts(), ['Team chat'])
        self.assertIn(ROOM, self.store.changed)
        # A message for the removed chat that comes later shows nothing.
        self.write(3, self.message('Late', chat=DESIGN), OTHER)
        self.assertIsNone(self.store.message(ROOM, entry_id(3)))

    def test_the_read_position_the_unread_count_and_a_mention_are_for_each_chat(self):
        self.write(1, self.member('Grace'), MEMBER)
        mention = entry_id(1)
        self.write(2, self.message('Team chat'), OTHER)
        self.write(50, self.chat())
        self.write(3, self.message('Design 1', chat=DESIGN), OTHER)
        self.write(4, self.message('@Grace design', chat=DESIGN, mentions=[mention]), OTHER)
        self.assertEqual((self.store.unread(ROOM, MEMBER), self.store.unread(ROOM, MEMBER, DESIGN)), (1, 2))
        self.assertIsNone(self.store.mention_first(ROOM, MEMBER))
        self.assertEqual(self.store.mention_first(ROOM, MEMBER, DESIGN), 5)
        # The position of a named chat starts at the `first` of its `chat` entry.
        self.assertEqual(self.store.read_position(ROOM, DESIGN), 3)
        self.assertEqual(self.store.mark_read(ROOM, 99, DESIGN), 5, 'Never past the newest message of the chat.')
        self.assertEqual((self.store.unread(ROOM, MEMBER), self.store.unread(ROOM, MEMBER, DESIGN)), (1, 0))
        self.assertIsNone(self.store.mention_first(ROOM, MEMBER, DESIGN))
        self.assertEqual(self.store.read_position(ROOM), 1, 'The Team chat keeps its own position.')
        self.assertIsNone(self.store.mark_read(ROOM, 4, DESIGN), 'The position never moves back.')

    def test_a_message_before_its_chat_does_not_count_as_unread(self):
        self.write(1, self.message('Early', chat=DESIGN), OTHER)
        self.write(50, self.chat())
        self.write(2, self.message('Later', chat=DESIGN), OTHER)
        self.assertEqual(self.store.unread(ROOM, MEMBER, DESIGN), 1)

    def test_typing_is_in_one_chat_at_a_time(self):
        self.write(50, self.chat())
        now = START / 1000
        self.store.clock = lambda: now
        self.write(60, {'kind': 'typing', 'rev': 1, 'at': '2026-11-10T12:00:00Z', 'on': True, 'chat': DESIGN}, OTHER)
        self.assertEqual([member for member, _, _ in self.store.typing_now(ROOM, DESIGN)], [OTHER])
        self.assertEqual(self.store.typing_now(ROOM), [])
        self.write(60, {'kind': 'typing', 'rev': 2, 'at': '2026-11-10T12:00:00Z', 'on': True}, OTHER)
        self.assertEqual(self.store.typing_now(ROOM, DESIGN), [])
        self.assertEqual(len(self.store.typing_now(ROOM)), 1)
        # A `chat` that is not an entry ID makes the entry not valid.
        self.assertIsNone(team_store.typing_fields({'on': True, 'chat': 'design'}))

    def test_a_write_again_changes_nothing(self):
        self.write(50, self.chat())
        self.store.changed.clear()
        before = self.store.chat(ROOM, DESIGN)
        self.write(50, self.chat(rev=2))
        self.assertEqual(self.store.chat(ROOM, DESIGN), before)
        self.assertNotIn(ROOM, self.store.changed)
        self.assertIn('chat', team_store.WRITTEN_AGAIN)

    def older_store(self, rows, read: int | None = None) -> sqlite3.Connection:
        """A store of a version without named chats: its `team_messages` has no `chat`, it kept each `chat` entry as an
        entry of an unknown kind, and each message of a named chat in the Team chat. `rows` are (number, member, kind,
        payload as a dict or as JSON text). `read` is the read position of its Team chat."""
        db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(db.close)
        db.execute('CREATE TABLE team_messages (room TEXT NOT NULL, entry TEXT NOT NULL, first INTEGER NOT NULL, '
                   'member TEXT NOT NULL, bot TEXT, text TEXT NOT NULL, reply_to TEXT, mentions TEXT NOT NULL, '
                   'created_at TEXT NOT NULL, updated_at TEXT NOT NULL, edited INTEGER NOT NULL, '
                   'PRIMARY KEY (room, entry))')
        db.execute('CREATE INDEX team_messages_order ON team_messages (room, first)')
        db.executescript(team_store.SCHEMA)
        old = TeamStore(db)
        old.add_room(room=ROOM, relay='https://relay.example', name='Example Team', member=MEMBER, admin=ADMIN,
                     state=team_store.ACTIVE, epoch=0, profile='{}', created_at='2026-11-10T12:00:00Z')
        for seq, (number, member, kind, payload) in enumerate(rows, start=1):
            text = payload if isinstance(payload, str) else json.dumps(payload)
            db.execute('INSERT INTO team_entries (room, id, seq, first, member, kind, rev, at, kept, payload) '
                       'VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?)',
                       (ROOM, entry_id(number), seq, seq, member, kind, START + seq, text))
            if kind == 'message':
                db.execute("INSERT INTO team_messages VALUES (?, ?, ?, ?, NULL, ?, NULL, '[]', 't', 't', 0)",
                           (ROOM, entry_id(number), seq, member, json.loads(text)['text']))
        if read is not None:
            db.execute('INSERT INTO team_reads (room, first) VALUES (?, ?)', (ROOM, read))
        return db

    def older_rows(self) -> tuple:
        return ((50, ADMIN, 'chat', self.chat()), (51, OTHER, 'chat', self.chat('Not the admin')),
                (1, OTHER, 'message', self.message('Design', chat=DESIGN)),
                (2, OTHER, 'message', self.message('Team chat')), (3, OTHER, 'message', self.message('Bad', chat='x')))

    def moved(self, db) -> tuple:
        store = TeamStore(db)
        return ([row['name'] for row in store.chats(ROOM)], [row['text'] for row in store.messages(ROOM)],
                [row['text'] for row in store.messages(ROOM, chat=DESIGN)])

    def test_an_older_store_reads_its_chats_and_moves_each_message_to_its_chat(self):
        db = self.older_store(self.older_rows())
        TeamStore(db).migrate()
        self.assertEqual(self.moved(db), (['Design'], ['Team chat'], ['Design']))
        indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn('team_messages_chat', indexes)
        self.assertNotIn('team_messages_order', indexes)
        # A second start changes nothing.
        changes = db.total_changes
        TeamStore(db).migrate()
        self.assertEqual(db.total_changes, changes)
        self.assertEqual(self.moved(db), (['Design'], ['Team chat'], ['Design']))

    def test_a_stop_in_the_middle_of_the_pass_leaves_the_store_of_before_and_the_next_start_does_it(self):
        db = self.older_store(self.older_rows())
        store = TeamStore(db)

        def stop(*arguments, **options):
            raise KeyboardInterrupt('The process stops here.')
        store._show_chat = stop
        with self.assertRaises(KeyboardInterrupt):
            store.migrate()
        self.assertFalse(db.in_transaction)
        self.assertNotIn('chat', {row[1] for row in db.execute('PRAGMA table_info(team_messages)')})
        TeamStore(db).migrate()
        self.assertEqual(self.moved(db), (['Design'], ['Team chat'], ['Design']))

    def test_the_pass_reads_the_chat_of_a_message_as_the_reader_of_a_message_does(self):
        # Two keys `chat`: Python takes the last one, as the reader of a message does. SQLite would take the first.
        twice = json.dumps(self.message('Twice'))[:-1] + f', "chat": "x", "chat": "{DESIGN}"}}'
        db = self.older_store(((50, ADMIN, 'chat', self.chat()), (1, OTHER, 'message', twice)))
        TeamStore(db).migrate()
        self.assertEqual(self.moved(db), (['Design'], [], ['Twice']))

    def test_a_moved_chat_starts_at_the_read_position_of_the_team_chat(self):
        rows = ((50, ADMIN, 'chat', self.chat()), (1, OTHER, 'message', self.message('Design 1', chat=DESIGN)),
                (2, OTHER, 'message', self.message('Design 2', chat=DESIGN)),
                (3, OTHER, 'message', self.message('Team chat')),
                (51, ADMIN, 'chat', self.chat('Launch')), (4, OTHER, 'message', self.message('Launch', chat=LAUNCH)))
        # The person read the Team chat of the older version up to the message `first` 5.
        db = self.older_store(rows, read=5)
        store = TeamStore(db)
        store.migrate()
        self.assertEqual((store.unread(ROOM, MEMBER, DESIGN), store.unread(ROOM, MEMBER, LAUNCH)), (0, 1))
        # Never past the newest message of the chat.
        self.assertEqual((store.read_position(ROOM, DESIGN), store.read_position(ROOM, LAUNCH)), (3, 5))

    def test_a_message_whose_chat_comes_later_in_the_read_notifies_once(self):
        self.store.db.execute('UPDATE team_rooms SET profile=? WHERE room=?', (json.dumps({'entry': entry_id(9)}), ROOM))
        self.store.clock = lambda: START / 1000 + 60
        self.write(9, self.member('Grace'), MEMBER)
        # The admin made the chat (`first` 2) and renamed it later. The relay keeps only the newest write of the chat
        # entry, so it comes after the message.
        self.seq = 2
        self.write(1, self.message('@Grace look', chat=DESIGN, mentions=[entry_id(9)]), OTHER)
        self.assertEqual(self.store.fresh, {})
        self.seq += 1
        entry = dict(seq=self.seq, first=2, id=DESIGN, member=ADMIN, at=START + self.seq, kept=True, body=b'')
        self.store.accept(self.store.room(ROOM), entry,
                          team_store.read_entry({'epoch': 0, 'payload': json.dumps(self.chat('Brand', rev=2)).encode()}))
        self.assertEqual(self.store.fresh, {ROOM: [entry_id(1)]})
        # A rename after that notes nothing again.
        self.store.fresh.clear()
        self.write(50, self.chat('Brand 2', rev=3))
        self.assertEqual(self.store.fresh, {})

    def test_a_member_that_joins_after_the_chat_has_messages_reads_from_its_join(self):
        self.store.db.execute('UPDATE team_rooms SET profile=? WHERE room=?', (json.dumps({'entry': entry_id(9)}), ROOM))
        self.write(50, self.chat())
        self.write(1, self.message('History', chat=DESIGN), OTHER)
        self.write(9, self.member('Grace'), MEMBER)
        self.write(2, self.message('New', chat=DESIGN), OTHER)
        self.assertEqual((self.store.read_position(ROOM, DESIGN), self.store.unread(ROOM, MEMBER, DESIGN)), (3, 1))


class ChatMacs(KeyChangeMacs):
    """Ada (the admin) on the first Mac, Grace on the second Mac, Lin on the third Mac."""

    def path(self, rest: str = '') -> str:
        return f'/v1/team/rooms/{self.room}/chats' + (f'/{rest}' if rest else '')

    def make(self, name: str = 'Design', key: str = '', service=None) -> str:
        """Make a chat on a Mac, and let each Mac read it."""
        chat = self.phone(service or self.first)('POST', self.path(), {'name': name}, key)['chat']
        self.sync()
        return chat

    def chats(self, service) -> list[dict]:
        return self.view(service)['chats']

    def send(self, service, text: str, chat: str | None = None, **fields) -> dict:
        body = dict(fields, text=text, **({'chat': chat} if chat else {}))
        return self.phone(service)('POST', f'/v1/team/rooms/{self.room}/messages', body)['message']

    def page(self, service, chat: str | None = None) -> list[str]:
        data = {'chat': chat} if chat else {}
        return [message['text'] for message in
                self.phone(service)('GET', f'/v1/team/rooms/{self.room}/messages', data)['messages']]


class ChatRouteTests(ChatMacs, unittest.TestCase):
    def test_the_admin_makes_a_chat_and_each_member_reads_it(self):
        design = self.make()
        self.sync()
        for service in self.macs:
            (chat,) = self.chats(service)
            self.assertEqual({name: chat[name] for name in ('id', 'name', 'unread', 'last_message', 'mentioned',
                                                             'typing')},
                             {'id': design, 'name': 'Design', 'unread': 0, 'last_message': None, 'mentioned': False,
                              'typing': []})
            self.assertTrue(chat['created_at'].endswith('Z'))
        self.assertEqual([self.view(service)['can_manage_chats'] for service in self.macs], [True, False, False])
        # Grace writes in Design. Ada and Lin see it there, and not in the Team chat.
        self.send(self.second, 'Hello Design', design)
        self.sync()
        for service in (self.first, self.third):
            self.assertEqual(self.page(service, design), ['Hello Design'])
            self.assertEqual(self.page(service), [])
            (chat,) = self.chats(service)
            self.assertEqual((chat['unread'], chat['last_message']['text']), (1, 'Hello Design'))
            self.assertEqual(self.view(service)['unread'], 0, 'The fields of the room stay the Team chat.')
        message = self.phone(self.first)('GET', f'/v1/team/rooms/{self.room}/messages', {'chat': design})['messages'][0]
        self.assertEqual(message['chat'], design)
        # The read position of the chat.
        answer = self.phone(self.first)('POST', f'/v1/team/rooms/{self.room}/read',
                                        {'first': message['first'], 'chat': design})
        self.assertEqual((answer['read'], answer['unread']), (message['first'], 0))
        self.assertEqual(self.chats(self.first)[0]['unread'], 0)
        self.assertEqual(self.chats(self.third)[0]['unread'], 1)

    def test_a_rename_and_a_removal_reach_each_member(self):
        design = self.make()
        self.send(self.second, 'Hello', design)
        self.sync()
        self.phone(self.first)('PATCH', self.path(design), {'name': 'Brand'})
        self.sync()
        self.assertEqual([chat['name'] for chat in self.chats(self.third)], ['Brand'])
        writes = self.writes()
        self.phone(self.first)('PATCH', self.path(design), {'name': 'Brand'})
        self.assertEqual(self.writes(), writes, 'The same name writes nothing.')
        answer = self.phone(self.first)('DELETE', self.path(design), {})
        self.assertEqual((answer['chat'], answer['room']['chats']), (design, []))
        self.sync()
        for service in self.macs:
            self.assertEqual(self.chats(service), [])
            self.assertEqual(self.refusal(self.page, service, design), (404, GONE_CHAT))
            self.assertEqual(service.db.execute('SELECT COUNT(*) FROM team_messages').fetchone()[0], 0)
        # A repeat of the delete gets the same answer and writes nothing.
        writes = self.writes()
        self.assertEqual(self.phone(self.first)('DELETE', self.path(design), {})['chat'], design)
        self.assertEqual(self.writes(), writes)

    def test_the_errors_of_the_routes(self):
        design = self.make()
        self.sync()
        # Only a paired device, and only on the admin's Mac.
        self.assertEqual(self.refusal(self.first.team_request, 'POST', self.path(), {'name': 'Launch'})[0], 403)
        self.assertEqual(self.refusal(self.first.team_chat_create, self.room, {'name': 'Launch'}, '')[0], 403)
        for call in (lambda: self.make('Launch', service=self.second),
                     lambda: self.phone(self.second)('PATCH', self.path(design), {'name': 'Launch'}),
                     lambda: self.phone(self.second)('DELETE', self.path(design), {})):
            self.assertEqual(self.refusal(call), (403, ADMIN_ONLY))
        # A name that another chat has, also with other case, and a name that does not fit.
        self.assertEqual(self.refusal(self.make, 'design'), (409, SAME_NAME))
        launch = self.make('Launch')
        self.assertEqual(self.refusal(self.phone(self.first), 'PATCH', self.path(launch), {'name': 'DESIGN'}),
                         (409, SAME_NAME))
        self.phone(self.first)('PATCH', self.path(design), {'name': 'DESIGN'})
        for name, text in (('', 'Give the name of the chat.'), (' Design', None), ('a\nb', None), ('x' * 51, None)):
            status, said = self.refusal(self.make, name)
            self.assertEqual(status, 400)
            if text:
                self.assertEqual(said, text)
        # An unknown chat, and a chat in a message route.
        for chat in ('f' * 32, 'nope'):
            self.assertEqual(self.refusal(self.phone(self.first), 'PATCH', self.path(chat), {'name': 'X'}),
                             (404, GONE_CHAT))
            self.assertEqual(self.refusal(self.send, self.first, 'Hi', chat), (404, GONE_CHAT))
            self.assertEqual(self.refusal(self.phone(self.first), 'POST', f'/v1/team/rooms/{self.room}/read',
                                          {'first': 1, 'chat': chat}), (404, GONE_CHAT))
            self.assertEqual(self.refusal(self.phone(self.first), 'POST', f'/v1/team/rooms/{self.room}/typing',
                                          {'chat': chat}), (404, GONE_CHAT))
        # At most MAX_CHATS chats.
        for number in range(MAX_CHATS - 2):
            self.make(f'Chat {number}')
        self.assertEqual(self.refusal(self.make, 'One more'), (409, TOO_MANY))
        # A room that is not active.
        with self.first.transaction():
            self.first.team_store.update_room(self.room, state='gone')
        self.assertEqual(self.refusal(self.make, 'Late')[0], 409)

    def test_a_repeat_with_the_same_key_makes_one_chat(self):
        key = str(uuid4())
        first = self.phone(self.first)('POST', self.path(), {'name': 'Design'}, key)
        self.assertEqual(self.phone(self.first)('POST', self.path(), {'name': 'Design'}, key), first)
        # A repeat after a lost answer: the Mac has no saved answer, and the entry ID finds the first write.
        self.first._team_replies.clear()
        self.assertEqual(self.phone(self.first)('POST', self.path(), {'name': 'Design'}, key)['chat'], first['chat'])
        self.assertEqual(len(self.chats(self.first)), 1)

    def test_a_room_without_a_chat_and_the_fields_of_the_room_stay_the_team_chat(self):
        view = self.view(self.second)
        self.assertEqual((view['chats'], view['can_manage_chats']), ([], False))
        fields = ('unread', 'last_message', 'mentioned', 'typing')
        before = {name: view[name] for name in fields}
        design = self.make()
        self.send(self.second, 'In Design', design)
        self.phone(self.third)('POST', f'/v1/team/rooms/{self.room}/typing', {'chat': design})
        self.sync()
        # An app of today reads only these fields of the room. A message and typing in a named chat change none of
        # them, so that app shows the Team chat as before.
        view = self.view(self.second)
        self.assertEqual({name: view[name] for name in fields}, before)
        self.assertEqual(view['chats'][0]['last_message']['text'], 'In Design')

    def test_an_older_mac_shows_a_message_of_a_named_chat_in_its_team_chat(self):
        """Lin's Mac reads as a Mac of an older Coconut: it does not know the kind `chat`, and its reader of a message
        ignores the field `chat` (rule 2 of the readers of a message). Nothing gets lost."""
        design = self.make()
        self.send(self.second, 'In Design', design)
        self.sync(self.second)

        def older(fields):
            return team_store.message_fields({name: value for name, value in fields.items() if name != 'chat'})
        shown = {name: check for name, check in team_store.SHOWN.items() if name != 'chat'}
        with mock.patch.dict(team_store.SHOWN, dict(shown, message=older), clear=True):
            # Lin's Mac reads the log again from its start, as a Mac of that version.
            self.third.team_store.remove_room(self.room)
            self.third.team_store.add_room(**dict(self.first.team_store.room(self.room), member=self.lin,
                                                  seq=0, profile='{}'))
            self.third.team_sync_pass()
        self.assertEqual(self.page(self.third), ['In Design'])

    def test_a_person_who_is_not_the_admin_gets_403_before_the_check_of_the_name(self):
        self.assertEqual(self.refusal(self.make, ' bad', '', self.second), (403, ADMIN_ONLY))
        design = self.make()
        self.assertEqual(self.refusal(self.phone(self.second), 'PATCH', self.path(design), {'name': ''}),
                         (403, ADMIN_ONLY))

    def test_an_edit_of_a_message_in_a_removed_chat_writes_nothing(self):
        design = self.make()
        sent = self.send(self.second, 'Hello', design)
        self.sync()
        self.phone(self.first)('DELETE', self.path(design), {})
        self.sync()
        writes = self.writes()
        self.assertEqual(self.refusal(self.phone(self.second), 'PATCH',
                                      f'/v1/team/rooms/{self.room}/messages/{sent["id"]}', {'text': 'Changed'}),
                         (404, 'This message is no longer in the chat.'))
        self.assertEqual(self.writes(), writes)

    def test_a_reply_shows_its_quote_only_in_the_same_chat(self):
        design = self.make()
        team = self.send(self.second, 'In the Team chat')
        self.sync()
        self.assertEqual(self.refusal(lambda: self.send(self.first, 'Answer', design, reply_to=team['id']))[0], 409)
        # A writer of another Mac can still write it. The quote then stays out.
        inner = self.send(self.second, 'In Design', design)
        with self.second._team_room_lock(self.room):
            self.second._team_write(self.second.team_store.room(self.room), entry_id(99), False,
                                    {'kind': 'message', 'text': 'Across', 'chat': design, 'reply_to': team['id']})
        self.send(self.second, 'Inside', design, reply_to=inner['id'])
        self.sync()
        replies = {message['text']: message['reply'] for message in
                   self.phone(self.first)('GET', f'/v1/team/rooms/{self.room}/messages', {'chat': design})['messages']}
        self.assertIsNone(replies['Across'])
        self.assertEqual(replies['Inside']['text'], 'In Design')

    def test_typing_in_a_named_chat(self):
        design = self.make()
        self.sync()
        self.phone(self.second)('POST', f'/v1/team/rooms/{self.room}/typing', {'chat': design})
        self.first.team_sync_pass()
        view = self.view(self.first)
        self.assertEqual(view['typing'], [])
        self.assertEqual([item['name'] for item in view['chats'][0]['typing']], ['Grace'])

    def test_the_events_of_a_change_of_the_chats(self):
        cursor = self.third._cursor()
        design = self.make()
        self.sync()
        self.assertEqual(self.changes(self.third, cursor).get('team_changed'), True)
        cursor = self.third._cursor()
        self.send(self.second, 'Hello', design)
        self.sync()
        self.assertEqual(self.changes(self.third, cursor).get('team_messages_changed'), [self.room])

    def test_no_log_line_has_the_name_of_a_chat(self):
        with self.assertLogs('coconut', level='DEBUG') as logs:
            logging.getLogger('coconut').info('start')
            design = self.make('Secret Plans')
            self.send(self.second, 'Hello', design)
            self.sync()
            self.refusal(self.make, 'Secret Plans')
            self.phone(self.first)('DELETE', self.path(design), {})
            self.sync()
        for line in logs.output:
            self.assertNotIn('Secret Plans', line)


class ChatKeyChangeTests(ChatMacs, unittest.TestCase):
    def test_the_admin_writes_its_chats_again_after_a_key_change(self):
        design = self.make()
        self.sync()
        before = self.chats(self.third)
        self.remove(self.grace)
        self.sync()
        self.assertEqual(self.body_epochs(self.ada)[design], 1)
        self.assertEqual(self.first.team_store.entry(self.room, design)['rev'], 2)
        self.assertEqual(self.chats(self.third), before)


class ChatPushTests(TeamPushMacs, unittest.TestCase):
    """Grace on the first Mac is the admin and writes. Ada on the second Mac has the paired phone."""

    def make(self, name: str = 'Design') -> str:
        return self.phone(self.first)('POST', f'/v1/team/rooms/{self.room}/chats', {'name': name})['chat']

    def test_the_alert_names_the_chat_and_the_team_and_the_thread_is_the_chat(self):
        self.seal()
        design = self.make()
        self.send('Hello', chat=design)
        self.second.team_sync_pass()
        flush(self.second)
        (payload,) = self.relay.sealed
        self.assertEqual(payload['aps']['alert'], {'title': 'Grace', 'subtitle': 'Design · Example Team',
                                                   'body': 'Hello'})
        self.assertEqual(payload['aps']['thread-id'], f'{self.room}:{design}')
        self.assertEqual(payload['coconut'], {'version': 1, 'device_id': self.phone_id, 'kind': 'team',
                                              'team': self.room, 'chat': design})

    def test_a_group_of_one_chat_opens_that_chat_and_a_group_of_two_chats_opens_inbox(self):
        design = self.make()
        self.send('One', chat=design)
        self.send('Two', chat=design)
        self.second.team_sync_pass()
        flush(self.second)
        (payload,) = self.payloads()
        self.assertEqual(payload['aps']['thread-id'], f'{self.room}:{design}')
        self.assertEqual(payload['coconut'], {'version': 1, 'device_id': self.phone_id, 'kind': 'group', 'count': 2,
                                              'team': self.room, 'chat': design})
        self.send('Three', chat=design)
        self.send('Four')
        self.second.team_sync_pass()
        flush(self.second)
        self.assertEqual(self.payloads()[-1]['aps']['thread-id'], 'coconut-inbox')

    def test_a_removed_chat_and_a_read_chat_send_nothing(self):
        design = self.make()
        self.send('Gone soon', chat=design)
        self.second.team_sync_pass()
        self.assertEqual(len(self.items()), 1)
        self.phone(self.first)('DELETE', f'/v1/team/rooms/{self.room}/chats/{design}', {})
        self.second.team_sync_pass()
        flush(self.second)
        self.assertEqual(self.payloads(), [])
        launch = self.make('Launch')
        sent = self.send('Read soon', chat=launch)
        self.second.team_sync_pass()
        self.phone(self.second)('POST', f'/v1/team/rooms/{self.room}/read', {'first': sent['first'], 'chat': launch})
        flush(self.second)
        self.assertEqual(self.payloads(), [])


class ChatMuteTests(TeamPushMacs, unittest.TestCase):
    """Ada on the second Mac mutes one named chat for her phone. Grace on the first Mac is the admin and writes."""

    def setUp(self):
        super().setUp()
        members = {member['name']: member['member'] for member in self.rooms(self.first)[0]['members']}
        self.ada = members['Ada']
        self.notices = self.make('Notices')
        self.second.team_sync_pass()

    def make(self, name: str) -> str:
        return self.phone(self.first)('POST', f'/v1/team/rooms/{self.room}/chats', {'name': name})['chat']

    def path(self, chat=None):
        return f'/v1/team/rooms/{self.room}/chats/{chat or self.notices}/notifications'

    def mute(self, enabled=False, chat=None):
        return self.phone(self.second)('PUT', self.path(chat), {'enabled': enabled})

    def notified(self, text, chat=None, **fields):
        """Send a message of Grace, and return whether it has an item on the phone of Ada."""
        sent = self.send(text, chat=chat, **fields)
        self.second.team_sync_pass()
        return sent['id'] in [item[6] for item in self.items()]

    def shown(self, service):
        return {chat['id']: chat.get('notifications') for chat in self.rooms(service)[0]['chats']}

    def test_a_plain_message_of_a_muted_chat_notifies_nothing_and_a_mention_does(self):
        self.mute()
        self.assertFalse(self.notified('Build 12 passed.', chat=self.notices))
        self.assertTrue(self.notified('Ada, look at build 12.', chat=self.notices, mentions=[self.ada]))
        # The Team chat and a chat that is not muted still notify while the switch of the team is on.
        self.assertTrue(self.notified('Hello team'))
        reports = self.make('Reports')
        self.second.team_sync_pass()
        self.assertTrue(self.notified('A report', chat=reports))
        # The unread count of a muted chat does not change: it counts each message.
        chats = {chat['id']: chat for chat in self.rooms(self.second)[0]['chats']}
        self.assertEqual(chats[self.notices]['unread'], 2)

    def test_the_switch_of_the_team_off_mutes_each_chat(self):
        self.phone(self.second)('PUT', f'/v1/team/rooms/{self.room}/notifications', {'enabled': False})
        self.assertFalse(self.notified('Quiet', chat=self.notices))
        self.assertFalse(self.notified('Quiet too'))
        self.assertTrue(self.notified('Ada, for you.', chat=self.notices, mentions=[self.ada]))

    def test_a_waiting_message_drops_when_the_person_mutes_its_chat(self):
        self.assertTrue(self.notified('Waiting', chat=self.notices))
        self.assertTrue(self.notified('Ada, waiting.', chat=self.notices, mentions=[self.ada]))
        self.mute()
        flush(self.second)
        self.assertEqual([item[9:] for item in self.items()], [('dropped', 'NoLongerRelevant'), ('sent', None)])

    def test_the_route_and_the_view(self):
        self.assertEqual(self.shown(self.second), {self.notices: True})
        cursor = self.second._cursor()
        self.assertEqual(self.mute(), {'room': self.room, 'chat': self.notices, 'notifications': False})
        self.assertEqual(self.shown(self.second), {self.notices: False})
        self.assertTrue(self.second.updates(cursor, 0, collection_deltas=True).get('team_changed'))
        # The same value again writes nothing.
        cursor = self.second._cursor()
        self.mute()
        self.assertEqual(self.second._cursor(), cursor)
        # The choice never goes to the relay, and the other Mac keeps its own.
        self.assertEqual(self.shown(self.first), {self.notices: True})
        self.assertNotIn('notifications', json.dumps([self.first.team_store.entry(self.room, row[0])['payload'] for row in
                                                      self.first.db.execute('SELECT id FROM team_entries')]))
        # Each member mutes a chat, not only the admin.
        self.phone(self.first)('PUT', self.path(), {'enabled': False})
        self.assertEqual(self.shown(self.first), {self.notices: False})
        self.mute(enabled=True)
        self.assertEqual(self.shown(self.second), {self.notices: True})
        # Only a paired device calls the route: the token of the bridge file gets 403.
        self.assertEqual(self.refusal(self.second.team_request, 'PUT', self.path(), {'enabled': True}, '', '')[0], 403)
        for body in ({}, {'enabled': 'no'}, {'enabled': 0}):
            self.assertEqual(self.refusal(self.phone(self.second), 'PUT', self.path(), body)[0], 400)
        for chat in ('f' * 32, 'not-a-chat'):
            self.assertEqual(self.refusal(self.phone(self.second), 'PUT', self.path(chat), {'enabled': False}),
                             (404, GONE_CHAT))
        self.assertEqual(self.refusal(self.phone(self.second), 'PUT',
                                      f'/v1/team/rooms/{"f" * 64}/chats/{self.notices}/notifications',
                                      {'enabled': False})[0], 404)

    def test_a_message_with_a_file_in_a_muted_chat_notifies_nothing(self):
        self.mute()
        draft = self.phone(self.first)('POST', f'/v1/team/rooms/{self.room}/files',
                                       {'name': 'plan.pdf', 'type': 'application/pdf', 'content': 'eA=='})['file']
        self.assertFalse(self.notified('', chat=self.notices, files=[draft['id']]))

    def test_only_the_two_switch_routes_keep_no_saved_answer(self):
        self.assertTrue(switch_route('PUT', ['rooms', 'r', 'notifications']))
        self.assertTrue(switch_route('PUT', ['rooms', 'r', 'chats', 'c', 'notifications']))
        # A route whose last ID only is the word, another method, and another place of the word.
        self.assertFalse(switch_route('DELETE', ['rooms', 'r', 'members', 'notifications']))
        self.assertFalse(switch_route('PATCH', ['rooms', 'r', 'messages', 'notifications']))
        self.assertFalse(switch_route('PUT', ['rooms', 'r', 'pages', 'notifications']))
        self.assertFalse(switch_route('GET', ['rooms', 'r', 'notifications']))
        self.assertFalse(switch_route('PUT', ['rooms', 'r', 'directs', 'm', 'notifications']))

    def test_a_repeat_with_the_key_of_an_earlier_mute_mutes_again(self):
        """The app sends the key of a write whose answer did not arrive again with the same body. A saved answer of
        the first mute would then cancel a mute after an unmute."""
        key, other = str(uuid4()), str(uuid4())
        for path in (self.path(), f'/v1/team/rooms/{self.room}/notifications'):
            with self.subTest(path=path):
                self.phone(self.second)('PUT', path, {'enabled': False}, key)
                self.phone(self.second)('PUT', path, {'enabled': True}, other)
                self.assertEqual(self.phone(self.second)('PUT', path, {'enabled': False}, key)['notifications'], False)
                room = self.rooms(self.second)[0]
                shown = room['chats'][0]['notifications'] if '/chats/' in path else room['notifications']
                self.assertIs(shown, False)
                self.phone(self.second)('PUT', path, {'enabled': True}, other)

    def test_the_choice_goes_with_the_chat_and_with_the_team(self):
        self.mute()
        self.phone(self.first)('DELETE', f'/v1/team/rooms/{self.room}/chats/{self.notices}', {})
        self.second.team_sync_pass()
        self.assertEqual(self.second.team_store.muted_chats(self.room), set())
        self.assertEqual(self.refusal(self.phone(self.second), 'PUT', self.path(), {'enabled': False}),
                         (404, GONE_CHAT))
        design = self.make('Design')
        self.second.team_sync_pass()
        self.mute(chat=design)
        self.assertEqual(self.second.team_store.muted_chats(self.room), {design})
        self.leave(self.second, {'room': self.room})
        self.assertEqual(self.second.db.execute('SELECT COUNT(*) FROM team_chat_mutes').fetchone()[0], 0)

    def test_a_store_from_before_the_choice_has_no_muted_chat(self):
        db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(db.close)
        db.executescript(team_store.SCHEMA)
        db.execute('DROP TABLE team_chat_mutes')
        db.execute("INSERT INTO team_chats VALUES ('r', ?, 1, 'Notices', 'now')", (DESIGN,))
        db.executescript(team_store.SCHEMA)
        store = TeamStore(db)
        store.migrate()
        self.assertEqual(store.muted_chats('r'), set())
        self.assertTrue(store.mute_chat('r', DESIGN, True))
        self.assertFalse(store.mute_chat('r', DESIGN, True))
        self.assertEqual(store.muted_chats('r'), {DESIGN})


class ChatPinTests(TeamPushMacs, unittest.TestCase):
    """Ada on the second Mac pins the Team chat and a named chat. Grace on the first Mac is the admin."""

    def setUp(self):
        super().setUp()
        self.notices = self.make('Notices')
        self.second.team_sync_pass()

    def make(self, name: str) -> str:
        return self.phone(self.first)('POST', f'/v1/team/rooms/{self.room}/chats', {'name': name})['chat']

    def path(self, chat=None):
        return f'/v1/team/rooms/{self.room}' + (f'/chats/{chat}' if chat else '') + '/pin'

    def pin(self, pinned=True, chat=None, key=''):
        return self.phone(self.second)('PUT', self.path(chat), {'pinned': pinned}, key)

    def shown(self, service):
        room = self.rooms(service)[0]
        return room.get('pinned'), {chat['id']: chat.get('pinned') for chat in room['chats']}

    def test_the_route_and_the_view(self):
        self.assertEqual(self.shown(self.second), (False, {self.notices: False}))
        cursor = self.second._cursor()
        self.assertEqual(self.pin(chat=self.notices), {'room': self.room, 'chat': self.notices, 'pinned': True})
        self.assertEqual(self.pin(), {'room': self.room, 'chat': None, 'pinned': True})
        self.assertEqual(self.shown(self.second), (True, {self.notices: True}))
        self.assertTrue(self.second.updates(cursor, 0, collection_deltas=True).get('team_changed'))
        # The same value again writes nothing.
        cursor = self.second._cursor()
        self.pin(chat=self.notices)
        self.pin()
        self.assertEqual(self.second._cursor(), cursor)
        # The choice never goes to the relay, and the other Mac keeps its own.
        self.assertEqual(self.shown(self.first), (False, {self.notices: False}))
        self.assertNotIn('pinned', json.dumps([self.first.team_store.entry(self.room, row[0])['payload'] for row in
                                               self.first.db.execute('SELECT id FROM team_entries')]))
        # A pin does not touch the mute, and an unpin takes the pin away.
        self.assertIs(self.rooms(self.second)[0]['chats'][0]['notifications'], True)
        self.pin(False, chat=self.notices)
        self.pin(False)
        self.assertEqual(self.shown(self.second), (False, {self.notices: False}))
        # Only a paired device calls the route: the token of the bridge file gets 403.
        self.assertEqual(self.refusal(self.second.team_request, 'PUT', self.path(), {'pinned': True}, '', '')[0], 403)
        for body in ({}, {'pinned': 'yes'}, {'pinned': 1}):
            self.assertEqual(self.refusal(self.phone(self.second), 'PUT', self.path(self.notices), body)[0], 400)
        for chat in ('f' * 32, 'not-a-chat'):
            self.assertEqual(self.refusal(self.phone(self.second), 'PUT', self.path(chat), {'pinned': True}),
                             (404, GONE_CHAT))
        self.assertEqual(self.refusal(self.phone(self.second), 'PUT', f'/v1/team/rooms/{"f" * 64}/pin',
                                      {'pinned': True})[0], 404)

    def test_a_repeat_with_the_key_of_an_earlier_pin_pins_again(self):
        key, other = str(uuid4()), str(uuid4())
        for chat in (None, self.notices):
            with self.subTest(chat=chat):
                self.pin(True, chat, key)
                self.pin(False, chat, other)
                self.assertIs(self.pin(True, chat, key)['pinned'], True)
                room = self.rooms(self.second)[0]
                self.assertIs(room['chats'][0]['pinned'] if chat else room['pinned'], True)
                self.pin(False, chat, other)
        self.assertTrue(switch_route('PUT', ['rooms', 'r', 'pin']))
        self.assertTrue(switch_route('PUT', ['rooms', 'r', 'chats', 'c', 'pin']))
        self.assertFalse(switch_route('PUT', ['rooms', 'r', 'pages', 'pin']))

    def test_the_choice_goes_with_the_chat_and_with_the_team(self):
        self.pin(chat=self.notices)
        self.pin()
        self.phone(self.first)('DELETE', f'/v1/team/rooms/{self.room}/chats/{self.notices}', {})
        self.second.team_sync_pass()
        self.assertEqual(self.second.team_store.pinned_chats(self.room), {None})
        self.assertEqual(self.refusal(self.phone(self.second), 'PUT', self.path(self.notices), {'pinned': True}),
                         (404, GONE_CHAT))
        self.leave(self.second, {'room': self.room})
        self.assertEqual(self.second.db.execute('SELECT COUNT(*) FROM team_chat_pins').fetchone()[0], 0)

    def test_a_store_from_before_the_pin_has_no_pinned_chat(self):
        db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(db.close)
        db.executescript(team_store.SCHEMA)
        db.execute('DROP TABLE team_chat_pins')
        db.execute("INSERT INTO team_chats VALUES ('r', ?, 1, 'Notices', 'now')", (DESIGN,))
        db.executescript(team_store.SCHEMA)
        store = TeamStore(db)
        store.migrate()
        self.assertEqual(store.pinned_chats('r'), set())
        self.assertTrue(store.pin_chat('r', None, True))
        self.assertTrue(store.pin_chat('r', DESIGN, True))
        self.assertFalse(store.pin_chat('r', DESIGN, True))
        self.assertEqual(store.pinned_chats('r'), {None, DESIGN})


class ChatBotTests(TeamTurnMacs, unittest.TestCase):
    """The bot Reporter of the team-only Mac answers in the named chat where Ada names it."""

    def make(self, name: str = 'Design') -> str:
        chat = self.call('POST', f'/v1/team/rooms/{self.room}/chats', {'name': name}, service=self.first)['chat']
        self.sync()
        return chat

    def chat_of(self, chat):
        rows = self.call('GET', f'/v1/team/rooms/{self.room}/messages', {'chat': chat} if chat else {},
                         service=self.first)['messages']
        return [(row['bot_name'] or row['member_name'], row['text'], row['reply_to']) for row in reversed(rows)]

    def test_a_reply_of_an_agent_in_a_muted_chat_notifies_nothing(self):
        phone = pair_phone(self.first)
        self.first.push.transport = RecordingTransport()
        register(self.first, phone, level='grouped')
        design = self.make()
        self.call('PUT', f'/v1/team/rooms/{self.room}/chats/{design}/notifications', {'enabled': False},
                  service=self.first)
        self.send('@Reporter what is open?', mentions=[self.mention], chat=design)
        self.sync()
        self.serve(reply('3 tasks are open.'))
        self.run_turns()
        self.sync()
        self.assertIn('3 tasks are open.', [text for _, text, _ in self.chat_of(design)])
        self.assertEqual(self.first.db.execute('SELECT COUNT(*) FROM push_items').fetchone()[0], 0)
        # The same reply in a chat that is not muted makes one item.
        self.call('PUT', f'/v1/team/rooms/{self.room}/chats/{design}/notifications', {'enabled': True},
                  service=self.first)
        self.send('@Reporter and now?', mentions=[self.mention], chat=design)
        self.sync()
        self.serve(reply('2 tasks are open.'))
        self.run_turns()
        self.sync()
        self.assertEqual(self.first.db.execute('SELECT COUNT(*) FROM push_items').fetchone()[0], 1)

    def test_a_mac_for_a_team_only_has_no_mute(self):
        design = self.make()
        (room,) = self.call('GET', '/v1/team/rooms')['rooms']
        (chat,) = room['chats']
        self.assertNotIn('notifications', chat)
        self.assertNotIn('pinned', chat)
        self.assertNotIn('pinned', room)
        for path in (f'/v1/team/rooms/{self.room}/pin', f'/v1/team/rooms/{self.room}/chats/{design}/pin'):
            self.assertEqual(self.refusal(self.call, 'PUT', path, {'pinned': True})[0], 409)
        self.assertEqual(self.refusal(self.call, 'PUT', f'/v1/team/rooms/{self.room}/chats/{design}/notifications',
                                      {'enabled': False})[0], 409)

    def test_the_trigger_the_context_and_the_reply_stay_in_the_chat(self):
        design = self.make()
        self.send('Plan in the Team chat.')
        self.send('Plan in Design.', chat=design)
        named = self.send('@Reporter what is open?', mentions=[self.mention], chat=design)
        self.sync()
        turns = []
        self.serve(lambda turn: (turns.append(turn), reply('Done.')(turn))[1])
        self.run_turns()
        (turn,) = turns
        self.assertEqual(turn['chat_id'], f'{self.bot["id"]}:team:{self.room}:{design}')
        self.assertEqual(turn['chat_name'], 'Example Team · Design')
        self.assertIn('the chat Design of the team Example Team', turn['team']['rules'])
        self.assertIn('Plan in Design.', turn['team']['context'])
        self.assertNotIn('Team chat', turn['team']['context'])
        self.assertEqual(self.chat_of(design)[-1], ('Reporter', 'Done.', named['id']))
        self.assertEqual([text for _, text, _ in self.chat_of(None)], ['Plan in the Team chat.'])
        self.assertEqual(team_turns.parse_chat(turn['chat_id']), (self.room, self.bot['id'], design, None))
        self.assertEqual(team_turns.parse_chat(team_turns.chat_id(self.room, self.bot['id'])),
                         (self.room, self.bot['id'], None, None))

    def test_a_reply_to_the_bot_in_another_chat_starts_nothing(self):
        design = self.make()
        self.send('@Reporter hello', mentions=[self.mention])
        self.sync()
        self.serve(reply('Hi.'))
        self.run_turns()
        (answer,) = [row for row in self.call('GET', f'/v1/team/rooms/{self.room}/messages', {},
                                              service=self.first)['messages'] if row['bot_name'] == 'Reporter']
        # The app cannot send it, so the store of the team-only Mac gets it as another Mac would write it.
        with self.first._team_room_lock(self.room):
            self.first._team_write(self.first.team_store.room(self.room), entry_id(98), False,
                                   {'kind': 'message', 'text': 'Thanks', 'chat': design, 'reply_to': answer['id']})
        self.sync()
        self.assertEqual(self.triggers(), [])

    def test_a_removed_chat_starts_no_turn_and_takes_no_reply(self):
        design = self.make()
        self.send('@Reporter what is open?', mentions=[self.mention], chat=design)
        self.sync()
        self.assertEqual(len(self.triggers()), 1)
        self.call('DELETE', f'/v1/team/rooms/{self.room}/chats/{design}', service=self.first)
        self.sync()
        self.assertEqual(self.triggers(), [])
        # A send of Hermes into the removed chat goes nowhere.
        data = {'home': str(self.home), 'chat_id': team_turns.chat_id(self.room, self.bot['id'], design),
                'kind': 'send', 'message_id': 'outside:1', 'text': 'Late news.'}
        self.assertFalse(self.only.gateway_message(data)['recorded'])

    def test_a_send_outside_a_turn_goes_into_its_named_chat(self):
        design = self.make()
        data = {'home': str(self.home), 'chat_id': team_turns.chat_id(self.room, self.bot['id'], design),
                'kind': 'send', 'message_id': 'outside:1', 'text': 'The background job finished.'}
        self.assertEqual(self.only.gateway_message(data), {'recorded': True})
        self.sync()
        self.assertEqual(self.chat_of(design), [('Reporter', 'The background job finished.', None)])

    def test_the_typing_entry_of_the_bot_names_its_chat(self):
        design = self.make()
        self.send('@Reporter what is open?', mentions=[self.mention], chat=design)
        self.sync()

        def script(turn):
            # A commentary first, so the gateway shows the turn and the bot types.
            yield {'kind': 'send', 'message_id': f'{turn["id"]}:1', 'text': 'I look.', 'interim': True}
            time.sleep(0.5)
            yield {'kind': 'send', 'message_id': f'{turn["id"]}:2', 'text': 'Done.'}
            yield {'kind': 'done', 'outcome': 'success'}
        self.serve(script)
        self.run_turns()
        for typing in list(self.only._turns_typing.values()):
            typing.thread.join(5)
        rows = self.only.db.execute("SELECT payload FROM team_entries WHERE room=? AND kind='typing'", (self.room,))
        payloads = [json.loads(payload) for (payload,) in rows.fetchall()]
        self.assertEqual([(payload['bot'], payload['chat'], payload['on']) for payload in payloads],
                         [(self.entry, design, False)])

    def test_a_running_turn_stops_when_its_chat_goes(self):
        design = self.make()
        self.send('@Reporter please', mentions=[self.mention], chat=design)
        self.sync()
        released = threading.Event()
        self.serve(lambda turn: (released.wait(5), reply('Late answer.')(turn))[1])
        runner = threading.Thread(target=self.only._turns_run, args=(self.room, self.bot['id']), daemon=True)
        with self.only._turns_lock:
            self.only._turns_running[(self.room, self.bot['id'])] = runner
        runner.start()
        deadline = time.monotonic() + 5
        while not self.gateway.turns and time.monotonic() < deadline:
            time.sleep(.05)
        self.call('DELETE', f'/v1/team/rooms/{self.room}/chats/{design}', service=self.first)
        self.sync()
        self.only._turns_check()
        self.assertEqual(self.only.gateway_bridge.turns[self.gateway.turns[0]['id']].state, 'stopping')
        released.set()
        runner.join(5)
        self.sync()
        self.assertEqual(self.chat_of(None), [])
        self.assertEqual(self.triggers(), [])

    def test_the_route_needs_a_paired_device_on_a_team_only_mac(self):
        self.assertEqual(self.refusal(self.only.team_request, 'POST', f'/v1/team/rooms/{self.room}/chats',
                                      {'name': 'Design'})[0], 403)


if __name__ == '__main__':
    unittest.main()
