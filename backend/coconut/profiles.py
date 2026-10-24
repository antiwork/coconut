"""Keep one project-contained Hermes profile for each Coconut bot."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from uuid import UUID


class ProfileError(ValueError):
    pass


MAX_PROMPT_FILE_BYTES = 1024 * 1024


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


def settings_report(home: str | Path) -> Path:
    """The report of the one-time settings step (settings_migration) for this profile home. It marks the step as done."""
    return Path(home).parent / 'settings-migration' / 'report.json'


def settings_migrated(home: str | Path) -> bool:
    return settings_report(home).is_file()


class HermesProfiles:
    """Hermes files own behavior. Coconut records identity and phone presentation."""

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir).expanduser().absolute()

    def home(self, bot: dict) -> Path:
        path = self.root / 'bots' / profile_id(bot) / 'home'
        _safe(path, self.root)
        return path

    def read_instructions(self, bot: dict) -> str:
        path = self.home(bot) / 'SOUL.md'
        _safe(path, self.root)
        return path.read_text() if path.exists() else bot.get('instructions', '')

    def prompt_revision(self, bot: dict) -> str:
        return prompt_revision(self.home(bot), boundary=self.root)

    def write_instructions(self, bot: dict, text: str) -> None:
        home = self.home(bot)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write(home / 'SOUL.md', text, self.root)

    def sync(self, bot: dict) -> dict:
        """Check the profile identity of this bot. Create the profile only when the bot has none.

        Coconut does not change an existing profile here. It writes into a profile only when the person acts: when they
        edit SOUL, change the model through the Hermes config, or create a bot.
        """
        identity = profile_id(bot)
        home = self.home(bot)
        status = 'deleted' if bot.get('_deleted') else 'archived' if bot.get('archived') else 'active'
        created = False
        # Deleting an unused bot does not create a new Hermes home.
        if status != 'deleted' and not home.exists():
            self._create(bot, home)
            created = True
        return dict(profile_id=identity, bot_id=bot['id'], session_id=bot['session_id'], status=status, created=created)

    def _create(self, bot: dict, home: Path) -> None:
        """Write the files of a new profile in a staging folder, then rename it, so a crash leaves no half profile."""
        staging = home.with_name(home.name + '.creating')
        _safe(staging, self.root)
        if staging.exists():
            # Only an interrupted creation leaves this folder. It holds no state yet.
            shutil.rmtree(staging)
        staging.mkdir(parents=True, mode=0o700)
        # New profiles start with the essential Hermes skills only. `hermes skills opt-in` enables the bundled catalog.
        _write(staging / '.no-bundled-skills',
               'Use essential Hermes skills only. Run hermes skills opt-in to enable the bundled catalog.\n', self.root)
        _write(staging / 'SOUL.md', bot.get('instructions', ''), self.root)
        _write(staging / 'config.yaml', _json({'toolsets': ['kanban'], 'terminal': {'cwd': str(home.parent / 'workspace')},
                                               'delegation': {'max_concurrent_children': None, 'worktree_isolation': True}}),
               self.root)
        _write(staging / 'profile.yaml', _json(dict(display_name=bot.get('name', profile_id(bot)),
                                                    description=bot.get('role', ''))), self.root)
        _safe(home, self.root)
        os.rename(staging, home)


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
        home = profiles.home(bot)
        state = 'deleted' if bot.get('_deleted') else 'archived' if bot.get('archived') else 'active'
        result.append(dict(profile_id=current, bot_id=bot['id'], session_id=bot['session_id'], name=bot['name'],
                           status=state, home=str(home), home_exists=home.is_dir()))
    if identity is not None and not result:
        raise ProfileError('This profile does not exist in the selected Coconut data directory.')
    return sorted(result, key=lambda item: (item['name'].casefold(), item['profile_id']))
