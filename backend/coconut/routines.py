"""Evaluate local cron schedules without installing an operating system job."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


UTC = timezone.utc
_SEARCH_DAYS = 146_097  # The Gregorian calendar repeats after 400 years.
_PART = re.compile(r'(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?')


class ScheduleError(ValueError):
    """A schedule is invalid or has no occurrence within the search limit."""


@dataclass(frozen=True)
class _Schedule:
    fields: tuple[str, ...]
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_wildcard: bool
    weekday_wildcard: bool

    def matches_date(self, value: date) -> bool:
        if value.month not in self.months:
            return False
        month_day = value.day in self.days
        week_day = (value.weekday() + 1) % 7 in self.weekdays
        # Cron uses OR only when neither day field contains a wildcard.
        if self.day_wildcard or self.weekday_wildcard:
            return month_day and week_day
        return month_day or week_day


def _field(text: str, minimum: int, maximum: int, name: str) -> frozenset[int]:
    values: set[int] = set()
    for item in text.split(','):
        match = _PART.fullmatch(item)
        if not match:
            raise ScheduleError(f'The {name} field must use numbers, *, lists, ranges, or steps.')
        base, step_text = match.groups()
        step = int(step_text) if step_text is not None else 1
        if step < 1:
            raise ScheduleError(f'The {name} step must be greater than zero.')
        if base == '*':
            start, end = minimum, maximum
        elif '-' in base:
            start, end = (int(value) for value in base.split('-'))
        else:
            start = int(base)
            end = maximum if step_text is not None else start
        if not minimum <= start <= end <= maximum:
            raise ScheduleError(f'The {name} field must stay between {minimum} and {maximum}, with ascending ranges.')
        values.update(range(start, end + 1, step))
    return frozenset(values)


@lru_cache(maxsize=128)
def _parse(cron: str) -> _Schedule:
    fields = tuple(cron.split())
    if len(fields) != 5:
        raise ScheduleError('Use five cron fields: minute, hour, day of month, month, and day of week.')
    minutes = _field(fields[0], 0, 59, 'minute')
    hours = _field(fields[1], 0, 23, 'hour')
    days = _field(fields[2], 1, 31, 'day of month')
    months = _field(fields[3], 1, 12, 'month')
    weekdays = frozenset(value % 7 for value in _field(fields[4], 0, 7, 'day of week'))
    schedule = _Schedule(fields, tuple(sorted(minutes)), tuple(sorted(hours)), days, months, weekdays, '*' in fields[2], '*' in fields[4])
    if schedule.day_wildcard or schedule.weekday_wildcard:
        if not any(day <= calendar.monthrange(2000, month)[1] for month in months for day in days):
            raise ScheduleError('This schedule selects no valid calendar date.')
    return schedule


@lru_cache(maxsize=1)
def _timezone_names() -> frozenset[str]:
    return frozenset(available_timezones()) - {'localtime', 'posixrules'}


@lru_cache(maxsize=128)
def _zone(name: str) -> ZoneInfo:
    if name not in _timezone_names():
        raise ScheduleError('Use a valid IANA time zone, such as America/New_York.')
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ScheduleError('The time zone is unavailable on this Mac.') from error


def _validated(cron: str, timezone_name: str) -> tuple[_Schedule, ZoneInfo]:
    if not isinstance(cron, str) or not cron.strip() or len(cron) > 256:
        raise ScheduleError('The cron schedule must contain between 1 and 256 characters.')
    if not isinstance(timezone_name, str) or not timezone_name or len(timezone_name) > 128:
        raise ScheduleError('Use a valid IANA time zone, such as America/New_York.')
    return _parse(cron), _zone(timezone_name)


def validate_schedule(cron: str, timezone_name: str) -> None:
    """Check cron syntax, IANA zone availability, and calendar feasibility."""
    _validated(cron, timezone_name)


def next_run(cron: str, timezone_name: str, after: datetime) -> datetime:
    """Return the first occurrence after an aware instant, in UTC.

    Missing local minutes never run. Repeated local minutes use their first
    occurrence only. Search at most 400 calendar years. An unavailable future
    occurrence raises ScheduleError; callers must not start work in that case.
    """
    schedule, zone = _validated(cron, timezone_name)
    if not isinstance(after, datetime) or after.tzinfo is None or after.utcoffset() is None:
        raise ScheduleError('The starting time must include a time zone.')
    try:
        after_utc = after.astimezone(UTC)
        local_date = after_utc.astimezone(zone).date()
    except (OverflowError, ValueError) as error:
        raise ScheduleError('The starting time is outside the supported date range.') from error
    last_ordinal = min(date.max.toordinal(), local_date.toordinal() + _SEARCH_DAYS)
    for ordinal in range(local_date.toordinal(), last_ordinal + 1):
        current_date = date.fromordinal(ordinal)
        if not schedule.matches_date(current_date):
            continue
        for hour in schedule.hours:
            for minute in schedule.minutes:
                wall = datetime(current_date.year, current_date.month, current_date.day, hour, minute)
                try:
                    # fold=0 selects the first instance of a repeated local minute.
                    candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
                    round_trip = candidate.astimezone(zone).replace(tzinfo=None)
                except (OverflowError, ValueError):
                    continue
                if round_trip == wall and candidate > after_utc:
                    return candidate
    raise ScheduleError('This schedule has no future occurrence within 400 years or the supported date range.')
