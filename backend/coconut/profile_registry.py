"""Map each Coconut bot to the Hermes profile that runs it.

The `profiles` table in coconut.sqlite3 maps a bot UUID to a profile name and its folder. `default` is the Hermes home
itself ($HERMES_HOME or ~/.hermes). Each other profile is `<Hermes home>/profiles/<name>`. A project and each of its
chats (bots with `chat_of`) have a row each, with the name and the folder of the project's profile, so several rows
share a name. Coconut writes no marker file into a profile.

This module uses the standard library only, so the service and a script that runs by its path can import it.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

SCHEMA = ('CREATE TABLE IF NOT EXISTS profiles (bot_id TEXT PRIMARY KEY, name TEXT NOT NULL, home TEXT NOT NULL, '
          'state TEXT NOT NULL, seen_at TEXT NOT NULL)')
# active: the bot runs on the profile. missing: a row of an earlier release whose folder was gone.
# removed: the person removed the bot from Coconut. The folder stays.
STATES = ('active', 'missing', 'removed')
DEFAULT = 'default'


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_table(db: sqlite3.Connection) -> None:
    db.execute(SCHEMA)
    found = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='profiles'").fetchone()
    if found and 'UNIQUE' in found[0]:
        # An earlier table allowed one bot for each profile name. The chats of an agent share its name.
        db.execute('ALTER TABLE profiles RENAME TO profiles_unique_name')
        db.execute(SCHEMA)
        db.execute('INSERT INTO profiles SELECT bot_id,name,home,state,seen_at FROM profiles_unique_name ORDER BY rowid')
        db.execute('DROP TABLE profiles_unique_name')


def rows(db: sqlite3.Connection) -> dict[str, dict]:
    ensure_table(db)
    return {row[0]: dict(bot_id=row[0], name=row[1], home=row[2], state=row[3], seen_at=row[4])
            for row in db.execute('SELECT bot_id,name,home,state,seen_at FROM profiles ORDER BY rowid')}


def write(db: sqlite3.Connection, row: dict) -> None:
    if row['state'] not in STATES:
        raise ValueError('Unknown profile state.')
    db.execute('INSERT INTO profiles(bot_id,name,home,state,seen_at) VALUES (?,?,?,?,?) ON CONFLICT(bot_id) DO UPDATE '
               'SET name=excluded.name, home=excluded.home, state=excluded.state, seen_at=excluded.seen_at',
               (row['bot_id'], row['name'], str(row['home']), row['state'], row.get('seen_at') or now()))


def _read_only(data_dir):
    database = Path(data_dir) / 'coconut.sqlite3'
    return closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) if database.is_file() else None


def _query(data_dir, sql: str, values=()) -> list:
    """Read the table in another process, for example a worker. An absent table reads as empty."""
    connection = _read_only(data_dir)
    if connection is None:
        return []
    try:
        with connection as db:
            return db.execute(sql, values).fetchall()
    except sqlite3.OperationalError:
        return []


def lookup(data_dir, bot_id: str) -> dict | None:
    found = _query(data_dir, 'SELECT bot_id,name,home,state FROM profiles WHERE bot_id=?', (bot_id,))
    return dict(zip(('bot_id', 'name', 'home', 'state'), found[0])) if found else None


def homes(data_dir) -> list[str]:
    return [row[0] for row in _query(data_dir, "SELECT DISTINCT home FROM profiles WHERE state!='removed'")]
