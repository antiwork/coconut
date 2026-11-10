"""Bots and pages that a Mac shares with a team, and the team pages in the Library of the other Macs
(coconut/team_pages.py, and the `bot` and `page` rules of TeamStore).

Each test has two Macs in one room on a FakeRelay. The first Mac runs a bot and shares it, and the second Mac reads
the log. Some tests write hostile entries to the relay as the first Mac would, without its store.
"""
import base64
from datetime import date, timedelta
import json
import secrets
import threading
import unittest
from unittest import mock
from uuid import uuid4

from coconut import documents, team_keys, team_store, team_sync
from coconut.documents import MAX_CONTENT
from coconut.service import ServiceError
from coconut.team_pages import team_ids
from coconut.team_relay import MAX_NUMBER, TeamRelayError
from team_fakes import RELAY, FakeHelper, TeamMacs, decode

PAGE = dict(key='weekly-report', title='Weekly report', format='markdown', summary='3 open tasks',
            content='3 tasks are open.\n\n| State | Tasks |\n|---|---|\n| Open | 3 |\n')
# The fields that the phone requires of each document (ios/Coconut/Library.swift, BotDocument), with their types.
# A date within the year that an end date can have.
END = (date.today() + timedelta(days=30)).isoformat()
REQUIRED = {'id': str, 'bot_id': str, 'key': str, 'title': str, 'format': str, 'version': int, 'created_at': str,
            'updated_at': str}


def own_entry(room: dict) -> str:
    """The entry ID of the `bot` entry of the one bot that this Mac shares with the room."""
    (bot,) = (bot for bot in room['bots'] if bot['this_mac'])
    return team_ids(bot['id'])[1]


