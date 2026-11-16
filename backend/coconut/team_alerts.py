"""Alerts: a message of an agent in a chat of a team with a type and a level (docs/team-log.md, "Alerts").

An alert is a `message` entry with the field `alert`: `{"type": "risk", "level": "critical"}`. Everything else about
it is a message of its chat: the storage, the order, the unread count, the mute, the notification, the reactions and
the replies. A reader that does not know the field shows the message without it.

- `type` is a label that the sender chooses: 1 to 32 characters of `a` to `z`, `0` to `9`, `-` and `_`.
- `level` is `info`, `notice` or `critical`. Without it, the level is `notice`.

A reader takes only an alert that fits (alert_fields). A writer on this Mac trims the texts and makes them lower case
first, and refuses each other value (alert_request). The app filters a chat by type and by level
(TeamMessages.team_messages), and the store counts the alerts of a chat for the filter (TeamStore.alert_counts).

A send of Hermes names only a chat and a text. So the target of a cron job can name the alert at the end of its chat ID:
`<chat ID>:alert:<type>` or `<chat ID>:alert:<type>:<level>` (split_target). Hermes gives the chat ID to the sender as
the job has it, so this needs no field of Hermes.
"""
from __future__ import annotations

import re

LEVELS = ('info', 'notice', 'critical')
DEFAULT_LEVEL = 'notice'
CRITICAL = 'critical'
MAX_TYPE = 32
TYPE = re.compile(r'[a-z0-9_-]{1,32}')
# The part of a chat ID that starts the alert of a send (split_target).
TARGET = ':alert:'
# The filter of a chat shows at most this many types: the ones with the most alerts.
MAX_TYPES = 64
MISFIT = ('An alert has a type of 1 to 32 characters (a to z, 0 to 9, - and _) and a level: info, notice or '
          'critical. Send the message with such an alert, or without one.')
FILTER_MISFIT = 'Filter the alerts by a type of 1 to 32 characters (a to z, 0 to 9, - and _) and by a level.'


def alert_fields(value) -> dict | None:
    """The alert of a `message` payload, or None when it is absent or does not fit. An alert that does not fit never
    makes its message invalid: the message shows as a plain message. A reader ignores each other key of the object, so
    a later version can add one."""
    if not isinstance(value, dict):
        return None
    kind, level = value.get('type'), value.get('level', DEFAULT_LEVEL)
    if not isinstance(kind, str) or len(kind) > MAX_TYPE or TYPE.fullmatch(kind) is None:
        return None
    if not isinstance(level, str) or level not in LEVELS:
        return None
    return dict(type=kind, level=level)


def alert_request(value) -> dict | None:
    """The alert of a message that this Mac writes, or None without one. The type and the level lose the space around
    them and become lower case, and a level of null is the default. Each other value is a ValueError with MISFIT."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(MISFIT)
    kind, level = value.get('type'), value.get('level')
    fields = alert_fields({'type': kind.strip().lower() if isinstance(kind, str) else kind,
                           'level': DEFAULT_LEVEL if level is None else
                           level.strip().lower() if isinstance(level, str) else level})
    if fields is None:
        raise ValueError(MISFIT)
    return fields


def split_target(chat_id) -> tuple[str, dict | None]:
    """The chat ID of a send without the alert at its end, and that alert as a request (alert_request), or None.
    `<chat>:alert:risk` gives the type `risk`, and `<chat>:alert:risk:critical` also the level."""
    text = str(chat_id)
    chat, separator, alert = text.rpartition(TARGET)
    if not separator:
        return text, None
    kind, _, level = alert.partition(':')
    return chat, {'type': kind, 'level': level or None}


def filter_fields(data: dict) -> tuple[str | None, str | None]:
    """The type and the level of the query of a page (`alert_type`, `alert_level`), each None without it. A value that
    no alert can have is a ValueError with FILTER_MISFIT."""
    kind, level = data.get('alert_type'), data.get('alert_level')
    if kind is not None and (not isinstance(kind, str) or TYPE.fullmatch(kind) is None):
        raise ValueError(FILTER_MISFIT)
    if level is not None and level not in LEVELS:
        raise ValueError(FILTER_MISFIT)
    return kind, level


def stored_alert(alert: dict | None) -> tuple[str | None, str | None]:
    """The columns `alert_type` and `alert_level` of a message: both texts, or both null."""
    return (alert['type'], alert['level']) if alert else (None, None)


def alert_view(kind: str | None, level: str | None) -> dict | None:
    """The `alert` of a message view from its columns: `{type, level}`, or None for a plain message."""
    return {'type': kind, 'level': level} if kind and level else None


def summary_view(rows) -> dict:
    """The alerts of one chat for the filter of the app, from rows of (type, count, critical count): the count of all
    alerts, of the critical ones, and the types with the most alerts first, at most MAX_TYPES."""
    rows = sorted(((kind, count, critical or 0) for kind, count, critical in rows), key=lambda row: (-row[1], row[0]))
    return {'total': sum(row[1] for row in rows), 'critical': sum(row[2] for row in rows),
            'types': [{'type': kind, 'count': count, 'critical': critical} for kind, count, critical in rows[:MAX_TYPES]]}
