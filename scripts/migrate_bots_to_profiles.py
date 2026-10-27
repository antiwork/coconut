#!/usr/bin/env python3
"""Move each Coconut bot home into a Hermes profile of the person, once.

Before this step, each bot has its own Hermes home in <data>/bots/<uuid>/home (the data directory is
~/Coconut/.runtime/host). After it, each bot is a profile in <Hermes home>/profiles/<name>, so Coconut chats, the
Hermes CLI (`hermes -p <name>`) and the Telegram gateway share the same skills, memories, config and cron jobs.

Run it by hand, in this order:
  python3 scripts/migrate_bots_to_profiles.py --dry-run     Show the plan. Changes nothing.
  python3 scripts/migrate_bots_to_profiles.py               Migrate.
  python3 scripts/migrate_bots_to_profiles.py --rollback    Move the folders back.

The migration:
1. Asks the service to finish its turns, stops it, and waits until no Coconut worker runs.
2. Backs up the data directory, without the workspaces, which it does not change.
3. Chooses a unique profile name for each bot, from the bot name.
4. Moves bots/<uuid>/home to <Hermes home>/profiles/<name>. On another disk, it copies the folder, compares the
   SHA-256 of every file, and then removes the original.
5. Writes the profiles rows into coconut.sqlite3 and a ledger (profile-migration.json) for --rollback.
6. Starts the service again. At its start, the service adds the person's default profile and every other profile as bots.

Bot IDs do not change, so paired phones keep their chats. Sessions, memories, skills and cron jobs move with the folder.
Kanban and workspaces stay in the data directory.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import time

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import host  # noqa: E402

registry = host.backend_module('profile_registry')

LEDGER = 'profile-migration.json'
# The backup leaves out the folders that the migration does not change. They can be large.
BACKUP_SKIP_ROOT = {'workspaces', 'runtimes', 'artifacts'}
BACKUP_SKIP_BOT = {'workspace', 'browser', 'tmp'}
WORKER_WAIT_SECONDS = 120


class MigrationError(SystemExit):
    pass


def say(text: str = '') -> None:
    print(text, flush=True)


# The plan


def legacy_bots(data_dir: Path) -> list[dict]:
    """Bots that are not deleted, have no profiles row, and still have a home in <data>/bots/<uuid>/home."""
    database = data_dir / 'coconut.sqlite3'
    if not database.is_file():
        raise MigrationError(f'{database} does not exist. Check --data-dir.')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        bots = [json.loads(row[0]) for row in db.execute('SELECT data FROM bots')]
        try:
            known = {row[0] for row in db.execute('SELECT bot_id FROM profiles')}
        except sqlite3.OperationalError:
            known = set()
    result = []
    for bot in bots:
        home = data_dir / 'bots' / bot['id'] / 'home'
        if bot.get('_deleted') or bot['id'] in known or not home.is_dir() or home.is_symlink():
            continue
        result.append(bot)
    return sorted(result, key=lambda bot: (bot.get('created_at', ''), bot['id']))


def device_of(path: Path) -> int:
    """The device of a path, or of its nearest existing parent."""
    for candidate in (path, *path.parents):
        try:
            return candidate.stat().st_dev
        except FileNotFoundError:
            continue
    raise MigrationError(f'No folder of {path} exists.')


def plan(data_dir: Path, hermes_home: Path) -> list[dict]:
    taken = set(registry.taken_names(hermes_home))
    moves = []
    for bot in legacy_bots(data_dir):
        name = registry.slug(bot.get('name') or 'bot', taken)
        taken.add(name)
        source = data_dir / 'bots' / bot['id'] / 'home'
        destination = hermes_home / 'profiles' / name
        method = 'rename' if device_of(source) == device_of(destination) else 'copy'
        moves.append(dict(bot_id=bot['id'], bot_name=bot.get('name', ''), name=name, source=str(source),
                          destination=str(destination), method=method, state='planned'))
    return moves


def show_plan(moves: list[dict], data_dir: Path, hermes_home: Path) -> None:
    say(f'Coconut data:  {data_dir}')
    say(f'Hermes home:   {hermes_home}')
    if not moves:
        say('No bot uses an earlier home. Nothing to migrate.')
        return
    say(f'{len(moves)} bots move into Hermes profiles:')
    for move in moves:
        how = 'move' if move['method'] == 'rename' else 'copy, check SHA-256, then remove (another disk)'
        say(f'  {move["bot_name"]!r} ({move["bot_id"]})')
        say(f'    {move["source"]}')
        say(f'    -> {move["destination"]}  [{how}]')
    say('Each bot keeps its ID. After the migration, `hermes -p <name>` opens the same profile as Coconut.')


# Stop the service and wait for its workers


def coconut_workers(data_dir: Path) -> list[int]:
    """The processes of Coconut workers for this data directory. `ps e` shows the environment of this account's processes."""
    listing = subprocess.run(['ps', '-Aeww', '-o', 'pid=,command='], capture_output=True, text=True, timeout=30).stdout
    markers = (f'COCONUT_DATA_DIR={data_dir} ', f'HERMES_HOME={data_dir}/bots/', f'COCONUT_WORKSPACE_ROOT={data_dir}/workspaces ',
               f'COCONUT_PROCESS_HOME={data_dir}/runtimes/')
    found = []
    for line in listing.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit() and int(fields[0]) != os.getpid() and any(
                marker in fields[1] + ' ' for marker in markers):
            found.append(int(fields[0]))
    return found


