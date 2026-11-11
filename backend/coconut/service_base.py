"""What each service object of this package needs, with the standard library only.

Service (service.py), the refuse mode without Hermes (hermes_unavailable.py) and the team-only service (team_only.py)
share these helpers. The team modules and documents.py import them from here, so a team-only service never imports
service.py and the modules of the full runtime.
"""
from __future__ import annotations

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
