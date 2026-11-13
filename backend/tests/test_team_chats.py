"""The named chats of a team (coconut/team_store.py, docs/team-log.md "Chats of a team").

ChatStoreTests gives TeamStore.accept entries as the relay sends them, also entries that another member made to break
the rules.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from coconut import team_store
from coconut.team_store import REFUSED, STORED, TeamStore
from test_team_messages import ADMIN, AGREEMENT, MEMBER, OTHER, ROOM, START, entry_id

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

    def test_an_older_store_reads_its_chats_and_moves_each_message_to_its_chat(self):
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
        rows = ((50, ADMIN, 'chat', self.chat()), (51, OTHER, 'chat', self.chat('Not the admin')),
                (1, OTHER, 'message', self.message('Design', chat=DESIGN)),
                (2, OTHER, 'message', self.message('Team chat')), (3, OTHER, 'message', self.message('Bad', chat='x')))
        for seq, (number, member, kind, payload) in enumerate(rows, start=1):
            db.execute('INSERT INTO team_entries (room, id, seq, first, member, kind, rev, at, kept, payload) '
                       'VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?)',
                       (ROOM, entry_id(number), seq, seq, member, kind, START + seq, json.dumps(payload)))
            if kind == 'message':
                db.execute("INSERT INTO team_messages VALUES (?, ?, ?, ?, NULL, ?, NULL, '[]', 't', 't', 0)",
                           (ROOM, entry_id(number), seq, member, payload['text']))
        store = TeamStore(db)
        store.migrate()
        store.migrate()
        self.assertEqual([row['name'] for row in store.chats(ROOM)], ['Design'])
        self.assertEqual([row['text'] for row in store.messages(ROOM)], ['Team chat'])
        self.assertEqual([row['text'] for row in store.messages(ROOM, chat=DESIGN)], ['Design'])
        indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn('team_messages_chat', indexes)
        self.assertNotIn('team_messages_order', indexes)


if __name__ == '__main__':
    unittest.main()
