"""Keep native tool profiles separate without hiding the account's Keychain."""
import os
from pathlib import Path
import pwd


def profile_environment(profile):
    profile = Path(profile).absolute()
    account_home = pwd.getpwuid(os.getuid()).pw_dir
    return dict(HOME=account_home, HERMES_REAL_HOME=account_home,
                CODEX_HOME=str(profile / '.codex'),
                CLAUDE_CONFIG_DIR=str(profile / '.claude'),
                GH_CONFIG_DIR=str(profile / '.config/gh'),
                XDG_CONFIG_HOME=str(profile / '.config'),
                XDG_CACHE_HOME=str(profile / '.cache'),
                XDG_DATA_HOME=str(profile / '.local/share'))


def prepare_profile(profile):
    try:
        from .profiles import _safe, _write
    except ImportError:
        from profiles import _safe, _write
    profile = Path(profile).absolute()
    environment = profile_environment(profile)
    for key, value in environment.items():
        if key in ('HOME', 'HERMES_REAL_HOME'):
            continue
        path = Path(value)
        _safe(path, profile)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    # CLAUDE_CONFIG_DIR moves the global config inside the existing Claude directory.
    previous, current = profile / '.claude.json', profile / '.claude/.claude.json'
    _safe(previous, profile)
    _safe(current, profile)
    if previous.is_file() and not current.exists():
        _write(current, previous.read_text(), profile)
    return environment
