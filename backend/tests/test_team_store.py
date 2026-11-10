"""The store of the team log, and the "Rules for a Mac that reads an entry" of docs/team-log.md.

Each test gives TeamStore.accept an entry as the relay sends it, and the answer of the helper for its body. No test
needs the helper or the relay.
"""
import json
import sqlite3
import unittest

from coconut import team_store
from coconut.team_store import DROPPED, PENDING, REFUSED, SAME, SKIPPED, STORED, TeamStore

ROOM = 'a' * 64
OTHER_ROOM = 'b' * 64
ADMIN = '1' * 64
MEMBER = '2' * 64
OUTSIDER = '3' * 64
# A public agreement key as a payload carries it: the room key of the test vectors of the contract.
AGREEMENT = 'cHFyc3R1dnd4eXp7fH1-f4CBgoOEhYaHiImKi4yNjo8'


def entry_id(number: int) -> str:
    return f'{number:032x}'


class TeamStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.executescript(team_store.SCHEMA)
        self.store = TeamStore(self.db)
        for room in (ROOM, OTHER_ROOM):
            self.store.add_room(room=room, relay='https://relay.example', name='Example Team', member=MEMBER, admin=ADMIN,
                                state=team_store.ACTIVE, epoch=0, profile='{}', created_at='2026-11-10T12:00:00Z')
        self.seq = 0

    def entry(self, number: int, member: str = MEMBER, *, kept: bool = False) -> dict:
        """An entry as the relay sends it. Each write gets the next `seq`, and `first` is the first one of its ID."""
        self.seq += 1
        existing = self.store.entry(ROOM, entry_id(number))
        return dict(seq=self.seq, first=existing['first'] if existing else self.seq, id=entry_id(number), member=member,
                    at=1_800_000_000_000 + self.seq, kept=kept, body=b'sealed ' + str(self.seq).encode())

    def accept(self, entry: dict, opened: dict, *, room: str = ROOM, first_epoch: int = 0):
        return self.store.accept(self.store.room(room), entry, opened, first_epoch)

    def write(self, number: int, payload, member: str = MEMBER, **options):
        """Accept an entry whose body opens with this payload."""
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return self.accept(self.entry(number, member), {'epoch': 0, 'payload': data}, **options)

    def member(self, name: str = 'Ada', rev: int = 1, **fields) -> dict:
        return {'kind': 'member', 'rev': rev, 'at': '2026-11-10T12:00:00Z', 'name': name, 'avatar': '', 'color': '',
                'agreement': AGREEMENT, **fields}

    def names(self, room: str = ROOM) -> list[str]:
        return [member['name'] for member in self.store.members(room)]

    def waiting(self, room: str = ROOM) -> list[tuple]:
        return [(row['id'], row['seq'], row['reason'], bytes(row['body'])) for row in self.store.pending(room)]

    # The entry row

    def test_an_entry_row_keeps_the_fields_of_the_relay_and_the_payload_as_written(self):
        text = '{"rev": 1,  "kind": "member", "name": "Ada", "agreement": "%s", "at": "2026-11-10T12:00:00Z"}' % AGREEMENT
        entry = self.entry(1, kept=True)
        self.assertEqual(self.accept(entry, {'epoch': 0, 'payload': text.encode()}), (STORED, None))
        self.assertEqual(self.store.entry(ROOM, entry_id(1)), dict(
            id=entry_id(1), seq=1, first=1, member=MEMBER, kind='member', rev=1, at=entry['at'], kept=1, payload=text))

    def test_each_room_has_its_own_entries_and_members(self):
        self.write(1, self.member('Ada'))
        entry = dict(self.entry(1), seq=1, first=1)
        self.assertEqual(self.accept(entry, {'epoch': 0, 'payload': json.dumps(self.member('Grace')).encode()},
                                     room=OTHER_ROOM), (STORED, None))
        self.assertEqual((self.names(ROOM), self.names(OTHER_ROOM)), (['Ada'], ['Grace']))
        self.store.remove_room(ROOM)
        self.assertIsNone(self.store.room(ROOM))
        self.assertEqual((self.store.counts(ROOM), self.names(ROOM)), ((0, 0), []))
        self.assertEqual((self.store.counts(OTHER_ROOM), self.names(OTHER_ROOM)), ((1, 0), ['Grace']))

    # Rule 1: a later version

    def test_a_body_of_a_later_version_waits_with_its_bytes(self):
        entry = self.entry(1)
        self.assertEqual(self.accept(entry, {'error': 'unknown_version'}), (PENDING, 'version'))
        self.assertIsNone(self.store.entry(ROOM, entry_id(1)))
        self.assertEqual(self.waiting(), [(entry_id(1), 1, 'version', b'sealed 1')])
        self.assertEqual(self.store.counts(ROOM), (0, 1))

    def test_the_version_check_comes_before_each_other_rule(self):
        # The entry ID belongs to another member. The Mac cannot read a later form, so it still keeps the bytes.
        self.write(1, {'kind': 'message', 'rev': 1}, ADMIN)
        self.assertEqual(self.accept(self.entry(1, MEMBER), {'error': 'unknown_version'}), (PENDING, 'version'))
        self.assertEqual(self.store.entry(ROOM, entry_id(1))['member'], ADMIN)
        self.assertEqual(len(self.waiting()), 1)

    def test_a_waiting_body_goes_into_the_store_when_it_opens_after_an_update(self):
        entry = self.entry(1)
        self.accept(entry, {'error': 'unknown_version'})
        # After an update of Coconut, the helper opens the same bytes.
        waiting = self.store.pending(ROOM)[0]
        self.assertEqual(self.accept(waiting, {'epoch': 0, 'payload': json.dumps(self.member()).encode()}), (STORED, None))
        self.assertEqual((self.waiting(), self.names()), ([], ['Ada']))
        self.assertEqual(self.store.entry(ROOM, entry_id(1))['seq'], 1)

    def test_a_newer_write_of_an_entry_replaces_the_body_that_waits(self):
        self.accept(self.entry(1), {'error': 'unknown_version'})
        self.accept(self.entry(1), {'error': 'unknown_version'})
        self.assertEqual(self.waiting(), [(entry_id(1), 2, 'version', b'sealed 2')])
        # A write that opens is the newest write of the entry, so the earlier body is gone from the relay.
        self.assertEqual(self.write(1, {'kind': 'message', 'rev': 3}), (STORED, None))
        self.assertEqual(self.waiting(), [])

    def test_a_body_that_waits_is_older_than_the_entry_of_the_store(self):
        self.write(1, {'kind': 'message', 'rev': 2})
        # The relay gives an earlier write again. The store has a newer one, so nothing waits.
        old = dict(self.entry(1), seq=1)
        self.assertEqual(self.accept(old, {'error': 'unknown_version'}), (SAME, None))
        self.assertEqual(self.waiting(), [])

    # Rule 2: the seal and the signature

    def test_a_body_with_a_failed_seal_is_dropped(self):
        self.assertEqual(self.accept(self.entry(1), {'error': 'seal'}), (DROPPED, 'seal'))
        self.assertEqual(self.store.counts(ROOM), (0, 0))

    def test_a_body_that_another_key_signed_is_dropped(self):
        self.write(1, self.member('Ada'))
        self.assertEqual(self.accept(self.entry(1), {'error': 'signature'}), (DROPPED, 'signature'))
        self.assertEqual(self.accept(self.entry(2), {'error': 'malformed'}), (DROPPED, 'malformed'))
        self.assertEqual((self.store.entry(ROOM, entry_id(1))['rev'], self.store.counts(ROOM), self.names()), (1, (1, 0), ['Ada']))

    def test_a_payload_that_is_not_the_payload_of_an_entry_is_dropped(self):
        for payload in (b'not json', b'\xff\xfe', b'[1]', b'"text"', {'rev': 1}, {'kind': 'message'},
                        {'kind': 'message', 'rev': 0}, {'kind': 'message', 'rev': True}, {'kind': 'message', 'rev': 1.5},
                        {'kind': 'message', 'rev': '1'}, {'kind': '', 'rev': 1}, {'kind': 7, 'rev': 1},
                        {'kind': 'k' * 65, 'rev': 1}):
            with self.subTest(payload=payload):
                self.assertEqual(self.write(1, payload), (DROPPED, 'payload'))
        self.assertEqual(self.store.counts(ROOM), (0, 0))

    def test_a_rev_is_a_whole_number_up_to_2_to_the_53_minus_1(self):
        # A larger number does not fit the binding of SQLite, and the error stopped the sync of the room.
        for rev in (2 ** 53, 2 ** 63, 2 ** 64):
            with self.subTest(rev=rev):
                self.assertEqual(self.write(1, {'kind': 'message', 'rev': rev}), (DROPPED, 'payload'))
        self.assertEqual(self.write(1, {'kind': 'message', 'rev': 2 ** 53 - 1}), (STORED, None))
        self.assertEqual(self.store.entry(ROOM, entry_id(1))['rev'], 2 ** 53 - 1)

    def test_a_kind_that_is_not_valid_unicode_text_is_dropped(self):
        # The JSON is valid UTF-8, but json.loads gives a lone surrogate, which SQLite cannot store.
        self.assertEqual(self.write(1, b'{"kind":"\\ud800","rev":1}'), (DROPPED, 'payload'))
        self.assertEqual(self.store.counts(ROOM), (0, 0))

    # Rule 3: a replacement

    def test_the_member_of_an_entry_replaces_it_with_a_higher_rev(self):
        self.write(1, {'kind': 'message', 'rev': 1, 'text': 'first'})
        self.write(2, {'kind': 'message', 'rev': 1, 'text': 'other'})
        self.assertEqual(self.write(1, {'kind': 'message', 'rev': 2, 'text': 'second'}), (STORED, None))
        row = self.store.entry(ROOM, entry_id(1))
        # The entry keeps its place: `first` stays, and `seq` is the one of the new write.
        self.assertEqual((row['seq'], row['first'], row['rev'], json.loads(row['payload'])['text']), (3, 1, 2, 'second'))
        self.assertEqual(self.store.counts(ROOM), (2, 0))

    def test_a_write_without_a_higher_rev_does_not_replace_an_entry(self):
        self.write(1, {'kind': 'message', 'rev': 2, 'text': 'second'})
        for rev in (2, 1):
            with self.subTest(rev=rev):
                self.assertEqual(self.write(1, {'kind': 'message', 'rev': rev, 'text': 'old'}), (REFUSED, 'rev'))
        row = self.store.entry(ROOM, entry_id(1))
        self.assertEqual((row['seq'], row['rev'], json.loads(row['payload'])['text']), (1, 2, 'second'))

    def test_another_member_does_not_replace_an_entry(self):
        self.write(1, {'kind': 'message', 'rev': 1, 'text': 'mine'})
        self.assertEqual(self.write(1, {'kind': 'message', 'rev': 2, 'text': 'theirs'}, OUTSIDER), (REFUSED, 'member'))
        self.assertEqual(self.write(1, {'kind': 'delete', 'rev': 2}, ADMIN), (REFUSED, 'member'))
        row = self.store.entry(ROOM, entry_id(1))
        self.assertEqual((row['member'], row['kind'], json.loads(row['payload'])['text']), (MEMBER, 'message', 'mine'))

    def test_the_same_write_twice_changes_nothing(self):
        entry = self.entry(1)
        opened = {'epoch': 0, 'payload': json.dumps({'kind': 'message', 'rev': 1}).encode()}
        self.assertEqual(self.accept(entry, opened), (STORED, None))
        self.assertEqual(self.accept(entry, opened), (SAME, None))

    # Rule 4: the kinds of the admin

    def test_remove_and_key_count_only_from_the_admin(self):
        for number, kind in enumerate(('remove', 'key'), start=1):
            with self.subTest(kind=kind):
                self.assertEqual(self.write(number, {'kind': kind, 'rev': 1}, MEMBER), (REFUSED, 'admin'))
                self.assertIsNone(self.store.entry(ROOM, entry_id(number)))
                self.assertEqual(self.write(number, {'kind': kind, 'rev': 1}, ADMIN), (STORED, None))
                self.assertEqual(self.store.entry(ROOM, entry_id(number))['kind'], kind)

    def test_a_member_cannot_make_its_entry_a_kind_of_the_admin(self):
        self.write(1, {'kind': 'message', 'rev': 1})
        self.assertEqual(self.write(1, {'kind': 'key', 'rev': 2}), (REFUSED, 'admin'))
        self.assertEqual(self.store.entry(ROOM, entry_id(1))['kind'], 'message')

    # Rule 5: an unknown kind

    def test_an_entry_of_an_unknown_kind_keeps_its_payload_and_shows_nothing(self):
        payload = {'kind': 'reaction', 'rev': 1, 'at': '2026-11-10T12:00:00Z', 'emoji': '🥥', 'name': 'Not a member'}
        self.assertEqual(self.write(1, payload), (STORED, None))
        row = self.store.entry(ROOM, entry_id(1))
        self.assertEqual((row['kind'], json.loads(row['payload'])), ('reaction', payload))
        self.assertEqual(self.names(), [])

    # The room key of an epoch

    def test_a_body_of_a_later_epoch_waits_for_its_room_key(self):
        self.assertEqual(self.accept(self.entry(1), {'error': 'unknown_epoch', 'epoch': 1}), (PENDING, 'epoch'))
        self.assertEqual(self.waiting(), [(entry_id(1), 1, 'epoch', b'sealed 1')])

    def test_a_body_from_before_this_mac_joined_is_skipped(self):
        # The welcome gave this Mac the room key of epoch 2. No key of an earlier epoch comes later.
        self.assertEqual(self.accept(self.entry(1), {'error': 'unknown_epoch', 'epoch': 1}, first_epoch=2), (SKIPPED, 'epoch'))
        self.assertEqual(self.store.counts(ROOM), (0, 0))
        self.assertEqual(self.accept(self.entry(2), {'error': 'unknown_epoch', 'epoch': 3}, first_epoch=2), (PENDING, 'epoch'))

    # The kind `member`

    def test_a_member_entry_gives_the_member_its_name_avatar_color_and_agreement_key(self):
        self.write(1, self.member('Ada', avatar='coconut', color='green'), ADMIN)
        self.write(2, self.member('Grace'))
        self.assertEqual(self.store.members(ROOM), [
            dict(member=ADMIN, name='Ada', avatar='coconut', color='green', agreement=AGREEMENT, entry=entry_id(1)),
            dict(member=MEMBER, name='Grace', avatar='', color='', agreement=AGREEMENT, entry=entry_id(2))])
        # A replacement changes the member, and the member keeps its place in the list.
        self.write(1, self.member('Ada L.', rev=2, color='blue'), ADMIN)
        self.assertEqual([(member['name'], member['color']) for member in self.store.members(ROOM)],
                         [('Ada L.', 'blue'), ('Grace', '')])

    def test_a_member_entry_with_a_field_that_does_not_fit_stays_in_the_store_and_changes_no_member(self):
        self.write(1, self.member('Ada'))
        for number, fields in enumerate((dict(name='n' * 81), dict(name=''), dict(name=7), dict(avatar='a' * 81),
                                         dict(color='c' * 21), dict(agreement='short'), dict(agreement=AGREEMENT[:-1] + '9'),
                                         dict(agreement=None)), start=2):
            with self.subTest(fields=fields):
                self.assertEqual(self.write(1, dict(self.member('Ada'), rev=number, **fields)), (STORED, None))
                self.assertEqual(self.store.entry(ROOM, entry_id(1))['rev'], number)
                # The newest write of the one `member` entry does not fit, so the member has no name to show.
                self.assertEqual(self.names(), [])
        self.assertEqual(self.write(1, self.member('Ada again', rev=20)), (STORED, None))
        self.assertEqual(self.names(), ['Ada again'])

    def test_a_member_field_that_is_not_valid_unicode_text_stays_in_the_store_and_changes_no_member(self):
        self.write(1, self.member('Ada'))
        for rev, field in enumerate(('name', 'avatar', 'color'), start=2):
            with self.subTest(field=field):
                text = json.dumps(dict(self.member('Ada', rev=rev), **{field: 'x\ud800'}))
                self.assertIn('\\ud800', text)
                self.assertEqual(self.write(1, text.encode()), (STORED, None))
                # The store keeps the payload as its member wrote it: valid UTF-8 with the escape in it.
                self.assertEqual(self.store.entry(ROOM, entry_id(1))['payload'], text)
                self.assertEqual(self.names(), [])

    def test_the_longest_fields_of_a_member_fit(self):
        self.write(1, self.member('n' * 80, avatar='a' * 80, color='c' * 20))
        self.assertEqual(self.names(), ['n' * 80])

    def test_the_member_entry_with_the_highest_seq_counts(self):
        self.write(1, self.member('First entry'))
        self.write(2, self.member('Second entry'))
        self.assertEqual(self.names(), ['Second entry'])
        self.assertEqual(self.store.member(ROOM, MEMBER)['entry'], entry_id(2))

    # The kind `delete`

    def test_a_delete_over_a_member_entry_takes_the_member_out_of_the_list(self):
        self.write(1, self.member('Ada'), ADMIN)
        self.write(2, self.member('Grace'))
        self.assertEqual(self.write(2, {'kind': 'delete', 'rev': 2, 'at': '2026-11-10T12:00:00Z'}), (STORED, None))
        self.assertEqual(self.names(), ['Ada'])
        row = self.store.entry(ROOM, entry_id(2))
        self.assertEqual((row['kind'], row['rev'], row['first']), ('delete', 2, 2))
        # The deleted entry stays as a `delete` row, so an earlier write of it cannot come back.
        self.assertEqual(self.write(2, self.member('Grace of before', rev=1)), (REFUSED, 'rev'))
        self.assertEqual(self.names(), ['Ada'])

    def test_only_the_member_of_an_entry_deletes_it(self):
        self.write(1, self.member('Grace'))
        self.assertEqual(self.write(1, {'kind': 'delete', 'rev': 2}, ADMIN), (REFUSED, 'member'))
        self.assertEqual(self.names(), ['Grace'])

    def test_a_delete_of_an_entry_that_this_mac_never_had_is_kept(self):
        self.assertEqual(self.write(1, {'kind': 'delete', 'rev': 4}), (STORED, None))
        self.assertEqual(self.store.entry(ROOM, entry_id(1))['kind'], 'delete')


if __name__ == '__main__':
    unittest.main()