def wait_for_workers(data_dir: Path, workers=None, timeout: float = WORKER_WAIT_SECONDS) -> None:
    workers = workers or coconut_workers
    deadline = time.monotonic() + timeout
    while True:
        running = workers(data_dir)
        if not running:
            return
        if time.monotonic() >= deadline:
            raise MigrationError('These Coconut workers still run: ' + ', '.join(map(str, running))
                                 + '. Stop them, then run the migration again. Nothing changed.')
        say(f'Waiting for {len(running)} Coconut workers to stop...')
        time.sleep(2)


@contextmanager
def service_lock(data_dir: Path):
    """Hold the lock that the service holds while it runs, so it cannot start during the migration."""
    descriptor = os.open(data_dir / 'service.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError('The Coconut service still runs. Stop it, then run the migration again. Nothing changed.') from None
        yield


@contextmanager
def stopped_service(data_dir: Path, manage: bool):
    """Stop the service of this installation, and start it again afterwards.

    The body sets control['restart'] to False when the folders and the database do not agree. Coconut then stays
    stopped, because a service start in that state could add the moved profiles as new bots.
    """
    state = None
    control = {'restart': True}
    if manage:
        say('Asking Coconut to finish its turns...')
        host.drain_for_update()
        say('Stopping Coconut...')
        state = host.stop_for_update()
    try:
        with service_lock(data_dir):
            wait_for_workers(data_dir)
            yield control
    finally:
        if state is not None and control['restart']:
            say('Starting Coconut again...')
            host.restore_after_update(state)
        elif state is not None:
            say('Coconut stays stopped. Fix the problem above, then run --rollback, or start Coconut with '
                'python3 scripts/host.py restart.')


# Copy, check and move folders


def special(path: Path) -> bool:
    mode = path.lstat().st_mode
    return not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode))


