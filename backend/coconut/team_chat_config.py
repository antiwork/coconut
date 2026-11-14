"""Connect the team chat of an agent of a team-only Mac to its Hermes home, or take it away (scripts/team_chat.py).

Run this file under the Python of the installed Hermes, in a process of its own. The team-only service imports no
Hermes, so it never runs this.

    python -I team_chat_config.py plan|connect|disconnect <Hermes source folder> <Hermes home> <bridge file>

`connect` makes at most three changes in the home, and `disconnect` takes the same three away:

1. The link `<home>/plugins/coconut` to the plugin folder of this Coconut.
2. `coconut` in `plugins.enabled` of `<home>/config.yaml`.
3. In `gateway.platforms.coconut` of that file: `enabled: true`, and in `extra`: `mode: team` and `bridge: <bridge
   file>`. A disconnect removes these keys, then each block that is empty after that.

`plan` and `plan-disconnect` change nothing. Each command prints one JSON object: `changes`, a line for a person for
each change that the command makes or made, and `problems`, or `error`.

A disconnect judges each of the three parts by itself (`without_team`). A part is done when Coconut removed it, or
when the home proves that it has nothing of Coconut there. A part whose form cannot be read is not proved: the
disconnect removes what it can, and `problems` has one line for each part that the operator must correct. The caller
then keeps the guard of the agent, and a second run after the correction finishes the job.

The edit uses the config reader and the round-trip writer of Hermes (`save`), so comments, key order, the layout and
the other keys stay. A plan reads the file with the safe YAML loader of Hermes's dependencies instead: an import of the
config module of Hermes makes the folders of a Hermes home and a copy of config.yaml in `backups/config`, as each start
of Hermes does, and a plan must change nothing.

A home whose `plugins/coconut` is another file, or whose `gateway.platforms.coconut` belongs to a full Coconut (a block
with `enabled` and without `mode: team`), gets an error and no change: those are not Coconut's to change here. A block
with only keys of the operator, for example what a disconnect left, is free to take.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys

NAME = 'coconut'
PLUGIN = Path(__file__).resolve().parents[2] / 'hermes_plugin' / NAME
TEAM = 'team'


class Refused(Exception):
    """The home has a form that this command does not change."""


def with_team(config: dict, config_file: Path, bridge: str) -> tuple[dict | None, list[str]]:
    """The config with the team chat, and a line for each change. None when nothing changes.
    Each form that is not Coconut's to change refuses before any write."""
    def value(mapping, key, empty):
        found = mapping.get(key)
        return empty if found is None else found
    plugins, gateway = value(config, 'plugins', {}), value(config, 'gateway', {})
    if not isinstance(plugins, dict) or not isinstance(gateway, dict):
        raise Refused(f'{config_file} has a form of plugins or gateway that Hermes does not read. Coconut did not '
                      'change it.')
    enabled, platforms = value(plugins, 'enabled', []), value(gateway, 'platforms', {})
    if not isinstance(enabled, list) or not isinstance(platforms, dict):
        raise Refused(f'{config_file} has a form of plugins.enabled or gateway.platforms that Hermes does not read. '
                      'Coconut did not change it.')
    block = platforms.get(NAME)
    extra = block.get('extra') if isinstance(block, dict) else None
    if (block is not None and not isinstance(block, dict)) or (extra is not None and not isinstance(extra, dict)):
        raise Refused(f'{config_file} has a form of gateway.platforms.{NAME} that Hermes does not read. Coconut did '
                      'not change it.')
    ours = extra is not None and extra.get('mode') == TEAM
    # A full Coconut writes `enabled` and no mode (gateway_plugin.PLATFORM). A block without both has only keys of
    # the operator, for example what a disconnect left: a connect takes it, and a disconnect leaves it.
    if block is not None and not ours and 'enabled' in block:
        raise Refused(f'gateway.platforms.{NAME} in {config_file} belongs to a full Coconut, not to the team chat. '
                      'Coconut did not change it.')
    result, changes = deepcopy(config), []
    if NAME not in enabled:
        result['plugins'] = dict(plugins, enabled=[*enabled, NAME])
        changes.append(f'Add {NAME} to plugins.enabled in {config_file}.')
    wanted = dict(block or {}, enabled=True, extra=dict(extra or {}, mode=TEAM, bridge=bridge))
    if wanted != block:
        result['gateway'] = dict(gateway, platforms=dict(platforms, **{NAME: wanted}))
        changes.append(f'Set gateway.platforms.{NAME} in {config_file}: enabled true, extra.mode {TEAM}, '
                       f'extra.bridge {bridge}.')
    return (None if result == config else result), changes


