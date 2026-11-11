"""Answer the phone while this Mac has no usable Hermes.

In this refuse mode the service keeps /health, pairing and GET /v1/setup, so the phone can say why Coconut cannot work.
Every other route answers 503 with the code hermes_unavailable. Nothing here imports Hermes or runs a bot, and no task
changes state, so the bots continue when the person installs or updates Hermes. The service then restarts by itself.
"""
from __future__ import annotations

import json
import threading

from .hermes_runtime import public
from .hermes_setup import HermesSetup
from .service import ServiceError, device_for_token, migrate_device_peers, now, pair_device
from .service_base import open_store, release_data_folder, take_data_folder
from .service_restart import ServiceRestart

# The same tables as in Service. A later start with Hermes reads them unchanged.
SCHEMA = '''
CREATE TABLE IF NOT EXISTS bots (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, bot_id TEXT, task_id TEXT, data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pairing (hash TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS pair_attempts (created REAL NOT NULL);
'''


class UnavailableService(HermesSetup):
    """The service object of refuse mode. server.Handler calls the same methods as on Service."""

    # server.Handler answers only the setup status of this service (refuse).
    setup_only = True

    # Nothing runs, so an update can stop this process at once. See host.drain_for_update.
    drains = False
    restart_enabled = False

    def __init__(self, data_dir, hermes: dict):
        self.root, self.process_lock = take_data_folder(data_dir)
        self.hermes_status = public(hermes)
        self.lock = threading.Lock()
        self.db = open_store(self.root, timeout=10)
        self.db.executescript(SCHEMA)
        migrate_device_peers(self.db)

    def refuse(self):
        raise ServiceError(503, self.hermes_status['reason'] or 'Coconut cannot use the Hermes on this Mac.',
                           code='hermes_unavailable', hermes=self.hermes_status)

    def pair(self, data: dict, peer: str | None = None) -> dict:
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                result = pair_device(self.db, data, lambda device_id: self.db.execute(
                    'INSERT INTO events(type,bot_id,task_id,data,created_at) VALUES (?,?,?,?,?)',
                    ('device.paired', None, None, json.dumps({'device_id': device_id}), now())), peer)
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        if isinstance(result, ServiceError):
            raise result
        return result

    def authenticate(self, token: str, peer: str | None = None) -> str:
        with self.lock:
            return device_for_token(self.db, token, peer)

    def take_service_restart(self) -> bool:
        # Nothing runs, so a new Hermes takes effect at once: the next start checks it.
        return self.hermes_changed()

    clear_service_drain = ServiceRestart.clear_service_drain

    def start(self):
        pass

    def close(self):
        self.db.close()
        release_data_folder(self.process_lock)
