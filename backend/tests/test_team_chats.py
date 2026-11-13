"""The named chats of a team (coconut/team_chats.py, docs/team-log.md "Chats of a team").

ChatStoreTests gives TeamStore.accept entries as the relay sends them, also entries that another member made to break
the rules. ChatRouteTests runs Ada's Mac (the admin), Grace's Mac and Lin's Mac with FakeRelay and FakeHelper, as the
app calls them. ChatPushTests checks the notification of a message in a named chat. scripts/test_team_log.py runs the real
helper and relay.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import unittest
from uuid import uuid4

from coconut import team_store
from coconut.team_chats import ADMIN_ONLY, GONE_CHAT, SAME_NAME, TOO_MANY
from coconut.team_store import MAX_CHATS, REFUSED, STORED, TeamStore
from test_team_key_change import KeyChangeMacs
from test_team_messages import ADMIN, AGREEMENT, MEMBER, OTHER, ROOM, START, entry_id
from test_team_push import TeamPushMacs, flush

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

    def test_an_older_service_has_no_chats_and_an_older_app_reads_the_team_chat(self):
        view = self.view(self.second)
        self.assertEqual((view['chats'], view['can_manage_chats']), ([], False))
        design = self.make()
        self.send(self.second, 'In Design', design)
        self.sync()
        # The fields of the room are the Team chat, as an app of today reads them.
        view = self.view(self.first)
        self.assertEqual((view['unread'], view['last_message']), (0, None))

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


if __name__ == '__main__':
    unittest.main()
