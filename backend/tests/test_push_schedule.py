import base64
from datetime import datetime
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4
from zoneinfo import ZoneInfo
from coconut.push_schedule import DEFAULT_SCHEDULE, LEGACY_SCHEDULE, due, in_window, merge, next_opening, validate
from coconut.service import Service, ServiceError
from test_push import RecordingTransport, pair_phone
from test_push_relay import EncryptedTransport
from test_service import ControlledProvider

NEW_YORK = ZoneInfo('America/New_York')


def instant(*parts, zone=NEW_YORK):
    return int(datetime(*parts, tzinfo=zone).timestamp())


def local(value, zone=NEW_YORK):
    return datetime.fromtimestamp(value, zone).isoformat()


class ScheduleTests(unittest.TestCase):
    def test_window_includes_start_and_excludes_end(self):
        for parts, inside in [((2026, 7, 15, 8, 59, 59), False), ((2026, 7, 15, 9), True),
                              ((2026, 7, 15, 17, 59, 59), True), ((2026, 7, 15, 18), False)]:
            self.assertEqual(in_window(DEFAULT_SCHEDULE, instant(*parts)), inside, parts)

    def test_next_opening_follows_local_time_through_daylight_saving(self):
        self.assertEqual(local(next_opening(DEFAULT_SCHEDULE, instant(2026, 3, 7, 18))), '2026-03-08T09:00:00-04:00')
        self.assertEqual(local(next_opening(DEFAULT_SCHEDULE, instant(2026, 10, 31, 19))), '2026-11-01T09:00:00-05:00')
        self.assertEqual(local(next_opening(DEFAULT_SCHEDULE, instant(2026, 11, 1, 8))), '2026-11-01T09:00:00-05:00')
        self.assertEqual(next_opening(DEFAULT_SCHEDULE, instant(2026, 7, 15, 10)), instant(2026, 7, 15, 10))

    def test_opening_inside_the_spring_gap_is_the_first_window_instant(self):
        schedule = dict(DEFAULT_SCHEDULE, start='02:30', end='05:00')
        opening = next_opening(schedule, instant(2026, 3, 8, 1, 59))
        self.assertEqual(local(opening), '2026-03-08T03:00:00-04:00')
        self.assertTrue(in_window(schedule, opening))
        self.assertFalse(in_window(schedule, opening - 1))

    def test_fall_back_opening_is_never_before_now(self):
        schedule = dict(DEFAULT_SCHEDULE, start='01:30', end='03:00')
        utc = ZoneInfo('UTC')
        stamp = lambda hour, minute: int(datetime(2026, 11, 1, hour, minute, tzinfo=utc).timestamp())
        # 00:50 EDT waits for the first 01:30 EDT.
        self.assertEqual(next_opening(schedule, stamp(4, 50)), stamp(5, 30))
        # 01:10 EST is the second pass. The first 01:30 EDT is already past, so the window opens at 01:30 EST.
        self.assertEqual(next_opening(schedule, stamp(6, 10)), stamp(6, 30))
        for at in (stamp(4, 50), stamp(5, 45), stamp(6, 10)):
            opening = next_opening(schedule, at)
            self.assertGreaterEqual(opening, at)
            self.assertTrue(in_window(schedule, opening))

    def test_fall_back_reopens_a_window_that_started_before_the_repeated_hour(self):
        schedule = dict(DEFAULT_SCHEDULE, start='00:30', end='01:30')
        utc = ZoneInfo('UTC')
        stamp = lambda hour, minute, second=0: int(datetime(2026, 11, 1, hour, minute, second, tzinfo=utc).timestamp())
        second_one = stamp(6, 0)
        self.assertEqual(local(second_one), '2026-11-01T01:00:00-05:00')
        # The first 01:45 EDT is outside. The clock repeats 01:00, so the window reopens at the second 01:00.
        self.assertEqual(next_opening(schedule, stamp(5, 45)), second_one)
        self.assertEqual(next_opening(schedule, stamp(5, 59, 59)), second_one)
        self.assertEqual(next_opening(schedule, second_one), second_one)
        self.assertEqual(next_opening(schedule, stamp(6, 29, 59)), stamp(6, 29, 59))
        # 01:30 EST closes it for the day. The next opening is the following 00:30.
        self.assertEqual(local(next_opening(schedule, stamp(6, 30))), '2026-11-02T00:30:00-05:00')
        # Before the window, the ordinary start is first.
        self.assertEqual(next_opening(schedule, stamp(4, 29)), stamp(4, 30))
        self.assertFalse(in_window(schedule, stamp(5, 45)))
        self.assertTrue(in_window(schedule, second_one))

    def test_window_inside_the_spring_gap_waits_for_the_next_day(self):
        schedule = dict(DEFAULT_SCHEDULE, start='02:10', end='02:50')
        at = instant(2026, 3, 8, 1, 59)
        self.assertEqual(local(next_opening(schedule, at)), '2026-03-09T02:10:00-04:00')

    def test_short_window_opening_in_the_gap_starts_when_the_clock_jumps(self):
        schedule = dict(DEFAULT_SCHEDULE, start='02:30', end='03:15')
        opening = next_opening(schedule, instant(2026, 3, 8, 1, 59))
        self.assertEqual(local(opening), '2026-03-08T03:00:00-04:00')
        self.assertTrue(in_window(schedule, opening))
        self.assertFalse(in_window(schedule, opening - 1))

    def test_default_is_every_day_nine_to_six_new_york(self):
        self.assertEqual(DEFAULT_SCHEDULE, dict(enabled=True, start='09:00', end='18:00', timezone='America/New_York',
                                                weekdays=[1, 2, 3, 4, 5, 6, 7]))

    def test_fall_back_repeated_hour_counts_both_times_inside_the_window(self):
        schedule = dict(DEFAULT_SCHEDULE, start='01:00', end='02:00')
        first = int(datetime(2026, 11, 1, 1, 30, tzinfo=NEW_YORK).timestamp())
        self.assertTrue(in_window(schedule, first))
        self.assertTrue(in_window(schedule, first + 3600))
        self.assertFalse(in_window(schedule, first + 1800 + 3600))

    def test_validation(self):
        self.assertEqual(validate(DEFAULT_SCHEDULE), DEFAULT_SCHEDULE)
        for change in ({'timezone': 'Mars/Base'}, {'timezone': 'EST5EDT/../x'}, {'timezone': ''}, {'start': '18:00'},
                       {'start': '9:00'}, {'end': '24:00'}, {'enabled': 1}, {'extra': 1}, {'weekdays': []},
                       {'weekdays': [0]}, {'weekdays': [8]}, {'weekdays': [True]}, {'weekdays': [1.0]}, {'weekdays': ['1']},
                       {'weekdays': 1}, {'weekdays': None}):
            self.assertIsNone(validate(dict(DEFAULT_SCHEDULE, **change)), change)

    def test_weekdays_normalize_and_the_retired_honor_routine_is_dropped(self):
        self.assertEqual(validate(dict(DEFAULT_SCHEDULE, weekdays=[5, 1, 1, 3]))['weekdays'], [1, 3, 5])
        for value in (True, False, 'x', None):
            self.assertEqual(validate(dict(DEFAULT_SCHEDULE, honor_routine=value)), DEFAULT_SCHEDULE, value)

    def test_window_and_opening_honor_weekdays(self):
        weekdays = dict(DEFAULT_SCHEDULE, weekdays=[1, 2, 3, 4, 5])
        # 2026-07-17 is a Friday and 2026-07-18 a Saturday.
        self.assertTrue(in_window(weekdays, instant(2026, 7, 17, 9)))
        self.assertFalse(in_window(weekdays, instant(2026, 7, 17, 18)))
        self.assertFalse(in_window(weekdays, instant(2026, 7, 18, 12)))
        self.assertFalse(in_window(weekdays, instant(2026, 7, 19, 12)))
        self.assertEqual(local(next_opening(weekdays, instant(2026, 7, 17, 18))), '2026-07-20T09:00:00-04:00')
        self.assertEqual(local(next_opening(weekdays, instant(2026, 7, 18, 8))), '2026-07-20T09:00:00-04:00')
        self.assertEqual(local(next_opening(weekdays, instant(2026, 7, 20, 8, 59, 59))), '2026-07-20T09:00:00-04:00')
        self.assertEqual(next_opening(weekdays, instant(2026, 7, 20, 9)), instant(2026, 7, 20, 9))

    def test_weekday_is_the_local_day_not_the_utc_day(self):
        friday_only = dict(DEFAULT_SCHEDULE, weekdays=[5], start='20:00', end='23:00')
        # 22:00 Friday in New York is already Saturday in UTC.
        self.assertTrue(in_window(friday_only, instant(2026, 7, 17, 22)))
        self.assertFalse(in_window(friday_only, instant(2026, 7, 18, 21)))

    def test_single_weekday_waits_up_to_a_week_and_never_loops_or_goes_back(self):
        sunday = dict(DEFAULT_SCHEDULE, weekdays=[7])
        at = instant(2026, 7, 19, 18)
        self.assertEqual(local(next_opening(sunday, at)), '2026-07-26T09:00:00-04:00')
        for probe in range(0, 8 * 86400, 3 * 3600 + 17 * 60):
            moment = instant(2026, 7, 14) + probe
            opening = next_opening(sunday, moment)
            self.assertGreaterEqual(opening, moment)
            self.assertTrue(in_window(sunday, opening))

    def test_weekday_opening_across_daylight_saving_follows_local_time(self):
        # 2026-03-08 is the spring-forward Sunday. Saturday evening waits for Sunday morning.
        sunday = dict(DEFAULT_SCHEDULE, weekdays=[7])
        self.assertEqual(local(next_opening(sunday, instant(2026, 3, 7, 20))), '2026-03-08T09:00:00-04:00')
        monday = dict(DEFAULT_SCHEDULE, weekdays=[1], start='01:30', end='03:00')
        self.assertEqual(local(next_opening(monday, instant(2026, 11, 1, 12))), '2026-11-02T01:30:00-05:00')
        # A gap start on a day that is not chosen does not open early.
        gap = dict(DEFAULT_SCHEDULE, weekdays=[1], start='02:30', end='05:00')
        self.assertEqual(local(next_opening(gap, instant(2026, 3, 8, 1, 59))), '2026-03-09T02:30:00-04:00')
        # The gap day itself, when chosen, opens when the clock jumps.
        gap_day = dict(gap, weekdays=[7])
        self.assertEqual(local(next_opening(gap_day, instant(2026, 3, 8, 1, 59))), '2026-03-08T03:00:00-04:00')

    def test_older_clients_without_new_fields_keep_the_stored_values(self):
        legacy = dict(enabled=True, start='08:00', end='17:00', timezone='Europe/Paris')
        current = dict(DEFAULT_SCHEDULE)
        self.assertEqual(merge(current, legacy), dict(current, **legacy))
        self.assertEqual(validate(legacy), dict(DEFAULT_SCHEDULE, **legacy))

    def test_turning_off_wins_over_invalid_draft_fields(self):
        draft = dict(enabled=False, start='18:00', end='09:00', timezone='Mars/Base')
        self.assertEqual(merge(DEFAULT_SCHEDULE, draft), dict(DEFAULT_SCHEDULE, enabled=False))
        self.assertEqual(merge(DEFAULT_SCHEDULE, dict(enabled=False)), dict(DEFAULT_SCHEDULE, enabled=False))
        self.assertIsNone(merge(DEFAULT_SCHEDULE, dict(enabled=True, start='18:00', end='09:00')))
        self.assertIsNone(merge(DEFAULT_SCHEDULE, 'off'))

    def test_merge_applies_partial_updates_over_the_stored_schedule(self):
        current = dict(DEFAULT_SCHEDULE, weekdays=[1, 2], start='08:00')
        self.assertEqual(merge(current, dict(end='17:00')), dict(current, end='17:00'))
        self.assertEqual(merge(current, dict(weekdays=[6, 7])), dict(current, weekdays=[6, 7]))
        self.assertEqual(merge(current, dict(honor_routine=True)), current)
        self.assertIsNone(merge(current, dict(weekdays=[])))
        draft = dict(enabled=False, weekdays=[], start='nope')
        self.assertEqual(merge(current, draft), dict(current, enabled=False))


