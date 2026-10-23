"""Run bots under the Mac account, so the person's normal logins apply, and use the account's shared Claude login."""
import os
from pathlib import Path
import pwd


CLAUDE_PROFILE_OVERRIDES = ('CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR')
# The login folders that a profile's .env can set. Without them, tools use the Mac account's folders.
LOGIN_FOLDERS = ('CODEX_HOME', 'GH_CONFIG_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME')


def profile_environment(profile):
    account = pwd.getpwuid(os.getuid())
    # Claude Code uses USER to select the macOS Keychain account. Codex, gh and XDG tools use their normal folders.
    # A profile that needs its own folders sets CODEX_HOME, GH_CONFIG_DIR or XDG_* in its .env file.
    return dict(HOME=account.pw_dir, HERMES_REAL_HOME=account.pw_dir, USER=account.pw_name)


def prepare_profile(profile):
    return profile_environment(Path(profile).absolute())


def login_folders(profile):
    """Read the login folders that the profile's .env sets. The service reads them without Hermes's dotenv package."""
    values = {}
    try:
        lines = (Path(profile) / '.env').read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        return values
    for line in lines:
        line = line.strip()
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        key, separator, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not separator or key not in LOGIN_FOLDERS:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        if value.startswith('/'):
            values[key] = value
    return values
