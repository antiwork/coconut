"""The direct chat of a person with a bot of a Mac for a team only, on the person's Mac (coconut/team_directs.py,
docs/team-log.md, "Direct chat with a bot").

Ada (the first Mac) makes the room, and Grace (the second Mac) and Lin (the third Mac) join it. Lin's Mac plays the Mac
that runs the bot: the test writes its `bot` entries, its `page` entries and the messages of its bot by hand, because
the side of a Mac for a team only comes in a later change. The publish step of Lin's Mac does nothing, so a pass does
not delete these entries.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import time
import unittest
from secrets import token_hex
from uuid import uuid4

from coconut import team_store
from coconut.push import DIRECT_BODY
from coconut.team_directs import BOT_GONE, BOT_SILENT, CHOOSE_PAGE, NOT_HERE, NOT_ON, TEAM_ONLY
from coconut.team_store import bot_fields, direct_fields
from team_fakes import DEVICE
from test_push import RecordingTransport, pair_phone
from test_push_relay import EncryptedTransport
from test_team_directs import DirectMacs
from test_team_only import TeamOnlyMacs
from test_team_push import ROUTE, flush, register

ENTRY = 'a' * 32
PAGE_ENTRY = 'b' * 32


def box_text(**fields) -> bytes:
    return json.dumps(fields).encode()


class TheBox(unittest.TestCase):
    def test_bot_and_page(self):
        self.assertEqual(direct_fields(box_text(to='m', text='Hi', bot=ENTRY, page=PAGE_ENTRY), 'm', page=True),
                         dict(text='Hi', reply_to=None, bot=ENTRY, page=PAGE_ENTRY))
        # A box without `bot` is a message of a chat of two people, and the reader ignores its `page`.
        self.assertEqual(direct_fields(box_text(to='m', text='Hi', page='not an entry'), 'm', page=True),
                         dict(text='Hi', reply_to=None, bot=None, page=None))
        # A message of the Mac of the bot: the reader ignores its `page`.
        self.assertEqual(direct_fields(box_text(to='m', text='Hi', bot=ENTRY, page='x'), 'm'),
                         dict(text='Hi', reply_to=None, bot=ENTRY, page=None))
        # Null is an absent field.
        self.assertEqual(direct_fields(box_text(to='m', text='Hi', bot=None, page=None), 'm', page=True),
                         dict(text='Hi', reply_to=None, bot=None, page=None))

    def test_a_bot_or_a_page_that_is_not_an_entry_id_does_not_fit(self):
        for fields in ({'bot': 'x'}, {'bot': ENTRY.upper()}, {'bot': 7}, {'bot': ENTRY, 'page': 'x'},
                       {'bot': ENTRY, 'page': 1}):
            with self.subTest(fields=fields):
                self.assertIsNone(direct_fields(box_text(to='m', text='Hi', **fields), 'm', page=True))
        # The typing sign of a bot has no `text`, so it shows nothing in this version.
        self.assertIsNone(direct_fields(box_text(to='m', bot=ENTRY, on=True), 'm'))


class TheMarkOfABot(unittest.TestCase):
    def test_only_the_value_true_is_the_mark(self):
        for value, mark in ((True, True), (None, False), ('true', False), (1, False), (False, False)):
            with self.subTest(value=value):
                fields = {'name': 'Reporter'} if value is None else {'name': 'Reporter', 'direct': value}
                self.assertIs(bot_fields(fields)['direct'], mark)

    def test_a_store_of_an_older_version_gets_the_columns_and_keeps_its_rows(self):
        db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(db.close)
        db.execute('''CREATE TABLE team_bots (room TEXT NOT NULL, entry TEXT NOT NULL, member TEXT NOT NULL,
            name TEXT NOT NULL, avatar TEXT NOT NULL, color TEXT NOT NULL, open INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (room, entry))''')
        db.execute('''CREATE TABLE team_directs (room TEXT NOT NULL, entry TEXT NOT NULL, first INTEGER NOT NULL,
            writer TEXT NOT NULL, member TEXT NOT NULL, text TEXT NOT NULL, reply_to TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, edited INTEGER NOT NULL, PRIMARY KEY (room, entry))''')
        db.execute('CREATE INDEX team_directs_order ON team_directs (room, member, first)')
        db.execute("INSERT INTO team_bots VALUES ('r', 'e', 'm', 'Reporter', '', '', 1)")
        db.execute("INSERT INTO team_directs VALUES ('r', 'd', 5, 'm', 'm', 'Hello', NULL, 'now', 'now', 0)")
        db.executescript(team_store.SCHEMA)
        store = team_store.TeamStore(db)
        store.migrate()
        store.migrate()
        self.assertEqual(db.execute('SELECT open, direct FROM team_bots').fetchone(), (1, 0))
        (row,) = store.directs('r', 'm')
        self.assertEqual((row['entry'], row['text'], row['bot'], row['page']), ('d', 'Hello', None, None))
        self.assertEqual(store.direct_unread('r', 'm'), 1)
        indexes = {row[1] for row in db.execute("PRAGMA index_list('team_directs')")}
        self.assertNotIn('team_directs_order', indexes)


class BotDirectMacs(DirectMacs):
    def setUp(self):
        super().setUp()
        self.turn_on(*self.macs)
        self.third._team_publish = lambda room_id: None
        self.bot = self.write_bot('Reporter', direct=True)
        self.sync()
        self.bot_id = self.team_id(self.bot)

    def team_id(self, entry: str) -> str:
        return f't-{self.room}-{entry}'

    def write(self, service, entry: str, fields: dict, kept: bool) -> None:
        room = service.team_store.room(self.room)
        with service._team_room_lock(self.room):
            service._team_write(room, entry, kept, fields)

    def write_bot(self, name: str, entry: str | None = None, **marks) -> str:
        """Write a `bot` entry of Lin's Mac. Return its entry ID."""
        entry = entry or token_hex(16)
        self.write(self.third, entry, dict({'kind': 'bot', 'name': name, 'avatar': '', 'color': ''}, **marks), True)
        return entry

    def write_page(self, bot: str, title: str = 'Weekly report', entry: str | None = None, **fields) -> str:
        """Write a `page` entry of a bot of Lin's Mac. Return its entry ID."""
        entry = entry or token_hex(16)
        page = dict(kind='page', bot=bot, key='weekly-report', title=title, format='markdown', summary=None,
                    content='3 tasks are open.', version=1, expires_at=None)
        self.write(self.third, entry, dict(page, **fields), True)
        return entry

    def box(self, service, entry: str, to: str, plaintext: dict) -> None:
        """Write a `direct` entry of a Mac with a box for `to`."""
        room = service.team_store.room(self.room)
        secrets = json.loads(self.key_file(service, self.room).read_text())
        agreement = service.team_store.member(self.room, to)['agreement']
        box = service.team_helper.seal_direct(self.room, entry, secrets['agreement_key'], room['member'], to,
                                              agreement, json.dumps(plaintext).encode())
        self.write(service, entry, {'kind': 'direct', 'box': box}, False)

    def bot_says(self, text: str, bot: str | None = None, service=None, **fields) -> str:
        """A message of the bot to Ada, as the Mac of the bot writes it, and a pass of Ada's Mac. Return its entry
        ID."""
        entry = token_hex(16)
        self.box(service or self.third, entry, self.ada, dict(fields, to=self.ada, text=text, bot=bot or self.bot))
        self.first.team_sync_pass()
        return entry

    def plaintext(self, service, entry: str) -> dict:
        """The plaintext of the box of a `direct` entry in the store of a Mac (the box of FakeHelper)."""
        box = json.loads(service.team_store.entry(self.room, entry)['payload'])['box']
        sealed = json.loads(base64.urlsafe_b64decode(box + '=' * (-len(box) % 4)))
        return json.loads(base64.b64decode(sealed['plaintext']))

    def bot_chat(self, service=None) -> list[tuple[str, bool]]:
        return self.chat(service or self.first, self.bot_id)


