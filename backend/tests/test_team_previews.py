"""Link previews in the team chats (docs/team-log.md, "Link previews").

PreviewRuleTests checks the reader of a preview, and PreviewStoreTests gives TeamStore.accept entries as the relay sends
them, also entries that another member made to break the rules. The other tests run Macs with FakeRelay and FakeHelper
(team_fakes.py), as the app calls them: Ada's Mac (`first`) made the room "North team", and Grace (`second`) and Lin
(`third`) joined it. PreviewAgentTests runs the Mac for a team only with its bot Reporter. scripts/test_team_log.py
runs the same path through the real relay in Miniflare and the real helper.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import time
import unittest
from secrets import token_hex
from uuid import uuid4

from coconut import team_keys, team_store
from coconut.team_messages import PREVIEW_MISFIT
from coconut.team_store import STORED, TeamStore, direct_fields, message_fields, preview_fields
from team_fakes import DEVICE, TeamMacs
from test_team_direct_turns import DirectTurnMacs
from test_team_push import TeamPushMacs, flush
from test_team_turns import reply

URL = 'https://example.com/a'
TEXT = f'Look at {URL} today'
# The title and the site are texts that no other part of a turn has, so a test finds them in a turn.
PREVIEW = {'url': URL, 'title': 'Example page title', 'site': 'Example Site'}
LONG_URL = 'https://example.com/' + 'a' * (team_store.MAX_PREVIEW_URL - len('https://example.com/'))
BROKEN_URL = 'https://example.com/a b'
# One text that contains each address of the cases below, so each case breaks only its own rule.
ALL_TEXT = f'{TEXT} {LONG_URL}b javascript:alert(1) ftp://example.com/a HTTPS://EXAMPLE.COM/A {BROKEN_URL}'
ROOM = 'a' * 64
ADMIN = '1' * 64
MEMBER = '2' * 64
OTHER = '3' * 64
START = 1_800_000_000_000


def entry_id(number: int) -> str:
    return f'{number:032x}'


def misfits() -> dict[str, object]:
    """Previews that do not fit, each with the rule that it breaks. Each one fits ALL_TEXT but for its own rule."""
    return {
        'a title of 10,000 characters': dict(PREVIEW, title='x' * 10_000),
        'a title of 201 characters': dict(PREVIEW, title='x' * (team_store.MAX_PREVIEW_TITLE + 1)),
        'a title over its limit only with its spaces': dict(PREVIEW, title=' ' + 'x' * team_store.MAX_PREVIEW_TITLE),
        'a site of 101 characters': dict(PREVIEW, site='x' * (team_store.MAX_PREVIEW_SITE + 1)),
        'a url of 2,001 characters': dict(PREVIEW, url=LONG_URL + 'b'),
        'a url that is not in the text': dict(PREVIEW, url='https://example.com/b'),
        'a javascript: address': dict(PREVIEW, url='javascript:alert(1)'),
        'an ftp address': dict(PREVIEW, url='ftp://example.com/a'),
        'a scheme in capitals': dict(PREVIEW, url='HTTPS://EXAMPLE.COM/A'),
        'a nested object': dict(PREVIEW, title={'text': 'Example page title'}),
        'a list': [PREVIEW],
        'a text': URL,
        'a line break in the title': dict(PREVIEW, title='Example page\ntitle'),
        'a carriage return in the site': dict(PREVIEW, site='Example Site\r'),
        'a line separator in the url': dict(PREVIEW, url=BROKEN_URL),
        'an extra key': dict(PREVIEW, image='https://example.com/a.png'),
        'a missing key': {'url': URL, 'title': 'Example page title'},
        'a title of spaces': dict(PREVIEW, title='   '),
        'an empty site': dict(PREVIEW, site=''),
        'a url that is a number': dict(PREVIEW, url=7),
        'a lone surrogate in the title': dict(PREVIEW, title='Example \ud800'),
    }


class PreviewRuleTests(unittest.TestCase):
    def test_a_preview_that_fits_and_its_limits(self):
        self.assertEqual(preview_fields(PREVIEW, TEXT), PREVIEW)
        # Each value fits at its limit as sent.
        title, site = 'x' * team_store.MAX_PREVIEW_TITLE, 'y' * team_store.MAX_PREVIEW_SITE
        self.assertEqual(preview_fields(dict(PREVIEW, url=LONG_URL, title=title, site=site), ALL_TEXT),
                         dict(url=LONG_URL, title=title, site=site))
        # The title and the site are trimmed. The limit counts the text as sent, with its spaces.
        self.assertEqual(preview_fields(dict(PREVIEW, title=f' {title[2:]} ', site=f'\t{site[2:]} '), TEXT),
                         dict(PREVIEW, title=title[2:], site=site[2:]))
        self.assertIsNone(preview_fields(dict(PREVIEW, title=f' {title}'), TEXT))
        self.assertIsNone(preview_fields(dict(PREVIEW, site=f'{site} '), TEXT))
        self.assertEqual(preview_fields(dict(PREVIEW, url='http://example.com/a'), 'See http://example.com/a.'),
                         dict(PREVIEW, url='http://example.com/a'))
        self.assertIsNone(preview_fields(None, TEXT))

    def test_a_preview_that_does_not_fit_is_left_out_and_the_message_stays(self):
        for name, preview in misfits().items():
            with self.subTest(case=name):
                self.assertIsNone(preview_fields(preview, ALL_TEXT))
                message = message_fields({'text': ALL_TEXT, 'preview': preview})
                self.assertEqual((message['text'], message['preview']), (ALL_TEXT, None))
                box = json.dumps({'to': MEMBER, 'text': ALL_TEXT, 'preview': preview}).encode()
                direct = direct_fields(box, MEMBER)
                self.assertEqual((direct['text'], direct['preview']), (ALL_TEXT, None))


class Watched(str):
    """A text that records each call of encode, splitlines and strip, with its length. A reader gets plain texts from
    JSON. A subclass lets the test see which of these calls a check makes, without a clock."""
    calls: list[tuple[str, int]] = []

    def encode(self, *arguments, **options):
        Watched.calls.append(('encode', len(self)))
        return super().encode(*arguments, **options)

    def splitlines(self, *arguments, **options):
        Watched.calls.append(('splitlines', len(self)))
        return super().splitlines(*arguments, **options)

    def strip(self, *arguments, **options):
        Watched.calls.append(('strip', len(self)))
        return super().strip(*arguments, **options)


class CountedKeys(dict):
    """A dict that counts each walk of its keys."""
    walks = 0

    def __iter__(self):
        CountedKeys.walks += 1
        return super().__iter__()

    def keys(self):
        CountedKeys.walks += 1
        return super().keys()


class PreviewCostTests(unittest.TestCase):
    """A hostile preview of up to 1 MiB costs no work that grows with its size: the check stops at the cheap rules."""

    def setUp(self):
        Watched.calls = []
        CountedKeys.walks = 0

    def check(self, preview: dict) -> None:
        self.assertIsNone(preview_fields(preview, TEXT))
        self.assertEqual(Watched.calls, [], 'No encode, split or trim of a value over its limit.')

    def test_a_title_of_600000_characters_with_line_breaks(self):
        self.check(dict(PREVIEW, title=Watched('ab\n' * 200_000)))

    def test_a_short_title_with_10000_spaces_around_it(self):
        self.check(dict(PREVIEW, title=Watched(' ' * 5_000 + 'Example page title' + ' ' * 5_000)))

    def test_a_long_url_and_a_long_site(self):
        self.check(dict(PREVIEW, url=Watched(URL + '\n' * 600_000)))
        self.check(dict(PREVIEW, site=Watched('\r' * 600_000)))

    def test_a_preview_with_100000_keys(self):
        preview = CountedKeys(PREVIEW)
        preview.update((f'key{number}', '') for number in range(100_000 - 3))
        self.assertEqual(len(preview), 100_000)
        self.assertIsNone(preview_fields(preview, TEXT))
        self.assertEqual(CountedKeys.walks, 0, 'No walk of the keys.')

    def test_a_preview_that_fits_reads_each_short_value(self):
        preview = {name: Watched(value) for name, value in PREVIEW.items()}
        self.assertEqual(preview_fields(preview, TEXT), PREVIEW)
        self.assertTrue(Watched.calls)
        self.assertTrue(all(length <= team_store.MAX_PREVIEW_URL for _, length in Watched.calls))


class PreviewStoreTests(unittest.TestCase):
    """The reading rules of a preview in a `message` entry. MEMBER is this Mac, and ADMIN and OTHER are the other
    members."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.executescript(team_store.SCHEMA)
        self.store = TeamStore(self.db)
        self.store.migrate()
        self.add_room(self.store)
        self.seq = 0

    @staticmethod
    def add_room(store):
        store.add_room(room=ROOM, relay='https://relay.example', name='North team', member=MEMBER, admin=ADMIN,
                       state=team_store.ACTIVE, epoch=0, profile='{}', created_at='2026-11-15T12:00:00Z')

    def write(self, number: int, payload: dict, member: str = ADMIN):
        self.seq += 1
        existing = self.store.entry(ROOM, entry_id(number))
        entry = dict(seq=self.seq, first=existing['first'] if existing else self.seq, id=entry_id(number),
                     member=member, at=START + self.seq, kept=False, body=b'')
        opened = team_store.read_entry({'epoch': 0, 'payload': json.dumps(payload).encode()})
        return self.store.accept(self.store.room(ROOM), entry, opened)

    @staticmethod
    def message(text: str = TEXT, rev: int = 1, **fields) -> dict:
        return {'kind': 'message', 'rev': rev, 'at': '2026-11-15T12:00:00Z', 'text': text, **fields}

    def test_a_reader_keeps_a_preview_that_fits_and_leaves_out_each_one_that_does_not(self):
        self.assertEqual(self.write(1, self.message(preview=dict(PREVIEW, title=' Example page title '))),
                         (STORED, None))
        (row,) = self.store.messages(ROOM)
        self.assertEqual(json.loads(row['preview']), PREVIEW)
        cases = misfits()
        for number, (name, preview) in enumerate(cases.items(), start=2):
            with self.subTest(case=name):
                self.assertEqual(self.write(number, self.message(ALL_TEXT, preview=preview)), (STORED, None))
                row = self.store.message(ROOM, entry_id(number))
                self.assertEqual((row['text'], row['preview']), (ALL_TEXT, None))
        self.assertEqual(len(self.store.messages(ROOM, limit=100)), len(cases) + 1)
        # The payload keeps the preview as its member wrote it, for a later version.
        self.assertEqual(json.loads(self.store.entry(ROOM, entry_id(2))['payload'])['preview']['title'], 'x' * 10_000)

    def test_a_new_write_without_a_fit_preview_removes_the_preview_of_the_row(self):
        self.write(1, self.message(preview=PREVIEW))
        self.write(1, self.message('Look at https://example.com/b', rev=2, preview=PREVIEW))
        self.assertIsNone(self.store.message(ROOM, entry_id(1))['preview'])

    def test_the_unread_count_is_the_same_with_and_without_a_preview(self):
        self.write(1, self.message())
        self.assertEqual(self.store.unread(ROOM, MEMBER), 1)
        self.write(2, self.message(preview=PREVIEW))
        self.assertEqual(self.store.unread(ROOM, MEMBER), 2)
        self.assertEqual(self.store.mark_read(ROOM, 2), 2)

    def test_an_older_store_gets_the_columns_and_keeps_its_rows(self):
        db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(db.close)
        db.executescript(team_store.SCHEMA)
        for table in ('team_messages', 'team_directs'):
            self.assertNotIn('preview', {row[1] for row in db.execute(f'PRAGMA table_info({table})')})
        store = TeamStore(db)
        self.add_room(store)
        db.execute("INSERT INTO team_messages VALUES (?, ?, 1, ?, NULL, 'Hello', NULL, '[]', 't', 't', 0, NULL)",
                   (ROOM, entry_id(1), ADMIN))
        db.execute("INSERT INTO team_directs VALUES (?, ?, 1, ?, ?, 'Hi', NULL, 't', 't', 0, NULL, NULL)",
                   (ROOM, entry_id(2), OTHER, OTHER))
        store.migrate()
        store.migrate()
        for table in ('team_messages', 'team_directs'):
            self.assertIn('preview', {row[1] for row in db.execute(f'PRAGMA table_info({table})')})
        self.assertEqual([(row['text'], row['preview']) for row in store.messages(ROOM)], [('Hello', None)])
        self.assertEqual((store.direct(ROOM, entry_id(2))['text'], store.direct(ROOM, entry_id(2))['preview']),
                         ('Hi', None))


