"""The copy of each team log on a Mac: the thread, its passes, its waits and its repairs (coconut/team_sync.py).

test_team_store.py covers the reading rules one entry at a time. This file covers how the sync gets the entries to
those rules: several answers of the relay, bodies that wait for an update, failures of the relay, and the writes of
this Mac.
"""
import base64
import json
import secrets
import sqlite3
import threading
import unittest
from unittest import mock

from coconut import team_keys, team_sync
from coconut.team_relay import TeamRelayError
from team_fakes import RELAY, FakeHelper, TeamMacs, decode


class TeamSyncTests(TeamMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.room = self.create()
        self.joined = self.join(self.second, self.invite(self.room)['link'])
        self.room_id = self.room['room']

    def write(self, service, fields: dict, *, entry: str | None = None, kept: bool = False, version=None,
              signer: str | None = None) -> str:
        """Write an entry to the relay as a Mac would, without the store of that Mac. Return the entry ID."""
        keys = team_keys.read(service.root, self.room_id)
        entry = entry or secrets.token_hex(16)
        payload = json.dumps(dict(fields, at='2026-11-10T12:00:00Z')).encode()
        body = FakeHelper().seal(self.room_id, entry, 0, keys['keys']['0'], signer or keys['member_key'], payload,
                                 version=version)
        self.relay.write(RELAY, self.room_id, keys['token'], entry, kept, body)
        return entry

    def reads(self) -> int:
        return sum(request[0] == 'read' for request in self.relay.requests)

    def sync_view(self, service) -> dict:
        return self.rooms(service)[0]['sync']

    def test_the_sync_reads_again_while_the_relay_has_more(self):
        self.relay.page = 2
        for number in range(5):
            self.write(self.first, {'kind': 'later', 'rev': 1, 'number': number})
        before = self.reads()
        view = self.sync_view(self.second)
        # The second Mac did not read the room since its join: it reads 7 entries, 2 in each answer.
        self.assertEqual((view['seq'], view['entries']), (7, 7))
        self.assertEqual(self.reads() - before, 4)

    def test_a_body_of_a_later_version_waits_and_opens_after_an_update(self):
        entry = self.write(self.first, {'kind': 'later', 'rev': 1}, version=2)
        with self.assertLogs('coconut.team_sync', 'WARNING') as logs:
            view = self.sync_view(self.second)
        self.assertEqual((view['seq'], view['entries'], view['waiting']), (3, 2, 1))
        self.assertIn('keeps 1 bodies that it cannot open yet', '\n'.join(logs.output))
        # An update of Coconut starts the service again with a helper that knows the version.
        self.second.team_helper = FakeHelper(version=2)
        self.second._team_retried.clear()
        view = self.sync_view(self.second)
        self.assertEqual((view['seq'], view['entries'], view['waiting']), (3, 3, 0))
        self.assertEqual(self.second.team_store.entry(self.room_id, entry)['kind'], 'later')

    def test_each_entry_that_the_mac_drops_gets_a_log_line(self):
        other = FakeHelper().member()
        forged = [self.write(self.first, {'kind': 'member', 'rev': 1, 'name': 'Mallory'}, signer=other['member_key'])
                  for _ in range(2)]
        with self.assertLogs('coconut.team_sync', 'WARNING') as logs:
            view = self.sync_view(self.second)
        self.assertEqual((view['seq'], view['entries']), (4, 2))
        for number, entry in enumerate(forged, start=3):
            self.assertIn(f'dropped the entry {entry} at seq {number} of member {self.room["member"][:8]} (signature)',
                          '\n'.join(logs.output))
        self.assertEqual(self.names(self.second, sync=False), ['Ada', 'Grace'])

    def test_a_room_whose_relay_fails_waits_longer_until_a_wake_with_retry(self):
        self.relay.down = True
        with self.assertLogs('coconut.team_sync', 'WARNING') as logs:
            view = self.sync_view(self.second)
        self.assertEqual(view['error'], 'The relay did not answer.')
        self.assertIn('The next try is in 60 seconds.', logs.output[0])
        self.assertEqual(self.second._team_failures[self.room_id][0], 1)
        # The next pass does not ask the relay while the room waits.
        before = len(self.relay.requests)
        self.second.team_sync_pass()
        self.assertEqual(len(self.relay.requests), before)
        self.assertEqual([team_sync.retry_delay(count) for count in range(1, 7)], [60, 120, 240, 480, 900, 900])
        self.relay.down = False
        self.second.team_wake(retry=True)
        view = self.sync_view(self.second)
        self.assertEqual((view['error'], self.second._team_failures), (None, {}))
        self.assertIsNotNone(view['synced_at'])

    def test_a_mac_that_the_relay_removed_or_a_room_that_went_keeps_its_copy(self):
        for service in (self.first, self.second):
            service.team_sync_pass()
        del self.relay.rooms[self.room_id]['members'][self.joined['member']]
        with self.assertLogs('coconut.team_sync', 'WARNING'):
            self.assertEqual(self.rooms(self.second)[0]['state'], 'removed')
        del self.relay.rooms[self.room_id]
        with self.assertLogs('coconut.team_sync', 'WARNING'):
            self.assertEqual(self.rooms(self.first)[0]['state'], 'gone')
        # The sync reads neither room again, and each Mac still shows the members of its copy.
        before = self.reads()
        for service in (self.first, self.second):
            self.assertEqual(self.names(service), ['Ada', 'Grace'])
        self.assertEqual(self.reads(), before)

    def test_the_sync_writes_the_member_entry_that_a_join_could_not_write(self):
        def lose_the_write():
            if self.relay.requests[-1][0] == 'write':
                raise TeamRelayError(0, 'The relay did not answer.')
        self.relay.before = lose_the_write
        with self.assertLogs('coconut.team', 'WARNING') as logs:
            joined = self.join(self.third, self.invite(self.room)['link'], 'Edsger')
        self.assertIn('the member entry waits for the next pass', '\n'.join(logs.output))
        self.assertEqual([member['name'] for member in joined['members']], [])
        self.relay.before = None
        # The Mac writes the entry again at most each 5 minutes.
        self.third.team_sync_pass()
        self.assertEqual(self.names(self.first), ['Ada', 'Grace'])
        with mock.patch.object(team_sync, 'ANNOUNCE_SECONDS', 0):
            self.third.team_sync_pass()
        for service in (self.first, self.second, self.third):
            self.assertEqual(self.names(service), ['Ada', 'Grace', 'Edsger'])

    def test_a_write_without_an_answer_is_read_back_before_the_next_write(self):
        own = json.loads(self.second.team_store.room(self.room_id)['profile'])['entry']
        write = self.relay.write

        def lose_the_answer(*arguments, **options):
            write(*arguments, **options)
            raise TeamRelayError(0, 'The relay did not answer.')
        renamed = {'kind': 'member', 'name': 'Grace H.', 'avatar': '', 'color': '',
                   'agreement': self.joined['members'][0]['agreement']}
        with mock.patch.object(self.relay, 'write', lose_the_answer), self.second._team_room_lock(self.room_id):
            with self.assertRaises(TeamRelayError):
                self.second._team_write(self.second.team_store.room(self.room_id), own, True, renamed)
        # The relay has the write with `rev` 2, and the store of the second Mac does not.
        self.assertEqual(self.second.team_store.entry(self.room_id, own)['rev'], 1)
        self.assertEqual(self.names(self.first), ['Ada', 'Grace H.'])
        # The leave reads the room first, so its `delete` entry has `rev` 3, and the first Mac takes it.
        self.leave(self.second, self.room)
        body = decode(next(row['body'] for row in self.relay.rooms[self.room_id]['entries'].values() if row['id'] == own))
        self.assertEqual(json.loads(base64.b64decode(body['payload']))['rev'], 3)
        self.assertEqual(self.names(self.first), ['Ada'])

    def test_a_leave_reads_back_a_member_entry_without_an_answer_and_deletes_it(self):
        write = self.relay.write

        def lose_the_answer(*arguments, **options):
            write(*arguments, **options)
            raise TeamRelayError(0, 'The relay did not answer.')
        with mock.patch.object(self.relay, 'write', lose_the_answer), self.assertLogs('coconut.team', 'WARNING'):
            joined = self.join(self.third, self.invite(self.room)['link'], 'Edsger')
        # The relay has the `member` entry of the third Mac, and its store does not.
        self.assertEqual(joined['members'], [])
        self.assertEqual(self.names(self.first), ['Ada', 'Grace', 'Edsger'])
        self.leave(self.third, self.room)
        self.assertEqual(self.names(self.first), ['Ada', 'Grace'])

    def test_the_sync_writes_no_member_entry_between_the_delete_entry_and_the_leave(self):
        own = json.loads(self.second.team_store.room(self.room_id)['profile'])['entry']
        threads, errors = [], []

        def sync():
            try:
                self.second.team_sync_room(self.room_id)
            except Exception as error:
                errors.append(error)

        def start_the_sync():
            if self.relay.requests[-1][0] == 'remove_member' and not threads:
                threads.append(threading.Thread(target=sync))
                threads[0].start()
                # Without the lock of the room, the sync writes the `member` entry of this Mac again now.
                threads[0].join(.5)
        with mock.patch.object(team_sync, 'ANNOUNCE_SECONDS', 0):
            self.relay.before = start_the_sync
            self.leave(self.second, self.room)
            threads[0].join(5)
        self.relay.before = None
        self.assertEqual((errors, threads[0].is_alive()), ([], False))
        # The relay keeps the `delete` entry, which is not kept, and no later `member` entry of the Mac.
        row = self.relay.rooms[self.room_id]['entries'][own]
        payload = json.loads(base64.b64decode(decode(row['body'])['payload']))
        self.assertEqual((row['kept'], payload['kind']), (False, 'delete'))
        self.assertEqual(self.names(self.first), ['Ada'])

    def test_an_unexpected_error_in_one_room_does_not_stop_the_other_rooms(self):
        other = self.create(name='Other Team')
        self.join(self.second, self.invite(other)['link'])
        sync_room = self.second.team_sync_room

        def fail_in_one_room(room):
            if room == self.room_id:
                raise RuntimeError('A defect of this code.')
            sync_room(room)
        with mock.patch.object(self.second, 'team_sync_room', fail_in_one_room), \
                self.assertLogs('coconut.team_sync', 'ERROR') as logs:
            self.second.team_sync_pass()
        self.assertIn('RuntimeError: A defect of this code.', '\n'.join(logs.output))
        self.assertEqual(self.second._team_failures[self.room_id][0], 1)
        self.assertEqual(self.second._team_status[self.room_id]['error'],
                         'The sync of this room failed. The log of the service has the reason.')
        self.assertEqual(self.second._team_status[other['room']]['error'], None)
        self.assertNotIn(other['room'], self.second._team_failures)

    def test_an_entry_that_the_store_cannot_take_is_dropped_and_the_next_entries_go_in(self):
        bad, good = (self.write(self.first, {'kind': 'later', 'rev': 1}) for _ in range(2))
        accept = self.second.team_store.accept

        def fail_for_one(room, entry, *arguments):
            result = accept(room, entry, *arguments)
            if entry['id'] == bad:
                # The store wrote the entry before the error. The savepoint takes it back.
                raise ValueError('A defect of this code.')
            return result
        with mock.patch.object(self.second.team_store, 'accept', fail_for_one), \
                self.assertLogs('coconut.team_sync', 'WARNING') as logs:
            view = self.sync_view(self.second)
        self.assertEqual((view['seq'], view['entries'], view['error']), (4, 3, None))
        self.assertIsNone(self.second.team_store.entry(self.room_id, bad))
        self.assertEqual(self.second.team_store.entry(self.room_id, good)['seq'], 4)
        self.assertIn(f'dropped the entry {bad} at seq 3', '\n'.join(logs.output))

    def test_a_waiting_body_that_the_store_cannot_take_is_dropped(self):
        bad, good = (self.write(self.first, {'kind': 'later', 'rev': 1}, version=2) for _ in range(2))
        with self.assertLogs('coconut.team_sync', 'WARNING'):
            self.assertEqual(self.sync_view(self.second)['waiting'], 2)
        self.second.team_helper = FakeHelper(version=2)
        self.second._team_retried.clear()
        accept = self.second.team_store.accept

        def fail_for_one(room, entry, *arguments):
            if entry['id'] == bad:
                raise OverflowError('A defect of this code.')
            return accept(room, entry, *arguments)
        with mock.patch.object(self.second.team_store, 'accept', fail_for_one), self.assertLogs('coconut.team_sync', 'WARNING'):
            view = self.sync_view(self.second)
        self.assertEqual((view['waiting'], view['error']), (0, None))
        self.assertIsNone(self.second.team_store.entry(self.room_id, bad))
        self.assertEqual(self.second.team_store.entry(self.room_id, good)['kind'], 'later')

    def test_a_database_failure_drops_no_entry(self):
        entry = self.write(self.first, {'kind': 'later', 'rev': 1})
        seq = self.second.team_store.room(self.room_id)['seq']
        accept = self.second.team_store.accept

        def full_disk(room, row, *arguments):
            if row['id'] == entry:
                raise sqlite3.OperationalError('database or disk is full')
            return accept(room, row, *arguments)
        with mock.patch.object(self.second.team_store, 'accept', full_disk), self.assertLogs('coconut.team_sync', 'ERROR'):
            self.second.team_sync_pass()
        # The answer rolled back, so the next pass reads the entry again.
        self.assertEqual(self.second.team_store.room(self.room_id)['seq'], seq)
        self.assertIsNone(self.second.team_store.entry(self.room_id, entry))
        self.second.team_wake(retry=True)
        self.assertEqual(self.sync_view(self.second)['seq'], 3)
        self.assertEqual(self.second.team_store.entry(self.room_id, entry)['kind'], 'later')

    def test_no_relay_call_holds_the_service_lock(self):
        held = []

        def probe(lock):
            free = lock.acquire(timeout=0)
            if free:
                lock.release()
            held.append(not free)

        def check():
            # The lock of the service is reentrant, so only another thread can see that this thread holds it.
            for service in (self.first, self.second):
                thread = threading.Thread(target=probe, args=(service.lock,))
                thread.start()
                thread.join()
        self.relay.before = check
        self.write(self.first, {'kind': 'later', 'rev': 1})
        self.second.team_sync_pass()
        self.leave(self.second, self.room)
        self.assertTrue(held)
        self.assertNotIn(True, held)

    def test_the_thread_starts_with_the_service_and_stops_with_it(self):
        self.second._start_team()
        thread = self.second._team_thread
        self.assertEqual((thread.name, thread.daemon, thread.is_alive()), ('coconut-team-sync', True, True))
        # A sync now waits for the pass of the thread.
        self.write(self.first, {'kind': 'later', 'rev': 1})
        answer = self.second.team_request('POST', '/v1/team/sync', {})
        self.assertEqual((answer['complete'], answer['rooms'][0]['sync']['seq']), (True, 3))
        self.second.close()
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
