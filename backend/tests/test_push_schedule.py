import base64
from datetime import datetime
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4
from zoneinfo import ZoneInfo
from coconut.push_schedule import DEFAULT_SCHEDULE, LEGACY_SCHEDULE, in_window, merge, next_opening, validate
from coconut.service import Service, ServiceError
from test_push import RecordingTransport
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


class OffHoursDeliveryTests(unittest.TestCase):
    NIGHT = instant(2026, 7, 15, 23)
    MORNING = instant(2026, 7, 16, 9)

    legacy = False

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.start()
        self.bot = self.write('POST', '/v1/bots', {'name': 'Desk'})

    def start(self):
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.transport = RecordingTransport()
        self.service.push.transport = self.transport
        if not hasattr(self, 'device'):
            self.device = self.service.pair({'code': self.service.pairing_code()})['device_id']
            self.register(schedule=DEFAULT_SCHEDULE)

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), self.device)

    def register(self, **values):
        preferences = {} if self.legacy else dict(preferences=dict(decisions=True, messages=True))
        return self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True, **preferences, **values))

    def at(self, ts):
        return mock.patch('coconut.push.time', SimpleNamespace(time=lambda: ts))

    def decision(self, urgent=False, ts=None):
        """Queue the decision's alert at `ts`. `urgent` is the bot's needs_attention_now."""
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text='Write report', mode='queue'))
        with self.at(self.queued(ts)):
            self.service._run_task(task['id'])
            decision = self.service._get('decisions', next(d['id'] for d in self.service.snapshot()['decisions']
                                                          if d['task_id'] == task['id']))
            self.service.db.execute('DELETE FROM push_outbox')
            decision['needs_attention_now'] = urgent
            self.service._put('decisions', decision)
            self.service.push.record('decision.created', self.service._public(decision), self.bot['id'], task['id'])
        return decision

    def queued(self, ts):
        """Alerts are queued ten seconds before the time they are meant to be delivered, past the two second settling delay."""
        return (ts or self.NIGHT) - 10

    def deliver(self, ts):
        """Run delivery with the clock at `ts`. Stored not_before values decide what is due; nothing here edits them."""
        with self.at(ts):
            while self.service.push.deliver_one():
                pass

    def release(self, ts, **values):
        """Save settings at `ts` and check that saving them, not delivery, made the held alert due."""
        held = self.service.db.execute("SELECT count(*) FROM push_outbox WHERE deferred_reason='OffHours'").fetchone()[0]
        self.assertEqual(held, 1)
        with self.at(ts):
            self.register(**values)
        self.assertEqual(self.service.db.execute('SELECT not_before,deferred_reason,deferred_until FROM push_outbox').fetchone(), (ts, None, None))

    def row(self):
        return self.service.db.execute('SELECT status,reason,urgent,deferred_reason,deferred_until,attempts FROM push_outbox').fetchone()

    def task_alert(self, kind, ts=None):
        """Queue the completion or failure alert of a finished task at `ts`. Task alerts exist for phones without category preferences."""
        self.service.db.execute('DELETE FROM push_preferences')
        self.legacy = True
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text='Write report', mode='queue'))
        with self.at(self.queued(ts)):
            self.service._run_task(task['id'])
            task = self.service._get('tasks', task['id'])
            task['status'] = kind
            self.service._put('tasks', task)
            self.service.db.execute('DELETE FROM push_outbox')
            self.service.push.record('task.updated', dict(status=kind), self.bot['id'], task['id'])
        return task

    def test_decision_waits_for_opening_and_stays_in_inbox(self):
        decision = self.decision()
        self.deliver(self.NIGHT)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.row(), ('queued', None, 0, 'OffHours', self.MORNING, 0))
        self.assertEqual([d['id'] for d in self.service.snapshot()['decisions'] if d['status'] == 'pending'], [decision['id']])
        self.deliver(self.MORNING - 1)
        self.assertEqual(self.transport.calls, [])
        self.deliver(self.MORNING)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.row()[:2], ('accepted', 'Accepted'))

    def test_only_a_true_needs_attention_now_skips_work_hours(self):
        for value in (False, None, 'true', 1, 'urgent'):
            self.service.db.execute('DELETE FROM push_outbox')
            self.decision(value)
            self.deliver(self.NIGHT)
            self.assertEqual(self.row()[:4], ('queued', None, 0, 'OffHours'), value)
        self.service.db.execute('DELETE FROM push_outbox')
        self.decision(True)
        self.deliver(self.NIGHT)
        self.assertEqual(self.row()[:4], ('accepted', 'Accepted', 1, None))
        self.assertEqual(len(self.transport.calls), 1)

    def test_failures_completions_and_messages_wait_even_when_marked(self):
        for kind in ('failed', 'completed'):
            self.service.db.execute('DELETE FROM push_outbox')
            task = self.task_alert(kind)
            self.service.db.execute('DELETE FROM push_outbox')
            with self.at(self.queued(None)):
                self.service.push.record('task.updated', dict(status=kind, needs_attention_now=True), self.bot['id'], task['id'])
            self.deliver(self.NIGHT)
            self.assertEqual(self.row(), ('queued', None, 0, 'OffHours', self.MORNING, 0), kind)
        self.service.db.execute('DELETE FROM push_outbox')
        task = self.service._get('tasks', self.service.db.execute('SELECT id FROM tasks').fetchone()[0])
        message = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=task['id'], role='assistant', kind='text', text='Hello')
        self.service._put('messages', message)
        self.legacy = False
        self.register(schedule=DEFAULT_SCHEDULE)
        with self.at(self.queued(None)):
            self.service.push.record('message.created', dict(self.service._get('messages', message['id']), needs_attention_now=True),
                                     self.bot['id'], task['id'])
        self.deliver(self.NIGHT)
        self.assertEqual(self.row(), ('queued', None, 0, 'OffHours', self.MORNING, 0))
        self.assertEqual(self.transport.calls, [])

    def test_weekend_alert_waits_for_the_next_chosen_weekday(self):
        self.register(schedule=dict(weekdays=[1, 2, 3, 4, 5]))
        saturday_noon = instant(2026, 7, 18, 12)
        self.decision(ts=saturday_noon)
        self.deliver(saturday_noon + 2)
        self.assertEqual(self.transport.calls, [])
        monday = instant(2026, 7, 20, 9)
        self.assertEqual(self.row()[3:5], ('OffHours', monday))
        self.deliver(monday)
        self.assertEqual(len(self.transport.calls), 1)

    def test_schedule_weekdays_persist_through_partial_updates_and_restart(self):
        self.register(schedule=dict(weekdays=[1, 3], honor_routine=True))
        self.register(schedule=dict(start='08:00'))
        expected = dict(DEFAULT_SCHEDULE, weekdays=[1, 3], start='08:00')
        self.assertEqual(self.service.push.schedule(self.device), expected)
        # A client that predates the new fields leaves them alone.
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

    def test_routine_sends_during_work_hours(self):
        noon = instant(2026, 7, 15, 12)
        self.decision(ts=noon)
        self.deliver(noon + 2)
        self.assertEqual(len(self.transport.calls), 1)

    def test_disabled_schedule_restores_immediate_push_and_releases_waiting_alerts(self):
        self.task_alert('completed')
        self.deliver(self.NIGHT)
        self.release(self.NIGHT + 10, schedule=dict(enabled=False))
        self.deliver(self.NIGHT + 60)
        self.assertEqual(len(self.transport.calls), 1)
        self.task_alert('completed')
        self.deliver(self.NIGHT)
        self.assertEqual(len(self.transport.calls), 2)

    def test_disabling_with_an_invalid_draft_still_restores_immediate_push(self):
        self.task_alert('completed')
        self.deliver(self.NIGHT)
        self.release(self.NIGHT + 10, schedule=dict(enabled=False, start='18:00', end='09:00', timezone='Mars/Base'))
        self.assertFalse(self.service.push.schedule(self.device)['enabled'])
        self.deliver(self.NIGHT + 60)
        self.assertEqual(len(self.transport.calls), 1)

    def test_schedule_change_reevaluates_queued_alerts(self):
        self.task_alert('completed')
        self.deliver(self.NIGHT)
        # Midnight in New York is already morning in Tokyo.
        with self.at(self.NIGHT + 10):
            self.register(schedule=dict(DEFAULT_SCHEDULE, timezone='Asia/Tokyo', start='12:00', end='16:00'))
        self.deliver(self.NIGHT + 20)
        self.assertEqual(len(self.transport.calls), 1)
        # A narrower window defers a later alert again to the new opening.
        self.register(schedule=DEFAULT_SCHEDULE)
        self.task_alert('completed')
        self.deliver(self.NIGHT)
        with self.at(self.NIGHT + 10):
            self.register(schedule=dict(DEFAULT_SCHEDULE, start='10:00', end='11:00'))
        self.deliver(self.NIGHT + 20)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.row()[4], instant(2026, 7, 16, 10))

    def test_early_release_clears_the_deferral_audit_and_keeps_a_retry_backoff(self):
        self.task_alert('completed')
        self.deliver(self.NIGHT)
        self.assertEqual(self.row()[3], 'OffHours')
        with self.at(self.NIGHT + 10):
            self.register(schedule=dict(enabled=False))
        self.assertEqual(self.row()[3:5], (None, None))
        # A transient failure backs off. Saving the schedule again must not shorten that delay.
        self.transport.result = (503, 'Unavailable')
        self.deliver(self.NIGHT + 20)
        backoff = self.service.db.execute('SELECT not_before FROM push_outbox').fetchone()[0]
        self.assertGreater(backoff, self.NIGHT + 20)
        with self.at(self.NIGHT + 30):
            self.register(schedule=dict(enabled=False))
        self.assertEqual(self.service.db.execute('SELECT not_before FROM push_outbox').fetchone()[0], backoff)

    def test_delivery_after_opening_clears_the_deferral_audit(self):
        self.task_alert('completed')
        self.deliver(self.NIGHT)
        self.deliver(self.MORNING)
        self.assertEqual(self.row()[:5], ('accepted', 'Accepted', 0, None, None))

    def test_a_row_with_a_sealed_envelope_is_never_deferred_on_retry(self):
        route = {'route_id': 'a' * 64, 'send_token': 'b' * 64, 'public_key': base64.b64encode(b'x' * 32).decode()}
        relay = EncryptedTransport()
        self.service.push.relay = relay
        self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True, relay=route,
                                                  schedule=DEFAULT_SCHEDULE))
        self.task_alert('completed', ts=self.MORNING)
        relay.result = (503, 'Unavailable')
        self.deliver(self.MORNING)
        self.assertEqual(len(relay.sealed), 1)
        # The relay is down, so the sealed row backs off and retries. Retrying after hours must send the same bytes, not wait.
        self.assertEqual(self.row()[0], 'queued')
        expires = self.service.db.execute('SELECT expires FROM push_outbox').fetchone()[0]
        self.deliver(instant(2026, 7, 16, 23))
        self.assertEqual(self.row()[3:5], (None, None))
        self.assertEqual(len(relay.sealed), 1)
        self.assertEqual(len(relay.sent), 2)
        self.assertEqual(self.service.db.execute('SELECT expires FROM push_outbox').fetchone()[0], expires)

    def test_early_release_through_the_relay_expires_within_a_day_of_delivery(self):
        route = {'route_id': 'a' * 64, 'send_token': 'b' * 64, 'public_key': base64.b64encode(b'x' * 32).decode()}
        relay = EncryptedTransport()
        self.service.push.relay = relay
        self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True, relay=route,
                                                  schedule=DEFAULT_SCHEDULE))
        self.task_alert('completed')
        self.deliver(self.NIGHT)
        self.assertEqual(self.row()[3:5], ('OffHours', self.MORNING))
        held = self.service.db.execute('SELECT expires FROM push_outbox').fetchone()[0]
        self.assertGreater(held, self.NIGHT + 86400)
        self.release(self.NIGHT + 10, relay=route, schedule=dict(enabled=False))
        sent_at = self.NIGHT + 20
        self.deliver(sent_at)
        self.assertEqual(len(relay.sent), 1)
        expires = relay.sent[0][4]
        # The relay rejects an expiry that is not in the future or is more than 24 hours past the send (relay/src/device.js).
        self.assertTrue(sent_at < expires <= sent_at + 86400, expires - sent_at)
        self.assertEqual(self.service.db.execute('SELECT expires FROM push_outbox').fetchone()[0], expires)
        # A retry of the sealed envelope keeps that expiry.
        relay.result = (503, 'Unavailable')
        self.service.db.execute("UPDATE push_outbox SET status='queued',not_before=0")
        self.deliver(sent_at + 3600)
        self.assertEqual(relay.sent[1][4], expires)
        self.assertEqual(len(relay.sealed), 1)

    def test_deferred_alert_survives_restart_and_sends_once(self):
        self.task_alert('completed')
        self.deliver(self.NIGHT)
        self.service.close()
        self.start()
        self.assertEqual(self.service.push.schedule(self.device), DEFAULT_SCHEDULE)
        # The stored not_before alone keeps the alert waiting.
        self.assertEqual(self.service.db.execute('SELECT not_before FROM push_outbox').fetchone()[0], self.MORNING)
        self.deliver(self.NIGHT + 60)
        self.deliver(self.MORNING - 1)
        self.assertEqual(self.transport.calls, [])
        self.deliver(self.MORNING)
        self.deliver(self.MORNING + 60)
        self.assertEqual(len(self.transport.calls), 1)

    def test_deferred_alert_after_restart_follows_a_schedule_changed_before_it(self):
        self.task_alert('completed')
        self.deliver(self.NIGHT)
        self.release(self.NIGHT + 10, schedule=dict(start='10:00', end='11:00'))
        self.service.close()
        self.start()
        self.deliver(self.NIGHT + 20)
        self.assertEqual(self.row()[3:5], ('OffHours', instant(2026, 7, 16, 10)))
        self.deliver(self.MORNING)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.row()[4], instant(2026, 7, 16, 10))
        self.deliver(instant(2026, 7, 16, 10))
        self.assertEqual(len(self.transport.calls), 1)

    def test_alert_deferred_across_the_clock_change_opens_at_local_time(self):
        night = instant(2026, 10, 31, 23)
        self.task_alert('completed', night)
        self.deliver(night)
        opening = instant(2026, 11, 1, 9)
        self.assertEqual(self.row()[4], opening)
        self.assertEqual(local(opening), '2026-11-01T09:00:00-05:00')
        self.deliver(opening - 1)
        self.assertEqual(self.transport.calls, [])
        self.deliver(opening)
        self.assertEqual(len(self.transport.calls), 1)

    def test_answered_decision_and_muted_category_still_discard_deferred_alerts(self):
        decision = self.decision()
        self.deliver(self.NIGHT)
        decision['status'] = 'approved'
        self.service._put('decisions', decision)
        self.deliver(self.MORNING)
        self.assertEqual(self.row()[:2], ('discarded', 'NoLongerRelevant'))
        self.decision()
        self.deliver(self.NIGHT)
        self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True,
                                                  preferences=dict(decisions=False, messages=True)))
        self.assertEqual(self.row()[:2], ('discarded', 'Disabled'))
        self.assertEqual(self.transport.calls, [])

    def test_a_deferred_alert_leaves_the_task_and_decision_untouched(self):
        decision = self.decision()
        before = self.service._get('decisions', decision['id'])
        self.deliver(self.NIGHT)
        self.assertEqual(self.service._get('decisions', decision['id']), before)

    def test_device_without_a_stored_schedule_keeps_immediate_delivery(self):
        self.service.db.execute('DELETE FROM push_schedules')
        self.assertEqual(self.service.push.schedule(self.device), LEGACY_SCHEDULE)
        self.assertFalse(LEGACY_SCHEDULE['enabled'])
        self.decision()
        self.deliver(self.NIGHT)
        self.assertEqual(self.row()[:2] + self.row()[3:4], ('accepted', 'Accepted', None))
        # Registering without a schedule, as an old app does, leaves it off. The updated app turns it on.
        self.register()
        self.assertEqual(self.service.push.schedule(self.device), LEGACY_SCHEDULE)
        self.register(schedule=DEFAULT_SCHEDULE)
        self.assertEqual(self.service.push.schedule(self.device), DEFAULT_SCHEDULE)

    def test_encrypted_relay_defers_before_sealing_and_sends_once_when_open(self):
        route = {'route_id': 'a' * 64, 'send_token': 'b' * 64, 'public_key': base64.b64encode(b'x' * 32).decode()}
        relay = EncryptedTransport()
        self.service.push.relay = relay
        self.write('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True, relay=route,
                                                  schedule=DEFAULT_SCHEDULE))
        self.decision()
        self.deliver(self.NIGHT)
        self.assertEqual((relay.sealed, relay.sent), ([], []))
        self.assertEqual(self.row()[:4], ('queued', None, 0, 'OffHours'))
        self.deliver(self.MORNING)
        self.deliver(self.MORNING + 60)
        self.assertEqual((len(relay.sealed), len(relay.sent)), (1, 1))
        self.assertEqual(self.row()[:2], ('accepted', 'Accepted'))

    def test_old_database_drops_importance_columns_and_keeps_only_urgent(self):
        path = tempfile.mkdtemp()
        db = sqlite3.connect(f'{path}/old.db')
        db.executescript('''
        CREATE TABLE push_outbox (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, event_key TEXT NOT NULL,
            bot_id TEXT NOT NULL, task_id TEXT NOT NULL, decision_id TEXT, kind TEXT NOT NULL,
            turn_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            not_before REAL NOT NULL, expires REAL NOT NULL, reason TEXT,
            importance TEXT, explicit_importance TEXT, importance_reason TEXT, UNIQUE(device_id, event_key));
        INSERT INTO push_outbox VALUES ('a','d','a','b','t','x','decision','u','queued',0,0,9e9,NULL,'urgent','urgent','Explicit');
        INSERT INTO push_outbox VALUES ('b','d','b','b','t','y','decision','u','queued',0,0,9e9,NULL,'important','routine','KindFloor');
        INSERT INTO push_outbox VALUES ('c','d','c','b','t',NULL,'completed','u','queued',0,0,9e9,NULL,NULL,NULL,NULL);''')
        from coconut.push import PushDelivery
        PushDelivery(SimpleNamespace(db=db, root=path))
        columns = {row[1] for row in db.execute('PRAGMA table_info(push_outbox)')}
        self.assertTrue({'urgent', 'deferred_reason', 'deferred_until'} <= columns)
        self.assertFalse({'importance', 'explicit_importance', 'importance_reason'} & columns)
        self.assertEqual(db.execute('SELECT id,urgent,deferred_until FROM push_outbox ORDER BY id').fetchall(),
                         [('a', 1, None), ('b', 0, None), ('c', 0, None)])
        PushDelivery(SimpleNamespace(db=db, root=path))
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