class ReadingAChatWithABot(BotDirectMacs, unittest.TestCase):
    def test_a_message_of_the_bot_shows_only_in_the_chat_with_the_bot(self):
        self.bot_says('Hello Ada')
        self.assertEqual(self.bot_chat(), [('Hello Ada', False)])
        self.assertEqual(self.chat(self.first, self.lin), [])
        room = self.ask(self.first)
        self.assertEqual((room['directs'], room['unread'], room['last_message']), ([], 0, None))
        self.assertEqual(self.count(self.first, 'team_messages'), 0)
        # Grace's Mac has the entry and shows nothing.
        self.sync(self.second)
        self.assertEqual((self.count(self.second, 'team_directs'), self.ask(self.second)['bot_directs']), (0, []))

    def test_a_message_of_the_bot_before_its_bot_entry_shows_when_the_entry_comes(self):
        later = token_hex(16)
        self.bot_says('Early', bot=later)
        self.assertEqual((self.chat(self.first, self.team_id(later)), self.ask(self.first)['bot_directs']), ([], []))
        cursor = self.first._cursor()
        self.write_bot('Planner', later, direct=True)
        self.first.team_sync_pass()
        self.assertEqual(self.chat(self.first, self.team_id(later)), [('Early', False)])
        self.assertEqual(self.first.updates(cursor, 0, collection_deltas=True)['team_bot_directs_changed'],
                         [{'team': self.room, 'bot': self.team_id(later)}])

    def test_a_bot_entry_of_another_member_hides_the_message(self):
        # Grace writes a message that names the bot of Lin's Mac.
        self.bot_says('Not the bot', service=self.second)
        self.assertEqual((self.bot_chat(), self.chat(self.first, self.grace)), ([], []))
        self.assertEqual(self.ask(self.first)['bot_directs'], [])

    def test_an_own_message_that_the_store_does_not_have(self):
        self.send(self.first, self.bot_id, 'To the bot')
        self.send(self.first, self.lin, 'To Lin')
        self.bot_says('To Ada')
        with self.first.transaction():
            for table in ('team_entries', 'team_members', 'team_directs'):
                self.first.db.execute(f'DELETE FROM {table} WHERE room=?', (self.room,))
            self.first.team_store.update_room(self.room, seq=0)
        self.first.team_sync_pass()
        self.assertEqual(self.bot_chat(), [('To Ada', False), ('To the bot', True)])
        self.assertEqual(self.chat(self.first, self.lin), [('To Lin', True)])

    def test_the_two_kinds_of_chat_stay_apart_in_each_query(self):
        to_lin = self.send(self.first, self.lin, 'To Lin')
        self.sync(self.third)
        from_lin = self.send(self.third, self.ada, 'From Lin')
        self.first.team_sync_pass()
        to_bot = self.send(self.first, self.bot_id, 'To the bot')
        self.bot_says('From the bot 1')
        self.bot_says('From the bot 2')
        self.assertEqual(self.chat(self.first, self.lin), [('From Lin', False), ('To Lin', True)])
        self.assertEqual(self.bot_chat(), [('From the bot 2', False), ('From the bot 1', False),
                                           ('To the bot', True)])
        room = self.ask(self.first)
        (person,) = room['directs']
        self.assertEqual((person['member'], person['unread'], person['last_message']['text']),
                         (self.lin, 1, 'From Lin'))
        (bot,) = room['bot_directs']
        self.assertEqual((bot['bot'], bot['unread'], bot['last_message']['text']), (self.bot_id, 2, 'From the bot 2'))
        # Each read position moves only its own chat, and never past the newest message of that chat.
        read = self.phone(self.first)('POST', self.path(self.lin, 'read'), {'first': 10 ** 9})
        self.assertEqual((read['read'], read['unread']), (from_lin['first'], 0))
        self.assertEqual(self.ask(self.first)['bot_directs'][0]['unread'], 2)
        read = self.phone(self.first)('POST', self.path(self.bot_id, 'read'), {'first': 10 ** 9})
        newest = self.phone(self.first)('GET', self.path(self.bot_id), {'limit': '1'})['messages'][0]['first']
        self.assertEqual(read, {'room': self.room, 'bot': self.bot_id, 'read': newest, 'unread': 0})
        self.assertEqual(self.first.team_store.direct_read_position(self.room, self.lin), from_lin['first'])
        # A reply quotes only a message of the same chat.
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.bot_id),
                                      {'text': 'No', 'reply_to': to_lin['id']})[0], 409)
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.lin),
                                      {'text': 'No', 'reply_to': to_bot['id']})[0], 409)
        reply = self.send(self.first, self.bot_id, 'Yes', reply_to=to_bot['id'])
        self.assertEqual(reply['reply'], {'id': to_bot['id'], 'this_mac': True, 'text': 'To the bot'})
        # A message of one chat is not a message of the other chat for an edit or a removal.
        self.assertEqual(self.refusal(self.phone(self.first), 'PATCH', self.path(self.lin, f'messages/{to_bot["id"]}'),
                                      {'text': 'No'})[0], 404)
        self.assertEqual(self.refusal(self.phone(self.first), 'DELETE',
                                      self.path(self.bot_id, f'messages/{to_lin["id"]}'), {})[0], 404)

    def test_the_chat_keeps_its_history_after_the_bot_leaves(self):
        self.bot_says('Hello Ada')
        self.write(self.third, self.bot, {'kind': 'delete'}, False)
        self.first.team_sync_pass()
        self.assertEqual(self.bot_chat(), [('Hello Ada', False)])
        (chat,) = self.ask(self.first)['bot_directs']
        self.assertEqual(chat['bot_name'], 'Reporter')
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.bot_id), {'text': 'Hi'}),
                         (409, BOT_GONE))

    def test_the_unread_count_stops_at_99(self):
        self.bot_says('Hello Ada')
        with self.first.transaction():
            for number in range(120):
                self.first.db.execute('''INSERT INTO team_directs (room, entry, first, writer, member, text, reply_to,
                    created_at, updated_at, edited, bot, page) VALUES (?, ?, ?, ?, ?, 'x', NULL, 'now', 'now', 0, ?,
                    NULL)''', (self.room, f'{number:032x}', 1000 + number, self.lin, self.lin, self.bot))
        self.assertEqual(self.ask(self.first)['bot_directs'][0]['unread'], 99)


