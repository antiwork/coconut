"""Map each Coconut bot to one Hermes profile of the person.

A bot keeps its UUID. The `profiles` table in coconut.sqlite3 maps that UUID to a Hermes profile: `default` is the
Hermes home ($HERMES_HOME or ~/.hermes), and each other profile is `<Hermes home>/profiles/<name>`. The table also
keeps the device and inode of the folder, so a renamed folder keeps its bot.

Coconut writes no marker file into a profile. This module uses the standard library only, so the migration script,
the workers and the service can all import it.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

SCHEMA = ('CREATE TABLE IF NOT EXISTS profiles (bot_id TEXT PRIMARY KEY, name TEXT NOT NULL, home TEXT NOT NULL, '
          'dev INTEGER, ino INTEGER, state TEXT NOT NULL, seen_at TEXT NOT NULL)')
# active: the folder exists. missing: the folder is gone, and the bot reattaches when it returns.
# removed: the person removed the bot from Coconut. The folder stays, and a rescan does not add it again.
STATES = ('active', 'missing', 'removed')
DEFAULT = 'default'
# The same rules as hermes_constants: a folder is a profile only when it has one of these files.
PROFILE_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
IDENTITY_FILES = ('config.yaml', '.env', 'SOUL.md', 'profile.yaml', 'auth.json', 'state.db')
TOMBSTONES = '.deleted'
# Names that Hermes does not accept for a new profile, because they are aliases or `hermes` subcommands.
RESERVED = frozenset({
    'hermes', 'default', 'test', 'tmp', 'root', 'sudo',
    'chat', 'model', 'gateway', 'setup', 'whatsapp', 'login', 'logout', 'status', 'cron', 'doctor', 'dump', 'config',
    'pairing', 'skills', 'tools', 'mcp', 'connectors', 'sessions', 'insights', 'version', 'update', 'uninstall',
    'profile', 'plugins', 'honcho', 'acp',
})


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Found:
    name: str
    home: Path
    dev: int
    ino: int


def is_profile(folder: Path) -> bool:
    """True when Hermes lists this folder of `<Hermes home>/profiles` as a live profile."""
    try:
        if folder.is_symlink() or not folder.is_dir() or not PROFILE_ID.match(folder.name) or folder.name == DEFAULT:
            return False
        if (folder.parent / TOMBSTONES / folder.name).exists():
            return False
        return any((folder / name).is_file() or (folder / name).is_symlink() for name in IDENTITY_FILES)
    except OSError:
        return False


def scan(hermes_home: str | Path) -> list[Found]:
    """List the profiles of a Hermes home: `default` first, then the named profiles in name order."""
    hermes_home = Path(hermes_home).expanduser().absolute()
    found = []
    try:
        state = hermes_home.stat()
    except OSError:
        return found
    found.append(Found(DEFAULT, hermes_home, state.st_dev, state.st_ino))
    folder = hermes_home / 'profiles'
    try:
        entries = sorted(folder.iterdir()) if folder.is_dir() and not folder.is_symlink() else []
    except OSError:
        entries = []
    for entry in entries:
        if is_profile(entry):
            try:
                state = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            found.append(Found(entry.name, entry, state.st_dev, state.st_ino))
    return found


def signature(hermes_home: str | Path) -> tuple:
    """A cheap value that changes when a profile folder appears, disappears, changes its name, or becomes a profile.

    A folder becomes a profile when it gets a profile file. That does not change the profiles folder itself, so each
    entry also carries whether it is a profile now.
    """
    hermes_home = Path(hermes_home).expanduser().absolute()
    folder = hermes_home / 'profiles'
    try:
        state = folder.stat()
        entries = tuple(sorted((entry.name, is_profile(entry)) for entry in folder.iterdir()))
        tombstones = folder / TOMBSTONES
        deleted = tuple(sorted(entry.name for entry in tombstones.iterdir())) if tombstones.is_dir() else ()
        return (hermes_home.exists(), state.st_ino, state.st_mtime_ns, entries, deleted)
    except OSError:
        return (hermes_home.exists(),)


def ensure_table(db: sqlite3.Connection) -> None:
    db.execute(SCHEMA)
    db.execute('CREATE INDEX IF NOT EXISTS profiles_home ON profiles (home)')


def rows(db: sqlite3.Connection) -> dict[str, dict]:
    ensure_table(db)
    return {row[0]: dict(bot_id=row[0], name=row[1], home=row[2], dev=row[3], ino=row[4], state=row[5], seen_at=row[6])
            for row in db.execute('SELECT bot_id,name,home,dev,ino,state,seen_at FROM profiles')}


def write(db: sqlite3.Connection, row: dict) -> None:
    if row['state'] not in STATES:
        raise ValueError('Unknown profile state.')
    db.execute('INSERT INTO profiles(bot_id,name,home,dev,ino,state,seen_at) VALUES (?,?,?,?,?,?,?) '
               'ON CONFLICT(bot_id) DO UPDATE SET name=excluded.name, home=excluded.home, dev=excluded.dev, '
               'ino=excluded.ino, state=excluded.state, seen_at=excluded.seen_at',
               (row['bot_id'], row['name'], str(row['home']), row.get('dev'), row.get('ino'), row['state'],
                row.get('seen_at') or now()))


def plan(known: dict[str, dict], found: list[Found]) -> list[tuple[str, dict]]:
    """Compare the table with the folders on disk. Return a list of (change, row).

    change is one of:
    - `new`: a folder that no row knows. The service creates a bot for it.
    - `moved`: a row whose folder has a new name or path, or a missing row whose folder returned.
    - `missing`: an active row whose folder is gone.
    A row first matches by device and inode, so a renamed folder keeps its bot. A row without that match then matches by
    name, so a folder that returns from a backup (with a new inode) reattaches. A removed row stays removed.
    """
    changes, matched = [], set()
    unmatched = list(found)
    for item in list(unmatched):
        row = next((r for r in known.values() if r['bot_id'] not in matched and r['dev'] == item.dev
                    and r['ino'] == item.ino and (r['name'] == DEFAULT) == (item.name == DEFAULT)), None)
        if row is not None:
            matched.add(row['bot_id'])
            unmatched.remove(item)
            changes.append(_moved(row, item))
    for item in list(unmatched):
        # A removed row matches only its own folder (by inode). A new folder with its name becomes a new bot.
        row = next((r for r in known.values() if r['bot_id'] not in matched and r['name'] == item.name
                    and r['state'] != 'removed'
                    and (r['state'] != 'active' or not Path(r['home']).exists() or Path(r['home']) == item.home)), None)
        if row is not None:
            matched.add(row['bot_id'])
            unmatched.remove(item)
            changes.append(_moved(row, item))
    changes = [change for change in changes if change is not None]
    for item in unmatched:
        changes.append(('new', dict(name=item.name, home=str(item.home), dev=item.dev, ino=item.ino, state='active')))
    for row in known.values():
        if row['bot_id'] not in matched and row['state'] == 'active':
            changes.append(('missing', dict(row, state='missing')))
    return changes


def _moved(row: dict, item: Found):
    state = 'removed' if row['state'] == 'removed' else 'active'
    updated = dict(row, name=item.name, home=str(item.home), dev=item.dev, ino=item.ino, state=state)
    if all(updated[key] == row[key] for key in ('name', 'home', 'dev', 'ino', 'state')):
        return None
    return ('moved', updated)


def _read_only(data_dir: str | Path):
    database = Path(data_dir) / 'coconut.sqlite3'
    if not database.is_file():
        return None
    return closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5))


def lookup(data_dir: str | Path, bot_id: str) -> dict | None:
    """Read one row in another process, for example a worker. Return None for a bot without a profile row."""
    connection = _read_only(data_dir)
    if connection is None:
        return None
    try:
        with connection as db:
            row = db.execute('SELECT bot_id,name,home,dev,ino,state FROM profiles WHERE bot_id=?', (bot_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else dict(zip(('bot_id', 'name', 'home', 'dev', 'ino', 'state'), row))


def bot_for_home(data_dir: str | Path, home: str | Path) -> str | None:
    """Return the bot of a profile folder. An active row wins over a removed one."""
    connection = _read_only(data_dir)
    if connection is None:
        return None
    try:
        with connection as db:
            found = db.execute("SELECT bot_id FROM profiles WHERE home=? ORDER BY state='active' DESC LIMIT 1",
                               (str(Path(home).absolute()),)).fetchone()
    except sqlite3.OperationalError:
        return None
    return found[0] if found else None


def homes(data_dir: str | Path) -> list[str]:
    """The folders of every profile that a bot uses, read in another process."""
    connection = _read_only(data_dir)
    if connection is None:
        return []
    try:
        with connection as db:
            return [row[0] for row in db.execute("SELECT home FROM profiles WHERE state!='removed'")]
    except sqlite3.OperationalError:
        return []


def has_profiles(data_dir: str | Path) -> bool:
    connection = _read_only(data_dir)
    if connection is None:
        return False
    try:
        with connection as db:
            return db.execute('SELECT 1 FROM profiles LIMIT 1').fetchone() is not None
    except sqlite3.OperationalError:
        return False


def slug(name: str, taken) -> str:
    """Choose a profile name from a bot name that Hermes accepts and that no profile uses yet."""
    base = re.sub(r'[^a-z0-9_-]+', '-', name.strip().lower()).strip('-_')[:56].strip('-_')
    if not PROFILE_ID.match(base) or base in RESERVED:
        base = (base + '-bot').strip('-_') if PROFILE_ID.match(base + '-bot') and base else 'bot'
    taken = {value.casefold() for value in taken}
    candidate, number = base, 2
    while candidate.casefold() in taken or candidate in RESERVED:
        candidate = f'{base}-{number}'
        number += 1
    return candidate


def taken_names(hermes_home: str | Path) -> set[str]:
    """Every name under `<Hermes home>/profiles`, live or not, so a new profile never reuses a folder or a tombstone."""
    folder = Path(hermes_home).expanduser() / 'profiles'
    names = set()
    for directory in (folder, folder / TOMBSTONES):
        try:
            names.update(entry.name for entry in directory.iterdir())
        except OSError:
            pass
    return names


def meta_value(home: str | Path, key: str) -> str:
    """Read one plain text value of profile.yaml without a YAML library. Return '' when it has none.

    profile.yaml is YAML from Hermes, or JSON from an earlier Coconut. Both write a simple value on one line.
    """
    try:
        text = (Path(home) / 'profile.yaml').read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return ''
    match = re.search(r'^\s*"?' + re.escape(key) + r'"?\s*:\s*(.+?)\s*,?\s*$', text, re.MULTILINE)
    if not match:
        return ''
    value = match[1].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1]
    return value.strip()


def display_name(home: str | Path) -> str:
    return meta_value(home, 'display_name')[:80]
