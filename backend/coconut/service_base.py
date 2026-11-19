"""What each service object of this package needs, with the standard library only.

Service (service.py), the refuse mode without Hermes (hermes_unavailable.py) and the team-only service (team_only.py)
share these helpers. The team modules and documents.py import them from here, so a team-only service never imports
service.py and the modules of the full runtime.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, IO
from uuid import uuid4


class ServiceError(Exception):
    def __init__(self, status: int, message: str, **details):
        super().__init__(message)
        self.status = status
        # Extra fields of the error response, for example a code that the phone acts on.
        self.details = details


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def instant(value) -> datetime | None:
    """A saved time as an aware datetime, or None when the value is not a time. A time without a zone counts as UTC."""
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def uid() -> str:
    return str(uuid4())


def encoded(value: Any) -> str:
    return json.dumps(value, separators=(',', ':'), sort_keys=True)


def require_text(data: dict, key: str, maximum: int | None = 10000) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or (maximum is not None and len(value) > maximum):
        bound = f'between 1 and {maximum} characters' if maximum is not None else 'nonempty text'
        raise ServiceError(400, f'{key} must contain {bound}.')
    return value.strip()


def take_data_folder(data_dir: str | Path) -> tuple[Path, IO]:
    """Make the data folder private, and take its service lock. Return the folder and the open lock file.

    One service process owns a data folder. The lock goes when the process closes the file or exits.
    """
    root = Path(data_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    process_lock = open(root / 'service.lock', 'a')
    try:
        fcntl.flock(process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        process_lock.close()
        raise ServiceError(409, 'Another Coconut service already owns this data directory.')
    return root, process_lock


# A team-only service (team_only.py) writes this mark in its store at its first start. Only the mark tells the mode of a
# folder: a folder that a team-only service opened is its folder, also before it has a bot or a room.
TEAM_MODE = 'team'
FULL_MODE = 'full'
MODE_MARK = '''CREATE TABLE IF NOT EXISTS service_mode (mode TEXT PRIMARY KEY);
INSERT OR IGNORE INTO service_mode (mode) VALUES ('team');'''
# The tables that only a full install fills.
FULL_INSTALL_TABLES = ('devices', 'tasks', 'messages', 'profiles', 'bots')
WRONG_FOLDER = {
    TEAM_MODE: 'This data folder belongs to a full Coconut install, so the team-only service does not start here. Use '
               'another data folder.',
    FULL_MODE: 'This data folder belongs to a team-only Coconut service, so the full service does not start here. Use '
               'another data folder.',
}


def folder_mode(db) -> str | None:
    """The mode of the service that owns a store: TEAM_MODE with the mark, FULL_MODE with the data of a full install
    (a phone, a task, a message, a Hermes profile or a bot), or None for a store that no service used."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'service_mode' in tables and db.execute('SELECT 1 FROM service_mode WHERE mode=?', (TEAM_MODE,)).fetchone():
        return TEAM_MODE
    if any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in FULL_INSTALL_TABLES if table in tables):
        return FULL_MODE
    return None


HERMES_REFUSAL = ('This data folder belongs to a team-only Coconut service, so Coconut changes nothing in Hermes. Run: '
                  'open -g /Applications/Coconut.app --args --set-up --team-only')


def team_folder(data_dir) -> bool:
    """True when the store in the data folder has the mark of a team-only service.

    Each step that finds, checks or changes Hermes asks this first (deploy.py, scripts/hermes_skills.py,
    scripts/setup_coconut.py), so a lost preference of Coconut.app never makes a team-only Mac a full install. The store
    opens read-only: the check writes nothing and makes no file. A missing store has no mark."""
    path = Path(data_dir) / 'coconut.sqlite3'
    if not path.is_file():
        return False
    with contextlib.closing(sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True, timeout=10)) as db:
        return folder_mode(db) == TEAM_MODE


def check_data_folder(root: Path, process_lock: IO, mode: str) -> None:
    """Stop the start of a service of `mode` on the folder of the other mode, before the service writes anything.
    The full service and the refuse mode would read the bot records of a team-only Mac that they do not understand,
    and a team-only service never takes over a full install. The caller holds the service lock. A refusal releases it."""
    path = root / 'coconut.sqlite3'
    if not path.exists():
        return
    try:
        with contextlib.closing(sqlite3.connect(path, timeout=10)) as db:
            found = folder_mode(db)
    except BaseException:
        release_data_folder(process_lock)
        raise
    if found is not None and found != mode:
        release_data_folder(process_lock)
        raise ServiceError(409, WRONG_FOLDER[mode], code='data_folder_mode')


def release_data_folder(process_lock: IO) -> None:
    fcntl.flock(process_lock.fileno(), fcntl.LOCK_UN)
    process_lock.close()


def open_store(root: Path, **options) -> sqlite3.Connection:
    """Open <data>/coconut.sqlite3 for the writer of the service, private to this account."""
    db = sqlite3.connect(root / 'coconut.sqlite3', check_same_thread=False, isolation_level=None, **options)
    os.chmod(root / 'coconut.sqlite3', 0o600)
    # This takes effect only in a new store. An older store changes mode in DatabaseMaintenance.
    db.execute('PRAGMA auto_vacuum=INCREMENTAL')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    return db