class Routes(BotDirectMacs, unittest.TestCase):
    def test_a_message_to_the_bot(self):
        sent = self.send(self.first, self.bot_id, 'Hello Reporter')
        self.assertEqual(set(sent), {'id', 'first', 'this_mac', 'text', 'reply_to', 'reply', 'created_at', 'edited_at',
                                     'page'})
        self.assertEqual((sent['text'], sent['this_mac'], sent['page']), ('Hello Reporter', True, None))
        # The box is for the Mac of the bot, and names the bot.
        self.assertEqual(self.plaintext(self.first, sent['id']), {'to': self.lin, 'text': 'Hello Reporter',
                                                                  'bot': self.bot})
        page = self.phone(self.first)('GET', self.path(self.bot_id), {})
        self.assertEqual((page['room'], page['bot'], page['before'], len(page['messages'])),
                         (self.room, self.bot_id, None, 1))
        self.assertNotIn('member', page)

    def test_a_page_of_the_chat_and_a_repeat(self):
        sent = [self.send(self.first, self.bot_id, f'Message {number}') for number in range(3)]
        page = self.phone(self.first)('GET', self.path(self.bot_id), {'limit': '2'})
        self.assertEqual(([row['id'] for row in page['messages']], page['before']),
                         ([sent[2]['id'], sent[1]['id']], sent[1]['first']))
        key = str(uuid4())
        once = self.send(self.first, self.bot_id, 'Once', key)
        self.assertEqual(self.send(self.first, self.bot_id, 'Once', key), once)
        self.assertEqual(len(self.bot_chat()), 4)

    def test_each_refusal_of_a_message_to_a_bot(self):
        silent = self.write_bot('Quiet')
        unknown = token_hex(16)
        self.sync()
        cases = ((self.team_id(silent), 409, BOT_SILENT), (self.team_id(unknown), 409, BOT_GONE),
                 (f't-{"f" * 64}-{self.bot}', 404, NOT_HERE), (f't-{self.room}-x', 404, NOT_HERE))
        for chat, status, text in cases:
            with self.subTest(chat=chat[-6:]):
                self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(chat), {'text': 'Hi'}),
                                 (status, text))
        # The bot of this Mac: a person's Mac never marks its bot.
        own = self.first.mutate('POST', '/v1/bots', {'name': 'Own'}, str(uuid4()), 'phone')
        self.share_page(self.first, self.room, own)
        self.first.team_sync_pass()
        (own_entry,) = self.first.team_store.shares(self.room).values()
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.team_id(own_entry)),
                                      {'text': 'Hi'}), (409, BOT_SILENT))
        # This Mac without the mark.
        with self.first.transaction():
            self.first.db.execute('DELETE FROM team_marks')
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.bot_id), {'text': 'Hi'}),
                         (409, NOT_ON))

    def test_a_bot_whose_mac_left_is_no_longer_in_the_team(self):
        self.bot_says('Hello Ada')
        self.leave(self.third, {'room': self.room})
        self.first.team_sync_pass()
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.bot_id), {'text': 'Hi'}),
                         (409, BOT_GONE))
        self.assertEqual(self.bot_chat(), [('Hello Ada', False)])

    def test_a_room_that_is_not_active(self):
        with self.first.transaction():
            self.first.team_store.update_room(self.room, state='removed')
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.bot_id), {'text': 'Hi'})[0], 409)

    def test_an_edit_keeps_the_bot_the_page_and_the_reply(self):
        page = self.write_page(self.bot)
        answered = self.bot_says('The report is ready.')
        sent = self.send(self.first, self.bot_id, 'Thanks', reply_to=answered,
                         about_document_id=self.team_id(page))
        edited = self.phone(self.first)('PATCH', self.path(self.bot_id, f'messages/{sent["id"]}'),
                                        {'text': 'Thank you'})['message']
        self.assertEqual((edited['text'], edited['reply_to'], edited['page']),
                         ('Thank you', answered, {'id': self.team_id(page), 'title': 'Weekly report'}))
        self.assertEqual(self.plaintext(self.first, sent['id']), {'to': self.lin, 'text': 'Thank you',
                                                                  'bot': self.bot, 'reply_to': answered, 'page': page})
        self.assertIsNotNone(edited['edited_at'])
        removed = self.phone(self.first)('DELETE', self.path(self.bot_id, f'messages/{sent["id"]}'), {})
        self.assertEqual(removed, {'room': self.room, 'bot': self.bot_id, 'id': sent['id'], 'deleted': True})
        self.assertEqual(self.bot_chat(), [('The report is ready.', False)])

    def test_a_message_of_the_bot_cannot_be_changed(self):
        entry = self.bot_says('Mine')
        for method, data in (('PATCH', {'text': 'No'}), ('DELETE', {})):
            with self.subTest(method=method):
                self.assertEqual(self.refusal(self.phone(self.first), method,
                                              self.path(self.bot_id, f'messages/{entry}'), data)[0], 403)

    def test_a_page_that_the_message_is_about(self):
        page = self.write_page(self.bot)
        other_bot = self.write_bot('Planner', direct=True)
        other_page = self.write_page(other_bot, title='Plan')
        ended = self.write_page(self.bot, title='Old report', expires_at='2000-01-01T00:00:00Z')
        own = self.first.mutate('POST', '/v1/bots', {'name': 'Own'}, str(uuid4()), 'phone')
        own_document = self.share_page(self.first, self.room, own)
        self.sync()
        own_page = self.first.team_store.page_entries(self.room)[own_document['id']]
        for value in (self.team_id(other_page), f't-{"f" * 64}-{page}', self.team_id(ended), own_document['id'],
                      self.team_id(own_page), self.team_id(token_hex(16)), 7, ''):
            with self.subTest(value=value):
                self.assertEqual(self.refusal(self.phone(self.first), 'POST', self.path(self.bot_id),
                                              {'text': 'About this', 'about_document_id': value}), (409, CHOOSE_PAGE))
        sent = self.send(self.first, self.bot_id, 'About this', about_document_id=self.team_id(page))
        self.assertEqual(sent['page'], {'id': self.team_id(page), 'title': 'Weekly report'})
        self.assertEqual(self.plaintext(self.first, sent['id'])['page'], page)
        # The title comes from the store at the time of the read.
        self.write_page(self.bot, title='Weekly report, week 2', entry=page)
        self.first.team_sync_pass()
        (message,) = self.phone(self.first)('GET', self.path(self.bot_id), {})['messages']
        self.assertEqual(message['page'], {'id': self.team_id(page), 'title': 'Weekly report, week 2'})
        # A page that no longer shows.
        self.write(self.third, page, {'kind': 'delete'}, False)
        self.first.team_sync_pass()
        (message,) = self.phone(self.first)('GET', self.path(self.bot_id), {})['messages']
        self.assertEqual(message['page'], {'id': None, 'title': None})

    def test_a_chat_of_two_people_keeps_its_answers(self):
        sent = self.send(self.first, self.lin, 'Hello Lin', about_document_id='anything')
        self.assertNotIn('page', sent)
        page = self.phone(self.first)('GET', self.path(self.lin), {})
        self.assertEqual((page['member'], 'bot' in page), (self.lin, False))
        self.assertEqual(self.plaintext(self.first, sent['id']), {'to': self.lin, 'text': 'Hello Lin'})


