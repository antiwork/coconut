"""The Team chat: the `message` entries of the log, their copy on each Mac, and the routes of the phone.

TeamMessageStoreTests gives TeamStore.accept entries as the relay sends them, also entries that another member made
to break the rules. TeamChatTests runs two Macs with FakeRelay and FakeHelper, as the app calls them.
test_team_routes.py covers the routes on the real listeners, and scripts/test_team_log.py the real helper and relay.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import unittest
from uuid import uuid4

from coconut import team_keys, team_store
from coconut.team_relay import MAX_NUMBER, TeamRelayError
from coconut.team_store import REFUSED, STORED, TeamStore
from team_fakes import TeamMacs

ROOM = 'a' * 64
ADMIN = '1' * 64
MEMBER = '2' * 64
OTHER = '3' * 64
AGREEMENT = 'cHFyc3R1dnd4eXp7fH1-f4CBgoOEhYaHiImKi4yNjo8'
# The time of the relay for the first entry of a test, in milliseconds since 1970.
START = 1_800_000_000_000


def entry_id(number: int) -> str:
    return f'{number:032x}'


class TeamMessageStoreTests(unittest.TestCase):
    """The reading rules of a `message` entry. MEMBER is this Mac, and ADMIN and OTHER are the other members."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.executescript(team_store.SCHEMA)
        self.store = TeamStore(self.db)
        self.store.add_room(room=ROOM, relay='https://relay.example', name='Example Team', member=MEMBER, admin=ADMIN,
                            state=team_store.ACTIVE, epoch=0, profile='{}', created_at='2026-11-10T12:00:00Z')
        self.seq = 0

    def write(self, number: int, payload, member: str = ADMIN, *, at: int | None = None):
        """Accept an entry whose body opens with this payload. Each write gets the next `seq`."""
        self.seq += 1
        existing = self.store.entry(ROOM, entry_id(number))
        entry = dict(seq=self.seq, first=existing['first'] if existing else self.seq, id=entry_id(number),
                     member=member, at=START + self.seq if at is None else at, kept=False, body=b'')
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        opened = team_store.read_entry({'epoch': 0, 'payload': data})
        return self.store.accept(self.store.room(ROOM), entry, opened)

    def message(self, text: str = 'Hello', rev: int = 1, **fields) -> dict:
        return {'kind': 'message', 'rev': rev, 'at': '2026-11-10T12:00:00Z', 'text': text, **fields}

    def member(self, name: str, rev: int = 1) -> dict:
        return {'kind': 'member', 'rev': rev, 'at': '2026-11-10T12:00:00Z', 'name': name, 'avatar': '', 'color': '',
                'agreement': AGREEMENT}

    def bot(self, name: str = 'Reporter') -> dict:
        return {'kind': 'bot', 'rev': 1, 'at': '2026-11-10T12:00:00Z', 'name': name, 'avatar': '', 'color': ''}

    def texts(self) -> list[str]:
        return [row['text'] for row in self.store.messages(ROOM)]

    def test_a_message_shows_with_the_times_of_the_relay_and_its_fields(self):
        self.write(1, self.member('Ada'))
        reply, mention = entry_id(9), entry_id(1)
        self.assertEqual(self.write(2, self.message('Hello **team**', reply_to=reply, mentions=[mention, mention])),
                         (STORED, None))
        (row,) = self.store.messages(ROOM)
        self.assertEqual(row, dict(entry=entry_id(2), first=2, member=ADMIN, member_name='Ada', bot=None, bot_name=None,
                                   text='Hello **team**', reply_to=reply, mentions=json.dumps([mention]),
                                   created_at=team_store.relay_time(START + 2),
                                   updated_at=team_store.relay_time(START + 2),
                                   edited=0))
        self.assertEqual(self.store.chatted, {ROOM})

    def test_unknown_fields_and_null_fields_stay_in_the_payload_and_change_nothing(self):
        payload = self.message('With an attachment', attachment={'sealed': 'later'}, reply_to=None, mentions=None,
                               bot=None, reaction=7)
        self.assertEqual(self.write(1, payload), (STORED, None))
        self.assertEqual(self.texts(), ['With an attachment'])
        self.assertEqual(json.loads(self.store.entry(ROOM, entry_id(1))['payload'])['attachment'], {'sealed': 'later'})

    def test_a_message_that_does_not_fit_is_stored_and_shows_nothing(self):
        cases = {
            'no text': self.message(text=''), 'a text that is not a string': dict(self.message(), text=['Hello']),
            'a text that is too long': self.message('x' * (team_store.MAX_TEXT + 1)),
            'a lone surrogate': self.message('Hello \ud800'),
            'a reply_to in capitals': self.message(reply_to=entry_id(5).upper().replace('0', 'A')),
            'a short reply_to': self.message(reply_to='abc'), 'a reply_to that is a number': self.message(reply_to=5),
            'mentions that are no list': self.message(mentions=entry_id(1)),
            'too many mentions': self.message(mentions=[entry_id(n) for n in range(team_store.MAX_MENTIONS + 1)]),
            'a mention of the wrong form': self.message(mentions=[entry_id(1), 'a' * 64]),
            'a mention that is no string': self.message(mentions=[1]),
            'a bot of the wrong form': self.message(bot='t-' + entry_id(1)),
        }
        for number, (name, payload) in enumerate(cases.items(), start=1):
            with self.subTest(case=name):
                self.assertEqual(self.write(number, payload), (STORED, None))
                self.assertIsNotNone(self.store.entry(ROOM, entry_id(number)))
        self.assertEqual(self.texts(), [])
        # A message at the limits fits.
        self.write(50, self.message('x' * team_store.MAX_TEXT, mentions=[entry_id(n) for n in range(16)]))
        self.assertEqual([len(text) for text in self.texts()], [team_store.MAX_TEXT])

    def test_numbers_out_of_range_show_nothing(self):
        # A `rev` out of range is no payload of an entry (rule 2), and a time of the relay after the year 9999 is no
        # time of the chat.
        self.assertEqual(self.write(1, self.message(rev=0))[0], team_store.DROPPED)
        self.assertEqual(self.write(2, self.message(rev=MAX_NUMBER + 1))[0], team_store.DROPPED)
        self.assertEqual(self.write(3, self.message(), at=MAX_NUMBER), (STORED, None))
        self.assertEqual(self.write(4, self.message(rev=True))[0], team_store.DROPPED)
        self.assertEqual(self.texts(), [])

    def test_a_message_of_a_bot_shows_only_with_a_bot_entry_of_the_same_member(self):
        self.write(1, self.bot(), OTHER)
        self.write(2, self.message('Of a bot of another member', bot=entry_id(1)))
        self.write(3, self.message('Of no known bot', bot=entry_id(9)))
        self.assertEqual(self.texts(), [])
        # The relay keeps only the newest write of a bot entry, so a Mac can read the message before its bot.
        self.write(4, self.message('Before its bot', bot=entry_id(5)))
        self.write(5, self.bot(), ADMIN)
        self.assertEqual(self.texts(), ['Before its bot'])
        self.assertEqual([(row['bot'], row['bot_name']) for row in self.store.messages(ROOM)],
                         [(entry_id(5), 'Reporter')])
        self.write(5, dict(self.bot('Reporter 2'), rev=2), ADMIN)
        self.assertEqual(self.store.messages(ROOM)[0]['bot_name'], 'Reporter 2')
        # A delete over the bot entry keeps the history of the bot and its last name.
        self.write(5, {'kind': 'delete', 'rev': 3}, ADMIN)
        self.assertEqual(self.texts(), ['Before its bot'])
        self.assertEqual(self.store.messages(ROOM)[0]['bot_name'], 'Reporter 2')
        # A bot entry that does not fit was never shown, so its messages do not show.
        self.write(6, dict(self.bot(), name=''), ADMIN)
        self.write(7, self.message('Of a bot that does not fit', bot=entry_id(6)))
        self.assertEqual(self.texts(), ['Before its bot'])

    def test_only_the_member_of_a_message_edits_or_deletes_it(self):
        self.write(1, self.message('First words'))
        self.assertEqual(self.write(1, self.message('Changed by another member', rev=2), OTHER), (REFUSED, 'member'))
        self.assertEqual(self.write(1, {'kind': 'delete', 'rev': 2}, OTHER), (REFUSED, 'member'))
        self.assertEqual(self.write(1, self.message('An old write', rev=1)), (REFUSED, 'rev'))
        self.assertEqual(self.texts(), ['First words'])
        self.write(2, self.message('Second words'))
        self.store.chatted.clear()
        # An edit keeps its place and its first time, and has the time of its own write.
        self.assertEqual(self.write(1, self.message('Last words', rev=2)), (STORED, None))
        rows = self.store.messages(ROOM)
        self.assertEqual([(row['text'], row['first'], row['edited']) for row in rows],
                         [('Second words', 5, 0), ('Last words', 1, 1)])
        self.assertEqual((rows[1]['created_at'], rows[1]['updated_at']),
                         (team_store.relay_time(START + 1), team_store.relay_time(START + 6)))
        self.assertEqual(self.store.chatted, {ROOM})
        # A delete removes the row. The newest write counts, as on the relay, so an edit after the delete shows the
        # message again on each Mac.
        self.write(1, {'kind': 'delete', 'rev': 3})
        self.assertEqual(self.texts(), ['Second words'])
        self.write(1, self.message('After the delete', rev=4))
        self.assertEqual(self.texts(), ['Second words', 'After the delete'])
        # A later write that does not fit hides the message.
        self.write(1, self.message('\udfff', rev=5))
        self.assertEqual(self.texts(), ['Second words'])

    def erase(self, number: int, target: int, member: str = ADMIN, rev: int = 1):
        return self.write(number, {'kind': 'erase', 'rev': rev, 'at': '2026-11-10T12:00:00Z', 'entry': entry_id(target)},
                          member)

    def test_an_erase_of_the_admin_removes_the_message_its_unread_count_and_its_waiting_trigger(self):
        self.write(1, self.member('Grace'), MEMBER)
        self.write(2, self.message('Wrong words'), OTHER)
        self.write(3, self.message('Kept words'), OTHER)
        for number, started in ((2, 0), (3, 0)):
            self.db.execute('INSERT INTO team_triggers (room, bot, entry, started) VALUES (?, ?, ?, ?)',
                            (ROOM, 'bot', entry_id(number), started))
        self.assertEqual(self.store.unread(ROOM, MEMBER), 2)
        self.store.chatted.clear()
        self.assertEqual(self.erase(4, 2), (STORED, None))
        self.assertEqual(self.texts(), ['Kept words'])
        self.assertEqual(self.store.unread(ROOM, MEMBER), 1)
        self.assertIsNone(self.store.message(ROOM, entry_id(2)))
        self.assertEqual(self.db.execute('SELECT entry FROM team_triggers').fetchall(), [(entry_id(3),)])
        self.assertEqual((self.store.chatted, self.store.triggered), ({ROOM}, {ROOM}))
        # The entry of the message stays in the store, as after a `delete`.
        self.assertEqual(self.store.entry(ROOM, entry_id(2))['kind'], 'message')
        # The same erase again changes nothing.
        self.store.chatted.clear()
        self.erase(5, 2)
        self.assertEqual(self.store.chatted, set())

    def test_an_erase_holds_against_a_later_write_and_counts_before_its_message(self):
        self.write(1, self.message('Wrong words'), OTHER)
        self.erase(2, 1)
        # The author edits the message: the edit has a newer `seq`, and the chat still shows nothing.
        self.assertEqual(self.write(1, self.message('Edited words', rev=2), OTHER), (STORED, None))
        self.write(1, {'kind': 'delete', 'rev': 3}, OTHER)
        self.write(1, self.message('Again', rev=4), OTHER)
        self.assertEqual(self.texts(), [])
        # An erase of an entry that this Mac does not have yet holds when the message comes.
        self.erase(3, 9)
        self.assertEqual(self.write(9, self.message('Late words'), OTHER), (STORED, None))
        self.assertEqual(self.texts(), [])
        # A write over the erase entry does not bring a message back.
        self.write(3, {'kind': 'delete', 'rev': 2})
        self.write(9, self.message('Later words', rev=2), OTHER)
        self.assertEqual(self.texts(), [])

    def test_an_erase_of_a_member_that_is_not_the_admin_does_nothing(self):
        self.write(1, self.message('Hello'), OTHER)
        self.assertEqual(self.erase(2, 1, OTHER), (REFUSED, 'admin'))
        self.assertEqual(self.erase(3, 1, MEMBER), (REFUSED, 'admin'))
        self.assertIsNone(self.store.entry(ROOM, entry_id(2)))
        self.assertEqual(self.texts(), ['Hello'])
        # A member cannot make its entry an erase entry either.
        self.write(4, self.message('Mine'), OTHER)
        self.assertEqual(self.write(4, {'kind': 'erase', 'rev': 2, 'entry': entry_id(1)}, OTHER), (REFUSED, 'admin'))
        self.assertEqual(self.texts(), ['Mine', 'Hello'])

    def test_an_erase_of_an_entry_of_another_kind_or_of_the_wrong_form_does_nothing(self):
        self.write(1, self.member('Lin'), OTHER)
        self.write(2, {'kind': 'direct', 'rev': 1, 'at': '2026-11-10T12:00:00Z', 'box': 'x' * 40}, OTHER)
        self.write(3, self.bot(), OTHER)
        for number, target in ((4, 1), (5, 2), (6, 3)):
            self.assertEqual(self.erase(number, target), (STORED, None))
        self.assertEqual([member['name'] for member in self.store.members(ROOM)], ['Lin'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM team_erased').fetchone()[0], 0)
        # A later message write over such an entry is a write of its member, and it shows.
        self.write(2, self.message('Now a message', rev=2), OTHER)
        self.assertEqual(self.texts(), ['Now a message'])
        # An erase without an entry ID is stored and does nothing.
        self.write(7, self.message('Hello'), OTHER)
        for number, value in ((8, None), (9, 'abc'), (10, 7)):
            self.assertEqual(self.write(number, {'kind': 'erase', 'rev': 1, 'entry': value}), (STORED, None))
        self.assertEqual(self.texts(), ['Hello', 'Now a message'])

    def test_the_admin_erases_a_message_of_a_bot_and_of_a_removed_member(self):
        self.write(1, self.bot(), OTHER)
        self.write(2, self.message('Of a bot', bot=entry_id(1)), OTHER)
        self.write(3, self.member('Lin'), OTHER)
        self.write(4, self.message('Of Lin'), OTHER)
        self.write(5, {'kind': 'remove', 'rev': 1, 'at': '2026-11-10T12:00:00Z', 'member': OTHER, 'name': 'Lin'})
        self.assertEqual(self.texts(), ['Of Lin', 'Of a bot'])
        self.erase(6, 2)
        self.erase(7, 4)
        self.assertEqual(self.texts(), [])

    def test_a_member_that_left_keeps_its_last_name(self):
        self.write(1, self.member('Ada'))
        self.write(2, self.message('Hello'))
        self.write(1, self.member('Ada L.', rev=2))
        self.assertEqual(self.store.messages(ROOM)[0]['member_name'], 'Ada L.')
        self.write(1, {'kind': 'delete', 'rev': 3})
        self.assertEqual(self.store.members(ROOM), [])
        self.assertEqual(self.store.messages(ROOM)[0]['member_name'], 'Ada L.')
        # A member whose `member` entry this Mac never saw has no name.
        self.write(3, self.message('From before my join'), OTHER)
        self.assertEqual(self.store.messages(ROOM)[0]['member_name'], None)

    def test_the_unread_count_and_the_read_position(self):
        # The position starts at the own `member` entry, so the history before the join is read.
        self.write(1, self.message('Before the join'))
        self.write(2, self.member('Grace'), MEMBER)
        self.assertEqual((self.store.read_position(ROOM), self.store.unread(ROOM, MEMBER)), (2, 0))
        self.write(3, self.message('One'))
        self.write(4, self.message('Mine'), MEMBER)
        self.write(5, self.message('Two'), OTHER)
        # Only a message of this Mac's person counts as read. The person did not write the reply of an own bot.
        self.write(6, self.bot(), MEMBER)
        self.write(7, self.message('Of my bot', bot=entry_id(6)), MEMBER)
        self.assertEqual(self.store.unread(ROOM, MEMBER), 3)
        self.assertEqual(self.store.mark_read(ROOM, 3), 3)
        self.assertEqual(self.store.unread(ROOM, MEMBER), 2)
        # The position never moves back, and never past the newest message.
        self.assertIsNone(self.store.mark_read(ROOM, 2))
        self.assertEqual(self.store.mark_read(ROOM, MAX_NUMBER), 7)
        self.assertEqual((self.store.read_position(ROOM), self.store.unread(ROOM, MEMBER)), (7, 0))
        for number in range(8, 208):
            self.write(number, self.message(f'Message {number}'))
        self.assertEqual(self.store.unread(ROOM, MEMBER), team_store.MAX_UNREAD)

    def plan(self, query: str, values: tuple) -> str:
        return ' '.join(row[-1] for row in self.db.execute('EXPLAIN QUERY PLAN ' + query, values))

    def test_a_new_name_changes_the_chat_of_a_room_with_messages(self):
        # A room without messages gets no event for a name.
        self.write(1, self.member('Ada'))
        self.write(2, self.bot(), ADMIN)
        self.assertEqual(self.store.chatted, set())
        # The message of a bot comes before its bot entry. It shows when the bot entry comes later.
        self.write(3, self.message('Before its bot', bot=entry_id(4)))
        self.store.chatted.clear()
        self.write(4, self.bot(), ADMIN)
        self.assertEqual((self.texts(), self.store.chatted), (['Before its bot'], {ROOM}))
        # A rename changes the names of the messages. The same profile again changes nothing.
        for fields, chatted in ((self.member('Ada L.', rev=2), {ROOM}), (self.member('Ada L.', rev=3), set()),
                                (dict(self.bot('Reporter 2'), rev=2), {ROOM}), (dict(self.bot('Reporter 2'), rev=3), set())):
            self.store.chatted.clear()
            self.write(1 if fields['kind'] == 'member' else 4, fields, ADMIN)
            self.assertEqual(self.store.chatted, chatted, fields)
        # A second `member` entry of the member takes the place of the first one, and its name.
        self.store.chatted.clear()
        self.write(5, self.member('Ada L.'), ADMIN)
        self.assertEqual(self.store.chatted, {ROOM})

    def test_a_page_and_the_unread_count_stay_fast_with_10000_messages(self):
        self.write(1, self.member('Grace'), MEMBER)
        self.db.execute('BEGIN')
        for number in range(2, 10_002):
            self.write(number, self.message(f'Message {number}'))
        self.db.execute('COMMIT')
        timings = {}
        for name, call in (('newest page', lambda: self.store.messages(ROOM, None, 101)),
                           ('oldest page', lambda: self.store.messages(ROOM, 102, 101)),
                           ('unread count', lambda: self.store.unread(ROOM, MEMBER)),
                           ('newest message', lambda: self.store.messages(ROOM, None, 1))):
            started = time.perf_counter()
            for _ in range(10):
                result = call()
            timings[name] = (time.perf_counter() - started) / 10 * 1000
            self.assertTrue(result)
        print('\n10,000 messages, milliseconds for each call: ' +
              ', '.join(f'{name} {value:.2f}' for name, value in timings.items()))
        self.assertEqual(self.store.unread(ROOM, MEMBER), team_store.MAX_UNREAD)
        self.assertEqual([row['first'] for row in self.store.messages(ROOM, 102, 101)], list(range(101, 1, -1)))
        # The index gives the order, so no call sorts or scans the room. A page with `before` starts its search
        # at `before`. The plan is the one of the statements that TeamStore.messages runs.
        for before, search in ((None, '(room=?)'), (50, '(room=? AND first<?)')):
            plan = self.plan(*self.store._page(ROOM, before, 101))
            self.assertIn(f'SEARCH m USING INDEX team_messages_order {search}', plan)
            self.assertNotIn('TEMP B-TREE', plan)
        plan = self.plan('SELECT 1 FROM team_messages m WHERE m.room=? AND m.first>? AND m.member!=? LIMIT 99',
                         (ROOM, 50, MEMBER))
        self.assertIn('team_messages_order', plan)
        for value in timings.values():
            self.assertLess(value, 50)


class TeamChatTests(TeamMacs, unittest.TestCase):
    """Two Macs in one room: Ada on the first Mac is the admin, and Grace on the second Mac joins."""

    def setUp(self):
        super().setUp()
        self.room = self.create()['room']
        self.join(self.second, self.invite({'room': self.room})['link'])
        self.first.team_sync_pass()

    def path(self, tail: str = 'messages') -> str:
        return f'/v1/team/rooms/{self.room}/{tail}'

    def send(self, service, text: str, **fields) -> dict:
        return self.phone(service)('POST', self.path(), dict(fields, text=text))['message']

    def page(self, service, sync: bool = True, **query) -> dict:
        if sync:
            service.team_sync_pass()
        return self.phone(service)('GET', self.path(), {name: str(value) for name, value in query.items()})

    def texts(self, service, sync: bool = True) -> list[str]:
        return [message['text'] for message in self.page(service, sync)['messages']]

    def view(self, service) -> dict:
        (room,) = self.rooms(service)
        return room

    def test_two_macs_share_one_chat(self):
        sent = self.send(self.first, 'Hello Grace')
        self.assertEqual({name: sent[name] for name in ('text', 'this_mac', 'member_name', 'bot', 'bot_name',
                                                        'reply_to', 'mentions', 'edited_at')},
                         dict(text='Hello Grace', this_mac=True, member_name='Ada', bot=None, bot_name=None,
                              reply_to=None,
                              mentions=[], edited_at=None))
        (received,) = self.page(self.second)['messages']
        self.assertEqual((received['id'], received['first'], received['this_mac'], received['member_name']),
                         (sent['id'], sent['first'], False, 'Ada'))
        self.assertEqual(self.view(self.second)['unread'], 1)
        # Grace answers and names Ada and her bot.
        bot = self.first.mutate('POST', '/v1/bots', {'name': 'Reporter'}, str(uuid4()), 'phone')
        self.share_page(self.first, self.room, bot)
        self.first.team_sync_pass()
        team_bot = self.view(self.second)['bots'][0]['id']
        ada = self.view(self.second)['members'][0]['member']
        answer = self.send(self.second, 'Hello Ada', reply_to=sent['id'], mentions=[ada, team_bot, ada])
        self.assertEqual((answer['reply_to'], answer['mentions']), (sent['id'], [ada, team_bot]))
        (first_view,) = [message for message in self.page(self.first)['messages'] if message['id'] == answer['id']]
        self.assertEqual(first_view['mentions'], [ada, team_bot])
        # The payload has the entry IDs of the contract.
        payload = json.loads(self.first.team_store.entry(self.room, answer['id'])['payload'])
        self.assertEqual(payload['mentions'], [self.first.team_store.members(self.room)[0]['entry'], team_bot[-32:]])
        # Ada edits, then deletes. Both Macs end with the same chat.
        edited = self.phone(self.first)('PATCH', self.path(f'messages/{sent["id"]}'),
                                         {'text': 'Hello, Grace'})['message']
        self.assertEqual((edited['text'], edited['first']), ('Hello, Grace', sent['first']))
        self.assertIsNotNone(edited['edited_at'])
        self.assertEqual(self.texts(self.second), ['Hello Ada', 'Hello, Grace'])
        self.assertEqual(self.phone(self.first)('DELETE', self.path(f'messages/{sent["id"]}'), {}),
                         {'room': self.room, 'id': sent['id'], 'deleted': True})
        chats = [[{name: value for name, value in message.items() if name != 'this_mac'}
                  for message in self.page(service)['messages']] for service in (self.first, self.second)]
        self.assertEqual(chats[0], chats[1])
        self.assertEqual([message['text'] for message in chats[0]], ['Hello Ada'])
        # The reply to a deleted message keeps its reply_to, and a new answer to it is refused.
        self.assertEqual(self.refusal(lambda: self.send(self.second, 'Late', reply_to=sent['id'])),
                         (409, 'The message that you answer is no longer in the chat.'))

    def test_the_rooms_have_the_unread_count_and_the_last_message(self):
        self.assertEqual((self.view(self.second)['unread'], self.view(self.second)['last_message']), (0, None))
        sent = self.send(self.first, 'x' * 300)
        view = self.view(self.second)
        self.assertEqual(view['unread'], 1)
        created = self.page(self.second, sync=False)['messages'][0]['created_at']
        self.assertEqual(view['last_message'], {'first': sent['first'], 'member': sent['member'], 'member_name': 'Ada',
                                                'this_mac': False, 'bot': None, 'bot_name': None,
                                                'text': 'x' * 200,
                                                'created_at': created})
        self.assertTrue(self.view(self.first)['last_message']['this_mac'], 'The writer sees its own message.')
        self.send(self.second, 'Mine')
        self.assertEqual({name: self.view(self.second)['last_message'][name] for name in ('member_name', 'this_mac')},
                         {'member_name': 'Grace', 'this_mac': True})
        self.assertEqual(self.view(self.second)['unread'], 1)
        answer = self.phone(self.second)('POST', self.path('read'), {'first': sent['first']})
        self.assertEqual(answer, {'room': self.room, 'read': sent['first'], 'unread': 0})
        self.assertEqual(self.view(self.second)['unread'], 0)
        self.assertEqual(self.view(self.first)['unread'], 1)
        # A position from before does not move it back.
        self.assertEqual(self.phone(self.second)('POST', self.path('read'), {'first': 1})['read'], sent['first'])
        for value in (-1, 'x', 1.5, True, None, MAX_NUMBER + 1):
            with self.subTest(first=value):
                refusal = self.refusal(self.phone(self.second), 'POST', self.path('read'), {'first': value})
                self.assertEqual(refusal[0], 400)

    def test_pages_go_back_in_order(self):
        sent = [self.send(self.first, f'Message {number}') for number in range(7)]
        page = self.page(self.second, limit=3)
        self.assertEqual([message['text'] for message in page['messages']], ['Message 6', 'Message 5', 'Message 4'])
        page = self.page(self.second, limit=3, before=page['before'])
        self.assertEqual([message['text'] for message in page['messages']], ['Message 3', 'Message 2', 'Message 1'])
        page = self.page(self.second, limit=3, before=page['before'])
        self.assertEqual(([message['id'] for message in page['messages']], page['before']), ([sent[0]['id']], None))
        for query in ({'limit': '0'}, {'limit': '101'}, {'limit': 'x'}, {'before': '-1'}, {'before': '1e3'},
                      {'before': '٣'}, {'before': str(MAX_NUMBER + 1)}):
            with self.subTest(query=query):
                self.assertEqual(self.refusal(self.phone(self.second), 'GET', self.path(), query)[0], 400)
        self.assertEqual(len(self.page(self.second, limit=100)['messages']), 7)

    def test_a_write_that_breaks_a_rule_is_refused_with_a_text_for_a_person(self):
        sent = self.send(self.first, 'Hello')
        self.second.team_sync_pass()
        mention, gone, own = ((400, 'Mention only people and bots of this team.'),
                              (404, 'This message is no longer in the chat.'), (403, 'You can change only your own messages.'))
        member_entry = self.first.team_store.members(self.room)[0]['entry']
        first, second = self.phone(self.first), self.phone(self.second)
        cases = [
            (lambda: self.send(self.first, ''), (400, 'Write a message.')),
            (lambda: self.send(self.first, ' \n'), (400, 'Write a message.')),
            (lambda: self.send(self.first, 'x' * 16001), (400, 'Use at most 16,000 characters for a message.')),
            (lambda: self.send(self.first, 'Hello \ud800'), (400, 'Use only valid text in the message.')),
            (lambda: self.send(self.first, 'Hi', reply_to='x'), (400, gone[1])),
            (lambda: self.send(self.first, 'Hi', mentions=['a' * 64]), mention),
            (lambda: self.send(self.first, 'Hi', mentions='a'), (400, 'Mention at most 16 people and bots in one message.')),
            (lambda: self.send(self.first, 'Hi', mentions=[{}]), mention),
            (lambda: self.send(self.first, 'Hi', mentions=['t-' + 'b' * 64 + '-' + 'c' * 32]), mention),
            (lambda: second('PATCH', self.path(f'messages/{sent["id"]}'), {'text': 'Mine now'}), own),
            (lambda: second('DELETE', self.path(f'messages/{sent["id"]}'), {}), own),
            (lambda: first('PATCH', self.path(f'messages/{"f" * 32}'), {'text': 'Hi'}), gone),
            (lambda: first('DELETE', self.path('messages/not-an-id'), {}), gone),
            # The `member` entry of a member is no message.
            (lambda: first('DELETE', self.path(f'messages/{member_entry}'), {}), gone),
            (lambda: first('GET', f'/v1/team/rooms/{"d" * 64}/messages', {}), (404, 'This Mac is not in this team.')),
        ]
        for call, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.refusal(call), expected)
        self.assertEqual(self.texts(self.first), ['Hello'])
        self.assertEqual(self.texts(self.second), ['Hello'])

    def test_a_failure_of_the_relay_changes_nothing_and_a_removed_mac_cannot_write(self):
        self.relay.down = True
        self.assertEqual(self.refusal(self.send, self.first, 'Lost'),
                         (503, 'Coconut could not reach its server. Try again later.'))
        self.relay.down = False
        self.assertEqual(self.texts(self.first, sync=False), [])
        # The relay takes a write and its answer gets lost. The next write reads the room first, so the chat has both.
        write = self.relay.write

        def lose_the_answer(*arguments, **options):
            self.relay.write = write
            write(*arguments, **options)
            raise TeamRelayError(0, 'The relay did not answer.')
        self.relay.write = lose_the_answer
        self.assertEqual(self.refusal(self.send, self.first, 'Taken')[0], 503)
        self.assertEqual(self.texts(self.first, sync=False), [])
        self.send(self.first, 'Found')
        self.assertEqual(self.texts(self.first, sync=False), ['Found', 'Taken'])
        self.leave(self.second, {'room': self.room})
        self.assertEqual(self.refusal(self.send, self.second, 'After the leave'),
                         (404, 'This Mac is not in this team.'))
        # A Mac that the relay removed keeps its history and cannot write.
        with self.first.transaction():
            self.first.team_store.update_room(self.room, state='removed')
        self.assertEqual(self.refusal(self.send, self.first, 'After the removal'),
                         (409, 'This Mac is no longer in the team.'))
        self.assertEqual(self.texts(self.first, sync=False), ['Found', 'Taken'])

    def test_the_app_learns_of_each_change_of_the_chat(self):
        def changed(service, cursor):
            return service.updates(cursor, 0, collection_deltas=True).get('team_messages_changed')
        cursor = {service: service._cursor() for service in (self.first, self.second)}
        sent = self.send(self.first, 'Hello')
        self.assertEqual(changed(self.first, cursor[self.first]), [self.room])
        self.second.team_sync_pass()
        self.assertEqual(changed(self.second, cursor[self.second]), [self.room])
        # A pass that reads nothing new adds no event. The first Mac takes the time of the relay for its own write.
        self.first.team_sync_pass()
        for service in (self.first, self.second):
            cursor[service] = service._cursor()
            service.team_sync_pass()
            self.assertEqual(service._cursor(), cursor[service])
        # The read position from another device of the person, an edit and a removal.
        self.phone(self.second)('POST', self.path('read'), {'first': sent['first']})
        self.assertEqual(changed(self.second, cursor[self.second]), [self.room])
        cursor[self.second] = self.second._cursor()
        self.phone(self.second)('POST', self.path('read'), {'first': sent['first']})
        self.assertEqual(self.second._cursor(), cursor[self.second])
        for method, body in (('PATCH', {'text': 'Hello again'}), ('DELETE', {})):
            self.phone(self.first)(method, self.path(f'messages/{sent["id"]}'), body)
            self.second.team_sync_pass()
            self.assertEqual(changed(self.second, cursor[self.second]), [self.room], method)
            cursor[self.second] = self.second._cursor()

    def test_an_edit_before_the_sync_keeps_the_relay_time_of_the_send(self):
        sent = self.send(self.first, 'Hello')
        stored = self.relay.rooms[self.room]['entries'][sent['id']]
        stored['at'] += 5000
        relay_at = stored['at']
        # The edit replaces the send on the relay before a pass reads the send back.
        self.phone(self.first)('PATCH', self.path(f'messages/{sent["id"]}'), {'text': 'Hello again'})
        self.first.team_sync_pass()
        (row,) = self.page(self.first, sync=False)['messages']
        self.assertEqual((row['text'], row['created_at']), ('Hello again', team_store.relay_time(relay_at)))

    def test_the_own_write_takes_the_time_of_the_relay(self):
        sent = self.send(self.first, 'Hello')
        stored = self.relay.rooms[self.room]['entries'][sent['id']]
        stored['at'] += 5000
        self.first.team_sync_pass()
        (row,) = self.page(self.first, sync=False)['messages']
        self.assertEqual(row['created_at'], team_store.relay_time(stored['at']))
        self.assertEqual(self.page(self.second)['messages'][0]['created_at'], row['created_at'])

    def raw(self, service, entry: str, fields: dict) -> None:
        """Write an entry to the relay as the member of `service`, as a later version of Coconut could write it."""
        keys = team_keys.read(service.root, self.room)
        payload = json.dumps(dict(fields, at='2026-11-10T12:00:00Z')).encode()
        body = service.team_helper.seal(self.room, entry, 0, keys['keys']['0'], keys['member_key'], payload)
        self.relay.write('', self.room, keys['token'], entry, fields['kind'] == 'bot', body)

    def test_a_message_carries_the_message_that_it_answers(self):
        first = self.send(self.first, 'x' * 300)
        for number in range(3):
            self.send(self.first, f'Message {number}')
        self.second.team_sync_pass()
        answer = self.send(self.second, 'Yes', reply_to=first['id'])
        # The answered message is not in the page, and the quote has the start of its text.
        (view,) = self.page(self.first, limit=1)['messages']
        self.assertEqual(view['reply'], {'id': first['id'], 'member': first['member'], 'member_name': 'Ada',
                                         'bot': None, 'bot_name': None, 'text': 'x' * 200})
        self.assertIsNone(self.page(self.first, limit=2)['messages'][1]['reply'])
        # A message of a bot, and an ID that the room never had.
        bot, by_bot, to_bot, to_nothing = (f'{number:032x}' for number in range(1, 5))
        self.raw(self.second, bot, {'kind': 'bot', 'rev': 1, 'name': 'Reporter', 'avatar': '', 'color': ''})
        self.raw(self.second, by_bot, {'kind': 'message', 'rev': 1, 'text': 'Done.', 'bot': bot})
        # The newest message of the list names its bot and the member of the bot.
        last = self.view(self.first)['last_message']
        self.assertEqual({name: last[name] for name in ('member', 'this_mac', 'bot', 'bot_name')},
                         {'member': answer['member'], 'this_mac': False, 'bot': f't-{self.room}-{bot}',
                          'bot_name': 'Reporter'})
        self.raw(self.second, to_bot, {'kind': 'message', 'rev': 1, 'text': 'Thanks', 'reply_to': by_bot})
        self.raw(self.second, to_nothing, {'kind': 'message', 'rev': 1, 'text': 'Hm', 'reply_to': 'c' * 32})
        views = {message['id']: message for message in self.page(self.first)['messages']}
        self.assertEqual(views[to_bot]['reply'], {'id': by_bot, 'member': answer['member'], 'member_name': 'Grace',
                                                  'bot': f't-{self.room}-{bot}', 'bot_name': 'Reporter',
                                                  'text': 'Done.'})
        self.assertEqual((views[to_nothing]['reply_to'], views[to_nothing]['reply']), ('c' * 32, None))
        # A removed message gives no quote. `reply_to` stays.
        self.phone(self.first)('DELETE', self.path(f'messages/{first["id"]}'), {})
        views = {message['id']: message for message in self.page(self.second)['messages']}
        self.assertEqual((views[answer['id']]['reply_to'], views[answer['id']]['reply']), (first['id'], None))

    def test_a_repeat_of_a_removal_after_a_lost_answer_writes_nothing(self):
        sent = self.send(self.first, 'Hello')
        write = self.relay.write

        def lose_the_answer(*arguments, **options):
            self.relay.write = write
            write(*arguments, **options)
            raise TeamRelayError(0, 'The relay did not answer.')
        self.relay.write = lose_the_answer
        path = self.path(f'messages/{sent["id"]}')
        self.assertEqual(self.refusal(self.phone(self.first), 'DELETE', path, {}, str(uuid4()), 'phone')[0], 503)
        writes = len([request for request in self.relay.requests if request[0] == 'write'])
        # The repeat has no saved answer, because the first request failed. It reads the delete back, and writes nothing.
        self.assertEqual(self.phone(self.first)('DELETE', path, {}, str(uuid4()), 'phone'),
                         {'room': self.room, 'id': sent['id'], 'deleted': True})
        self.assertEqual(len([request for request in self.relay.requests if request[0] == 'write']), writes)
        stored = self.first.team_store.entry(self.room, sent['id'])
        self.assertEqual((stored['kind'], stored['rev'], stored['seq']),
                         ('delete', 2, self.relay.rooms[self.room]['entries'][sent['id']]['seq']))
        self.assertEqual(self.texts(self.first, sync=False), [])
        self.assertEqual(self.texts(self.second), [])
        # An ID that the store does not have is 404. On a Mac that is not the admin, an entry of another member is 403.
        other = self.send(self.first, 'Mine')
        self.second.team_sync_pass()
        self.assertEqual(self.refusal(self.phone(self.second), 'DELETE', self.path(f'messages/{"f" * 32}'), {})[0], 404)
        self.assertEqual(self.refusal(self.phone(self.second), 'DELETE', self.path(f'messages/{other["id"]}'), {})[0],
                         403)
        self.phone(self.first)('DELETE', self.path(f'messages/{other["id"]}'), {})
        self.second.team_sync_pass()
        self.assertEqual(self.refusal(self.phone(self.second), 'DELETE', self.path(f'messages/{other["id"]}'), {})[0],
                         403)

    def writes(self) -> int:
        return len([request for request in self.relay.requests if request[0] == 'write'])

    def test_the_admin_deletes_a_message_of_another_member_on_each_mac(self):
        self.join(self.third, self.invite({'room': self.room})['link'], name='Lin')
        wrong = self.send(self.second, 'Wrong words')
        kept = self.send(self.second, 'Kept words')
        self.assertEqual(self.view(self.third)['unread'], 2)
        self.first.team_sync_pass()
        writes = self.writes()
        path = self.path(f'messages/{wrong["id"]}')
        self.assertEqual(self.phone(self.first)('DELETE', path, {}), {'room': self.room, 'id': wrong['id'], 'deleted': True})
        self.assertEqual(self.writes(), writes + 1)
        # The message goes on the admin's Mac at once, and on the other Macs at their next pass.
        self.assertEqual(self.texts(self.first, sync=False), ['Kept words'])
        self.assertEqual(self.texts(self.second), ['Kept words'])
        self.assertEqual(self.texts(self.third), ['Kept words'])
        self.assertEqual(self.view(self.third)['unread'], 1)
        # A repeat gets the same answer and writes nothing.
        self.assertEqual(self.phone(self.first)('DELETE', path, {}), {'room': self.room, 'id': wrong['id'], 'deleted': True})
        self.assertEqual(self.writes(), writes + 1)
        # The author cannot bring the message back with an edit: the chat of the author does not show it.
        self.assertEqual(self.refusal(self.phone(self.second), 'PATCH', path, {'text': 'Changed words'}),
                         (404, 'This message is no longer in the chat.'))
        # A member that is not the admin cannot delete a message of another member.
        self.assertEqual(self.refusal(self.phone(self.third), 'DELETE', self.path(f'messages/{kept["id"]}'), {}),
                         (403, 'You can change only your own messages.'))
        for service in (self.first, self.second, self.third):
            self.assertEqual(self.texts(service), ['Kept words'])
        # A message that its author deleted is gone already. The admin gets the same answer and writes nothing.
        self.phone(self.second)('DELETE', self.path(f'messages/{kept["id"]}'), {})
        self.first.team_sync_pass()
        writes = self.writes()
        self.assertEqual(self.phone(self.first)('DELETE', self.path(f'messages/{kept["id"]}'), {})['deleted'], True)
        self.assertEqual(self.writes(), writes)

    def test_the_admin_deletes_a_message_of_a_bot_and_a_later_write_shows_nothing(self):
        bot, by_bot = (f'{number:032x}' for number in range(1, 3))
        self.raw(self.second, bot, {'kind': 'bot', 'rev': 1, 'name': 'Reporter', 'avatar': '', 'color': ''})
        self.raw(self.second, by_bot, {'kind': 'message', 'rev': 1, 'text': 'Done.', 'bot': bot})
        self.assertEqual(self.texts(self.first), ['Done.'])
        self.phone(self.first)('DELETE', self.path(f'messages/{by_bot}'), {})
        self.assertEqual(self.texts(self.second), [])
        # A later write of the erased entry by its member, for example an edit, shows nothing on each Mac.
        self.raw(self.second, by_bot, {'kind': 'message', 'rev': 2, 'text': 'Done again.', 'bot': bot})
        self.assertEqual(self.texts(self.first), [])
        self.assertEqual(self.texts(self.second), [])

    def test_a_repeat_of_an_erase_after_a_lost_answer_writes_nothing(self):
        sent = self.send(self.second, 'Wrong words')
        self.first.team_sync_pass()
        write = self.relay.write

        def lose_the_answer(*arguments, **options):
            self.relay.write = write
            write(*arguments, **options)
            raise TeamRelayError(0, 'The relay did not answer.')
        self.relay.write = lose_the_answer
        path = self.path(f'messages/{sent["id"]}')
        self.assertEqual(self.refusal(self.phone(self.first), 'DELETE', path, {})[0], 503)
        writes = self.writes()
        self.assertEqual(self.phone(self.first)('DELETE', path, {}), {'room': self.room, 'id': sent['id'], 'deleted': True})
        self.assertEqual(self.writes(), writes)
        self.assertEqual(self.texts(self.first, sync=False), [])
        self.assertEqual(self.texts(self.second), [])

    def test_only_the_admin_of_an_active_room_can_delete_messages(self):
        self.assertEqual(self.view(self.first)['can_delete_messages'], True)
        self.assertEqual(self.view(self.second)['can_delete_messages'], False)
        with self.first.transaction():
            self.first.team_store.update_room(self.room, state='removed')
        self.assertEqual(self.view(self.first)['can_delete_messages'], False)

    def test_an_erase_of_a_member_that_is_not_the_admin_changes_nothing_and_its_log_line_has_no_name(self):
        sent = self.send(self.first, 'Hello')
        self.second.team_sync_pass()
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        logger = logging.getLogger('coconut')
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.raw(self.second, 'e' * 32, {'kind': 'erase', 'rev': 1, 'entry': sent['id']})
        self.assertEqual(self.texts(self.first), ['Hello'])
        self.assertEqual(self.texts(self.second), ['Hello'])
        # Each of the two Macs writes one line.
        lines = [record.getMessage() for record in records if 'e' * 32 in record.getMessage()]
        self.assertEqual(len(lines), 2, lines)
        for line in lines:
            self.assertIn('refused the entry', line)
            self.assertIn('(admin)', line)
            for name in ('Ada', 'Grace', 'Hello'):
                self.assertNotIn(name, line)

    def test_a_repeat_with_the_same_key_sends_one_message(self):
        key = str(uuid4())
        answers = [self.phone(self.first)('POST', self.path(), {'text': 'Once'}, key, 'phone') for _ in range(2)]
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(self.texts(self.second), ['Once'])
        # The relay takes the write, and its answer gets lost. The repeat finds the write and sends no second message.
        write, key = self.relay.write, str(uuid4())

        def lose_the_answer(*arguments, **options):
            self.relay.write = write
            write(*arguments, **options)
            raise TeamRelayError(0, 'The relay did not answer.')
        self.relay.write = lose_the_answer
        lost = self.refusal(self.phone(self.first), 'POST', self.path(), {'text': 'Twice'}, key, 'phone')
        self.assertEqual(lost[0], 503)
        repeat = self.phone(self.first)('POST', self.path(), {'text': 'Twice'}, key, 'phone')['message']
        self.assertEqual((repeat['text'], repeat['edited_at']), ('Twice', None))
        self.assertEqual(self.texts(self.second), ['Twice', 'Once'])
        # The same key of another device is another request.
        self.phone(self.first)('POST', self.path(), {'text': 'Twice'}, key, 'tablet')
        self.assertEqual(self.texts(self.second), ['Twice', 'Twice', 'Once'])
        # A repeat of a removal gets the same answer.
        key = str(uuid4())
        deleted = [self.phone(self.first)('DELETE', self.path(f'messages/{answers[0]["message"]["id"]}'), {}, key,
                                           'phone') for _ in range(2)]
        self.assertEqual(deleted[0], deleted[1])
        self.assertEqual(self.texts(self.second), ['Twice', 'Twice'])

    def test_a_message_of_a_member_that_left_stays_with_its_name(self):
        self.send(self.second, 'Goodbye')
        self.first.team_sync_pass()
        self.leave(self.second, {'room': self.room})
        (message,) = self.page(self.first)['messages']
        self.assertEqual((message['text'], message['member_name']), ('Goodbye', 'Grace'))
        self.assertEqual([member['name'] for member in self.view(self.first)['members']], ['Ada'])

    def test_no_log_line_has_the_text_of_a_message(self):
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, handler)
        secret = 'The private words 7f3a'
        sent = self.send(self.first, secret)
        self.second.team_sync_pass()
        self.phone(self.first)('PATCH', self.path(f'messages/{sent["id"]}'), {'text': secret + ' again'})
        # A message of another member that breaks a rule, and a write that the relay does not answer.
        keys = team_keys.read(self.second.root, self.room)
        body = self.second.team_helper.seal(self.room, 'e' * 32, 0, keys['keys']['0'], keys['member_key'],
                                            json.dumps({'kind': 'message', 'rev': 1, 'text': secret + '\ud800'}).encode())
        self.relay.write('', self.room, keys['token'], 'e' * 32, False, body)
        self.first.team_sync_pass()
        self.relay.down = True
        self.refusal(self.send, self.first, secret)
        # A pass that fails writes a line of coconut.team_log.
        self.first.team_wake(retry=True)
        self.first.team_sync_pass()
        self.relay.down = False
        self.phone(self.first)('DELETE', self.path(f'messages/{sent["id"]}'), {})
        self.second.team_sync_pass()
        output = '\n'.join(logging.Formatter().format(record) for record in records)
        self.assertIn('coconut.team_log', output)
        self.assertNotIn('private words', output)


if __name__ == '__main__':
    unittest.main()