def without_team(config, config_file: Path, bridge: str) -> tuple[dict | None, list[str], list[str]]:
    """The config without the team chat, the line of each change, and the line of each part that is not proved.

    Each part counts by itself. `plugins.enabled` is done when the key is missing, or when it is a list without
    `coconut` after the removal. `gateway.platforms.coconut` is done when the block is missing, or when it is a mapping
    without the keys of the team chat after the removal: `mode: team`, the `bridge` of this Coconut, and the `enabled`
    that goes with them. A block of a full Coconut (`enabled` without `mode: team`) has none of them. A part whose form
    cannot be read is not proved, and the config keeps it as it is.
    """
    if config is None:
        config = {}
    if not isinstance(config, dict):
        return None, [], [f'{config_file} is not a mapping. Correct it.']
    result, changes, problems = deepcopy(config), [], []
    plugins = config.get('plugins')
    enabled = plugins.get('enabled') if isinstance(plugins, dict) else None
    if plugins is not None and not isinstance(plugins, dict):
        problems.append(f'plugins in {config_file} is not a mapping. Correct it, or remove coconut from it.')
    elif enabled is not None and not isinstance(enabled, list):
        problems.append(f'plugins.enabled in {config_file} is not a list. Correct it, or remove coconut from it.')
    elif enabled is not None and NAME in enabled:
        # The list stays, also when it is empty: Hermes reads a missing list as "nothing chosen yet".
        result['plugins'] = dict(plugins, enabled=[entry for entry in enabled if entry != NAME])
        changes.append(f'Remove {NAME} from plugins.enabled in {config_file}.')
    gateway = config.get('gateway')
    platforms = gateway.get('platforms') if isinstance(gateway, dict) else None
    block = platforms.get(NAME) if isinstance(platforms, dict) else None
    extra = block.get('extra') if isinstance(block, dict) else None
    if gateway is not None and not isinstance(gateway, dict):
        problems.append(f'gateway in {config_file} is not a mapping. Correct it.')
    elif platforms is not None and not isinstance(platforms, dict):
        problems.append(f'gateway.platforms in {config_file} is not a mapping. Correct it.')
    elif block is not None and not isinstance(block, dict):
        problems.append(f'gateway.platforms.{NAME} in {config_file} is not a mapping. Correct it, or remove it.')
    elif extra is not None and not isinstance(extra, dict):
        problems.append(f'gateway.platforms.{NAME}.extra in {config_file} is not a mapping. Correct it, or remove it.')
    elif extra is not None and (extra.get('mode') == TEAM or extra.get('bridge') == bridge):
        rest = {key: item for key, item in block.items() if key not in ('enabled', 'extra')}
        left = {key: item for key, item in extra.items() if key not in ('mode', 'bridge')}
        if left:
            rest['extra'] = left
        platforms = {key: item for key, item in platforms.items() if key != NAME}
        if rest:
            platforms[NAME] = rest
        gateway = dict(gateway, platforms=platforms) if platforms else {k: v for k, v in gateway.items()
                                                                         if k != 'platforms'}
        if gateway:
            result['gateway'] = gateway
        else:
            result.pop('gateway', None)
        changes.append(f'Remove gateway.platforms.{NAME} from {config_file}.' if not rest else
                       f'Remove enabled, extra.mode and extra.bridge from gateway.platforms.{NAME} in {config_file}.')
    return (None if result == config else result), changes, problems


