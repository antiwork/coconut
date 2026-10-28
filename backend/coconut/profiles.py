"""Find the Hermes profile of each Coconut bot.

Each bot is a Hermes profile of the person. The `profiles` table (profile_registry) names its folder. Coconut never
creates a profile folder itself: bot_profiles uses the Hermes profile API. Coconut keeps its own files of a bot in
`<data>/bots/<uuid>` (bot_folder).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from uuid import UUID


try:
    from . import profile_registry
except ImportError:
    import profile_registry


class ProfileError(ValueError):
    pass


NO_PROFILE = 'This bot has no Hermes profile.'


MAX_PROMPT_FILE_BYTES = 1024 * 1024
# The skills that Coconut ships with its source, such as `coconut`. Hermes skips the folder when it is missing.
SKILLS = Path(__file__).resolve().parents[2] / 'skills'


def profile_id(bot: dict) -> str:
    value = bot['id']
    if str(UUID(value)) != value or bot.get('profile_id', value) != value:
        raise ProfileError('The Hermes profile identity does not match this bot.')
    return value


def _safe(path: Path, boundary: Path) -> None:
    if path != boundary and boundary not in path.parents:
        raise ProfileError('The Hermes profile must stay inside its data directory.')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ProfileError('The Hermes profile contains a symbolic link.')
        if part == boundary:
            break


def _write(path: Path, text: str, boundary: Path) -> None:
    _safe(path, boundary)
    if path.exists() and path.read_text() == text:
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.coconut-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _safe(path, boundary)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def read_regular(path: Path, name: str, limit: int = MAX_PROMPT_FILE_BYTES, kind: str = 'The Hermes profile prompt file') -> bytes:
    """Read one regular file without following a link. Raise ProfileError for any other kind of file."""
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ProfileError(f'{kind} must be a regular file: {name}')
    if before.st_size > limit:
        raise ProfileError(f'{kind} exceeds {limit // (1024 * 1024)} MiB: {name}')
    # A path can change after stat. Nonblocking open prevents a replacement FIFO from waiting.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as handle:
        current = os.fstat(handle.fileno())
        if not stat.S_ISREG(current.st_mode):
            raise ProfileError(f'{kind} must be a regular file: {name}')
        if current.st_size > limit:
            raise ProfileError(f'{kind} exceeds {limit // (1024 * 1024)} MiB: {name}')
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ProfileError(f'{kind} exceeds {limit // (1024 * 1024)} MiB: {name}')
    return content


def prompt_revision(home: str | Path, *, boundary: str | Path | None = None) -> str:
    home = Path(home).expanduser().absolute()
    boundary = Path(boundary).expanduser().absolute() if boundary is not None else home
    hashes = {}
    for name in ('SOUL.md', 'memories/MEMORY.md', 'memories/USER.md'):
        path = home / name
        _safe(path, boundary)
        try:
            content = read_regular(path, name)
        except FileNotFoundError:
            hashes[name] = None
            continue
        try:
            content.decode('utf-8')
        except UnicodeDecodeError:
            raise ProfileError('The Hermes profile prompt file must contain valid UTF-8: ' + name) from None
        hashes[name] = hashlib.sha256(content).hexdigest()
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def bot_folder(home: str | Path) -> Path:
    """Coconut's own folder of a bot, `<data>/bots/<uuid>`, for its profile home.

    Routine receipts, terminal scripts and worker progress go there, never into the profile. A worker knows its folder
    from COCONUT_BOT_DIR. A settings process finds it in the profiles table of COCONUT_DATA_DIR. A tool without either
    uses the parent of the home.
    """
    home = Path(home).absolute()
    folder, current = os.environ.get('COCONUT_BOT_DIR'), os.environ.get('HERMES_HOME')
    if folder and current and Path(current).absolute() == home:
        return Path(folder)
    data_dir = os.environ.get('COCONUT_DATA_DIR')
    if not data_dir:
        return home.parent
    bot_id = profile_registry.bot_for_home(data_dir, home)
    if bot_id is None:
        raise ProfileError('Coconut cannot find the bot of this Hermes profile.')
    return Path(data_dir) / 'bots' / bot_id


# The revision of a profile without prompt files.
EMPTY_REVISION = hashlib.sha256(json.dumps(dict.fromkeys(('SOUL.md', 'memories/MEMORY.md', 'memories/USER.md')),
                                           sort_keys=True).encode()).hexdigest()


class HermesProfiles:
    """Hermes files own behavior. Coconut records identity and phone presentation."""

    def __init__(self, data_dir: str | Path, rows: dict | None = None):
        self.root = Path(data_dir).expanduser().absolute()
        # The service passes its copy of the profiles table. Without it, each lookup reads the table.
        self.rows = rows

    def row(self, bot: dict) -> dict | None:
        identity = profile_id(bot)
        return self.rows.get(identity) if self.rows is not None else profile_registry.lookup(self.root, identity)

    def boundary(self, home: Path) -> Path:
        return self.root if self.root in home.parents else home

    def home(self, bot: dict) -> Path:
        row = self.row(bot)
        if row is None:
            raise ProfileError(NO_PROFILE)
        path = Path(row['home'])
        _safe(path, self.boundary(path))
        return path

    def existing_home(self, bot: dict) -> Path:
        """The profile folder, for a write. A missing folder stays missing: only Hermes creates profiles."""
        home = self.home(bot)
        if not home.is_dir():
            raise ProfileError(f'The Hermes profile folder {home} is missing. Restore it, or remove the bot from Coconut.')
        return home

    def read_instructions(self, bot: dict) -> str:
        if self.row(bot) is None:
            # A bot without a profile row shows its saved instructions. It runs no turns.
            return bot.get('instructions', '')
        home = self.home(bot)
        path = home / 'SOUL.md'
        _safe(path, self.boundary(home))
        return path.read_text() if path.exists() else bot.get('instructions', '')

    def prompt_revision(self, bot: dict) -> str:
        if self.row(bot) is None:
            return EMPTY_REVISION
        home = self.home(bot)
        return prompt_revision(home, boundary=self.boundary(home))

    def write_instructions(self, bot: dict, text: str) -> None:
        home = self.existing_home(bot)
        _write(home / 'SOUL.md', text, self.boundary(home))

    def sync(self, bot: dict) -> dict:
        """Check the profile of this bot. Coconut changes no profile here."""
        status = 'deleted' if bot.get('_deleted') else 'archived' if bot.get('archived') else 'active'
        if status != 'deleted' and self.row(bot) is None:
            # The service starts, and the bot runs no turns.
            status = 'no_profile'
        elif status != 'deleted' and not self.home(bot).is_dir():
            status = 'missing'
        return dict(profile_id=profile_id(bot), bot_id=bot['id'], session_id=bot['session_id'], status=status, created=False)


def merge_runtime_config(home: str | Path) -> None:
    """Load profile credentials without changing native settings."""
    from dotenv import dotenv_values
    home = Path(home).absolute()
    _safe(home / '.env', home)
    try:
        from .runtime_environment import CLAUDE_PROFILE_OVERRIDES, profile_environment
    except ImportError:
        from runtime_environment import CLAUDE_PROFILE_OVERRIDES, profile_environment
    protected = set(profile_environment(home)) | set(CLAUDE_PROFILE_OVERRIDES) | {'HERMES_HOME', 'PYTHONPATH'}
    for key in CLAUDE_PROFILE_OVERRIDES:
        os.environ.pop(key, None)
    for key, value in dotenv_values(home / '.env').items():
        if value is not None and key not in protected:
            os.environ[key] = value


def inspect_profiles(data_dir: str | Path, identity: str | None = None, *, include_deleted: bool = False) -> list[dict]:
    """Read profile identities without loading instructions, memory, or credentials."""
    profiles = HermesProfiles(data_dir)
    database = profiles.root / 'coconut.sqlite3'
    _safe(database, profiles.root)
    if not database.is_file():
        raise ProfileError('This data directory does not contain a Coconut database.')
    if identity is not None and str(UUID(identity)) != identity:
        raise ProfileError('Use the full profile ID from the profile list.')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
        bots = [json.loads(row[0]) for row in connection.execute('SELECT data FROM bots')]
    result = []
    for bot in bots:
        current = profile_id(bot)
        if (identity is not None and current != identity) or (bot.get('_deleted') and not include_deleted):
            continue
        try:
            home = profiles.home(bot)
        except ProfileError:
            continue
        state = 'deleted' if bot.get('_deleted') else 'archived' if bot.get('archived') else 'active'
        result.append(dict(profile_id=current, bot_id=bot['id'], session_id=bot['session_id'], name=bot['name'],
                           status=state, home=str(home), home_exists=home.is_dir()))
    if identity is not None and not result:
        raise ProfileError('This profile does not exist in the selected Coconut data directory.')
    return sorted(result, key=lambda item: (item['name'].casefold(), item['profile_id']))