class PreviewMacs(TeamMacs):
    def setUp(self):
        super().setUp()
        self.room = self.create(name='North team')['room']
        for service, name in ((self.second, 'Grace'), (self.third, 'Lin')):
            self.join(service, self.invite({'room': self.room})['link'], name)
        self.macs = (self.first, self.second, self.third)
        for service in self.macs:
            service.team_request('GET', '/v1/team/rooms', {}, '', DEVICE, True)
        self.sync()
        self.ada, self.grace, self.lin = (service.team_store.room(self.room)['member'] for service in self.macs)

    def sync(self, *services):
        for _ in range(2):
            for service in services or self.macs:
                service.team_sync_pass()

    def path(self, rest: str = 'messages') -> str:
        return f'/v1/team/rooms/{self.room}/{rest}'

    def page(self, service, path: str, **query) -> list[dict]:
        return self.phone(service)('GET', path, query)['messages']

    def view(self, service, path: str, message: str) -> dict:
        return next(row for row in self.page(service, path) if row['id'] == message)

    def writes(self) -> list[str]:
        """Record the entry ID of each write to the relay from now on."""
        writes = []
        write = self.relay.write
        self.relay.write = lambda *arguments, **options: (writes.append(arguments[3]), write(*arguments, **options))[1]
        return writes

    def write(self, service, entry: str, fields: dict) -> None:
        """Write an entry of a Mac by hand, as a Mac of another version could."""
        room = service.team_store.room(self.room)
        with service._team_room_lock(self.room):
            service._team_write(room, entry, False, fields)

    def box(self, service, entry: str, to: str, plaintext: dict) -> None:
        """Write a `direct` entry of a Mac with a box for `to` by hand."""
        room = service.team_store.room(self.room)
        secrets = team_keys.read(service.root, self.room)
        agreement = service.team_store.member(self.room, to)['agreement']
        box = service.team_helper.seal_direct(self.room, entry, secrets['agreement_key'], room['member'], to,
                                              agreement, json.dumps(plaintext).encode())
        self.write(service, entry, {'kind': 'direct', 'box': box})

    def plaintext(self, service, entry: str) -> dict:
        """The plaintext of the box of a `direct` entry in the store of a Mac (the box of FakeHelper)."""
        box = json.loads(service.team_store.entry(self.room, entry)['payload'])['box']
        sealed = json.loads(base64.urlsafe_b64decode(box + '=' * (-len(box) % 4)))
        return json.loads(base64.b64decode(sealed['plaintext']))


