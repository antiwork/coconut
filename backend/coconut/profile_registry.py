"""Map each Coconut bot to one Hermes profile of the person, by the profile name.

An agent can have more chats (bots with `chat_of`). Each chat has a row of its own with the name and the folder of the
agent's profile, so several rows can share a name. The row of the agent is the oldest one.

The `profiles` table in coconut.sqlite3 maps a bot UUID to a profile name and its folder. `default` is the Hermes home
itself ($HERMES_HOME or ~/.hermes). Each other profile is `<Hermes home>/profiles/<name>`. Coconut writes no marker
file into a profile and does not follow renames: a renamed folder is a missing bot and a new bot.

This module uses the standard library only, so the migration script, the workers and the service can import it.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

SCHEMA = ('CREATE TABLE IF NOT EXISTS profiles (bot_id TEXT PRIMARY KEY, name TEXT NOT NULL, home TEXT NOT NULL, '
          'state TEXT NOT NULL, seen_at TEXT NOT NULL)')
# active: the folder exists. missing: the folder is gone; the bot keeps its history and returns with the folder.
# removed: the person removed the bot from Coconut. The folder stays, and a scan does not add it again.
STATES = ('active', 'missing', 'removed')
DEFAULT = 'default'
# The rules of hermes_constants: a folder is a profile when it has one of these files and no tombstone.
PROFILE_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
IDENTITY_FILES = ('config.yaml', '.env', 'SOUL.md', 'profile.yaml', 'auth.json', 'state.db')
TOMBSTONES = '.deleted'
# Names that Hermes does not accept for a new profile: aliases and `hermes` subcommands.
RESERVED = frozenset({
    'hermes', 'default', 'test', 'tmp', 'root', 'sudo',
    'chat', 'model', 'gateway', 'setup', 'whatsapp', 'login', 'logout', 'status', 'cron', 'doctor', 'dump', 'config',
    'pairing', 'skills', 'tools', 'mcp', 'connectors', 'sessions', 'insights', 'version', 'update', 'uninstall',
    'profile', 'plugins', 'honcho', 'acp',
})


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_profile(folder: Path) -> bool:
    """True when Hermes lists this folder of `<Hermes home>/profiles` as a live profile."""
    try:
        return (not folder.is_symlink() and folder.is_dir() and bool(PROFILE_ID.match(folder.name))
                and folder.name != DEFAULT and not (folder.parent / TOMBSTONES / folder.name).exists()
                and any((folder / name).is_file() or (folder / name).is_symlink() for name in IDENTITY_FILES))
    except OSError:
        return False


def scan(hermes_home: str | Path) -> dict[str, Path]:
    """The profiles of a Hermes home as {name: folder}: `default`, then the named profiles."""
    hermes_home = Path(hermes_home).expanduser().absolute()
    if not hermes_home.is_dir():
        return {}
    found = {DEFAULT: hermes_home}
    try:
        entries = sorted((hermes_home / 'profiles').iterdir())
    except OSError:
        entries = []
    found.update((entry.name, entry) for entry in entries if is_profile(entry))
    return found


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


def bot_for_home(data_dir, home) -> str | None:
    found = _query(data_dir, "SELECT bot_id FROM profiles WHERE home=? AND state!='removed' ORDER BY rowid", (str(Path(home).absolute()),))
    return found[0][0] if found else None


def homes(data_dir) -> list[str]:
    return [row[0] for row in _query(data_dir, "SELECT DISTINCT home FROM profiles WHERE state!='removed'")]


def slug(name: str, taken) -> str:
    """A profile name from a bot name that Hermes accepts and that no profile uses yet."""
    base = re.sub(r'[^a-z0-9_-]+', '-', name.strip().lower()).strip('-_')[:56].strip('-_')
    if not PROFILE_ID.match(base) or base in RESERVED:
        base = base + '-bot' if base and PROFILE_ID.match(base + '-bot') else 'bot'
    taken = {value.casefold() for value in taken}
    candidate, number = base, 2
    while candidate.casefold() in taken or candidate in RESERVED:
        candidate, number = f'{base}-{number}', number + 1
    return candidate


def taken_names(hermes_home) -> set[str]:
    """Every name in `<Hermes home>/profiles`, live or deleted, so a new profile never reuses a folder or a tombstone."""
    folder = Path(hermes_home).expanduser() / 'profiles'
    names = set()
    for directory in (folder, folder / TOMBSTONES):
        try:
            names.update(entry.name for entry in directory.iterdir())
        except OSError:
            pass
    return names


def meta_value(home, key: str) -> str:
    """One plain value of profile.yaml (YAML from Hermes, or JSON from an earlier Coconut), without a YAML library."""
    try:
        text = (Path(home) / 'profile.yaml').read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return ''
    match = re.search(r'^\s*"?' + re.escape(key) + r'"?\s*:\s*(.+?)\s*,?\s*$', text, re.MULTILINE)
    value = match[1].strip() if match else ''
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1]
    return value.strip()
