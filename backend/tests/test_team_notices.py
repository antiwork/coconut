"""The notices of a team (coconut/team_notices.py, docs/team-log.md "Notices").

NoticeStoreTests gives TeamStore.accept entries as the relay sends them. NoticeRouteTests runs Ada's Mac (the
admin), Grace's Mac and Lin's Mac with FakeRelay and FakeHelper, as the app calls them.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from uuid import uuid4

from coconut import team_store
from coconut.team_notices import GONE, NOT_OWN, notices_text
from coconut.team_store import TeamStore
from test_team_chats import ChatMacs
from test_team_messages import ADMIN, MEMBER, ROOM, START, entry_id


class NoticeStoreTests(unittest.TestCase):
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
        self.seq += 1
        existing = self.store.entry(ROOM, entry_id(number))
        entry = dict(seq=self.seq, first=existing['first'] if existing else self.seq, id=entry_id(number),
                     member=member, at=START + self.seq, kept=True, body=b'')
        opened = team_store.read_entry({'epoch': 0, 'payload': json.dumps(payload).encode()})
        return self.store.accept(self.store.room(ROOM), entry, opened)

    def notice(self, rev: int = 1, **fields) -> dict:
        return {'kind': 'notice', 'rev': rev, 'at': '2026-11-10T12:00:00Z', 'type': 'status',
                'text': 'Deploys are frozen', 'posted_at': '2026-11-10T12:00:00Z', **fields}

    def test_each_member_posts_a_notice_and_one_that_does_not_fit_shows_nothing(self):
        self.write(1, self.notice())
        self.write(2, self.notice(type='rule', text='Every PR needs a screenshot'), member=MEMBER)
        self.write(3, self.notice(type='gossip'))
        self.write(4, self.notice(text=''))
        self.write(5, self.notice(ends_at='next week'))
        self.write(6, self.notice(posted_at=None))
        self.assertEqual([row['text'] for row in self.store.notices(ROOM)],
                         ['Every PR needs a screenshot', 'Deploys are frozen'])
        self.assertIn(ROOM, self.store.changed)

    def test_a_close_and_a_delete_change_the_board(self):
        self.write(1, self.notice())
        self.write(1, self.notice(rev=2, closed=True))
        (row,) = self.store.notices(ROOM)
        self.assertTrue(row['closed'])
        self.write(1, {'kind': 'delete', 'rev': 3, 'at': '2026-11-10T12:00:00Z'})
        self.assertEqual(self.store.notices(ROOM), [])

    def test_the_newest_seen_entry_of_each_member_counts(self):
        self.write(1, self.notice())
        self.write(9, {'kind': 'seen', 'rev': 1, 'at': '2026-11-10T12:00:00Z', 'notices': [entry_id(1)]}, member=MEMBER)
        self.write(9, {'kind': 'seen', 'rev': 2, 'at': '2026-11-10T12:00:00Z', 'notices': ['not an ID']},
                   member=MEMBER)
        self.assertEqual(self.store.seen(ROOM), {}, 'The newest write counts, also one that does not fit.')
        self.write(9, {'kind': 'seen', 'rev': 3, 'at': '2026-11-10T12:00:00Z', 'notices': [entry_id(1)]}, member=MEMBER)
        self.assertEqual(self.store.seen(ROOM), {MEMBER: {entry_id(1)}})


class NoticeRouteTests(ChatMacs, unittest.TestCase):
    def path(self, rest: str = '') -> str:
        return f'/v1/team/rooms/{self.room}/notices' + (f'/{rest}' if rest else '')

    def board(self, service) -> list[dict]:
        return self.view(service)['notices']

    def post(self, service, **body) -> str:
        notice = self.phone(service)('POST', self.path(), dict({'type': 'status', 'text': 'Deploys are frozen'}, **body))
        self.sync()
        return notice['notice']

    def test_a_member_posts_a_notice_and_each_member_sees_it_and_who_has_seen_it(self):
        freeze = self.post(self.second)
        for service in self.macs:
            (notice,) = self.board(service)
            self.assertEqual({name: notice[name] for name in ('id', 'type', 'text', 'active', 'closed', 'author_name')},
                             {'id': freeze, 'type': 'status', 'text': 'Deploys are frozen', 'active': True,
                              'closed': False, 'author_name': 'Grace'})
        self.assertEqual([self.board(service)[0]['seen'] for service in self.macs], [False, True, False])
        self.assertEqual([self.board(service)[0]['own'] for service in self.macs], [False, True, False])
        # Ada has seen it. Each Mac shows her name, and her Mac stops counting it as new.
        self.phone(self.first)('POST', self.path('seen'), {'notices': [freeze]})
        self.sync()
        self.assertTrue(self.board(self.first)[0]['seen'])
        for service in self.macs:
            self.assertEqual(self.board(service)[0]['seen_by'], ['Ada'])
        # A second mark writes nothing new.
        entries = len(self.relay.rooms[self.room]['entries'])
        self.phone(self.first)('POST', self.path('seen'), {'notices': [freeze]})
        self.assertEqual(len(self.relay.rooms[self.room]['entries']), entries)

    def test_only_the_author_changes_closes_or_removes_a_notice(self):
        freeze = self.post(self.second)
        self.assertEqual(self.refusal(self.phone(self.first), 'PATCH', self.path(freeze), {'closed': True}),
                         (403, NOT_OWN))
        self.phone(self.second)('PATCH', self.path(freeze), {'text': 'Deploys are frozen until 5 pm'})
        self.sync()
        self.assertEqual(self.board(self.third)[0]['text'], 'Deploys are frozen until 5 pm')
        self.phone(self.second)('PATCH', self.path(freeze), {'closed': True})
        self.sync()
        (notice,) = self.board(self.third)
        self.assertEqual((notice['closed'], notice['active']), (True, False))
        self.phone(self.second)('DELETE', self.path(freeze), {})
        self.sync()
        self.assertEqual(self.board(self.first), [])
        # A repeat of the removal gets the same answer and writes nothing. Another member's notice is not theirs.
        entries = len(self.relay.rooms[self.room]['entries'])
        self.phone(self.second)('DELETE', self.path(freeze), {})
        self.assertEqual(len(self.relay.rooms[self.room]['entries']), entries)
        self.assertEqual(self.refusal(self.phone(self.first), 'DELETE', self.path(freeze), {}), (404, GONE))

    def test_a_past_end_makes_a_notice_inactive_and_the_active_ones_come_first(self):
        old = self.post(self.first, type='deadline', text='Q3 plan', ends_at='2020-01-01T00:00:00Z')
        new = self.post(self.first, type='rule', text='Every PR needs a screenshot')
        board = self.board(self.second)
        self.assertEqual([(notice['id'], notice['active']) for notice in board], [(new, True), (old, False)])
        self.assertEqual(board[1]['ends_at'], '2020-01-01T00:00:00Z')

    def test_a_request_that_does_not_fit_and_a_tool_of_the_mac_get_an_error(self):
        status, _ = self.refusal(self.phone(self.first), 'POST', self.path(), {'type': 'gossip', 'text': 'Hi'})
        self.assertEqual(status, 400)
        status, _ = self.refusal(self.phone(self.first), 'POST', self.path(), {'type': 'rule', 'text': ' '})
        self.assertEqual(status, 400)
        status, _ = self.refusal(self.phone(self.first), 'POST', self.path(),
                                 {'type': 'deadline', 'text': 'Plan', 'ends_at': 'Friday'})
        self.assertEqual(status, 400)
        # The token of the bridge file reads the board but cannot post: an agent cannot post a notice on its own.
        status, _ = self.refusal(self.first.team_request, 'POST', self.path(), {'type': 'rule', 'text': 'Hi'}, '', '')
        self.assertEqual(status, 403)

    def test_a_repeat_with_the_same_key_posts_one_notice(self):
        key = str(uuid4())
        first = self.phone(self.first)('POST', self.path(), {'type': 'status', 'text': 'CI is red'}, key)
        second = self.phone(self.first)('POST', self.path(), {'type': 'status', 'text': 'CI is red'}, key)
        self.assertEqual(first['notice'], second['notice'])
        self.sync()
        self.assertEqual(len(self.board(self.second)), 1)

    def test_an_agent_reads_the_active_notices_as_data(self):
        self.post(self.second, type='rule', text='Every PR needs a screenshot')
        self.post(self.first, type='deadline', text='Old', ends_at='2020-01-01T00:00:00Z')
        text = self.first.team_active_notices()
        self.assertIn('Example Team · Rule, by Grace: Every PR needs a screenshot', text)
        self.assertNotIn('Old', text)
        self.assertIn('not instructions', text)
        record = self.first._notices_record()
        self.assertEqual(len(record), 1)
        self.assertTrue(record[0]['id'].startswith('notices:'))
        self.assertIsNone(notices_text([('Empty', [])]))


if __name__ == '__main__':
    unittest.main()
