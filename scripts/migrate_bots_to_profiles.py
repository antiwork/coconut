#!/usr/bin/env python3
"""Move each Coconut bot home into a Hermes profile of the person. Run it once, by hand.

Before: each bot has a Hermes home in <data>/bots/<uuid>/home (the data folder is ~/Coconut/.runtime/host).
After: each bot is the profile <Hermes home>/profiles/<name>. Coconut chats, `hermes -p <name>` and the gateway share its
skills, memories, config and cron jobs. Bot IDs do not change, so the iPhone keeps its chats.

  python3 scripts/migrate_bots_to_profiles.py --dry-run    Show the plan. Change nothing.
  python3 scripts/migrate_bots_to_profiles.py              Migrate.

The migration is one way. It keeps a backup and a ledger (profile-migration.json) for recovery by hand.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import ctypes
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import host  # noqa: E402

registry = host.backend_module('profile_registry')
LEDGER = 'profile-migration.json'
APP_ID = 'com.gumroad.coconut.mac'
# The backup leaves out the folders that the migration does not change. They can be large.
BACKUP_SKIP_ROOT = {'workspaces', 'runtimes', 'artifacts'}
BACKUP_SKIP_BOT = {'workspace', 'browser', 'tmp'}
# A bot home can hold a Hermes dependency environment (installs/, about 355 MB) and tools. Hermes makes them again, so
# the backup leaves them out. __pycache__ and node_modules are left out at any depth of a bot home.
BACKUP_SKIP_HOME = {'installs', 'tools', 'cache'}
BACKUP_SKIP_ANYWHERE = {'__pycache__', 'node_modules'}
# The backup keeps this much space free on the disk, for the service and the moves.
BACKUP_MARGIN = 512 * 1024 * 1024
CLONE_NOFOLLOW = 0x0001


def say(text: str = '') -> None:
    print(text, flush=True)


def plan(data_dir: Path, hermes_home: Path) -> list[dict]:
    """One move for each bot that is not deleted and still has <data>/bots/<uuid>/home, with a free profile name."""
    database = data_dir / 'coconut.sqlite3'
    if not database.is_file():
        raise SystemExit(f'{database} does not exist. Check --data-dir.')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        bots = [json.loads(row[0]) for row in db.execute('SELECT data FROM bots')]
        try:
            known = {row[0] for row in db.execute('SELECT bot_id FROM profiles')}
            taken = {row[0] for row in db.execute('SELECT name FROM profiles')}
        except sqlite3.OperationalError:
            known, taken = set(), set()
    taken |= registry.taken_names(hermes_home)
    moves = []
    for bot in sorted(bots, key=lambda bot: (bot.get('created_at', ''), bot['id'])):
        source = data_dir / 'bots' / bot['id'] / 'home'
        if bot.get('_deleted') or bot['id'] in known or source.is_symlink() or not source.is_dir():
            continue
        name = registry.slug(bot.get('name') or 'bot', taken)
        taken.add(name)
        moves.append(dict(bot_id=bot['id'], bot_name=bot.get('name', ''), name=name, source=str(source),
                          destination=str(hermes_home / 'profiles' / name), instructions=bot.get('instructions', ''),
                          state='planned'))
    return moves


def show(moves: list[dict], data_dir: Path, hermes_home: Path) -> None:
    say(f'Coconut data: {data_dir}')
    say(f'Hermes home:  {hermes_home}')
    if not moves:
        say('No bot has an earlier home. Nothing to migrate.')
    for move in moves:
        say(f'  {move["bot_name"]!r} ({move["bot_id"]}): {move["source"]} -> {move["destination"]}')


def home_problem(data_dir: Path, hermes_home: Path) -> str | None:
    """Why the service would not find the bots in hermes_home, or None.

    The service scans the profiles of its own Hermes home. An earlier install can have HERMES_HOME set to a folder of
    Coconut (.runtime/hermes/terminal/hermes), and then it would not see the moved bots.
    """
    target = hermes_home.expanduser().resolve()
    inside = host.ROOT.resolve()
    if target == inside or inside in target.parents:
        return f'{hermes_home} is a folder of Coconut. Give your Hermes home, for example --hermes-home ~/.hermes.'
    status = host.hermes_runtime().read_status(data_dir) or {}
    service = Path(status['home']).expanduser().resolve() if isinstance(status.get('home'), str) else None
    if service is None or service == target:
        return None
    return (f'The Coconut service uses HERMES_HOME={service}, but the bots would move into {target}. First run:\n'
            f'  defaults write {APP_ID} ServiceEnvironment -dict-add HERMES_HOME "{target}"\n'
            '  launchctl kickstart -k "gui/$(id -u)/com.gumroad.coconut.service"\n'
            f'Then check that {data_dir / "hermes.json"} names {target} as "home", and run this command again.')


def stop_service(manage: bool) -> dict | None:
    """Let the running turns finish, then stop the service. Return the state that starts it again."""
    if not manage:
        return None
    say('Asking Coconut to finish its turns, then stopping it...')
    host.drain_for_update()
    return host.stop_for_update()


def coconut_workers(data_dir: Path) -> list[int]:
    """Coconut workers of this data folder. `ps e` shows the environment of this account's processes."""
    listing = subprocess.run(['ps', '-Aeww', '-o', 'pid=,command='], capture_output=True, text=True, timeout=30).stdout
    markers = (f'COCONUT_DATA_DIR={data_dir} ', f'HERMES_HOME={data_dir}/bots/', f'COCONUT_PROCESS_HOME={data_dir}/runtimes/')
    return [int(line.split(None, 1)[0]) for line in listing.splitlines()
            if line.strip() and any(marker in line + ' ' for marker in markers) and int(line.split(None, 1)[0]) != os.getpid()]


