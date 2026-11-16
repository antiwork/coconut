"""Alerts: the operational notices that the phone shows in its alerts feed.

An alert is a notice from the tooling of the Mac: a cron job that failed, a finding of a watchdog, a deploy that
ended. A producer posts it with the token of <data>/gateway/bridge.json (scripts/alerts.py). An alert has a level:
`info`, `notice` or `critical`. The feed shows each level. Only a critical alert notifies the phone (push.py).

A producer can post an alert again, for example on a retry. A post with the `dedupe_key` of an alert of the last 24
hours answers with that alert. It adds no alert, no event and no notification.

The person reads an alert, and resolves it when its cause is gone. The store keeps the alerts of 30 days and at most
500 of them.

This module imports only the standard library at its top. `Alerts` is the store. `AlertFeed` gives a service its
routes and its part of the state.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

SCHEMA = '''
CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS alerts_created ON alerts (json_extract(data,'$.created_at'));
'''
LEVELS = ('info', 'notice', 'critical')
MAX_SOURCE = 80
MAX_TITLE = 120
MAX_BODY = 2000
MAX_URL = 2000
MAX_DEDUPE_KEY = 200
# A post with the key of an alert that is younger than this is a repeat of that alert.
DEDUPE_WINDOW = timedelta(hours=24)
KEPT_FOR = timedelta(days=30)
KEPT_ROWS = 500
LIST_LIMIT = 200
# The part of the state: the phone gets it on each load, so it stays small.
STATE_OPEN = 50
STATE_RESOLVED = 20
STATE_RESOLVED_FOR = timedelta(days=7)
ROUTE = '/v1/alerts'
# An alert is not resolved, then newest first. The row ID orders two alerts of one moment.
_ORDER = "ORDER BY json_extract(data,'$.resolved_at') IS NOT NULL, json_extract(data,'$.created_at') DESC, rowid DESC"
_UNREAD = "json_extract(data,'$.read_at') IS NULL"
_OPEN = "json_extract(data,'$.resolved_at') IS NULL"


class AlertError(ValueError):
    """A post that the store refuses. The text names the field and the rule, for the producer."""


class AlertMissing(LookupError):
    """No alert has this ID."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _moment(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _before(current: str, age: timedelta) -> str:
    """The time `age` before `current`, as text that sorts with the stored times."""
    return (_moment(current) - age).isoformat().replace('+00:00', 'Z')


def _text(data: dict, name: str, maximum: int, *, required: bool) -> str:
    value = data.get(name)
    if value is None and not required:
        return ''
    if not isinstance(value, str):
        raise AlertError(f'{name} must be text.')
    value = value.strip()
    if required and not value:
        raise AlertError(f'{name} must not be empty.')
    if len(value) > maximum:
        raise AlertError(f'{name} must contain at most {maximum} characters.')
    return value


def checked(data: dict) -> dict:
    """The fields of a post, or an AlertError with the reason."""
    if not isinstance(data, dict):
        raise AlertError('Send a JSON object.')
    level = data.get('level')
    if level not in LEVELS:
        raise AlertError('level must be info, notice or critical.')
    url = _text(data, 'url', MAX_URL, required=False) or None
    if url is not None:
        try:
            parts = urlsplit(url)
            valid = parts.scheme in ('http', 'https') and bool(parts.hostname)
        except ValueError:
            valid = False
        if not valid or any(character.isspace() or ord(character) < 32 for character in url):
            raise AlertError('url must be an http or https address.')
    return dict(level=level, source=_text(data, 'source', MAX_SOURCE, required=True),
                title=_text(data, 'title', MAX_TITLE, required=True), body=_text(data, 'body', MAX_BODY, required=False),
                url=url, dedupe_key=_text(data, 'dedupe_key', MAX_DEDUPE_KEY, required=False) or None)