class TeamOnlyMacsHaveNoChatWithABot(TeamOnlyMacs, unittest.TestCase):
    def test_a_mac_for_a_team_only_answers_409_for_a_team_bot_id(self):
        room = self.team()
        chat = f't-{room}-{"1" * 32}'
        for method, rest in (('GET', 'messages'), ('POST', 'messages'), ('PATCH', 'messages/' + '1' * 32),
                             ('DELETE', 'messages/' + '1' * 32), ('POST', 'read')):
            with self.subTest(method=method, rest=rest):
                self.assertEqual(self.refusal(self.call, method, f'/v1/team/rooms/{room}/directs/{chat}/{rest}',
                                              {'text': 'Hi', 'first': 1}), (409, TEAM_ONLY))


class TheListOfTheTeams(BotDirectMacs, unittest.TestCase):
    def test_the_two_fields_only_with_the_header(self):
        self.write_bot('Quiet')
        self.sync()
        self.bot_says('Hello Ada')
        plain = self.first.team_request('GET', '/v1/team/rooms', {}, '', DEVICE)['rooms'][0]
        self.assertNotIn('bot_directs', plain)
        self.assertTrue(all('direct' not in bot for bot in plain['bots']))
        room = self.ask(self.first)
        self.assertEqual({bot['name']: bot['direct'] for bot in room['bots']}, {'Reporter': True, 'Quiet': False})
        (chat,) = room['bot_directs']
        self.assertEqual(chat, {'bot': self.bot_id, 'bot_name': 'Reporter', 'unread': 1, 'typing': None,
                                'last_message': {'first': chat['last_message']['first'], 'this_mac': False,
                                                 'text': 'Hello Ada',
                                                 'created_at': chat['last_message']['created_at']}})
        # A tool on this Mac gets neither field.
        tool = self.first.team_request('GET', '/v1/team/rooms', {}, '', '', True)['rooms'][0]
        self.assertNotIn('bot_directs', tool)

    def test_the_newest_chat_comes_first_and_the_text_has_200_code_points(self):
        other = self.write_bot('Planner', direct=True)
        self.sync()
        self.bot_says('Old')
        self.bot_says('é' * 300, bot=other)
        chats = self.ask(self.first)['bot_directs']
        self.assertEqual([chat['bot'] for chat in chats], [self.team_id(other), self.bot_id])
        self.assertEqual(chats[0]['last_message']['text'], 'é' * 200)

    def test_a_change_of_the_mark_changes_the_team(self):
        cursor = self.first._cursor()
        self.write_bot('Reporter', self.bot)
        self.first.team_sync_pass()
        self.assertTrue(self.first.updates(cursor, 0, collection_deltas=True)['team_changed'])
        self.assertEqual([bot['direct'] for bot in self.ask(self.first)['bots']], [False])
        self.assertEqual(self.first.team_store.bots(self.room)[0]['direct'], 0)

    def test_a_persons_mac_writes_no_mark_and_no_new_bot_entry(self):
        own = self.first.mutate('POST', '/v1/bots', {'name': 'Own'}, str(uuid4()), 'phone')
        self.share_page(self.first, self.room, own)
        self.sync()
        (entry,) = self.first.team_store.shares(self.room).values()
        for _ in range(3):
            self.first.team_sync_pass()
        stored = self.first.team_store.entry(self.room, entry)
        self.assertEqual(stored['rev'], 1)
        self.assertNotIn('direct', json.loads(stored['payload']))


