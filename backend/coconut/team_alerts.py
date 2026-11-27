"""Alerts: a message of an agent in a chat of a team with a type and a level (docs/team-log.md, "Alerts").

An alert is a `message` entry with the field `alert`: `{"type": "risk", "level": "critical"}`. Everything else about
it is a message of its chat: the storage, the order, the unread count, the mute, the notification, the reactions and
the replies. A reader that does not know the field shows the message without it.

- `type` is a label that the sender chooses: 1 to 32 characters of `a` to `z`, `0` to `9`, `-` and `_`.
- `level` is `info`, `notice` or `critical`. Without it, the level is `notice`.
- `key` names the condition that the alert reports, as the sender calls it, for example the name of a CloudWatch alarm:
  1 to 128 characters on one line. Without it, each alert is a notice of its own.
- `resolved` is true on the alert that says that the condition of its key is over. It counts only with a key.

A keyed alert continues the notice of the newest alert of the same agent with the same key in its chat: the Mac of the
sender writes the field `thread` of the message (TeamStore.alert_thread), so each reader puts it into that thread
(docs/team-log.md, "Inbox chats"). An alert that fires after a resolution starts a new notice. The newest keyed alert of
a thread says whether its notice is resolved (TeamStore.threads).

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
MAX_TYPE = 32
TYPE = re.compile(r'[a-z0-9_-]{1,32}')
# The part of a chat ID that starts the alert of a send (split_target).
TARGET = ':alert:'
# The filter of a chat shows at most this many types: the ones with the most alerts.
MAX_TYPES = 64
MISFIT = ('An alert has a type of 1 to 32 characters (a to z, 0 to 9, - and _) and a level: info, notice or '
          'critical. Send the message with such an alert, or without one.')
MAX_KEY = 128
KEY_MISFIT = ('The key of an alert is a text of 1 to 128 characters on one line, and resolved is true or false, true '
              'only with a key. Send the alert with such a key, or without one.')
FILTER_MISFIT = 'Filter the alerts by a type of 1 to 32 characters (a to z, 0 to 9, - and _) and by a level.'


def key_fits(value) -> bool:
    """True for the key of an alert: a str of 1 to MAX_KEY code points on one line that UTF-8 can encode."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_KEY or value.splitlines() != [value]:
        return False
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def alert_fields(value) -> dict | None:
    """The alert of a `message` payload, or None when it is absent or does not fit. An alert that does not fit never
    makes its message invalid: the message shows as a plain message. A key that does not fit is left out, and so is
    `resolved` without a key, so the alert stays as a notice of its own. `key` and `resolved: true` are in the result
    only when they count. A reader ignores each other key of the object, so a later version can add one."""
    if not isinstance(value, dict):
        return None
    kind, level = value.get('type'), value.get('level', DEFAULT_LEVEL)
    if not isinstance(kind, str) or len(kind) > MAX_TYPE or TYPE.fullmatch(kind) is None:
        return None
    if not isinstance(level, str) or level not in LEVELS:
        return None
    fields = dict(type=kind, level=level)
    if key_fits(value.get('key')):
        fields['key'] = value['key']
        if value.get('resolved') is True:
            fields['resolved'] = True
    return fields


def alert_request(value) -> dict | None:
    """The alert of a message that this Mac writes, or None without one. The type and the level lose the space around
    them and become lower case, and a level of null is the default. Each other value is a ValueError with MISFIT. The
    key loses the space around it and keeps its case, and `resolved` of null is false. A key that does not fit, a
    `resolved` that is not true or false, and `resolved: true` without a key are a ValueError with KEY_MISFIT."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(MISFIT)
    kind, level, key, resolved = (value.get(name) for name in ('type', 'level', 'key', 'resolved'))
    fields = alert_fields({'type': kind.strip().lower() if isinstance(kind, str) else kind,
                           'level': DEFAULT_LEVEL if level is None else
                           level.strip().lower() if isinstance(level, str) else level})
    if fields is None:
        raise ValueError(MISFIT)
    key = key.strip() if isinstance(key, str) else key
    if key is not None and not key_fits(key) or resolved is not None and not isinstance(resolved, bool) \
            or resolved and key is None:
        raise ValueError(KEY_MISFIT)
    if key is not None:
        fields['key'] = key
    if resolved:
        fields['resolved'] = True
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


def stored_alert(alert: dict | None) -> tuple[str | None, str | None, str | None, int | None]:
    """The columns `alert_type`, `alert_level`, `alert_key` and `alert_resolved` of a message. A plain message has
    each null, and an alert without a key has a null key and a null `alert_resolved`. With a key, `alert_resolved` is 1
    for a resolution and 0 for each other alert."""
    if not alert:
        return None, None, None, None
    key = alert.get('key')
    return alert['type'], alert['level'], key, (int(alert.get('resolved') is True) if key else None)


def alert_view(kind: str | None, level: str | None, resolved: int | None = None) -> dict | None:
    """The `alert` of a message view from its columns: `{type, level, resolved}`, or None for a plain message.
    `resolved` is true only for an alert that resolves the condition of its key."""
    return {'type': kind, 'level': level, 'resolved': bool(resolved)} if kind and level else None


def summary_view(rows) -> dict:
    """The alerts of one chat for the filter of the app, from rows of (type, count, critical count): the count of all
    alerts, of the critical ones, and the types with the most alerts first, at most MAX_TYPES."""
    rows = sorted(((kind, count, critical or 0) for kind, count, critical in rows), key=lambda row: (-row[1], row[0]))
    return {'total': sum(row[1] for row in rows), 'critical': sum(row[2] for row in rows),
            'types': [{'type': kind, 'count': count, 'critical': critical} for kind, count, critical in rows[:MAX_TYPES]]}
