"""Pages that a person shares with a team, the author entries of their bots, and the team pages in the Library of the
other Macs (coconut/team_pages.py, and the `bot` and `page` rules of TeamStore).

Each test has two person's Macs in one room on a FakeRelay. Ada's Mac runs a bot, and Ada shares a page of it. Grace's
Mac reads the log. Some tests write hostile entries to the relay as the first Mac would, without its store. The bots of
a Mac that runs Coconut for a team only are in test_team_only.py.
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
from coconut.service import Service, ServiceError
from coconut.team import PERSON_ONLY
from coconut.team_pages import NO_PAGE_SHARE, PERSONAL_BOTS, team_ids
from coconut.team_relay import MAX_NUMBER, TeamRelayError
from team_fakes import DEVICE, RELAY, FakeHelper, TeamMacs, decode

PAGE = dict(key='weekly-report', title='Weekly report', format='markdown', summary='3 open tasks',
            content='3 tasks are open.\n\n| State | Tasks |\n|---|---|\n| Open | 3 |\n')
# The fields that the phone requires of each document (ios/Coconut/Library.swift, BotDocument), with their types.
# A date within the year that an end date can have.
END = (date.today() + timedelta(days=30)).isoformat()
# The key of the page that makes a bot an author for the tests of hostile entries.
AUTHOR_PAGE = 'author-page'
REQUIRED = {'id': str, 'bot_id': str, 'key': str, 'title': str, 'format': str, 'version': int, 'created_at': str,
            'updated_at': str}


def own_entry(room: dict) -> str:
    """The entry ID of the `bot` entry of the one bot of this Mac that authors a page in the room."""
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

    def publish(self, bot=None, **fields) -> dict:
        return self.first.publish_page((bot or self.bot)['id'], dict(PAGE, **fields))

    def document_id(self, key: str = 'weekly-report', bot=None, service=None) -> str:
        return (service or self.first)._document_of_key((bot or self.bot)['id'], key)['id']

    def share(self, key: str = 'weekly-report', bot=None, room=None, service=None) -> dict:
        """Ada shares the page of the key with the room, as on the page in the Library. Return the document."""
        path = f'/v1/team/rooms/{room or self.room_id}/pages/{self.document_id(key, bot, service)}'
        return (service or self.first).team_request('PUT', path, {}, '', DEVICE)['document']

    def stop(self, key: str = 'weekly-report', bot=None, room=None, service=None) -> dict:
        path = f'/v1/team/rooms/{room or self.room_id}/pages/{self.document_id(key, bot, service)}'
        return (service or self.first).team_request('DELETE', path, {}, '', DEVICE)['document']

    def view(self, service=None) -> dict:
        """The room as the Team screen of the first Mac reads it, without a pass."""
        (room,) = (room for room in self.rooms(service or self.first, sync=False) if room['room'] == self.room_id)
        return room

    def sync(self):
        """A pass on the first Mac, which writes, then one on the second Mac, which reads."""
        self.first.team_sync_pass()
        self.second.team_sync_pass()

    def library(self, service=None) -> dict:
        return (service or self.second).documents()

    def team_pages(self, service=None) -> list[dict]:
        return [document for document in self.library(service)['documents'] if 'team' in document]

    def own(self, key: str = 'weekly-report') -> dict:
        """The document of the key in the Library of the first Mac."""
        (document,) = (row for row in self.library(self.first)['documents'] if row.get('key') == key
                       and 'team' not in row)
        return document

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
        """Share a page of the key `author-page`, so the bot has an author entry. Return its entry ID."""
        self.publish(key=AUTHOR_PAGE, title='Author page')
        self.share(AUTHOR_PAGE)
        return own_entry(self.view())

    def hostile_pages(self, service=None) -> list[dict]:
        """The team pages without the page that bot_entry_id shares."""
        return [page for page in self.team_pages(service) if page['key'] != AUTHOR_PAGE]

    def events(self, service, step, kind: str) -> int:
        """How many events of a kind a Mac adds in a step."""
        cursor = service._cursor()
        step()
        return service.db.execute('SELECT COUNT(*) FROM events WHERE seq>? AND type=?', (cursor, kind)).fetchone()[0]

    # A share and a stop

    def test_a_share_sends_the_page_and_a_stop_takes_it_back(self):
        self.publish()
        (local,) = self.library(self.first)['documents']
        # A page that the person did not share is private, and the team has nothing of it.
        self.assertEqual((local['audience'], local['teams']), ('private', []))
        self.assertEqual(self.view()['bots'], [])
        team = {'room': self.room_id, 'name': 'Example Team'}
        shared = self.share()
        self.assertEqual((shared['id'], shared['audience'], shared['teams']), (local['id'], 'team', [team]))
        self.assertEqual((self.own()['audience'], self.own()['teams']), ('team', [team]))
        self.assertEqual(self.first.document(local['id'])['teams'], [team])
        self.sync()
        (page,) = self.team_pages()
        (team_bot,) = self.library()['team_bots']
        self.assertEqual((page['title'], page['bot_id'], team_bot['name'], team_bot['member']['name']),
                         ('Weekly report', team_bot['id'], 'Reporter', 'Ada'))
        self.assertEqual(self.second.document(page['id'])['content'], PAGE['content'])
        # A second share changes nothing: no event, and the next pass writes nothing.
        self.assertEqual(self.events(self.first, self.share, 'document.updated'), 0)
        before = len(self.relay.requests)
        self.first.team_sync_pass()
        self.assertEqual([request[0] for request in self.relay.requests[before:]], ['read'])
        stopped = self.stop()
        self.assertEqual((stopped['audience'], stopped['teams']), ('private', []))
        self.sync()
        self.assertEqual((self.team_pages(), self.library()['team_bots']), ([], []))
        self.assertEqual(self.refusal(self.second.document, page['id']), (404, 'Document was not found.'))
        # The Mac replaced its entries with `delete` entries, which the relay does not keep.
        self.assertEqual((self.entries('bot'), self.entries('page')), ([], []))
        self.assertTrue(all(not row['kept'] for row in self.entries('delete')))
        # A stop of a page that the room does not have changes nothing.
        self.assertEqual(self.events(self.first, self.stop, 'document.updated'), 0)
        # A share again gives the page its entry ID again: one document keeps one entry ID.
        self.share()
        self.sync()
        self.assertEqual(self.team_pages()[0]['id'], page['id'])

    def test_a_share_and_a_stop_tell_the_phone(self):
        self.publish()
        cursor = self.first._cursor()
        self.share()
        update = self.first.updates(cursor, 0, collection_deltas=True)
        # The Library shows the teams of the page, and the Team screen the bot.
        self.assertTrue(update['documents_changed'])
        self.assertTrue(update['team_changed'])
        self.assertNotIn('reset', update)
        cursor = self.first._cursor()
        self.stop()
        update = self.first.updates(cursor, 0, collection_deltas=True)
        self.assertEqual((update['documents_changed'], update['team_changed']), (True, True))

    def test_each_new_version_of_a_shared_page_reaches_the_team(self):
        self.publish()
        self.share()
        self.sync()
        (written,) = self.entries('page')
        for version, content in ((2, '4 tasks are open.'), (3, '5 tasks are open.')):
            with self.subTest(version=version):
                # A save of the bot names no audience, and the page stays shared.
                with mock.patch.object(self.first, 'team_wake', wraps=self.first.team_wake) as wake:
                    self.assertEqual(self.publish(content=content)['version'], version)
                wake.assert_called()
                self.sync()
                (page,) = self.team_pages()
                self.assertEqual((page['version'], self.second.document(page['id'])['content']), (version, content))
                self.assertEqual(self.entries('page')[0]['id'], written['id'])
        self.assertEqual(self.own()['teams'], [{'room': self.room_id, 'name': 'Example Team'}])

    def test_the_author_entry_comes_with_the_first_page_and_goes_with_the_last(self):
        self.publish()
        self.publish(key='daily-note', title='Daily note')
        self.share()
        entry = own_entry(self.view())
        self.share('daily-note')
        self.sync()
        # One bot entry for the two pages of the bot.
        (written,) = self.entries('bot')
        self.assertEqual((written['id'], written['kept'], written['payload']['name']), (entry, True, 'Reporter'))
        self.assertEqual(len(self.team_pages()), 2)
        # A stop of one page keeps the bot. A delete of the last page takes the bot out.
        self.stop()
        self.sync()
        self.assertEqual(([page['key'] for page in self.team_pages()], len(self.entries('bot'))), (['daily-note'], 1))
        self.assertEqual(own_entry(self.view()), entry)
        self.first.mutate('DELETE', f'/v1/documents/{self.document_id("daily-note")}', {}, str(uuid4()), 'phone')
        self.assertEqual(self.view()['bots'], [])
        self.sync()
        self.assertEqual((self.team_pages(), self.library()['team_bots'], self.entries('bot')), ([], [], []))
        self.assertEqual(self.first.team_store.shares(self.room_id), {})
        # A delete of the bot through its tool takes the page and the bot out too.
        self.share()
        self.sync()
        self.assertEqual(len(self.entries('bot')), 1)
        self.first.delete_page(self.bot['id'], {'key': 'weekly-report'})
        self.sync()
        self.assertEqual((self.team_pages(), self.entries('bot')), ([], []))

    def test_a_new_name_of_the_bot_writes_its_entry_again(self):
        self.publish()
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

    def test_an_archived_or_deleted_bot_takes_its_shared_pages_out(self):
        for change in ('archive', 'delete'):
            with self.subTest(change=change):
                self.publish()
                self.share()
                self.sync()
                self.assertEqual(len(self.team_pages()), 1)
                if change == 'archive':
                    self.first.mutate('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': True}, str(uuid4()), 'phone')
                else:
                    self.first.mutate('DELETE', f'/v1/bots/{self.bot["id"]}', {}, str(uuid4()), 'phone')
                self.sync()
                self.assertEqual((self.team_pages(), self.library()['team_bots']), ([], []))
                self.assertEqual((self.entries('bot'), self.entries('page')), ([], []))
                if change == 'archive':
                    # The page cannot be shared while its bot is archived. A restore brings the page back.
                    self.assertEqual(self.refusal(self.share),
                                     (409, 'The bot of this page is archived. Restore the bot, then share the page.'))
                    self.first.mutate('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': False}, str(uuid4()), 'phone')
                    self.sync()
                    self.assertEqual(len(self.team_pages()), 1)
                    self.stop()
                    self.sync()

    # A bot cannot share

    def test_a_bot_cannot_share_and_its_save_keeps_the_shares(self):
        self.publish()
        self.share()
        for audience in ('team', 'private'):
            with self.subTest(audience=audience), self.assertRaises(ServiceError) as refused:
                self.publish(content='Version 2.', audience=audience)
            self.assertEqual((refused.exception.status, str(refused.exception)), (400, documents.BOT_SHARE))
        # A save without it keeps the page shared, and the bot reads that the page is a team page.
        self.assertEqual(self.publish(content='Version 2.')['version'], 2)
        self.assertEqual(self.own()['audience'], 'team')
        self.assertEqual(self.first.read_page(self.bot['id'], {'key': 'weekly-report'})['audience'], 'team')
        self.assertEqual(self.first.read_page(self.bot['id'], {})['documents'][0]['audience'], 'team')

    def test_an_app_or_a_tool_of_today_gets_the_reason_from_the_old_routes(self):
        self.publish()
        self.share()
        for method, path, body in (('POST', f'/v1/team/rooms/{self.room_id}/bots', {'bot_id': self.bot['id']}),
                                   ('DELETE', f'/v1/team/rooms/{self.room_id}/bots/{self.bot["id"]}', {})):
            with self.subTest(method=method):
                self.assertEqual(self.refusal(self.first.team_request, method, path, body, '', DEVICE),
                                 (409, PERSONAL_BOTS))
                # A tool of this Mac gets the line of the person's Mac. `coconut team add-bot` reads the mode first.
                self.assertEqual(self.refusal(self.first.team_request, method, path, body), (403, PERSON_ONLY))
        # The refusal changes nothing.
        self.assertEqual(self.own()['teams'], [{'room': self.room_id, 'name': 'Example Team'}])

    def test_a_page_that_does_not_fit_in_one_entry_is_refused(self):
        # Each control character takes 6 bytes in JSON, so 512 KB of them do not fit in one entry. A private page has
        # no such limit, so the share refuses it.
        self.assertEqual(self.publish(content='\x01' * MAX_CONTENT)['status'], 'saved')
        status, text = self.refusal(self.share)
        self.assertEqual(status, 413)
        self.assertIn('the limit is 1048479', text)
        self.assertEqual(self.own()['teams'], [])
        # A save of a shared page that does not fit is refused, so the bot can make it smaller.
        self.publish(content='é' * (MAX_CONTENT // 2))
        self.share()
        with self.assertRaises(ServiceError) as raised:
            self.publish(content='\x01' * MAX_CONTENT)
        self.assertEqual(raised.exception.status, 413)

    def test_a_share_refuses_what_it_cannot_share(self):
        self.publish()
        document = self.document_id()
        self.sync()
        other = self.second.mutate('POST', '/v1/bots', {'name': 'Helper'}, str(uuid4()), 'phone')
        self.share_page(self.second, self.room_id, other)
        self.second.team_sync_pass()
        self.first.team_sync_pass()
        (team_page,) = self.team_pages(self.first)
        missing = (404, 'Document was not found.')
        cases = [
            ('a team page of another Mac', self.room_id, team_page['id'], missing),
            ('a document of no Mac', self.room_id, str(uuid4()), missing),
            ('a long ID', self.room_id, 'd' * 201, missing),
            ('a path part', self.room_id, '..', missing),
            ('a room of no Mac', '0' * 64, document, (404, 'This Mac is not in this team.')),
            ('a room ID that is not one', 'not-a-room', document, (404, 'This Mac is not in this team.')),
        ]
        for name, room, page, answer in cases:
            with self.subTest(name):
                self.assertEqual(self.refusal(self.first.team_request, 'PUT', f'/v1/team/rooms/{room}/pages/{page}',
                                              {}, '', DEVICE), answer)
                self.assertEqual(self.refusal(self.first.team_request, 'DELETE',
                                              f'/v1/team/rooms/{room}/pages/{page}', {}, '', DEVICE), answer)
        # A page past its end cannot be shared.
        self.publish(key='old-note', title='Old note', expires_at=END)
        later = (date.today() + timedelta(days=60)).isoformat() + 'T00:00:00.000000Z'
        with mock.patch('coconut.service.now', return_value=later):
            self.assertEqual(self.refusal(self.share, 'old-note'), missing)
        # A room that this Mac is no longer in takes no share.
        del self.relay.rooms[self.room_id]
        with self.assertLogs('coconut.team_sync', 'WARNING'):
            self.first.team_sync_pass()
        self.assertEqual(self.refusal(self.share), (409, 'This team no longer exists.'))
        self.assertEqual(self.own()['teams'], [])

    # The shares of today

    def test_the_one_step_keeps_the_pages_that_a_team_has_today(self):
        """A store of today: Ada added Reporter to the team, and Reporter saved a page with the audience "team". Ada
        also added Writer, whose pages are private. The old code writes that state. The new service keeps the page
        shared with the same entries, and takes the author entry of Writer out."""
        writer = self.first.mutate('POST', '/v1/bots', {'name': 'Writer'}, str(uuid4()), 'phone')
        with mock.patch.object(Service, 'library_reader', False):
            for bot in (self.bot, writer):
                self.first.team_request('POST', f'/v1/team/rooms/{self.room_id}/bots', {'bot_id': bot['id']})
            self.publish(audience='team')
            self.publish(key='private-note', title='Private note')
            self.publish(writer, key='draft', title='Draft')
            self.sync()
        self.assertEqual(len(self.team_pages()), 1)
        self.assertEqual(sorted(bot['name'] for bot in self.library()['team_bots']), ['Reporter', 'Writer'])
        (page_entry,) = self.entries('page')
        reporter_entry = self.first.team_store.shares(self.room_id)[self.bot['id']]
        self.assertIn('audience', json.loads(self.first.db.execute('SELECT data FROM documents WHERE id=?',
                                                                    (self.document_id(),)).fetchone()[0]))
        # The update of Coconut starts the service again on the same folder.
        self.first.close()
        self.first = self.mac('first')
        self.assertEqual(self.first.team_store.page_shares(self.room_id), {self.document_id()})
        self.assertEqual(self.first.team_store.shares(self.room_id), {self.bot['id']: reporter_entry})
        stored = [json.loads(row[0]) for row in self.first.db.execute('SELECT data FROM documents')]
        self.assertEqual([document for document in stored if 'audience' in document], [])
        self.assertEqual(self.own()['teams'], [{'room': self.room_id, 'name': 'Example Team'}])
        self.assertEqual(self.own('private-note')['teams'], [])
        writes = sum(request[0] == 'write' for request in self.relay.requests)
        self.sync()
        # The page and Reporter keep their entries. Only Writer goes: one `delete` entry.
        self.assertEqual(sum(request[0] == 'write' for request in self.relay.requests) - writes, 1)
        self.assertEqual([row['id'] for row in self.entries('page')], [page_entry['id']])
        self.assertEqual([(page['title'], page['version']) for page in self.team_pages()], [('Weekly report', 1)])
        self.assertEqual([bot['name'] for bot in self.library()['team_bots']], ['Reporter'])
        # A second start finds nothing to do.
        self.first.close()
        self.first = self.mac('first')
        self.assertEqual(self.first.team_store.page_shares(self.room_id), {self.document_id()})
        self.first.team_sync_pass()
        self.assertEqual(sum(request[0] == 'write' for request in self.relay.requests) - writes, 1)

    # More than one team, and a leave

    def test_a_mac_in_two_teams_shares_a_page_with_each_team_on_its_own(self):
        other = self.create(name='Design Crew', member_name='Ada')
        self.join(self.third, self.invite(other)['link'], 'Linus')
        self.publish()
        self.share()
        self.first.team_sync_pass()
        self.second.team_sync_pass()
        self.third.team_sync_pass()
        self.assertEqual((len(self.team_pages()), self.team_pages(self.third)), (1, []))
        both = self.share(room=other['room'])
        # The teams follow their names.
        self.assertEqual(both['teams'], [{'room': other['room'], 'name': 'Design Crew'},
                                         {'room': self.room_id, 'name': 'Example Team'}])
        self.first.team_sync_pass()
        self.third.team_sync_pass()
        self.assertEqual([page['team']['name'] for page in self.team_pages(self.third)], ['Design Crew'])
        # Each room has its own author entry of the bot.
        entries = [self.first.team_store.shares(room)[self.bot['id']] for room in (self.room_id, other['room'])]
        self.assertNotEqual(entries[0], entries[1])
        self.assertEqual(self.stop()['teams'], [{'room': other['room'], 'name': 'Design Crew'}])
        self.sync()
        self.third.team_sync_pass()
        self.assertEqual((self.team_pages(), len(self.team_pages(self.third))), ([], 1))
        self.assertEqual(self.first.team_store.shares(self.room_id), {})

    def test_a_leave_takes_the_shares_of_that_team(self):
        other = self.second.mutate('POST', '/v1/bots', {'name': 'Helper'}, str(uuid4()), 'phone')
        shared = self.share_page(self.second, self.room_id, other)
        self.assertEqual(shared['audience'], 'team')
        self.second.team_sync_pass()
        self.first.team_sync_pass()
        self.assertEqual(len(self.team_pages(self.first)), 1)
        cursor = self.second._cursor()
        self.leave(self.second, self.joined)
        self.assertTrue(self.second.updates(cursor, 0, collection_deltas=True)['documents_changed'])
        (local,) = self.library(self.second)['documents']
        self.assertEqual((local['audience'], local['teams']), ('private', []))
        self.assertEqual(self.second.db.execute('SELECT COUNT(*) FROM team_page_shares').fetchone()[0], 0)
        self.first.team_sync_pass()
        self.assertEqual((self.team_pages(self.first), self.library(self.first)['team_bots']), ([], []))

    def test_only_a_paired_device_shares_a_page_or_stops(self):
        # A bot can read the token of the bridge file, so a tool of this Mac cannot share or stop, with or without an
        # Idempotency-Key (team.tool_route). The refusal changes nothing.
        self.publish()
        path = f'/v1/team/rooms/{self.room_id}/pages/{self.document_id()}'
        # The route drops each slash at the end of the path, so the check must too.
        for method in ('PUT', 'DELETE'):
            for key in ('', str(uuid4())):
                for end in ('', '/', '//', '///'):
                    with self.subTest(method=method, key=key, end=end):
                        self.assertEqual(self.refusal(self.first.team_request, method, path + end, {}, key),
                                         (403, PERSON_ONLY))
                        self.assertEqual(self.own()['teams'], [])
        self.share()
        cursor = self.first._cursor()
        self.assertEqual(self.refusal(self.first.team_request, 'DELETE', path, {}), (403, PERSON_ONLY))
        self.assertEqual(self.own()['teams'], [{'room': self.room_id, 'name': 'Example Team'}])
        self.assertEqual(self.first._cursor(), cursor, 'A refusal adds no event.')
        self.assertEqual(self.stop()['teams'], [])

    def test_a_room_with_only_own_shares_that_stops_being_active_tells_the_phone(self):
        # This Mac shares a page, and no other member shares anything, so the Library has no team page of the room.
        self.publish()
        self.share()
        self.first.team_sync_pass()
        self.assertEqual(self.library(self.first)['team_bots'], [])
        member = self.relay.rooms[self.room_id]['members'][self.room['member']]
        for state, change in (('removed', lambda: self.relay.rooms[self.room_id]['members'].pop(self.room['member'])),
                              ('gone', lambda: self.relay.rooms.pop(self.room_id))):
            with self.subTest(state):
                if state == 'gone':
                    # The relay knows this Mac again, and the room is active again on this Mac.
                    self.relay.rooms[self.room_id]['members'][self.room['member']] = member
                    with self.first.transaction():
                        self.first.team_store.update_room(self.room_id, state='active')
                self.assertEqual(self.own()['teams'], [{'room': self.room_id, 'name': 'Example Team'}])
                change()
                cursor = self.first._cursor()
                with self.assertLogs('coconut.team_sync', 'WARNING'):
                    self.first.team_sync_pass()
                self.assertEqual(self.first.team_store.room(self.room_id)['state'], state)
                self.assertTrue(self.first.updates(cursor, 0, collection_deltas=True)['documents_changed'])
                self.assertEqual((self.own()['teams'], self.own()['audience']), ([], 'private'))

    def test_a_mac_that_runs_coconut_for_a_team_only_takes_no_page_share(self):
        self.publish()
        with mock.patch.object(Service, 'library_reader', False):
            for method in ('PUT', 'DELETE'):
                with self.subTest(method=method):
                    self.assertEqual(self.refusal(self.first.team_request, method,
                                                  f'/v1/team/rooms/{self.room_id}/pages/{self.document_id()}', {}),
                                     (409, NO_PAGE_SHARE))

    # The Library of the other Mac

    def test_the_other_mac_lists_opens_and_pins_a_team_page_and_cannot_delete_it(self):
        self.publish()
        self.share()
        self.sync()
        (page,) = self.team_pages()
        (written,) = self.entries('page')
        self.assertEqual(page['id'], f't-{self.room_id}-{written["id"]}')
        self.assertEqual(page['bot_id'], self.library()['team_bots'][0]['id'])
        self.assertEqual((page['key'], page['title'], page['format'], page['summary'], page['version'], page['pinned'],
                          page['team']), ('weekly-report', 'Weekly report', 'markdown', '3 open tasks', 1, False,
                                          {'room': self.room_id, 'name': 'Example Team'}))
        self.assertNotIn('teams', page)
        self.assertEqual(self.second.document(page['id'])['content'], PAGE['content'])
        pinned = self.second.mutate('PATCH', f'/v1/documents/{page["id"]}', {'pinned': True}, str(uuid4()), 'phone')
        self.assertEqual(pinned['pinned'], True)
        self.assertEqual(self.team_pages()[0]['pinned'], True)
        with self.assertRaises(ServiceError) as raised:
            self.second.mutate('DELETE', f'/v1/documents/{page["id"]}', {}, str(uuid4()), 'phone')
        self.assertEqual(raised.exception.status, 403)
        self.assertIn('another Mac of the team', str(raised.exception))
        # A pin of the own page answers with its teams.
        local = self.first.mutate('PATCH', f'/v1/documents/{self.document_id()}', {'pinned': True}, str(uuid4()),
                                  'phone')
        self.assertEqual(local['teams'], [{'room': self.room_id, 'name': 'Example Team'}])
        # A new version replaces the entry, and the pin of the second Mac stays.
        self.publish(content='4 tasks are open.')
        self.sync()
        (page,) = self.team_pages()
        self.assertEqual((page['version'], page['pinned']), (2, True))
        self.assertEqual(self.second.document(page['id'])['content'], '4 tasks are open.')
        # The page goes when the document is deleted. The next pass forgets its entry ID, because the log has a
        # `delete` entry now.
        self.first.mutate('DELETE', f'/v1/documents/{local["id"]}', {}, str(uuid4()), 'phone')
        self.sync()
        self.assertEqual(self.team_pages(), [])
        self.assertEqual(list(self.first.team_store.page_entries(self.room_id)), [local['id']])
        self.first.team_sync_pass()
        self.assertEqual(self.first.team_store.page_entries(self.room_id), {})

    def test_each_team_page_has_the_fields_that_a_phone_of_today_requires(self):
        self.publish()
        self.publish(key='daily-note', title='Daily note', summary=None, expires_at=END)
        self.share()
        self.share('daily-note')
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
        # The first Mac shows its own documents once, as local documents with their teams.
        local = self.library(self.first)
        self.assertEqual((len(local['documents']), local['team_bots']), (2, []))
        self.assertEqual({document['audience'] for document in local['documents']}, {'team'})

    def test_a_page_past_its_end_goes_and_answers_as_an_ended_document(self):
        self.publish(expires_at=END)
        self.share()
        self.sync()
        (page,) = self.team_pages()
        later = (date.today() + timedelta(days=60)).isoformat() + 'T00:00:00.000000Z'
        with mock.patch('coconut.service.now', return_value=later):
            self.assertEqual(self.team_pages(), [])
            self.assertEqual(self.refusal(self.second.document, page['id']), (404, 'This document ended.'))
            # The first Mac replaces the entry of the ended page with a `delete` entry, and the bot has no page
            # to show, so its entry goes too.
            self.first.team_sync_pass()
        self.assertEqual((self.entries('page'), self.entries('bot')), ([], []))

    def test_a_member_that_leaves_takes_its_bots_and_pages_from_each_library(self):
        self.publish()
        self.share()
        self.sync()
        self.assertEqual(len(self.team_pages()), 1)
        self.leave(self.second, self.room)
        # The second Mac shared nothing. Now it shares a page, joins again and leaves.
        joined = self.join(self.second, self.invite(self.room)['link'])
        other = self.second.mutate('POST', '/v1/bots', {'name': 'Helper'}, str(uuid4()), 'phone')
        self.share_page(self.second, self.room_id, other)
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
        self.publish()
        self.publish(key='large-page', title='Large page')
        self.share()
        self.share('large-page')
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
            self.publish(content='Version 2.')
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
        self.publish()
        self.share()
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
        self.publish(title='Weekly \x00 report  ', summary='\x00 \U0001f600',
                     content='Text \x00 with   and \U0001f600.')
        self.share()
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
        self.publish()
        self.share()
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
        self.publish()
        self.share()
        self.first.team_sync_pass()
        with mock.patch.dict('coconut.team_store.SHOWN', page=probe):
            self.second.team_sync_pass()
        self.assertEqual(held, [False])
        self.assertEqual(len(self.team_pages()), 1)

    # The state of the room

    def test_the_library_shows_a_room_only_while_it_is_active(self):
        for state, change in (('removed', self.remove_second), ('gone', self.drop_room)):
            with self.subTest(state):
                self.publish()
                self.share()
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
        self.publish()
        self.share()
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
        self.publish()
        self.share()
        self.first.team_sync_pass()
        self.relay.before = None
        self.assertTrue(held)
        self.assertNotIn(True, held)
        self.assertEqual(len(self.entries('page')), 1)

    # The phone learns of a change

    def test_a_change_of_the_team_library_adds_an_event_that_does_not_reset_the_phone(self):
        self.publish()
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
                self.assertEqual(self.hostile_pages(), [])
                self.assertEqual(self.refusal(self.second.document, f't-{self.room_id}-{entry}')[0], 404)
        self.assertIsNone(self.rooms(self.second, sync=False)[0]['sync']['error'])
        # A valid page of the same bot shows.
        self.write(self.hostile_page(bot))
        self.second.team_sync_pass()
        self.assertEqual(len(self.hostile_pages()), 1)

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
        self.assertEqual([page['key'] for page in self.hostile_pages()], ['other-page'])

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
        self.assertEqual([page['key'] for page in self.hostile_pages()], ['other-page'])

    def test_a_page_of_another_member_that_names_a_bot_of_this_member_shows_nothing(self):
        # The second Mac shares a bot. The first Mac writes a page that names the bot entry of the second Mac.
        other = self.second.mutate('POST', '/v1/bots', {'name': 'Helper'}, str(uuid4()), 'phone')
        self.share_page(self.second, self.room_id, other, AUTHOR_PAGE)
        their_bot = own_entry(self.view(self.second))
        self.second.team_sync_pass()
        self.first.team_sync_pass()
        self.write(self.hostile_page(their_bot))
        for service in (self.first, self.second):
            service.team_sync_pass()
            self.assertEqual(self.hostile_pages(service), [])

    def test_numbers_out_of_range_do_not_stop_the_sync(self):
        bot = self.bot_entry_id()
        self.sync()
        late = self.write(self.hostile_page(bot))
        # The relay gives the largest time of the contract, which Python cannot make a date of.
        self.relay.rooms[self.room_id]['entries'][late]['at'] = MAX_NUMBER
        self.write(self.hostile_page(bot, key='other-page', version=MAX_NUMBER + 1, rev=MAX_NUMBER))
        self.second.team_sync_pass()
        self.assertIsNone(self.rooms(self.second, sync=False)[0]['sync']['error'])
        self.assertEqual([page['key'] for page in self.hostile_pages()], ['other-page'])
        self.assertEqual(self.hostile_pages()[0]['version'], MAX_NUMBER)

    def test_a_bot_entry_that_does_not_fit_or_that_is_deleted_hides_its_pages(self):
        bot = self.write({'kind': 'bot', 'rev': 1, 'name': 'Reporter', 'avatar': '', 'color': ''})
        self.write(self.hostile_page(bot))
        self.second.team_sync_pass()
        self.assertEqual(len(self.hostile_pages()), 1)
        for fields in ({'name': ''}, {'name': 'n' * 81}, {'color': 'c' * 21}, {'avatar': ['a']}, {'name': '\ud800'}):
            with self.subTest(fields):
                rev = self.second.team_store.entry(self.room_id, bot)['rev'] + 1
                self.write(dict({'kind': 'bot', 'rev': rev, 'name': 'Reporter', 'avatar': '', 'color': ''}, **fields),
                           entry=bot)
                self.second.team_sync_pass()
                self.assertEqual((self.hostile_pages(), self.library()['team_bots']), ([], []))
        rev = self.second.team_store.entry(self.room_id, bot)['rev'] + 1
        self.write({'kind': 'bot', 'rev': rev, 'name': 'Reporter', 'avatar': '', 'color': ''}, entry=bot)
        self.second.team_sync_pass()
        self.assertEqual(len(self.hostile_pages()), 1)
        self.write({'kind': 'delete', 'rev': rev + 1}, entry=bot, kept=False)
        self.second.team_sync_pass()
        self.assertEqual((self.hostile_pages(), self.library()['team_bots']), ([], []))

    def test_a_team_id_that_names_no_page_is_not_found(self):
        missing = f't-{self.room_id}-{"0" * 32}'
        self.assertEqual(self.refusal(self.second.document, missing), (404, 'Document was not found.'))
        self.assertEqual(self.second.mutate('DELETE', f'/v1/documents/{missing}', {}, str(uuid4()), 'phone'), {'ok': True})
        with self.assertRaises(ServiceError) as raised:
            self.second.mutate('PATCH', f'/v1/documents/{missing}', {'pinned': True}, str(uuid4()), 'phone')
        self.assertEqual(raised.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
