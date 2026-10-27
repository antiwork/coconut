"""Add the folder of Coconut's skills to `skills.external_dirs` of Hermes profiles, or remove it.

Hermes scans each folder in `skills.external_dirs` of a profile's config.yaml for skills ("External Skill Directories"
in the Hermes docs). Coconut ships its `coconut` skill in `<install folder>/skills`. Setup adds that folder to the
Hermes profiles (scripts/hermes_skills.py), and the uninstaller removes it. The edit uses the config reader and the
round-trip writer of Hermes (native_settings), so comments, key order and the other keys stay.

Run this file under the Python of the installed Hermes:

    python -I skill_dirs.py add|remove <Hermes source folder> <skills folder> <Hermes home>...

Each Hermes home gets the change in its config.yaml. The first home is the default profile: its config.yaml is created
when it is missing. Each other home, and each named profile in `<home>/profiles`, changes only when it has a config.yaml.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import sys


def _same(entry, folder, home) -> bool:
    """Whether a config entry names the folder. Hermes expands ~ and ${VAR}, and reads a relative path from the home."""
    path = Path(os.path.expanduser(os.path.expandvars(str(entry).strip())))
    if not path.is_absolute():
        path = Path(home) / path
    return os.path.normpath(path) == os.path.normpath(folder)


def with_folder(config: dict, folder: str, present: bool, home='.') -> dict | None:
    """Return a copy of the config with the folder in `skills.external_dirs` (present) or without it.

    Return None when nothing changes, or when `skills` or `skills.external_dirs` has a form that Hermes does not read
    as a folder list. Hermes reads a single string as a list of one folder. A removal that empties the list removes the
    key, and then an empty `skills` section too.
    """
    skills = config.get('skills')
    if skills is None:
        skills = {}
    if not isinstance(skills, dict):
        return None
    folders = skills.get('external_dirs')
    if folders is None:
        folders = []
    elif isinstance(folders, str):
        folders = [folders]
    elif not isinstance(folders, list):
        return None
    kept = [entry for entry in folders if not _same(entry, folder, home)]
    if present:
        if len(kept) < len(folders):
            return None
        kept.append(str(folder))
    elif len(kept) == len(folders):
        return None
    result = deepcopy(config)
    skills = deepcopy(skills)
    if kept:
        skills['external_dirs'] = kept
    else:
        skills.pop('external_dirs', None)
    if skills:
        result['skills'] = skills
    else:
        result.pop('skills', None)
    return result


def homes(default: Path, others=()) -> list[tuple[Path, bool]]:
    """The homes to change, each with whether its config.yaml can be created: the default home, its named profiles,
    then the other homes."""
    result = [(Path(default), True)]
    profiles = Path(default) / 'profiles'
    if profiles.is_dir():
        result += [(path, False) for path in sorted(profiles.iterdir()) if path.is_dir()]
    result += [(Path(path), False) for path in others]
    return result


def update(home: Path, folder: str, present: bool, *, create: bool = False) -> str:
    """Change the config.yaml of one Hermes home. Return "added", "removed", "unchanged" or "skipped"."""
    path = Path(home) / 'config.yaml'
    if not path.is_file() and not (create and present and Path(home).is_dir()):
        return 'skipped'
    if __package__:
        from .native_settings import raw_config, save_config
    else:
        from native_settings import raw_config, save_config
    # Hermes checks some writes against the active home.
    os.environ['HERMES_HOME'] = str(home)
    changed = with_folder(raw_config(home), folder, present, home)
    if changed is None:
        return 'unchanged'
    save_config(home, changed)
    return 'added' if present else 'removed'


def main(arguments: list[str]) -> int:
    if len(arguments) < 4 or arguments[0] not in ('add', 'remove'):
        print(__doc__, file=sys.stderr)
        return 64
    action, source, folder, default, *others = arguments
    # -I leaves the folder of this file and PYTHONPATH out of sys.path. Hermes comes first, as its launcher puts it.
    sys.path[:0] = [path for path in (source, str(Path(__file__).resolve().parent)) if path]
    failed = False
    for home, create in homes(Path(default), [Path(path) for path in others]):
        try:
            result = update(home, os.path.normpath(folder), action == 'add', create=create)
        except Exception as error:  # Hermes refuses a config.yaml that it cannot read. The other homes still change.
            failed = True
            print(f'{home / "config.yaml"}: not changed: {error}')
            continue
        if result != 'skipped':
            print(f'{home / "config.yaml"}: {result}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
