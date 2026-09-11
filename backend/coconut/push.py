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
        CREATE TABLE IF NOT EXISTS push_routes (
            device_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        ''')
        if 'envelope' not in {row[1] for row in service.db.execute('PRAGMA table_info(push_outbox)')}:
            service.db.execute('ALTER TABLE push_outbox ADD COLUMN envelope TEXT')

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
        if not enabled:
            self.unregister_jobs(device_id)
        return {'registered': True, 'configured': self.configured}

    def unregister_jobs(self, device_id):
        self.service.db.execute("UPDATE push_outbox SET status='discarded',reason='Disabled' WHERE device_id=? AND status='queued'", (device_id,))

    def unregister(self, device_id):
        self.service.db.execute('DELETE FROM push_devices WHERE device_id=?', (device_id,))
        self.service.db.execute('DELETE FROM push_routes WHERE device_id=?', (device_id,))
        self.unregister_jobs(device_id)
        return {'registered': False, 'configured': self.configured}

    def record(self, event_type, data, bot_id, task_id):
        if event_type == 'decision.created' and data.get('status') == 'pending':
            kind, decision_id = 'decision', data['id']
        elif event_type == 'task.updated' and data.get('status') in ('completed', 'failed'):
            kind, decision_id = data['status'], None
        else:
            return
        task = self.service._get('tasks', task_id)
        bot = self.service._get('bots', bot_id)
        if not bot.get('notifications', True) or bot.get('archived') or bot.get('_deleted'):
            return
        # Peer replies return to the originating task, which sends its own completion.
        if kind == 'completed' and task.get('source_task_id'):
            return
        if kind == 'failed' and task.get('_assignment_task_id'):
            return
        turn = task.get('_native_turn_id') or task.get('updated_at') or task['created_at']
        event_key = ':'.join((task_id, kind, decision_id or turn))
        stamp = time.time()
        for (device_id,) in self.service.db.execute('SELECT device_id FROM push_devices WHERE enabled=1'):
            notification_id = str(uuid5(NAMESPACE_URL, device_id + ':' + event_key))
            self.service.db.execute('''INSERT OR IGNORE INTO push_outbox
                (id,device_id,event_key,bot_id,task_id,decision_id,kind,turn_id,status,not_before,expires)
                VALUES (?,?,?,?,?,?,?,?,'queued',?,?)''',
                (notification_id, device_id, event_key, bot_id, task_id, decision_id, kind, turn, stamp + 2, stamp + 86400))

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
                     or conversation.get('_deleted') or conversation.get('archived'))
            decision = None
            if job['decision_id']:
                decision = service._get('decisions', job['decision_id'])
                stale = stale or decision['status'] != 'pending' or decision.get('_hidden', False)
            else:
                turn = task.get('_native_turn_id') or task.get('updated_at') or task['created_at']
                stale = stale or task['status'] != job['kind'] or turn != job['turn_id']
            if stale:
                service.db.execute("UPDATE push_outbox SET status='discarded',reason='NoLongerRelevant' WHERE id=?", (job['id'],))
                return True
            bodies = {'decision': 'A bot needs your decision.', 'completed': 'A bot finished its task.', 'failed': 'A bot could not finish its task.'}
            payload = {'aps': {'alert': {'title': 'Coconut', 'body': bodies[job['kind']]}, 'sound': 'default',
                               'thread-id': task.get('thread_id', task['id'])},
                       'coconut': {'version': 1, 'device_id': job['device_id'], 'bot_id': task['bot_id'] if task.get('source_bot_id') else conversation['id'],
                                   'task_id': task['id'], 'decision_id': job['decision_id'], 'kind': job['kind']}}
            collapse = hashlib.sha256(task['id'].encode()).hexdigest()
            if route:
                if job['envelope']:
                    envelope = json.loads(job['envelope'])
                else:
                    payload['aps']['alert'] = notification_alert(service, job, bot, task, decision)
                    try:
                        envelope = self.relay.seal(route['public_key'], payload, job['id'], job['expires'])
                    except Exception:
                        delay = min(3600, 30 * 2 ** min(job['attempts'], 7))
                        service.db.execute("UPDATE push_outbox SET attempts=attempts+1,not_before=?,reason='EncryptionUnavailable' WHERE id=?",
                                           (time.time() + delay, job['id']))
                        return True
                    service.db.execute('UPDATE push_outbox SET envelope=? WHERE id=?', (json.dumps(envelope), job['id']))
            service.db.execute('UPDATE push_outbox SET attempts=attempts+1,not_before=? WHERE id=?', (time.time() + 30, job['id']))
        if route:
            status, reason = self.relay.send(route, envelope, job['id'], collapse, job['expires'])
        else:
            status, reason = self.transport.send(device[0], device[1], payload, job['id'], collapse, job['expires'])
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