class PreviewChatTests(PreviewMacs, unittest.TestCase):
    def send(self, service, text: str = TEXT, key: str = '', **fields) -> dict:
        return self.phone(service)('POST', self.path(), dict(fields, text=text), key)['message']

    def test_a_send_with_a_preview_shows_on_each_mac(self):
        sent = self.send(self.first, preview=dict(PREVIEW, title='  Example page title  ', site='Example Site '))
        self.assertEqual(sent['preview'], PREVIEW)
        self.assertEqual(json.loads(self.first.team_store.entry(self.room, sent['id'])['payload'])['preview'], PREVIEW)
        plain = self.send(self.first, 'No link here')
        self.assertIsNone(plain['preview'])
        self.assertNotIn('preview', json.loads(self.first.team_store.entry(self.room, plain['id'])['payload']))
        # Null is no preview.
        self.assertIsNone(self.send(self.first, TEXT, preview=None)['preview'])
        self.sync()
        for service in self.macs:
            with self.subTest(mac=service.root.name):
                self.assertEqual(self.view(service, self.path(), sent['id'])['preview'], PREVIEW)
                self.assertIsNone(self.view(service, self.path(), plain['id'])['preview'])

    def test_a_send_with_a_preview_in_a_named_chat(self):
        chat = self.phone(self.first)('POST', self.path('chats'), {'name': 'Design'})['chat']
        self.sync()
        sent = self.send(self.second, chat=chat, preview=PREVIEW)
        self.assertEqual((sent['chat'], sent['preview']), (chat, PREVIEW))
        self.sync()
        self.assertEqual(self.page(self.first, self.path(), chat=chat)[0]['preview'], PREVIEW)
        self.assertEqual(self.page(self.first, self.path()), [])

    def test_each_preview_that_does_not_fit_answers_400_and_writes_nothing(self):
        writes = self.writes()
        for name, preview in misfits().items():
            with self.subTest(case=name):
                self.assertEqual(self.refusal(lambda: self.send(self.first, ALL_TEXT, preview=preview)),
                                 (400, PREVIEW_MISFIT))
        self.assertEqual(writes, [])
        self.assertEqual(self.page(self.first, self.path()), [])

    def test_a_preview_of_another_mac_that_does_not_fit_never_reaches_a_view(self):
        entries = {}
        for name, preview in misfits().items():
            if name == 'a lone surrogate in the title':
                continue  # The encoder of a payload of this Mac cannot write it. PreviewStoreTests reads one.
            entries[name] = token_hex(16)
            self.write(self.third, entries[name], {'kind': 'message', 'text': ALL_TEXT, 'preview': preview})
        self.sync()
        for service in self.macs:
            page = {row['id']: row for row in self.page(service, self.path(), limit='100')}
            for name, entry in entries.items():
                with self.subTest(mac=service.root.name, case=name):
                    self.assertEqual((page[entry]['text'], page[entry]['preview']), (ALL_TEXT, None))
            (room,) = self.rooms(service)
            self.assertEqual(room['last_message']['text'], ALL_TEXT[:200])

    def test_an_edit_keeps_the_preview_while_the_text_has_its_url(self):
        sent = self.send(self.first, preview=PREVIEW)
        edit = self.path(f'messages/{sent["id"]}')
        kept = self.phone(self.first)('PATCH', edit, {'text': f'Now look at {URL}', 'preview': {'url': 'x'}})['message']
        self.assertEqual(kept['preview'], PREVIEW)
        self.sync()
        self.assertEqual(self.view(self.second, self.path(), sent['id'])['preview'], PREVIEW)
        dropped = self.phone(self.first)('PATCH', edit, {'text': 'Look at https://example.com/b'})['message']
        self.assertIsNone(dropped['preview'])
        self.assertNotIn('preview', json.loads(self.first.team_store.entry(self.room, sent['id'])['payload']))
        self.sync()
        self.assertIsNone(self.view(self.second, self.path(), sent['id'])['preview'])
        # A preview that went does not come back with its url.
        self.assertIsNone(self.phone(self.first)('PATCH', edit, {'text': TEXT})['message']['preview'])

    def test_a_repeat_with_the_same_key_sends_one_message_with_its_preview(self):
        key = str(uuid4())
        answers = [self.send(self.first, key=key, preview=PREVIEW) for _ in range(2)]
        self.assertEqual(answers[0], answers[1])
        self.sync()
        self.assertEqual([row['preview'] for row in self.page(self.second, self.path())], [PREVIEW])


