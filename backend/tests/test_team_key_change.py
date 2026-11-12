"""The key change after a member removal (coconut/team_epochs.py, docs/team-log.md, "Key change").

Ada (the first Mac) is the admin. Grace (the second Mac) and Lin (the third Mac) joined. Ada removes Grace, and the room
gets the room key of epoch 1, which Grace never gets. Each Mac is a Service with FakeRelay and FakeHelper
(team_fakes.py). A fake body opens only with the room key of its epoch, and a fake key box only for its own pair, as
with the real helper. scripts/test_team_log.py runs a key change through the real relay in Miniflare and the real
helper, and the tests of the format check the real boxes against the vectors of the contract.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import unittest
from unittest import mock
from uuid import uuid4

from coconut import team_epochs, team_keys
from coconut.team_epochs import key_entry
from coconut.team_relay import TeamRelayError
from team_fakes import DEVICE, RELAY, FakeHelper, decode
from test_team_only import TeamOnlyMacs
from test_team_remove import RemovalMacs


class KeyChangeMacs(RemovalMacs):
    def keys(self, service) -> dict:
        return team_keys.read(service.root, self.room)

    def epoch(self, service) -> int:
        return service.team_store.room(self.room)['epoch']

    def key_entry(self) -> dict | None:
        """The `key` entry of epoch 1 on the relay: its row, the epoch of its body and its payload."""
        row = self.relay.rooms[self.room]['entries'].get(key_entry(self.keys(self.first)['token'], self.room, 1))
        if row is None:
            return None
        body = decode(row['body'])
        return dict(row, epoch=body['epoch'], payload=json.loads(base64.b64decode(body['payload'])))

    def body_epochs(self, member: str) -> dict[str, int]:
        """The epoch of the body of each kept entry of a member on the relay, by entry ID."""
        return {entry: decode(row['body'])['epoch'] for entry, row in self.relay.rooms[self.room]['entries'].items()
                if row['member'] == member and row['kept']}

    def writes(self) -> int:
        return sum(request[0] == 'write' for request in self.relay.requests)

    def message(self, service, text: str) -> dict:
        return self.phone(service)('POST', f'/v1/team/rooms/{self.room}/messages', {'text': text})['message']

    def texts(self, service) -> list[str]:
        return [message['text'] for message in self.phone(service)('GET', f'/v1/team/rooms/{self.room}/messages', {})[
            'messages']]

    def write_as(self, service, fields: dict, *, epoch: int, entry: str | None = None, kept: bool = False) -> str:
        """Write an entry to the relay as a Mac would, without the store of that Mac."""
        keys = self.keys(service)
        entry = entry or secrets.token_hex(16)
        payload = json.dumps(dict(fields, at='2026-11-10T12:00:00Z')).encode()
        body = FakeHelper().seal(self.room, entry, epoch, keys['keys'][str(epoch)], keys['member_key'], payload)
        self.relay.write(RELAY, self.room, keys['token'], entry, kept, body)
        return entry


class AdminTests(KeyChangeMacs, unittest.TestCase):
    def test_a_removal_gives_the_room_a_key_that_the_removed_member_never_gets(self):
        self.remove(self.grace)
        # Ada saved the key of epoch 1 and uses it. The `key` entry is kept, in epoch 0, with a box for Lin only.
        keys = self.keys(self.first)
        self.assertEqual((self.epoch(self.first), sorted(keys['keys'])), (1, ['0', '1']))
        self.assertNotEqual(keys['keys']['0'], keys['keys']['1'])
        change = self.key_entry()
        self.assertEqual((change['member'], change['kept'], change['epoch']), (self.ada, True, 0))
        payload = change['payload']
        self.assertEqual((payload['kind'], payload['rev'], payload['epoch'], sorted(payload['boxes'])),
                         ('key', 1, 1, [self.lin]))
        self.assertEqual(payload['agreement'], self.first.team_store.member(self.room, self.ada)['agreement'])
        # Each next write of Ada has epoch 1, and an invite carries the new key.
        sent = self.message(self.first, 'After the change')
        self.assertEqual(decode(self.relay.rooms[self.room]['entries'][sent['id']]['body'])['epoch'], 1)
        # Grace's key file opens no entry of epoch 1.
        grace_keys = self.keys(self.second)
        (opened,) = FakeHelper().open(self.room, grace_keys['keys'], [
            dict(self.relay.rooms[self.room]['entries'][sent['id']])])
        self.assertEqual(opened, {'error': 'unknown_epoch', 'epoch': 1})
        self.assertNotIn('1', grace_keys['keys'])

    def test_a_change_is_due_on_the_admin_only_and_one_change_covers_each_removal_before_it(self):
        self.sync()
        self.assertFalse(self.first.team_store.key_change_due(self.room))
        self.remove(self.grace)
        self.assertFalse(self.first.team_store.key_change_due(self.room))
        writes = self.writes()
        # A pass with nothing due writes no `key` entry.
        self.sync(self.first)
        self.assertEqual(self.key_entry()['payload']['rev'], 1)
        # Lin's Mac is no admin, so it makes no change, also when a removal is newer than its newest `key` entry.
        self.sync(self.third)
        self.assertEqual(self.epoch(self.third), 1)
        self.assertEqual(sorted(self.keys(self.third)['keys']), ['0', '1'])
        self.assertGreater(self.writes(), writes)

    def test_a_second_removal_makes_a_second_change(self):
        self.remove(self.grace)
        self.sync()
        self.remove(self.lin)
        self.assertEqual((self.epoch(self.first), sorted(self.keys(self.first)['keys'])), (2, ['0', '1', '2']))
        entry = key_entry(self.keys(self.first)['token'], self.room, 2)
        body = decode(self.relay.rooms[self.room]['entries'][entry]['body'])
        self.assertEqual((body['epoch'], json.loads(base64.b64decode(body['payload']))['boxes']), (1, {}))

    def test_each_step_of_the_admin_can_stop_and_the_next_pass_finishes_with_one_key_and_one_entry(self):
        write = self.relay.write
        calls = {'key': 0}

        def failing_key_write(relay, room, token, entry, kept, body, tries=1):
            if decode(body)['epoch'] == 0 and json.loads(base64.b64decode(decode(body)['payload']))['kind'] == 'key':
                calls['key'] += 1
                raise TeamRelayError(503, 'The relay answered 503.')
            return write(relay, room, token, entry, kept, body, tries)
        # Step 3 fails after step 1: the removal is complete, and the key of epoch 1 waits in the key file.
        with mock.patch.object(self.relay, 'write', failing_key_write), self.assertLogs('coconut.team', 'WARNING'):
            room = self.remove(self.grace)
        self.assertEqual([member['name'] for member in room['members']], ['Ada', 'Lin'])
        self.assertEqual(calls['key'], 1)
        saved = self.keys(self.first)['keys']['1']
        self.assertEqual((self.epoch(self.first), self.key_entry()), (0, None))
        # The next pass uses the saved key: no second key, one entry.
        self.sync(self.first)
        self.assertEqual((self.epoch(self.first), self.keys(self.first)['keys']['1']), (1, saved))
        self.assertEqual(self.key_entry()['payload']['rev'], 1)
        # Lin gets the same key.
        self.sync(self.third)
        self.assertEqual(self.keys(self.third)['keys']['1'], saved)

    def test_a_key_entry_that_the_relay_took_without_an_answer_is_not_written_again(self):
        write = self.relay.write

        def lost(relay, room, token, entry, kept, body, tries=1):
            position = write(relay, room, token, entry, kept, body, tries)
            if json.loads(base64.b64decode(decode(body)['payload']))['kind'] == 'key':
                raise TeamRelayError(0, 'The relay did not answer.')
            return position
        with mock.patch.object(self.relay, 'write', lost), self.assertLogs('coconut.team', 'WARNING'):
            self.remove(self.grace)
        self.assertEqual(self.epoch(self.first), 0)
        # The next pass reads the entry, and moves the room. It writes no second entry.
        self.sync(self.first)
        self.assertEqual((self.epoch(self.first), self.key_entry()['payload']['rev']), (1, 1))

    def test_a_stop_between_the_entry_and_the_move_moves_the_room_at_the_next_pass(self):
        with mock.patch.object(self.first, '_team_take_keys'):
            self.remove(self.grace)
        self.assertEqual((self.epoch(self.first), self.key_entry()['payload']['rev']), (0, 1))
        writes = self.writes()
        self.first.team_sync_room(self.room)
        self.assertEqual(self.epoch(self.first), 1)
        # The pass writes the kept entries of Ada again in epoch 1, and no `key` entry.
        self.assertEqual(self.key_entry()['payload']['rev'], 1)
        self.assertEqual(set(self.body_epochs(self.ada).values()), {0, 1})
        self.assertGreaterEqual(self.writes(), writes)

    def test_a_repeat_of_the_removal_makes_no_second_change(self):
        key = str(uuid4())
        self.remove(self.grace, key=key)
        saved = self.keys(self.first)['keys']
        writes = self.writes()
        self.remove(self.grace)
        self.assertEqual((self.keys(self.first)['keys'], self.epoch(self.first)), (saved, 1))
        self.assertEqual(self.writes(), writes)


class ReaderTests(KeyChangeMacs, unittest.TestCase):
    def test_each_other_mac_takes_the_key_and_the_two_macs_still_read_each_other(self):
        self.turn_on(*self.macs)
        self.remove(self.grace)
        self.sync(self.first, self.third)
        self.assertEqual((self.epoch(self.third), self.keys(self.third)['keys']['1']),
                         (1, self.keys(self.first)['keys']['1']))
        self.message(self.third, 'Hello from Lin')
        self.message(self.first, 'Hello from Ada')
        self.send(self.first, self.lin, 'Only for Lin')
        self.sync(self.first, self.third)
        self.assertEqual(self.texts(self.first)[:2], ['Hello from Ada', 'Hello from Lin'])
        self.assertEqual(self.texts(self.third)[:2], ['Hello from Ada', 'Hello from Lin'])
        self.assertEqual(self.chat(self.third, self.ada), [('Only for Lin', False)])
        self.assertEqual(self.rooms(self.third, sync=False)[0]['sync']['waiting'], 0)

    def test_a_mac_that_has_the_key_already_opens_no_box(self):
        self.remove(self.grace)
        team_keys.add_key(self.third.root, self.room, 1, self.keys(self.first)['keys']['1'])
        self.sync(self.third)
        self.assertEqual(self.epoch(self.third), 1)
        self.assertNotIn('open_key', self.third.team_helper.calls)

    def test_a_mac_without_a_box_keeps_its_epoch_with_one_log_line_and_its_entries_wait(self):
        self.remove(self.grace)
        # Ada writes the `key` entry again without the box of Lin, as an admin that missed her would.
        entry = key_entry(self.keys(self.first)['token'], self.room, 1)
        change = self.key_entry()['payload']
        self.write_as(self.first, dict(change, rev=2, boxes={}), epoch=0, entry=entry, kept=True)
        self.message(self.first, 'In epoch 1')
        with self.assertLogs('coconut.team_epochs', 'WARNING') as logs:
            self.sync(self.third)
        self.assertEqual(len([line for line in logs.output if 'gives this Mac no room key' in line]), 1)
        self.assertIn('(no box)', logs.output[0])
        self.assertEqual((self.epoch(self.third), sorted(self.keys(self.third)['keys'])), (0, ['0']))
        self.assertGreater(self.rooms(self.third, sync=False)[0]['sync']['waiting'], 0)
        # The same write gives no second log line.
        with self.assertNoLogs('coconut.team_epochs', 'WARNING'):
            self.sync(self.third)

    def test_a_box_that_does_not_open_keeps_the_epoch(self):
        self.remove(self.grace)
        entry = key_entry(self.keys(self.first)['token'], self.room, 1)
        change = self.key_entry()['payload']
        self.write_as(self.first, dict(change, rev=2, boxes={self.lin: change['boxes'][self.lin][:-4]}), epoch=0,
                      entry=entry, kept=True)
        with self.assertLogs('coconut.team_epochs', 'WARNING') as logs:
            self.sync(self.third)
        self.assertIn('(seal)', '\n'.join(logs.output))
        self.assertEqual(self.epoch(self.third), 0)
        # A new write of the entry with a good box gets its own try.
        self.write_as(self.first, dict(change, rev=3), epoch=0, entry=entry, kept=True)
        self.sync(self.third)
        self.assertEqual(self.epoch(self.third), 1)

    def test_a_key_entry_of_another_member_or_of_a_wrong_epoch_gives_no_key(self):
        self.remove(self.grace)
        change = self.key_entry()['payload']
        lin = self.keys(self.third)
        # Lin writes a `key` entry of epoch 1 with a box for herself: rule 4 refuses it.
        forged = self.write_as(self.third, dict(change, epoch=1, rev=1), epoch=0, kept=True)
        # Ada writes one in epoch 0 that names epoch 3.
        wrong = self.write_as(self.first, dict(change, epoch=3, rev=1), epoch=0, kept=True)
        with mock.patch.object(self.third, '_team_take_keys'), self.assertLogs('coconut.team_sync', 'WARNING') as logs:
            self.sync(self.third)
        output = '\n'.join(logs.output)
        self.assertIn(f'refused the entry {forged}', output)
        self.assertIn(f'refused the entry {wrong}', output)
        self.assertIn('(admin)', output)
        self.assertIn('(key)', output)
        self.assertEqual(self.third.team_store.key_changes(self.room, 0)[0]['entry'],
                         key_entry(self.keys(self.first)['token'], self.room, 1))
        self.assertEqual(lin['keys'], self.keys(self.third)['keys'])

    def test_an_entry_of_the_old_epoch_after_the_change_stays_valid(self):
        self.remove(self.grace)
        # Lin did not read the `key` entry yet, so she writes in epoch 0.
        self.message(self.third, 'Written in epoch 0')
        self.sync(self.first)
        self.assertEqual(self.texts(self.first)[0], 'Written in epoch 0')

    def test_entries_of_the_new_epoch_before_and_after_the_key_entry_open_in_one_answer(self):
        self.remove(self.grace)
        self.message(self.first, 'After the key entry')
        # A new write of the `key` entry comes after an entry of epoch 1 in the log.
        entry = key_entry(self.keys(self.first)['token'], self.room, 1)
        self.write_as(self.first, dict(self.key_entry()['payload'], rev=2), epoch=0, entry=entry, kept=True)
        self.message(self.first, 'After the second write')
        self.third.team_sync_pass()
        self.assertEqual(self.texts(self.third)[:2], ['After the second write', 'After the key entry'])
        self.assertEqual((self.epoch(self.third), self.rooms(self.third, sync=False)[0]['sync']['waiting']), (1, 0))

    def test_entries_of_the_new_epoch_open_across_two_answers(self):
        self.remove(self.grace)
        for number in range(4):
            self.message(self.first, f'Message {number}')
        self.relay.page = 2
        self.third.team_sync_pass()
        self.assertEqual(self.texts(self.third)[:4], [f'Message {number}' for number in (3, 2, 1, 0)])
        self.assertEqual(self.rooms(self.third, sync=False)[0]['sync']['waiting'], 0)

    def test_the_waiting_bodies_open_after_a_new_key_also_after_the_first_pass(self):
        # The service tried its waiting bodies at its start already.
        self.sync()
        self.assertIn(self.room, self.third._team_retried)
        self.remove(self.grace)
        self.message(self.first, 'In epoch 1')
        with mock.patch.object(self.third, '_team_take_keys'):
            self.third.team_sync_pass()
        self.assertEqual(self.rooms(self.third, sync=False)[0]['sync']['waiting'], 1)
        self.third.team_sync_pass()
        self.assertEqual(self.rooms(self.third, sync=False)[0]['sync']['waiting'], 0)
        self.assertEqual(self.texts(self.third)[0], 'In epoch 1')

    def test_a_stop_after_the_save_of_a_key_moves_the_room_at_the_next_pass(self):
        self.remove(self.grace)
        with mock.patch.object(self.third, '_team_take_keys'):
            self.third.team_sync_pass()
        with mock.patch.object(self.third.team_store, 'update_room', side_effect=RuntimeError('stop')):
            with self.assertRaises(RuntimeError):
                self.third._team_take_keys(self.room)
        self.assertEqual((self.epoch(self.third), sorted(self.keys(self.third)['keys'])), (0, ['0', '1']))
        calls = len(self.third.team_helper.calls)
        self.third.team_sync_pass()
        self.assertEqual(self.epoch(self.third), 1)
        self.assertNotIn('open_key', self.third.team_helper.calls[calls:])


class MissedMemberTests(KeyChangeMacs, unittest.TestCase):
    def test_a_member_that_joined_at_the_time_of_the_change_gets_a_box_in_a_new_write(self):
        noor = self.mac('fourth')
        link = self.invite({'room': self.room})['link']
        self.join(noor, link, 'Noor')
        noor_id = noor.team_store.room(self.room)['member']
        # Ada's Mac did not read the `member` entry of Noor before the change.
        self.remove(self.grace)
        self.assertNotIn(noor_id, self.key_entry()['payload']['boxes'])
        self.sync(self.first)
        change = self.key_entry()
        self.assertEqual((change['payload']['rev'], change['epoch']), (2, 0))
        self.assertEqual(sorted(change['payload']['boxes']), sorted([self.lin, noor_id]))
        # Noor reads the log from its start: the `member` entry of Ada is in epoch 1 already, and the key entry gives
        # Noor the agreement key of Ada.
        noor.team_sync_pass()
        self.assertEqual(noor.team_store.room(self.room)['epoch'], 1)
        self.assertEqual(self.names(noor, sync=False), ['Ada', 'Lin', 'Noor'])
        self.assertEqual(noor.team_store.counts(self.room)[1], 0)
        # A next pass of Ada writes the entry no more.
        self.sync(self.first)
        self.assertEqual(self.key_entry()['payload']['rev'], 2)

    def test_a_member_that_the_helper_cannot_seal_a_box_for_causes_no_loop_of_writes(self):
        noor = self.mac('fourth')
        self.join(noor, self.invite({'room': self.room})['link'], 'Noor')
        noor_id = noor.team_store.room(self.room)['member']
        self.remove(self.grace)
        seal = self.first.team_helper.seal_key

        def without_noor(room, entry, epoch, key, agreement_key, member, members):
            return [{'error': 'malformed'} if other['member'] == noor_id else result
                    for other, result in zip(members, seal(room, entry, epoch, key, agreement_key, member, members))]
        with mock.patch.object(self.first.team_helper, 'seal_key', without_noor):
            with self.assertLogs('coconut.team_epochs', 'WARNING'):
                self.sync(self.first)
            self.sync(self.first)
        change = self.key_entry()['payload']
        self.assertEqual((change['rev'], sorted(change['boxes'])), (2, [self.lin]))

    def test_a_member_that_joins_after_the_change_needs_no_new_write(self):
        self.remove(self.grace)
        noor = self.mac('fourth')
        self.join(noor, self.invite({'room': self.room})['link'], 'Noor')
        self.sync(self.first)
        self.assertEqual(self.key_entry()['payload']['rev'], 1)


class WriteAgainTests(KeyChangeMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        bot = self.third.mutate('POST', '/v1/bots', {'name': 'Helper'}, str(uuid4()), 'phone')
        self.share_page(self.third, self.room, bot)
        self.sync()

    def test_each_mac_writes_its_kept_entries_again_once(self):
        self.assertEqual(set(self.body_epochs(self.lin).values()), {0})
        self.assertEqual(len(self.body_epochs(self.lin)), 3)
        self.remove(self.grace)
        self.sync()
        # Lin's `member`, `bot` and `page` entries, and Ada's `member` entry, are in epoch 1 with the next `rev`.
        self.assertEqual(set(self.body_epochs(self.lin).values()), {1})
        for entry in self.body_epochs(self.lin):
            self.assertEqual(self.third.team_store.entry(self.room, entry)['rev'], 2)
        epochs = self.body_epochs(self.ada)
        own = self.first.team_store.member(self.room, self.ada)['entry']
        self.assertEqual(epochs[own], 1)
        # The `remove` and `key` entries of Ada stay in epoch 0.
        self.assertEqual(sorted(epochs.values()), [0, 0, 1])
        writes = self.writes()
        self.sync()
        self.assertEqual(self.writes(), writes)

    def team_page(self, service) -> dict:
        (page,) = [page for page in service.documents()['documents'] if 'team' in page]
        return page

    def events(self, service, cursor: int) -> list[str]:
        return [row[0] for row in service.db.execute('SELECT type FROM events WHERE seq>?', (cursor,))]

    def own_page(self) -> tuple:
        return self.third.db.execute('SELECT version, created_at, updated_at FROM team_pages WHERE room=? AND member=?',
                                     (self.room, self.lin)).fetchone()

    def test_a_write_again_changes_nothing_that_a_person_sees(self):
        before = self.team_page(self.first)
        bots = self.view(self.first)['bots']
        own = self.own_page()
        self.remove(self.grace)
        self.sync(self.first)
        # Lin takes the key first, then writes her kept entries again in a pass of its own.
        with mock.patch.object(self.third, '_team_write_again'):
            self.third.team_sync_pass()
        lin_cursor, ada_cursor = self.third._cursor(), self.first._cursor()
        self.third.team_sync_pass()
        self.assertEqual(set(self.body_epochs(self.lin).values()), {1})
        # The own rows of Lin keep their times, and Lin's phone gets no event.
        self.assertEqual(self.own_page(), own)
        self.assertEqual(self.events(self.third, lin_cursor), [])
        # Ada's Mac reads the writes again: the page keeps its times, only its version moves, and no event comes.
        self.first.team_sync_pass()
        after = self.team_page(self.first)
        self.assertEqual((after['created_at'], after['updated_at'], after['version']),
                         (before['created_at'], before['updated_at'], before['version'] + 1))
        self.assertEqual(self.view(self.first)['bots'], bots)
        self.assertEqual(self.names_of(self.first), ['Ada', 'Lin'])
        self.assertEqual(self.events(self.first, ada_cursor), [])
        # A real change of the page after it gets a new time and an event.
        bot = self.third.team_store.shares(self.room)
        (bot_id,) = bot
        self.third.publish_page(bot_id, dict(key='weekly-report', title='Weekly report', format='markdown',
                                             content='4 tasks are open.'))
        self.sync(self.third, self.first)
        changed = self.team_page(self.first)
        self.assertGreater(changed['updated_at'], before['updated_at'])
        self.assertEqual(changed['created_at'], before['created_at'])
        self.assertIn('document.updated', self.events(self.first, ada_cursor))
        # A Mac that joins later takes the page as it comes.
        noor = self.mac('fourth')
        self.join(noor, self.invite({'room': self.room})['link'], 'Noor')
        noor.team_sync_pass()
        self.assertEqual(self.team_page(noor)['title'], 'Weekly report')
        self.assertEqual(self.first.document(changed['id'])['content'], '4 tasks are open.')

    def test_a_stop_in_the_middle_writes_the_rest_at_the_next_pass(self):
        self.remove(self.grace)
        self.sync(self.first)
        write = self.relay.write
        done = []

        def once(relay, room, token, entry, kept, body, tries=1):
            if done:
                raise TeamRelayError(503, 'The relay answered 503.')
            done.append(entry)
            return write(relay, room, token, entry, kept, body, tries)
        with mock.patch.object(self.relay, 'write', once), self.assertLogs('coconut.team_sync', 'WARNING'):
            self.third.team_sync_pass()
        self.assertEqual(sorted(self.body_epochs(self.lin).values()), [0, 0, 1])
        self.third.team_wake(retry=True)
        self.third.team_sync_pass()
        self.assertEqual(set(self.body_epochs(self.lin).values()), {1})
        self.assertEqual({self.third.team_store.entry(self.room, entry)['rev'] for entry in self.body_epochs(self.lin)},
                         {2})

    def test_the_writes_again_keep_to_a_limit_in_each_minute(self):
        self.remove(self.grace)
        self.sync(self.first)
        with mock.patch.object(team_epochs, 'WRITE_AGAIN_LIMIT', 1):
            self.third.team_sync_pass()
            self.assertEqual(sorted(self.body_epochs(self.lin).values()), [0, 0, 1])
            self.assertIn(self.room, self.third._team_busy)
            self.third.team_sync_pass()
            self.assertEqual(sorted(self.body_epochs(self.lin).values()), [0, 0, 1])
            # A new minute writes the next one. A busy room reads at each pass, also with an open socket.
            self.third._team_written_again[self.room] = (0.0, 1)
            with mock.patch.object(self.third, 'team_socket_open', return_value=True):
                self.third._team_read_at[self.room] = float('inf')
                self.third.team_sync_pass()
            self.assertEqual(sorted(self.body_epochs(self.lin).values()), [0, 1, 1])

    def test_a_mac_that_joins_after_the_change_reads_the_kept_entries_and_the_new_messages(self):
        self.remove(self.grace)
        self.sync()
        self.message(self.third, 'Hello Noor')
        noor = self.mac('fourth')
        room = self.join(noor, self.invite({'room': self.room})['link'], 'Noor')
        self.assertEqual(room['epoch'], 1)
        self.assertEqual(sorted(team_keys.read(noor.root, self.room)['keys']), ['1'])
        noor.team_sync_pass()
        self.assertEqual(self.names(noor, sync=False), ['Ada', 'Lin', 'Noor'])
        self.assertEqual([page['title'] for page in noor.documents()['documents'] if 'team' in page], ['Weekly report'])
        self.assertEqual(self.texts(noor), ['Hello Noor'])


class TeamOnlyKeyChangeTests(TeamOnlyMacs, unittest.TestCase):
    def test_a_mac_for_a_team_only_takes_the_new_key_and_its_bot_stays(self):
        room = self.team()
        bot = self.declare()
        self.share(room, bot)
        self.save()
        grace = self.join(self.second, self.invite({'room': room})['link'])['member']
        self.sync()
        self.second.team_sync_pass()
        self.first.team_request('DELETE', f'/v1/team/rooms/{room}/members/{grace}', {}, '', DEVICE)
        self.sync()
        self.sync()
        self.assertEqual(self.only.team_store.room(room)['epoch'], 1)
        own = self.only.team_store.room(room)['member']
        epochs = {decode(row['body'])['epoch'] for row in self.relay.rooms[room]['entries'].values()
                  if row['member'] == own and row['kept']}
        self.assertEqual(epochs, {1})
        # The page is the same. Its version for the phone is the `rev` of the entry, so it shows the new write.
        self.assertEqual(self.library(), {'pages': [('Weekly report', 2)], 'bots': ['Reporter']})


class LogTests(KeyChangeMacs, unittest.TestCase):
    def test_no_log_line_has_a_key_or_a_name(self):
        self.turn_on(*self.macs)
        with self.assertLogs(level=logging.DEBUG) as logs:
            self.remove(self.grace)
            entry = key_entry(self.keys(self.first)['token'], self.room, 1)
            # Lin finds no box in a new write of the `key` entry. Then Ada writes it again with her box.
            change = dict(self.key_entry()['payload'], rev=2, boxes={})
            self.write_as(self.first, change, epoch=0, entry=entry, kept=True)
            self.third.team_sync_pass()
            self.sync()
        self.assertEqual(self.epoch(self.third), 1)
        output = '\n'.join(logs.output)
        self.assertIn('gives this Mac no room key', output)
        keys = [key for service in self.macs for key in self.keys(service)['keys'].values()]
        for value in [*keys, *(key[:12] for key in keys), self.keys(self.first)['agreement_key'], 'Grace', 'Lin',
                      'Example Team']:
            self.assertNotIn(value, output)
        self.assertIn('wrote the key entry', output)


if __name__ == '__main__':
    unittest.main()