class Events(BotDirectMacs, unittest.TestCase):
    def changes(self, cursor) -> dict:
        return self.first.updates(cursor, 0, collection_deltas=True)

    def test_each_change_of_a_chat_with_a_bot_adds_its_own_event(self):
        cursor = self.first._cursor()
        sent = self.send(self.first, self.bot_id, 'Hello')
        answer = self.changes(cursor)
        self.assertNotIn('reset', answer)
        self.assertEqual(answer['team_bot_directs_changed'], [{'team': self.room, 'bot': self.bot_id}])
        self.assertEqual((answer['team_directs_changed'], answer['team_messages_changed']), ([], []))
        # A pass that changes nothing adds no event.
        cursor = self.first._cursor()
        self.sync(self.first)
        self.assertEqual(self.first._cursor(), cursor)
        steps = (lambda: self.bot_says('Hi Ada'),
                 lambda: self.phone(self.first)('POST', self.path(self.bot_id, 'read'), {'first': 10 ** 9}),
                 lambda: self.phone(self.first)('PATCH', self.path(self.bot_id, f'messages/{sent["id"]}'),
                                                {'text': 'Hello again'}),
                 lambda: self.phone(self.first)('DELETE', self.path(self.bot_id, f'messages/{sent["id"]}'), {}),
                 lambda: (self.write_bot('Reporter 2', self.bot, direct=True), self.first.team_sync_pass()))
        for number, step in enumerate(steps):
            with self.subTest(step=number):
                cursor = self.first._cursor()
                step()
                answer = self.changes(cursor)
                self.assertEqual(answer['team_bot_directs_changed'], [{'team': self.room, 'bot': self.bot_id}])
                self.assertEqual(answer['team_directs_changed'], [])
        # No event of a chat of two people names no member.
        rows = self.first.db.execute("SELECT data FROM events WHERE type='team_directs.updated'").fetchall()
        self.assertTrue(all('member' in json.loads(row[0]) for row in rows))