class PreviewDirectTests(PreviewMacs, unittest.TestCase):
    def direct(self, member: str, rest: str = 'messages') -> str:
        return self.path(f'directs/{member}/{rest}')

    def send(self, service, member: str, text: str = TEXT, key: str = '', **fields) -> dict:
        return self.phone(service)('POST', self.direct(member), dict(fields, text=text), key)['message']

    def test_a_direct_message_with_a_preview_reaches_only_its_member(self):
        sent = self.send(self.first, self.grace, preview=PREVIEW)
        self.assertEqual(sent['preview'], PREVIEW)
        self.assertEqual(self.plaintext(self.first, sent['id'])['preview'], PREVIEW)
        plain = self.send(self.first, self.grace, 'No link here')
        self.assertIsNone(plain['preview'])
        self.assertNotIn('preview', self.plaintext(self.first, plain['id']))
        self.sync()
        self.assertEqual([row['preview'] for row in self.page(self.second, self.direct(self.ada))], [None, PREVIEW])
        self.assertEqual([row['preview'] for row in self.page(self.first, self.direct(self.grace))], [None, PREVIEW])
        # Lin stores the entries and shows nothing. The payload has no preview outside the box.
        self.assertEqual(self.third.db.execute('SELECT COUNT(*) FROM team_directs').fetchone()[0], 0)
        self.assertNotIn('Example', self.third.team_store.entry(self.room, sent['id'])['payload'])
        self.assertEqual(self.third.team_request('GET', '/v1/team/rooms', {}, '', DEVICE, True)['rooms'][0]['directs'],
                         [])

    def test_each_preview_that_does_not_fit_answers_400_and_writes_nothing(self):
        writes = self.writes()
        for name, preview in misfits().items():
            with self.subTest(case=name):
                self.assertEqual(self.refusal(lambda: self.send(self.first, self.grace, ALL_TEXT, preview=preview)),
                                 (400, PREVIEW_MISFIT))
        self.assertEqual(writes, [])

    def test_a_box_with_a_preview_that_does_not_fit_shows_its_message_without_it(self):
        entries = {}
        for name, preview in misfits().items():
            entries[name] = token_hex(16)
            self.box(self.third, entries[name], self.grace, {'to': self.grace, 'text': ALL_TEXT, 'preview': preview})
        self.sync()
        page = {row['id']: row for row in self.page(self.second, self.direct(self.lin), limit='100')}
        for name, entry in entries.items():
            with self.subTest(case=name):
                self.assertEqual((page[entry]['text'], page[entry]['preview']), (ALL_TEXT, None))

    def test_an_edit_keeps_or_drops_the_preview(self):
        sent = self.send(self.first, self.grace, preview=PREVIEW)
        edit = self.direct(self.grace, f'messages/{sent["id"]}')
        self.assertEqual(self.phone(self.first)('PATCH', edit, {'text': f'Now {URL}'})['message']['preview'], PREVIEW)
        self.assertEqual(self.plaintext(self.first, sent['id'])['preview'], PREVIEW)
        self.sync()
        self.assertEqual(self.page(self.second, self.direct(self.ada))[0]['preview'], PREVIEW)
        self.assertIsNone(self.phone(self.first)('PATCH', edit, {'text': 'No link now'})['message']['preview'])
        self.assertNotIn('preview', self.plaintext(self.first, sent['id']))
        self.sync()
        self.assertIsNone(self.page(self.second, self.direct(self.ada))[0]['preview'])

    def test_a_repeat_with_the_same_key_and_the_unread_count(self):
        key = str(uuid4())
        answers = [self.send(self.first, self.grace, key=key, preview=PREVIEW) for _ in range(2)]
        self.assertEqual(answers[0], answers[1])
        self.sync()
        self.assertEqual([row['preview'] for row in self.page(self.second, self.direct(self.ada))], [PREVIEW])
        unread = [chat['unread'] for chat in
                  self.second.team_request('GET', '/v1/team/rooms', {}, '', DEVICE, True)['rooms'][0]['directs']]
        self.send(self.first, self.grace, TEXT)
        self.sync()
        after = [chat['unread'] for chat in
                 self.second.team_request('GET', '/v1/team/rooms', {}, '', DEVICE, True)['rooms'][0]['directs']]
        self.assertEqual((unread, after), ([1], [2]))


