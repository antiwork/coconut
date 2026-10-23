"""Answer the phone while this Mac has no usable Hermes.

In this refuse mode the service keeps /health, pairing and GET /v1/setup, so the phone can say why Coconut cannot work.
Every other route answers 503 with the code hermes_unavailable. Nothing here imports Hermes or runs a bot, and no task
changes state, so the bots continue when the person installs or updates Hermes and restarts Coconut.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import threading

from .hermes_runtime import DEFAULT_HOME, public
from .hermes_setup import HermesSetup
from .service import ServiceError, device_for_token, migrate_device_peers, now, pair_device
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

    # Nothing runs, so an update can stop this process at once. See host.drain_for_update.
    drains = False
    restart_enabled = False

    def __init__(self, data_dir, hermes: dict):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.process_lock = open(self.root / 'service.lock', 'a')
        try:
            fcntl.flock(self.process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.process_lock.close()
            raise ServiceError(409, 'Another Coconut service already owns this data directory.')
        self.hermes_status = public(hermes)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.root / 'coconut.sqlite3', check_same_thread=False, isolation_level=None, timeout=10)
        os.chmod(self.root / 'coconut.sqlite3', 0o600)
        # As in Service: this takes effect only in a new store.
        self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
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

    def _setup_source(self) -> Path | None:
        # HermesSetup reads the shared home from the provider. Refuse mode has no provider.
        root = super()._setup_source()
        return None if root is not None and (root == DEFAULT_HOME.resolve() or DEFAULT_HOME.resolve() in root.parents) else root

    def _source_profiles(self) -> dict:
        with self.lock:
            bots = [json.loads(row[0]) for row in self.db.execute('SELECT data FROM bots')]
        return {bot['source_profile']: bot['id'] for bot in bots if bot.get('source_profile') and not bot.get('_deleted')}

    def take_service_restart(self) -> bool:
        return False

    clear_service_drain = ServiceRestart.clear_service_drain

    def start(self):
        pass

    def close(self):
        self.db.close()
        fcntl.flock(self.process_lock.fileno(), fcntl.LOCK_UN)
        self.process_lock.close()