def backup_ignore(data_dir: Path):
    """The ignore function of shutil.copytree for the backup: the folders that the migration does not change or that
    Hermes makes again, special files, and the live database (backup() copies it with SQLite)."""
    def ignore(directory, names):
        directory = Path(directory)
        skipped = {name for name in names if special(directory / name)}
        if directory == data_dir:
            skipped |= {name for name in names if name in BACKUP_SKIP_ROOT or name.startswith('coconut.sqlite3')}
        if directory.parent == data_dir / 'bots':
            skipped |= {name for name in names if name in BACKUP_SKIP_BOT}
        relative = directory.relative_to(data_dir).parts
        if len(relative) >= 3 and relative[0] == 'bots' and relative[2] == 'home':
            skipped |= {name for name in names if name in BACKUP_SKIP_ANYWHERE and (directory / name).is_dir()}
            if len(relative) == 3:
                skipped |= {name for name in names if name in BACKUP_SKIP_HOME and (directory / name).is_dir()}
        return skipped
    return ignore


def backup_size(data_dir: Path) -> int:
    """The bytes of the files that the backup copies, with the database."""
    ignore = backup_ignore(data_dir)
    total = 0
    for directory, folders, files in os.walk(data_dir):
        skipped = ignore(directory, folders + files)
        folders[:] = [name for name in folders if name not in skipped and not os.path.islink(os.path.join(directory, name))]
        for name in files:
            if name not in skipped:
                try:
                    info = os.lstat(os.path.join(directory, name))
                except OSError:
                    continue
                total += info.st_size if stat.S_ISREG(info.st_mode) else 0
    database = data_dir / 'coconut.sqlite3'
    return total + (database.stat().st_size if database.is_file() else 0)


def _clonefile():
    """clonefile(2) of macOS, or None. On APFS, a clone shares the blocks of its source until one of them changes."""
    if sys.platform != 'darwin':
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).clonefile
    except (OSError, AttributeError):
        return None
    function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
    function.restype = ctypes.c_int
    return function


CLONEFILE = _clonefile()


def clone_or_copy(source, destination):
    """The copy_function of the backup: an APFS clone when the disk supports it, else shutil.copy2."""
    if CLONEFILE is not None and CLONEFILE(os.fsencode(source), os.fsencode(destination), CLONE_NOFOLLOW) == 0:
        return destination
    return shutil.copy2(source, destination)


def clones_work(folder: Path) -> bool:
    """True when a clone of a file in this folder works, so the backup takes almost no space."""
    if CLONEFILE is None:
        return False
    probe = folder / f'.coconut-clone-check-{os.getpid()}'
    copy = probe.with_name(probe.name + '-copy')
    try:
        probe.write_bytes(b'clone')
        return CLONEFILE(os.fsencode(probe), os.fsencode(copy), CLONE_NOFOLLOW) == 0
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)
        copy.unlink(missing_ok=True)