class TeamPageTests(TeamMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.room = self.create()
        self.room_id = self.room['room']
        self.joined = self.join(self.second, self.invite(self.room)['link'])
        self.bot = self.first.mutate('POST', '/v1/bots', {'name': 'Reporter'}, str(uuid4()), 'phone')

    # Steps

    def share(self, bot=None, service=None) -> dict:
        return (service or self.first).team_request('POST', f'/v1/team/rooms/{self.room_id}/bots',
                                                    {'bot_id': (bot or self.bot)['id']})['room']

    def unshare(self, bot=None) -> dict:
        return self.first.team_request('DELETE', f'/v1/team/rooms/{self.room_id}/bots/{(bot or self.bot)["id"]}', {})['room']

    def publish(self, bot=None, **fields) -> dict:
        return self.first.publish_page((bot or self.bot)['id'], dict(PAGE, **fields))

    def sync(self):
        """A pass on the first Mac, which writes, then one on the second Mac, which reads."""
        self.first.team_sync_pass()
        self.second.team_sync_pass()

    def library(self, service=None) -> dict:
        return (service or self.second).documents()

    def team_pages(self, service=None) -> list[dict]:
        return [document for document in self.library(service)['documents'] if 'team' in document]

    def entries(self, kind: str) -> list[dict]:
        """The entries of the first Mac on the relay of this kind, with their payloads."""
        found = []
        for row in self.relay.rooms[self.room_id]['entries'].values():
            payload = json.loads(base64.b64decode(decode(row['body'])['payload']))
            if payload['kind'] == kind and row['member'] == self.room['member']:
                found.append(dict(row, payload=payload))
        return found

    def write(self, fields: dict, *, entry: str | None = None, kept: bool = True, service=None) -> str:
        """Write an entry to the relay as a Mac would, without its store. Return the entry ID."""
        keys = team_keys.read((service or self.first).root, self.room_id)
        entry = entry or secrets.token_hex(16)
        payload = json.dumps(dict(fields, at='2026-11-10T12:00:00Z')).encode()
        body = FakeHelper().seal(self.room_id, entry, 0, keys['keys']['0'], keys['member_key'], payload)
        self.relay.write(RELAY, self.room_id, keys['token'], entry, kept, body)
        return entry

    def hostile_page(self, bot_entry: str, **fields) -> dict:
        return dict({'kind': 'page', 'rev': 1, 'bot': bot_entry, 'key': 'weekly-report', 'title': 'Weekly report',
                     'format': 'markdown', 'summary': None, 'content': 'Text.', 'version': 1, 'expires_at': None},
                    **fields)

    def bot_entry_id(self) -> str:
        return own_entry(self.share())

    # Sharing a bot

    def test_a_shared_bot_gets_one_bot_entry_and_the_other_mac_lists_it(self):
        room = self.share()
        self.assertEqual([(bot['bot_id'], bot['name'], bot['this_mac']) for bot in room['bots']],
                         [(self.bot['id'], 'Reporter', True)])
        self.sync()
        (written,) = self.entries('bot')
        self.assertEqual((written['kept'], written['id'], written['payload']['name']), (True, own_entry(room),
                                                                                      'Reporter'))
        (team_bot,) = self.library()['team_bots']
        self.assertEqual(team_bot, {
            'id': f't-{self.room_id}-{written["id"]}', 'name': 'Reporter', 'avatar': self.bot['avatar'],
            'color': self.bot['color'], 'team': {'room': self.room_id, 'name': 'Example Team'},
            'member': {'id': self.room['member'], 'name': 'Ada'}})
        # The Mac that runs the bot does not list it as a team bot. A second share changes nothing.
        self.assertEqual(self.library(self.first)['team_bots'], [])
        self.assertEqual(own_entry(self.share()), written['id'])
        before = len(self.relay.requests)
        self.first.team_sync_pass()
        self.assertEqual([request[0] for request in self.relay.requests[before:]], ['read'])

    def test_a_new_name_of_the_bot_writes_its_entry_again(self):
        self.share()
        self.sync()
        long_name = 'R' * 150
        self.first.mutate('PATCH', f'/v1/bots/{self.bot["id"]}', {'name': long_name, 'color': '#123456789012345678901'},
                          str(uuid4()), 'phone')
        self.sync()
        (written,) = self.entries('bot')
        # A bot can have a longer name than a member, so the entry takes the start of the name.
        self.assertEqual((written['payload']['rev'], written['payload']['name']), (2, 'R' * 80))
        (team_bot,) = self.library()['team_bots']
        self.assertEqual((team_bot['name'], len(team_bot['color'])), ('R' * 80, 20))

    def test_the_share_routes_refuse_what_they_cannot_share(self):
        self.assertEqual(self.refusal(self.first.team_request, 'POST', f'/v1/team/rooms/{self.room_id}/bots',
                                      {'bot_id': 'not-a-bot'})[0], 404)
        self.assertEqual(self.refusal(self.first.team_request, 'POST', f'/v1/team/rooms/{self.room_id}/bots', {})[0], 400)
        self.first.mutate('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': True}, str(uuid4()), 'phone')
        self.assertEqual(self.refusal(self.share), (409, 'The bot is archived. Restore it, then add it to the team.'))
        self.assertEqual(self.refusal(self.first.team_request, 'POST', f'/v1/team/rooms/{"0" * 64}/bots',
                                      {'bot_id': self.bot['id']})[0], 404)

    def test_an_unshared_archived_or_deleted_bot_goes_from_the_other_mac(self):
        for change in ('unshare', 'archive', 'delete'):
            with self.subTest(change=change):
                self.first.mutate('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': False}, str(uuid4()), 'phone')
                self.share()
                self.publish(audience='team')
                self.sync()
                self.assertEqual(len(self.team_pages()), 1)
                if change == 'unshare':
                    self.unshare()
                elif change == 'archive':
                    self.first.mutate('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': True}, str(uuid4()), 'phone')
                else:
                    self.first.mutate('DELETE', f'/v1/bots/{self.bot["id"]}', {}, str(uuid4()), 'phone')
                self.sync()
                self.assertEqual((self.team_pages(), self.library()['team_bots']), ([], []))
                # The Mac replaced its entries with `delete` entries, which the relay does not keep.
                self.assertEqual((self.entries('bot'), self.entries('page')), ([], []))
                self.assertTrue(all(not row['kept'] for row in self.entries('delete')))
                if change == 'delete':
                    return
                if change == 'unshare':
                    continue
                self.unshare()

    # The audience of a page

    def test_a_team_page_needs_a_shared_bot_and_a_save_without_audience_keeps_it(self):
        with self.assertRaises(ServiceError) as raised:
            self.publish(audience='team')
        self.assertEqual(raised.exception.status, 409)
        self.assertIn('shared with no team', str(raised.exception))
        with self.assertRaises(ServiceError) as raised:
            self.publish(audience='everyone')
        self.assertEqual(raised.exception.status, 400)
        # A new page without the field is private, and the row of a private page has no field.
        self.publish()
        (local,) = self.library(self.first)['documents']
        self.assertNotIn('audience', local)
        self.share()
        self.publish(audience='team', content='Version 2.')
        # A save without the field keeps the audience, also through the tooling path of the Mac.
        self.assertEqual(self.publish(content='Version 3.')['version'], 3)
        (local,) = self.library(self.first)['documents']
        self.assertEqual((local['audience'], local['version']), ('team', 3))
        # A save that only changes the audience is a change.
        self.assertEqual(self.publish(content='Version 3.', audience='private')['status'], 'saved')
        (local,) = self.library(self.first)['documents']
        self.assertNotIn('audience', local)

    def test_a_page_that_does_not_fit_in_one_entry_is_refused_at_its_save(self):
        self.share()
        # Each control character takes 6 bytes in JSON, so 512 KB of them do not fit in one entry.
        with self.assertRaises(ServiceError) as raised:
            self.publish(audience='team', content='\x01' * MAX_CONTENT)
        self.assertEqual(raised.exception.status, 413)
        self.assertIn('the limit is 1048479', str(raised.exception))
        # The payload has no ASCII escapes, so text outside ASCII keeps its size in UTF-8.
        self.assertEqual(self.publish(audience='team', content='é' * (MAX_CONTENT // 2))['status'], 'saved')
        # A private page has no such limit.
        self.assertEqual(self.publish(key='private-page', content='\x01' * MAX_CONTENT)['status'], 'saved')

    # The Library of the other Mac

    def test_the_other_mac_lists_opens_and_pins_a_team_page_and_cannot_delete_it(self):
        self.share()
        self.publish(audience='team')
        self.sync()
        (page,) = self.team_pages()
        (written,) = self.entries('page')
        self.assertEqual(page['id'], f't-{self.room_id}-{written["id"]}')
        self.assertEqual(page['bot_id'], self.library()['team_bots'][0]['id'])
        self.assertEqual((page['key'], page['title'], page['format'], page['summary'], page['version'], page['pinned'],
                          page['team']), ('weekly-report', 'Weekly report', 'markdown', '3 open tasks', 1, False,
                                          {'room': self.room_id, 'name': 'Example Team'}))
        self.assertEqual(self.second.document(page['id'])['content'], PAGE['content'])
        pinned = self.second.mutate('PATCH', f'/v1/documents/{page["id"]}', {'pinned': True}, str(uuid4()), 'phone')
        self.assertEqual(pinned['pinned'], True)
        self.assertEqual(self.team_pages()[0]['pinned'], True)
        with self.assertRaises(ServiceError) as raised:
            self.second.mutate('DELETE', f'/v1/documents/{page["id"]}', {}, str(uuid4()), 'phone')
        self.assertEqual(raised.exception.status, 403)
        self.assertIn('another Mac of the team', str(raised.exception))
        # A new version replaces the entry, and the pin of the second Mac stays.
        self.publish(content='4 tasks are open.')
        self.sync()
        (page,) = self.team_pages()
        self.assertEqual((page['version'], page['pinned']), (2, True))
        self.assertEqual(written['payload']['version'], 1)
        self.assertEqual(self.entries('page')[0]['payload']['version'], 2)
        self.assertEqual(self.second.document(page['id'])['content'], '4 tasks are open.')
        # The page goes when it is private again, and when the document is deleted.
        self.publish(content='4 tasks are open.', audience='private')
        self.sync()
        self.assertEqual(self.team_pages(), [])
        self.assertEqual(self.refusal(self.second.document, page['id']), (404, 'Document was not found.'))
        self.publish(audience='team')
        self.sync()
        self.assertEqual(len(self.team_pages()), 1)
        # One document keeps one entry ID.
        self.assertEqual(self.entries('page')[0]['id'], written['id'])
        local = self.library(self.first)['documents'][0]
        self.first.mutate('DELETE', f'/v1/documents/{local["id"]}', {}, str(uuid4()), 'phone')
        self.sync()
        self.assertEqual(self.team_pages(), [])
        # The next pass forgets the entry ID of the deleted document, because the log has a `delete` entry now.
        self.assertEqual(list(self.first.team_store.page_entries(self.room_id)), [local['id']])
        self.first.team_sync_pass()
        self.assertEqual(self.first.team_store.page_entries(self.room_id), {})

    def test_each_team_page_has_the_fields_that_a_phone_of_today_requires(self):
        self.share()
        self.publish(audience='team')
        self.publish(key='daily-note', title='Daily note', summary=None, audience='team', expires_at=END)
        self.sync()
        answer = self.library()
        self.assertEqual(len(answer['documents']), 2)
        for document in answer['documents']:
            for field, kind in REQUIRED.items():
                self.assertIsInstance(document[field], kind, field)
            self.assertNotIsInstance(document['version'], bool)
            self.assertRegex(document['id'], r'^t-[a-f0-9]{64}-[a-f0-9]{32}$')
            # The times are the ones of a local document, so the phone sorts them together.
            self.assertRegex(document['updated_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$')
        ends = sorted(answer['documents'], key=lambda document: document['key'])
        self.assertEqual(ends[1]['expires_at'], None)
        self.assertRegex(ends[0]['expires_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        # The first Mac shows its own documents once, as local documents with their audience.
        local = self.library(self.first)
        self.assertEqual((len(local['documents']), local['team_bots']), (2, []))
        self.assertEqual({document['audience'] for document in local['documents']}, {'team'})

    def test_a_page_past_its_end_goes_and_answers_as_an_ended_document(self):
        self.share()
        self.publish(audience='team', expires_at=END)
        self.sync()
        (page,) = self.team_pages()
        later = (date.today() + timedelta(days=60)).isoformat() + 'T00:00:00.000000Z'
        with mock.patch('coconut.service.now', return_value=later):
            self.assertEqual(self.team_pages(), [])
            self.assertEqual(self.refusal(self.second.document, page['id']), (404, 'This document ended.'))
            # The first Mac replaces the entry of the ended page with a `delete` entry.
            self.first.team_sync_pass()
        self.assertEqual(self.entries('page'), [])

    def test_a_member_that_leaves_takes_its_bots_and_pages_from_each_library(self):
        self.share()
        self.publish(audience='team')
        self.sync()
        self.assertEqual(len(self.team_pages()), 1)
        self.leave(self.second, self.room)
        # The second Mac shared nothing. Now the first Mac shares, and the second Mac joins again and leaves.
        joined = self.join(self.second, self.invite(self.room)['link'])
        other = self.second.mutate('POST', '/v1/bots', {'name': 'Helper'}, str(uuid4()), 'phone')
        self.share(other, self.second)
        self.second.publish_page(other['id'], dict(PAGE, audience='team'))
        self.second.team_sync_pass()
        self.first.team_sync_pass()
        self.assertEqual([bot['name'] for bot in self.library(self.first)['team_bots']], ['Helper'])
        self.assertEqual(len(self.team_pages(self.first)), 1)
        self.assertEqual(self.team_pages()[0]['title'], 'Weekly report')
        self.leave(self.second, joined)
        self.first.team_sync_pass()
        self.assertEqual((self.team_pages(self.first), self.library(self.first)['team_bots']), ([], []))

    # The refusals of the relay

    def test_a_page_that_the_relay_refuses_does_not_stop_the_others_and_is_not_sent_again(self):
        # The first Mac reads the member entry of the second one, so a later pass changes no member.
        self.sync()
        self.share()
        self.publish(audience='team')
        self.publish(key='large-page', title='Large page', audience='team')
        large = next(document['id'] for document in self.library(self.first)['documents']
                     if document['key'] == 'large-page')
        write = self.relay.write
        attempts = []

        def refuse_one(relay, room, token, entry, kept, body, tries=1):
            if entry == self.first.team_store.page_entries(self.room_id).get(large):
                attempts.append(entry)
                raise TeamRelayError(507, 'The relay answered 507.')
            return write(relay, room, token, entry, kept, body, tries)
        # Each write of the large page fails, and the other writes go on. The app learns of the refusal once.
        with mock.patch.object(self.relay, 'write', refuse_one), self.assertLogs('coconut.team_pages', 'WARNING'):
            self.assertEqual(self.team_events(self.sync), 1)
            self.publish(content='Version 2.', audience='team')
            self.assertEqual(self.team_events(self.sync), 0)
            self.assertEqual(self.team_events(self.sync), 0)
        refused_entry = self.first.team_store.page_entries(self.room_id)[large]
        self.assertEqual(len(attempts), 1)
        self.assertEqual([page['key'] for page in self.team_pages()], ['weekly-report'])
        self.assertEqual(self.team_pages()[0]['version'], 2)
        (room,) = self.rooms(self.first, sync=False)
        self.assertEqual((room['sync']['error'], room['sync']['refused']), (None, [{
            'document': large, 'key': 'large-page', 'entry': refused_entry, 'kind': 'page',
            'reason': 'The relay refused the entry with status 507.'}]))
        # A new version of the page tries again.
        self.publish(key='large-page', title='Large page', content='Smaller.')
        # The refusal clears, and the app learns of that once.
        self.assertEqual(self.team_events(self.sync), 1)
        self.assertEqual(sorted(page['key'] for page in self.team_pages()), ['large-page', 'weekly-report'])
        self.assertEqual(self.rooms(self.first, sync=False)[0]['sync']['refused'], [])
        self.assertEqual(self.team_events(self.sync), 0)

    def team_events(self, step) -> int:
        """How many `team.updated` events the first Mac adds in a step."""
        cursor = self.first._cursor()
        step()
        return self.first.db.execute("SELECT COUNT(*) FROM events WHERE seq>? AND type='team.updated'",
                                     (cursor,)).fetchone()[0]

    # The publish step in each pass

    def writes(self) -> int:
        return sum(request[0] == 'write' for request in self.relay.requests)

    def test_a_pass_after_the_writes_makes_no_write_and_opens_no_write_transaction(self):
        self.share()
        self.publish(audience='team')
        # The first pass writes the bot and the page, and the second one reads them back.
        self.first.team_sync_pass()
        self.first.team_sync_pass()
        before, opened = self.writes(), []
        transaction = self.first.transaction

        def count():
            opened.append(1)
            return transaction()
        with mock.patch.object(self.first, 'transaction', count):
            self.first.team_sync_pass()
        self.assertEqual((self.writes() - before, opened), (0, []))

    def test_unusual_text_in_a_bot_and_a_page_is_written_once(self):
        name = 'Re\x00port\x1fer   \U0001f600 é'
        self.first.mutate('PATCH', f'/v1/bots/{self.bot["id"]}', {'name': name, 'avatar': 'a\x00b'}, str(uuid4()),
                          'phone')
        self.share()
        self.publish(audience='team', title='Weekly \x00 report  ', summary='\x00 \U0001f600',
                     content='Text \x00 with   and \U0001f600.')
        self.first.team_sync_pass()
        self.first.team_sync_pass()
        before = self.writes()
        self.first.team_sync_pass()
        self.first.team_sync_pass()
        self.assertEqual(self.writes(), before)
        (written,) = self.entries('bot')
        self.assertEqual(written['payload']['name'], name)
        self.second.team_sync_pass()
        self.assertEqual(self.library()['team_bots'][0]['name'], name)

    def test_an_own_page_that_the_reading_rules_refuse_is_not_written_again(self):
        self.share()
        self.publish(audience='team')
        # A rule of a later version refuses the page, for example a page that a former version saved.
        with mock.patch.dict('coconut.team_store.SHOWN', page=lambda fields: None):
            self.first.team_sync_pass()
            self.first.team_sync_pass()
            before = self.writes()
            self.first.team_sync_pass()
        self.assertEqual(self.writes(), before)

    def test_the_checks_of_a_page_run_without_the_lock_of_the_service(self):
        held = []
        page_fields = team_store.SHOWN['page']

        def probe(fields):
            def check():
                free = self.second.lock.acquire(timeout=0)
                if free:
                    self.second.lock.release()
                held.append(not free)
            thread = threading.Thread(target=check)
            thread.start()
            thread.join()
            return page_fields(fields)
        self.share()
        self.publish(audience='team')
        self.first.team_sync_pass()
        with mock.patch.dict('coconut.team_store.SHOWN', page=probe):
            self.second.team_sync_pass()
        self.assertEqual(held, [False])
        self.assertEqual(len(self.team_pages()), 1)

    # The state of the room

    def test_the_library_shows_a_room_only_while_it_is_active(self):
        for state, change in (('removed', self.remove_second), ('gone', self.drop_room)):
            with self.subTest(state):
                self.share()
                self.publish(audience='team')
                self.sync()
                (page,) = self.team_pages()
                change()
                after = self.second._cursor()
                with self.assertLogs('coconut.team_sync', 'WARNING'):
                    self.assertEqual(self.rooms(self.second)[0]['state'], state)
                # The phone learns that the Library changed.
                self.assertTrue(self.second.updates(after, 0, collection_deltas=True)['documents_changed'])
                self.assertEqual((self.team_pages(), self.library()['team_bots']), ([], []))
                self.assertEqual(self.refusal(self.second.document, page['id']), (404, 'Document was not found.'))
                with self.assertRaises(ServiceError):
                    self.second.mutate('PATCH', f'/v1/documents/{page["id"]}', {'pinned': True}, str(uuid4()), 'phone')
                # The copy stays in the store until the person leaves the room on this Mac.
                self.assertEqual(self.second.team_store.page(*page['id'][2:].split('-')), None)
                self.assertTrue(self.second.team_store.counts(self.room_id)[0])
                self.leave(self.second, self.room)
                if state == 'removed':
                    self.relay.rooms[self.room_id]['members'].pop(self.joined['member'], None)
                    self.joined = self.join(self.second, self.invite(self.room)['link'])

    def remove_second(self):
        del self.relay.rooms[self.room_id]['members'][self.joined['member']]

    def drop_room(self):
        del self.relay.rooms[self.room_id]

    def test_a_relay_that_does_not_answer_fails_the_room_and_the_next_pass_writes(self):
        self.share()
        self.publish(audience='team')
        self.relay.down = True
        with self.assertLogs('coconut.team_sync', 'WARNING'):
            self.first.team_sync_pass()
        self.relay.down = False
        self.first.team_wake(retry=True)
        self.sync()
        self.assertEqual(len(self.team_pages()), 1)

    def test_no_relay_call_of_a_share_or_a_page_holds_the_service_lock(self):
        held = []

        def check():
            def probe():
                free = self.first.lock.acquire(timeout=0)
                if free:
                    self.first.lock.release()
                held.append(not free)
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join()
        self.relay.before = check
        self.share()
        self.publish(audience='team')
        self.first.team_sync_pass()
        self.relay.before = None
        self.assertTrue(held)
        self.assertNotIn(True, held)
        self.assertEqual(len(self.entries('page')), 1)

    # The phone learns of a change

    def test_a_change_of_the_team_library_adds_an_event_that_does_not_reset_the_phone(self):
        self.share()
        self.first.team_sync_pass()
        after = self.second._cursor()
        self.second.team_sync_pass()
        self.assertGreater(self.second._cursor(), after)
        update = self.second.updates(after, 0, collection_deltas=True)
        self.assertNotIn('reset', update)
        self.assertTrue(update['documents_changed'])
        # A pass that changes nothing adds no event.
        after = self.second._cursor()
        self.second.team_sync_pass()
        self.assertEqual(self.second._cursor(), after)

    # Hostile entries of another member

    def test_a_page_that_breaks_a_rule_stays_in_the_store_and_shows_nothing(self):
        bot = self.bot_entry_id()
        self.sync()
        cases = {
            'bad html': dict(format='html', content='<p style="color:#ff0000">x</p>'),
            'too large': dict(content='x' * (MAX_CONTENT + 1)),
            'lone surrogate': dict(title='Weekly \ud800 report'),
            'surrogate in content': dict(content='Text \udfff.'),
            'no content': dict(content=''),
            'bad key': dict(key='Weekly Report'),
            'long summary': dict(summary='s' * 201),
            'summary number': dict(summary=7),
            'bad end': dict(expires_at='2026-13-01T00:00:00Z'),
            'end as number': dict(expires_at=1800000000),
            'no bot': dict(bot=None),
            'bot of a list': dict(bot=[bot]),
            'unknown bot': dict(bot=secrets.token_hex(16)),
        }
        for name, fields in cases.items():
            with self.subTest(name):
                entry = self.write(self.hostile_page(bot, **fields))
                self.second.team_sync_pass()
                self.assertEqual(self.second.team_store.entry(self.room_id, entry)['kind'], 'page')
                self.assertEqual(self.team_pages(), [])
                self.assertEqual(self.refusal(self.second.document, f't-{self.room_id}-{entry}')[0], 404)
        self.assertIsNone(self.rooms(self.second, sync=False)[0]['sync']['error'])
        # A valid page of the same bot shows.
        self.write(self.hostile_page(bot))
        self.second.team_sync_pass()
        self.assertEqual(len(self.team_pages()), 1)

    def test_a_page_whose_check_raises_stays_in_the_store_and_the_room_goes_on(self):
        bot = self.bot_entry_id()
        self.sync()
        page_fields = team_store.SHOWN['page']

        def check(fields):
            if '<![foo]>' in fields['content']:
                # Some versions of Python raise this in the HTML parser for a marked section with an unknown
                # keyword. page_problems catches it, so this check stands for any other defect of a check.
                raise AssertionError('unknown status keyword')
            return page_fields(fields)
        broken = self.write(self.hostile_page(bot, format='html', content='<p>x</p><![foo]>'))
        self.write(self.hostile_page(bot, key='other-page'))
        with mock.patch.dict(team_store.SHOWN, page=check), self.assertLogs('coconut.team_store', 'ERROR') as logs:
            self.second.team_sync_pass()
        self.assertEqual(len(logs.records), 1)
        self.assertIn('AssertionError: unknown status keyword', logs.output[0])
        # The parser itself raising makes the page not valid, and it does not stop the room either.
        feed = documents._PageCode.feed
        with mock.patch.object(documents._PageCode, 'feed', side_effect=AssertionError('unknown status keyword')):
            self.assertIsNone(team_store.page_fields(self.hostile_page(bot, format='html', content='<p>x</p>')))
        self.assertIs(documents._PageCode.feed, feed)
        (room,) = self.rooms(self.second, sync=False)
        self.assertEqual((room['sync']['error'], room['sync']['seq']), (None, self.relay.rooms[self.room_id]['seq']))
        self.assertEqual(self.second.team_store.entry(self.room_id, broken)['kind'], 'page')
        self.assertEqual([page['key'] for page in self.team_pages()], ['other-page'])

    def test_an_error_in_the_first_part_of_the_read_drops_only_its_entry(self):
        bot = self.bot_entry_id()
        self.sync()
        read_entry = team_sync.read_entry

        def fail_for_one(opened):
            if b'broken-page' in (opened.get('payload') or b''):
                raise RuntimeError('A defect of this code.')
            return read_entry(opened)
        broken = self.write(self.hostile_page(bot, key='broken-page'))
        self.write(self.hostile_page(bot, key='other-page'))
        with mock.patch.object(team_sync, 'read_entry', fail_for_one), \
                self.assertLogs('coconut.team_sync', 'WARNING') as logs:
            self.second.team_sync_pass()
        self.assertIn('RuntimeError: A defect of this code.', '\n'.join(logs.output))
        (room,) = self.rooms(self.second, sync=False)
        self.assertEqual((room['sync']['error'], room['sync']['seq']), (None, self.relay.rooms[self.room_id]['seq']))
        self.assertIsNone(self.second.team_store.entry(self.room_id, broken))
        self.assertEqual([page['key'] for page in self.team_pages()], ['other-page'])

    def test_a_page_of_another_member_that_names_a_bot_of_this_member_shows_nothing(self):
        # The second Mac shares a bot. The first Mac writes a page that names the bot entry of the second Mac.
        other = self.second.mutate('POST', '/v1/bots', {'name': 'Helper'}, str(uuid4()), 'phone')
        their_bot = own_entry(self.share(other, self.second))
        self.second.team_sync_pass()
        self.first.team_sync_pass()
        self.write(self.hostile_page(their_bot))
        for service in (self.first, self.second):
            service.team_sync_pass()
            self.assertEqual(self.team_pages(service), [])

    def test_numbers_out_of_range_do_not_stop_the_sync(self):
        bot = self.bot_entry_id()
        self.sync()
        late = self.write(self.hostile_page(bot))
        # The relay gives the largest time of the contract, which Python cannot make a date of.
        self.relay.rooms[self.room_id]['entries'][late]['at'] = MAX_NUMBER
        self.write(self.hostile_page(bot, key='other-page', version=MAX_NUMBER + 1, rev=MAX_NUMBER))
        self.second.team_sync_pass()
        self.assertIsNone(self.rooms(self.second, sync=False)[0]['sync']['error'])
        self.assertEqual([page['key'] for page in self.team_pages()], ['other-page'])
        self.assertEqual(self.team_pages()[0]['version'], MAX_NUMBER)

    def test_a_bot_entry_that_does_not_fit_or_that_is_deleted_hides_its_pages(self):
        bot = self.write({'kind': 'bot', 'rev': 1, 'name': 'Reporter', 'avatar': '', 'color': ''})
        self.write(self.hostile_page(bot))
        self.second.team_sync_pass()
        self.assertEqual(len(self.team_pages()), 1)
        for fields in ({'name': ''}, {'name': 'n' * 81}, {'color': 'c' * 21}, {'avatar': ['a']}, {'name': '\ud800'}):
            with self.subTest(fields):
                rev = self.second.team_store.entry(self.room_id, bot)['rev'] + 1
                self.write(dict({'kind': 'bot', 'rev': rev, 'name': 'Reporter', 'avatar': '', 'color': ''}, **fields),
                           entry=bot)
                self.second.team_sync_pass()
                self.assertEqual((self.team_pages(), self.library()['team_bots']), ([], []))
        rev = self.second.team_store.entry(self.room_id, bot)['rev'] + 1
        self.write({'kind': 'bot', 'rev': rev, 'name': 'Reporter', 'avatar': '', 'color': ''}, entry=bot)
        self.second.team_sync_pass()
        self.assertEqual(len(self.team_pages()), 1)
        self.write({'kind': 'delete', 'rev': rev + 1}, entry=bot, kept=False)
        self.second.team_sync_pass()
        self.assertEqual((self.team_pages(), self.library()['team_bots']), ([], []))

    def test_a_team_id_that_names_no_page_is_not_found(self):
        missing = f't-{self.room_id}-{"0" * 32}'
        self.assertEqual(self.refusal(self.second.document, missing), (404, 'Document was not found.'))
        self.assertEqual(self.second.mutate('DELETE', f'/v1/documents/{missing}', {}, str(uuid4()), 'phone'), {'ok': True})
        with self.assertRaises(ServiceError) as raised:
            self.second.mutate('PATCH', f'/v1/documents/{missing}', {'pinned': True}, str(uuid4()), 'phone')
        self.assertEqual(raised.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
