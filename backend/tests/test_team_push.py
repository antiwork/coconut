"""Push notifications for the messages of a team chat (coconut/push.py, record_team, and TeamStore._note_fresh).

Grace on the first Mac is the admin and writes. Ada on the second Mac joins and has the paired phone. The Macs share
FakeRelay and FakeHelper (team_fakes.py), and the phone gets its notifications through the fake transports of the push
tests. scripts/test_team_log.py runs the same path through the real relay in Miniflare.
"""
from __future__ import annotations

import base64
import json
import logging
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

from coconut import team_store
from coconut.push import TEAM_BODY, PushDelivery
from coconut.push_relay import group_text
from team_fakes import DEVICE, TeamMacs
from test_push import RecordingTransport, pair_phone
from test_push_relay import EncryptedTransport
from test_team_turns import TeamTurnMacs, reply

# The columns of push_items. A store from before team notifications gets `team` as the last column.
ITEM_COLUMNS = ['device_id', 'event_key', 'kind', 'bot_id', 'task_id', 'decision_id', 'message_id', 'turn_id', 'urgent',
                'created', 'status', 'delivery_id', 'reason', 'team']
ROUTE = {'route_id': 'a' * 64, 'send_token': 'b' * 64, 'public_key': base64.b64encode(b'x' * 32).decode()}


def register(service, device, token='c' * 64, route=None, **preferences):
    """Register a phone as the app does. Without `preferences`, the phone is an app before the levels."""
    data = dict(token=token, environment='development', enabled=True)
    if preferences:
        data['preferences'] = dict({'level': 'grouped', 'messages': False}, **preferences)
    if route:
        data['relay'] = route
    return service.push.registration(device, data)


def flush(service):
    """Make each waiting item and notification due now, and deliver all of them."""
    with service.transaction():
        service.db.execute('UPDATE push_items SET created=created-900')
        service.db.execute('UPDATE push_outbox SET not_before=0,created=created-900,sent=sent-900')
    while service.push.deliver_one():
        pass


class TeamPushMacs(TeamMacs):
    def setUp(self):
        super().setUp()
        self.room = self.create(member_name='Grace')['room']
        self.join(self.second, self.invite({'room': self.room})['link'], name='Ada')
        self.first.team_sync_pass()
        self.second.team_sync_pass()
        self.phone_id = pair_phone(self.second)
        self.transport = RecordingTransport()
        self.second.push.transport = self.transport
        register(self.second, self.phone_id, level='grouped')

    def send(self, text, service=None, **fields):
        return self.phone(service or self.first)('POST', f'/v1/team/rooms/{self.room}/messages',
                                                 dict(fields, text=text))['message']

    def items(self, service=None):
        return (service or self.second).db.execute(
            'SELECT device_id,kind,bot_id,task_id,turn_id,decision_id,message_id,team,urgent,status,reason '
            'FROM push_items ORDER BY created,rowid').fetchall()

    def payloads(self):
        return [call[2] for call in self.transport.calls]

    def seal(self):
        """Give the phone a relay route, so its notifications carry the text."""
        self.relay = EncryptedTransport()
        self.second.push.relay = self.relay
        register(self.second, self.phone_id, route=ROUTE)


