"""Work-hours schedule for push timing. Code enforces it; bots only supply importance."""
from __future__ import annotations

from datetime import datetime, time as clock, timedelta
import re
from zoneinfo import ZoneInfo

# The window is [start, end) in the zone's local time, on the chosen ISO weekdays (1 Monday ... 7 Sunday).
# honor_routine lets a bot's explicit routine lower a decision or failure to routine. It is off unless the person turns it on.
DEFAULT_SCHEDULE = {'enabled': True, 'start': '09:00', 'end': '18:00', 'timezone': 'America/New_York',
                    'weekdays': [1, 2, 3, 4, 5, 6, 7], 'honor_routine': False}
# A device with no stored schedule has not registered with an app that knows work hours.
# It keeps the old behavior, every alert at once, until the updated app registers its window.
LEGACY_SCHEDULE = dict(DEFAULT_SCHEDULE, enabled=False)
IMPORTANCE = ('urgent', 'important', 'routine')
# Without an explicit value, decisions and failures should not wait for morning. Completions and messages can.
KIND_FLOOR = {'decision': 'important', 'failed': 'important', 'completed': 'routine', 'message': 'routine'}


def explicit_importance(value):
    """The bot's explicit urgent, important, or routine, or None when it is missing or invalid."""
    value = value.strip().lower() if isinstance(value, str) else None
    return value if value in IMPORTANCE else None


def effective_importance(kind, explicit, honor_routine=False):
    """The importance used for timing and why, as (level, reason).

    Urgent and important always elevate. Explicit routine lowers a decision or failure only when the
    person's schedule honors it. A missing or invalid value is the kind's floor."""
    floor = KIND_FLOOR.get(kind, 'routine')
    explicit = explicit_importance(explicit)
    if explicit in ('urgent', 'important'):
        return explicit, 'Explicit'
    if explicit == 'routine':
        if floor == 'routine':
            return 'routine', 'Explicit'
        return ('routine', 'HonoredRoutine') if honor_routine is True else (floor, 'KindFloor')
    return floor, 'KindDefault'


def _minutes(value):
    match = re.fullmatch(r'([01]\d|2[0-3]):([0-5]\d)', value) if isinstance(value, str) else None
    return int(match[1]) * 60 + int(match[2]) if match else None


def _zone(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*', name):
        return None
    try:
        return ZoneInfo(name)
    except (OSError, ValueError, KeyError):
        return None


def _weekdays(value):
    """Sorted unique ISO weekdays, or None. Only real integers 1...7 count; booleans do not."""
    if not isinstance(value, list) or not value or any(type(day) is not int or not 1 <= day <= 7 for day in value):
        return None
    return sorted(set(value))


def validate(data):
    """Return a complete normalized schedule, or None when the values are unusable.

    Older clients omit the fields they do not know; those take the defaults."""
    if not isinstance(data, dict) or set(data) - set(DEFAULT_SCHEDULE):
        return None
    schedule = dict(DEFAULT_SCHEDULE, **data)
    if (type(schedule['enabled']) is not bool or type(schedule['honor_routine']) is not bool
            or _zone(schedule['timezone']) is None):
        return None
    schedule['weekdays'] = _weekdays(schedule['weekdays'])
    start, end = _minutes(schedule['start']), _minutes(schedule['end'])
    if schedule['weekdays'] is None or start is None or end is None or start >= end:
        return None
    return schedule


def merge(current, data):
    """Apply a client's schedule over the stored one, or None when it is unusable.

    Fields the client omits keep their stored values. Turning the schedule off always works,
    even when the other draft fields are invalid."""
    if not isinstance(data, dict):
        return None
    if data.get('enabled') is False:
        return dict(current, enabled=False)
    return validate(dict(current, **data))


def stored(data):
    """A schedule read from the database, tolerant of rows written before newer fields existed."""
    return validate(data) or dict(LEGACY_SCHEDULE)


def _local(schedule, at):
    return datetime.fromtimestamp(at, _zone(schedule['timezone']))


def in_window(schedule, at):
    """Half-open: the start minute is inside, the end minute is outside. Only on the chosen weekdays."""
    local = _local(schedule, at)
    if local.isoweekday() not in schedule['weekdays']:
        return False
    seconds = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
    return _minutes(schedule['start']) * 60 <= seconds < _minutes(schedule['end']) * 60


def _starts(schedule, day, minutes):
    """Every instant on `day` at which the local clock reads the start time.

    A repeated hour yields both passes. A time skipped by spring-forward yields the instant the clock jumps past it."""
    zone = _zone(schedule['timezone'])
    wall = datetime.combine(day, clock(minutes // 60, minutes % 60))
    found = set()
    for fold in (0, 1):
        stamp = int(wall.replace(tzinfo=zone, fold=fold).timestamp())
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == wall:
            found.add(stamp)
        else:
            low, high = stamp - 6 * 3600, stamp + 6 * 3600
            while high - low > 1:
                middle = (low + high) // 2
                low, high = (middle, high) if _local(schedule, middle).replace(tzinfo=None) < wall else (low, middle)
            found.add(high)
    return found


def _shifts(schedule, day):
    """Every instant on `day` at which the zone's UTC offset changes.

    A fall-back change can put the local clock inside a window that began before the repeated hour."""
    zone = _zone(schedule['timezone'])
    low = int(datetime.combine(day, clock(0)).replace(tzinfo=zone).timestamp())
    high = low + 26 * 3600
    offset = lambda stamp: datetime.fromtimestamp(stamp, zone).utcoffset()
    before = offset(low)
    if offset(high) == before:
        return set()
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (middle, high) if offset(middle) == before else (low, middle)
    return {high} if _local(schedule, high).date() == day else set()


def next_opening(schedule, at):
    """The first instant at or after `at` that is inside the window.

    Never earlier than `at`. A window that never opens within the search (inside a skipped hour) waits an hour.
    The search covers more than a week, so any chosen weekday is found."""
    if not schedule['enabled'] or in_window(schedule, at):
        return at
    first = _local(schedule, at).date()
    start = _minutes(schedule['start'])
    for ahead in range(9):
        day = first + timedelta(days=ahead)
        if day.isoweekday() not in schedule['weekdays']:
            continue
        openings = [stamp for stamp in _starts(schedule, day, start) | _shifts(schedule, day) if stamp >= at and in_window(schedule, stamp)]
        if openings:
            return min(openings)
    return at + 3600