def check_backup_space(data_dir: Path) -> None:
    """Refuse the migration when the disk has too little free space for the backup. A clone counts as no space, but
    the database copy of SQLite is a real copy."""
    database = data_dir / 'coconut.sqlite3'
    files = backup_size(data_dir)
    needed = (database.stat().st_size if database.is_file() else 0) if clones_work(data_dir.parent) else files
    needed += BACKUP_MARGIN
    free = shutil.disk_usage(data_dir.parent).free
    if free < needed:
        gigabytes = lambda value: f'{value / 1024 ** 3:.1f} GB'
        raise SystemExit(f'The backup needs about {gigabytes(needed)} on the disk of {data_dir.parent}, but only '
                         f'{gigabytes(free)} is free. Free some space, then run this command again. Nothing changed.')


def backup(data_dir: Path) -> Path:
    check_backup_space(data_dir)
    target = data_dir.parent / f'{data_dir.name}-backup-before-profiles-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}'
    try:
        shutil.copytree(data_dir, target, symlinks=True, ignore=backup_ignore(data_dir), copy_function=clone_or_copy)
        with closing(sqlite3.connect(data_dir / 'coconut.sqlite3')) as source, \
                closing(sqlite3.connect(target / 'coconut.sqlite3')) as copy:
            source.backup(copy)
    except BaseException:
        # A partial backup only takes space, for example on a full disk.
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def special(path: Path) -> bool:
    mode = path.lstat().st_mode
    return not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode))