class BotDirectPushMacs(BotDirectMacs):
    """Ada's Mac (`first`) has the phone."""

    def setUp(self):
        super().setUp()
        self.phone_id = pair_phone(self.first)
        self.transport = RecordingTransport()
        self.first.push.transport = self.transport
        register(self.first, self.phone_id, level='grouped')

    def items(self):
        return self.first.db.execute('SELECT kind,message_id,team,status,reason FROM push_items ORDER BY created,rowid'
                                     ).fetchall()

    def payloads(self):
        return [call[2] for call in self.transport.calls]

    def seal(self):
        self.encrypted = EncryptedTransport()
        self.first.push.relay = self.encrypted
        register(self.first, self.phone_id, route=ROUTE)


class Notifications(BotDirectPushMacs, unittest.TestCase):
    def test_a_message_of_the_bot_notifies_once(self):
        entry = self.bot_says('Hello Ada')
        self.assertEqual(self.items(), [('direct', entry, self.room, 'waiting', None)])
        self.first.team_sync_pass()
        flush(self.first)
        (payload,) = self.payloads()
        self.assertEqual(payload['aps']['alert'], {'title': 'Coconut', 'body': DIRECT_BODY})
        self.assertEqual(payload['aps']['thread-id'], f'{self.room}:{self.bot}')
        self.assertEqual(payload['coconut'], {'version': 1, 'device_id': self.phone_id, 'kind': 'direct',
                                              'direct': {'team': self.room, 'bot': self.bot_id}})

    def test_an_own_message_an_old_message_and_a_hidden_message_notify_nothing(self):
        self.send(self.first, self.bot_id, 'Mine')
        self.bot_says('Not the bot', service=self.second)
        self.first.team_store.clock = lambda: time.time() + team_store.TRIGGER_SECONDS
        self.bot_says('Late')
        self.assertEqual(self.items(), [])

    def test_the_read_position_of_the_chat_with_the_bot_decides(self):
        self.bot_says('Read on the Mac')
        self.phone(self.first)('POST', self.path(self.lin, 'read'), {'first': 10 ** 9})
        self.phone(self.first)('POST', self.path(self.bot_id, 'read'), {'first': 10 ** 9})
        flush(self.first)
        self.assertEqual(self.items()[0][3:], ('dropped', 'NoLongerRelevant'))
        self.bot_says('After the read')
        self.phone(self.first)('POST', self.path(self.lin, 'read'), {'first': 10 ** 9})
        flush(self.first)
        self.assertEqual(len(self.payloads()), 1)

    def test_the_sealed_alert_names_the_bot(self):
        self.seal()
        self.bot_says('**Ready**: the report.')
        flush(self.first)
        (payload,) = self.encrypted.sealed
        self.assertEqual(payload['aps']['alert'], {'title': 'Reporter', 'body': 'Ready: the report.'})
        self.assertEqual(payload['aps']['thread-id'], f'{self.room}:{self.bot}')
        self.assertNotIn('Ready', json.dumps(self.encrypted.sent))

    def test_a_group_of_one_chat_with_a_bot_opens_that_chat(self):
        self.bot_says('One')
        self.bot_says('Two')
        flush(self.first)
        (payload,) = self.payloads()
        self.assertEqual(payload['aps']['thread-id'], f'{self.room}:{self.bot}')
        self.assertEqual(payload['coconut'], {'version': 1, 'device_id': self.phone_id, 'kind': 'group', 'count': 2,
                                              'direct': {'team': self.room, 'bot': self.bot_id}})

    def test_a_group_of_a_chat_with_a_bot_and_another_chat_opens_inbox(self):
        self.bot_says('From the bot')
        self.send(self.third, self.ada, 'From Lin')
        self.first.team_sync_pass()
        flush(self.first)
        (payload,) = self.payloads()
        self.assertEqual((payload['aps']['thread-id'], payload['coconut']['count']), ('coconut-inbox', 2))
        self.assertNotIn('direct', payload['coconut'])


if __name__ == '__main__':
    unittest.main()