class PreviewPushTests(TeamPushMacs, unittest.TestCase):
    """Grace (`first`) writes. Ada (`second`) has the paired phone."""

    def test_a_preview_changes_no_unread_count_read_position_or_push_item(self):
        self.seal()
        seen = []
        for preview in (None, PREVIEW):
            sent = self.send(TEXT, preview=preview)
            self.second.team_sync_pass()
            (room,) = self.rooms(self.second)
            flush(self.second)
            seen.append((sent, room['unread'], self.second.team_store.read_position(self.room)))
        (plain, unread, position), (with_preview, unread_2, position_2) = seen
        self.assertEqual((plain['preview'], with_preview['preview']), (None, PREVIEW))
        self.assertEqual((unread, unread_2, position_2), (1, 2, position))
        # One item for each message, the same but for the message ID, and the same sealed alert.
        plain_item, preview_item = self.items()
        self.assertEqual((plain_item[6], preview_item[6]), (plain['id'], with_preview['id']))
        self.assertEqual(plain_item[:6] + plain_item[7:], preview_item[:6] + preview_item[7:])
        first, second = self.relay.sealed
        self.assertEqual(first, second)
        self.assertEqual(first['aps']['alert']['body'], TEXT)
        self.phone(self.second)('POST', f'/v1/team/rooms/{self.room}/read', {'first': with_preview['first']})
        (room,) = self.rooms(self.second)
        self.assertEqual((room['unread'], self.second.team_store.read_position(self.room)), (0, with_preview['first']))


