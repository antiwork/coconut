"""Who types in the Team chat (coconut/team_typing.py, docs/team-log.md "Typing").

Ada's Mac (`first`) made the room, and Grace's Mac (`second`) joined it. The Macs share the FakeRelay and the FakeHelper
of team_fakes.py.
"""
import json
import logging
import time
import unittest
from unittest import mock

from coconut import team_typing
from coconut.team_store import TYPING_PERSON_SECONDS
from team_fakes import TeamMacs


class TypingMacs(TeamMacs):
    def setUp(self):
        super().setUp()
        self.room = self.create()['room']
        self.join(self.second, self.invite({'room': self.room})['link'])
        self.first.team_sync_pass()
        self.second.team_sync_pass()
        self.grace = self.view(self.second)['member']
        # Each test starts with the floor of the route open.
        self.now = 1000.0
        patcher = mock.patch.object(team_typing, 'clock', lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def view(self, service, sync: bool = True) -> dict:
        (room,) = self.rooms(service, sync=sync)
        return room

    def types(self, service=None) -> dict:
        return self.phone(service or self.second)('POST', f'/v1/team/rooms/{self.room}/typing', {})

    def typing(self, service=None, sync: bool = True) -> list:
        return self.view(service or self.first, sync)['typing']

    def own_typing(self, service) -> list[dict]:
        """The `typing` entries of this Mac in its store."""
        rows = service.db.execute("SELECT id, payload FROM team_entries WHERE room=? AND kind='typing'",
                                  (self.room,)).fetchall()
        return [dict(json.loads(payload), id=entry) for entry, payload in rows]

    def later(self, service, seconds: float) -> None:
        """Move the clock of the store of a Mac forward."""
        service.team_store.clock = lambda: time.time() + seconds


class PersonTests(TypingMacs, unittest.TestCase):
    def test_a_person_types_and_the_other_mac_shows_it_until_a_message(self):
        self.assertEqual(self.types(), {'room': self.room})
        (item,) = self.typing()
        self.assertEqual(item, {'member': self.grace, 'name': 'Grace', 'bot': None, 'bot_name': None,
                                'seconds': TYPING_PERSON_SECONDS})
        # The own person is never in the list of its Mac.
        self.assertEqual(self.typing(self.second), [])
        # A message of Grace ends it at once.
        self.phone(self.second)('POST', f'/v1/team/rooms/{self.room}/messages', {'text': 'Hello'})
        self.assertEqual(self.typing(), [])

    def test_on_false_and_the_time_end_it(self):
        self.types()
        self.assertEqual(len(self.typing()), 1)
        self.later(self.first, TYPING_PERSON_SECONDS + 0.5)
        self.assertEqual(self.typing(sync=False), [], 'The time ends it without an entry.')
        self.first.team_store.clock = time.time
        self.now += team_typing.FLOOR_SECONDS
        self.types()
        self.assertEqual(len(self.typing()), 1)
        with self.second._team_room_lock(self.room):
            self.second._team_typing_write(self.second.team_store.room(self.room), None, False)
        self.assertEqual(self.typing(), [])

    def test_an_old_entry_and_an_entry_that_does_not_fit_show_nothing(self):
        self.types()
        (entry,) = self.own_typing(self.second)
        self.relay.rooms[self.room]['entries'][entry['id']]['at'] -= 61_000
        self.assertEqual(self.typing(), [], 'An entry that the Mac reads late shows nothing.')
        for fields in ({'on': 'yes'}, {'on': True, 'bot': 'not an entry'}, {'on': True, 'bot': 'f' * 32}):
            with self.subTest(fields=fields), self.second._team_room_lock(self.room):
                self.second._team_write(self.second.team_store.room(self.room), entry['id'], False,
                                        dict(fields, kind='typing'))
                self.assertEqual(self.typing(), [])

    def test_the_floor_and_one_replaced_entry(self):
        writes = []
        write = self.relay.write
        self.relay.write = lambda *arguments, **options: (writes.append(arguments[3]), write(*arguments, **options))[1]
        for step in (0, 1, 3.9, 4, 5, 8.5):
            self.now = 1000.0 + step
            self.assertEqual(self.types(), {'room': self.room})
        self.assertEqual(len(writes), 3, 'One write at 0, 4 and 8.5 seconds.')
        (entry,) = self.own_typing(self.second)
        self.assertEqual(set(writes), {entry['id']})
        self.assertEqual((entry['rev'], entry['on']), (3, True))
        self.assertEqual([row['id'] for row in self.relay.rooms[self.room]['entries'].values()
                          if row['member'] == self.grace and not row['kept']], [entry['id']])

    def test_a_failed_write_answers_as_a_failed_send_and_does_not_hold_the_floor(self):
        self.relay.down = True
        self.assertEqual(self.refusal(self.types), (503, 'Coconut could not reach its server. Try again later.'))
        self.relay.down = False
        self.assertEqual(self.types(), {'room': self.room})
        self.assertEqual(len(self.typing()), 1)

    def test_the_event_tells_each_change(self):
        def changed(service, cursor):
            return service.updates(cursor, 0, collection_deltas=True).get('team_typing_changed')
        cursor = self.first._cursor()
        self.types()
        self.first.team_sync_pass()
        self.assertEqual(changed(self.first, cursor), [self.room])
        cursor = self.first._cursor()
        self.first.team_sync_pass()
        self.assertEqual(self.first._cursor(), cursor, 'A pass that reads nothing adds no event.')
        self.phone(self.second)('POST', f'/v1/team/rooms/{self.room}/messages', {'text': 'Hello'})
        self.first.team_sync_pass()
        self.assertEqual(changed(self.first, cursor), [self.room], 'The message ends the typing.')
        # The writer adds no event of typing for its own entry.
        self.assertFalse(self.second.db.execute("SELECT 1 FROM events WHERE type='team_typing.updated'").fetchone())

    def test_only_a_paired_device_calls_the_route(self):
        path = f'/v1/team/rooms/{self.room}/typing'
        self.assertEqual(self.refusal(self.second.team_request, 'POST', path, {}),
                         (403, 'Only the person can do this in the team. Use the Coconut app.'))
        # The handler itself refuses a call without a device too, also when a later change opens the route.
        self.assertEqual(self.refusal(self.second.team_typing, self.room, '')[0], 403)
        self.assertEqual(self.refusal(self.phone(self.second), 'POST', f'/v1/team/rooms/{"d" * 64}/typing', {}),
                         (404, 'This Mac is not in this team.'))
        self.assertEqual(self.own_typing(self.second), [])
        # The route keeps no answer for its `Idempotency-Key`: its many calls would push out the other answers.
        self.assertEqual(self.phone(self.second)('POST', path, {}, '6c2c8f2c-3f3c-4bf5-9f43-2b3f2a6e0c11'),
                         {'room': self.room})
        self.assertEqual(self.second._team_replies, {})
        self.assertEqual(len(self.own_typing(self.second)), 1)

    def test_a_restart_forgets_and_the_view_has_typing(self):
        self.types()
        self.assertEqual(len(self.typing()), 1)
        self.first.team_store.typing.clear()
        self.assertEqual(self.typing(sync=False), [])
        self.assertIn('typing', self.view(self.second))

    def test_no_log_line_has_a_name(self):
        with self.assertLogs('coconut', level='DEBUG') as logs:
            logging.getLogger('coconut').info('start')
            self.types()
            self.typing()
            self.relay.down = True
            self.now += team_typing.FLOOR_SECONDS
            self.refusal(self.types)
        for line in logs.output:
            self.assertNotIn('Grace', line)
            self.assertNotIn('Example Team', line)


if __name__ == '__main__':
    unittest.main()
