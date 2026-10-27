"""Bots are the Hermes profiles of the person: rescans, creation, removal, and profile deletion."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut import profile_registry
from coconut.profiles import HermesProfiles, ProfileError, bot_folder
from coconut.service import Service, ServiceError
from test_service import ControlledProvider


def listing(folder):
    return sorted(str(path.relative_to(folder)) for path in Path(folder).rglob('*'))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.hermes = Path(temporary.name) / '.hermes'
        (self.hermes / 'profiles').mkdir(parents=True)
        (self.hermes / 'config.yaml').write_text('model: {}\n')

    def profile(self, name, marker='SOUL.md'):
        folder = self.hermes / 'profiles' / name
        folder.mkdir()
        (folder / marker).write_text('Hello.\n')
        return folder

    def test_scan_lists_default_and_live_profiles_only(self):
        self.profile('work')
        (self.hermes / 'profiles' / 'empty').mkdir()  # no profile file: Hermes does not list it
        self.profile('Bad Name')
        self.profile('gone')
        (self.hermes / 'profiles' / '.deleted').mkdir()
        (self.hermes / 'profiles' / '.deleted' / 'gone').write_text('deleted\n')
        self.assertEqual([found.name for found in profile_registry.scan(self.hermes)], ['default', 'work'])

    def test_plan_keeps_a_renamed_folder_and_reattaches_a_returned_one(self):
        work = self.profile('work')
        known = {}
        for change, row in profile_registry.plan(known, profile_registry.scan(self.hermes)):
            self.assertEqual(change, 'new')
            known[row['name']] = dict(row, bot_id=row['name'] + '-bot', seen_at='')
        work.rename(self.hermes / 'profiles' / 'office')
        changes = profile_registry.plan(known, profile_registry.scan(self.hermes))
        self.assertEqual([(change, row['bot_id'], row['name']) for change, row in changes], [('moved', 'work-bot', 'office')])
        known['work'] = changes[0][1]
        moved = self.hermes / 'profiles' / 'office'
        stash = Path(self.hermes.parent / 'stash')
        moved.rename(stash)
        changes = profile_registry.plan(known, profile_registry.scan(self.hermes))
        self.assertEqual([(change, row['bot_id'], row['state']) for change, row in changes], [('missing', 'work-bot', 'missing')])
        known['work'] = changes[0][1]
        # The folder returns from a copy, with a new inode, under its earlier name.
        shutil.copytree(stash, self.hermes / 'profiles' / 'office')
        changes = profile_registry.plan(known, profile_registry.scan(self.hermes))
        self.assertEqual([(change, row['bot_id'], row['state']) for change, row in changes], [('moved', 'work-bot', 'active')])

    def test_a_removed_profile_stays_removed(self):
        self.profile('work')
        found = profile_registry.scan(self.hermes)
        known = {row['name']: dict(row, bot_id=row['name'], seen_at='', state='removed' if row['name'] == 'work' else 'active')
                 for _, row in profile_registry.plan({}, found)}
        self.assertEqual(profile_registry.plan(known, found), [])
        shutil.rmtree(self.hermes / 'profiles' / 'work')
        self.assertEqual(profile_registry.plan(known, profile_registry.scan(self.hermes)), [])

    def test_slugs_are_unique_and_valid(self):
        taken = {'ada', 'ada-2', 'Bot'}
        self.assertEqual(profile_registry.slug('Ada', taken), 'ada-3')
        self.assertEqual(profile_registry.slug('Research Lead!', taken), 'research-lead')
        self.assertEqual(profile_registry.slug('default', set()), 'default-bot')
        self.assertEqual(profile_registry.slug('Hermes', set()), 'hermes-bot')
        self.assertEqual(profile_registry.slug('🙂', taken), 'bot-2')
        long = profile_registry.slug('x' * 90, set())
        self.assertTrue(profile_registry.PROFILE_ID.match(long))

    def test_display_name_reads_yaml_and_json(self):
        work = self.profile('work')
        (work / 'profile.yaml').write_text('description: Research\ndisplay_name: "Lin"\n')
        self.assertEqual(profile_registry.display_name(work), 'Lin')
        (work / 'profile.yaml').write_text('{\n  "display_name": "Ada",\n  "description": ""\n}\n')
        self.assertEqual(profile_registry.display_name(work), 'Ada')


class ServiceProfileRescanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'data'
        self.hermes = self.base / '.hermes'
        (self.hermes / 'profiles').mkdir(parents=True)
        (self.hermes / 'config.yaml').write_text('model: {}\n')
        self.work = self.hermes / 'profiles' / 'work'
        self.work.mkdir()
        (self.work / 'SOUL.md').write_text('Work soul.\n')
        (self.work / 'profile.yaml').write_text('display_name: Lin\ndescription: Research helper\n')
        self.provider = ControlledProvider()
        self.service = None
        self.open()

    def tearDown(self):
        self.service.close()

    def open(self):
        if self.service is not None:
            self.service.close()
        self.service = Service(self.root, self.provider, start_workers=False, hermes_home=self.hermes)

    def bots(self):
        return {bot['profile']: bot for bot in self.service.snapshot()['bots']}

    def write(self, method, path, data=None):
        return self.service.mutate(method, path, data or {}, str(uuid4()), 'phone')

    def test_every_profile_becomes_a_bot_without_writing_into_profiles(self):
        before = listing(self.hermes)
        bots = self.bots()
        self.assertEqual(set(bots), {'default', 'work'})
        self.assertEqual(bots['default']['name'], 'Hermes')
        self.assertEqual(bots['work']['name'], 'Lin')
        self.assertEqual(bots['work']['role'], 'Research helper')
        self.assertEqual(self.service.profiles.home(bots['work']), self.work)
        self.assertEqual(self.service.profiles.home(bots['default']), self.hermes)
        self.assertEqual(self.service.profiles.read_instructions(bots['work']), 'Work soul.\n')
        self.assertEqual(listing(self.hermes), before)
        # A start again finds the same bots.
        self.open()
        self.assertEqual({bot['id'] for bot in self.bots().values()}, {bot['id'] for bot in bots.values()})

    def test_rename_missing_and_return_keep_the_bot(self):
        bot = self.bots()['work']
        self.work.rename(self.hermes / 'profiles' / 'office')
        self.service.rescan_profiles()
        renamed = self.bots()['office']
        self.assertEqual(renamed['id'], bot['id'])
        self.assertEqual(self.service.profiles.home(renamed), self.hermes / 'profiles' / 'office')
        (self.hermes / 'profiles' / 'office').rename(self.base / 'away')
        self.service.rescan_profiles()
        self.assertEqual(self.service._get('bots', bot['id'])['profile_state'], 'missing')
        self.assertEqual(self.service.profiles.sync(renamed)['status'], 'missing')
        (self.base / 'away').rename(self.hermes / 'profiles' / 'office')
        self.service.rescan_profiles()
        self.assertEqual(self.service._get('bots', bot['id'])['profile_state'], 'active')
        self.assertEqual(len(self.bots()), 2)

    def test_a_new_profile_folder_becomes_a_bot(self):
        folder = self.hermes / 'profiles' / 'ops'
        folder.mkdir()
        (folder / 'config.yaml').write_text('{}\n')
        self.service.rescan_profiles()
        self.assertEqual(self.bots()['ops']['name'], 'ops')

    def test_create_uses_the_hermes_profile_api(self):
        calls = []

        def command(operation, **payload):
            calls.append((operation, payload))
            home = self.hermes / 'profiles' / payload['name']
            home.mkdir()
            (home / 'SOUL.md').write_text(payload['soul'])
            return {'home': str(home)}

        with patch.object(self.service, '_hermes_profile_command', side_effect=command):
            bot = self.write('POST', '/v1/bots', {'name': 'Lin', 'role': 'Writer', 'instructions': 'Write well.'})
        # `work` holds no name `lin`, so the slug is free.
        self.assertEqual(calls[0][0], 'create')
        self.assertEqual(calls[0][1]['name'], 'lin')
        self.assertEqual(calls[0][1]['display_name'], 'Lin')
        # The new profile loads Coconut's own skills, as setup gives them to the person's profiles.
        from coconut.profiles import SKILLS
        self.assertEqual(calls[0][1]['skills'], str(SKILLS))
        self.assertEqual(bot['profile'], 'lin')
        self.assertEqual(bot['profile_id'], bot['id'])
        self.assertEqual(profile_registry.lookup(self.root, bot['id'])['home'], str(self.hermes / 'profiles' / 'lin'))
        self.service.rescan_profiles()
        self.assertEqual(len(self.bots()), 3)
        self.write('PATCH', f'/v1/bots/{bot["id"]}', {'instructions': 'Write better.'})
        self.assertEqual((self.hermes / 'profiles' / 'lin' / 'SOUL.md').read_text(), 'Write better.')

    def test_a_failed_profile_creation_creates_no_bot(self):
        with patch.object(self.service, '_hermes_profile_command', side_effect=ProfileError('Hermes refused it.')):
            with self.assertRaises(ServiceError) as raised:
                self.write('POST', '/v1/bots', {'name': 'Lin'})
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(len(self.bots()), 2)

    def test_remove_from_coconut_keeps_the_folder_and_does_not_return(self):
        bot = self.bots()['work']
        self.write('DELETE', f'/v1/bots/{bot["id"]}')
        self.assertTrue(self.work.is_dir())
        self.assertEqual(profile_registry.lookup(self.root, bot['id'])['state'], 'removed')
        self.service.rescan_profiles()
        self.open()
        self.assertEqual(set(self.bots()), {'default'})

    def test_deleting_a_hermes_profile_needs_the_typed_name(self):
        bots = self.bots()
        with self.assertRaises(ServiceError) as raised:
            self.write('POST', f'/v1/bots/{bots["default"]["id"]}/profile/delete', {'confirm': 'default'})
        self.assertEqual(raised.exception.status, 409)
        with self.assertRaises(ServiceError) as raised:
            self.write('POST', f'/v1/bots/{bots["work"]["id"]}/profile/delete', {'confirm': 'Work'})
        self.assertEqual(raised.exception.status, 400)
        self.assertTrue(self.work.is_dir())
        calls = []

        def command(operation, **payload):
            calls.append((operation, payload))
            shutil.rmtree(self.hermes / 'profiles' / payload['name'])
            return {'home': str(self.work)}

        with patch.object(self.service, '_hermes_profile_command', side_effect=command):
            result = self.write('POST', f'/v1/bots/{bots["work"]["id"]}/profile/delete', {'confirm': 'work'})
        self.assertEqual(result, {'ok': True, 'profile': 'work'})
        self.assertEqual(calls, [('delete', {'name': 'work'})])
        self.assertTrue(self.service._get('bots', bots['work']['id'])['_deleted'])
        self.assertFalse(self.work.exists())
        self.assertTrue(self.hermes.is_dir())

    def test_a_bot_can_only_remove_another_bot_from_coconut(self):
        from coconut.hermes_worker import BOT_DELETION_SCHEMA
        self.assertEqual(set(BOT_DELETION_SCHEMA['parameters']['properties']),
                         {'source_message_id', 'target_bot_id', 'expected_name'})
        self.assertIn('Hermes profile folder stays', BOT_DELETION_SCHEMA['description'])


class LegacyLayoutTests(unittest.TestCase):
    def test_bots_in_earlier_homes_wait_for_the_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, hermes = Path(temporary) / 'data', Path(temporary) / '.hermes'
            (hermes / 'profiles' / 'work').mkdir(parents=True)
            (hermes / 'profiles' / 'work' / 'SOUL.md').write_text('Soul.\n')
            service = Service(root, ControlledProvider(), start_workers=False)
            bot = service.mutate('POST', '/v1/bots', {'name': 'Ada'}, str(uuid4()), 'phone')
            service.close()
            service = Service(root, ControlledProvider(), start_workers=False, hermes_home=hermes)
            try:
                self.assertFalse(service.profiles_ready)
                self.assertEqual([b['id'] for b in service.snapshot()['bots']], [bot['id']])
                self.assertEqual(service.profiles.home(bot), root.resolve() / 'bots' / bot['id'] / 'home')
                created = service.mutate('POST', '/v1/bots', {'name': 'Lin'}, str(uuid4()), 'phone')
                self.assertTrue((root / 'bots' / created['id'] / 'home' / 'SOUL.md').is_file())
            finally:
                service.close()


class WorkerEnvironmentTests(unittest.TestCase):
    def test_a_worker_runs_in_the_profile_and_keeps_coconut_files_in_the_data_directory(self):
        from coconut.providers import HermesProvider
        with tempfile.TemporaryDirectory() as temporary:
            root, hermes = Path(temporary) / 'data', Path(temporary) / '.hermes'
            home = hermes / 'profiles' / 'work'
            home.mkdir(parents=True)
            bot_id = str(uuid4())
            catalog = Mock()
            provider = HermesProvider(root, catalog=catalog)
            provider.profile_rows = {bot_id: dict(bot_id=bot_id, name='work', home=str(home), dev=0, ino=0, state='active')}
            env, workspace = provider.environment(bot_id)
            self.assertEqual(env['HERMES_HOME'], str(home))
            self.assertEqual(env['COCONUT_DATA_DIR'], str(root.resolve()))
            self.assertEqual(env['COCONUT_BOT_ID'], bot_id)
            self.assertEqual(env['COCONUT_BOT_DIR'], str(root.resolve() / 'bots' / bot_id))
            self.assertEqual(workspace, root.resolve() / 'bots' / bot_id / 'workspace')
            self.assertFalse((root / 'bots' / bot_id / 'home').exists())
            self.assertEqual(listing(home), [])
            shutil.rmtree(home)
            with self.assertRaisesRegex(RuntimeError, 'profile of this bot is missing'):
                provider.environment(bot_id)

    def test_bot_folder_finds_the_coconut_folder_of_a_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'data'
            legacy = root / 'bots' / str(uuid4()) / 'home'
            self.assertEqual(bot_folder(legacy), legacy.parent)
            service = Service(root, ControlledProvider(), start_workers=False)
            bot_id = str(uuid4())
            with service.transaction():
                profile_registry.write(service.db, dict(bot_id=bot_id, name='work', home=str(Path(temporary) / 'work'),
                                                        dev=1, ino=2, state='active'))
            service.close()
            with patch.dict(os.environ, {'COCONUT_DATA_DIR': str(root)}):
                self.assertEqual(bot_folder(Path(temporary) / 'work'), root / 'bots' / bot_id)
                with self.assertRaises(ProfileError):
                    bot_folder(Path(temporary) / 'other')
            with patch.dict(os.environ, {'HERMES_HOME': str(Path(temporary) / 'work'), 'COCONUT_BOT_DIR': '/coconut/bot'}):
                self.assertEqual(bot_folder(Path(temporary) / 'work'), Path('/coconut/bot'))
            self.assertEqual(HermesProfiles(root).home({'id': bot_id}), Path(temporary) / 'work')


class ReviewFindingTests(ServiceProfileRescanTests):
    """Checks for the review of the first version (PR #274)."""

    def test_a_renamed_profile_replaces_a_cached_kanban_worker(self):
        from coconut.providers import HermesProvider
        provider = HermesProvider(self.root, catalog=Mock())
        provider.profile_rows = self.service.profiles.rows
        bot = self.bots()['work']
        started, stopped = [], []

        class Process:
            def __init__(self, *args, env=None, **kwargs):
                self.pid, self.env = 1000 + len(started), env
                started.append(self)

            def poll(self):
                return None

        with patch('coconut.providers.subprocess.Popen', Process), \
                patch.object(provider, '_terminate', side_effect=stopped.append):
            provider.prepare_kanban_worker('claim-1', bot, str(self.base), 'execution', worker_id='worker')
            provider.release_kanban_preparation('claim-1')
            provider.prepare_kanban_worker('claim-2', bot, str(self.base), 'execution', worker_id='worker')
            self.assertEqual(len(started), 1)
            provider.release_kanban_preparation('claim-2')
            self.work.rename(self.hermes / 'profiles' / 'office')
            self.service.rescan_profiles()
            provider.prepare_kanban_worker('claim-3', bot, str(self.base), 'execution', worker_id='worker')
        self.assertEqual(len(started), 2)
        self.assertEqual(started[1].env['HERMES_HOME'], str(self.hermes / 'profiles' / 'office'))
        self.assertEqual(stopped, [started[0]])

    def test_a_retry_never_deletes_a_replacement_profile(self):
        bot = self.bots()['work']
        key = str(uuid4())
        calls = []

        def fails(operation, **payload):
            calls.append(payload['name'])
            raise ProfileError('Hermes is busy.')

        with patch.object(self.service, '_hermes_profile_command', side_effect=fails):
            with self.assertRaises(ServiceError):
                self.service.mutate('POST', f'/v1/bots/{bot["id"]}/profile/delete', {'confirm': 'work'}, key, 'phone')
        # The person deletes the folder by hand and creates another profile with the same name. The old folder goes
        # away last, so the new one cannot reuse its inode.
        self.work.rename(self.base / 'old-work')
        self.work.mkdir()
        shutil.rmtree(self.base / 'old-work')
        (self.work / 'SOUL.md').write_text('A new profile.\n')
        with patch.object(self.service, '_hermes_profile_command', side_effect=fails):
            self.service.mutate('POST', f'/v1/bots/{bot["id"]}/profile/delete', {'confirm': 'work'}, key, 'phone')
        self.assertEqual(calls, ['work'])
        self.assertEqual((self.work / 'SOUL.md').read_text(), 'A new profile.\n')
        # The new folder is a new bot, not the removed one.
        self.service.rescan_profiles()
        replacement = self.bots()['work']
        self.assertNotEqual(replacement['id'], bot['id'])

    def test_a_finished_deletion_never_runs_again(self):
        bot = self.bots()['work']
        key = str(uuid4())
        calls = []

        def deletes(operation, **payload):
            calls.append(payload['name'])
            shutil.rmtree(self.hermes / 'profiles' / payload['name'])
            return {'home': str(self.work)}

        with patch.object(self.service, '_hermes_profile_command', side_effect=deletes):
            self.service.mutate('POST', f'/v1/bots/{bot["id"]}/profile/delete', {'confirm': 'work'}, key, 'phone')
            self.work.mkdir()
            (self.work / 'SOUL.md').write_text('A new profile.\n')
            self.service.mutate('POST', f'/v1/bots/{bot["id"]}/profile/delete', {'confirm': 'work'}, key, 'phone')
        self.assertEqual(calls, ['work'])
        self.assertTrue(self.work.is_dir())

    def test_workspace_recovery_reads_the_profile_homes(self):
        from coconut.workspace_recovery import lifecycle_homes
        homes = set(lifecycle_homes(self.service.root))
        self.assertIn(self.work, homes)
        self.assertIn(self.hermes, homes)

    def test_a_folder_that_gets_a_profile_file_changes_the_signature(self):
        folder = self.hermes / 'profiles' / 'later'
        folder.mkdir()
        before = profile_registry.signature(self.hermes)
        (folder / 'SOUL.md').write_text('Now a profile.\n')
        self.assertNotEqual(profile_registry.signature(self.hermes), before)


class ProfileCommandTests(unittest.TestCase):
    """PROFILE_COMMAND runs against a small stand-in for hermes_cli.profiles, so the command itself is checked here.

    The Hermes contract check (verify_profile_api_contract) runs the same command against the installed Hermes.
    """

    def test_create_and_delete_go_through_the_hermes_profile_api(self):
        from coconut.bot_profiles import run_profile_command
        with tempfile.TemporaryDirectory() as temporary:
            source, hermes = Path(temporary) / 'source', Path(temporary) / '.hermes'
            (source / 'hermes_cli').mkdir(parents=True)
            (source / 'hermes_cli' / '__init__.py').write_text('')
            (source / 'hermes_cli' / 'profiles.py').write_text(
                'import os\n'
                'from pathlib import Path\n'
                'def _root():\n'
                '    return Path(os.environ["HERMES_HOME"]) / "profiles"\n'
                'def create_profile(name, description=None, soul=None):\n'
                '    print("Hermes prints to stdout.")\n'
                '    home = _root() / name\n'
                '    home.mkdir(parents=True)\n'
                '    (home / "SOUL.md").write_text(soul)\n'
                '    (home / "description").write_text(description or "")\n'
                '    return home\n'
                'def seed_profile_skills(home, quiet=False):\n'
                '    (home / "skills").mkdir()\n'
                'def check_alias_collision(name):\n'
                '    return "taken"\n'
                'def write_profile_meta(home, display_name=None):\n'
                '    (home / "profile.yaml").write_text("display_name: " + display_name + "\\n")\n'
                'def delete_profile(name, yes=False):\n'
                '    import shutil\n'
                '    assert yes\n'
                '    shutil.rmtree(_root() / name)\n'
                '    return _root() / name\n')
            # Stand-ins for the config reader and writer of Hermes, which skill_dirs uses through native_settings.
            (source / 'native_settings.py').write_text(
                'import json\n'
                'from pathlib import Path\n'
                'def raw_config(home):\n'
                '    path = Path(home) / "config.yaml"\n'
                '    return json.loads(path.read_text()) if path.exists() else {}\n'
                'def save_config(home, config):\n'
                '    (Path(home) / "config.yaml").write_text(json.dumps(config))\n')
            hermes.mkdir()
            import json
            import sys
            from coconut.profiles import SKILLS
            coconut = Path(__file__).resolve().parents[1] / 'coconut'
            created = run_profile_command(sys.executable, source, hermes, 'create', name='lin', display_name='Lin',
                                          description='Writer', soul='Write.\n', skills=str(SKILLS), coconut=str(coconut))
            home = hermes / 'profiles' / 'lin'
            self.assertEqual(Path(created['home']), home)
            self.assertEqual(created['skills'], 'added')
            self.assertEqual(json.loads((home / 'config.yaml').read_text()), {'skills': {'external_dirs': [str(SKILLS)]}})
            self.assertEqual((home / 'SOUL.md').read_text(), 'Write.\n')
            self.assertTrue((home / 'skills').is_dir())
            self.assertEqual(profile_registry.display_name(home), 'Lin')
            run_profile_command(sys.executable, source, hermes, 'delete', name='lin')
            self.assertFalse(home.exists())
            with self.assertRaisesRegex(ProfileError, 'refused'):
                run_profile_command(sys.executable, source, hermes, 'delete', name='lin')


if __name__ == '__main__':
    unittest.main()
