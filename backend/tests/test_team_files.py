"""Files in the messages of the team chats (coconut/team_files.py, docs/team-log.md "Files").

FileRuleTests checks the readers of an item and of a piece. FileStoreTests gives TeamStore.accept entries as the relay
sends them, also entries that another member made to break the rules. The other tests run Macs with FakeRelay and
FakeHelper (team_fakes.py), as the app calls them: Ada's Mac (`first`) made the room, and Grace (`second`) joined it.
scripts/test_team_log.py runs the same path through the real relay in Miniflare and the real helper.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import sqlite3
import threading
import time
import unittest
from uuid import uuid4

from coconut import team_files, team_keys, team_store
from coconut.team_files import (FILE_BUDGET, GONE_DRAFT, GONE_FILE, NOT_COMPLETE, SPACE_FULL, TEAM_ONLY, FileContent,
                                file_entry)
from coconut.team_relay import TeamRelayError
from coconut.team_store import CHUNK, MAX_FILE, STORED, TeamStore, file_item, files_fields, message_fields, piece_fields
from team_fakes import DEVICE, TeamMacs
from test_team_push import TeamPushMacs, flush
from test_team_routes import PHONE
from test_team_routes import Mac as ListenerMac
from test_team_turns import TeamTurnMacs, reply

ROOM = 'a' * 64
ADMIN = '1' * 64
MEMBER = '2' * 64
OTHER = '3' * 64
START = 1_800_000_000_000
FILE = 'f' * 32


def entry_id(number: int) -> str:
    return f'{number:032x}'


def item(data: bytes, file: str = FILE, **fields) -> dict:
    return dict(id=file, name='report.pdf', type='application/pdf', size=len(data),
                hash=hashlib.sha256(data).hexdigest(), **fields)


def piece_id(index: int, file: str = FILE, member: str = ADMIN) -> str:
    """The entry ID of a piece in the store tests: a test value, not the HMAC of a writer."""
    return hashlib.sha256(f'{member}:{file}:{index}'.encode()).hexdigest()[:32]


def pieces(data: bytes) -> list[bytes]:
    return [data[start:start + CHUNK] for start in range(0, len(data), CHUNK)]


class FileRuleTests(unittest.TestCase):
    def test_an_item_that_fits_and_each_one_that_does_not(self):
        good = item(b'abc', width=640, height=480)
        self.assertEqual(file_item(good), good)
        self.assertEqual(file_item(dict(item(b'abc'), type=None)), dict(item(b'abc'), type=None, width=None,
                                                                         height=None))
        self.assertEqual(file_item(dict(item(b'x'), size=MAX_FILE, name='n' * 160))['size'], MAX_FILE)
        misfits = {
            'an ID that is no entry ID': dict(good, id='F' * 32),
            'an empty name': dict(good, name=''),
            'a name of 161 code points': dict(good, name='n' * 161),
            'a slash in the name': dict(good, name='a/b'),
            'a backslash in the name': dict(good, name='a\\b'),
            'a control character in the name': dict(good, name='a\x00b'),
            'a line break in the name': dict(good, name='a\nb'),
            'the name .': dict(good, name='.'),
            'the name ..': dict(good, name='..'),
            'a lone surrogate in the name': dict(good, name='a\ud800'),
            'a type of 101 characters': dict(good, type='a' * 101),
            'a type that is not ASCII': dict(good, type='image/pngé'),
            'an empty type': dict(good, type=''),
            'a size of 0': dict(good, size=0),
            'a size over 4 MiB': dict(good, size=MAX_FILE + 1),
            'a size that is true': dict(good, size=True),
            'a size that is a float': dict(good, size=3.0),
            'a hash in capitals': dict(good, hash=good['hash'].upper()),
            'a short hash': dict(good, hash='ab'),
            'a width without a height': dict(good, height=None),
            'a width of 0': dict(good, width=0),
            'a height over 20,000': dict(good, height=20_001),
            'a list': [good],
        }
        for name, value in misfits.items():
            with self.subTest(case=name):
                self.assertIsNone(file_item(value))

    def test_a_files_value_that_does_not_fit_is_left_out(self):
        one = item(b'abc')
        self.assertEqual(len(files_fields([one, dict(one, id='e' * 32)])), 2)
        for name, value in {'null': None, 'an empty list': [], 'five items': [dict(one, id=f'{n:032x}') for n in range(5)],
                            'the same ID twice': [one, one], 'one bad item': [one, dict(one, id='e' * 32, size=0)],
                            'an object': one}.items():
            with self.subTest(case=name):
                self.assertIsNone(files_fields(value))
                # The text keeps the message, and an empty text makes it not valid.
                self.assertEqual(message_fields({'text': 'Hi', 'files': value})['files'], [])
                self.assertIsNone(message_fields({'text': '', 'files': value}))
        self.assertEqual(message_fields({'text': '', 'files': [one]})['files'], [file_item(one)])
        self.assertIsNone(message_fields({'text': '', 'files': None}))

    def test_a_piece_that_fits_and_each_one_that_does_not(self):
        data = base64.b64encode(b'x' * CHUNK).decode()
        self.assertEqual(piece_fields({'file': FILE, 'index': 5, 'data': data}),
                         {'file': FILE, 'index': 5, 'data': b'x' * CHUNK})
        misfits = {
            'an index of 6': {'file': FILE, 'index': 6, 'data': 'eA=='},
            'an index of -1': {'file': FILE, 'index': -1, 'data': 'eA=='},
            'an index that is true': {'file': FILE, 'index': True, 'data': 'eA=='},
            'a file that is no ID': {'file': 'x', 'index': 0, 'data': 'eA=='},
            'empty data': {'file': FILE, 'index': 0, 'data': ''},
            'data that is not base64': {'file': FILE, 'index': 0, 'data': 'e A=='},
            'urlsafe base64': {'file': FILE, 'index': 0, 'data': '-_-_'},
            'data over CHUNK bytes': {'file': FILE, 'index': 0, 'data': base64.b64encode(b'x' * (CHUNK + 1)).decode()},
            'data that is a number': {'file': FILE, 'index': 0, 'data': 7},
            'data that is not ASCII': {'file': FILE, 'index': 0, 'data': 'éé=='},
        }
        for name, value in misfits.items():
            with self.subTest(case=name):
                self.assertIsNone(piece_fields(value))


class FileStoreTests(unittest.TestCase):
    """The reading rules of a `file` entry and of `files`. MEMBER is this Mac, and ADMIN and OTHER are the other
    members."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.executescript(team_store.SCHEMA)
        self.store = TeamStore(self.db)
        self.store.migrate()
        self.add_room(self.store)
        self.seq = 0
        self.now = START / 1000
        self.store.clock = lambda: self.now

    @staticmethod
    def add_room(store):
        store.add_room(room=ROOM, relay='https://relay.example', name='North team', member=MEMBER, admin=ADMIN,
                       state=team_store.ACTIVE, epoch=0, profile='{}', created_at='2026-11-16T12:00:00Z')

    def write(self, entry: str, payload: dict, member: str = ADMIN):
        self.seq += 1
        existing = self.store.entry(ROOM, entry)
        entry = dict(seq=self.seq, first=existing['first'] if existing else self.seq, id=entry,
                     member=member, at=START + self.seq, kept=False, body=b'')
        opened = team_store.read_entry({'epoch': 0, 'payload': json.dumps(payload).encode()})
        return self.store.accept(self.store.room(ROOM), entry, opened)

    def piece(self, index: int, data: bytes, member: str = ADMIN, file: str = FILE, rev: int = 1):
        return self.write(piece_id(index, file, member), {'kind': 'file', 'rev': rev, 'file': file, 'index': index,
                                                      'data': base64.b64encode(data).decode()}, member)

    def message(self, number: int, files: list, text: str = '', member: str = ADMIN, **fields):
        return self.write(entry_id(number), {'kind': 'message', 'rev': 1, 'text': text, 'files': files, **fields},
                          member)

    def send(self, data: bytes, number: int = 1, member: str = ADMIN):
        for index, part in enumerate(pieces(data)):
            self.piece(index, part, member)
        self.message(number, [item(data)], member=member)

    def views(self, number: int = 1) -> list[dict]:
        row = self.store.message(ROOM, entry_id(number))
        return self.store.file_views(ROOM, row['member'], row['files'])

    def complete(self, number: int = 1) -> bool:
        return self.views(number)[0]['complete']

    def stored_pieces(self) -> int:
        return self.db.execute('SELECT COUNT(*) FROM team_file_pieces').fetchone()[0]

    def test_the_number_of_pieces_at_the_borders(self):
        for number, size in enumerate((1, CHUNK, CHUNK + 1, MAX_FILE), start=1):
            with self.subTest(size=size):
                data = bytes(range(256)) * (size // 256) + b'z' * (size % 256)
                file = f'{number:032x}'
                parts = pieces(data)
                self.assertEqual(len(parts), {1: 1, CHUNK: 1, CHUNK + 1: 2, MAX_FILE: 6}[size])
                self.message(number, [item(data, file)])
                for index, part in enumerate(parts[:-1]):
                    self.piece(index, part, file=file)
                self.assertFalse(self.complete(number))
                self.piece(len(parts) - 1, parts[-1], file=file)
                self.assertTrue(self.complete(number))
                self.assertEqual(self.store.file_bytes(ROOM, ADMIN, item(data, file)), data)

    def test_a_message_before_its_pieces_and_the_event_of_the_chat(self):
        data = b'a' * (CHUNK + 10)
        self.assertEqual(self.message(1, [item(data)]), (STORED, None))
        self.assertEqual(self.views(), [{'id': FILE, 'name': 'report.pdf', 'type': 'application/pdf',
                                         'size': len(data), 'width': None, 'height': None, 'complete': False}])
        self.store.chatted.clear()
        self.piece(1, data[CHUNK:])
        self.assertEqual(self.store.chatted, {ROOM}, 'A piece of a shown file changes the chat.')
        self.piece(0, data[:CHUNK])
        self.assertTrue(self.complete())
        # A piece of a file that no message names changes no chat.
        self.store.chatted.clear()
        self.piece(0, b'x', file='e' * 32)
        self.assertEqual(self.store.chatted, set())

    def test_a_piece_of_another_member_never_counts(self):
        data = b'b' * 100
        self.message(1, [item(data)])
        self.piece(0, data, member=OTHER)
        self.assertFalse(self.complete())
        self.piece(0, data, member=ADMIN, file=FILE)
        self.assertTrue(self.complete())

    def test_a_wrong_hash_a_short_piece_and_a_missing_piece(self):
        data = b'c' * (CHUNK + 5)
        self.message(1, [dict(item(data), hash='0' * 64)])
        self.piece(0, data[:CHUNK])
        self.piece(1, data[CHUNK:])
        self.assertFalse(self.complete())
        self.assertIsNone(self.store.file_bytes(ROOM, ADMIN, dict(item(data), hash='0' * 64)))
        # The sizes add up, but the first piece is short.
        self.message(2, [item(data, 'e' * 32)])
        self.piece(0, data[:CHUNK - 1], file='e' * 32)
        self.piece(1, data[CHUNK - 1:], file='e' * 32)
        self.assertFalse(self.complete(2))

    def test_the_payload_of_a_piece_has_no_data_and_a_piece_that_does_not_fit_shows_nothing(self):
        self.piece(0, b'hello')
        payload = json.loads(self.store.entry(ROOM, piece_id(0))['payload'])
        self.assertEqual(payload, {'kind': 'file', 'rev': 1, 'file': FILE, 'index': 0, 'size': 5})
        self.assertEqual(self.db.execute('SELECT data FROM team_file_pieces').fetchone()[0], b'hello')
        self.write(piece_id(9), {'kind': 'file', 'rev': 1, 'file': FILE, 'index': 9, 'data': 'aGk='})
        self.assertEqual(json.loads(self.store.entry(ROOM, piece_id(9))['payload']),
                         {'kind': 'file', 'rev': 1, 'file': FILE, 'index': 9})
        self.assertEqual(self.stored_pieces(), 1)

    def test_a_delete_of_a_piece_removes_its_bytes(self):
        data = b'd' * 10
        self.send(data)
        self.assertTrue(self.complete())
        self.write(piece_id(0), {'kind': 'delete', 'rev': 2})
        self.assertEqual(self.stored_pieces(), 0)
        self.assertFalse(self.complete())

    def test_the_bytes_go_with_a_delete_an_erase_or_a_removed_chat(self):
        for case in ('delete', 'erase', 'chat'):
            with self.subTest(case=case):
                self.db.execute('DELETE FROM team_file_pieces')
                number = {'delete': 1, 'erase': 2, 'chat': 3}[case]
                chat = entry_id(99)
                if case == 'chat':
                    self.write(chat, {'kind': 'chat', 'rev': 1, 'name': 'Design'})
                data = case.encode()
                file = f'{number:032x}'
                self.piece(0, data, file=file)
                self.message(number, [item(data, file)], **({'chat': chat} if case == 'chat' else {}))
                self.assertEqual(self.stored_pieces(), 1)
                if case == 'delete':
                    self.write(entry_id(number), {'kind': 'delete', 'rev': 2})
                elif case == 'erase':
                    self.write(entry_id(50), {'kind': 'erase', 'rev': 1, 'entry': entry_id(number)})
                else:
                    self.write(chat, {'kind': 'delete', 'rev': 2})
                self.assertEqual(self.stored_pieces(), 0)
                self.assertIsNone(self.store.shown_file(ROOM, file))

    def test_a_file_that_another_message_of_the_member_names_keeps_its_bytes(self):
        data = b'e'
        self.piece(0, data)
        self.message(1, [item(data)])
        self.message(2, [item(data)])
        self.write(entry_id(1), {'kind': 'delete', 'rev': 2})
        self.assertTrue(self.complete(2))

    def test_the_sweep_of_pieces_that_no_message_names(self):
        self.piece(0, b'old', file='e' * 32)
        self.send(b'named')
        self.now += team_store.PIECE_SECONDS - 1
        self.piece(0, b'young', file='d' * 32)
        self.assertEqual(self.stored_pieces(), 3)
        self.now += 2
        self.piece(1, b'new', file='c' * 32)
        files = {row[0] for row in self.db.execute('SELECT file FROM team_file_pieces')}
        self.assertEqual(files, {FILE, 'd' * 32, 'c' * 32})

    def test_the_text_of_the_list_and_of_a_notification(self):
        self.assertEqual(team_store.shown_text('Hi', None), 'Hi')
        image = json.dumps([dict(item(b'x'), type='image/png'), item(b'y', 'e' * 32)])
        self.assertEqual(team_store.shown_text('', image), 'Photo')
        self.assertEqual(team_store.shown_text('', json.dumps([item(b'y')])), 'report.pdf')
        self.assertEqual(team_store.shown_text('', json.dumps([dict(item(b'y'), type=None)])), 'report.pdf')
        self.assertEqual(team_store.shown_text('Look', image), 'Look')

    def older_store(self) -> sqlite3.Connection:
        """A store of a version without files: it kept each `file` entry as an entry of an unknown kind with its
        base64 text, showed a message with files and a text without them, and no message with an empty text."""
        db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(db.close)
        db.executescript(team_store.SCHEMA)
        old = TeamStore(db)
        self.add_room(old)
        data = b'old bytes'
        rows = ((entry_id(10), 'file', {'kind': 'file', 'rev': 1, 'file': FILE, 'index': 0,
                                        'data': base64.b64encode(data).decode()}),
                (entry_id(1), 'message', {'kind': 'message', 'rev': 1, 'text': '', 'files': [item(data)]}),
                (entry_id(2), 'message', {'kind': 'message', 'rev': 1, 'text': 'Hi', 'files': [item(data)]}))
        for seq, (entry, kind, payload) in enumerate(rows, start=1):
            db.execute('INSERT INTO team_entries (room, id, seq, first, member, kind, rev, at, kept, payload) '
                       'VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?)', (ROOM, entry, seq, seq, ADMIN, kind, START + seq,
                                                                 json.dumps(payload)))
        db.execute("INSERT INTO team_messages VALUES (?, ?, 3, ?, NULL, 'Hi', NULL, '[]', 't', 't', 0, NULL)",
                   (ROOM, entry_id(2), ADMIN))
        return db

    def test_an_older_store_opens_and_reads_its_files_once(self):
        db = self.older_store()
        store = TeamStore(db)
        store.migrate()
        self.assertNotIn('data', db.execute('SELECT payload FROM team_entries WHERE kind=?', ('file',)).fetchone()[0])
        self.assertEqual([row['entry'] for row in store.messages(ROOM)], [entry_id(2), entry_id(1)])
        self.assertEqual(store.file_bytes(ROOM, ADMIN, item(b'old bytes')), b'old bytes')
        changes = db.total_changes
        TeamStore(db).migrate()
        self.assertEqual(db.total_changes, changes, 'A second start changes nothing.')

    def test_a_stop_in_the_pass_leaves_the_store_of_before(self):
        db = self.older_store()
        store = TeamStore(db)

        def stop(*arguments, **options):
            raise KeyboardInterrupt('The process stops here.')
        store._show_piece = stop
        with self.assertRaises(KeyboardInterrupt):
            store.migrate()
        self.assertNotIn('files', {row[1] for row in db.execute('PRAGMA table_info(team_messages)')})
        self.assertIn('data', db.execute("SELECT payload FROM team_entries WHERE kind='file'").fetchone()[0])
        TeamStore(db).migrate()
        self.assertEqual(len(TeamStore(db).messages(ROOM)), 2)


class FileMacs(TeamMacs):
    def setUp(self):
        super().setUp()
        self.room = self.create(name='North team')['room']
        self.join(self.second, self.invite({'room': self.room})['link'], 'Grace')
        self.macs = (self.first, self.second)
        self.sync()

    def sync(self):
        for _ in range(2):
            for service in self.macs:
                service.team_sync_pass()

    def path(self, rest: str = 'messages') -> str:
        return f'/v1/team/rooms/{self.room}/{rest}'

    def upload(self, service, data: bytes, name: str = 'report.pdf', **fields) -> dict:
        body = dict(name=name, content=base64.b64encode(data).decode(), **fields)
        return self.phone(service)('POST', self.path('files'), body)['file']

    def send(self, service, files: list[str], text: str | None = None, key: str = '', **fields) -> dict:
        body = dict(fields, files=files, **({'text': text} if text is not None else {}))
        return self.phone(service)('POST', self.path(), body, key)['message']

    def writes(self) -> list[str]:
        """Record the entry ID of each write to the relay from now on."""
        writes = []
        write = self.relay.write
        self.relay.write = lambda *arguments, **options: (writes.append(arguments[3]), write(*arguments, **options))[1]
        return writes

    def token(self, service) -> str:
        return team_keys.read(service.root, self.room)['token']

    def drafts(self, service) -> int:
        return service.db.execute('SELECT COUNT(*) FROM team_file_drafts').fetchone()[0]

    def content(self, service, file: str):
        return self.phone(service)('GET', self.path(f'files/{file}'), {})


class FileRouteTests(FileMacs, unittest.TestCase):
    def test_a_draft_and_its_rules(self):
        writes = self.writes()
        draft = self.upload(self.first, b'png', 'photo.png', type='image/png', width=640, height=480)
        self.assertEqual({name: draft[name] for name in ('name', 'type', 'size', 'width', 'height')},
                         {'name': 'photo.png', 'type': 'image/png', 'size': 3, 'width': 640, 'height': 480})
        self.assertTrue(team_store.entry_id(draft['id']))
        self.assertEqual(self.drafts(self.first), 1)
        upload = lambda body: self.phone(self.first)('POST', self.path('files'), body)
        content = base64.b64encode(b'x').decode()
        self.assertEqual(self.refusal(upload, {'name': 'a', 'content': ''})[0], 413)
        self.assertEqual(self.refusal(upload, {'name': 'a', 'content': base64.b64encode(b'x' * (MAX_FILE + 1)).decode()}),
                         (413, team_files.FILE_SIZE))
        for body in ({'name': '../a', 'content': content}, {'name': 'a', 'content': 'not base64!'},
                     {'name': 'a', 'content': content, 'width': 10}, {'name': 'a', 'content': content, 'type': 7}):
            with self.subTest(body=body):
                self.assertEqual(self.refusal(upload, body)[0], 400)
        self.assertEqual(writes, [], 'Nothing goes to the relay.')
        self.assertEqual(self.phone(self.first)('DELETE', self.path(f'files/{draft["id"]}'), {}),
                         {'room': self.room, 'file': draft['id'], 'deleted': True})
        self.assertEqual(self.refusal(self.phone(self.first), 'DELETE', self.path(f'files/{draft["id"]}'), {}),
                         (404, GONE_DRAFT))
        # The token of the bridge file reaches no file route on a person's Mac.
        self.assertEqual(self.refusal(self.first.team_request, 'POST', self.path('files'),
                                      {'name': 'a', 'content': content})[0], 403)

    def test_a_draft_goes_24_hours_after_its_upload(self):
        clock = [time.time()]
        self.first.team_store.clock = lambda: clock[0]
        old = self.upload(self.first, b'old')
        clock[0] += team_files.DRAFT_SECONDS - 1
        young = self.upload(self.first, b'young')
        clock[0] += 2
        self.upload(self.first, b'new')
        ids = {row[0] for row in self.first.db.execute('SELECT id FROM team_file_drafts')}
        self.assertNotIn(old['id'], ids)
        self.assertIn(young['id'], ids)

    def test_a_send_writes_the_pieces_then_the_message_and_the_other_mac_gets_the_file(self):
        data = bytes(range(256)) * 4000  # 1,024,000 bytes: two pieces.
        draft = self.upload(self.first, data)
        writes = self.writes()
        sent = self.send(self.first, [draft['id']])
        token = self.token(self.first)
        self.assertEqual(writes, [file_entry(token, self.room, draft['id'], 0),
                                  file_entry(token, self.room, draft['id'], 1), sent['id']])
        self.assertEqual(sent['text'], '')
        self.assertEqual(sent['files'], [dict(draft, complete=True)])
        self.assertEqual(self.drafts(self.first), 0)
        payload = json.loads(self.first.team_store.entry(self.room, sent['id'])['payload'])
        self.assertEqual(payload['files'], [{'id': draft['id'], 'name': 'report.pdf', 'size': len(data),
                                             'hash': hashlib.sha256(data).hexdigest()}])
        self.sync()
        (seen,) = self.phone(self.second)('GET', self.path(), {})['messages']
        self.assertEqual(seen['files'], [dict(draft, complete=True)])
        answer = self.content(self.second, draft['id'])
        self.assertEqual(answer, FileContent(data, 'report.pdf', 'application/octet-stream'))
        # A message without files has the key with an empty list.
        plain = self.phone(self.first)('POST', self.path(), {'text': 'Hi'})['message']
        self.assertEqual(plain['files'], [])

    def test_a_draft_that_does_not_exist_and_a_repeat_with_the_same_key(self):
        self.assertEqual(self.refusal(self.send, self.first, ['e' * 32], 'Hi'), (400, GONE_DRAFT))
        self.assertEqual(self.refusal(self.send, self.first, [], 'Hi')[0], 400)
        draft = self.upload(self.first, b'abc')
        key = str(uuid4())
        first = self.send(self.first, [draft['id']], 'Look', key)
        # The app sends the same request again after a lost answer. The service runs it again: no saved answer.
        self.first._team_replies.clear()
        writes = self.writes()
        self.assertEqual(self.send(self.first, [draft['id']], 'Look', key), first)
        self.assertEqual(writes, [])
        self.sync()
        self.assertEqual(len(self.phone(self.second)('GET', self.path(), {})['messages']), 1)

    def test_the_space_of_a_member_and_a_507_of_the_relay(self):
        ada = self.first.team_store.room(self.room)['member']
        now = int(time.time() * 1000)
        with self.first.transaction():
            for number, (size, at) in enumerate(((FILE_BUDGET - 10, now - 1000),
                                                 (FILE_BUDGET, now - team_files.SPACE_SECONDS * 1000 - 5000))):
                self.first.db.execute('INSERT INTO team_entries (room, id, seq, first, member, kind, rev, at, kept, '
                                      "payload) VALUES (?, ?, 0, 0, ?, 'file', 1, ?, 0, ?)",
                                      (self.room, entry_id(number + 900), ada, at,
                                       json.dumps({'kind': 'file', 'file': entry_id(number + 800), 'size': size})))
        over = self.upload(self.first, b'x' * 11)
        writes = self.writes()
        self.assertEqual(self.refusal(self.send, self.first, [over['id']]), (413, SPACE_FULL))
        self.assertEqual(writes, [])
        self.assertEqual(self.drafts(self.first), 1)
        fits = self.upload(self.first, b'x' * 10)
        self.send(self.first, [fits['id']])
        # The relay answers 507: its own count of the space is full.
        with self.first.transaction():
            self.first.db.execute("DELETE FROM team_entries WHERE kind='file'")
        write = self.relay.write

        def full(relay, room, token, entry, kept, body, tries=1):
            if json.loads(base64.b64decode(json.loads(base64.b64decode(body))['payload']))['kind'] == 'file':
                raise TeamRelayError(507, 'The relay answered 507.')
            return write(relay, room, token, entry, kept, body, tries)
        self.relay.write = full
        self.assertEqual(self.refusal(self.send, self.first, [over['id']]), (413, SPACE_FULL))

    def test_an_edit_keeps_the_files_and_takes_an_empty_text(self):
        draft = self.upload(self.first, b'abc')
        sent = self.send(self.first, [draft['id']], 'First text')
        edit = self.path(f'messages/{sent["id"]}')
        edited = self.phone(self.first)('PATCH', edit, {'text': ''})['message']
        self.assertEqual((edited['text'], [file['id'] for file in edited['files']]), ('', [draft['id']]))
        self.sync()
        (seen,) = self.phone(self.second)('GET', self.path(), {})['messages']
        self.assertEqual((seen['text'], seen['files'][0]['complete']), ('', True))
        plain = self.phone(self.first)('POST', self.path(), {'text': 'Hi'})['message']
        self.assertEqual(self.refusal(self.phone(self.first), 'PATCH', self.path(f'messages/{plain["id"]}'),
                                      {'text': ''})[0], 400)

    def test_the_download_route(self):
        self.assertEqual(self.refusal(self.content, self.first, 'e' * 32), (404, GONE_FILE))
        data = b'q' * (CHUNK + 1)
        draft = self.upload(self.first, data, 'notes.txt', type='text/plain')
        sent = self.send(self.first, [draft['id']], 'Notes')
        self.sync()
        # Grace has the message without its last piece.
        with self.second.transaction():
            self.second.db.execute('DELETE FROM team_file_pieces WHERE piece=1')
        self.second.team_store.verified.clear()
        self.assertEqual(self.refusal(self.content, self.second, draft['id']), (409, NOT_COMPLETE))
        self.assertEqual(self.content(self.first, draft['id']), FileContent(data, 'notes.txt', 'text/plain'))
        self.phone(self.first)('DELETE', self.path(f'messages/{sent["id"]}'), {})
        self.sync()
        for service in self.macs:
            with self.subTest(mac=service.root.name):
                self.assertEqual(self.refusal(self.content, service, draft['id']), (404, GONE_FILE))
                self.assertEqual(service.db.execute('SELECT COUNT(*) FROM team_file_pieces').fetchone()[0], 0)

    def test_the_admin_erases_a_message_and_its_bytes_go(self):
        draft = self.upload(self.second, b'grace')
        sent = self.send(self.second, [draft['id']])
        self.sync()
        self.phone(self.first)('DELETE', self.path(f'messages/{sent["id"]}'), {})
        self.sync()
        for service in self.macs:
            self.assertEqual(service.db.execute('SELECT COUNT(*) FROM team_file_pieces').fetchone()[0], 0)

    def test_the_list_shows_photo_or_the_name(self):
        image = self.upload(self.first, b'png', 'cat.png', type='image/png', width=2, height=2)
        self.send(self.first, [image['id']])
        self.sync()
        (room,) = self.rooms(self.second)
        self.assertEqual(room['last_message']['text'], 'Photo')
        other = self.upload(self.first, b'pdf', 'plan.pdf', type='application/pdf')
        self.send(self.first, [other['id']], '')
        (room,) = self.rooms(self.second)
        self.assertEqual(room['last_message']['text'], 'plan.pdf')

    def test_a_direct_message_takes_no_file(self):
        grace = self.second.team_store.room(self.room)['member']
        for service in self.macs:
            service.team_request('GET', '/v1/team/rooms', {}, '', DEVICE, True)
        self.sync()
        draft = self.upload(self.first, b'abc')
        direct = self.path(f'directs/{grace}/messages')
        self.assertEqual(self.refusal(self.phone(self.first), 'POST', direct, {'text': 'Hi', 'files': [draft['id']]})[0],
                         400)
        sent = self.phone(self.first)('POST', direct, {'text': 'Hi'})['message']
        self.assertNotIn('files', sent)


class FileListenerTests(FileMacs, unittest.TestCase):
    """The download route on the real listeners: server.py sends the bytes, not JSON."""

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        return server

    def test_the_bytes_and_their_headers_on_each_listener(self):
        mac = ListenerMac(self, self.first)
        data = bytes(range(256)) * 3
        draft = self.upload(self.first, data, 'Q3 plan é.pdf', type='application/pdf')
        self.send(self.first, [draft['id']])
        path = self.path(f'files/{draft["id"]}')
        for server, token, headers in ((mac.server, mac.device, {}), (mac.tunnel, mac.phone, PHONE)):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            self.addCleanup(connection.close)
            connection.request('GET', path, headers=dict(headers, Authorization=f'Bearer {token}'))
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (200, data))
            self.assertEqual(response.getheader('Content-Type'), 'application/pdf')
            self.assertEqual(response.getheader('Content-Disposition'), 'attachment; filename="Q3 plan .pdf"')
        # A file that is not there answers JSON with the text for a person.
        connection = http.client.HTTPConnection('127.0.0.1', mac.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request('GET', self.path(f'files/{"e" * 32}'), headers={'Authorization': f'Bearer {mac.device}'})
        response = connection.getresponse()
        self.assertEqual((response.status, json.load(response)), (404, {'error': GONE_FILE}))


class FilePushTests(TeamPushMacs, unittest.TestCase):
    """Grace (`first`) writes. Ada (`second`) has the paired phone."""

    def test_the_notification_of_a_message_with_an_empty_text(self):
        self.seal()
        for name, kind in (('cat.png', 'image/png'), ('plan.pdf', 'application/pdf')):
            draft = self.phone(self.first)('POST', f'/v1/team/rooms/{self.room}/files',
                                           {'name': name, 'type': kind, 'content': 'eA=='})['file']
            self.send('', files=[draft['id']])
            self.second.team_sync_pass()
            flush(self.second)
        self.assertEqual([payload['aps']['alert']['body'] for payload in self.relay.sealed], ['Photo', 'plan.pdf'])


class FileTeamOnlyTests(TeamTurnMacs, unittest.TestCase):
    def test_the_mac_for_a_team_only_takes_no_file_and_the_agent_gets_one_line_for_each_file(self):
        self.assertEqual(self.refusal(self.call, 'POST', f'/v1/team/rooms/{self.room}/files',
                                      {'name': 'a', 'content': 'eA=='}), (409, TEAM_ONLY))
        draft = self.call('POST', f'/v1/team/rooms/{self.room}/files',
                          {'name': 'plan.pdf', 'type': 'application/pdf', 'content': 'eA=='}, service=self.first)['file']
        turns = []
        self.serve(lambda turn: (turns.append(turn), reply('Seen.')(turn))[1])
        self.send('@Reporter look', mentions=[self.mention], files=[draft['id']])
        self.sync()
        self.run_turns()
        (turn,) = turns
        self.assertEqual(turn['text'], '@Reporter look\n[File: plan.pdf, application/pdf, 1 bytes]')


if __name__ == '__main__':
    unittest.main()