class DueTests(unittest.TestCase):
    NOON = instant(2026, 7, 15, 12)

    def test_grouped_settles_a_burst_and_waits_ten_minutes_after_the_last_notification(self):
        self.assertEqual(due(DEFAULT_SCHEDULE, 'grouped', self.NOON, 0, self.NOON), self.NOON + 30)
        self.assertEqual(due(DEFAULT_SCHEDULE, 'grouped', self.NOON, self.NOON - 60, self.NOON), self.NOON + 540)
        self.assertEqual(due(DEFAULT_SCHEDULE, 'every', self.NOON, self.NOON - 1, self.NOON), self.NOON + 2)

    def test_outside_work_hours_everything_waits_for_the_opening(self):
        night = instant(2026, 7, 15, 23)
        for level in ('every', 'grouped'):
            self.assertEqual(due(DEFAULT_SCHEDULE, level, night, night - 5, night), instant(2026, 7, 16, 9))
        # A wait that ends after the window closes moves to the next opening.
        self.assertEqual(due(DEFAULT_SCHEDULE, 'grouped', instant(2026, 7, 15, 17, 55), instant(2026, 7, 15, 17, 55), instant(2026, 7, 15, 17, 55)),
                         instant(2026, 7, 16, 9))
        self.assertEqual(due(LEGACY_SCHEDULE, 'grouped', night, 0, night), night + 30)

    def test_a_wait_that_ended_while_coconut_was_stopped_still_respects_the_window(self):
        # The item was due at 17:55, inside work hours. Coconut restarts at 23:00, so it waits for the morning.
        night = instant(2026, 7, 15, 23)
        self.assertEqual(due(DEFAULT_SCHEDULE, 'grouped', instant(2026, 7, 15, 17, 54), 0, night), instant(2026, 7, 16, 9))
        self.assertEqual(due(LEGACY_SCHEDULE, 'grouped', night - 3600, 0, night), night)

    def test_only_urgent_never_groups(self):
        self.assertIsNone(due(DEFAULT_SCHEDULE, 'urgent_only', self.NOON, 0, self.NOON))


