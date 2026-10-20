"""Keep bot settings separate and use the Mac account's shared Claude login."""
import os
from pathlib import Path
import pwd


CLAUDE_PROFILE_OVERRIDES = ('CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR')


def profile_environment(profile):
    profile = Path(profile).absolute()
    account = pwd.getpwuid(os.getuid())
    # Claude Code uses USER to select the macOS Keychain account.
    return dict(HOME=account.pw_dir, HERMES_REAL_HOME=account.pw_dir, USER=account.pw_name,
                CODEX_HOME=str(profile / '.codex'),
                GH_CONFIG_DIR=str(profile / '.config/gh'),
                XDG_CONFIG_HOME=str(profile / '.config'),
                XDG_CACHE_HOME=str(profile / '.cache'),
                XDG_DATA_HOME=str(profile / '.local/share'))


def prepare_profile(profile):
    try:
        from .profiles import _safe
    except ImportError:
        from profiles import _safe
    profile = Path(profile).absolute()
    environment = profile_environment(profile)
    for key, value in environment.items():
        if key in ('HOME', 'HERMES_REAL_HOME', 'USER'):
            continue
        path = Path(value)
        _safe(path, profile)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    return environment
