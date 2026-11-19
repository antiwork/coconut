"""Find the Hermes profile of each Coconut bot.

A project and each of its chats run on a Hermes profile of the person. The `profiles` table (profile_registry) names
its folder. Coconut creates no profile folder. It keeps its own files of a bot in `<data>/bots/<uuid>`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from uuid import UUID

from . import profile_registry


class ProfileError(ValueError):
    pass


NO_PROFILE = 'This agent has no Hermes profile.'


MAX_PROMPT_FILE_BYTES = 1024 * 1024
# The skills that Coconut ships with its source, such as `coconut`. Hermes skips the folder when it is missing.
SKILLS = Path(__file__).resolve().parents[2] / 'skills'


def profile_id(bot: dict) -> str:
    value = bot['id']
    if str(UUID(value)) != value or bot.get('profile_id', value) != value:
        raise ProfileError('The Hermes profile identity does not match this agent.')
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
            raise ProfileError(f'The Hermes profile folder {home} is missing. Restore it, or remove the agent from Coconut.')
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

    def soul_revision(self, bot: dict) -> tuple[str, float] | None:
        """The hash of SOUL.md and the time of its last change, or None without SOUL.md."""
        if self.row(bot) is None:
            return None
        home = self.home(bot)
        path = home / 'SOUL.md'
        _safe(path, self.boundary(home))
        try:
            content = read_regular(path, 'SOUL.md')
        except FileNotFoundError:
            return None
        return hashlib.sha256(content).hexdigest(), path.stat().st_mtime

    def sync(self, bot: dict) -> dict:
        """Check the profile of this bot. Coconut changes no profile here."""
        status = 'deleted' if bot.get('_deleted') else 'archived' if bot.get('archived') else 'active'
        if status != 'deleted' and self.row(bot) is None:
            # The service starts, and the bot runs no turns.
            status = 'no_profile'
        elif status != 'deleted' and not self.home(bot).is_dir():
            status = 'missing'
        return dict(profile_id=profile_id(bot), bot_id=bot['id'], session_id=bot['session_id'], status=status, created=False)
