"""Keep one project-contained Hermes profile for each Coconut bot."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
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


def prompt_revision(home: str | Path, *, boundary: str | Path | None = None) -> str:
    home = Path(home).expanduser().absolute()
    boundary = Path(boundary).expanduser().absolute() if boundary is not None else home
    hashes = {}
    for name in ('SOUL.md', 'memories/MEMORY.md', 'memories/USER.md'):
        path = home / name
        _safe(path, boundary)
        try:
            before = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            hashes[name] = None
            continue
        if not stat.S_ISREG(before.st_mode):
            raise ProfileError('The Hermes profile prompt file must be a regular file: ' + name)
        if before.st_size > MAX_PROMPT_FILE_BYTES:
            raise ProfileError('The Hermes profile prompt file exceeds 1 MiB: ' + name)
        # A path can change after stat. Nonblocking open prevents a replacement FIFO from waiting.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as handle:
            current = os.fstat(handle.fileno())
            if not stat.S_ISREG(current.st_mode):
                raise ProfileError('The Hermes profile prompt file must be a regular file: ' + name)
            if current.st_size > MAX_PROMPT_FILE_BYTES:
                raise ProfileError('The Hermes profile prompt file exceeds 1 MiB: ' + name)
            content = handle.read(MAX_PROMPT_FILE_BYTES + 1)
        if len(content) > MAX_PROMPT_FILE_BYTES:
            raise ProfileError('The Hermes profile prompt file exceeds 1 MiB: ' + name)
        try:
            content.decode('utf-8')
        except UnicodeDecodeError:
            raise ProfileError('The Hermes profile prompt file must contain valid UTF-8: ' + name) from None
        hashes[name] = hashlib.sha256(content).hexdigest()
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


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
        identity = profile_id(bot)
        home = self.home(bot)
        metadata_path = home / 'coconut-profile.json'
        _safe(metadata_path, self.root)
        previous = None
        if metadata_path.exists():
            try:
                previous = json.loads(metadata_path.read_text())
            except (ValueError, OSError):
                raise ProfileError('The Hermes profile identity file is invalid.') from None
            if not isinstance(previous, dict) or any(previous.get(key) != value for key, value in (
                    ('profile_id', identity), ('bot_id', bot['id']), ('session_id', bot['session_id']))):
                raise ProfileError('The Hermes profile belongs to another bot or session.')
            if previous.get('status') == 'deleted' and not bot.get('_deleted'):
                raise ProfileError('This Hermes profile belongs to a deleted bot.')
        status = 'deleted' if bot.get('_deleted') else 'archived' if bot.get('archived') else 'active'
        metadata = dict(version=2, native_memory=True, skill_defaults_applied=True,
                        profile_id=identity, bot_id=bot['id'], session_id=bot['session_id'],
                        status=status, managed_files=['coconut-profile.json'])
        # Deleting an unused bot does not create a new Hermes home.
        if status == 'deleted' and not home.exists():
            return metadata
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        if status != 'deleted':
            # Apply the native default once. Later Hermes opt-in choices remain authoritative.
            if not previous or not previous.get('skill_defaults_applied'):
                _write(home / '.no-bundled-skills',
                       'Use essential Hermes skills only. Run hermes skills opt-in to enable the bundled catalog.\n',
                       self.root)
            soul = home / 'SOUL.md'
            _safe(soul, self.root)
            if previous is None and soul.exists():
                backup = home / 'SOUL.before-coconut.md'
                _safe(backup, self.root)
                if not backup.exists():
                    _write(backup, soul.read_text(), self.root)
            if not soul.exists():
                _write(soul, bot.get('instructions', ''), self.root)
            if not previous or not previous.get('native_memory'):
                # Each import preserves native entries and tolerates an interrupted migration.
                memories = home / 'memories'
                _safe(memories, self.root)
                memories.mkdir(exist_ok=True, mode=0o700)
                for filename, entries in (
                    ('MEMORY.md', [bot.get('memory', '')]),
                    ('USER.md', [item['text'] for item in bot.get('preferences', [])]),
                ):
                    target = memories / filename
                    _safe(target, self.root)
                    content = target.read_text() if target.exists() else ''
                    for entry in entries:
                        if entry.strip() and entry.strip() not in content:
                            content = content.rstrip() + '\n\n' + entry.strip() + '\n'
                    if content:
                        _write(target, content.lstrip(), self.root)
                legacy = home / 'COCONUT_MEMORY.md'
                _safe(legacy, self.root)
                legacy.unlink(missing_ok=True)
            config = home / 'config.yaml'
            _safe(config, self.root)
            if not config.exists():
                _write(config, _json({'toolsets': ['kanban'], 'terminal': {'cwd': str(home.parent / 'workspace')},
                                      'delegation': {'max_concurrent_children': None, 'worktree_isolation': True}}), self.root)
            meta = home / 'profile.yaml'
            _safe(meta, self.root)
            if not meta.exists():
                _write(meta, _json(dict(display_name=bot.get('name', identity), description=bot.get('role', ''))), self.root)
        _write(metadata_path, _json(metadata), self.root)
        return metadata


def merge_runtime_config(home: str | Path) -> None:
    """Load profile credentials without changing native settings."""
    from dotenv import dotenv_values
    home = Path(home).absolute()
    _safe(home / '.env', home)
    try:
        from .runtime_environment import CLAUDE_PROFILE_OVERRIDES, profile_environment
    except ImportError:
        from runtime_environment import CLAUDE_PROFILE_OVERRIDES, profile_environment
    protected = set(profile_environment(home)) | set(CLAUDE_PROFILE_OVERRIDES) | {'HERMES_HOME', 'PYTHONPATH', 'COCONUT_DEFAULT_HOME'}
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
        metadata_path = home / 'coconut-profile.json'
        _safe(metadata_path, profiles.root)
        metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
        state = 'deleted' if bot.get('_deleted') else 'archived' if bot.get('archived') else 'active'
        result.append(dict(profile_id=current, bot_id=bot['id'], session_id=bot['session_id'], name=bot['name'],
                           status=state, home=str(home), home_exists=home.is_dir(),
                           identity_recorded=all(metadata.get(key) == value for key, value in (
                               ('profile_id', current), ('bot_id', bot['id']), ('session_id', bot['session_id']), ('status', state)))))
    if identity is not None and not result:
        raise ProfileError('This profile does not exist in the selected Coconut data directory.')
    return sorted(result, key=lambda item: (item['name'].casefold(), item['profile_id']))