class Alerts:
    """The alerts of one sqlite store. `now` gives the time as ISO 8601 UTC text. `event` takes the type and the data
    of each change, and a call that changes nothing sends none. The caller holds the transaction of a write."""

    def __init__(self, db, now=utc_now, event=None):
        self.db = db
        self.now = now
        self.event = event or (lambda event_type, data: None)

    def post(self, level, source, title, body='', url=None, dedupe_key=None) -> dict:
        """Add an alert and return it. A repeat of a key inside DEDUPE_WINDOW returns the first alert of the key."""
        fields = checked(dict(level=level, source=source, title=title, body=body, url=url, dedupe_key=dedupe_key))
        current = self.now()
        if fields['dedupe_key'] is not None:
            row = self.db.execute(
                "SELECT data FROM alerts WHERE json_extract(data,'$.dedupe_key')=? "
                "ORDER BY json_extract(data,'$.created_at') DESC, rowid DESC LIMIT 1", (fields['dedupe_key'],)).fetchone()
            if row:
                existing = json.loads(row[0])
                if _moment(current) - _moment(existing['created_at']) < DEDUPE_WINDOW:
                    return existing
        alert = dict(fields, id=str(uuid4()), created_at=current, read_at=None, resolved_at=None)
        self._put(alert)
        self.prune()
        self.event('alert.created', alert)
        return alert

    def list(self, limit: int = LIST_LIMIT) -> list[dict]:
        """The alerts that are not resolved, then the resolved ones. Each part has its newest alert first."""
        limit = max(1, min(int(limit), LIST_LIMIT))
        return self._rows(f'SELECT data FROM alerts {_ORDER} LIMIT ?', (limit,))

    def get(self, alert_id: str) -> dict:
        row = self.db.execute('SELECT data FROM alerts WHERE id=?', (alert_id,)).fetchone()
        if not row:
            raise AlertMissing('This alert was not found.')
        return json.loads(row[0])

    def read(self, alert_id: str) -> dict:
        alert = self.get(alert_id)
        if alert['read_at'] is None:
            alert['read_at'] = self.now()
            self._put(alert)
            self.event('alert.updated', alert)
        return alert

    def read_all(self) -> int:
        """Mark each alert as read. Return the count of the alerts that changed."""
        current = self.now()
        changed = self.db.execute(f"UPDATE alerts SET data=json_set(data,'$.read_at',?) WHERE {_UNREAD}",
                                  (current,)).rowcount
        if changed:
            self.event('alert.updated', {'read_all': True, 'read_at': current})
        return changed

    def resolve(self, alert_id: str) -> dict:
        """Resolve an alert. A resolved alert is read too: it asks nothing more of the person."""
        alert = self.get(alert_id)
        if alert['resolved_at'] is None:
            alert['resolved_at'] = self.now()
            alert['read_at'] = alert['read_at'] or alert['resolved_at']
            self._put(alert)
            self.event('alert.updated', alert)
        return alert

    def unread_count(self) -> int:
        return self.db.execute(f'SELECT COUNT(*) FROM alerts WHERE {_UNREAD}').fetchone()[0]

    def state(self) -> dict:
        """The part of the state of the phone: the newest alerts that are not resolved, the alerts that were resolved
        a short time ago, and the count of all unread alerts."""
        resolved_after = _before(self.now(), STATE_RESOLVED_FOR)
        items = self._rows(f'SELECT data FROM alerts WHERE {_OPEN} {_ORDER} LIMIT ?', (STATE_OPEN,))
        items += self._rows(f"SELECT data FROM alerts WHERE json_extract(data,'$.resolved_at')>=? {_ORDER} LIMIT ?",
                            (resolved_after, STATE_RESOLVED))
        return {'items': items, 'unread': self.unread_count()}

    def prune(self) -> int:
        """Delete the alerts that are older than KEPT_FOR, then the ones over KEPT_ROWS: the resolved ones first, and
        the oldest first. Return the count of the deleted alerts."""
        deleted = self.db.execute("DELETE FROM alerts WHERE json_extract(data,'$.created_at')<?",
                                  (_before(self.now(), KEPT_FOR),)).rowcount
        extra = self.db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0] - KEPT_ROWS
        if extra > 0:
            deleted += self.db.execute(
                "DELETE FROM alerts WHERE id IN (SELECT id FROM alerts ORDER BY "
                "json_extract(data,'$.resolved_at') IS NULL, json_extract(data,'$.created_at'), rowid LIMIT ?)",
                (extra,)).rowcount
        return deleted

    def _put(self, alert: dict):
        self.db.execute('INSERT INTO alerts(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                        (alert['id'], json.dumps(alert, separators=(',', ':'), sort_keys=True)))

    def _rows(self, query: str, values: tuple) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute(query, values).fetchall()]


class AlertFeed:
    """The alert routes and the alert part of the state of a service. The service gives `db`, `transaction`,
    `_read_view`, `_read_transaction`, `_now` and `_event`. A write holds the transaction for its sqlite statements
    only."""

    def _alerts(self) -> Alerts:
        # A read view has its own connection, so each call makes a store of the connection of its caller.
        return Alerts(self.db, self._now, self._event)

    def _alerts_state(self) -> dict:
        """`alerts` of the state and of an update. The caller holds a read transaction."""
        return self._alerts().state()

    def alert_request(self, method: str, path: str, query: str, data: dict) -> tuple[int, dict]:
        """One request of the alert routes. Return the status and the answer.

            POST /v1/alerts                the alert, with 201
            GET  /v1/alerts?limit=         {items, unread}
            POST /v1/alerts/read-all       {unread}
            POST /v1/alerts/<id>/read      the alert
            POST /v1/alerts/<id>/resolve   the alert
        """
        from .service_base import ServiceError
        parts = path.removeprefix(ROUTE).strip('/').split('/') if path != ROUTE else []
        try:
            if method == 'GET' and not parts:
                try:
                    limit = int(parse_qs(query).get('limit', [str(LIST_LIMIT)])[0])
                except ValueError:
                    raise ServiceError(400, 'limit must contain a number.') from None
                with self._read_view() as view, view._read_transaction():
                    alerts = view._alerts()
                    return 200, {'items': alerts.list(limit), 'unread': alerts.unread_count()}
            if method == 'POST' and not parts:
                fields = checked(data)
                with self.transaction():
                    return 201, self._alerts().post(**fields)
            if method == 'POST' and parts == ['read-all']:
                with self.transaction():
                    alerts = self._alerts()
                    alerts.read_all()
                    return 200, {'unread': alerts.unread_count()}
            if method == 'POST' and len(parts) == 2 and parts[1] in ('read', 'resolve'):
                with self.transaction():
                    alerts = self._alerts()
                    return 200, alerts.read(parts[0]) if parts[1] == 'read' else alerts.resolve(parts[0])
        except AlertError as error:
            raise ServiceError(400, str(error)) from None
        except AlertMissing as error:
            raise ServiceError(404, str(error)) from None
        raise ServiceError(404, 'This route was not found.')
