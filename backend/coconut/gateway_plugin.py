"""Install Coconut's `coconut` gateway platform in a Hermes profile, or remove it.

Hermes loads a user plugin from `<home>/plugins/<name>` when `plugins.enabled` of the profile names it, and the gateway
runs a platform when `gateway.platforms.<name>.enabled` is true ("Adding a Platform Adapter" in the Hermes docs). Coconut
ships the plugin in `<install folder>/hermes_plugin/coconut`. The add step links `<home>/plugins/coconut` to that folder
and sets both keys. The remove step takes both away. A multiplexing gateway loads plugins and platforms per profile,
so each profile that is a bot gets the change.

skill_dirs.py runs this for each home at setup and at uninstall, and bot_profiles for a new profile. The config edit
uses the config reader and the round-trip writer of Hermes, as skill_dirs does. Each run is safe to repeat.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path

NAME = 'coconut'
PLUGIN = Path(__file__).resolve().parents[2] / 'hermes_plugin' / NAME
# Coconut's keys in `gateway.platforms.coconut`. Each run sets them again. Other keys of the block stay.
PLATFORM = {'enabled': True}


def with_platform(config: dict, present: bool) -> dict | None:
    """Return a copy of the config with the plugin and its platform enabled (present) or without them.

    Return None when nothing changes, or when `plugins`, `plugins.enabled`, `gateway` or `gateway.platforms` has a form
    that Hermes does not read. Coconut owns its keys in `gateway.platforms.coconut` (PLATFORM) and sets them again.
    Other keys of that block stay.
    """
    def value(mapping, key, empty):
        found = mapping.get(key)
        return empty if found is None else found
    plugins, gateway = value(config, 'plugins', {}), value(config, 'gateway', {})
    if not isinstance(plugins, dict) or not isinstance(gateway, dict):
        return None
    enabled, platforms = value(plugins, 'enabled', []), value(gateway, 'platforms', {})
    if not isinstance(enabled, list) or not isinstance(platforms, dict):
        return None
    result = deepcopy(config)
    if present and NAME not in enabled:
        result['plugins'] = dict(plugins, enabled=[*enabled, NAME])
    elif not present and NAME in enabled:
        # The list stays, also when it is empty: Hermes reads a missing list as "nothing chosen yet".
        result['plugins'] = dict(plugins, enabled=[entry for entry in enabled if entry != NAME])
    block = platforms.get(NAME)
    if present and (not isinstance(block, dict) or any(block.get(key) != value for key, value in PLATFORM.items())):
        block = dict(block, **PLATFORM) if isinstance(block, dict) else dict(PLATFORM)
        result['gateway'] = dict(gateway, platforms=dict(platforms, **{NAME: block}))
    elif not present and NAME in platforms:
        platforms = {key: value for key, value in platforms.items() if key != NAME}
        gateway = dict(gateway, platforms=platforms) if platforms else {k: v for k, v in gateway.items() if k != 'platforms'}
        if gateway:
            result['gateway'] = gateway
        else:
            result.pop('gateway')
    return None if result == config else result


def link(home: Path, folder: Path, present: bool) -> str:
    """Link `<home>/plugins/coconut` to the plugin folder, or remove that link. Return "added", "removed",
    "unchanged", or "taken" for another file of that name, which stays in both directions."""
    path = Path(home) / 'plugins' / NAME
    ours = path.is_symlink() and os.path.normpath(os.path.join(path.parent, os.readlink(path))) == os.path.normpath(folder)
    if present:
        if ours:
            return 'unchanged'
        if path.exists() or path.is_symlink():
            return 'taken'
        path.parent.mkdir(exist_ok=True, mode=0o700)
        path.symlink_to(folder, target_is_directory=True)
        return 'added'
    if not ours:
        return 'taken' if path.exists() or path.is_symlink() else 'unchanged'
    path.unlink()
    return 'removed'


def update(home: Path, present: bool, folder: Path = PLUGIN) -> str:
    """Change one Hermes home. Return "added", "removed", "unchanged", "skipped" without a config.yaml, or "taken"."""
    home = Path(home)
    if not (home / 'config.yaml').is_file():
        return 'skipped'
    if __package__:
        from .native_settings import raw_config, save_config
    else:
        from native_settings import raw_config, save_config
    # Hermes checks some writes against the active home.
    os.environ['HERMES_HOME'] = str(home)
    linked = link(home, Path(folder), present)
    if linked == 'taken':
        # Another plugin has the name. Its settings are not Coconut's to add or to remove.
        return 'taken'
    changed = with_platform(raw_config(home), present)
    if changed is not None:
        save_config(home, changed)
    if changed is None and linked == 'unchanged':
        return 'unchanged'
    return 'added' if present else 'removed'