class WhichMessageNotifies(TeamPushMacs, unittest.TestCase):
    def test_a_fresh_message_of_another_member_makes_one_item_for_each_phone(self):
        other = pair_phone(self.second)
        register(self.second, other, token='d' * 64, level='every')
        sent = self.send('Hello Ada')
        self.second.team_sync_pass()
        self.assertEqual(sorted(self.items()), sorted(
            (phone, 'team', '', '', '', None, sent['id'], self.room, 0, 'waiting', None) for phone in (self.phone_id, other)))
        # A pass that reads nothing new, and the read back of the same write, add nothing.
        self.second.team_sync_pass()
        self.assertEqual(len(self.items()), 2)
        flush(self.second)
        self.assertEqual(sorted(payload['coconut']['device_id'] for payload in self.payloads()), sorted([self.phone_id, other]))
        flush(self.second)
        self.assertEqual(len(self.transport.calls), 2)

    def test_an_own_message_an_edit_a_removal_and_an_old_message_notify_nothing(self):
        self.send('Mine', service=self.second)
        sent = self.send('First')
        self.second.team_sync_pass()
        self.assertEqual([item[6] for item in self.items()], [sent['id']])
        # The edit and the removal are later writes of the same entry.
        self.phone(self.first)('PATCH', f'/v1/team/rooms/{self.room}/messages/{sent["id"]}', {'text': 'First, edited'})
        self.second.team_sync_pass()
        self.phone(self.first)('DELETE', f'/v1/team/rooms/{self.room}/messages/{sent["id"]}', {})
        self.second.team_sync_pass()
        self.assertEqual(len(self.items()), 1)
        # A message that this Mac reads one hour after the relay took it is history.
        self.send('Late')
        self.second.team_store.clock = lambda: time.time() + team_store.TRIGGER_SECONDS
        self.second.team_sync_pass()
        self.assertEqual(len(self.items()), 1)

    def test_a_message_that_does_not_show_notifies_nothing(self):
        room = self.first.team_store.room(self.room)
        # A message of a bot that has no `bot` entry does not show.
        with self.first._team_room_lock(self.room):
            self.first._team_write(room, 'a' * 32, False, {'kind': 'message', 'text': 'From a bot', 'bot': '9' * 32})
        # A message that does not fit: an empty text.
        with self.first._team_room_lock(self.room):
            self.first._team_write(room, 'b' * 32, False, {'kind': 'message', 'text': ''})
        self.second.team_sync_pass()
        self.assertEqual(self.items(), [])

    def test_the_history_that_a_mac_reads_after_a_join_notifies_nothing(self):
        self.send('Before the third Mac')
        third_phone = pair_phone(self.third)
        self.third.push.transport = RecordingTransport()
        register(self.third, third_phone, level='every')
        self.join(self.third, self.invite({'room': self.room})['link'], name='Lin')
        self.third.team_sync_pass()
        self.assertEqual(self.items(self.third), [])
        after = self.send('After the join')
        self.third.team_sync_pass()
        self.assertEqual([item[6] for item in self.items(self.third)], [after['id']])

    def test_the_level_and_the_preferences_of_each_phone_decide(self):
        phones = {}
        for name, preferences in (('every', {'level': 'every'}), ('urgent', {'level': 'urgent_only', 'messages': True}),
                                  ('old app', {})):
            phones[name] = pair_phone(self.second)
            register(self.second, phones[name], token=str(len(phones)) * 64, **preferences)
        self.send('Hello')
        self.second.team_sync_pass()
        # The switch for bot replies is off on the first phone: it does not decide.
        self.assertEqual(sorted(item[0] for item in self.items()), sorted([self.phone_id, phones['every']]))
        # A phone that changes to Only urgent drops its waiting items.
        register(self.second, self.phone_id, level='urgent_only')
        self.assertEqual({item[0]: item[9:] for item in self.items()},
                         {self.phone_id: ('dropped', 'Disabled'), phones['every']: ('waiting', None)})


