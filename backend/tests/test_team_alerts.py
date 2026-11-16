"""Alerts in a chat of a team: a message of a bot with a type and a level (coconut/team_alerts.py).

The team-only Mac (`only`) runs the bot Reporter and gets each send at `POST /v1/gateway/messages`, as a script of the
Mac and the plugin send it. Ada's Mac (`first`) is a full Mac: it reads the log and serves the chat to the app. The
Macs share the fakes of team_fakes.py. Each ID and each name here is made up.
"""
import json
import unittest

from coconut import team_alerts, team_store, team_turns
from coconut.team_alerts import MISFIT, alert_fields, alert_request, filter_fields, split_target, summary_view
from test_team_direct_turns import DirectTurnMacs
from test_team_turns import TeamTurnMacs


class AlertFieldTests(unittest.TestCase):
    def test_a_reader_takes_an_alert_that_fits_and_leaves_each_other_one_out(self):
        self.assertEqual(alert_fields({'type': 'risk', 'level': 'critical'}), {'type': 'risk', 'level': 'critical'})
        self.assertEqual(alert_fields({'type': 'a-b_9'}), {'type': 'a-b_9', 'level': 'notice'})
        self.assertEqual(alert_fields({'type': 'x' * 32, 'level': 'info'}), {'type': 'x' * 32, 'level': 'info'})
        # A key of a later version changes nothing.
        self.assertEqual(alert_fields({'type': 'risk', 'level': 'info', 'title': 'Later'}),
                         {'type': 'risk', 'level': 'info'})
        for value in (None, 'risk', [], {}, {'level': 'info'}, {'type': ''}, {'type': 'x' * 33}, {'type': 'Risk'},
                      {'type': 'ri sk'}, {'type': 'risk\n'}, {'type': 'résumé'}, {'type': 7},
                      {'type': ['risk']}, {'type': 'risk', 'level': 'urgent'}, {'type': 'risk', 'level': 'Critical'},
                      {'type': 'risk', 'level': None}, {'type': 'risk', 'level': 2}, {'type': 'risk', 'level': []}):
            with self.subTest(value=value):
                self.assertIsNone(alert_fields(value))

    def test_a_writer_normalizes_the_case_and_the_space_and_refuses_the_rest(self):
        self.assertIsNone(alert_request(None))
        self.assertEqual(alert_request({'type': ' Risk ', 'level': ' CRITICAL'}), {'type': 'risk', 'level': 'critical'})
        self.assertEqual(alert_request({'type': 'deploy'}), {'type': 'deploy', 'level': 'notice'})
        self.assertEqual(alert_request({'type': 'deploy', 'level': None}), {'type': 'deploy', 'level': 'notice'})
        for value in ('risk', [], {}, {'type': ''}, {'type': '  '}, {'type': 'two words'}, {'type': 'x' * 33},
                      {'type': 'risk!'}, {'type': 5}, {'type': 'risk', 'level': 'urgent'}, {'type': 'risk', 'level': ''},
                      {'type': 'risk', 'level': 1}, {'level': 'critical'}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    alert_request(value)
                self.assertEqual(str(raised.exception), MISFIT)

    def test_the_end_of_a_chat_id_names_an_alert(self):
        self.assertEqual(split_target('b:team:r'), ('b:team:r', None))
        self.assertEqual(split_target('b:team:r:alert:risk'), ('b:team:r', {'type': 'risk', 'level': None}))
        self.assertEqual(split_target('b:team:r:c:alert:risk:critical'),
                         ('b:team:r:c', {'type': 'risk', 'level': 'critical'}))
        self.assertEqual(split_target('b:team:r:alert:'), ('b:team:r', {'type': '', 'level': None}))
        self.assertEqual(split_target(None), ('None', None))

    def test_the_filter_of_a_page_takes_only_what_an_alert_can_have(self):
        self.assertEqual(filter_fields({}), (None, None))
        self.assertEqual(filter_fields({'alert_type': 'risk', 'alert_level': 'critical'}), ('risk', 'critical'))
        for data in ({'alert_type': 'Risk'}, {'alert_type': ''}, {'alert_type': 'x' * 33}, {'alert_level': 'urgent'},
                     {'alert_level': ''}, {'alert_type': ['risk']}):
            with self.subTest(data=data):
                self.assertRaises(ValueError, filter_fields, data)

    def test_the_summary_sorts_the_types_by_their_count_and_keeps_the_totals(self):
        rows = [('deploy', 2, 0), ('risk', 5, 3), ('infra', 2, None)]
        self.assertEqual(summary_view(rows), {'total': 9, 'critical': 3, 'types': [
            {'type': 'risk', 'count': 5, 'critical': 3}, {'type': 'deploy', 'count': 2, 'critical': 0},
            {'type': 'infra', 'count': 2, 'critical': 0}]})
        many = summary_view([(f't{number:03}', 1, 0) for number in range(team_alerts.MAX_TYPES + 5)])
        self.assertEqual((many['total'], len(many['types'])), (team_alerts.MAX_TYPES + 5, team_alerts.MAX_TYPES))
        self.assertEqual(summary_view([]), {'total': 0, 'critical': 0, 'types': []})

    def test_only_a_message_of_a_bot_reads_as_an_alert(self):
        alert = {'type': 'risk', 'level': 'critical'}
        self.assertEqual(team_store.message_fields({'text': 'Hi', 'bot': 'a' * 32, 'alert': alert})['alert'], alert)
        self.assertIsNone(team_store.message_fields({'text': 'Hi', 'alert': alert})['alert'])
        # An alert that does not fit leaves the message valid.
        plain = team_store.message_fields({'text': 'Hi', 'bot': 'a' * 32, 'alert': {'type': 'No Fit'}})
        self.assertEqual((plain['text'], plain['alert']), ('Hi', None))


class AlertMacs(TeamTurnMacs):
    def post(self, text='Exposure is over the limit.', message_id='outside:1', chat=None, **fields):
        """A send as a script of the Mac makes it: `kind: send` with a message ID, a text, the home and the chat."""
        return self.only.gateway_message({'kind': 'send', 'message_id': message_id, 'text': text,
                                          'home': str(self.home), 'chat_id': chat or self.chat_id(), **fields})

    def chat_id(self, chat=None):
        return team_turns.chat_id(self.room, self.bot['id'], chat)

    def named(self, name='Alerts'):
        chat = self.call('POST', f'/v1/team/rooms/{self.room}/chats', {'name': name}, service=self.first)['chat']
        self.sync()
        return chat

    def page(self, service=None, **query):
        return self.call('GET', f'/v1/team/rooms/{self.room}/messages', query, service=service or self.first)

    def shown(self, service=None, **query):
        """The page as the app gets it, oldest first: (text, alert)."""
        return [(row['text'], row['alert']) for row in reversed(self.page(service, **query)['messages'])]

    def payloads(self, service=None):
        """The payloads of the messages of the bot in the store of a Mac, oldest first."""
        service = service or self.first
        with service.lock:
            rows = service.db.execute("SELECT payload FROM team_entries WHERE kind='message' ORDER BY first").fetchall()
        return [payload for payload in (json.loads(row[0]) for row in rows) if payload.get('bot')]


class SendTests(AlertMacs, unittest.TestCase):
    def test_a_typed_alert_and_a_plain_message_reach_each_member(self):
        self.assertEqual(self.post(alert={'type': 'risk', 'level': 'critical'}), {'recorded': True})
        self.assertEqual(self.post('The nightly job ran.', 'outside:2'), {'recorded': True})
        self.sync()
        expected = [('Exposure is over the limit.', {'type': 'risk', 'level': 'critical'}),
                    ('The nightly job ran.', None)]
        # Ada's Mac reads them from the relay, and the Mac of the bot shows its own writes the same way.
        self.assertEqual(self.shown(), expected)
        self.assertEqual(self.shown(self.only), expected)
        first, second = self.payloads()
        self.assertEqual(first['alert'], {'type': 'risk', 'level': 'critical'})
        self.assertNotIn('alert', second)
        self.assertEqual(self.page()['alerts'], {'total': 1, 'critical': 1,
                                                 'types': [{'type': 'risk', 'count': 1, 'critical': 1}]})

    def test_an_alert_without_a_level_is_a_notice_and_the_service_normalizes_the_texts(self):
        self.post(alert={'type': 'deploy'})
        self.post(message_id='outside:2', alert={'type': ' Payouts ', 'level': 'INFO'})
        self.sync()
        self.assertEqual([alert for _, alert in self.shown()],
                         [{'type': 'deploy', 'level': 'notice'}, {'type': 'payouts', 'level': 'info'}])

    def test_an_alert_that_does_not_fit_is_refused_and_writes_nothing(self):
        for alert in ({'type': 'two words'}, {'type': 'x' * 33}, {'type': ''}, {'type': 'risk', 'level': 'urgent'},
                      {'level': 'critical'}, 'risk', ['risk'], {'type': 3}):
            with self.subTest(alert=alert):
                self.assertEqual(self.refusal(lambda: self.post(alert=alert)), (400, MISFIT))
        self.sync()
        self.assertEqual(self.shown(), [])
        self.assertEqual(self.payloads(), [])

    def test_a_repeat_with_the_same_message_id_adds_no_second_alert(self):
        for _ in range(2):
            self.assertEqual(self.post(alert={'type': 'risk'}), {'recorded': True})
        self.sync()
        self.assertEqual(self.page()['alerts']['total'], 1)

    def test_the_chat_id_of_a_cron_target_names_the_alert(self):
        self.assertEqual(self.post(chat=self.chat_id() + ':alert:deploy'), {'recorded': True})
        self.assertEqual(self.post(message_id='outside:2', chat=self.chat_id() + ':alert:Risk:critical'),
                         {'recorded': True})
        # The field `alert` counts when a send has both.
        self.post(message_id='outside:3', chat=self.chat_id() + ':alert:deploy', alert={'type': 'infra', 'level': 'info'})
        self.sync()
        self.assertEqual([alert for _, alert in self.shown()], [
            {'type': 'deploy', 'level': 'notice'}, {'type': 'risk', 'level': 'critical'},
            {'type': 'infra', 'level': 'info'}])
        for end in (':alert:', ':alert:two words', ':alert:risk:urgent', ':alert:risk:critical:more'):
            with self.subTest(end=end):
                self.assertEqual(self.refusal(lambda: self.post(message_id='outside:4', chat=self.chat_id() + end)),
                                 (400, MISFIT))
        self.sync()
        self.assertEqual(len(self.shown()), 3)

    def test_an_alert_goes_into_a_named_chat_and_stays_out_of_the_team_chat(self):
        alerts = self.named()
        self.post(chat=self.chat_id(alerts), alert={'type': 'risk'})
        self.post('Deployed.', 'outside:2', chat=self.chat_id(alerts) + ':alert:deploy:info')
        self.post('A plain note.', 'outside:3', chat=self.chat_id(alerts))
        self.sync()
        self.assertEqual(self.shown(chat=alerts), [
            ('Exposure is over the limit.', {'type': 'risk', 'level': 'notice'}),
            ('Deployed.', {'type': 'deploy', 'level': 'info'}), ('A plain note.', None)])
        self.assertEqual(self.page(chat=alerts)['alerts']['total'], 2)
        self.assertEqual((self.shown(), self.page()['alerts']), ([], {'total': 0, 'critical': 0, 'types': []}))

    def test_each_part_of_a_long_alert_carries_the_alert(self):
        self.assertEqual(self.post('a' * (team_store.MAX_TEXT + 1), alert={'type': 'risk'}), {'recorded': True})
        self.sync()
        self.assertEqual([(len(text), alert) for text, alert in self.shown()],
                         [(team_store.MAX_TEXT, {'type': 'risk', 'level': 'notice'}),
                          (1, {'type': 'risk', 'level': 'notice'})])

    def test_an_alert_is_unread_and_last_as_each_other_message_of_the_bot(self):
        self.post(alert={'type': 'risk', 'level': 'critical'})
        self.sync()
        room = self.call('GET', '/v1/team/rooms', service=self.first)['rooms'][0]
        self.assertEqual((room['unread'], room['last_message']['text']), (1, 'Exposure is over the limit.'))

    def test_a_person_writes_no_alert(self):
        sent = self.send('I am a person.', alert={'type': 'risk', 'level': 'critical'})
        self.assertIsNone(sent['alert'])
        self.sync()
        self.assertEqual(self.shown(), [('I am a person.', None)])
        self.assertEqual(self.page()['alerts']['total'], 0)

    def test_an_alert_of_a_later_version_that_does_not_fit_shows_as_a_plain_message(self):
        with self.only.lock:
            room = self.only.team_store.room(self.room)
        with self.only._team_room_lock(self.room):
            self.only._team_write(room, 'c' * 32, False, {'kind': 'message', 'text': 'From a later version.',
                                                         'bot': self.entry, 'alert': {'type': 'risk', 'level': 'fatal'}})
        self.sync()
        self.assertEqual(self.shown(), [('From a later version.', None)])
        self.assertEqual(self.payloads()[0]['alert'], {'type': 'risk', 'level': 'fatal'})


class DirectChatTests(DirectTurnMacs, unittest.TestCase):
    def test_a_direct_chat_takes_the_text_without_the_alert(self):
        answer = self.only.gateway_message({'kind': 'send', 'message_id': 'outside:1', 'text': 'Only for you.',
                                            'home': str(self.home), 'chat_id': self.direct_chat_id() + ':alert:risk',
                                            'alert': {'type': 'risk', 'level': 'critical'}})
        self.assertEqual(answer, {'recorded': True})
        self.assertEqual(self.bot_replies(), ['Only for you.'])
        self.assertEqual(self.chat(), [])


class FilterTests(AlertMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        sends = [('risk', 'critical'), ('deploy', 'info'), ('risk', 'notice'), None, ('deploy', 'critical'),
                 ('risk', 'notice')]
        for number, alert in enumerate(sends):
            fields = {'alert': {'type': alert[0], 'level': alert[1]}} if alert else {}
            self.post(f'm{number}', f'outside:{number}', **fields)
        self.sync()

    def texts(self, **query):
        return [text for text, _ in self.shown(**query)]

    def test_all_shows_each_message_and_a_type_or_a_level_only_its_alerts(self):
        self.assertEqual(self.texts(), ['m0', 'm1', 'm2', 'm3', 'm4', 'm5'])
        self.assertEqual(self.texts(alert_type='risk'), ['m0', 'm2', 'm5'])
        self.assertEqual(self.texts(alert_type='deploy'), ['m1', 'm4'])
        self.assertEqual(self.texts(alert_level='critical'), ['m0', 'm4'])
        self.assertEqual(self.texts(alert_type='risk', alert_level='critical'), ['m0'])
        self.assertEqual(self.texts(alert_type='payouts'), [])

    def test_the_newest_page_counts_each_type_of_the_whole_chat_also_with_a_filter(self):
        counts = {'total': 5, 'critical': 2, 'types': [{'type': 'risk', 'count': 3, 'critical': 1},
                                                      {'type': 'deploy', 'count': 2, 'critical': 1}]}
        self.assertEqual(self.page()['alerts'], counts)
        self.assertEqual(self.page(alert_type='deploy')['alerts'], counts)
        self.assertEqual(self.page(limit='1')['alerts'], counts)
        # An older page has no counts: the app keeps the ones of the newest page.
        self.assertNotIn('alerts', self.page(before='4'))

    def test_a_filtered_chat_has_pages(self):
        first = self.page(alert_type='risk', limit='2')
        self.assertEqual([row['text'] for row in first['messages']], ['m5', 'm2'])
        second = self.page(alert_type='risk', limit='2', before=str(first['before']))
        self.assertEqual(([row['text'] for row in second['messages']], second['before']), (['m0'], None))

    def test_a_filter_that_no_alert_can_have_is_refused(self):
        for query in ({'alert_type': 'Risk'}, {'alert_type': 'x' * 33}, {'alert_level': 'urgent'}):
            with self.subTest(query=query):
                self.assertEqual(self.refusal(lambda: self.page(**query))[0], 400)

    def test_a_filter_moves_no_read_position(self):
        def unread():
            return self.call('GET', '/v1/team/rooms', service=self.first)['rooms'][0]['unread']
        before = unread()
        self.page(alert_type='risk')
        self.page(alert_level='critical')
        self.assertEqual((before, unread()), (6, 6))

    def test_a_removed_alert_leaves_the_counts(self):
        newest = self.page(alert_type='risk')['messages'][0]
        self.call('DELETE', f'/v1/team/rooms/{self.room}/messages/{newest["id"]}', service=self.first)
        self.sync()
        self.assertEqual(self.texts(alert_type='risk'), ['m0', 'm2'])
        # Two types now have two alerts each, and the name gives their order.
        self.assertEqual(self.page()['alerts'], {'total': 4, 'critical': 2, 'types': [
            {'type': 'deploy', 'count': 2, 'critical': 1}, {'type': 'risk', 'count': 2, 'critical': 1}]})

    def test_the_pages_of_a_filter_read_the_indexes_of_the_alerts(self):
        store = self.first.team_store
        for query, index in (({'alert_type': 'risk'}, 'team_messages_alert_type'),
                             ({'alert_level': 'critical'}, 'team_messages_alert_level')):
            with self.subTest(query=query):
                statement, values = store._page(self.room, None, None, 50, **query)
                plan = ' '.join(row[3] for row in self.first.db.execute('EXPLAIN QUERY PLAN ' + statement, values))
                self.assertIn(index, plan)


class OlderStoreTests(AlertMacs, unittest.TestCase):
    def test_a_store_of_an_older_version_gets_its_alerts_at_the_update(self):
        """A service without alerts kept the payload and showed a plain message. The update reads the payload again."""
        self.post(alert={'type': 'risk', 'level': 'critical'})
        self.post('The nightly job ran.', 'outside:2')
        self.sync()
        store, db = self.first.team_store, self.first.db
        with self.first.lock:
            before = db.execute('SELECT entry, first, created_at, updated_at, text FROM team_messages').fetchall()
            position = store.read_position(self.room)
            # The table as a version before alerts had it.
            for index in ('team_messages_alert_type', 'team_messages_alert_level'):
                db.execute(f'DROP INDEX {index}')
            for column in ('alert_type', 'alert_level'):
                db.execute(f'ALTER TABLE team_messages DROP COLUMN {column}')
            store.migrate()
            after = db.execute('SELECT entry, first, created_at, updated_at, text FROM team_messages').fetchall()
            self.assertEqual((after, store.read_position(self.room)), (before, position))
            # A second start does nothing more.
            store.migrate()
        self.assertEqual(self.shown(), [('Exposure is over the limit.', {'type': 'risk', 'level': 'critical'}),
                                        ('The nightly job ran.', None)])
        self.assertEqual(self.page(alert_type='risk')['alerts']['total'], 1)


if __name__ == '__main__':
    unittest.main()