class PreviewAgentTests(DirectTurnMacs, unittest.TestCase):
    """Ada writes to the bot Reporter of the Mac for a team only (`only`): in its direct chat and in the Team chat."""

    def capture(self) -> list[dict]:
        turns = []
        self.serve(lambda turn: (turns.append(turn), reply(f'Seen: {URL}')(turn))[1])
        return turns

    def test_nothing_of_a_preview_goes_into_a_turn_and_the_agent_sends_none(self):
        turns = self.capture()
        self.save()
        self.passes()
        page = next(page['id'] for page in self.first.documents()['documents'] if 'team' in page)
        # History of each chat: it starts no turn, and it is the context of the next turn.
        self.phone(self.first)('POST', self.path(), {'text': f'Earlier {URL}', 'preview': PREVIEW})
        self.send(f'Earlier in the team {URL}', preview=PREVIEW)
        self.only.team_store.clock = lambda: time.time() + team_store.TRIGGER_SECONDS + 1
        self.only.team_sync_pass()
        self.only.team_store.clock = time.time
        sent = self.dm(f'What is at {URL}?', preview=PREVIEW, about_document_id=page)
        self.assertEqual(sent['preview'], PREVIEW)
        self.send(f'@Reporter what is at {URL}?', mentions=[self.mention], preview=PREVIEW)
        self.sync()
        self.run_turns()
        self.assertEqual(len(turns), 2)
        for turn in turns:
            text = json.dumps(turn, ensure_ascii=False)
            self.assertIn(URL, turn['text'])
            self.assertIn(f'Earlier', turn['team']['context'])
            for word in (PREVIEW['title'], PREVIEW['site']):
                self.assertNotIn(word, text)
        (direct,) = [turn for turn in turns if turn['chat_id'] == self.direct_chat_id()]
        self.assertIn('Weekly report', direct['team']['context'])
        # The Mac for a team only keeps the preview of the person in its store, and no turn reads it there.
        with self.only.lock:
            stored = self.only.db.execute('SELECT preview FROM team_directs WHERE room=? AND entry=?',
                                          (self.room, sent['id'])).fetchone()[0]
        self.assertEqual(json.loads(stored), PREVIEW)
        # Each answer of the agent has no preview: in the payload, in the box and in the views.
        self.first.team_sync_pass()
        answers = [row for row in self.phone(self.first)('GET', self.path(), {})['messages'] if not row['this_mac']]
        team = [row for row in self.phone(self.first)('GET', f'/v1/team/rooms/{self.room}/messages', {})['messages']
                if row['bot']]
        self.assertEqual(([row['preview'] for row in answers], [row['preview'] for row in team]), ([None], [None]))
        self.assertNotIn('preview', self.plaintext(self.first, answers[0]['id']))
        self.assertNotIn('preview', json.loads(self.first.team_store.entry(self.room, team[0]['id'])['payload']))


if __name__ == '__main__':
    unittest.main()
