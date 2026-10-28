"""Point the paths in a profile's config.yaml from an old Hermes home to its new folder.

scripts/migrate_bots_to_profiles.py moves each bot home into `<Hermes home>/profiles/<name>`. Its config.yaml can name
files in the old home, for example `terminal.shell_init_files`. The edit reads and saves config.yaml with the config
reader and the round-trip writer of Hermes (native_settings), as skill_dirs does, so comments, key order and the other
keys stay. Every string value counts: in maps, in block and inline lists, at any depth. A value changes when it is the
old home or a path below it. Only that prefix changes. Keys do not change.

Run this file under the Python of the installed Hermes:

    python -I config_paths.py <Hermes source folder> <old home> <new home> [<old home> <new home>]...

The new home is the folder that has the config.yaml.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import sys


def moved(value: str, old: str, new: str) -> str | None:
    """The value with the old home replaced by the new home, or None when the value is not in the old home."""
    if value == old or value.startswith(old + '/'):
        return new + value[len(old):]
    return None


def with_paths(config, old: str, new: str):
    """Return a copy of the config with each string value in the old home moved to the new home, or None when no value
    changes."""
    result = deepcopy(config)
    changed = False
    pending = [result]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            slots = list(node.items())
        elif isinstance(node, list):
            slots = list(enumerate(node))
        else:
            continue
        for slot, value in slots:
            if isinstance(value, str):
                replaced = moved(value, old, new)
                if replaced is not None:
                    try:
                        # A ruamel scalar string keeps its quote style in its type.
                        node[slot] = type(value)(replaced)
                    except TypeError:
                        node[slot] = replaced
                    changed = True
            else:
                pending.append(value)
    return result if changed else None


def update(home: Path, old: str) -> str:
    """Change the config.yaml of one moved home. Return "changed", "unchanged" or "skipped"."""
    path = Path(home) / 'config.yaml'
    if not path.is_file() or path.is_symlink():
        return 'skipped'
    if __package__:
        from .native_settings import raw_config, save_config
    else:
        from native_settings import raw_config, save_config
    # Hermes checks some writes against the active home.
    os.environ['HERMES_HOME'] = str(home)
    changed = with_paths(raw_config(home), str(old), str(home))
    if changed is None:
        return 'unchanged'
    save_config(home, changed)
    return 'changed'


def main(arguments: list[str]) -> int:
    if len(arguments) < 3 or len(arguments) % 2 != 1:
        print(__doc__, file=sys.stderr)
        return 64
    source, pairs = arguments[0], arguments[1:]
    # -I leaves the folder of this file and PYTHONPATH out of sys.path. Hermes comes first, as its launcher puts it.
    sys.path[:0] = [path for path in (source, str(Path(__file__).resolve().parent)) if path]
    failed = False
    for old, new in zip(pairs[::2], pairs[1::2]):
        home = Path(new)
        try:
            result = update(home, os.path.normpath(old))
        except Exception as error:  # Hermes refuses a config.yaml that it cannot read. The other homes still change.
            failed = True
            print(f'{home / "config.yaml"}: not changed: {error}')
            continue
        if result != 'skipped':
            print(f'{home / "config.yaml"}: {result}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