def move_tree(source: Path, destination: Path, rename=os.rename) -> None:
    """Rename a folder. On another disk, copy it to a hidden folder, rename that, then remove the original."""
    if destination.exists():
        raise SystemExit(f'{destination} already exists.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        rename(source, destination)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    staging = destination.with_name('.' + destination.name + '.coconut-copy')
    shutil.copytree(source, staging, symlinks=True, ignore=lambda folder, names: [n for n in names if special(Path(folder) / n)])
    os.rename(staging, destination)
    shutil.rmtree(source)


def write_ledger(data_dir: Path, ledger: dict) -> None:
    pending = data_dir / (LEDGER + '.tmp')
    pending.write_text(json.dumps(ledger, indent=2) + '\n')
    os.chmod(pending, 0o600)
    os.replace(pending, data_dir / LEDGER)


def record_rows(data_dir: Path, moves: list[dict]) -> None:
    with closing(sqlite3.connect(data_dir / 'coconut.sqlite3', isolation_level=None)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            registry.ensure_table(db)
            for move in moves:
                registry.write(db, dict(bot_id=move['bot_id'], name=move['name'], home=move['destination'], state='active'))
                bot = json.loads(db.execute('SELECT data FROM bots WHERE id=?', (move['bot_id'],)).fetchone()[0])
                bot.update(profile=move['name'], profile_state='active')
                db.execute('UPDATE bots SET data=? WHERE id=?', (json.dumps(bot), move['bot_id']))
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise


def add_skills_folder(homes: list[Path]) -> None:
    """Add <install>/skills to skills.external_dirs of the moved profiles, as setup does (skill_dirs). A failure only warns."""
    runtime_module = host.hermes_runtime()
    environment = host.service_environment()
    try:
        runtime = runtime_module.find(environment)
        with runtime_module.isolated(environment) as (isolated, folder):
            result = subprocess.run([str(runtime.python), '-I', '-B', str(host.ROOT / 'backend/coconut/skill_dirs.py'), 'add',
                                     str(runtime.root or ''), str(host.ROOT / 'skills'), *map(str, homes)],
                                    cwd=folder, env=isolated, stdin=subprocess.DEVNULL, text=True, capture_output=True,
                                    timeout=120)
        failed = result.returncode != 0
    except (runtime_module.HermesUnavailable, OSError, subprocess.TimeoutExpired):
        failed = True
    if failed:
        say('The moved profiles did not get the Coconut skills folder. Run: python3 scripts/hermes_skills.py add')


def recovery_steps(data_dir: Path, ledger: dict) -> str:
    """The manual steps that put each bot back, from the ledger."""
    lines = [f'Coconut stays stopped. The backup is {ledger["backup"]}. To undo by hand:']
    for move in ledger['moves']:
        if move['state'] in ('moving', 'moved'):
            lines.append(f'  mv "{move["destination"]}" "{move["source"]}"   # if the first folder exists')
    lines.append(f'  rm "{data_dir / LEDGER}"')
    lines.append('  python3 scripts/host.py restart')
    lines.append('A copy to another disk can stop halfway. Then compare both folders with the backup before you move one.')
    return '\n'.join(lines)


def follow_env_paths(source: Path, destination: Path) -> None:
    """A bot's .env can name a login folder inside its old home (GH_CONFIG_DIR, CODEX_HOME, XDG_*). Point it at the new home.

    Each assignment line is read as written: an optional `export`, the key with the key rule of the .env reader that
    login_folders uses (runtime_environment.env_key, so quoted keys count), `=`, and the raw value without its quotes.
    When the raw value is the old home or a path below it, only that prefix changes. The rest of the value, such as a
    ${VAR:-default} part or an escaped character, the quotes, `export` and a comment stay as written.
    """
    path = destination / '.env'
    if not path.is_file() or path.is_symlink():
        return
    runtime = host.backend_module('runtime_environment')
    old, new = str(source), str(destination)
    lines = path.read_text(encoding='utf-8').split('\n')
    changed = False
    for number, line in enumerate(lines):
        start = len(line) - len(line.lstrip(' \t'))
        export = re.match(r'export\s+', line[start:])
        start += export.end() if export else 0
        key, start = runtime.env_key(line, start)
        if key is None:
            continue
        equals = re.match(r'[ \t]*=[ \t]*', line[start:])
        if not equals or '=' not in equals.group(0):
            continue
        start += equals.end()
        if start < len(line) and line[start] in '"\'':
            # The raw value ends at the closing quote, which the reader finds. A value over several lines stays.
            closing = runtime._quoted(line, start + 1, line[start])[1]
            if closing > len(line) or line[closing - 1] != line[start]:
                continue
            first, last = start + 1, closing - 1
        else:
            first, last = start, start + len(re.split(r'\s+#', line[start:], maxsplit=1)[0].rstrip())
        raw = line[first:last]
        if raw == old or raw.startswith(old + '/'):
            lines[number] = line[:first] + new + line[first + len(old):]
            changed = True
    if changed:
        path.write_text('\n'.join(lines), encoding='utf-8')


# One line of a block-style YAML file, as Hermes writes config.yaml: indentation, list dashes, an optional key, and the
# value. A key is plain or quoted, and ends with a colon and a space.
YAML_LINE = re.compile(r"""^(?P<lead>[ \t]*(?:-[ \t]+)*(?:(?:'[^']*'|"(?:[^"\\]|\\.)*"|[^\s#'"\-?:,\[\]{}][^#]*?)[ \t]*:[ \t]+)?)(?P<rest>.*)$""")


def follow_config_paths(source: Path, destination: Path) -> None:
    """A profile's config.yaml can name files inside its old home, for example terminal.shell_init_files. Point them
    at the new home.

    Each scalar value on one line is read as written: plain, or in single or double quotes. When the whole value is
    the old home or a path below it, only that prefix changes. A comment, the quotes and the rest of the line stay.
    """
    path = destination / 'config.yaml'
    if not path.is_file() or path.is_symlink():
        return
    old, new = str(source), str(destination)
    lines = path.read_text(encoding='utf-8').split('\n')
    changed = False
    for number, line in enumerate(lines):
        match = YAML_LINE.match(line)
        if not match or line.lstrip().startswith('#'):
            continue
        start = match.start('rest')
        rest = match['rest']
        if rest[:1] in ('"', "'"):
            # The value ends at its closing quote. A value over several lines stays.
            quoted = re.match(r"'[^']*'" if rest[0] == "'" else r'"(?:[^"\\]|\\.)*"', rest)
            if not quoted:
                continue
            first, last = start + 1, start + quoted.end() - 1
        else:
            first, last = start, start + len(re.split(r'\s+#', rest, maxsplit=1)[0].rstrip())
        raw = line[first:last]
        if raw == old or raw.startswith(old + '/'):
            lines[number] = line[:first] + new + line[first + len(old):]
            changed = True
    if changed:
        path.write_text('\n'.join(lines), encoding='utf-8')


def migrate(data_dir: Path, hermes_home: Path, *, manage_service: bool = True, rename=os.rename) -> dict | None:
    if (data_dir / LEDGER).exists():
        raise SystemExit(f'{data_dir / LEDGER} exists: the migration ran before. Read it before you run it again.')
    moves = plan(data_dir, hermes_home)
    show(moves, data_dir, hermes_home)
    if not moves:
        return None
    check_backup_space(data_dir)
    state = stop_service(manage_service)
    # restart: whether the folders and the database agree, so Coconut can start again.
    restart = True
    try:
        # The service lock stays held until the database has the new rows, so the service cannot start halfway.
        with host.stopped_service(data_dir, 'migrating the bots'):
            running = coconut_workers(data_dir)
            if running:
                raise SystemExit('These Coconut workers still run: ' + ', '.join(map(str, running)) + '. Nothing changed.')
            moves = plan(data_dir, hermes_home)
            souls = {move['bot_id']: move.pop('instructions') for move in moves}
            say('Backing up the data folder (without workspaces)...')
            ledger = dict(created_at=datetime.now(timezone.utc).isoformat(), hermes_home=str(hermes_home),
                          backup=str(backup(data_dir)), moves=moves)
            write_ledger(data_dir, ledger)
            try:
                for move in moves:
                    say(f'Moving {move["bot_name"]!r} to {move["destination"]}...')
                    move['state'] = 'moving'
                    write_ledger(data_dir, ledger)
                    source, destination = Path(move['source']), Path(move['destination'])
                    try:
                        move_tree(source, destination, rename=rename)
                    except BaseException:
                        # A copy to another disk can fail after the destination exists. That bot then has two
                        # folders, so Coconut stays stopped and the ledger stays.
                        if destination.exists():
                            restart = False
                        raise
                    move['state'] = 'moved'
                    restart = False
                    if not any((destination / name).exists() for name in registry.IDENTITY_FILES):
                        # Hermes lists a folder as a profile only with a profile file.
                        (destination / 'SOUL.md').write_text(souls[move['bot_id']])
                    follow_env_paths(source, destination)
                    follow_config_paths(source, destination)
                    write_ledger(data_dir, ledger)
                record_rows(data_dir, moves)
                restart = True
            except BaseException as error:
                say(f'The migration failed: {error}')
                if restart:
                    # No folder moved. The ledger describes nothing, so it goes.
                    (data_dir / LEDGER).unlink(missing_ok=True)
                    say('No bot moved. Coconut starts again as before.')
                else:
                    say(recovery_steps(data_dir, ledger))
                raise SystemExit(1) from error
            ledger['completed_at'] = datetime.now(timezone.utc).isoformat()
            try:
                write_ledger(data_dir, ledger)
            except OSError as error:
                say(f'The migration finished, but the ledger could not record it: {error}')
            add_skills_folder([Path(move['destination']) for move in moves])
    finally:
        if state is not None and restart:
            host.restore_after_update(state)
    say(f'Done. {len(moves)} bots are Hermes profiles. Coconut adds your other profiles as bots when it starts.')
    for move in moves:
        say(f'  {move["bot_name"]!r}: hermes -p {move["name"]}')
    return ledger


def main(argv=None) -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', type=Path, default=host.DEFAULT_DATA, help='The Coconut data folder.')
    parser.add_argument('--hermes-home', type=Path, help='Your Hermes home. Defaults to the one of the service.')
    parser.add_argument('--dry-run', action='store_true', help='Show the plan. Change nothing.')
    parser.add_argument('--no-service', action='store_true', help='The service is already stopped. Do not start it.')
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    status = host.hermes_runtime().read_status(data_dir) or {}
    hermes_home = (args.hermes_home or Path(status.get('home') or Path.home() / '.hermes')).expanduser().absolute()
    if not hermes_home.is_dir():
        raise SystemExit(f'{hermes_home} does not exist. Give your Hermes home with --hermes-home.')
    problem = home_problem(data_dir, hermes_home)
    if args.dry_run:
        show(plan(data_dir, hermes_home), data_dir, hermes_home)
        if problem:
            say('Before the migration: ' + problem)
        say('This was a dry run. Nothing changed.')
        return
    if problem:
        raise SystemExit(problem + '\nNothing changed.')
    if not args.no_service and data_dir != host.DEFAULT_DATA.resolve():
        raise SystemExit('Stop the service of that data folder yourself, then add --no-service.')
    migrate(data_dir, hermes_home, manage_service=not args.no_service)


if __name__ == '__main__':
    main()
