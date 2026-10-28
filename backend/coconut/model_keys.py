"""Model access the Hermes way: each Hermes profile keeps the provider keys that it uses in its own `.env`.

Hermes reads a profile's keys from `<home>/.env`. A multiplexing gateway serves each profile with that profile's own
secrets only, so a key in the default profile's `.env` does not reach a named profile. Hermes itself copies the
default `.env` into a named profile that has none (`hermes_cli.profiles.backfill_profile_envs`). Coconut applies the
same rule to each provider key: a named profile without a value for a provider key gets the default profile's value.
A key that a profile already has stays, also when it differs.

Earlier versions of Coconut kept model keys in `<data>/credentials.json` and gave them to the workers through the
service environment. The first run of this step moves those keys into the default profile's `.env` (only the keys that
it does not have yet), and then removes the file. `scripts/host.py save-model-access` and `remote.py model-access`
still write that file. The next start of the service moves the keys in the same way. Before it reads the file, this
step claims it with an atomic rename, so a file that arrives during an import waits for the next start.

skill_dirs.py runs this step under the Python of the person's Hermes, with the env writer of Hermes
(`hermes_cli.config.save_env_value`), at setup, at each update and at each start. It never prints a key value.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# The keys that Coconut saved in credentials.json. Hermes lists the other provider keys (provider_keys).
COCONUT_KEYS = ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY')


def provider_keys() -> set[str]:
    """The names of the provider API keys that Hermes knows, from its own list of settings."""
    from hermes_cli.config import OPTIONAL_ENV_VARS
    return {name for name, spec in OPTIONAL_ENV_VARS.items()
            if isinstance(spec, dict) and spec.get('category') == 'provider' and spec.get('password')} | set(COCONUT_KEYS)


def read_env(home: Path) -> dict[str, str]:
    from agent.secret_scope import load_env_file
    path = Path(home) / '.env'
    return {key: value for key, value in load_env_file(path).items() if value} if path.is_file() else {}


def save(home: Path, key: str, value: str) -> None:
    # Hermes writes the `.env` of the active home.
    os.environ['HERMES_HOME'] = str(home)
    from hermes_cli.config import save_env_value
    save_env_value(key, value)


def import_saved(default: Path, saved: Path) -> list[str]:
    """Move the keys of Coconut's credentials.json into the default profile's `.env`. Return the moved key names.

    The step claims the file with an atomic rename before it reads it, and it removes only the claimed file. A claimed
    file that an earlier run did not finish comes first.
    """
    claimed = saved.with_name(saved.name + '.importing')
    while True:
        if not claimed.is_file() or claimed.is_symlink():
            if not saved.is_file() or saved.is_symlink():
                return []
            os.replace(saved, claimed)
        try:
            values = json.loads(claimed.read_text())
            if not isinstance(values, dict):
                raise ValueError('not an object')
            break
        except ValueError:
            # Keep a bad file aside for the person, and go on with a file that arrived after it.
            os.replace(claimed, saved.with_name(saved.name + '.invalid'))
            if not saved.is_file():
                raise
    present = read_env(default)
    moved = []
    for key in COCONUT_KEYS:
        value = values.get(key)
        if isinstance(value, str) and value and key not in present:
            save(default, key, value)
            moved.append(key)
    claimed.unlink()
    return moved


def fill(default: Path, home: Path, names: set[str]) -> list[str]:
    """Give a named profile the default profile's provider keys that it lacks. Return the added key names."""
    if Path(home) == Path(default) or not Path(home).is_dir():
        return []
    source, present = read_env(default), read_env(home)
    added = []
    for key in sorted(names):
        if key in source and key not in present:
            save(home, key, source[key])
            added.append(key)
    return added
