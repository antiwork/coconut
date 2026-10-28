"""scripts/migrate_bots_to_profiles.py moves the earlier bot homes into Hermes profiles, once."""
import contextlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut import hermes_runtime, profile_registry
from coconut.service import Service
from test_service import ControlledProvider

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('migrate_bots_to_profiles', SCRIPTS / 'migrate_bots_to_profiles.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)
# setUp replaces it, and one test runs it.
EDIT_PROFILE_CONFIGS = migration.edit_profile_configs


def tree(folder):
    return {str(path.relative_to(folder)): path.read_bytes() for path in sorted(Path(folder).rglob('*'))
            if path.is_file() and not path.name.startswith('coconut.sqlite3-')}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.root = base / 'Coconut/.runtime/host'
        self.hermes = base / '.hermes'
        (self.hermes / 'profiles/ada').mkdir(parents=True)  # The person already has a profile named ada.
        (self.hermes / 'profiles/ada/SOUL.md').write_text('The own Ada.\n')
        (self.hermes / 'config.yaml').write_text('model: {}\n')
        self.root.mkdir(parents=True)
        # An install from before the profile layout: a database with bots, and a home for each bot.
        self.bots = [dict(id=str(uuid4()), session_id=str(uuid4()), name=name, role='', instructions=name + ' soul',
                          created_at=f'2026-10-0{index + 1}', archived=False, pinned=False, status='idle')
                     for index, name in enumerate(('Ada', 'Ada', 'Research Lead'))]
        deleted = dict(self.bots[0], id=str(uuid4()), _deleted=True)
        with contextlib.closing(sqlite3.connect(self.root / 'coconut.sqlite3')) as db:
            db.execute('CREATE TABLE bots (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            for bot in (*self.bots, deleted):
                bot['profile_id'] = bot['id']
                db.execute('INSERT INTO bots VALUES (?, ?)', (bot['id'], json.dumps(bot)))
            db.commit()
        for bot in (*self.bots, deleted):
            home = self.home(bot)
            (home / 'memories').mkdir(parents=True)
            (home / 'SOUL.md').write_text(bot['instructions'])
            (home / 'memories/MEMORY.md').write_text('Remember ' + bot['id'])
            (home / 'cron').mkdir()
            (home / 'cron/jobs.json').write_text('{"jobs": []}')
            os.symlink('SOUL.md', home / 'link.md')
            (self.root / 'bots' / bot['id'] / 'workspace').mkdir()
        self.deleted = deleted
        self.before = {bot['id']: tree(self.home(bot)) for bot in self.bots}
        self.calls = []
        for name, value in (('coconut_workers', []), ('edit_profile_configs', None)):
            patcher = patch.object(migration, name, side_effect=lambda *args, value=value, name=name: self.calls.append((name, args)) or value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (('drain_for_update', None), ('stop_for_update', {'mode': 'app'}), ('restore_after_update', None)):
            patcher = patch.object(migration.host, name, side_effect=lambda *args, value=value, name=name: self.calls.append((name, args)) or value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def home(self, bot):
        return self.root / 'bots' / bot['id'] / 'home'

    def run_quietly(self, function, *args, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                return function(*args, **kwargs), output.getvalue()
            except SystemExit as error:
                error.output = output.getvalue()
                raise

    def steps(self):
        return [call[0] for call in self.calls if call[0] != 'coconut_workers']

    def test_dry_run_shows_unique_names_and_changes_nothing(self):
        before = tree(self.root)
        _, output = self.run_quietly(migration.main, ['--data-dir', str(self.root), '--hermes-home', str(self.hermes), '--dry-run'])
        self.assertEqual(tree(self.root), before)
        for name in ('ada-2', 'ada-3', 'research-lead'):
            self.assertIn(name, output)
        self.assertNotIn(self.deleted['id'], output)
        self.assertEqual(self.steps(), [])

    def test_the_migration_moves_each_home_and_the_service_runs_the_bots_as_profiles(self):
        ledger, output = self.run_quietly(migration.migrate, self.root, self.hermes)
        self.assertEqual(self.steps(), ['drain_for_update', 'stop_for_update', 'edit_profile_configs', 'restore_after_update'])
        names = {move['bot_id']: move['name'] for move in ledger['moves']}
        self.assertEqual(sorted(names.values()), ['ada-2', 'ada-3', 'research-lead'])
        for bot in self.bots:
            folder = self.hermes / 'profiles' / names[bot['id']]
            self.assertEqual(tree(folder), self.before[bot['id']])
            self.assertTrue((folder / 'link.md').is_symlink())
            self.assertFalse(self.home(bot).exists())
            self.assertTrue((self.root / 'bots' / bot['id'] / 'workspace').is_dir())
        self.assertTrue(self.home(self.deleted).is_dir())
        edited = next(args[0] for name, args in self.calls if name == 'edit_profile_configs')
        self.assertEqual(sorted(Path(move['destination']) for move in edited),
                         sorted(self.hermes / 'profiles' / name for name in names.values()))
        self.assertTrue((Path(ledger['backup']) / 'coconut.sqlite3').is_file())
        self.assertTrue((Path(ledger['backup']) / 'bots' / self.bots[0]['id'] / 'home/SOUL.md').is_file())
        self.assertFalse((Path(ledger['backup']) / 'bots' / self.bots[0]['id'] / 'workspace').exists())
        self.assertIn('hermes -p research-lead', output)
        service = Service(self.root, ControlledProvider(), start_workers=False, hermes_home=self.hermes)
        try:
            bots = {bot['id']: bot for bot in service.snapshot()['bots']}
            for bot in self.bots:
                self.assertEqual(bots[bot['id']]['session_id'], bot['session_id'])
                self.assertEqual(service.profiles.home(bot), self.hermes / 'profiles' / names[bot['id']])
            self.assertEqual(sorted(bot['profile'] for bot in bots.values()), ['ada', 'ada-2', 'ada-3', 'default', 'research-lead'])
            self.assertFalse(service.hermes_setup()['migration_required'])
        finally:
            service.close()
        with self.assertRaises(SystemExit):
            self.run_quietly(migration.migrate, self.root, self.hermes)

    def test_another_disk_copies_then_removes(self):
        def rename(source, destination):
            if 'bots' in str(source):
                raise OSError(errno.EXDEV, 'Cross-device link')
            os.rename(source, destination)

        ledger, _ = self.run_quietly(migration.migrate, self.root, self.hermes, rename=rename)
        for move in ledger['moves']:
            self.assertFalse(Path(move['source']).exists())
            self.assertEqual(tree(move['destination']), self.before[move['bot_id']])

    def test_a_failure_before_the_first_move_changes_nothing_and_starts_coconut(self):
        with patch.object(migration, 'backup', side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            with self.assertRaises(OSError):
                self.run_quietly(migration.migrate, self.root, self.hermes)
        self.assertEqual(self.steps(), ['drain_for_update', 'stop_for_update', 'restore_after_update'])
        self.assertFalse((self.root / migration.LEDGER).exists())
        for bot in self.bots:
            self.assertEqual(tree(self.home(bot)), self.before[bot['id']])

    def test_a_failure_halfway_stops_and_prints_the_recovery_steps(self):
        moved = []

        def rename(source, destination):
            if len(moved) == 1:
                raise OSError(errno.ENOSPC, 'No space left on device')
            moved.append(source)
            os.rename(source, destination)

        with self.assertRaises(SystemExit) as raised:
            self.run_quietly(migration.migrate, self.root, self.hermes, rename=rename)
        output = raised.exception.output
        self.assertEqual(self.steps(), ['drain_for_update', 'stop_for_update'])
        ledger = json.loads((self.root / migration.LEDGER).read_text())
        first = ledger['moves'][0]
        self.assertEqual([move['state'] for move in ledger['moves']], ['moved', 'moving', 'planned'])
        self.assertIn(f'mv "{first["destination"]}" "{first["source"]}"', output)
        self.assertIn('Coconut stays stopped', output)
        self.assertIsNone(profile_registry.lookup(self.root, first['bot_id']))
        self.assertNotIn('instructions', json.dumps(ledger))

    def test_a_failed_first_move_changes_nothing_and_starts_coconut(self):
        def rename(source, destination):
            raise OSError(errno.EACCES, 'Permission denied')

        with self.assertRaises(SystemExit) as raised:
            self.run_quietly(migration.migrate, self.root, self.hermes, rename=rename)
        self.assertIn('No bot moved', raised.exception.output)
        self.assertEqual(self.steps(), ['drain_for_update', 'stop_for_update', 'restore_after_update'])
        self.assertFalse((self.root / migration.LEDGER).exists())
        for bot in self.bots:
            self.assertEqual(tree(self.home(bot)), self.before[bot['id']])

    def test_a_failed_last_ledger_write_still_starts_coconut(self):
        original = migration.write_ledger

        def write(data_dir, ledger):
            if 'completed_at' in ledger:
                raise OSError(errno.ENOSPC, 'No space left on device')
            original(data_dir, ledger)

        with patch.object(migration, 'write_ledger', side_effect=write):
            _, output = self.run_quietly(migration.migrate, self.root, self.hermes)
        self.assertIn('the ledger could not record it', output)
        self.assertEqual(self.steps()[-1], 'restore_after_update')
        self.assertIsNotNone(profile_registry.lookup(self.root, self.bots[0]['id']))

    def test_login_folders_inside_the_old_home_follow_the_move(self):
        home = self.home(self.bots[2])
        (home / '.env').write_text(f'GH_CONFIG_DIR={home}/.config/gh\nexport CODEX_HOME="{home}/codex"  # login\n'
                                   f"HOME_COPY='{home}'\nXDG_CONFIG_HOME=/elsewhere/config\nOTHER={home}-sibling\n"
                                   f'MIDDLE=/mirror{home}/gh\nLIST=/a:{home}/bin\n'
                                   f"'GH_TOKEN_DIR'=\"{home}/.config/token\"\n\"XDG_DATA_HOME\"='{home}/data'\n"
                                   f'GH_CONFIG_DIR_2="{home}/${{GH_SUBDIR:-.config/gh}}"\nESCAPED="{home}/a\\"b"\n'
                                   f'NOT_A_HOME="${{BASE}}{home}/x"\n')
        ledger, _ = self.run_quietly(migration.migrate, self.root, self.hermes)
        destination = next(Path(move['destination']) for move in ledger['moves'] if move['bot_id'] == self.bots[2]['id'])
        self.assertEqual((destination / '.env').read_text(),
                         f'GH_CONFIG_DIR={destination}/.config/gh\nexport CODEX_HOME="{destination}/codex"  # login\n'
                         f"HOME_COPY='{destination}'\nXDG_CONFIG_HOME=/elsewhere/config\nOTHER={home}-sibling\n"
                         f'MIDDLE=/mirror{home}/gh\nLIST=/a:{home}/bin\n'
                         f"'GH_TOKEN_DIR'=\"{destination}/.config/token\"\n\"XDG_DATA_HOME\"='{destination}/data'\n"
                         f'GH_CONFIG_DIR_2="{destination}/${{GH_SUBDIR:-.config/gh}}"\nESCAPED="{destination}/a\\"b"\n'
                         f'NOT_A_HOME="${{BASE}}{home}/x"\n')
        # The keys that login_folders reads, quoted or not, now name folders in the new home.
        runtime = migration.host.backend_module('runtime_environment')
        self.assertEqual(runtime.login_folders(destination)['XDG_DATA_HOME'], f'{destination}/data')

    def test_the_config_edits_run_under_the_python_of_hermes(self):
        moves = [dict(source='/old/a', destination='/new/a'), dict(source='/old/b', destination='/new/b')]
        runtime = SimpleNamespace(python=Path('/hermes/venv/bin/python'), root=Path('/hermes/source'))
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 1 if 'config_paths.py' in command[3] else 0, '', '')
        with patch.object(migration, 'edit_profile_configs', EDIT_PROFILE_CONFIGS), \
                patch.object(migration.host, 'hermes_runtime',
                             return_value=SimpleNamespace(find=lambda environ: runtime, isolated=hermes_runtime.isolated,
                                                          HermesUnavailable=hermes_runtime.HermesUnavailable)), \
                patch.object(migration.host, 'service_environment', return_value=dict(os.environ)), \
                patch.object(migration.subprocess, 'run', side_effect=run):
            _, output = self.run_quietly(migration.edit_profile_configs, moves)
        backend = migration.host.ROOT / 'backend/coconut'
        self.assertEqual(commands, [
            ['/hermes/venv/bin/python', '-I', '-B', str(backend / 'config_paths.py'), '/hermes/source',
             '/old/a', '/new/a', '/old/b', '/new/b'],
            ['/hermes/venv/bin/python', '-I', '-B', str(backend / 'skill_dirs.py'), 'add', '/hermes/source',
             str(migration.host.ROOT / 'skills'), '/new/a', '/new/b'],
        ])
        # A failed step only warns, and the next step still runs.
        self.assertIn('can still name its old home', output)
        self.assertNotIn('skills folder', output)

    def test_the_backup_leaves_out_folders_that_hermes_makes_again(self):
        home = self.home(self.bots[0])
        for folder in ('installs/abc/venv', 'tools/node', 'cache/x', 'skills/a/__pycache__', 'skills/a/node_modules/m',
                       'skills/a/cache', 'memories/tools'):
            (home / folder).mkdir(parents=True)
            (home / folder / 'file').write_text('data')
        (home / 'tools.md').write_text('A file of this name stays.')
        target = migration.backup(self.root)
        copy = target / 'bots' / self.bots[0]['id'] / 'home'
        for folder in ('installs', 'tools', 'cache', 'skills/a/__pycache__', 'skills/a/node_modules'):
            self.assertFalse((copy / folder).exists(), folder)
        for folder in ('skills/a/cache/file', 'memories/tools/file', 'tools.md', 'SOUL.md', 'memories/MEMORY.md'):
            self.assertTrue((copy / folder).is_file(), folder)
        self.assertTrue((copy / 'link.md').is_symlink())
        # The estimate counts the copied files only.
        self.assertLess(migration.backup_size(self.root), sum(path.stat().st_size for path in self.root.rglob('*') if path.is_file()))

    def test_the_backup_clones_each_file_and_copies_when_a_clone_fails(self):
        cloned = []
        def clone(source, destination, flags):
            cloned.append(os.fsdecode(source))
            self.assertEqual(flags, migration.CLONE_NOFOLLOW)
            return -1  # For example another volume: the copy takes over.
        with patch.object(migration, 'CLONEFILE', side_effect=clone):
            target = migration.backup(self.root)
        home = self.home(self.bots[0])
        # copytree calls itself for each folder, and the nested files use the same copy function.
        self.assertIn(str(home / 'memories/MEMORY.md'), cloned)
        self.assertEqual((target / 'bots' / self.bots[0]['id'] / 'home/memories/MEMORY.md').read_text(), 'Remember ' + self.bots[0]['id'])

    @unittest.skipUnless(sys.platform == 'darwin' and migration.CLONEFILE is not None, 'clonefile is a macOS call')
    def test_a_clone_on_macos_has_the_content_and_the_mode(self):
        source = self.root / 'clone-source'
        source.write_text('content')
        source.chmod(0o640)
        destination = self.root / 'clone-destination'
        with patch.object(migration.shutil, 'copy2', side_effect=AssertionError('The clone fell back to a copy.')):
            migration.clone_or_copy(source, destination)
        self.assertEqual((destination.read_text(), destination.stat().st_mode & 0o777), ('content', 0o640))

    def test_too_little_free_space_refuses_before_the_service_stops(self):
        usage = migration.shutil.disk_usage(self.root)
        small = usage._replace(free=migration.BACKUP_MARGIN // 2)
        with patch.object(migration.shutil, 'disk_usage', return_value=small), self.assertRaises(SystemExit) as raised:
            self.run_quietly(migration.migrate, self.root, self.hermes)
        self.assertIn('Free some space', str(raised.exception.code))
        self.assertIn('Nothing changed', str(raised.exception.code))
        self.assertEqual(self.steps(), [])
        self.assertEqual(sorted(path.name for path in self.root.parent.iterdir()), ['host'])
        for bot in self.bots:
            self.assertEqual(tree(self.home(bot)), self.before[bot['id']])

    def test_the_space_estimate_counts_full_copies_without_clones(self):
        with patch.object(migration, 'clones_work', return_value=False), \
                patch.object(migration, 'backup_size', return_value=10 * 1024 ** 3), \
                patch.object(migration.shutil, 'disk_usage', return_value=migration.shutil.disk_usage(self.root)._replace(free=9 * 1024 ** 3)):
            with self.assertRaisesRegex(SystemExit, r'needs about 10\.5 GB.*only 9\.0 GB is free'):
                migration.check_backup_space(self.root)
        with patch.object(migration, 'clones_work', return_value=True), \
                patch.object(migration, 'backup_size', return_value=10 * 1024 ** 3), \
                patch.object(migration.shutil, 'disk_usage', return_value=migration.shutil.disk_usage(self.root)._replace(free=9 * 1024 ** 3)):
            migration.check_backup_space(self.root)

    def test_a_failed_backup_in_the_same_second_keeps_an_earlier_backup(self):
        moment = migration.datetime(2026, 10, 27, 12, 0, 0, tzinfo=migration.timezone.utc)
        clock = SimpleNamespace(now=lambda zone=None: moment)
        # A folder with the name of this second exists already, for example an earlier backup.
        earlier = self.root.parent / 'host-backup-before-profiles-20261027T120000Z'
        earlier.mkdir()
        (earlier / 'coconut.sqlite3').write_text('earlier backup')
        with patch.object(migration, 'datetime', clock):
            first = migration.backup(self.root)
            with patch.object(migration, 'clone_or_copy', side_effect=OSError(errno.ENOSPC, 'No space left on device')), \
                    self.assertRaises(OSError):
                migration.backup(self.root)
        self.assertEqual((earlier / 'coconut.sqlite3').read_text(), 'earlier backup')
        self.assertTrue(first.name.startswith('host-backup-before-profiles-20261027T120000Z-'))
        self.assertTrue((first / 'coconut.sqlite3').is_file())
        # Only the folder of the failed attempt is gone.
        self.assertEqual(sorted(path.name for path in self.root.parent.iterdir()), sorted(['host', earlier.name, first.name]))

    def test_a_failed_backup_removes_its_partial_copy_and_starts_coconut(self):
        copies = []
        def copy(source, destination):
            copies.append(source)
            if len(copies) == 3:
                raise OSError(errno.ENOSPC, 'No space left on device')
            return migration.shutil.copy2(source, destination)
        with patch.object(migration, 'clone_or_copy', side_effect=copy), self.assertRaises(OSError):
            self.run_quietly(migration.migrate, self.root, self.hermes)
        self.assertEqual(self.steps(), ['drain_for_update', 'stop_for_update', 'restore_after_update'])
        self.assertEqual(sorted(path.name for path in self.root.parent.iterdir()), ['host'])
        self.assertFalse((self.root / migration.LEDGER).exists())

    def test_a_copy_that_fails_after_its_destination_exists_keeps_coconut_stopped(self):
        def rename(source, destination):
            if 'bots' in str(source):
                raise OSError(errno.EXDEV, 'Cross-device link')
            os.rename(source, destination)

        with patch.object(migration.shutil, 'rmtree', side_effect=OSError(errno.EBUSY, 'Resource busy')), \
                self.assertRaises(SystemExit) as raised:
            self.run_quietly(migration.migrate, self.root, self.hermes, rename=rename)
        self.assertEqual(self.steps(), ['drain_for_update', 'stop_for_update'])
        self.assertIn('Coconut stays stopped', raised.exception.output)
        ledger = json.loads((self.root / migration.LEDGER).read_text())
        self.assertEqual(ledger['moves'][0]['state'], 'moving')
        self.assertTrue(Path(ledger['moves'][0]['destination']).is_dir())

    def test_running_workers_stop_the_migration_before_any_change(self):
        with patch.object(migration, 'coconut_workers', return_value=[123]):
            with self.assertRaises(SystemExit):
                self.run_quietly(migration.migrate, self.root, self.hermes)
        self.assertEqual(self.steps(), ['drain_for_update', 'stop_for_update', 'restore_after_update'])
        for bot in self.bots:
            self.assertTrue(self.home(bot).is_dir())

    def test_a_running_service_stops_the_migration_before_any_change(self):
        import fcntl
        with open(self.root / 'service.lock', 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaises(SystemExit):
                self.run_quietly(migration.migrate, self.root, self.hermes, manage_service=False)
        for bot in self.bots:
            self.assertTrue(self.home(bot).is_dir())

    def test_the_hermes_home_must_be_the_one_of_the_service(self):
        hermes_runtime.write_status(self.root, {'status': 'ok', 'home': str(migration.host.ROOT / '.runtime/hermes/terminal/hermes')})
        problem = migration.home_problem(self.root, self.hermes)
        self.assertIn('ServiceEnvironment -dict-add HERMES_HOME', problem)
        with self.assertRaises(SystemExit) as raised:
            self.run_quietly(migration.main, ['--data-dir', str(self.root), '--hermes-home', str(self.hermes), '--no-service'])
        self.assertIn('Nothing changed', str(raised.exception.code))
        self.assertTrue(all(self.home(bot).is_dir() for bot in self.bots))
        _, output = self.run_quietly(migration.main, ['--data-dir', str(self.root), '--hermes-home', str(self.hermes), '--dry-run'])
        self.assertIn('Before the migration', output)
        # Without --hermes-home, the script takes the home of the service, which is a folder of Coconut here.
        self.assertIn('is a folder of Coconut', migration.home_problem(self.root, migration.host.ROOT / '.runtime/hermes/terminal/hermes'))
        hermes_runtime.write_status(self.root, {'status': 'ok', 'home': str(self.hermes)})
        self.assertIsNone(migration.home_problem(self.root, self.hermes))


if __name__ == '__main__':
    unittest.main()