class SwitchAndRelevance(TeamPushMacs, unittest.TestCase):
    def path(self):
        return f'/v1/team/rooms/{self.room}/notifications'

    def test_the_switch_of_a_room_is_a_choice_of_this_mac(self):
        (room,) = self.rooms(self.second)
        self.assertIs(room['notifications'], True)
        cursor = self.second._cursor()
        answer = self.phone(self.second)('PUT', self.path(), {'enabled': False})
        self.assertEqual(answer, {'room': self.room, 'notifications': False})
        self.assertIs(self.rooms(self.second)[0]['notifications'], False)
        self.assertTrue(self.second.updates(cursor, 0, collection_deltas=True).get('team_changed'))
        # The same value again writes nothing.
        cursor = self.second._cursor()
        self.phone(self.second)('PUT', self.path(), {'enabled': False})
        self.assertEqual(self.second._cursor(), cursor)
        # The choice never goes to the relay, and the other Mac keeps its own.
        self.assertIs(self.rooms(self.first)[0]['notifications'], True)
        self.assertNotIn('notifications', json.dumps([self.first.team_store.entry(self.room, row[0])['payload'] for row in
                                                      self.first.db.execute('SELECT id FROM team_entries')]))
        # Only a paired device calls the route on a person's Mac.
        self.assertEqual(self.refusal(self.second.team_request, 'PUT', self.path(), {'enabled': True}, '', '')[0], 403)
        for body in ({}, {'enabled': 'yes'}, {'enabled': 1}):
            self.assertEqual(self.refusal(self.phone(self.second), 'PUT', self.path(), body)[0], 400)
        self.assertEqual(self.refusal(self.phone(self.second), 'PUT', f'/v1/team/rooms/{"f" * 64}/notifications',
                                      {'enabled': True})[0], 404)
        # A quiet room queues nothing.
        self.send('Quiet')
        self.second.team_sync_pass()
        self.assertEqual(self.items(), [])
        self.phone(self.second)('PUT', self.path(), {'enabled': True})
        self.send('Loud')
        self.second.team_sync_pass()
        self.assertEqual(len(self.items()), 1)

    def test_a_store_from_before_the_switch_has_notifications_on(self):
        db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(db.close)
        db.execute('''CREATE TABLE team_rooms (room TEXT PRIMARY KEY, relay TEXT NOT NULL, name TEXT NOT NULL,
            member TEXT NOT NULL, admin TEXT NOT NULL, state TEXT NOT NULL, epoch INTEGER NOT NULL,
            seq INTEGER NOT NULL DEFAULT 0, profile TEXT NOT NULL, created_at TEXT NOT NULL)''')
        db.execute("INSERT INTO team_rooms VALUES ('r', 'x', 'Example Team', 'm', 'm', 'active', 0, 0, '{}', 'now')")
        db.executescript(team_store.SCHEMA)
        store = team_store.TeamStore(db)
        store.migrate()
        self.assertEqual(store.room('r')['notifications'], 1)

    def test_a_message_that_no_longer_needs_the_person_sends_nothing(self):
        def waiting(text):
            sent = self.send(text)
            self.second.team_sync_pass()
            return sent

        def outcome():
            flush(self.second)
            return self.items()[-1][9:]

        # The person read it on the Mac.
        sent = waiting('Read on the Mac')
        self.phone(self.second)('POST', f'/v1/team/rooms/{self.room}/read', {'first': sent['first']})
        self.assertEqual(outcome(), ('dropped', 'NoLongerRelevant'))
        # Its writer removed it.
        sent = waiting('Removed')
        self.phone(self.first)('DELETE', f'/v1/team/rooms/{self.room}/messages/{sent["id"]}', {})
        self.second.team_sync_pass()
        self.assertEqual(outcome(), ('dropped', 'NoLongerRelevant'))
        # The person made the team quiet.
        waiting('Quiet now')
        self.phone(self.second)('PUT', f'/v1/team/rooms/{self.room}/notifications', {'enabled': False})
        self.assertEqual(outcome(), ('dropped', 'NoLongerRelevant'))
        self.phone(self.second)('PUT', f'/v1/team/rooms/{self.room}/notifications', {'enabled': True})
        # A queued notification checks again at its send.
        waiting('Queued')
        with self.second.transaction():
            self.second.db.execute('UPDATE push_items SET created=created-900')
            self.second.push.plan(time.time())
        self.assertEqual(self.items()[-1][9], 'queued')
        with self.second.transaction():
            self.second.team_store.mark_read(self.room, 10 ** 9)
        self.assertEqual(outcome(), ('dropped', 'NoLongerRelevant'))
        # The person left the team.
        waiting('Before the leave')
        self.leave(self.second, {'room': self.room})
        self.assertEqual(outcome(), ('dropped', 'NoLongerRelevant'))
        self.assertEqual(self.transport.calls, [])


