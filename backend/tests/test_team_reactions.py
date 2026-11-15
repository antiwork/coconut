"""Reactions on the messages of the team chats (coconut/team_reactions.py, docs/team-log.md "Reactions").

ReactionStoreTests gives TeamStore.accept entries as the relay sends them, also entries that another member made to
break the rules. The other tests run Macs with FakeRelay and FakeHelper (team_fakes.py), as the app calls them: Ada's
Mac (`first`) made the room, and Grace (`second`) and Lin (`third`) joined it. scripts/test_team_log.py runs the same
path through the real relay in Miniflare and the real helper.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest import mock

from coconut import team_keys, team_store
from coconut.team import PERSON_ONLY
from coconut.team_directs import LEFT
from coconut.team_messages import GONE_MESSAGE
from coconut.team_reactions import CHOOSE, TEAM_ONLY, reaction_entry
from coconut.team_store import REACTIONS, REFUSED, STORED, TeamStore
from team_fakes import TeamMacs
from test_team_direct_turns import DirectTurnMacs
from test_team_directs import DirectMacs
from test_team_directs import TEAM_ONLY as DIRECTS_TEAM_ONLY
from test_team_push import TeamPushMacs, flush
from test_team_turns import TeamTurnMacs, reply

THUMBS, HEART, JOY, WOW, SAD, PRAY = REACTIONS
ROOM = 'a' * 64
ADMIN = '1' * 64
MEMBER = '2' * 64
OTHER = '3' * 64
START = 1_800_000_000_000


def entry_id(number: int) -> str:
    return f'{number:032x}'


class ReactionStoreTests(unittest.TestCase):
    """The reading rules of a `reaction` entry. MEMBER is this Mac, and ADMIN and OTHER are the other members."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.executescript(team_store.SCHEMA)
        self.store = TeamStore(self.db)
        self.store.migrate()
        self.store.add_room(room=ROOM, relay='https://relay.example', name='North team', member=MEMBER, admin=ADMIN,
                            state=team_store.ACTIVE, epoch=0, profile='{}', created_at='2026-11-10T12:00:00Z')
        self.seq = 0

    def write(self, number: int, payload: dict, member: str = ADMIN):
        self.seq += 1
        existing = self.store.entry(ROOM, entry_id(number))
        entry = dict(seq=self.seq, first=existing['first'] if existing else self.seq, id=entry_id(number),
                     member=member, at=START + self.seq, kept=False, body=b'')
        opened = team_store.read_entry({'epoch': 0, 'payload': json.dumps(payload).encode()})
        return self.store.accept(self.store.room(ROOM), entry, opened)

    @staticmethod
    def message(text: str = 'Hello', rev: int = 1) -> dict:
        return {'kind': 'message', 'rev': rev, 'at': '2026-11-10T12:00:00Z', 'text': text}

    @staticmethod
    def reaction(emoji: str, message: int = 1, rev: int = 1, **fields) -> dict:
        return {'kind': 'reaction', 'rev': rev, 'at': '2026-11-10T12:00:00Z', 'message': entry_id(message),
                'emoji': emoji, **fields}

    def reactions(self, message: int = 1) -> list[tuple[str, str]]:
        return self.store.reactions_of(ROOM, [entry_id(message)]).get(entry_id(message), [])

    def test_the_six_emoji_and_an_empty_one_fit_and_nothing_else(self):
        for emoji in (*REACTIONS, ''):
            self.assertEqual(team_store.reaction_fields({'message': entry_id(1), 'emoji': emoji}),
                             {'message': entry_id(1), 'emoji': emoji})
        # A heart without the variation selector, another emoji, text, and a message that is no entry ID do not fit.
        for fields in ({'message': entry_id(1), 'emoji': '❤'}, {'message': entry_id(1), 'emoji': '\U0001F389'},
                       {'message': entry_id(1), 'emoji': 'like'}, {'message': entry_id(1), 'emoji': None},
                       {'message': entry_id(1)}, {'message': 'x', 'emoji': THUMBS}, {'emoji': THUMBS}):
            with self.subTest(fields=fields):
                self.assertIsNone(team_store.reaction_fields(fields))

    def test_add_change_and_take_back(self):
        self.write(1, self.message())
        self.store.chatted.clear()
        self.assertEqual(self.write(2, self.reaction(THUMBS), OTHER), (STORED, None))
        self.assertEqual(self.reactions(), [(THUMBS, OTHER)])
        self.assertEqual(self.store.chatted, {ROOM})
        self.write(2, self.reaction(HEART, rev=2), OTHER)
        self.assertEqual(self.reactions(), [(HEART, OTHER)])
        self.store.chatted.clear()
        self.write(2, self.reaction('', rev=3), OTHER)
        self.assertEqual(self.reactions(), [])
        self.assertEqual(self.store.chatted, {ROOM})
        # A `delete` over the entry and a write that does not fit take a reaction back too.
        self.write(2, self.reaction(JOY, rev=4), OTHER)
        self.write(2, {'kind': 'delete', 'rev': 5}, OTHER)
        self.assertEqual(self.reactions(), [])
        self.write(2, self.reaction(JOY, rev=6), OTHER)
        self.write(2, self.reaction('\U0001F389', rev=7), OTHER)
        self.assertEqual(self.reactions(), [])

    def test_two_members_keep_the_order_of_the_log(self):
        self.write(1, self.message())
        self.write(2, self.reaction(HEART), OTHER)
        self.write(3, self.reaction(THUMBS), MEMBER)
        self.write(4, self.reaction(HEART), ADMIN)
        self.assertEqual(self.reactions(), [(HEART, OTHER), (THUMBS, MEMBER), (HEART, ADMIN)])
        self.assertEqual(team_store.reaction_views(self.reactions(), MEMBER), [
            {'emoji': THUMBS, 'members': [MEMBER], 'mine': True},
            {'emoji': HEART, 'members': [OTHER, ADMIN], 'mine': False}])

    def test_the_entry_of_another_member_with_the_same_id_is_refused(self):
        self.write(1, self.message())
        self.write(2, self.reaction(THUMBS), OTHER)
        self.assertEqual(self.write(2, self.reaction(SAD, rev=2), ADMIN), (REFUSED, 'member'))
        self.assertEqual(self.write(2, self.reaction(SAD, rev=1), OTHER), (REFUSED, 'rev'))
        self.assertEqual(self.reactions(), [(THUMBS, OTHER)])

    def test_the_newest_write_of_a_member_counts_also_across_entries(self):
        # A Mac derives one entry ID for a message, but a reader cannot check it. So two entries of one member for one
        # message give one reaction: the one of the newest write.
        self.write(1, self.message())
        self.write(2, self.reaction(THUMBS), OTHER)
        self.write(3, self.reaction(WOW), OTHER)
        self.assertEqual(self.reactions(), [(WOW, OTHER)])
        self.write(2, self.reaction(PRAY, rev=2), OTHER)
        self.assertEqual(self.reactions(), [(PRAY, OTHER)])
        self.write(3, self.reaction('', rev=2), OTHER)
        self.assertEqual(self.reactions(), [])
        # An entry that names another message later moves the reaction.
        self.write(9, self.message('Second'))
        self.write(4, self.reaction(SAD), OTHER)
        self.write(4, self.reaction(SAD, message=9, rev=2), OTHER)
        self.assertEqual((self.reactions(), self.reactions(9)), ([], [(SAD, OTHER)]))

    def test_an_older_write_that_comes_late_changes_nothing(self):
        # The end of a read shows a waiting `direct` entry after the entries that came later (TeamSync), so a write
        # with a lower `seq` can come after a newer one of the same member.
        self.write(1, self.message())
        self.write(3, self.reaction(WOW), OTHER)
        late = dict(id=entry_id(2), seq=1, member=OTHER)
        self.store._show_reaction(ROOM, late, dict(message=entry_id(1), emoji=THUMBS, peer=None, bot=None))
        self.store._show_reaction(ROOM, late, dict(message=entry_id(1), emoji='', peer=None, bot=None))
        self.assertEqual(self.reactions(), [(WOW, OTHER)])

    def test_a_reaction_before_its_message_shows_when_the_message_comes(self):
        self.write(2, self.reaction(THUMBS, message=1), OTHER)
        self.assertEqual(self.store.chatted, set(), 'A reaction on a message that the store does not have.')
        self.write(1, self.message())
        self.assertEqual(self.reactions(), [(THUMBS, OTHER)])

    def test_a_reaction_changes_no_unread_count_no_read_position_no_notification_and_no_trigger(self):
        self.store.answers = True
        self.store.clock = lambda: START / 1000
        self.write(9, {'kind': 'member', 'rev': 1, 'at': 'x', 'name': 'Grace', 'avatar': '', 'color': '',
                       'agreement': 'cHFyc3R1dnd4eXp7fH1-f4CBgoOEhYaHiImKi4yNjo8'}, MEMBER)
        self.write(1, self.message())
        self.store.fresh.clear()
        unread, position = self.store.unread(ROOM, MEMBER), self.store.read_position(ROOM)
        self.write(2, self.reaction(THUMBS), OTHER)
        self.assertEqual((self.store.unread(ROOM, MEMBER), self.store.read_position(ROOM)), (unread, position))
        self.assertEqual((self.store.fresh, self.store.triggered), ({}, set()))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM team_triggers').fetchone()[0], 0)
        self.assertEqual([row['text'] for row in self.store.messages(ROOM)], ['Hello'])

    def test_an_older_mac_stores_the_entry_and_shows_nothing(self):
        # A Mac of an older version has no reader of the kind (rule 5 of a reader).
        with mock.patch.dict(team_store.SHOWN):
            del team_store.SHOWN['reaction']
            self.write(1, self.message())
            self.assertEqual(self.write(2, self.reaction(THUMBS), OTHER), (STORED, None))
        self.assertEqual(self.store.entry(ROOM, entry_id(2))['kind'], 'reaction')
        self.assertEqual(self.reactions(), [])
        # Its reader of a direct message finds no text in the box of a reaction.
        box = json.dumps({'to': MEMBER, 'message': entry_id(1), 'emoji': THUMBS}).encode()
        self.assertIsNone(team_store.direct_fields(box, MEMBER))
        self.assertEqual(team_store.reaction_box_fields(box, MEMBER), {'message': entry_id(1), 'emoji': THUMBS,
                                                                       'bot': None})
        self.assertIsNone(team_store.reaction_box_fields(box, OTHER))

    def older_store(self) -> sqlite3.Connection:
        """A store of a version without reactions: it kept each `reaction` entry as an entry of an unknown kind."""
        db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(db.close)
        db.executescript(team_store.SCHEMA)
        old = TeamStore(db)
        old.add_room(room=ROOM, relay='https://relay.example', name='North team', member=MEMBER, admin=ADMIN,
                     state=team_store.ACTIVE, epoch=0, profile='{}', created_at='2026-11-10T12:00:00Z')
        rows = ((1, ADMIN, 'message', self.message()), (2, OTHER, 'reaction', self.reaction(THUMBS)),
                (3, OTHER, 'reaction', self.reaction(JOY)), (4, ADMIN, 'reaction', self.reaction('nope')),
                (5, MEMBER, 'reaction', self.reaction(HEART)))
        for seq, (number, member, kind, payload) in enumerate(rows, start=1):
            db.execute('INSERT INTO team_entries (room, id, seq, first, member, kind, rev, at, kept, payload) '
                       'VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?)',
                       (ROOM, entry_id(number), seq, seq, member, kind, START + seq, json.dumps(payload)))
        db.execute("INSERT INTO team_messages VALUES (?, ?, 1, ?, NULL, 'Hello', NULL, '[]', 't', 't', 0, NULL)",
                   (ROOM, entry_id(1), ADMIN))
        return db

    def test_an_older_store_gets_the_table_and_reads_its_reactions_once(self):
        db = self.older_store()
        store = TeamStore(db)
        # The table of an older store: SCHEMA has no team_reactions, so only migrate makes it.
        self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='team_reactions'").fetchone())
        store.migrate()
        self.assertEqual(store.reactions_of(ROOM, [entry_id(1)]), {entry_id(1): [(JOY, OTHER), (HEART, MEMBER)]})
        changes = db.total_changes
        TeamStore(db).migrate()
        self.assertEqual(db.total_changes, changes, 'A second start changes nothing.')

    def test_a_stop_in_the_pass_leaves_the_store_of_before(self):
        db = self.older_store()
        store = TeamStore(db)

        def stop(*arguments, **options):
            raise KeyboardInterrupt('The process stops here.')
        store._show_reaction = stop
        with self.assertRaises(KeyboardInterrupt):
            store.migrate()
        self.assertFalse(db.in_transaction)
        self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='team_reactions'").fetchone())
        store = TeamStore(db)
        store.migrate()
        self.assertEqual(len(store.reactions_of(ROOM, [entry_id(1)])[entry_id(1)]), 2)

    def test_each_statement_of_the_pass_and_of_a_reaction_uses_an_index(self):
        # Many reactions, so a statement without an index would scan. Each statement that names the table, in the pass
        # of an older store and in a new write, must find its rows by an index.
        db = self.older_store()
        for number in range(10, 210):
            db.execute('INSERT INTO team_entries (room, id, seq, first, member, kind, rev, at, kept, payload) '
                       "VALUES (?, ?, ?, ?, ?, 'reaction', 1, ?, 0, ?)",
                       (ROOM, entry_id(number), number, number, OTHER, START + number,
                        json.dumps(self.reaction(THUMBS, message=number))))
        statements, during = [], []
        store = TeamStore(db)
        replay = store._read_reactions_again

        def read_again():
            # The plans below are taken after the pass. So the indexes at the start of the pass must be the same.
            during.extend(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE tbl_name='team_reactions'"))
            db.set_trace_callback(statements.append)
            replay()
        store._read_reactions_again = read_again
        store.migrate()
        self.db, self.store, self.seq = db, store, 1000
        self.write(300, self.reaction(SAD), OTHER)
        self.write(300, {'kind': 'delete', 'rev': 2}, OTHER)
        db.set_trace_callback(None)
        checked = 0
        # An INSERT with ON CONFLICT finds its row by the primary key, and its plan is empty. Each read and each delete
        # must name its index in the plan.
        for statement in {text for text in statements if 'team_reactions' in text and 'sqlite_master' not in text
                          and text.lstrip().startswith(('SELECT', 'DELETE', 'UPDATE'))}:
            plan = ' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + statement))
            with self.subTest(statement=statement[:80]):
                self.assertNotRegex(plan, r'SCAN (r|x|team_reactions)\b', plan)
                self.assertRegex(plan, r'USING (INDEX|PRIMARY KEY)|sqlite_autoindex_team_reactions_1|'
                                 r'team_reactions_message', plan)
            checked += 1
        self.assertGreaterEqual(checked, 3, 'The trace has the statements with their values.')
        indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE tbl_name='team_reactions'")}
        self.assertEqual(set(during), indexes, 'The pass has each index of the table.')
        self.assertIn('team_reactions_message', indexes)


class ReactionOrderTests(unittest.TestCase):
    """Each Mac shows the same reactions for the same log, in whatever order it applies the entries: the log in order,
    only the newest write of each entry (a Mac that joins later or reads again), and an entry that waits for its key
    and comes at the end of the read (TeamStore.end_wait). Each reaction here is in a direct chat of OTHER with this
    Mac, MEMBER, so the wait path applies."""

    def store(self) -> TeamStore:
        db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(db.close)
        db.executescript(team_store.SCHEMA)
        store = TeamStore(db)
        store.migrate()
        store.add_room(room=ROOM, relay='https://relay.example', name='North team', member=MEMBER, admin=ADMIN,
                       state=team_store.ACTIVE, epoch=0, profile='{}', created_at='2026-11-10T12:00:00Z')
        return store

    @staticmethod
    def accept(store, entry: str, seq: int, rev: int, reaction: dict | None, wait: bool = False) -> None:
        opened = team_store.read_entry({'epoch': 0, 'payload': json.dumps(
            {'kind': 'direct', 'rev': rev, 'at': 'x', 'box': 'sealed'}).encode()})
        opened.update(direct=None, wait=wait)
        if reaction is not None and not wait:
            opened['reaction'] = dict(reaction, peer=OTHER, bot=None)
        existing = store.entry(ROOM, entry)
        row = dict(id=entry, seq=seq, first=existing['first'] if existing else seq, member=OTHER, at=START + seq,
                   kept=False, body=b'')
        store.accept(store.room(ROOM), row, opened)

    def view(self, store, messages) -> dict:
        return store.reactions_of(ROOM, messages, OTHER, None)

    def views(self, writes: list[tuple[str, int, int, dict]], late: str) -> dict[str, dict]:
        """`writes` are (entry, seq, rev, reaction) in the order of the log. `late` is the entry that waits."""
        messages = sorted({reaction['message'] for _, _, _, reaction in writes})
        newest = {}
        for write in writes:
            newest[write[0]] = write
        newest = sorted(newest.values(), key=lambda write: write[1])
        result = {}
        store = self.store()
        for entry, seq, rev, reaction in writes:
            self.accept(store, entry, seq, rev, reaction)
        result['in order'] = self.view(store, messages)
        store = self.store()
        for entry, seq, rev, reaction in newest:
            self.accept(store, entry, seq, rev, reaction)
        result['newest writes'] = self.view(store, messages)
        store = self.store()
        for entry, seq, rev, reaction in newest:
            self.accept(store, entry, seq, rev, reaction, wait=entry == late)
        (entry, seq, rev, reaction) = next(write for write in newest if write[0] == late)
        store.end_wait(ROOM, store.entry(ROOM, entry), {'reaction': dict(reaction, peer=OTHER, bot=None)})
        result['one entry late'] = self.view(store, messages)
        return result

    def assertSame(self, views: dict[str, dict], expected: dict) -> None:
        for order, view in views.items():
            with self.subTest(order=order):
                self.assertEqual(view, expected)

    def test_a_late_reaction_does_not_come_back_after_a_newer_empty_one(self):
        # The case of the review: A (seq 10) waits, and B (seq 20) of the same member takes the reaction back.
        m = entry_id(1)
        views = self.views([('a' * 32, 10, 1, {'message': m, 'emoji': THUMBS}),
                            ('b' * 32, 20, 1, {'message': m, 'emoji': ''})], late='a' * 32)
        self.assertSame(views, {})

    def test_three_entries_and_an_entry_that_moves_to_another_message(self):
        # F (seq 5, M1), E rev 1 (seq 10, M1), E rev 2 (seq 20, M2). The newest write of E names M2, so F alone says
        # the reaction of the member on M1.
        m1, m2 = entry_id(1), entry_id(2)
        views = self.views([('f' * 32, 5, 1, {'message': m1, 'emoji': THUMBS}),
                            ('e' * 32, 10, 1, {'message': m1, 'emoji': THUMBS}),
                            ('e' * 32, 20, 2, {'message': m2, 'emoji': THUMBS})], late='f' * 32)
        self.assertSame(views, {m1: [(THUMBS, OTHER)], m2: [(THUMBS, OTHER)]})

    def test_the_newest_entry_of_a_member_counts_in_each_order(self):
        m = entry_id(1)
        views = self.views([('a' * 32, 10, 1, {'message': m, 'emoji': THUMBS}),
                            ('b' * 32, 20, 1, {'message': m, 'emoji': HEART}),
                            ('a' * 32, 30, 2, {'message': m, 'emoji': SAD})], late='b' * 32)
        self.assertSame(views, {m: [(SAD, OTHER)]})


class ReactionMacs(TeamMacs):
    def setUp(self):
        super().setUp()
        self.room = self.create()['room']
        for service, name in ((self.second, 'Grace'), (self.third, 'Lin')):
            self.join(service, self.invite({'room': self.room})['link'], name)
        self.macs = (self.first, self.second, self.third)
        self.sync()
        self.ada, self.grace, self.lin = (service.team_store.room(self.room)['member'] for service in self.macs)

    def sync(self, *services):
        for _ in range(2):
            for service in services or self.macs:
                service.team_sync_pass()

    def send(self, service, text: str, **fields) -> dict:
        return self.phone(service)('POST', f'/v1/team/rooms/{self.room}/messages', dict(fields, text=text))['message']

    def react(self, service, message: str, emoji: str, key: str = '') -> dict:
        return self.phone(service)('PUT', f'/v1/team/rooms/{self.room}/messages/{message}/reaction',
                                   {'emoji': emoji}, key)

    def seen(self, service, message: str, chat: str | None = None) -> list[dict]:
        """The `reactions` of a message in the page of a Mac."""
        page = self.phone(service)('GET', f'/v1/team/rooms/{self.room}/messages', {'chat': chat} if chat else {})
        return next(row['reactions'] for row in page['messages'] if row['id'] == message)

    def writes(self) -> list[str]:
        """Record the entry ID of each write to the relay from now on."""
        writes = []
        write = self.relay.write
        self.relay.write = lambda *arguments, **options: (writes.append(arguments[3]), write(*arguments, **options))[1]
        return writes


class ReactionRouteTests(ReactionMacs, unittest.TestCase):
    def test_add_change_remove_and_repeat(self):
        message = self.send(self.first, 'Hello team')
        self.sync()
        writes = self.writes()
        answer = self.react(self.second, message['id'], THUMBS)
        self.assertEqual(answer['room'], self.room)
        self.assertEqual(answer['message']['id'], message['id'])
        self.assertEqual(answer['message']['reactions'], [{'emoji': THUMBS, 'members': [self.grace], 'mine': True}])
        self.sync()
        self.assertEqual(self.seen(self.first, message['id']),
                         [{'emoji': THUMBS, 'members': [self.grace], 'mine': False}])
        # The same emoji again writes nothing: the route sets a value.
        self.react(self.second, message['id'], THUMBS)
        self.react(self.second, message['id'], HEART)
        self.sync()
        self.assertEqual(self.seen(self.third, message['id']),
                         [{'emoji': HEART, 'members': [self.grace], 'mine': False}])
        self.assertEqual(self.react(self.second, message['id'], '')['message']['reactions'], [])
        self.react(self.second, message['id'], '')
        self.sync()
        self.assertEqual(self.seen(self.first, message['id']), [])
        # Three writes of one entry, whose ID comes from the token of Grace's Mac, the room and the message.
        token = team_keys.read(self.second.root, self.room)['token']
        self.assertEqual(writes, [reaction_entry(token, self.room, message['id'])] * 3)
        entry = self.second.team_store.entry(self.room, writes[0])
        self.assertEqual((entry['kind'], entry['rev'], entry['kept']), ('reaction', 3, 0))
        self.assertEqual(json.loads(entry['payload'])['emoji'], '')
        # A message of the person's own and a repeat with an `Idempotency-Key` work too.
        own = self.send(self.second, 'Mine')
        key = '6c2c8f2c-3f3c-4bf5-9f43-2b3f2a6e0c11'
        self.assertEqual(self.react(self.second, own['id'], PRAY, key), self.react(self.second, own['id'], PRAY, key))

    def test_two_members_on_one_message(self):
        message = self.send(self.third, 'Lunch at noon?')
        self.sync()
        self.react(self.second, message['id'], HEART)
        self.react(self.first, message['id'], THUMBS)
        self.sync()
        self.react(self.third, message['id'], HEART)
        self.sync()
        for service, own in ((self.first, self.ada), (self.second, self.grace), (self.third, self.lin)):
            with self.subTest(member=own[:4]):
                self.assertEqual(self.seen(service, message['id']), [
                    {'emoji': THUMBS, 'members': [self.ada], 'mine': own == self.ada},
                    {'emoji': HEART, 'members': [self.grace, self.lin], 'mine': own in (self.grace, self.lin)}])

    def test_each_refusal(self):
        message = self.send(self.first, 'Hello team')
        self.sync()
        path = f'/v1/team/rooms/{self.room}/messages/{message["id"]}/reaction'
        for emoji in ('\U0001F389', '❤', 'like', None, 1):
            with self.subTest(emoji=emoji):
                self.assertEqual(self.refusal(self.phone(self.second), 'PUT', path, {'emoji': emoji}), (400, CHOOSE))
        self.assertEqual(self.refusal(self.phone(self.second), 'PUT', path, {}), (400, CHOOSE))
        for entry in ('f' * 32, 'not-an-entry'):
            with self.subTest(entry=entry):
                self.assertEqual(self.refusal(self.react, self.second, entry, THUMBS), (404, GONE_MESSAGE))
        # A tool of a person's Mac cannot react: only the person can.
        self.assertEqual(self.refusal(self.second.team_request, 'PUT', path, {'emoji': THUMBS}), (403, PERSON_ONLY))
        self.assertEqual(self.refusal(self.phone(self.second), 'PUT', f'/v1/team/rooms/{"d" * 64}/messages/'
                                      f'{message["id"]}/reaction', {'emoji': THUMBS}),
                         (404, 'This Mac is not in this team.'))
        with self.second.transaction():
            self.second.team_store.update_room(self.room, state=team_store.REMOVED)
        self.assertEqual(self.refusal(self.react, self.second, message['id'], THUMBS),
                         (409, 'This Mac is no longer in the team.'))
        self.assertEqual(self.second.db.execute("SELECT COUNT(*) FROM team_entries WHERE kind='reaction'").fetchone(),
                         (0,))

    def test_a_deleted_or_erased_message_takes_no_reaction_and_shows_none(self):
        deleted = self.send(self.second, 'Wrong words')
        erased = self.send(self.second, 'Harmful words')
        kept = self.send(self.second, 'Kept words')
        self.sync()
        for message in (deleted, erased, kept):
            self.react(self.third, message['id'], SAD)
        self.sync()
        self.phone(self.second)('DELETE', f'/v1/team/rooms/{self.room}/messages/{deleted["id"]}', {})
        # Ada is the admin: her delete of a message of Grace is an `erase` entry.
        self.phone(self.first)('DELETE', f'/v1/team/rooms/{self.room}/messages/{erased["id"]}', {})
        self.sync()
        for service in self.macs:
            page = self.phone(service)('GET', f'/v1/team/rooms/{self.room}/messages', {})['messages']
            self.assertEqual([(row['text'], len(row['reactions'])) for row in page], [('Kept words', 1)])
            for message in (deleted, erased):
                self.assertEqual(self.refusal(self.react, service, message['id'], THUMBS), (404, GONE_MESSAGE))

    def test_a_named_chat_and_the_event_of_a_message_change(self):
        chat = self.phone(self.first)('POST', f'/v1/team/rooms/{self.room}/chats', {'name': 'Design'})['chat']
        self.sync()
        message = self.send(self.second, 'A new logo', chat=chat)
        self.sync()
        cursor = self.first._cursor()
        self.react(self.third, message['id'], WOW)
        self.sync()
        self.assertEqual(self.seen(self.first, message['id'], chat), [{'emoji': WOW, 'members': [self.lin],
                                                                       'mine': False}])
        updates = self.first.updates(cursor, 0, collection_deltas=True)
        self.assertEqual(updates.get('team_messages_changed'), [self.room])
        self.assertFalse(updates.get('team_changed'))
        # A pass that reads nothing new adds no event.
        cursor = self.first._cursor()
        self.sync()
        self.assertEqual(self.first._cursor(), cursor)

    def test_a_member_that_left_keeps_its_reaction(self):
        # A message of a member that left stays in the history, and so does a reaction.
        message = self.send(self.first, 'Hello team')
        self.sync()
        self.react(self.third, message['id'], PRAY)
        self.sync()
        self.leave(self.third, {'room': self.room})
        self.sync(self.first, self.second)
        self.assertEqual(self.seen(self.second, message['id']), [{'emoji': PRAY, 'members': [self.lin],
                                                                  'mine': False}])


class ReactionPushTests(TeamPushMacs, unittest.TestCase):
    """Grace (`first`) writes. Ada (`second`) has the paired phone."""

    def test_a_reaction_notifies_nothing_and_changes_no_unread_count_or_read_position(self):
        grace_message = self.send('Hello Ada')
        own = self.send('Mine', service=self.second)
        self.first.team_sync_pass()
        self.second.team_sync_pass()
        flush(self.second)
        items = self.items()
        (room,) = self.rooms(self.second)
        unread, position = room['unread'], self.second.team_store.read_position(self.room)
        self.assertEqual(unread, 1)
        for message in (grace_message, own):
            self.phone(self.first)('PUT', f'/v1/team/rooms/{self.room}/messages/{message["id"]}/reaction',
                                   {'emoji': HEART})
        self.second.team_sync_pass()
        flush(self.second)
        (room,) = self.rooms(self.second)
        self.assertEqual((room['unread'], self.second.team_store.read_position(self.room)), (unread, position))
        self.assertEqual(self.items(), items)
        self.assertEqual(room['last_message']['text'], 'Mine')
        page = self.phone(self.second)('GET', f'/v1/team/rooms/{self.room}/messages', {})['messages']
        self.assertEqual([len(row['reactions']) for row in page], [1, 1])


class ReactionTurnTests(TeamTurnMacs, unittest.TestCase):
    def test_a_reaction_starts_no_turn_and_the_team_only_mac_cannot_react(self):
        named = self.send('@Reporter what is open?', mentions=[self.mention])
        self.sync()
        self.serve(reply('3 tasks are open.'))
        self.run_turns()
        self.assertEqual(self.triggers(), [])
        answer = self.call('GET', f'/v1/team/rooms/{self.room}/messages', service=self.first)['messages'][0]
        for message in (answer['id'], named['id']):
            self.call('PUT', f'/v1/team/rooms/{self.room}/messages/{message}/reaction', {'emoji': THUMBS},
                      service=self.first)
        self.sync()
        self.assertEqual(self.triggers(), [])
        with self.only.lock:
            self.assertEqual(self.only.team_store.waiting(), [])
            self.assertEqual(len(self.only.team_store.reactions_of(self.room, [answer['id'], named['id']])), 2)
        self.assertEqual(self.refusal(self.call, 'PUT', f'/v1/team/rooms/{self.room}/messages/{answer["id"]}/reaction',
                                      {'emoji': THUMBS}), (409, TEAM_ONLY))


class DirectReactionTests(DirectMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.turn_on(*self.macs)

    def react(self, service, member: str, message: str, emoji: str) -> dict:
        return self.phone(service)('PUT', self.path(member, f'messages/{message}/reaction'), {'emoji': emoji})

    def seen(self, service, member: str) -> list[list[dict]]:
        return [row['reactions'] for row in self.phone(service)('GET', self.path(member), {})['messages']]

    def test_only_the_two_people_of_the_chat_see_it(self):
        sent = self.send(self.first, self.grace, 'Hello Grace')
        self.sync()
        cursor = self.first._cursor()
        answer = self.react(self.second, self.ada, sent['id'], JOY)
        self.assertEqual((answer['room'], answer['member'], answer['message']['reactions']),
                         (self.room, self.ada, [{'emoji': JOY, 'members': [self.grace], 'mine': True}]))
        self.sync()
        self.assertEqual(self.seen(self.first, self.grace), [[{'emoji': JOY, 'members': [self.grace], 'mine': False}]])
        updates = self.first.updates(cursor, 0, collection_deltas=True)
        self.assertEqual(updates.get('team_directs_changed'), [{'team': self.room, 'member': self.grace}])
        self.assertFalse(updates.get('team_messages_changed'))
        # The entry is a `direct` entry: Lin stores it and shows nothing.
        token = team_keys.read(self.second.root, self.room)['token']
        entry = reaction_entry(token, self.room, sent['id'], direct=True)
        self.assertNotEqual(entry, reaction_entry(token, self.room, sent['id']))
        for service in self.macs:
            self.assertEqual(service.team_store.entry(self.room, entry)['kind'], 'direct')
        self.assertEqual(self.count(self.third, 'team_reactions'), 0)
        self.assertEqual(self.count(self.first, 'team_reactions'), 1)
        # A repeat writes nothing, and an empty emoji takes the reaction back.
        self.react(self.second, self.ada, sent['id'], JOY)
        self.react(self.second, self.ada, sent['id'], '')
        self.sync()
        self.assertEqual(self.seen(self.first, self.grace), [[]])
        self.assertEqual(self.second.team_store.entry(self.room, entry)['rev'], 2)
        # No unread count of the chat changed: the message of Ada is Ada's own, and Grace's count stays at 1.
        (chat,) = self.ask(self.second)['directs']
        self.assertEqual(chat['unread'], 1)

    def test_a_reaction_shows_only_in_the_chat_that_it_names(self):
        to_grace = self.send(self.first, self.grace, 'Hello Grace')
        self.sync()
        # Lin writes a box to Grace that names the message of the chat of Ada and Grace. Grace's Mac shows nothing.
        room = self.third.team_store.room(self.room)
        secrets = team_keys.read(self.third.root, self.room)
        agreement = self.third.team_store.member(self.room, self.grace)['agreement']
        box = self.third.team_helper.seal_direct(self.room, 'e' * 32, secrets['agreement_key'], room['member'],
                                                 self.grace, agreement, json.dumps(
                                                     {'to': self.grace, 'message': to_grace['id'], 'emoji': SAD}).encode())
        with self.third._team_room_lock(self.room):
            self.third._team_write(room, 'e' * 32, False, {'kind': 'direct', 'box': box})
        self.sync()
        self.assertEqual(self.seen(self.second, self.ada), [[]])
        self.assertEqual(self.count(self.second, 'team_reactions'), 1, 'The row of Lin names the chat with Lin.')
        # A team `reaction` entry that names a direct message shows nowhere.
        with self.third._team_room_lock(self.room):
            self.third._team_write(room, 'f' * 32, False, {'kind': 'reaction', 'message': to_grace['id'], 'emoji': SAD})
        self.sync()
        self.assertEqual((self.seen(self.second, self.ada), self.seen(self.first, self.grace)), ([[]], [[]]))
        # A message of another chat answers 404.
        self.assertEqual(self.refusal(self.react, self.second, self.lin, to_grace['id'], THUMBS), (404, GONE_MESSAGE))
        self.assertEqual(self.second.team_store.own_reaction(self.room, to_grace['id'], self.grace), '',
                         'The refusal writes nothing.')

    def test_a_member_that_left_cannot_get_a_new_reaction(self):
        sent = self.send(self.first, self.grace, 'Hello Grace')
        self.sync()
        self.react(self.first, self.grace, sent['id'], THUMBS)
        self.leave(self.second, {'room': self.room})
        self.sync(self.first)
        self.assertEqual(self.refusal(self.react, self.first, self.grace, sent['id'], HEART), (409, LEFT))
        # The same emoji writes nothing, so it needs no box.
        self.assertEqual(self.react(self.first, self.grace, sent['id'], THUMBS)['message']['reactions'],
                         [{'emoji': THUMBS, 'members': [self.ada], 'mine': True}])

    def rewind(self, service):
        with service.transaction():
            for table in ('team_entries', 'team_members', 'team_directs', 'team_reactions'):
                service.db.execute(f'DELETE FROM {table} WHERE room=?', (self.room,))
            service.team_store.update_room(self.room, seq=0)

    def test_an_entry_before_the_member_entry_of_its_key_and_an_own_entry_after_a_lost_store(self):
        sent = self.send(self.first, self.grace, 'Hello Grace')
        self.sync()
        self.react(self.second, self.ada, sent['id'], WOW)
        self.react(self.first, self.grace, sent['id'], THUMBS)
        # Grace writes her `member` entry again, at a new `seq` behind her reaction.
        self.phone(self.second)('PATCH', f'/v1/team/rooms/{self.room}', {'member_name': 'Grace H.'})
        expected = [[{'emoji': THUMBS, 'members': [self.ada], 'mine': True},
                     {'emoji': WOW, 'members': [self.grace], 'mine': False}]]
        self.rewind(self.first)
        with self.first._team_room_lock(self.room):
            while self.first._team_read(self.room):
                pass
        # The message, the reaction of Ada and the reaction of Grace wait for the `member` entries of their keys.
        self.assertEqual(self.count(self.first, 'team_direct_waits'), 3)
        self.assertEqual(self.seen(self.first, self.grace), [])
        self.first.team_sync_pass()
        self.assertEqual(self.seen(self.first, self.grace), expected)
        self.assertEqual(self.count(self.first, 'team_direct_waits'), 0)


class BotDirectReactionTests(DirectMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.turn_on(*self.macs)

    def test_a_person_reacts_in_a_chat_with_a_bot(self):
        from secrets import token_hex
        self.third._team_publish = lambda room_id: None
        bot = token_hex(16)
        room = self.third.team_store.room(self.room)
        with self.third._team_room_lock(self.room):
            self.third._team_write(room, bot, True, {'kind': 'bot', 'name': 'Reporter', 'avatar': '', 'color': '',
                                                     'direct': True})
        self.sync()
        bot_id = f't-{self.room}-{bot}'
        sent = self.send(self.first, bot_id, 'What is open?')
        cursor = self.first._cursor()
        answer = self.phone(self.first)('PUT', self.path(bot_id, f'messages/{sent["id"]}/reaction'), {'emoji': PRAY})
        updates = self.first.updates(cursor, 0, collection_deltas=True)
        self.assertEqual(updates.get('team_bot_directs_changed'), [{'team': self.room, 'bot': bot_id}])
        self.assertFalse(updates.get('team_directs_changed'))
        self.assertEqual((answer['bot'], answer['message']['reactions']),
                         (bot_id, [{'emoji': PRAY, 'members': [self.ada], 'mine': True}]))
        self.sync()
        # The chat of two people of Ada and Lin does not show it.
        self.assertEqual(self.chat(self.first, self.lin), [])
        page = self.phone(self.first)('GET', self.path(bot_id), {})['messages']
        self.assertEqual(page[0]['reactions'], [{'emoji': PRAY, 'members': [self.ada], 'mine': True}])


class TheTwoReadersOfABoxWithoutText(unittest.TestCase):
    """A box without `text` is a reaction or the typing sign of a bot. Neither reader takes the box of the other."""

    def test_each_reader_takes_only_its_own_box(self):
        to, message, bot = MEMBER, entry_id(1), entry_id(2)

        def box(**fields) -> bytes:
            return json.dumps(dict(fields, to=to)).encode()
        reaction, sign = box(message=message, emoji=THUMBS, bot=bot), box(bot=bot, on=True)
        self.assertEqual(team_store.reaction_box_fields(reaction, to), {'message': message, 'emoji': THUMBS, 'bot': bot})
        self.assertIsNone(team_store.direct_typing_fields(reaction, to))
        self.assertEqual(team_store.direct_typing_fields(sign, to), {'bot': bot, 'on': True})
        self.assertIsNone(team_store.reaction_box_fields(sign, to))
        # A box with the fields of both is neither.
        both = box(message=message, emoji=THUMBS, bot=bot, on=False)
        self.assertEqual((team_store.reaction_box_fields(both, to), team_store.direct_typing_fields(both, to)),
                         (None, None))


class TeamOnlyMacAndAReactionBox(DirectTurnMacs, unittest.TestCase):
    """Ada reacts in her direct chat with the bot Reporter of the Mac for a team only (`only`)."""

    def own_writes(self) -> list[str]:
        """The entries of the Mac for a team only on the relay: its answers and its typing signs would be here."""
        return sorted(entry for entry, row in self.relay.rooms[self.room]['entries'].items()
                      if row['member'] == self.mac)

    def reaction_box(self, entry: str, message: str, emoji: str = THUMBS) -> None:
        """A reaction box of Ada's Mac for the Mac of the bot, written by hand, as a Mac of another version could."""
        self.box(self.first, entry, self.mac, {'to': self.mac, 'message': message, 'emoji': emoji, 'bot': self.entry})

    def test_a_reaction_starts_no_turn_and_the_mac_keeps_nothing_of_it(self):
        turns = []
        self.serve(lambda turn: (turns.append(turn), reply('Noted.')(turn))[1])
        # The first entry of Ada to the bot is a reaction, on a message ID that the Mac does not have.
        writes = self.own_writes()
        self.reaction_box('e' * 32, 'f' * 32)
        self.only.team_sync_pass()
        self.assertEqual(self.triggers(), [], 'No turn.')
        self.assertEqual((self.stored(), self.count_reactions()), ([], 0), 'No message and no reaction.')
        # A message, its turn, and then reactions on the message and on the answer of the bot: by the route, and as a
        # write over the entry of the reaction.
        sent = self.dm('What is open?')
        self.run_turns()
        self.assertEqual(len(turns), 1)
        self.first.team_sync_pass()
        answer = self.phone(self.first)('GET', self.path(), {})['messages'][0]
        writes = self.own_writes()
        for message, emoji in ((sent['id'], THUMBS), (answer['id'], HEART), (answer['id'], ''), (answer['id'], JOY)):
            self.phone(self.first)('PUT', self.path(f'messages/{message}/reaction'), {'emoji': emoji})
            self.only.team_sync_pass()
        self.reaction_box('e' * 32, answer['id'], SAD)
        self.only.team_sync_pass()
        self.assertEqual(self.triggers(), [])
        self.assertEqual(self.only.team_store.waiting(), [])
        self.run_turns()
        self.assertEqual(len(turns), 1, 'No turn.')
        self.assertEqual(self.own_writes(), writes, 'No answer and no typing sign.')
        # The Mac of the bot keeps only its chat: the message and the answer, and no reaction.
        self.assertEqual([text for _, _, _, text in self.stored()], ['What is open?', 'Noted.'])
        self.assertEqual(self.count_reactions(), 0)
        # Ada's Mac shows her own reactions.
        page = self.phone(self.first)('GET', self.path(), {})['messages']
        self.assertEqual([row['reactions'] for row in page], [
            [{'emoji': JOY, 'members': [self.ada], 'mine': True}],
            [{'emoji': THUMBS, 'members': [self.ada], 'mine': True}]])
        # The next turn has no emoji in its text or its context.
        self.dm('And now?')
        self.run_turns()
        self.assertEqual(len(turns), 2)
        text = json.dumps(turns[1], ensure_ascii=False)
        for emoji in REACTIONS:
            self.assertNotIn(emoji, text)

    def count_reactions(self) -> int:
        with self.only.lock:
            return self.only.db.execute('SELECT COUNT(*) FROM team_reactions').fetchone()[0]

    def test_the_mac_for_a_team_only_cannot_react(self):
        sent = self.dm('Hello')
        self.assertEqual(self.refusal(self.call, 'PUT', f'/v1/team/rooms/{self.room}/directs/{self.ada}/messages/'
                                      f'{sent["id"]}/reaction', {'emoji': THUMBS}), (409, DIRECTS_TEAM_ONLY))
        self.send('Hello team')
        self.sync()
        message = self.call('GET', f'/v1/team/rooms/{self.room}/messages')['messages'][0]
        self.assertEqual(self.refusal(self.call, 'PUT', f'/v1/team/rooms/{self.room}/messages/{message["id"]}/reaction',
                                      {'emoji': THUMBS}), (409, TEAM_ONLY))


if __name__ == '__main__':
    unittest.main()