class DeliveryTimingTests(unittest.TestCase):
    NIGHT = instant(2026, 7, 15, 23)
    MORNING = instant(2026, 7, 16, 9)
    NOON = instant(2026, 7, 15, 12)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.start()
        self.bot = self.write('POST', '/v1/bots', {'name': 'Desk'})

    def start(self):
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.transport = RecordingTransport()
        self.service.push.transport = self.transport
        if not hasattr(self, 'device'):
            self.device = pair_phone(self.service)
            self.register(schedule=DEFAULT_SCHEDULE)

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), self.device)

    def register(self, level='grouped', messages=True, **values):
        return self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True,
                                                         preferences=dict(level=level, messages=messages), **values))

    def at(self, ts):
        return mock.patch('coconut.push.time', SimpleNamespace(time=lambda: ts))

    def decision(self, urgent=False, ts=None):
        """Record a pending card at `ts`, by default at night. `urgent` is the bot's needs_attention_now."""
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text='Write report', mode='queue'))
        self.service._run_task(task['id'])
        decision = self.service._get('decisions', next(d['id'] for d in self.service.snapshot()['decisions']
                                                      if d['task_id'] == task['id']))
        self.service.db.execute('DELETE FROM push_items WHERE task_id=?', (task['id'],))
        decision['needs_attention_now'] = urgent
        self.service._put('decisions', decision)
        with self.at(ts or self.NIGHT):
            self.service.push.record('decision.created', self.service._public(decision), self.bot['id'], task['id'])
        return decision

    def failure(self, ts=None):
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text='Hello', mode='queue'))
        self.service._run_task(task['id'])
        self.service.db.execute('DELETE FROM push_items WHERE task_id=?', (task['id'],))
        task = self.service._get('tasks', task['id'])
        task['status'] = 'failed'
        self.service._put('tasks', task)
        with self.at(ts or self.NIGHT):
            self.service.push.record('task.updated', dict(status='failed', needs_attention_now=True), self.bot['id'], task['id'])
        return task

    def deliver(self, ts):
        with self.at(ts):
            while self.service.push.deliver_one():
                pass

    def sent(self):
        return [call[2] for call in self.transport.calls]

    def items(self):
        return self.service.db.execute('SELECT kind,urgent,status FROM push_items ORDER BY created,rowid').fetchall()

    def test_a_card_waits_for_the_opening_and_stays_in_inbox(self):
        decision = self.decision()
        self.deliver(self.NIGHT + 60)
        self.deliver(self.MORNING - 1)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.items(), [('decision', 0, 'waiting')])
        self.assertEqual([d['id'] for d in self.service.snapshot()['decisions'] if d['status'] == 'pending'], [decision['id']])
        self.deliver(self.MORNING)
        self.assertEqual([payload['coconut']['decision_id'] for payload in self.sent()], [decision['id']])
        self.assertEqual(self.items(), [('decision', 0, 'sent')])

    def test_the_opening_sends_one_group_for_everything_that_waited(self):
        for level in ('grouped', 'every'):
            self.service.db.execute('DELETE FROM push_items')
            self.service.db.execute('DELETE FROM push_outbox')
            self.transport.calls.clear()
            self.register(level=level)
            for _ in range(3):
                self.decision()
            self.failure()
            self.deliver(self.NIGHT + 60)
            self.assertEqual(self.sent(), [], level)
            self.deliver(self.MORNING)
            self.deliver(self.MORNING + 3600)
            self.assertEqual(len(self.sent()), 1, level)
            payload = self.sent()[0]
            self.assertEqual({key: payload['coconut'][key] for key in ('version', 'device_id', 'kind', 'count')},
                             dict(version=1, device_id=self.device, kind='group', count=4))
            self.assertEqual(payload['aps']['alert'], dict(title='Coconut', body='3 decisions and 1 failed task are waiting'))
            self.assertEqual(self.service.db.execute('SELECT count(*) FROM push_outbox').fetchone()[0], 1)

    def test_a_card_that_needs_attention_now_sends_at_once_and_alone(self):
        self.decision()
        urgent = self.decision(urgent=True)
        self.deliver(self.NIGHT)
        self.assertEqual([payload['coconut'].get('decision_id') for payload in self.sent()], [urgent['id']])
        self.assertEqual(self.items(), [('decision', 0, 'waiting'), ('decision', 1, 'sent')])

    def test_only_a_card_can_need_attention_now(self):
        self.failure()
        self.deliver(self.NIGHT + 60)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.items(), [('failed', 0, 'waiting')])

    def test_grouped_settles_a_burst_and_limits_the_rate_inside_work_hours(self):
        self.decision(ts=self.NOON)
        self.decision(ts=self.NOON + 10)
        self.deliver(self.NOON + 29)
        self.assertEqual(self.sent(), [])
        self.deliver(self.NOON + 30)
        self.assertEqual([payload['coconut'].get('count') for payload in self.sent()], [2])
        # The next card waits until ten minutes after the last notification.
        later = self.decision(ts=self.NOON + 60)
        self.deliver(self.NOON + 629)
        self.assertEqual(len(self.sent()), 1)
        self.deliver(self.NOON + 630)
        self.assertEqual(self.sent()[1]['coconut']['decision_id'], later['id'])

    def test_every_card_notifies_a_burst_one_card_at_a_time(self):
        self.register(level='every')
        first = self.decision(ts=self.NOON)
        second = self.decision(ts=self.NOON + 1)
        self.deliver(self.NOON + 3)
        self.assertEqual([payload['coconut']['decision_id'] for payload in self.sent()], [first['id'], second['id']])
        self.assertTrue(all('count' not in payload['coconut'] for payload in self.sent()))

    def test_the_ten_minute_gap_counts_from_the_delivery_and_waits_for_a_retry(self):
        self.decision(ts=self.NOON)
        self.transport.result = (503, 'Unavailable')
        self.deliver(self.NOON + 30)
        self.assertEqual(len(self.sent()), 1)
        self.decision(ts=self.NOON + 60)
        # The first notification still retries, so the new card does not form a second group.
        with self.at(self.NOON + 700):
            with self.service.transaction():
                self.service.push.plan(self.NOON + 700)
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM push_outbox').fetchone()[0], 1)
        self.transport.result = (200, 'Accepted')
        self.service.db.execute('UPDATE push_outbox SET not_before=0')
        self.deliver(self.NOON + 700)
        self.assertEqual(len(self.sent()), 2)
        self.deliver(self.NOON + 1299)
        self.assertEqual(len(self.sent()), 2)
        self.deliver(self.NOON + 1300)
        self.assertEqual(len(self.sent()), 3)

    def test_a_retrying_group_does_not_hold_new_cards_at_every_card(self):
        self.register(level='every')
        self.decision()
        self.decision()
        self.transport.result = (503, 'Unavailable')
        self.deliver(self.MORNING)
        self.assertEqual([payload['coconut'].get('count') for payload in self.sent()], [2])
        self.transport.result = (200, 'Accepted')
        later = self.decision(ts=self.MORNING + 60)
        self.deliver(self.MORNING + 62)
        self.assertEqual(self.sent()[-1]['coconut'].get('decision_id'), later['id'])
        self.assertNotIn('count', self.sent()[-1]['coconut'])

    def test_a_retry_stops_when_the_bot_is_muted(self):
        route = {'route_id': 'a' * 64, 'send_token': 'b' * 64, 'public_key': base64.b64encode(b'x' * 32).decode()}
        relay = EncryptedTransport()
        self.service.push.relay = relay
        self.register(relay=route, schedule=DEFAULT_SCHEDULE)
        self.decision(ts=self.NOON)
        relay.result = (503, 'Unavailable')
        self.deliver(self.NOON + 30)
        self.assertEqual(len(relay.sent), 1)
        self.write('PATCH', f'/v1/bots/{self.bot["id"]}', {'notifications': False})
        relay.result = (200, 'Accepted')
        self.service.db.execute('UPDATE push_outbox SET not_before=0')
        self.deliver(self.NOON + 3600)
        self.assertEqual(len(relay.sent), 1)
        self.assertEqual(self.service.db.execute('SELECT status,reason FROM push_outbox').fetchone(), ('discarded', 'NoLongerRelevant'))

    def test_a_group_still_opens_the_first_card_in_an_older_app(self):
        first, _ = self.decision(), self.decision()
        self.deliver(self.MORNING)
        target = self.sent()[0]['coconut']
        self.assertEqual((target['kind'], target['decision_id'], target['bot_id']), ('group', first['id'], self.bot['id']))
        self.assertEqual(target['task_id'], first['task_id'])

    def test_every_card_notifies_each_card_inside_work_hours(self):
        self.register(level='every')
        first = self.decision(ts=self.NOON)
        self.deliver(self.NOON + 2)
        second = self.decision(ts=self.NOON + 5)
        self.deliver(self.NOON + 7)
        self.assertEqual([payload['coconut']['decision_id'] for payload in self.sent()], [first['id'], second['id']])

    def test_only_urgent_keeps_other_items_in_inbox(self):
        self.register(level='urgent_only')
        self.decision(ts=self.NOON)
        self.failure(ts=self.NOON)
        urgent = self.decision(urgent=True, ts=self.NOON)
        self.deliver(self.NOON + 3600)
        self.assertEqual([payload['coconut']['decision_id'] for payload in self.sent()], [urgent['id']])
        self.assertEqual(self.items(), [('decision', 1, 'sent')])

    def test_choosing_only_urgent_drops_waiting_items(self):
        self.decision()
        self.register(level='urgent_only')
        self.assertEqual(self.items(), [('decision', 0, 'dropped')])
        self.deliver(self.MORNING)
        self.assertEqual(self.sent(), [])

    def test_messages_off_also_mutes_failed_tasks(self):
        self.register(messages=False)
        self.failure()
        self.assertEqual(self.items(), [])

    def test_an_answered_card_leaves_the_group_and_a_group_of_one_reads_as_the_card(self):
        first, second = self.decision(), self.decision()
        first['status'] = 'approved'
        self.service._put('decisions', first)
        self.deliver(self.MORNING)
        self.assertEqual([payload['coconut'].get('decision_id') for payload in self.sent()], [second['id']])
        self.assertEqual(self.items(), [('decision', 0, 'dropped'), ('decision', 0, 'sent')])

    def test_a_group_that_lost_every_item_is_discarded(self):
        decision = self.decision()
        self.deliver(self.MORNING - 1)
        with self.at(self.MORNING):
            with self.service.transaction():
                self.service.push.plan(self.MORNING)
        decision['status'] = 'approved'
        self.service._put('decisions', decision)
        self.deliver(self.MORNING)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.service.db.execute('SELECT status,reason FROM push_outbox').fetchall(), [('discarded', 'NoLongerRelevant')])

    def test_weekend_items_wait_for_the_next_chosen_weekday(self):
        self.register(schedule=dict(weekdays=[1, 2, 3, 4, 5]))
        saturday_noon = instant(2026, 7, 18, 12)
        self.decision(ts=saturday_noon)
        self.deliver(saturday_noon + 3600)
        self.assertEqual(self.sent(), [])
        self.deliver(instant(2026, 7, 20, 9))
        self.assertEqual(len(self.sent()), 1)

    def test_a_waiting_item_follows_a_new_schedule_at_once(self):
        self.decision()
        # Midnight in New York is already noon in Tokyo.
        self.register(schedule=dict(DEFAULT_SCHEDULE, timezone='Asia/Tokyo', start='12:00', end='16:00'))
        self.deliver(self.NIGHT + 60)
        self.assertEqual(len(self.sent()), 1)

    def test_turning_work_hours_off_sends_waiting_items(self):
        self.decision()
        self.register(schedule=dict(enabled=False, start='18:00', end='09:00', timezone='Mars/Base'))
        self.assertFalse(self.service.push.schedule(self.device)['enabled'])
        self.deliver(self.NIGHT + 60)
        self.assertEqual(len(self.sent()), 1)

    def test_waiting_item_survives_restart_and_sends_once(self):
        self.decision()
        self.deliver(self.NIGHT + 60)
        self.service.close()
        self.start()
        self.assertEqual(self.service.push.schedule(self.device), DEFAULT_SCHEDULE)
        self.deliver(self.MORNING - 1)
        self.assertEqual(self.sent(), [])
        self.deliver(self.MORNING)
        self.deliver(self.MORNING + 3600)
        self.assertEqual(len(self.sent()), 1)

    def test_opening_after_the_clock_change_follows_local_time(self):
        night = instant(2026, 10, 31, 23)
        self.decision(ts=night)
        opening = instant(2026, 11, 1, 9)
        self.assertEqual(local(opening), '2026-11-01T09:00:00-05:00')
        self.deliver(opening - 1)
        self.assertEqual(self.sent(), [])
        self.deliver(opening)
        self.assertEqual(len(self.sent()), 1)

    def test_an_item_that_waits_more_than_a_week_stays_only_in_inbox(self):
        self.register(schedule=dict(weekdays=[3]))
        self.decision(ts=instant(2026, 7, 15, 19))
        self.service.db.execute('UPDATE push_items SET created=created-8*86400')
        self.deliver(instant(2026, 7, 22, 9))
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.service.db.execute('SELECT status,reason FROM push_items').fetchone(), ('dropped', 'Expired'))

    def test_sealed_group_retries_the_same_bytes_after_hours(self):
        route = {'route_id': 'a' * 64, 'send_token': 'b' * 64, 'public_key': base64.b64encode(b'x' * 32).decode()}
        relay = EncryptedTransport()
        self.service.push.relay = relay
        self.register(relay=route, schedule=DEFAULT_SCHEDULE)
        self.decision(ts=self.MORNING - 60)
        self.decision(ts=self.MORNING - 50)
        relay.result = (503, 'Unavailable')
        self.deliver(self.MORNING)
        self.assertEqual(len(relay.sealed), 1)
        expires = relay.sent[0][4]
        self.assertTrue(self.MORNING < expires <= self.MORNING + 86400)
        relay.result = (200, 'Accepted')
        self.deliver(instant(2026, 7, 16, 23))
        self.assertEqual((len(relay.sealed), len(relay.sent)), (1, 2))
        self.assertEqual(relay.sent[1][1:], relay.sent[0][1:])
        self.assertEqual(self.service.db.execute('SELECT status FROM push_outbox').fetchall(), [('accepted',)])

    def test_schedule_weekdays_persist_through_partial_updates_and_restart(self):
        self.register(schedule=dict(weekdays=[1, 3], honor_routine=True))
        self.register(schedule=dict(start='08:00'))
        expected = dict(DEFAULT_SCHEDULE, weekdays=[1, 3], start='08:00')
        self.assertEqual(self.service.push.schedule(self.device), expected)
        self.register(schedule=dict(enabled=True, start='08:00', end='17:00', timezone='America/New_York'))
        self.assertEqual(self.service.push.schedule(self.device), dict(expected, end='17:00'))
        self.service.close()
        self.start()
        self.assertEqual(self.service.push.schedule(self.device), dict(expected, end='17:00'))

    def test_invalid_schedule_updates_are_rejected_and_change_nothing(self):
        before = self.service.push.schedule(self.device)
        for bad in (dict(weekdays=[]), dict(weekdays=[0]), dict(weekdays=[True]), dict(weekdays='1'), dict(start='19:00'),
                    dict(extra=True)):
            with self.subTest(bad=bad), self.assertRaises(ServiceError) as raised:
                self.register(schedule=bad)
            self.assertEqual(raised.exception.status, 400)
            self.assertEqual(self.service.push.schedule(self.device), before)
        self.register(schedule=dict(enabled=False, weekdays=[], honor_routine='x'))
        self.assertEqual(self.service.push.schedule(self.device), dict(before, enabled=False))

    def test_stored_row_without_new_fields_gets_safe_defaults(self):
        old = dict(enabled=True, start='08:00', end='17:00', timezone='Europe/Paris')
        self.service.db.execute('UPDATE push_schedules SET data=? WHERE device_id=?', (__import__('json').dumps(old), self.device))
        self.assertEqual(self.service.push.schedule(self.device), dict(DEFAULT_SCHEDULE, **old))
        self.service.db.execute('UPDATE push_schedules SET data=? WHERE device_id=?', ('{"weekdays": []}', self.device))
        self.assertEqual(self.service.push.schedule(self.device), LEGACY_SCHEDULE)

    def test_device_without_a_stored_schedule_has_no_work_hours(self):
        self.service.db.execute('DELETE FROM push_schedules')
        self.assertFalse(self.service.push.schedule(self.device)['enabled'])
        self.decision()
        self.deliver(self.NIGHT + 30)
        self.assertEqual(len(self.sent()), 1)
        self.register()
        self.assertEqual(self.service.push.schedule(self.device), LEGACY_SCHEDULE)

    def test_old_outbox_rows_become_items_and_nothing_sends_twice(self):
        path = tempfile.mkdtemp()
        db = sqlite3.connect(f'{path}/old.db')
        db.executescript('''
        CREATE TABLE push_outbox (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, event_key TEXT NOT NULL,
            bot_id TEXT NOT NULL, task_id TEXT NOT NULL, decision_id TEXT, kind TEXT NOT NULL,
            turn_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            not_before REAL NOT NULL, expires REAL NOT NULL, reason TEXT,
            importance TEXT, explicit_importance TEXT, importance_reason TEXT, UNIQUE(device_id, event_key));
        INSERT INTO push_outbox VALUES ('a','d','a','b','t','x','decision','u','queued',0,5,9e9,NULL,'urgent','urgent','Explicit');
        INSERT INTO push_outbox VALUES ('b','d','b','b','t','y','decision','u','queued',0,6,9e9,NULL,'important','routine','KindFloor');
        INSERT INTO push_outbox VALUES ('c','d','c','b','t',NULL,'completed','u','accepted',1,7,9e9,'Accepted',NULL,NULL,NULL);
        CREATE TABLE push_preferences (device_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        INSERT INTO push_preferences VALUES ('d', '{"decisions": false, "messages": true}');
        INSERT INTO push_preferences VALUES ('e', '{"decisions": true, "messages": false}');
        INSERT INTO push_preferences VALUES ('f', '{"decisions": "yes"}');''')
        from coconut.push import PushDelivery
        for _ in range(2):
            PushDelivery(SimpleNamespace(db=db, root=path))
            self.assertEqual(db.execute('SELECT event_key,urgent,status,created FROM push_items ORDER BY event_key').fetchall(),
                             [('a', 1, 'waiting', 5), ('b', 0, 'waiting', 6), ('c', 0, 'sent', 7)])
            self.assertEqual(db.execute('SELECT count(*) FROM push_outbox').fetchone()[0], 0)
            self.assertNotIn('kind', {row[1] for row in db.execute('PRAGMA table_info(push_outbox)')})
            self.assertEqual(db.execute('SELECT device_id,data FROM push_preferences ORDER BY device_id').fetchall(),
                             [('d', '{"level": "urgent_only", "messages": true}'), ('e', '{"level": "grouped", "messages": false}')])
        db.close()

    def test_legacy_decision_path_keeps_only_a_true_needs_attention_now(self):
        for given, expected in ((True, True), ('true', False), (1, False), (None, False)):
            payload = dict(title=f'Go {given}?', action_type='create_file', needs_attention_now=given,
                           action=dict(type='create_file', filename='a.txt', content='A'))
            self.service.provider.start_task = lambda task, bot, history, emit, payload=payload: emit('decision', payload)
            task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text='Go', mode='queue'))
            self.service._run_task(task['id'])
            decision = next(d for d in self.service.snapshot()['decisions'] if d['task_id'] == task['id'])
            self.assertIs(decision['needs_attention_now'], expected, given)
