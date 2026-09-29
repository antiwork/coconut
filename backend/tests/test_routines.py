from datetime import datetime, timezone
import unittest
from zoneinfo import ZoneInfo

from coconut.routines import ScheduleError, next_run, validate_schedule


UTC = timezone.utc


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class RoutineScheduleTests(unittest.TestCase):
    def assert_next(self, cron, zone, after, expected):
        result = next_run(cron, zone, instant(after))
        self.assertEqual(result, instant(expected))
        self.assertIs(result.tzinfo, UTC)
        self.assertGreater(result, instant(after))

    def test_daily_local_time_uses_the_routine_timezone(self):
        self.assert_next('0 9 * * *', 'America/New_York', '2026-09-08T12:00:00Z', '2026-09-08T13:00:00Z')
        self.assert_next('0 9 * * *', 'America/New_York', '2026-01-08T12:00:00Z', '2026-01-08T14:00:00Z')

    def test_weekdays_skip_the_weekend(self):
        self.assert_next('30 8 * * 1-5', 'America/New_York', '2026-09-11T12:30:00Z', '2026-09-14T12:30:00Z')

    def test_hourly_and_every_minute_boundaries(self):
        self.assert_next('0 * * * *', 'UTC', '2026-09-08T12:00:00Z', '2026-09-08T13:00:00Z')
        self.assert_next('* * * * *', 'UTC', '2026-09-08T12:00:00Z', '2026-09-08T12:01:00Z')
        self.assert_next('* * * * *', 'UTC', '2026-09-08T12:00:59.999999Z', '2026-09-08T12:01:00Z')
        self.assert_next('* * * * *', 'UTC', '2026-09-08T11:59:59.999999Z', '2026-09-08T12:00:00Z')

    def test_lists_ranges_and_steps(self):
        self.assert_next('5,20-40/10 8-10 * * *', 'UTC', '2026-09-08T08:20:00Z', '2026-09-08T08:30:00Z')
        self.assert_next('*/15 * * * *', 'UTC', '2026-09-08T08:45:00Z', '2026-09-08T09:00:00Z')
        self.assert_next('5/15 * * * *', 'UTC', '2026-09-08T08:35:00Z', '2026-09-08T08:50:00Z')
        self.assert_next('0 */23 * * *', 'UTC', '2026-09-08T23:00:00Z', '2026-09-09T00:00:00Z')

    def test_day_of_month_and_weekday_use_or_when_both_are_restricted(self):
        self.assert_next('0 9 1 * 1', 'UTC', '2026-09-01T09:00:00Z', '2026-09-07T09:00:00Z')
        self.assert_next('0 9 1 * 1', 'UTC', '2026-09-28T09:00:00Z', '2026-10-01T09:00:00Z')

    def test_day_wildcard_does_not_override_the_weekday(self):
        self.assert_next('0 9 * * 1', 'UTC', '2026-09-08T00:00:00Z', '2026-09-14T09:00:00Z')
        self.assert_next('0 9 15 * *', 'UTC', '2026-09-08T00:00:00Z', '2026-09-15T09:00:00Z')

    def test_stepped_day_wildcards_keep_cron_and_semantics(self):
        self.assert_next('0 9 */2 * 1', 'UTC', '2026-09-07T09:00:00Z', '2026-09-21T09:00:00Z')
        self.assert_next('0 9 15 * */2', 'UTC', '2026-09-01T00:00:00Z', '2026-09-15T09:00:00Z')

    def test_sunday_accepts_zero_and_seven(self):
        for weekday in ('0', '7', '0,7'):
            with self.subTest(weekday=weekday):
                self.assert_next(f'0 9 * * {weekday}', 'UTC', '2026-09-08T00:00:00Z', '2026-09-13T09:00:00Z')

    def test_impossible_month_day_is_valid_with_restricted_weekday_or(self):
        validate_schedule('0 9 31 2 1', 'UTC')
        self.assert_next('0 9 31 2 1', 'UTC', '2026-02-01T00:00:00Z', '2026-02-02T09:00:00Z')

    def test_year_month_and_leap_year_boundaries(self):
        self.assert_next('0 0 1 1 *', 'UTC', '2026-12-31T23:59:59Z', '2027-01-01T00:00:00Z')
        self.assert_next('0 0 31 * *', 'UTC', '2026-04-01T00:00:00Z', '2026-05-31T00:00:00Z')
        self.assert_next('0 0 29 2 *', 'UTC', '2096-02-29T00:00:00Z', '2104-02-29T00:00:00Z')

    def test_rare_leap_day_with_stepped_weekday_searches_beyond_eight_years(self):
        self.assert_next('0 0 29 2 */7', 'UTC', '2004-02-29T00:00:00Z', '2032-02-29T00:00:00Z')

    def test_spring_missing_time_is_skipped(self):
        self.assert_next('30 2 * * *', 'America/New_York', '2026-03-07T07:30:00Z', '2026-03-09T06:30:00Z')

    def test_fall_repeated_time_uses_only_the_first_occurrence(self):
        self.assert_next('30 1 * * *', 'America/New_York', '2026-11-01T04:00:00Z', '2026-11-01T05:30:00Z')
        self.assert_next('30 1 * * *', 'America/New_York', '2026-11-01T05:30:00Z', '2026-11-02T06:30:00Z')
        self.assert_next('30 1 * * *', 'America/New_York', '2026-11-01T06:00:00Z', '2026-11-02T06:30:00Z')

    def test_every_minute_skips_all_repeated_wall_minutes(self):
        self.assert_next('* * * * *', 'America/New_York', '2026-11-01T05:59:00Z', '2026-11-01T07:00:00Z')
        self.assert_next('* * * * *', 'America/New_York', '2026-11-01T06:15:00Z', '2026-11-01T07:00:00Z')

    def test_half_hour_transitions_use_zone_rules(self):
        self.assert_next('15 2 * * *', 'Australia/Lord_Howe', '2026-10-02T15:45:00Z', '2026-10-04T15:15:00Z')
        self.assert_next('45 1 * * *', 'Australia/Lord_Howe', '2026-04-04T14:45:00Z', '2026-04-05T15:15:00Z')

    def test_missing_calendar_date_is_skipped(self):
        self.assert_next('0 9 * * *', 'Pacific/Apia', '2011-12-29T19:00:00Z', '2011-12-30T19:00:00Z')

    def test_after_can_use_an_aware_local_datetime(self):
        after = datetime(2026, 11, 1, 1, 15, tzinfo=ZoneInfo('America/New_York'), fold=1)
        self.assertEqual(next_run('30 1 * * *', 'America/New_York', after), instant('2026-11-02T06:30:00Z'))

    def test_invalid_syntax_and_field_values(self):
        schedules = (
            '', '* * * *', '* * * * * *', '@daily', '60 * * * *', '* 24 * * *',
            '* * 0 * *', '* * 32 * *', '* * * 0 *', '* * * 13 *', '* * * * 8',
            '*/0 * * * *', '*/-1 * * * *', '10-1 * * * *', '1,,2 * * * *',
            '*/2/3 * * * *', '0 9 * JAN MON', '0 9 ? * *', '-1 * * * *',
            '1.5 * * * *', '0 9 * * 5-1', '0 9 * * 1;echo', '١ * * * *', ' ' * 257,
        )
        for cron in schedules:
            with self.subTest(cron=cron), self.assertRaises(ScheduleError):
                validate_schedule(cron, 'UTC')

    def test_impossible_calendar_dates_fail_validation(self):
        for cron in ('0 9 31 2 *', '0 9 30 2 *', '0 9 31 4,6,9,11 *'):
            with self.subTest(cron=cron), self.assertRaisesRegex(ScheduleError, 'no valid calendar date'):
                validate_schedule(cron, 'UTC')

    def test_invalid_timezones_and_parameter_types(self):
        for zone in ('', 'Mars/Olympus', '+03:00', '/etc/passwd', '../UTC', 'localtime', 'posixrules', None, []):
            with self.subTest(zone=zone), self.assertRaises(ScheduleError):
                validate_schedule('* * * * *', zone)
        for cron in (None, [], 5):
            with self.subTest(cron=cron), self.assertRaises(ScheduleError):
                validate_schedule(cron, 'UTC')
        for after in (None, '2026-09-08T00:00:00Z', datetime(2026, 9, 8)):
            with self.subTest(after=after), self.assertRaises(ScheduleError):
                next_run('* * * * *', 'UTC', after)

    def test_search_fails_clearly_when_every_selected_local_time_is_missing(self):
        # March 8 on a Sunday is always the missing hour under current US rules.
        with self.assertRaisesRegex(ScheduleError, 'no future occurrence within 400 years'):
            next_run('30 2 8 3 */7', 'America/New_York', instant('2026-01-01T00:00:00Z'))

    def test_datetime_upper_boundary_fails_clearly(self):
        with self.assertRaisesRegex(ScheduleError, 'supported date range'):
            next_run('* * * * *', 'UTC', datetime.max.replace(tzinfo=UTC))


if __name__ == '__main__':
    unittest.main()
