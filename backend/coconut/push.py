"""Durable, private notification delivery through Apple APNs.

Each bot event that can notify becomes one item per phone in `push_items`. Coconut decides when items notify
(`push_schedule.due`) and writes one `push_outbox` row per notification: one urgent card, or one group of the items
that waited. The row ID is fixed, and a sealed envelope is kept, so a retry never sends a second notification."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
from datetime import datetime, timezone
import subprocess
import tempfile
import threading
import time
from uuid import NAMESPACE_URL, uuid5
from .push_relay import RelayTransport, validate_route, notification_alert, group_text
from .push_schedule import (LEGACY_SCHEDULE, OUTSIDE, due, holds_urgent, in_window, merge as merge_schedule,
                            stored as stored_schedule)

LEVELS = ('every', 'grouped', 'urgent_only')
# A phone that never sent its preferences keeps the old categories: cards, failed and finished tasks, no messages.
LEGACY_PREFERENCES = {'level': 'grouped', 'messages': False}
# An item that waits longer than this, for example through a long weekend outside work hours, stays only in Inbox.
ITEM_LIFETIME = 7 * 86400
# Sent items and finished rows are kept this long, so a repeated event does not notify again.
HISTORY = 30 * 86400
# The app refreshes its registration while it runs. A registration this old belongs to an install nobody uses.
STALE_REGISTRATION = 30 * 86400
PUSH_TABLES = ('push_devices', 'push_routes', 'push_preferences', 'push_schedules')
# The phone reports the last result of its notification previews here. `coconut status` reads this file. A report stays
# when the phone turns notifications off: the phone sends a report again only when it changes.
PREVIEWS_FILE = 'push-previews.json'
PREVIEW_TIMES = ('failing_since', 'failed_at', 'worked_at')
URGENT_DECISION = "json_extract(data,'$.needs_attention_now') IS 1"
# A phone reaches the Mac only through the relay tunnel, which needs the phone's static key. A device without one is an
# old version 1 pairing or a tool on this Mac, so it is not paired for notifications.
PHONES = 'SELECT id FROM devices WHERE peer_key IS NOT NULL'


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace('+00:00', 'Z') if stamp else None


def preview_report(value):
    """One preview report of the phone (ios/PushShared/PreviewDiagnostics.swift), or None. It holds a reason code
    and times only. Raise ValueError when it is not valid."""
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value) <= {'reason', *PREVIEW_TIMES}:
        raise ValueError
    reason = value.get('reason')
    if reason is not None and not (isinstance(reason, str) and re.fullmatch(r"[\w .:'-]{1,80}", reason)):
        raise ValueError
    result = {'reason': reason}
    for name in PREVIEW_TIMES:
        text = value.get(name)
        if text is not None and not isinstance(text, str):
            raise ValueError
        result[name] = iso(datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()) if text else None
    return result


def preview_line(report):
    """The state of the previews of one phone, as `coconut status` prints it."""
    app, extension = report.get('app') or {}, report.get('extension') or {}
    for source, value in (('the app: ', app), ('', extension)):
        if value.get('reason'):
            return f"failing since {value.get('failing_since') or value.get('failed_at')}: {source}{value['reason']}"
    if extension.get('worked_at'):
        return f"working (last preview {extension['worked_at']})"
    return 'no report from the notification extension yet'


def preferences(data):
    """Normalized {level, messages}, or None when the values are unusable.

    Apps before the levels send {decisions, messages}. Decisions off becomes Only urgent; everything else is Grouped."""
    if not isinstance(data, dict) or type(data.get('messages')) is not bool:
        return None
    if set(data) == {'level', 'messages'}:
        return dict(data) if data['level'] in LEVELS else None
    if set(data) == {'decisions', 'messages'} and type(data['decisions']) is bool:
        return {'level': 'grouped' if data['decisions'] else 'urgent_only', 'messages': data['messages']}
    return None


class APNsTransport:
    def __init__(self, root):
        self.directory = Path(root) / 'push'

    @property
    def configured(self):
        return any(self.configured_for(environment) for environment in ('development', 'production'))

    def configured_for(self, environment):
        if environment not in ('development', 'production'):
            return False
        directory = self.directory if environment == 'development' else self.directory / 'production'
        if self.directory.is_symlink() or directory.is_symlink():
            return False
        paths = [directory / name for name in ('config.json', 'AuthKey.p8')]
        paths.append(self.directory / 'apns-provider')
        return all(p.is_file() and not p.is_symlink() and not p.stat().st_mode & 0o077 for p in paths)

    def send(self, token, environment, payload, notification_id, collapse_id, expires):
        if not self.configured_for(environment):
            return 0, 'PushNotConfigured'
        directory = self.directory if environment == 'development' else self.directory / 'production'
        request = dict(token=token, environment=environment, payload=payload,
                       id=notification_id, collapse_id=collapse_id, expires=int(expires))
        try:
            result = subprocess.run([str(self.directory / 'apns-provider'), str(directory)],
                                    input=json.dumps(request).encode(), capture_output=True, timeout=15)
            response = json.loads(result.stdout) if result.returncode == 0 else {}
            return int(response.get('status', 0)), str(response.get('reason', 'TransportUnavailable'))[:80]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 0, 'TransportUnavailable'


class PushDelivery:
    def __init__(self, service, transport=None):
        self.service = service
        self.transport = transport or APNsTransport(service.root)
        self.relay = RelayTransport(service.root)
        self.thread = None
        self.pruned = 0
        db = service.db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS push_devices (
            device_id TEXT PRIMARY KEY, token TEXT NOT NULL, environment TEXT NOT NULL,
            enabled INTEGER NOT NULL, updated REAL NOT NULL, UNIQUE(token, environment));
        CREATE TABLE IF NOT EXISTS push_items (
            device_id TEXT NOT NULL, event_key TEXT NOT NULL, kind TEXT NOT NULL, bot_id TEXT NOT NULL,
            task_id TEXT NOT NULL, decision_id TEXT, message_id TEXT, turn_id TEXT NOT NULL,
            urgent INTEGER NOT NULL, created REAL NOT NULL, status TEXT NOT NULL, delivery_id TEXT, reason TEXT,
            PRIMARY KEY(device_id, event_key));
        CREATE INDEX IF NOT EXISTS push_items_waiting ON push_items(status, device_id);
        CREATE INDEX IF NOT EXISTS push_items_delivery ON push_items(delivery_id);
        CREATE TABLE IF NOT EXISTS push_preferences (
            device_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS push_schedules (
            device_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS push_routes (
            device_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS push_status (
            device_id TEXT PRIMARY KEY, last_delivered_at REAL, last_error TEXT, last_error_at REAL);
        CREATE TABLE IF NOT EXISTS push_previews (
            device_id TEXT PRIMARY KEY, data TEXT NOT NULL, reported REAL NOT NULL);
        ''')
        # The urgent summary of the settings screen counts the cards of the last week and lists the last urgent cards.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='decisions'").fetchone():
            db.execute("CREATE INDEX IF NOT EXISTS decisions_created ON decisions (json_extract(data,'$.created_at'))")
            db.execute(f"CREATE INDEX IF NOT EXISTS decisions_urgent ON decisions (json_extract(data,'$.created_at')) WHERE {URGENT_DECISION}")
        columns = {row[1] for row in db.execute('PRAGMA table_info(push_outbox)')}
        if 'kind' in columns:
            # The outbox before grouping held one row per event. Its rows become items, so nothing notifies twice.
            urgent = 'urgent' if 'urgent' in columns else "explicit_importance IS 'urgent'" if 'explicit_importance' in columns else '0'
            message = 'message_id' if 'message_id' in columns else 'NULL'
            db.execute(f'''INSERT OR IGNORE INTO push_items
                SELECT device_id,event_key,kind,bot_id,task_id,decision_id,{message},turn_id,{urgent},not_before,
                       CASE status WHEN 'queued' THEN 'waiting' ELSE 'sent' END,NULL,reason FROM push_outbox''')
            db.execute('DROP TABLE push_outbox')
        db.execute('''CREATE TABLE IF NOT EXISTS push_outbox (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, event_key TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, not_before REAL NOT NULL, expires REAL NOT NULL,
            envelope TEXT, reason TEXT, UNIQUE(device_id, event_key))''')
        if 'sent' not in {row[1] for row in db.execute('PRAGMA table_info(push_outbox)')}:
            db.execute('ALTER TABLE push_outbox ADD COLUMN sent REAL')
        for device_id, data in db.execute('SELECT device_id,data FROM push_preferences').fetchall():
            value = preferences(json.loads(data))
            if value is None:
                db.execute('DELETE FROM push_preferences WHERE device_id=?', (device_id,))
            elif value != json.loads(data):
                db.execute('UPDATE push_preferences SET data=? WHERE device_id=?', (json.dumps(value), device_id))
        if db.execute("SELECT 1 FROM pragma_table_info('devices') WHERE name='peer_key'").fetchone():
            self.remove_stale(time.time())

    @property
    def configured(self):
        return self.relay.configured or self.transport.configured

    def registration(self, device_id, data):
        from .service import ServiceError
        if not self.service.db.execute('SELECT 1 FROM devices WHERE id=? AND peer_key IS NOT NULL', (device_id,)).fetchone():
            raise ServiceError(401, 'Pair this iPhone before enabling notifications.')
        token, environment, enabled = data.get('token'), data.get('environment'), data.get('enabled')
        if (not isinstance(token, str) or not re.fullmatch(r'[0-9a-fA-F]{32,512}', token)
                or len(token) % 2 or environment not in ('development', 'production') or type(enabled) is not bool):
            raise ServiceError(400, 'Supply an APNs token, environment, and notification preference.')
        chosen = data.get('preferences')
        if chosen is not None:
            chosen = preferences(chosen)
            if chosen is None:
                raise ServiceError(400, 'Supply a notification level (every, grouped, or urgent_only) and true or false for messages.')
        schedule = data.get('schedule')
        if schedule is not None:
            schedule = merge_schedule(self.schedule(device_id), schedule)
            if schedule is None:
                raise ServiceError(400, 'Supply start before end as HH:MM, an IANA timezone, at least one weekday from 1 to 7, and true or false for enabled.')
        # Apps before the Outside work hours choice do not send it, and Macs before it ignore it, so it is not in the schedule.
        outside = data.get('outside_work_hours')
        if outside is not None:
            if outside not in OUTSIDE:
                raise ServiceError(400, 'Supply urgent or nothing for outside_work_hours.')
            schedule = dict(schedule or self.schedule(device_id), outside=outside)
        token = token.lower()
        route = data.get('relay')
        if route is not None:
            if not validate_route(route) or not self.relay.configured:
                raise ServiceError(400, 'Register this iPhone with Coconut notifications first.')
            self.service.db.execute('INSERT INTO push_routes VALUES (?,?) ON CONFLICT(device_id) DO UPDATE SET data=excluded.data',
                                    (device_id, json.dumps(route)))
        elif self.service.db.execute('SELECT 1 FROM push_routes WHERE device_id=?', (device_id,)).fetchone():
            raise ServiceError(409, 'This iPhone uses encrypted notifications. Refresh its registration.')
        # A reinstall can pair again while retaining the same APNs address.
        self.service.db.execute('DELETE FROM push_devices WHERE token=? AND environment=? AND device_id<>?',
                                (token, environment, device_id))
        self.service.db.execute('''INSERT INTO push_devices (device_id,token,environment,enabled,updated) VALUES (?,?,?,?,?) ON CONFLICT(device_id)
            DO UPDATE SET token=excluded.token,environment=excluded.environment,enabled=excluded.enabled,updated=excluded.updated''',
            (device_id, token, environment, int(enabled), time.time()))
        if chosen is not None:
            self.service.db.execute('INSERT INTO push_preferences VALUES (?,?) ON CONFLICT(device_id) DO UPDATE SET data=excluded.data',
                                    (device_id, json.dumps(chosen)))
            for event_key, kind, urgent in self.service.db.execute(
                    "SELECT event_key,kind,urgent FROM push_items WHERE device_id=? AND status='waiting'", (device_id,)).fetchall():
                if not self.allows(device_id, kind, urgent):
                    self.service.db.execute("UPDATE push_items SET status='dropped',reason='Disabled' WHERE device_id=? AND event_key=?",
                                            (device_id, event_key))
        if schedule is not None:
            self.service.db.execute('INSERT INTO push_schedules VALUES (?,?) ON CONFLICT(device_id) DO UPDATE SET data=excluded.data',
                                    (device_id, json.dumps(schedule)))
        if not enabled:
            self.unregister_jobs(device_id)
        return {'registered': True, 'configured': self.configured}

    def schedule(self, device_id):
        row = self.service.db.execute('SELECT data FROM push_schedules WHERE device_id=?', (device_id,)).fetchone()
        return stored_schedule(json.loads(row[0])) if row else dict(LEGACY_SCHEDULE)

    def stored_preferences(self, device_id):
        row = self.service.db.execute('SELECT data FROM push_preferences WHERE device_id=?', (device_id,)).fetchone()
        return preferences(json.loads(row[0])) if row else None

    def preferences(self, device_id):
        return self.stored_preferences(device_id) or dict(LEGACY_PREFERENCES)

    def unregister_jobs(self, device_id):
        self.service.db.execute("UPDATE push_items SET status='dropped',reason='Disabled' WHERE device_id=? AND status='waiting'", (device_id,))
        self.service.db.execute("UPDATE push_outbox SET status='discarded',reason='Disabled' WHERE device_id=? AND status='queued'", (device_id,))

    def unregister(self, device_id):
        for table in PUSH_TABLES:
            self.service.db.execute(f'DELETE FROM {table} WHERE device_id=?', (device_id,))
        self.unregister_jobs(device_id)
        return {'registered': False, 'configured': self.configured}

    def previews(self, device_id, data):
        """Keep the phone's last preview results, log a change, and write them for `coconut status`."""
        from .service import ServiceError
        try:
            if not isinstance(data, dict) or not set(data) <= {'extension', 'app'}:
                raise ValueError
            report = {source: preview_report(data.get(source)) for source in ('extension', 'app')}
        except (ValueError, TypeError, OverflowError):
            raise ServiceError(400, 'Supply the preview reports of the notification extension and the app.') from None
        row = self.service.db.execute('SELECT data FROM push_previews WHERE device_id=?', (device_id,)).fetchone()
        line, previous = preview_line(report), preview_line(json.loads(row[0])) if row else ''
        # A newer working preview is not a change of state, so it is not logged.
        if line != previous and not (line.startswith('working') and previous.startswith('working')):
            name = self.service.db.execute('SELECT name FROM devices WHERE id=?', (device_id,)).fetchone()
            log = logging.getLogger(__name__)
            (log.warning if line.startswith('failing') else log.info)(
                'Notification previews on %s: %s', name[0] if name else device_id, line)
        self.service.db.execute('''INSERT INTO push_previews VALUES (?,?,?) ON CONFLICT(device_id)
            DO UPDATE SET data=excluded.data,reported=excluded.reported''', (device_id, json.dumps(report), time.time()))
        self.write_previews()
        return {'recorded': True}

    def write_previews(self):
        rows = self.service.db.execute(f'''SELECT p.device_id,d.name,p.data,p.reported FROM push_previews p
            JOIN devices d ON d.id=p.device_id WHERE p.device_id IN ({PHONES}) ORDER BY d.name''').fetchall()
        phones = [dict(device_id=device_id, name=name, reported_at=iso(reported), line=preview_line(json.loads(data)),
                       **json.loads(data)) for device_id, name, data, reported in rows]
        folder = Path(self.service.root)
        path = folder / PREVIEWS_FILE
        if not phones and not path.exists():
            return
        try:
            with tempfile.NamedTemporaryFile('w', dir=folder, prefix='.push-previews-', delete=False) as handle:
                json.dump({'phones': phones}, handle)
            os.chmod(handle.name, 0o600)
            os.replace(handle.name, path)
        except OSError:
            logging.getLogger(__name__).exception('Coconut could not write %s.', PREVIEWS_FILE)

    def note(self, device_id, error=None):
        """Record the phone's last delivered notification, or its last error."""
        column = 'last_error_at' if error else 'last_delivered_at'
        self.service.db.execute(f'''INSERT INTO push_status (device_id,{column},last_error) VALUES (?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET {column}=excluded.{column},
            last_error=CASE WHEN excluded.last_error IS NULL THEN last_error ELSE excluded.last_error END''',
            (device_id, time.time(), error))

    def status(self, device_id):
        """What the phone's notification settings show: its registration, choices, and the last delivery and error."""
        device = self.service.db.execute('SELECT enabled FROM push_devices WHERE device_id=?', (device_id,)).fetchone()
        row = self.service.db.execute('SELECT last_delivered_at,last_error,last_error_at FROM push_status WHERE device_id=?',
                                      (device_id,)).fetchone() or (None, None, None)
        return {'registered': bool(device and device[0]), 'configured': self.configured,
                'preferences': self.preferences(device_id), 'schedule': self.schedule(device_id),
                'last_delivered_at': iso(row[0]), 'last_error': row[1], 'last_error_at': iso(row[2]),
                'urgent': self.urgent_summary(time.time())}

    def urgent_summary(self, current, examples=3):
        """How many cards of the last week were urgent, and the last urgent cards, for "What counts as urgent?"."""
        db = self.service.db
        week = iso(current - 7 * 86400)
        # Each read uses an index (decisions_created, decisions_urgent), so the settings screen does not scan the history.
        total = db.execute("SELECT COUNT(*) FROM decisions WHERE json_extract(data,'$.created_at')>=?", (week,)).fetchone()[0]
        urgent = db.execute(f"SELECT COUNT(*) FROM decisions WHERE {URGENT_DECISION} AND json_extract(data,'$.created_at')>=?",
                            (week,)).fetchone()[0]
        rows = db.execute(f'''SELECT json_extract(d.data,'$.title'),json_extract(b.data,'$.name'),json_extract(d.data,'$.created_at')
            FROM decisions d LEFT JOIN bots b ON b.id=json_extract(d.data,'$.bot_id')
            WHERE {URGENT_DECISION.replace('data', 'd.data')} ORDER BY json_extract(d.data,'$.created_at') DESC LIMIT ?''',
            (examples,)).fetchall()
        return {'week_urgent': urgent, 'week_total': total,
                'examples': [{'title': title, 'bot_name': bot, 'created_at': created} for title, bot, created in rows]}

    def remove_stale(self, current):
        """Remove registrations of devices that are not paired phones, or that did not refresh for a long time."""
        db = self.service.db
        for table in PUSH_TABLES + ('push_status',):
            for (device_id,) in db.execute(f'SELECT device_id FROM {table} WHERE device_id NOT IN ({PHONES})').fetchall():
                self.unregister(device_id)
                db.execute('DELETE FROM push_status WHERE device_id=?', (device_id,))
        if db.execute(f'DELETE FROM push_previews WHERE device_id NOT IN ({PHONES})').rowcount:
            self.write_previews()
        for (device_id,) in db.execute('SELECT device_id FROM push_devices WHERE updated<?', (current - STALE_REGISTRATION,)).fetchall():
            self.unregister(device_id)
            self.note(device_id, 'NotRefreshed')

    def allows(self, device_id, kind, urgent=False):
        """Whether the phone's preferences let this item notify. A card that needs attention now always may."""
        chosen = self.stored_preferences(device_id)
        if kind == 'decision':
            return bool(urgent) or (chosen or LEGACY_PREFERENCES)['level'] != 'urgent_only'
        if chosen is None:
            return kind in ('failed', 'completed')
        return kind in ('message', 'failed') and chosen['messages'] and chosen['level'] != 'urgent_only'

    @staticmethod
    def visible_message(message, task):
        return (message.get('role') == 'assistant' and message.get('kind') in ('text', 'artifact')
                and not message.get('streaming') and not message.get('_hidden') and not message.get('_deleted')
                and not message.get('_interim')
                and not message.get('recipient_bot_ids') and not message.get('exchange_recipient_bot_ids')
                and (not task.get('source_task_id') or task.get('_is_return') or message.get('decision_id'))
                and bool(message.get('text', '').strip() or message.get('artifact_id')))

    def record(self, event_type, data, bot_id, task_id):
        if not task_id:
            return
        message_id = None
        if event_type == 'decision.created' and data.get('status') == 'pending':
            kind, decision_id = 'decision', data['id']
        elif event_type in ('message.created', 'message.completed'):
            kind, decision_id, message_id = 'message', None, data['id']
        elif event_type == 'task.updated' and data.get('status') in ('completed', 'failed'):
            kind, decision_id = data['status'], None
        else:
            return
        task = self.service._get('tasks', task_id)
        if message_id:
            message = self.service._get('messages', message_id)
            if not self.visible_message(message, task):
                return
            bot_id = message['bot_id']
        bot = self.service._get('bots', bot_id)
        if not bot.get('notifications', True) or bot.get('archived') or bot.get('_deleted'):
            return
        if kind == 'completed' and task.get('_silent_completion'):
            return
        # Peer replies return to the originating task, which sends its own completion.
        if kind == 'completed' and task.get('source_task_id'):
            return
        if kind == 'failed' and task.get('_assignment_task_id'):
            return
        turn = task.get('_native_turn_id') or task.get('updated_at') or task['created_at']
        event_key = ':'.join((task_id, kind, decision_id or message_id or turn))
        urgent = int(kind == 'decision' and data.get('needs_attention_now') is True)
        stamp = time.time()
        for (device_id,) in self.service.db.execute(f'SELECT device_id FROM push_devices WHERE enabled=1 AND device_id IN ({PHONES})').fetchall():
            if self.allows(device_id, kind, urgent):
                self.service.db.execute('''INSERT OR IGNORE INTO push_items
                    (device_id,event_key,kind,bot_id,task_id,decision_id,message_id,turn_id,urgent,created,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'waiting')''',
                    (device_id, event_key, kind, bot_id, task_id, decision_id, message_id, turn, urgent, stamp))

    def relevant(self, item):
        """Whether the item still describes something the person has not seen: an open card, a visible message,
        or a task that is still failed or finished in the same turn."""
        service = self.service
        task = service._get('tasks', item['task_id'])
        bot = service._get('bots', item['bot_id'])
        conversation = service._get('bots', task.get('conversation_bot_id', bot['id']))
        if (bot.get('_deleted') or bot.get('archived') or not bot.get('notifications', True)
                or conversation.get('_deleted') or conversation.get('archived') or not conversation.get('notifications', True)
                or not self.allows(item['device_id'], item['kind'], item['urgent'])):
            return False
        if item['decision_id']:
            decision = service._get('decisions', item['decision_id'])
            # A held answer (answer_undo.py) means the person already answered the card.
            return decision['status'] == 'pending' and not decision.get('_hidden', False) and not decision.get('_held_answer')
        if item['message_id']:
            return self.visible_message(service._get('messages', item['message_id']), task)
        turn = task.get('_native_turn_id') or task.get('updated_at') or task['created_at']
        return task['status'] == item['kind'] and turn == item['turn_id']

    def inbox_badge_count(self):
        visible = {b['id'] for b in self.service._all('bots') if not b.get('_deleted')}
        count = 0
        for (raw,) in self.service.db.execute("SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending'"):
            decision = json.loads(raw)
            if any(decision.get(key) for key in ('_hidden', '_deleted', '_source_recheck')) or decision['bot_id'] not in visible:
                continue
            task = self.service._get('tasks', decision['task_id'])
            if task['bot_id'] in visible and task.get('conversation_bot_id', task['bot_id']) in visible:
                count += 1
        return count

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._run, name='coconut-push', daemon=True)
        self.thread.start()

    def _run(self):
        while not self.service.stopping.is_set():
            try:
                sent = self.deliver_one()
            except Exception:
                # Notification failure must not stop agent work or expose payloads in logs.
                sent = False
            if not sent:
                self.service.stopping.wait(2)

    def _items(self, where, values):
        cursor = self.service.db.execute(f'SELECT * FROM push_items WHERE {where} ORDER BY created,rowid', values)
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _queue(self, device_id, key, items, current):
        """Write one notification for `items`. The same key always gives the same notification ID."""
        notification_id = str(uuid5(NAMESPACE_URL, device_id + ':' + key))
        self.service.db.execute('''INSERT OR IGNORE INTO push_outbox (id,device_id,event_key,status,created,not_before,expires)
            VALUES (?,?,?,'queued',?,?,?)''', (notification_id, device_id, key, current, current, current + 86400))
        for item in items:
            self.service.db.execute("UPDATE push_items SET status='queued',delivery_id=? WHERE device_id=? AND event_key=?",
                                    (notification_id, device_id, item['event_key']))

    def plan(self, current):
        """Turn waiting items into notifications that are due now."""
        db = self.service.db
        if current - self.pruned > 3600:
            self.pruned = current
            db.execute("UPDATE push_items SET status='dropped',reason='Expired' WHERE status='waiting' AND created<?", (current - ITEM_LIFETIME,))
            db.execute("DELETE FROM push_items WHERE status<>'waiting' AND created<?", (current - HISTORY,))
            db.execute("DELETE FROM push_outbox WHERE status<>'queued' AND created<?", (current - HISTORY,))
            self.remove_stale(current)
        for (device_id,) in db.execute("SELECT DISTINCT device_id FROM push_items WHERE status='waiting'").fetchall():
            device = db.execute('SELECT enabled FROM push_devices WHERE device_id=?', (device_id,)).fetchone()
            items = self._items("device_id=? AND status='waiting'", (device_id,))
            schedule, level = self.schedule(device_id), self.preferences(device_id)['level']
            # With Nothing outside work hours, an urgent card waits while work hours are closed, and also when it arrived
            # outside them. It then joins the count and follows the timing of the other waiting items.
            quiet = holds_urgent(schedule, current)
            for item in items:
                item['held'] = bool(item['urgent']) and (quiet or holds_urgent(schedule, item['created']))
            alone = lambda item: item['urgent'] and not item['held']
            waiting = [item for item in items if not alone(item)]
            # Only urgent has no timing of its own: its held urgent cards wait as One summary at a time.
            timing = level if level in ('every', 'grouped') else 'grouped'
            # The gap counts from the last delivered notification. With Grouped, new items also wait while a group retries.
            # Every card has no gap, so a retrying group does not hold its new items.
            last = db.execute("SELECT MAX(sent) FROM push_outbox WHERE device_id=? AND status='accepted'", (device_id,)).fetchone()[0]
            pending = timing == 'grouped' and db.execute(
                "SELECT 1 FROM push_outbox WHERE device_id=? AND status='queued' AND event_key LIKE 'group:%'", (device_id,)).fetchone()
            opens = due(schedule, timing, waiting[0]['created'], last or 0, current) if waiting and not pending else None
            # Items are checked only when they would notify, so a long wait outside work hours costs no reads.
            ready = [item for item in items if alone(item) or (opens is not None and opens <= current)]
            for item in ready:
                if not device or not device[0] or not self.relevant(item):
                    db.execute("UPDATE push_items SET status='dropped',reason='NoLongerRelevant' WHERE device_id=? AND event_key=?",
                               (device_id, item['event_key']))
                    item['dropped'] = True
            for item in ready:
                if alone(item) and not item.get('dropped'):
                    self._queue(device_id, 'item:' + item['event_key'], [item], current)
            group = [item for item in ready if not alone(item) and not item.get('dropped')]
            if level == 'every':
                # Every card: an item that arrived inside work hours notifies alone. Items held from outside them notify as one group.
                for item in [item for item in group if in_window(schedule, item['created']) or not schedule['enabled']]:
                    self._queue(device_id, 'item:' + item['event_key'], [item], current)
                    group.remove(item)
            if group:
                self._queue(device_id, 'group:' + group[0]['event_key'], group, current)

    def _payload(self, device_id, items, sealed):
        """The notification for one item, or the count of several. Items are rechecked when the text is written."""
        service = self.service
        if len(items) > 1:
            # Apps before groups need a bot, task, and card to open a notification. They open the first card in Inbox.
            first = next((item for item in items if item['decision_id']), items[0])
            task = service._get('tasks', first['task_id'])
            return {'aps': {'alert': {'title': 'Coconut', 'body': group_text(items)}, 'sound': 'default', 'thread-id': 'coconut-inbox'},
                    'coconut': {'version': 1, 'device_id': device_id, 'kind': 'group', 'count': len(items),
                                'bot_id': task.get('conversation_bot_id', task['bot_id']), 'task_id': task['id'],
                                'decision_id': first['decision_id'], 'message_id': None}}, None
        item = items[0]
        task = service._get('tasks', item['task_id'])
        bot = service._get('bots', item['bot_id'])
        conversation = service._get('bots', task.get('conversation_bot_id', bot['id']))
        decision = service._get('decisions', item['decision_id']) if item['decision_id'] else None
        bodies = {'message': 'A bot sent you a message.', 'decision': 'A bot needs your decision.', 'completed': 'A bot finished its task.', 'failed': 'A bot could not finish its task.'}
        payload = {'aps': {'alert': {'title': 'Coconut', 'body': bodies[item['kind']]}, 'sound': 'default',
                           'thread-id': task.get('thread_id', task['id'])},
                   'coconut': {'version': 1, 'device_id': device_id, 'bot_id': task['bot_id'] if task.get('source_bot_id') else conversation['id'],
                               'task_id': task['id'], 'decision_id': item['decision_id'], 'message_id': item['message_id'], 'kind': item['kind']}}
        if sealed:
            payload['aps']['alert'] = notification_alert(service, item, bot, task, decision)
        return payload, item['decision_id'] or item['message_id'] or task['id']

    def deliver_one(self):
        if not self.configured:
            return False
        service = self.service
        with service.transaction():
            self.plan(time.time())
        with service.transaction():
            row = service.db.execute("SELECT * FROM push_outbox WHERE status='queued' AND not_before<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return False
            names = [c[1] for c in service.db.execute('PRAGMA table_info(push_outbox)')]
            job = dict(zip(names, row))
            device = service.db.execute('SELECT token,environment,enabled,updated FROM push_devices WHERE device_id=?', (job['device_id'],)).fetchone()
            route_row = service.db.execute('SELECT data FROM push_routes WHERE device_id=?', (job['device_id'],)).fetchone()
            route = json.loads(route_row[0]) if route_row else None
            # With Nothing outside work hours, a notification that work hours closed on before its first send goes back to
            # Inbox. Its items wait again, so the next plan applies the current choice and queues them with a new expiry.
            # It never reached the relay, so the same ID is safe. A notification that already tried to send keeps its ID
            # and retries: the relay may have delivered it, and it sends each ID only once.
            if job['attempts'] == 0 and holds_urgent(self.schedule(job['device_id']), time.time()):
                service.db.execute('DELETE FROM push_outbox WHERE id=?', (job['id'],))
                service.db.execute("UPDATE push_items SET status='waiting',delivery_id=NULL WHERE delivery_id=?", (job['id'],))
                return True
            items = self._items('delivery_id=?', (job['id'],))
            live = [item for item in items if self.relevant(item)]
            # Every send checks again, so a muted bot or category stops a retry too. A sealed retry keeps its bytes.
            if job['expires'] <= time.time() or not device or not device[2] or not live:
                service.db.execute("UPDATE push_outbox SET status='discarded',reason='NoLongerRelevant' WHERE id=?", (job['id'],))
                return True
            if not job['envelope']:
                items = live
            payload, subject = self._payload(job['device_id'], items, sealed=bool(route))
            if route:
                payload['aps']['badge'] = self.inbox_badge_count()
            collapse = hashlib.sha256((subject or job['id']).encode()).hexdigest()
            if route:
                if job['envelope']:
                    envelope = json.loads(job['envelope'])
                else:
                    try:
                        envelope = self.relay.seal(route['public_key'], payload, job['id'], job['expires'])
                    except Exception:
                        delay = min(3600, 30 * 2 ** min(job['attempts'], 7))
                        service.db.execute("UPDATE push_outbox SET attempts=attempts+1,not_before=?,reason='EncryptionUnavailable' WHERE id=?",
                                           (time.time() + delay, job['id']))
                        self.note(job['device_id'], 'EncryptionUnavailable')
                        return True
                    service.db.execute('UPDATE push_outbox SET envelope=? WHERE id=?', (json.dumps(envelope), job['id']))
            service.db.execute('UPDATE push_outbox SET attempts=attempts+1,not_before=? WHERE id=?', (time.time() + 30, job['id']))
        if route:
            status, reason = self.relay.send(route, envelope, job['id'], collapse, job['expires'])
        else:
            status, reason = self.transport.send(device[0], device[1], payload, job['id'], collapse, job['expires'])
        with service.transaction():
            self.note(job['device_id'], None if status == 200 else reason)
            if status == 200:
                state = 'accepted'
            # APNs or the relay no longer knows the phone: an invalid token, or a relay route that refuses the send token.
            elif status == 410 or (status == 400 and reason in ('BadDeviceToken', 'DeviceTokenNotForTopic')) or (route and status == 401):
                state = 'failed'
                # A registration that changed during the send is new, so the answer does not apply to it.
                if service.db.execute('SELECT 1 FROM push_devices WHERE device_id=? AND token=? AND environment=? AND updated=?',
                                      (job['device_id'], device[0], device[1], device[3])).fetchone():
                    self.unregister(job['device_id'])
            elif status in (0, 429, 500, 503) or status == 403:
                state = 'queued'
            else:
                state = 'failed'
            delay = min(3600, 30 * 2 ** min(job['attempts'], 7))
            service.db.execute("UPDATE push_outbox SET status=?,reason=?,not_before=?,sent=? WHERE id=? AND status='queued'",
                               (state, reason, time.time() + delay, time.time() if state == 'accepted' else None, job['id']))
            if state != 'queued':
                service.db.execute("UPDATE push_items SET status=? WHERE delivery_id=?", ('sent' if state == 'accepted' else 'failed', job['id']))
        return True

    def close(self):
        if self.thread:
            self.thread.join(timeout=18)