def link_state(home: Path, folder: Path) -> str:
    """"ours" for a link to the plugin folder, "taken" for another file of that name, else "missing"."""
    path = home / 'plugins' / NAME
    if path.is_symlink() and os.path.normpath(os.path.join(path.parent, os.readlink(path))) == os.path.normpath(folder):
        return 'ours'
    return 'taken' if path.exists() or path.is_symlink() else 'missing'


def read_only(home: Path) -> dict:
    """The config of a home for a plan, read without a module of Hermes."""
    from ruamel.yaml import YAML
    with open(home / 'config.yaml', encoding='utf-8-sig') as handle:
        config = YAML(typ='safe').load(handle)
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise Refused(f'{home / "config.yaml"} is not a mapping. Coconut changed nothing.')
    return config


def save(home: Path, config: dict) -> None:
    """Save the config with the round-trip writer of Hermes, which keeps comments, key order and the layout of each
    node that does not change. Coconut's save_config (model_catalog.save_yaml) first rewrites each flow list and
    mapping in block style, for the configs that Coconut made as JSON: a production config keeps its layout here."""
    from utils import atomic_roundtrip_yaml_save
    atomic_roundtrip_yaml_save(home / 'config.yaml', config)


def run(action: str, home: Path, bridge: str, folder: Path = PLUGIN) -> tuple[list[str], list[str]]:
    """Plan or make the change. Return the lines of the changes and of the parts that a disconnect did not prove."""
    present = action in ('plan', 'connect')
    write = action in ('connect', 'disconnect')
    config_file = home / 'config.yaml'
    linked = link_state(home, folder)
    if present and not config_file.is_file():
        raise Refused(f'{home} has no config.yaml, so it is not the home of a Hermes profile. Coconut changed nothing.')
    if present and linked == 'taken':
        raise Refused(f'{home / "plugins" / NAME} is another plugin, not Coconut. Coconut changed nothing.')
    # Hermes checks some writes against the active home.
    os.environ['HERMES_HOME'] = str(home)
    if write:
        from native_settings import raw_config
    changed, changes, problems = None, [], []
    if present and config_file.is_file():
        changed, changes = with_team(raw_config(home) if write else read_only(home), config_file, bridge)
    elif not present and (config_file.exists() or config_file.is_symlink()):
        try:
            config = raw_config(home) if write else read_only(home)
        except Exception as error:  # Hermes refuses a config.yaml that it cannot read or parse.
            problems.append(f'{config_file} does not read ({type(error).__name__}). Correct it.')
        else:
            changed, changes, problems = without_team(config, config_file, bridge)
    link = home / 'plugins' / NAME
    if present and linked == 'missing':
        changes.insert(0, f'Link {link} to {folder}.')
    elif not present and linked == 'ours':
        changes.insert(0, f'Remove the link {link}.')
    if write:
        from gateway_plugin import link as set_link
        if present:
            set_link(home, folder, True)
        if changed is not None:
            save(home, changed)
        if not present:
            set_link(home, folder, False)
    return changes, problems


def main(arguments: list[str]) -> int:
    if len(arguments) != 4 or arguments[0] not in ('plan', 'plan-disconnect', 'connect', 'disconnect'):
        print(__doc__, file=sys.stderr)
        return 64
    action, source, home, bridge = arguments
    # -I leaves the folder of this file and PYTHONPATH out of sys.path. Hermes comes first, as its launcher puts it.
    sys.path[:0] = [path for path in (source, str(Path(__file__).resolve().parent)) if path]
    try:
        changes, problems = run(action, Path(home), bridge)
    except Refused as error:
        print(json.dumps({'error': str(error)}))
        return 1
    except Exception as error:  # Hermes refuses a config.yaml that it cannot read.
        print(json.dumps({'error': f'Coconut could not read or write the config of {home}: {error}'}))
        return 1
    print(json.dumps({'changes': changes, 'problems': problems}))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