def manifest(root: Path) -> dict:
    """Describe a folder: the kind, mode and SHA-256 (or link target) of every entry. Sockets and pipes do not count."""
    result = {}
    for directory, folders, files in os.walk(root):
        for name in sorted(folders + files):
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                result[relative] = ('link', os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                result[relative] = ('dir', stat.S_IMODE(info.st_mode))
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                with path.open('rb') as handle:
                    for block in iter(lambda: handle.read(1 << 20), b''):
                        digest.update(block)
                result[relative] = ('file', stat.S_IMODE(info.st_mode), digest.hexdigest())
    return result


def copy_tree(source: Path, destination: Path) -> None:
    def ignore(directory, names):
        return [name for name in names if special(Path(directory) / name)]
    shutil.copytree(source, destination, symlinks=True, ignore=ignore, copy_function=shutil.copy2)


def move_tree(source: Path, destination: Path, rename=os.rename) -> str:
    """Move a folder. Return 'rename', or 'copy' when the folders are on different disks.

    A copy goes to a hidden folder beside the destination first, so Hermes never lists a half copy. The original is
    removed only after the SHA-256 of every file matches.
    """
    if destination.exists() or destination.is_symlink():
        raise MigrationError(f'{destination} already exists. Nothing moved.')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        rename(source, destination)
        return 'rename'
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    staging = destination.with_name('.' + destination.name + '.coconut-copy')
    if staging.exists():
        shutil.rmtree(staging)
    copy_tree(source, staging)
    if manifest(source) != manifest(staging):
        shutil.rmtree(staging)
        raise MigrationError(f'The copy of {source} does not match the original. The original stays in place.')
    os.rename(staging, destination)
    shutil.rmtree(source)
    return 'copy'


def ensure_identity(home: Path, instructions: str) -> None:
    """Hermes lists a folder as a profile only when it has a profile file. An earlier bot home always had SOUL.md."""
    if not any((home / name).is_file() or (home / name).is_symlink() for name in registry.IDENTITY_FILES):
        (home / 'SOUL.md').write_text(instructions or '', encoding='utf-8')


# The backup, the ledger and the database


def stamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def backup(data_dir: Path) -> Path:
    target = data_dir.parent / f'{data_dir.name}-backup-before-profiles-{stamp()}'

    def ignore(directory, names):
        directory = Path(directory)
        skipped = [name for name in names if special(directory / name)]
        if directory == data_dir:
            skipped += [name for name in names if name in BACKUP_SKIP_ROOT or name.startswith('coconut.sqlite3')]
        if directory.parent == data_dir / 'bots':
            skipped += [name for name in names if name in BACKUP_SKIP_BOT]
        if directory == data_dir / 'kanban' / 'kanban':
            skipped += [name for name in names if name == 'workspaces']
        return skipped

    shutil.copytree(data_dir, target, symlinks=True, ignore=ignore, copy_function=shutil.copy2)
    # A consistent copy of the database, with its WAL.
    with closing(sqlite3.connect(data_dir / 'coconut.sqlite3')) as source, closing(sqlite3.connect(target / 'coconut.sqlite3')) as copy:
        source.backup(copy)
    os.chmod(target, 0o700)
    return target


def write_ledger(data_dir: Path, ledger: dict) -> None:
    path = data_dir / LEDGER
    pending = path.with_name(path.name + '.tmp')
    pending.write_text(json.dumps(ledger, indent=2) + '\n')
    os.chmod(pending, 0o600)
    os.replace(pending, path)


def read_ledger(data_dir: Path) -> dict:
    try:
        return json.loads((data_dir / LEDGER).read_text())
    except FileNotFoundError:
        raise MigrationError(f'{data_dir / LEDGER} does not exist. No migration to roll back.') from None


def record_rows(data_dir: Path, moves: list[dict]) -> None:
    with closing(sqlite3.connect(data_dir / 'coconut.sqlite3', isolation_level=None)) as db:
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('BEGIN IMMEDIATE')
        try:
            registry.ensure_table(db)
            for move in moves:
                info = Path(move['destination']).stat()
                registry.write(db, dict(bot_id=move['bot_id'], name=move['name'], home=move['destination'],
                                        dev=info.st_dev, ino=info.st_ino, state='active'))
                row = db.execute('SELECT data FROM bots WHERE id=?', (move['bot_id'],)).fetchone()
                bot = json.loads(row[0])
                bot.update(profile=move['name'], profile_state='active')
                db.execute('UPDATE bots SET data=? WHERE id=?', (json.dumps(bot), move['bot_id']))
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise


def forget_rows(data_dir: Path, bot_ids: list[str]) -> None:
    with closing(sqlite3.connect(data_dir / 'coconut.sqlite3', isolation_level=None)) as db:
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('BEGIN IMMEDIATE')
        try:
            registry.ensure_table(db)
            for bot_id in bot_ids:
                db.execute('DELETE FROM profiles WHERE bot_id=?', (bot_id,))
                row = db.execute('SELECT data FROM bots WHERE id=?', (bot_id,)).fetchone()
                if row:
                    bot = json.loads(row[0])
                    bot.pop('profile', None)
                    bot.pop('profile_state', None)
                    db.execute('UPDATE bots SET data=? WHERE id=?', (json.dumps(bot), bot_id))
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise


def current_home(data_dir: Path, bot_id: str) -> str | None:
    """The profile folder of a bot now. The person can have renamed it since the migration."""
    row = registry.lookup(data_dir, bot_id)
    return row['home'] if row else None


# The commands


def migrate(data_dir: Path, hermes_home: Path, *, manage_service: bool = True, rename=os.rename) -> dict | None:
    if (data_dir / LEDGER).exists():
        raise MigrationError(f'{data_dir / LEDGER} exists, so the migration ran before. Use --rollback first to run it again.')
    moves = plan(data_dir, hermes_home)
    show_plan(moves, data_dir, hermes_home)
    if not moves:
        return None
    with stopped_service(data_dir, manage_service) as control:
        # The plan can change while the service stops, for example when a bot was created.
        moves = plan(data_dir, hermes_home)
        instructions = {bot['id']: bot.get('instructions', '') for bot in legacy_bots(data_dir)}
        say('Backing up the data directory (without workspaces)...')
        saved = backup(data_dir)
        say(f'Backup: {saved}')
        ledger = dict(version=1, created_at=datetime.now(timezone.utc).isoformat(), data_dir=str(data_dir),
                      hermes_home=str(hermes_home), backup=str(saved), moves=moves)
        write_ledger(data_dir, ledger)
        try:
            for move in moves:
                source, destination = Path(move['source']), Path(move['destination'])
                say(f'Moving {move["bot_name"]!r} to {destination}...')
                move['state'] = 'moving'
                write_ledger(data_dir, ledger)
                move['method'] = move_tree(source, destination, rename=rename)
                ensure_identity(destination, instructions.get(move['bot_id'], ''))
                move['state'] = 'moved'
                write_ledger(data_dir, ledger)
            record_rows(data_dir, moves)
        except BaseException as error:
            # Before Coconut starts again, every bot must be back in its earlier home. Else it stays stopped.
            say(f'The migration failed: {error}')
            try:
                undo_moves(data_dir, ledger, rename=rename)
            except BaseException:
                control['restart'] = False
                say(f'Coconut could not move every folder back. The ledger {data_dir / LEDGER} and the backup '
                    f'{saved} show where each bot is.')
                raise
            os.replace(data_dir / LEDGER, data_dir / f'profile-migration.failed-{stamp()}.json')
            say('Every bot is back in its earlier home.')
            raise
        ledger['completed_at'] = datetime.now(timezone.utc).isoformat()
        write_ledger(data_dir, ledger)
        # A moved profile keeps the folder of Coconut's own skills in skills.external_dirs, or gets it now.
        add_skills_folder([Path(move['destination']) for move in moves])
    say(f'Done. {len(moves)} bots are Hermes profiles now:')
    for move in moves:
        say(f'  {move["bot_name"]!r}: hermes -p {move["name"]}')
    say(f'To undo: python3 {Path(__file__).name} --rollback')
    return ledger


def add_skills_folder(homes: list[Path]) -> bool:
    """Add <install>/skills to skills.external_dirs of these profiles, with the config writer of Hermes (skill_dirs).

    A failure does not undo the migration. The command that setup runs adds the folder later.
    """
    runtime_module = host.hermes_runtime()
    environment = host.service_environment()
    try:
        runtime = runtime_module.find(environment)
        with runtime_module.isolated(environment) as (isolated, folder):
            result = subprocess.run([str(runtime.python), '-I', '-B', str(host.ROOT / 'backend/coconut/skill_dirs.py'), 'add',
                                     str(runtime.root or ''), str(host.ROOT / 'skills'), *map(str, homes)],
                                    cwd=folder, env=isolated, stdin=subprocess.DEVNULL, text=True, capture_output=True,
                                    timeout=120)
    except (runtime_module.HermesUnavailable, OSError, subprocess.TimeoutExpired) as error:
        say(f'The moved profiles did not get the Coconut skills folder ({error}). Run: python3 scripts/hermes_skills.py add')
        return False
    if result.returncode:
        say(result.stdout + result.stderr)
        say('Some moved profiles did not get the Coconut skills folder. Run: python3 scripts/hermes_skills.py add')
        return False
    return True


def undo_moves(data_dir: Path, ledger: dict, rename=os.rename) -> None:
    """Move back each folder that a failed migration moved. The database has no profiles rows for them yet."""
    for move in reversed(ledger['moves']):
        if move['state'] == 'planned':
            continue
        source, destination = Path(move['source']), Path(move['destination'])
        staging = destination.with_name('.' + destination.name + '.coconut-copy')
        if staging.exists():
            shutil.rmtree(staging)
        if destination.exists() and not source.exists():
            say(f'Moving {move["bot_name"]!r} back to {source}...')
            move_tree(destination, source, rename=rename)
        elif destination.exists():
            raise MigrationError(f'Both {source} and {destination} exist. Check them by hand.')
        move['state'] = 'planned'
        write_ledger(data_dir, ledger)


def rollback(data_dir: Path, *, manage_service: bool = True, rename=os.rename) -> dict:
    ledger = read_ledger(data_dir)
    say(f'Rolling back the migration of {ledger["created_at"]} ({len(ledger["moves"])} bots).')
    with stopped_service(data_dir, manage_service) as control:
        # A rollback that stops halfway leaves Coconut stopped, so the service never sees half of the bots moved.
        control['restart'] = False
        for move in reversed(ledger['moves']):
            source = Path(move['source'])
            moved = Path(current_home(data_dir, move['bot_id']) or move['destination'])
            if move['state'] == 'planned' or (source.exists() and not moved.exists()):
                continue
            if source.exists():
                raise MigrationError(f'Both {source} and {moved} exist. Check them by hand. The rollback stopped.')
            if not moved.exists():
                raise MigrationError(f'{moved} is missing. Restore it from {ledger["backup"]}. The rollback stopped.')
            say(f'Moving {move["bot_name"]!r} back to {source}...')
            move_tree(moved, source, rename=rename)
            move['state'] = 'planned'
            write_ledger(data_dir, ledger)
        forget_rows(data_dir, [move['bot_id'] for move in ledger['moves']])
        done = data_dir / f'profile-migration.rolled-back-{stamp()}.json'
        os.replace(data_dir / LEDGER, done)
        control['restart'] = True
    say(f'Done. The bots use their earlier homes again. The ledger is now {done.name}. The backup stays in {ledger["backup"]}.')
    return ledger


APP_ID = 'com.gumroad.coconut.mac'


def service_hermes_home(data_dir: Path) -> Path | None:
    """The Hermes home of the running service, from the status that it saved at its last start (hermes.json)."""
    status = host.hermes_runtime().read_status(data_dir)
    home = (status or {}).get('home')
    return Path(home).expanduser() if isinstance(home, str) and home else None


def default_hermes_home(data_dir: Path) -> Path:
    found = service_hermes_home(data_dir)
    if found is not None:
        return found
    runtime = host.hermes_runtime()
    return Path(runtime.hermes_home(environ=host.service_environment())).expanduser()


def home_problem(data_dir: Path, hermes_home: Path) -> str | None:
    """Explain why the service would not find the bots in hermes_home, or return None.

    The service rescans the profiles of its own Hermes home. When its HERMES_HOME is another folder, for example the
    shared model home in .runtime/hermes/terminal/hermes of an earlier install, it would not see the moved bots.
    """
    service = service_hermes_home(data_dir)
    target = hermes_home.expanduser().resolve()
    inside = host.ROOT.resolve() / '.runtime'
    if target == inside or inside in target.parents:
        return (f'{hermes_home} is a folder of Coconut, not your Hermes home. Give your Hermes home with '
                '--hermes-home, for example --hermes-home ~/.hermes.')
    if service is None or service.resolve() == target:
        return None
    return (f'The Coconut service uses HERMES_HOME={service}, but the bots would move into {hermes_home}.\n'
            'First make the service use that Hermes home:\n'
            f'  defaults write {APP_ID} ServiceEnvironment -dict-add HERMES_HOME "{target}"\n'
            '  launchctl kickstart -k "gui/$(id -u)/com.gumroad.coconut.service"\n'
            f'Then check that {data_dir / "hermes.json"} names {target} as "home", and run this command again.')


def main(argv=None) -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', type=Path, default=host.DEFAULT_DATA, help='The Coconut data directory.')
    parser.add_argument('--hermes-home', type=Path, help='The Hermes home. Defaults to the one that the service uses.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Show the plan. Change nothing.')
    mode.add_argument('--rollback', action='store_true', help='Move the bot folders back to their earlier homes.')
    parser.add_argument('--no-service', action='store_true',
                        help='Do not stop and start the service. The service must already be stopped.')
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    manage = not args.no_service and data_dir == host.DEFAULT_DATA.resolve()
    if not args.no_service and not manage:
        raise SystemExit('Stop the service of that data directory yourself, then add --no-service.')
    if args.rollback:
        rollback(data_dir, manage_service=manage)
        return
    hermes_home = (args.hermes_home or default_hermes_home(data_dir)).expanduser().absolute()
    if not hermes_home.is_dir():
        raise SystemExit(f'{hermes_home} does not exist. Install Hermes, or give its home with --hermes-home.')
    problem = home_problem(data_dir, hermes_home)
    if args.dry_run:
        show_plan(plan(data_dir, hermes_home), data_dir, hermes_home)
        if problem:
            say('Before the migration: ' + problem)
        say('This was a dry run. Nothing changed.')
        return
    if problem:
        raise SystemExit(problem + '\nNothing changed.')
    migrate(data_dir, hermes_home, manage_service=manage)


if __name__ == '__main__':
    main()
