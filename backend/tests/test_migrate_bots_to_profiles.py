"""scripts/migrate_bots_to_profiles.py moves the earlier bot homes into Hermes profiles, and back."""
import contextlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut import profile_registry
from coconut.service import Service
from test_service import ControlledProvider

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('migrate_bots_to_profiles', SCRIPTS / 'migrate_bots_to_profiles.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def tree(folder):
    """Every file of a folder with its content."""
    return {str(path.relative_to(folder)): path.read_bytes() for path in sorted(Path(folder).rglob('*'))
            if path.is_file() and not path.name.startswith('coconut.sqlite3-')}


ADD_SKILLS_FOLDER = migration.add_skills_folder


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.root = base / 'Coconut' / '.runtime' / 'host'
        self.hermes = base / '.hermes'
        (self.hermes / 'profiles' / 'ada').mkdir(parents=True)  # The person already has a profile named ada.
        (self.hermes / 'profiles' / 'ada' / 'SOUL.md').write_text('The person\'s own Ada.\n')
        (self.hermes / 'config.yaml').write_text('model: {}\n')
        service = Service(self.root, ControlledProvider(), start_workers=False)
        self.bots = [service.mutate('POST', '/v1/bots', {'name': name, 'instructions': name + ' soul'}, str(uuid4()), 'phone')
                     for name in ('Ada', 'Ada', 'Research Lead')]
        deleted = service.mutate('POST', '/v1/bots', {'name': 'Old'}, str(uuid4()), 'phone')
        service.mutate('DELETE', f'/v1/bots/{deleted["id"]}', {}, str(uuid4()), 'phone')
        service.close()
        self.deleted = deleted
        for bot in self.bots:
            home = self.home(bot)
            (home / 'sessions').mkdir()
            (home / 'sessions' / 'chat.json').write_text(json.dumps({'bot': bot['id']}))
            (home / 'memories').mkdir()
            (home / 'memories' / 'MEMORY.md').write_text('Remember ' + bot['id'])
            (home / 'cron').mkdir()
            (home / 'cron' / 'jobs.json').write_text('{"jobs": []}')
            (home / 'skills' / 'mine').mkdir(parents=True)
            (home / 'skills' / 'mine' / 'SKILL.md').write_text('A skill.')
            os.symlink('SKILL.md', home / 'skills' / 'mine' / 'link.md')
            (self.root / 'bots' / bot['id'] / 'workspace' / 'repo').mkdir(parents=True)
        self.before = {bot['id']: tree(self.home(bot)) for bot in self.bots}
        patcher = patch.object(migration, 'coconut_workers', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        # The Hermes config writer is not on this test Python. Record which profiles get the skills folder.
        self.skill_homes = []
        patcher = patch.object(migration, 'add_skills_folder', side_effect=self.skill_homes.extend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def home(self, bot):
        return self.root / 'bots' / bot['id'] / 'home'

    def run_quietly(self, function, *args, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = function(*args, **kwargs)
        return result, output.getvalue()

    def test_dry_run_shows_unique_names_and_changes_nothing(self):
        before = tree(self.root)
        _, output = self.run_quietly(migration.main, ['--data-dir', str(self.root), '--hermes-home', str(self.hermes),
                                                      '--dry-run', '--no-service'])
        self.assertEqual(tree(self.root), before)
        self.assertIn('ada-2', output)
        self.assertIn('ada-3', output)
        self.assertIn('research-lead', output)
        self.assertNotIn(self.deleted['id'], output)
        self.assertIn('Nothing changed', output)
        self.assertEqual(sorted(path.name for path in (self.hermes / 'profiles').iterdir()), ['ada'])

    def test_migration_moves_homes_and_keeps_bot_ids(self):
        ledger, output = self.run_quietly(migration.migrate, self.root, self.hermes, manage_service=False)
        names = {move['bot_id']: move['name'] for move in ledger['moves']}
        self.assertEqual(sorted(names.values()), ['ada-2', 'ada-3', 'research-lead'])
        for bot in self.bots:
            destination = self.hermes / 'profiles' / names[bot['id']]
            self.assertFalse(self.home(bot).exists())
            self.assertEqual(tree(destination), self.before[bot['id']])
            self.assertTrue((destination / 'skills' / 'mine' / 'link.md').is_symlink())
            row = profile_registry.lookup(self.root, bot['id'])
            self.assertEqual((row['home'], row['state']), (str(destination), 'active'))
            self.assertEqual(row['ino'], destination.stat().st_ino)
            # Workspaces stay in the data directory.
            self.assertTrue((self.root / 'bots' / bot['id'] / 'workspace' / 'repo').is_dir())
        self.assertEqual((self.hermes / 'profiles' / 'ada' / 'SOUL.md').read_text(), 'The person\'s own Ada.\n')
        backup = Path(ledger['backup'])
        self.assertTrue((backup / 'coconut.sqlite3').is_file())
        self.assertTrue((backup / 'bots' / self.bots[0]['id'] / 'home' / 'memories' / 'MEMORY.md').is_file())
        self.assertFalse((backup / 'bots' / self.bots[0]['id'] / 'workspace').exists())
        self.assertTrue((self.root / migration.LEDGER).is_file())
        self.assertIn('hermes -p research-lead', output)
        # Each moved profile keeps or gets the folder of Coconut's skills.
        self.assertEqual(sorted(self.skill_homes), sorted(Path(move['destination']) for move in ledger['moves']))
        # The service then runs each bot in its profile, and the default profile and the person's ada become bots.
        service = Service(self.root, ControlledProvider(), start_workers=False, hermes_home=self.hermes)
        try:
            self.assertTrue(service.profiles_ready)
            bots = {bot['id']: bot for bot in service.snapshot()['bots']}
            for bot in self.bots:
                self.assertEqual(bots[bot['id']]['session_id'], bot['session_id'])
                self.assertEqual(bots[bot['id']]['profile'], names[bot['id']])
                self.assertEqual(service.profiles.home(bot), self.hermes / 'profiles' / names[bot['id']])
            self.assertEqual(sorted(bot.get('profile') for bot in bots.values()),
                             ['ada', 'ada-2', 'ada-3', 'default', 'research-lead'])
        finally:
            service.close()
        with self.assertRaises(SystemExit):
            self.run_quietly(migration.migrate, self.root, self.hermes, manage_service=False)

    def test_another_disk_copies_checks_and_removes(self):
        calls = []

        def rename(source, destination):
            calls.append(source)
            if 'bots' in str(source):
                raise OSError(errno.EXDEV, 'Cross-device link')
            os.rename(source, destination)

        ledger, _ = self.run_quietly(migration.migrate, self.root, self.hermes, manage_service=False, rename=rename)
        self.assertEqual({move['method'] for move in ledger['moves']}, {'copy'})
        for move in ledger['moves']:
            self.assertFalse(Path(move['source']).exists())
            self.assertEqual(tree(move['destination']), self.before[move['bot_id']])
        self.assertFalse(any(path.name.endswith('.coconut-copy') for path in (self.hermes / 'profiles').iterdir()))

    def test_a_copy_that_does_not_match_keeps_the_original(self):
        source = self.home(self.bots[0])
        destination = self.hermes / 'profiles' / 'copy-check'
        original = migration.manifest
        with patch.object(migration, 'manifest', side_effect=lambda root: original(root) if root == source else {}):
            with self.assertRaises(SystemExit):
                migration.move_tree(source, destination, rename=lambda *_: (_ for _ in ()).throw(OSError(errno.EXDEV, 'x')))
        self.assertEqual(tree(source), self.before[self.bots[0]['id']])
        self.assertFalse(destination.exists())
        self.assertEqual(sorted(path.name for path in (self.hermes / 'profiles').iterdir()), ['ada'])

    def test_rollback_restores_the_earlier_homes(self):
        ledger, _ = self.run_quietly(migration.migrate, self.root, self.hermes, manage_service=False)
        # The person renames one profile after the migration. The service records the new folder.
        renamed = ledger['moves'][0]
        Path(renamed['destination']).rename(self.hermes / 'profiles' / 'renamed')
        service = Service(self.root, ControlledProvider(), start_workers=False, hermes_home=self.hermes)
        service.close()
        self.run_quietly(migration.rollback, self.root, manage_service=False)
        for bot in self.bots:
            self.assertEqual(tree(self.home(bot)), self.before[bot['id']])
            self.assertIsNone(profile_registry.lookup(self.root, bot['id']))
        self.assertEqual(sorted(path.name for path in (self.hermes / 'profiles').iterdir()), ['ada'])
        self.assertFalse((self.root / migration.LEDGER).exists())
        self.assertTrue(any(self.root.glob('profile-migration.rolled-back-*.json')))
        with sqlite3.connect(self.root / 'coconut.sqlite3') as db:
            data = json.loads(db.execute('SELECT data FROM bots WHERE id=?', (self.bots[0]['id'],)).fetchone()[0])
        self.assertNotIn('profile', data)
        # The service keeps the earlier layout again, and the migration can run once more.
        service = Service(self.root, ControlledProvider(), start_workers=False, hermes_home=self.hermes)
        try:
            self.assertFalse(service.profiles_ready)
            self.assertEqual(service.profiles.home(self.bots[0]), self.home(self.bots[0]))
        finally:
            service.close()

    def test_a_running_service_stops_the_migration_before_any_change(self):
        import fcntl
        with open(self.root / 'service.lock', 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaises(SystemExit):
                self.run_quietly(migration.migrate, self.root, self.hermes, manage_service=False)
        for bot in self.bots:
            self.assertTrue(self.home(bot).is_dir())
        self.assertFalse((self.root / migration.LEDGER).exists())

    def service_calls(self):
        """Stand-ins for the host.py steps that stop and start the service."""
        calls = []
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(migration.host, 'drain_for_update', side_effect=lambda: calls.append('drain')))
        stack.enter_context(patch.object(migration.host, 'stop_for_update', side_effect=lambda: calls.append('stop') or {'mode': 'app'}))
        stack.enter_context(patch.object(migration.host, 'restore_after_update', side_effect=lambda state: calls.append('start')))
        self.addCleanup(stack.close)
        return calls

    def test_a_failed_move_puts_every_bot_back_before_the_service_starts(self):
        calls = self.service_calls()
        moved = []

        def rename(source, destination):
            if len(moved) == 2:
                moved.append('failed')
                raise OSError(errno.ENOSPC, 'No space left on device')
            moved.append(source)
            os.rename(source, destination)

        with self.assertRaises(OSError):
            self.run_quietly(migration.migrate, self.root, self.hermes, manage_service=True, rename=rename)
        self.assertEqual(calls, ['drain', 'stop', 'start'])
        for bot in self.bots:
            self.assertEqual(tree(self.home(bot)), self.before[bot['id']])
            self.assertIsNone(profile_registry.lookup(self.root, bot['id']))
        self.assertEqual(sorted(path.name for path in (self.hermes / 'profiles').iterdir()), ['ada'])
        self.assertFalse((self.root / migration.LEDGER).exists())
        self.assertTrue(any(self.root.glob('profile-migration.failed-*.json')))

    def test_a_failed_database_write_also_puts_every_bot_back(self):
        self.service_calls()
        with patch.object(migration, 'record_rows', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_quietly(migration.migrate, self.root, self.hermes, manage_service=True)
        for bot in self.bots:
            self.assertEqual(tree(self.home(bot)), self.before[bot['id']])

    def test_coconut_stays_stopped_when_the_folders_cannot_move_back(self):
        calls = self.service_calls()
        with patch.object(migration, 'record_rows', side_effect=sqlite3.OperationalError('database is locked')), \
                patch.object(migration, 'undo_moves', side_effect=OSError('Disk error')):
            with self.assertRaises(OSError):
                self.run_quietly(migration.migrate, self.root, self.hermes, manage_service=True)
        self.assertEqual(calls, ['drain', 'stop'])
        ledger = json.loads((self.root / migration.LEDGER).read_text())
        self.assertEqual({move['state'] for move in ledger['moves']}, {'moved'})
        # The ledger still lets --rollback finish the job.
        self.run_quietly(migration.rollback, self.root, manage_service=False)
        for bot in self.bots:
            self.assertEqual(tree(self.home(bot)), self.before[bot['id']])

    def test_the_hermes_home_must_be_the_one_of_the_service(self):
        from coconut import hermes_runtime
        hermes_runtime.write_status(self.root, {'status': 'ok', 'home': str(self.root.parent / 'hermes' / 'terminal' / 'hermes')})
        problem = migration.home_problem(self.root, self.hermes)
        self.assertIn('ServiceEnvironment -dict-add HERMES_HOME', problem)
        self.assertIn(str(self.hermes), problem)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            migration.main(['--data-dir', str(self.root), '--hermes-home', str(self.hermes), '--no-service'])
        self.assertIn('Nothing changed', str(raised.exception.code))
        for bot in self.bots:
            self.assertTrue(self.home(bot).is_dir())
        # The dry run reports the same problem.
        _, output = self.run_quietly(migration.main, ['--data-dir', str(self.root), '--hermes-home', str(self.hermes),
                                                      '--dry-run', '--no-service'])
        self.assertIn('Before the migration', output)
        hermes_runtime.write_status(self.root, {'status': 'ok', 'home': str(self.hermes)})
        self.assertIsNone(migration.home_problem(self.root, self.hermes))
        # A folder of Coconut is never the Hermes home of the bots.
        coconut_home = migration.host.ROOT / '.runtime' / 'hermes' / 'terminal' / 'hermes'
        self.assertIn('is a folder of Coconut', migration.home_problem(self.root, coconut_home))

    def test_the_skills_folder_goes_through_the_hermes_config_writer(self):
        from types import SimpleNamespace
        runtime = SimpleNamespace(python='/hermes/python', root='/hermes/source')
        calls = []

        @contextlib.contextmanager
        def isolated(environment):
            yield {'PATH': '/usr/bin'}, '/tmp'

        module = SimpleNamespace(find=lambda environment: runtime, isolated=isolated, HermesUnavailable=RuntimeError)
        homes = [self.hermes / 'profiles' / 'ada-2']
        with patch.object(migration.host, 'hermes_runtime', return_value=module), \
                patch.object(migration.subprocess, 'run', side_effect=lambda command, **kwargs: calls.append(command)
                             or SimpleNamespace(returncode=0, stdout='', stderr='')):
            # setUp replaces add_skills_folder. This calls the real function.
            self.assertTrue(ADD_SKILLS_FOLDER(homes))
        self.assertEqual(calls[0][1:5], ['-I', '-B', str(migration.host.ROOT / 'backend/coconut/skill_dirs.py'), 'add'])
        self.assertEqual(calls[0][5:], ['/hermes/source', str(migration.host.ROOT / 'skills'), str(homes[0])])

    def test_running_workers_stop_the_migration_before_any_change(self):
        with patch.object(migration, 'coconut_workers', return_value=[123]), patch.object(migration.time, 'sleep'):
            with self.assertRaises(SystemExit):
                self.run_quietly(migration.wait_for_workers, self.root, timeout=0)


if __name__ == '__main__':
    unittest.main()
