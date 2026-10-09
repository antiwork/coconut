"""Durable, private notification delivery through Apple APNs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from uuid import NAMESPACE_URL, uuid5
from .push_relay import RelayTransport, validate_route, notification_alert
from .push_schedule import LEGACY_SCHEDULE, effective_importance, explicit_importance, in_window, merge as merge_schedule, next_opening, stored as stored_schedule


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
        self.service.db.executescript('''
        CREATE TABLE IF NOT EXISTS push_devices (
            device_id TEXT PRIMARY KEY, token TEXT NOT NULL, environment TEXT NOT NULL,
            enabled INTEGER NOT NULL, updated REAL NOT NULL, UNIQUE(token, environment));
        CREATE TABLE IF NOT EXISTS push_outbox (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, event_key TEXT NOT NULL,
            bot_id TEXT NOT NULL, task_id TEXT NOT NULL, decision_id TEXT, kind TEXT NOT NULL,
            turn_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            not_before REAL NOT NULL, expires REAL NOT NULL, reason TEXT,
            UNIQUE(device_id, event_key));
        CREATE TABLE IF NOT EXISTS push_preferences (
            device_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS push_schedules (
            device_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS push_routes (
            device_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        ''')
        columns = {row[1] for row in service.db.execute('PRAGMA table_info(push_outbox)')}
        for column, kind in (('envelope', 'TEXT'), ('message_id', 'TEXT'), ('importance', 'TEXT'), ('explicit_importance', 'TEXT'), ('importance_reason', 'TEXT'),
                             ('deferred_reason', 'TEXT'), ('deferred_until', 'REAL')):
            if column not in columns:
                service.db.execute(f'ALTER TABLE push_outbox ADD COLUMN {column} {kind}')

    @property
    def configured(self):
        return self.relay.configured or self.transport.configured

    def registration(self, device_id, data):
        from .service import ServiceError
        if not self.service.db.execute('SELECT 1 FROM devices WHERE id=?', (device_id,)).fetchone():
            raise ServiceError(401, 'Pair this iPhone before enabling notifications.')
        token, environment, enabled = data.get('token'), data.get('environment'), data.get('enabled')
        if (not isinstance(token, str) or not re.fullmatch(r'[0-9a-fA-F]{32,512}', token)
                or len(token) % 2 or environment not in ('development', 'production') or type(enabled) is not bool):
            raise ServiceError(400, 'Supply an APNs token, environment, and notification preference.')
        preferences = data.get('preferences')
        if preferences is not None and (not isinstance(preferences, dict)
                or set(preferences) != {'decisions', 'messages'}
                or any(type(value) is not bool for value in preferences.values())):
            raise ServiceError(400, 'Supply decision and message notification preferences.')
        schedule = data.get('schedule')
        if schedule is not None:
            schedule = merge_schedule(self.schedule(device_id), schedule)
            if schedule is None:
                raise ServiceError(400, 'Supply start before end as HH:MM, an IANA timezone, at least one weekday from 1 to 7, and true or false for enabled and honor_routine.')
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
        self.service.db.execute('''INSERT INTO push_devices VALUES (?,?,?,?,?) ON CONFLICT(device_id)
            DO UPDATE SET token=excluded.token,environment=excluded.environment,enabled=excluded.enabled,updated=excluded.updated''',
            (device_id, token, environment, int(enabled), time.time()))
        if preferences is not None:
            self.service.db.execute('INSERT INTO push_preferences VALUES (?,?) ON CONFLICT(device_id) DO UPDATE SET data=excluded.data',
                                    (device_id, json.dumps(preferences)))
            for kind in ('decision', 'message', 'failed', 'completed'):
                if not self.allows(device_id, kind):
                    self.service.db.execute("UPDATE push_outbox SET status='discarded',reason='Disabled' WHERE device_id=? AND kind=? AND status='queued'",
                                            (device_id, kind))
        if schedule is not None:
            self.set_schedule(device_id, schedule)
        if not enabled:
            self.unregister_jobs(device_id)
        return {'registered': True, 'configured': self.configured}

    def schedule(self, device_id):
        row = self.service.db.execute('SELECT data FROM push_schedules WHERE device_id=?', (device_id,)).fetchone()
        return stored_schedule(json.loads(row[0])) if row else dict(LEGACY_SCHEDULE)

    def set_schedule(self, device_id, schedule):
        self.service.db.execute('INSERT INTO push_schedules VALUES (?,?) ON CONFLICT(device_id) DO UPDATE SET data=excluded.data',
                                (device_id, json.dumps(schedule)))
        # Alerts held by the schedule are released to check the new one at once. Delivery defers them again if it still applies.
        # Rows in a retry backoff are not held by the schedule and keep their delay.
        self.service.db.execute("UPDATE push_outbox SET not_before=?,deferred_until=NULL,deferred_reason=NULL "
                                "WHERE device_id=? AND status='queued' AND deferred_reason='OffHours' AND deferred_until>?",
                                (time.time(), device_id, time.time()))

    def unregister_jobs(self, device_id):
        self.service.db.execute("UPDATE push_outbox SET status='discarded',reason='Disabled' WHERE device_id=? AND status='queued'", (device_id,))

    def unregister(self, device_id):
        self.service.db.execute('DELETE FROM push_devices WHERE device_id=?', (device_id,))
        self.service.db.execute('DELETE FROM push_routes WHERE device_id=?', (device_id,))
        self.service.db.execute('DELETE FROM push_preferences WHERE device_id=?', (device_id,))
        self.service.db.execute('DELETE FROM push_schedules WHERE device_id=?', (device_id,))
        self.unregister_jobs(device_id)
        return {'registered': False, 'configured': self.configured}

    def allows(self, device_id, kind):
        row = self.service.db.execute('SELECT data FROM push_preferences WHERE device_id=?', (device_id,)).fetchone()
        # Older phones keep task alerts until they register the new categories.
        if not row:
            return kind != 'message'
        preferences = json.loads(row[0])
        return kind != 'completed' and preferences['decisions' if kind == 'decision' else 'messages']

    @staticmethod
    def visible_message(message, task):
        return (message.get('role') == 'assistant' and message.get('kind') in ('text', 'artifact')
                and not message.get('streaming') and not message.get('_hidden') and not message.get('_deleted')
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
        stamp = time.time()
        explicit = explicit_importance(data.get('notification_importance')) if kind == 'decision' else None
        for (device_id,) in self.service.db.execute('SELECT device_id FROM push_devices WHERE enabled=1'):
            if not self.allows(device_id, kind):
                continue
            notification_id = str(uuid5(NAMESPACE_URL, device_id + ':' + event_key))
            self.service.db.execute('''INSERT OR IGNORE INTO push_outbox
                (id,device_id,event_key,bot_id,task_id,decision_id,kind,turn_id,message_id,status,not_before,expires,explicit_importance)
                VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?,?)''',
                (notification_id, device_id, event_key, bot_id, task_id, decision_id, kind, turn, message_id, stamp + 2, stamp + 86400, explicit))

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

    def deliver_one(self):
        if not self.configured:
            return False
        service = self.service
        with service.transaction():
            row = service.db.execute("SELECT * FROM push_outbox WHERE status='queued' AND not_before<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return False
            names = [c[1] for c in service.db.execute('PRAGMA table_info(push_outbox)')]
            job = dict(zip(names, row))
            device = service.db.execute('SELECT token,environment,enabled,updated FROM push_devices WHERE device_id=?', (job['device_id'],)).fetchone()
            route_row = service.db.execute('SELECT data FROM push_routes WHERE device_id=?', (job['device_id'],)).fetchone()
            route = json.loads(route_row[0]) if route_row else None
            task = service._get('tasks', job['task_id'])
            bot = service._get('bots', job['bot_id'])
            conversation = service._get('bots', task.get('conversation_bot_id', bot['id']))
            stale = (job['expires'] <= time.time() or not device or not device[2]
                     or bot.get('_deleted') or bot.get('archived') or not bot.get('notifications', True)
                     or conversation.get('_deleted') or conversation.get('archived')
                     or not conversation.get('notifications', True) or not self.allows(job['device_id'], job['kind']))
            decision = None
            if job['decision_id']:
                decision = service._get('decisions', job['decision_id'])
                stale = stale or decision['status'] != 'pending' or decision.get('_hidden', False)
            elif job['message_id']:
                message = service._get('messages', job['message_id'])
                stale = stale or not self.visible_message(message, task)
            else:
                turn = task.get('_native_turn_id') or task.get('updated_at') or task['created_at']
                stale = stale or task['status'] != job['kind'] or turn != job['turn_id']
            if stale:
                service.db.execute("UPDATE push_outbox SET status='discarded',reason='NoLongerRelevant' WHERE id=?", (job['id'],))
                return True
            # Only routine alerts wait. The kind's floor, or an explicit urgent or important, keeps immediate delivery.
            schedule, current = self.schedule(job['device_id']), time.time()
            level, why = effective_importance(job['kind'], job['explicit_importance'], schedule['honor_routine'])
            # A sealed envelope fixes its text, badge, and expiry, so a retry never waits again.
            held = schedule['enabled'] and level == 'routine' and not in_window(schedule, current) and not job['envelope']
            opening = next_opening(schedule, current) if held else None
            service.db.execute('UPDATE push_outbox SET importance=?,importance_reason=? WHERE id=?', (level, why, job['id']))
            if not held:
                service.db.execute('UPDATE push_outbox SET deferred_until=NULL,deferred_reason=NULL WHERE id=?', (job['id'],))
            if held:
                service.db.execute("UPDATE push_outbox SET not_before=?,deferred_until=?,deferred_reason='OffHours',expires=MAX(expires,?) WHERE id=?",
                                   (opening, opening, opening + 86400, job['id']))
                return True
            # A relay accepts at most 24 hours from delivery. An alert released early keeps the expiry it got when held, so cap it before sealing.
            expires = job['expires'] if job['envelope'] else min(job['expires'], current + 86400)
            if expires != job['expires']:
                service.db.execute('UPDATE push_outbox SET expires=? WHERE id=?', (expires, job['id']))
            bodies = {'message': 'A bot sent you a message.', 'decision': 'A bot needs your decision.', 'completed': 'A bot finished its task.', 'failed': 'A bot could not finish its task.'}
            payload = {'aps': {'alert': {'title': 'Coconut', 'body': bodies[job['kind']]}, 'sound': 'default',
                               'thread-id': task.get('thread_id', task['id'])},
                       'coconut': {'version': 1, 'device_id': job['device_id'], 'bot_id': task['bot_id'] if task.get('source_bot_id') else conversation['id'],
                                   'task_id': task['id'], 'decision_id': job['decision_id'], 'message_id': job['message_id'], 'kind': job['kind']}}
            if route:
                payload['aps']['badge'] = self.inbox_badge_count()
            collapse = hashlib.sha256((job['decision_id'] or job['message_id'] or task['id']).encode()).hexdigest()
            if route:
                if job['envelope']:
                    envelope = json.loads(job['envelope'])
                else:
                    payload['aps']['alert'] = notification_alert(service, job, bot, task, decision)
                    try:
                        envelope = self.relay.seal(route['public_key'], payload, job['id'], expires)
                    except Exception:
                        delay = min(3600, 30 * 2 ** min(job['attempts'], 7))
                        service.db.execute("UPDATE push_outbox SET attempts=attempts+1,not_before=?,reason='EncryptionUnavailable' WHERE id=?",
                                           (time.time() + delay, job['id']))
                        return True
                    service.db.execute('UPDATE push_outbox SET envelope=? WHERE id=?', (json.dumps(envelope), job['id']))
            service.db.execute('UPDATE push_outbox SET attempts=attempts+1,not_before=? WHERE id=?', (time.time() + 30, job['id']))
        if route:
            status, reason = self.relay.send(route, envelope, job['id'], collapse, expires)
        else:
            status, reason = self.transport.send(device[0], device[1], payload, job['id'], collapse, expires)
        with service.transaction():
            if status == 200:
                state = 'accepted'
            elif status == 410 or (status == 400 and reason in ('BadDeviceToken', 'DeviceTokenNotForTopic')):
                state = 'failed'
                service.db.execute('UPDATE push_devices SET enabled=0 WHERE device_id=? AND token=? AND environment=? AND updated=?',
                                   (job['device_id'], device[0], device[1], device[3]))
            elif status in (0, 429, 500, 503) or status == 403:
                state = 'queued'
            else:
                state = 'failed'
            delay = min(3600, 30 * 2 ** min(job['attempts'], 7))
            service.db.execute("UPDATE push_outbox SET status=?,reason=?,not_before=? WHERE id=? AND status='queued'",
                               (state, reason, time.time() + delay, job['id']))
        return True

    def close(self):
        if self.thread:
            self.thread.join(timeout=18)