class Alerts(TeamPushMacs, unittest.TestCase):
    def test_the_sealed_alert_names_the_writer_and_the_team(self):
        self.seal()
        self.send('**Hello** Ada, the [report](https://example.com) is ready.')
        self.second.team_sync_pass()
        flush(self.second)
        (payload,) = self.relay.sealed
        self.assertEqual(payload['aps']['alert'], {'title': 'Grace', 'subtitle': 'Example Team',
                                                   'body': 'Hello Ada, the report is ready.'})
        self.assertEqual(payload['aps']['thread-id'], self.room)
        self.assertEqual(payload['coconut'], {'version': 1, 'device_id': self.phone_id, 'kind': 'team', 'team': self.room})
        # The badge counts cards only.
        self.assertEqual(payload['aps']['badge'], 0)
        self.assertNotIn('Hello', json.dumps(self.relay.sent))

    def test_without_a_name_for_the_writer_the_title_is_the_team(self):
        self.seal()
        self.send('Who wrote this?')
        self.second.team_sync_pass()
        with self.second.transaction():
            self.second.db.execute("DELETE FROM team_names WHERE kind='member'")
        flush(self.second)
        self.assertEqual(self.relay.sealed[0]['aps']['alert'], {'title': 'Example Team', 'body': 'Who wrote this?'})

    def test_a_phone_without_a_relay_route_gets_no_text(self):
        self.send('Private words of the team')
        self.second.team_sync_pass()
        flush(self.second)
        (payload,) = self.payloads()
        self.assertEqual(payload['aps']['alert'], {'title': 'Coconut', 'body': TEAM_BODY})
        self.assertEqual(payload['coconut']['kind'], 'team')
        for text in ('Private words', 'Grace', 'Example Team'):
            self.assertNotIn(text, json.dumps(payload))

    def test_the_text_of_a_group(self):
        def item(kind):
            return {'kind': kind}
        self.assertEqual(group_text([item('team')]), '1 team message is waiting')
        self.assertEqual(group_text([item('decision')] + [item('team')] * 3), '1 decision and 3 team messages are waiting')
        self.assertEqual(group_text([item('message'), item('team'), item('failed')]),
                         '1 message, 1 team message and 1 failed task are waiting')

    def test_a_group_of_one_team_opens_its_chat(self):
        self.send('One')
        self.send('Two')
        self.second.team_sync_pass()
        flush(self.second)
        (payload,) = self.payloads()
        self.assertEqual(payload['aps']['alert'], {'title': 'Coconut', 'body': '2 team messages are waiting'})
        self.assertEqual(payload['aps']['thread-id'], self.room)
        self.assertEqual(payload['coconut'], {'version': 1, 'device_id': self.phone_id, 'kind': 'group', 'count': 2,
                                              'team': self.room})

    def test_a_team_message_waits_and_groups_with_a_card_and_a_bot_reply(self):
        register(self.second, self.phone_id, messages=True)
        bot = self.second.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), self.phone_id)
        task = self.second.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Write report', 'mode': 'queue'},
                                  str(uuid4()), self.phone_id)
        self.second._run_task(task['id'])
        reply_task = self.second.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Hello', 'mode': 'queue'},
                                        str(uuid4()), self.phone_id)
        with self.second.transaction():
            self.second._message(bot['id'], 'Done.', task_id=reply_task['id'])
        self.send('A team message')
        self.second.team_sync_pass()
        kinds = sorted(item[1] for item in self.items() if item[9] == 'waiting')
        self.assertEqual(kinds, ['decision', 'message', 'team'])
        # Grouped waits 30 seconds after the first item, for team messages as for bot replies.
        self.assertFalse(self.second.push.deliver_one())
        flush(self.second)
        (payload,) = self.payloads()
        decision = self.second.snapshot()['decisions'][0]
        self.assertEqual(payload['aps']['alert']['body'], '1 decision, 1 message and 1 team message are waiting')
        self.assertEqual(payload['aps']['thread-id'], 'coconut-inbox')
        self.assertEqual({name: payload['coconut'][name] for name in ('kind', 'count', 'bot_id', 'decision_id')},
                         {'kind': 'group', 'count': 3, 'bot_id': bot['id'], 'decision_id': decision['id']})
        self.assertNotIn('team', payload['coconut'])

    def test_a_mixed_group_without_a_card_takes_the_fields_from_the_bot_reply(self):
        register(self.second, self.phone_id, messages=True)
        self.send('First the team')
        self.second.team_sync_pass()
        bot = self.second.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), self.phone_id)
        task = self.second.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Hello', 'mode': 'queue'},
                                  str(uuid4()), self.phone_id)
        with self.second.transaction():
            message = self.second._message(bot['id'], 'Done.', task_id=task['id'])
        flush(self.second)
        (payload,) = self.payloads()
        self.assertEqual({name: payload['coconut'][name] for name in ('bot_id', 'task_id', 'decision_id', 'message_id')},
                         {'bot_id': bot['id'], 'task_id': task['id'], 'decision_id': None, 'message_id': None})
        self.assertEqual(payload['aps']['alert']['body'], '1 message and 1 team message are waiting')
        self.assertTrue(message['id'])

    def test_no_log_line_has_the_text_or_a_name(self):
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, handler)
        self.send('The private words 5c1d')
        self.second.team_sync_pass()
        flush(self.second)
        self.seal()
        self.send('The private words again')
        self.second.team_sync_pass()
        self.relay.result = (500, 'InternalServerError')
        flush(self.second)
        output = '\n'.join(logging.Formatter().format(record) for record in records)
        for text in ('private words', 'Grace', 'Example Team'):
            self.assertNotIn(text, output)


