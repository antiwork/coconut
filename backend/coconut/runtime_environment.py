"""Run bots under the Mac account, so the person's normal logins apply, and use the account's shared Claude login."""
import os
from pathlib import Path
import pwd
import re


# The login folders that a profile's .env can set. Without them, tools use the Mac account's folders.
LOGIN_FOLDERS = ('CODEX_HOME', 'GH_CONFIG_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME')


def profile_environment(profile):
    account = pwd.getpwuid(os.getuid())
    # Claude Code uses USER to select the macOS Keychain account. Codex, gh and XDG tools use their normal folders.
    # A profile that needs its own folders sets CODEX_HOME, GH_CONFIG_DIR or XDG_* in its .env file.
    return dict(HOME=account.pw_dir, HERMES_REAL_HOME=account.pw_dir, USER=account.pw_name)


def prepare_profile(profile):
    return profile_environment(Path(profile).absolute())


_REFERENCE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}')
_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', "'": "'", '\\': '\\'}


def _quoted(text, start, quote):
    """Read a quoted value that starts after `quote` at `start`. It can span lines. Return the value and the end."""
    value, index = [], start
    while index < len(text):
        char = text[index]
        if char == '\\' and index + 1 < len(text) and (quote == '"' or text[index + 1] in "\\'"):
            following = text[index + 1]
            value.append(_ESCAPES.get(following, '\\' + following) if quote == '"' else following)
            index += 2
            continue
        if char == quote:
            return ''.join(value), index + 1
        value.append(char)
        index += 1
    return None, len(text)


def env_key(text, start):
    """Read the key of a .env line at `start`, after an optional `export`. It can be quoted.

    Return the key and the index after it. Return (None, len(text)) for a quoted key without its end, and (None, start)
    for a line without a key.
    """
    if start < len(text) and text[start] in '"\'':
        return _quoted(text, start + 1, text[start])
    match = re.match(r'[^=#\s]+', text[start:])
    return (match.group(0), start + match.end()) if match else (None, start)


def _env_values(path):
    """Read a .env file with the rules of python-dotenv's dotenv_values, which the worker uses.

    The service uses python-dotenv when it runs in the Hermes environment. Without it, this parser follows the same
    rules for the values that the service reads: an optional `export`, quoted keys and values that can span lines,
    escapes in double quotes, a comment after an unquoted value, and ${NAME} or ${NAME:-default} references. A
    reference reads earlier lines first, then the process environment. Its default applies only to an unset name.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        pass
    else:
        return dotenv_values(path)
    try:
        text = Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return {}
    values = {}

    def expand(value):
        def replace(match):
            name, default = match.group(1), match.group(2)
            current = values[name] if name in values else os.environ.get(name)
            return current if current is not None else (default or '')
        return _REFERENCE.sub(replace, value)

    index = 0
    while index < len(text):
        end = text.find('\n', index)
        end = len(text) if end < 0 else end
        line = text[index:end].lstrip(' \t')
        start = index + (len(text[index:end]) - len(line))
        index = end + 1
        if not line.strip() or line.startswith('#'):
            continue
        if re.match(r'export\s', line):
            start += len(re.match(r'export\s+', line).group(0))
        key, start = env_key(text, start)
        if key is None:
            if start >= len(text):
                break
            continue
        while start < len(text) and text[start] in ' \t':
            start += 1
        if start >= len(text) or text[start] != '=':
            index = max(index, text.find('\n', start) + 1 if text.find('\n', start) >= 0 else len(text))
            continue
        start += 1
        while start < len(text) and text[start] in ' \t':
            start += 1
        if start < len(text) and text[start] in '"\'':
            value, start = _quoted(text, start + 1, text[start])
            if value is None:
                break
            values[key] = expand(value)
        else:
            stop = text.find('\n', start)
            stop = len(text) if stop < 0 else stop
            values[key] = expand(re.split(r'\s+#', text[start:stop], maxsplit=1)[0].strip())
            start = stop
        newline = text.find('\n', start)
        index = len(text) if newline < 0 else newline + 1
    return values


def login_folders(profile):
    """Read the login folders that the profile's .env sets, as the worker reads them."""
    values = _env_values(Path(profile) / '.env')
    return {key: value for key in LOGIN_FOLDERS if isinstance(value := values.get(key), str) and value.startswith('/')}