class BotMessages(TeamTurnMacs, unittest.TestCase):
    """Ada's Mac (`first`) has the phone. The bot Reporter of the team-only Mac (`only`) answers her."""

    def test_a_message_of_the_team_bot_names_the_bot_and_the_team_only_mac_queues_nothing(self):
        phone = pair_phone(self.first)
        relay = EncryptedTransport()
        self.first.push.relay = relay
        register(self.first, phone, route=ROUTE, level='grouped')
        named = self.send('@Reporter what is open?', mentions=[self.mention])
        self.sync()
        # The team-only Mac has no push. Its store notes the message, and the note goes nowhere.
        self.assertFalse(hasattr(self.only, 'push'))
        self.assertEqual(self.only.team_store.fresh, {})
        self.assertNotIn('notifications', self.call('GET', '/v1/team/rooms')['rooms'][0])
        self.assertEqual(self.refusal(self.call, 'PUT', f'/v1/team/rooms/{self.room}/notifications',
                                      {'enabled': False})[0], 409)
        self.serve(reply('3 tasks are open.'))
        self.run_turns()
        flush(self.first)
        (payload,) = relay.sealed
        self.assertEqual(payload['aps']['alert'], {'title': 'Reporter', 'subtitle': 'Example Team',
                                                   'body': '3 tasks are open.'})
        self.assertEqual(payload['coconut']['kind'], 'team')
        self.assertTrue(named['id'])


class OldStores(unittest.TestCase):
    OUTBOX = '''
        CREATE TABLE push_outbox (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, event_key TEXT NOT NULL,
            bot_id TEXT NOT NULL, task_id TEXT NOT NULL, decision_id TEXT, kind TEXT NOT NULL,
            turn_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            not_before REAL NOT NULL, expires REAL NOT NULL, reason TEXT, UNIQUE(device_id, event_key));
        INSERT INTO push_outbox VALUES ('a','d','a','b','t','x','decision','u','queued',0,5,9e9,NULL);'''

    def store(self, script):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db = sqlite3.connect(f'{folder.name}/old.db', isolation_level=None)
        self.addCleanup(db.close)
        db.executescript(script)
        for _ in range(2):
            PushDelivery(SimpleNamespace(db=db, root=folder.name))
        return db

    def test_an_old_outbox_becomes_items_with_the_new_column(self):
        db = self.store(self.OUTBOX)
        self.assertEqual([row[1] for row in db.execute('PRAGMA table_info(push_items)')], ITEM_COLUMNS)
        self.assertEqual(db.execute('SELECT event_key,kind,status,team FROM push_items').fetchall(),
                         [('a', 'decision', 'waiting', None)])

    def test_old_items_get_the_new_column_before_the_old_outbox_moves(self):
        db = self.store('''
        CREATE TABLE push_items (
            device_id TEXT NOT NULL, event_key TEXT NOT NULL, kind TEXT NOT NULL, bot_id TEXT NOT NULL,
            task_id TEXT NOT NULL, decision_id TEXT, message_id TEXT, turn_id TEXT NOT NULL,
            urgent INTEGER NOT NULL, created REAL NOT NULL, status TEXT NOT NULL, delivery_id TEXT, reason TEXT,
            PRIMARY KEY(device_id, event_key));
        INSERT INTO push_items VALUES ('d','old','completed','b','t',NULL,NULL,'u',0,1,'sent',NULL,NULL);''' + self.OUTBOX)
        self.assertEqual(db.execute('SELECT event_key,team FROM push_items ORDER BY event_key').fetchall(),
                         [('a', None), ('old', None)])


if __name__ == '__main__':
    unittest.main()
