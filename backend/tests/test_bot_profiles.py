"""Bots are the Hermes profiles of the person, keyed by the profile name."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut import profile_registry
from coconut.bot_profiles import run_profile_command
from coconut.profiles import SKILLS, ProfileError
from coconut.service import Service, ServiceError
from test_service import ControlledProvider


class HermesProvider(ControlledProvider):
    """A provider with a Hermes install, so the service creates profiles through the Hermes profile API."""
    python = sys.executable
    hermes_root = '/hermes/source'

    def __init__(self):
        super().__init__()
        self.catalog = Mock()


class ProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'data'
        self.hermes = self.base / '.hermes'
        (self.hermes / 'profiles').mkdir(parents=True)
        (self.hermes / 'config.yaml').write_text('model: {}\n')
        self.work = self.profile('work', 'display_name: Lin\ndescription: Research helper\n')
        self.provider = HermesProvider()
        self.commands = []
        patcher = patch('coconut.bot_profiles.run_profile_command', side_effect=self.hermes_command)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = None
        self.open()

    def tearDown(self):
        self.service.close()

    def profile(self, name, meta=None):
        folder = self.hermes / 'profiles' / name
        folder.mkdir()
        (folder / 'SOUL.md').write_text(name + ' soul.\n')
        if meta:
            (folder / 'profile.yaml').write_text(meta)
        return folder

    def hermes_command(self, python, source, hermes_home, operation, **payload):
        """Stands in for hermes_cli.profiles.create_profile and delete_profile."""
        self.commands.append((operation, payload['name']))
        home = Path(hermes_home) / 'profiles' / payload['name']
        if operation == 'create':
            home.mkdir()
            (home / 'SOUL.md').write_text(payload['soul'])
        else:
            shutil.rmtree(home)
        return {'home': str(home)}

    def open(self):
        if self.service is not None:
            self.service.close()
        self.service = Service(self.root, self.provider, start_workers=False, hermes_home=self.hermes)

    def bots(self):
        return {bot['profile']: bot for bot in self.service.snapshot()['bots']}

    def write(self, method, path, data=None, key=None):
        return self.service.mutate(method, path, data or {}, key or str(uuid4()), 'phone')

    def test_each_profile_is_a_bot_and_coconut_writes_nothing_into_profiles(self):
        before = sorted(str(path) for path in self.hermes.rglob('*'))
        bots = self.bots()
        self.assertEqual(set(bots), {'default', 'work'})
        self.assertEqual((bots['default']['name'], bots['work']['name'], bots['work']['role']), ('Hermes', 'Lin', 'Research helper'))
        self.assertEqual(self.service.profiles.home(bots['default']), self.hermes)
        self.assertEqual(self.service.profiles.home(bots['work']), self.work)
        self.assertEqual(sorted(str(path) for path in self.hermes.rglob('*')), before)
        self.open()
        self.assertEqual({bot['id'] for bot in self.bots().values()}, {bot['id'] for bot in bots.values()})

    def test_a_missing_folder_keeps_its_bot_and_a_new_name_is_a_new_bot(self):
        bot = self.bots()['work']
        self.work.rename(self.hermes / 'profiles' / 'office')
        self.assertEqual(sorted(self.service.rescan_profiles()), [('missing', 'work'), ('new', 'office')])
        self.assertEqual(self.service._get('bots', bot['id'])['profile_state'], 'missing')
        self.assertNotEqual(self.bots()['office']['id'], bot['id'])
        # Settings writes fail and create no folder. Only Hermes creates profiles.
        with self.assertRaisesRegex(ServiceError, 'missing'):
            self.write('PATCH', f'/v1/bots/{bot["id"]}', {'instructions': 'New.'})
        self.assertFalse(self.work.exists())
        with self.assertRaisesRegex(Exception, 'missing'):
            self.provider_environment(bot)
        self.assertFalse(self.work.exists())
        # A folder with the name again brings the bot back, with its history.
        self.profile('work')
        self.assertEqual(self.service.rescan_profiles(), [('returned', 'work')])
        self.assertEqual(self.service._get('bots', bot['id'])['profile_state'], 'active')

    def test_a_new_hermes_home_moves_the_rows_of_the_same_names(self):
        bots = self.bots()
        old_default = self.service.profiles.home(bots['default'])
        # HERMES_HOME of the service changes to another folder with a profile of the same name, and the service starts again.
        self.hermes = self.base / 'other-hermes'
        (self.hermes / 'profiles').mkdir(parents=True)
        (self.hermes / 'config.yaml').write_text('model: {}\n')
        moved = self.profile('work')
        self.open()
        after = self.bots()
        self.assertEqual({name: bot['id'] for name, bot in after.items()}, {name: bot['id'] for name, bot in bots.items()})
        self.assertNotEqual(old_default, self.hermes)
        self.assertEqual(self.service.profiles.home(after['default']), self.hermes)
        self.assertEqual(self.service.profiles.home(after['work']), moved)
        rows = {row['name']: row for row in profile_registry.rows(self.service.db).values()}
        self.assertEqual((rows['default']['home'], rows['work']['home']), (str(self.hermes), str(moved)))
        self.assertEqual({row['state'] for row in rows.values()}, {'active'})
        # A scan without a change changes nothing.
        self.assertEqual(self.service.rescan_profiles(), [])

    def provider_environment(self, bot):
        from coconut.providers import HermesProvider as Real
        provider = Real(self.root, catalog=Mock())
        provider.profile_rows = self.service.profiles.rows
        return provider.environment(bot['id'])

    def test_the_watcher_signature_sees_a_new_profile_file(self):
        folder = self.hermes / 'profiles' / 'later'
        folder.mkdir()
        before = sorted(profile_registry.scan(self.hermes))
        (folder / 'config.yaml').write_text('{}\n')
        self.assertNotEqual(sorted(profile_registry.scan(self.hermes)), before)

    def test_a_new_bot_comes_from_the_hermes_profile_api_once(self):
        bot = self.write('POST', '/v1/bots', {'name': 'Lin', 'role': 'Writer', 'instructions': 'Write well.'})
        self.assertEqual(self.commands, [('create', 'lin')])
        self.assertEqual((bot['profile'], bot['profile_id']), ('lin', bot['id']))
        self.assertEqual((self.hermes / 'profiles/lin/SOUL.md').read_text(), 'Write well.')
        self.service.rescan_profiles()
        self.assertEqual(len(self.bots()), 3)

    def test_a_failed_creation_fails_the_request_and_a_leftover_folder_becomes_a_bot(self):
        def leaves_a_folder(python, source, hermes_home, operation, **payload):
            self.hermes_command(python, source, hermes_home, operation, **payload)
            raise ProfileError('Hermes did not answer the profile request.')

        with patch('coconut.bot_profiles.run_profile_command', side_effect=leaves_a_folder):
            with self.assertRaisesRegex(ServiceError, 'Hermes did not create the profile'):
                self.write('POST', '/v1/bots', {'name': 'Lin'})
        self.assertEqual(len(self.bots()), 2)
        self.service.rescan_profiles()
        self.assertEqual(self.bots()['lin']['name'], 'lin')

    def test_the_create_command_passes_the_coconut_skills_folder(self):
        with patch('coconut.bot_profiles.run_profile_command', side_effect=self.hermes_command) as command:
            self.write('POST', '/v1/bots', {'name': 'Lin'})
        self.assertEqual(command.call_args.kwargs['skills'], str(SKILLS))

    def test_settings_are_native(self):
        self.provider.catalog.use_hermes_home.assert_called_with(self.hermes.absolute())

    def test_default_settings_stay_in_the_base_home_when_the_cli_uses_another_profile(self):
        from coconut.model_catalog import HermesModelCatalog
        source = self.base / 'source'
        source.mkdir()
        (self.hermes / 'active_profile').write_text('work\n')
        catalog = HermesModelCatalog(self.base / 'catalog-data', source=source, python=sys.executable)
        catalog.use_hermes_home(self.hermes)
        self.assertEqual(catalog._settings_home(), self.hermes.absolute())
        self.assertIn(str(self.hermes / 'config.yaml'), [entry[0] for entry in catalog._signature()])

    def test_remove_from_coconut_keeps_the_folder_and_it_does_not_return(self):
        bot = self.bots()['work']
        self.write('DELETE', f'/v1/bots/{bot["id"]}')
        self.assertTrue(self.work.is_dir())
        self.service.rescan_profiles()
        self.open()
        self.assertEqual(set(self.bots()), {'default'})
        self.assertEqual(self.commands, [])

    def test_deleting_a_hermes_profile_needs_its_typed_name_and_never_default(self):
        bots = self.bots()
        with self.assertRaisesRegex(ServiceError, 'never default'):
            self.write('POST', f'/v1/bots/{bots["default"]["id"]}/profile/delete', {'confirm': 'default'})
        with self.assertRaisesRegex(ServiceError, 'Type the profile name'):
            self.write('POST', f'/v1/bots/{bots["work"]["id"]}/profile/delete', {'confirm': 'Work'})
        self.assertTrue(self.work.is_dir())
        self.assertEqual(self.write('POST', f'/v1/bots/{bots["work"]["id"]}/profile/delete', {'confirm': 'work'}),
                         {'ok': True, 'profile': 'work'})
        self.assertEqual(self.commands, [('delete', 'work')])
        self.assertFalse(self.work.exists())
        self.assertTrue(self.hermes.is_dir())
        # The name is free again: a new profile with it is a new bot.
        self.profile('work')
        self.service.rescan_profiles()
        self.assertNotEqual(self.bots()['work']['id'], bots['work']['id'])

    def test_a_bot_can_only_remove_another_bot_from_coconut(self):
        from coconut.hermes_worker import BOT_DELETION_SCHEMA
        self.assertEqual(set(BOT_DELETION_SCHEMA['parameters']['properties']), {'source_message_id', 'target_bot_id', 'expected_name'})
        self.assertIn('Hermes profile folder stays', BOT_DELETION_SCHEMA['description'])

    def test_a_bot_from_before_the_migration_runs_no_turns(self):
        bot_id = str(uuid4())
        with self.service.transaction():
            self.service._put('bots', dict(self.bots()['work'], id=bot_id, profile_id=bot_id, session_id=str(uuid4())))
        # The service still starts, so GET /v1/setup can say what to do.
        self.open()
        self.assertTrue(self.service.hermes_setup()['migration_required'])
        self.assertEqual(self.service.profiles.sync(self.service._get('bots', bot_id))['status'], 'unmigrated')
        with self.assertRaisesRegex(Exception, 'migrate_bots_to_profiles'):
            self.provider_environment({'id': bot_id})


class WorkerTests(unittest.TestCase):
    def test_a_worker_runs_in_the_profile_and_is_replaced_when_the_home_changes(self):
        from coconut.providers import HermesProvider as Real
        with tempfile.TemporaryDirectory() as temporary:
            root, hermes = Path(temporary) / 'data', Path(temporary) / '.hermes'
            bot_id = str(uuid4())
            first, second = hermes / 'profiles' / 'work', hermes / 'profiles' / 'office'
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            provider = Real(root, catalog=Mock())
            provider.profile_rows = {bot_id: dict(bot_id=bot_id, name='work', home=str(first), state='active')}
            env, _ = provider.environment(bot_id)
            self.assertEqual((env['HERMES_HOME'], env['COCONUT_BOT_ID']), (str(first), bot_id))
            self.assertEqual(env['COCONUT_BOT_DIR'], str(root.resolve() / 'bots' / bot_id))
            self.assertEqual(sorted(first.iterdir()), [])
            started, stopped = [], []

            class Process:
                def __init__(self, *args, env=None, **kwargs):
                    self.pid, self.env = 1000 + len(started), env
                    started.append(self)

                def poll(self):
                    return None

            bot = {'id': bot_id}
            with patch('coconut.providers.subprocess.Popen', Process), patch.object(provider, '_terminate', side_effect=stopped.append):
                provider.prepare_kanban_worker('one', bot, temporary, 'execution', worker_id='worker')
                provider.release_kanban_preparation('one')
                provider.prepare_kanban_worker('two', bot, temporary, 'execution', worker_id='worker')
                provider.release_kanban_preparation('two')
                self.assertEqual(len(started), 1)
                provider.profile_rows[bot_id]['home'] = str(second)
                provider.prepare_kanban_worker('three', bot, temporary, 'execution', worker_id='worker')
            self.assertEqual(started[1].env['HERMES_HOME'], str(second))
            self.assertEqual(stopped, [started[0]])


class ProfileCommandTests(unittest.TestCase):
    def test_create_and_delete_use_the_hermes_profile_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, hermes = Path(temporary) / 'source', Path(temporary) / '.hermes'
            (source / 'hermes_cli').mkdir(parents=True)
            (source / 'hermes_cli/__init__.py').write_text('')
            (source / 'hermes_cli/profiles.py').write_text(
                'import os, shutil\nfrom pathlib import Path\n'
                'def _root():\n    return Path(os.environ["HERMES_HOME"]) / "profiles"\n'
                'def create_profile(name, description=None, soul=None):\n'
                '    print("Hermes prints to stdout.")\n'
                '    home = _root() / name\n    home.mkdir(parents=True)\n    (home / "SOUL.md").write_text(soul)\n    return home\n'
                'def seed_profile_skills(home, quiet=False):\n    (home / "skills").mkdir()\n'
                'def check_alias_collision(name):\n    return "taken"\n'
                'def write_profile_meta(home, display_name=None):\n'
                '    (home / "profile.yaml").write_text("display_name: " + display_name + "\\n")\n'
                'def delete_profile(name, yes=False):\n    assert yes\n    shutil.rmtree(_root() / name)\n    return _root() / name\n')
            # Stand-ins for the config reader and writer of Hermes, which skill_dirs uses.
            (source / 'native_settings.py').write_text(
                'import json\nfrom pathlib import Path\n'
                'def raw_config(home):\n    path = Path(home) / "config.yaml"\n'
                '    return json.loads(path.read_text()) if path.exists() else {}\n'
                'def save_config(home, config):\n    (Path(home) / "config.yaml").write_text(json.dumps(config))\n')
            hermes.mkdir()
            coconut = Path(__file__).resolve().parents[1] / 'coconut'
            created = run_profile_command(sys.executable, source, hermes, 'create', name='lin', display_name='Lin',
                                          description='Writer', soul='Write.\n', skills=str(SKILLS), coconut=str(coconut))
            home = hermes / 'profiles/lin'
            self.assertEqual((Path(created['home']), created['skills']), (home, 'added'))
            self.assertEqual((home / 'SOUL.md').read_text(), 'Write.\n')
            self.assertEqual(profile_registry.meta_value(home, 'display_name'), 'Lin')
            self.assertEqual(json.loads((home / 'config.yaml').read_text()), {'skills': {'external_dirs': [str(SKILLS)]}})
            run_profile_command(sys.executable, source, hermes, 'delete', name='lin')
            self.assertFalse(home.exists())
            with self.assertRaisesRegex(ProfileError, 'refused'):
                run_profile_command(sys.executable, source, hermes, 'delete', name='lin')


class RegistryTests(unittest.TestCase):
    def test_slugs_are_unique_and_valid(self):
        taken = {'ada', 'ada-2'}
        self.assertEqual(profile_registry.slug('Ada', taken), 'ada-3')
        self.assertEqual(profile_registry.slug('Research Lead!', taken), 'research-lead')
        self.assertEqual(profile_registry.slug('default', set()), 'default-bot')
        self.assertEqual(profile_registry.slug('🙂', set()), 'bot')
        self.assertTrue(profile_registry.PROFILE_ID.match(profile_registry.slug('x' * 90, set())))

    def test_scan_lists_live_profiles_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            hermes = Path(temporary)
            for name, marker in (('work', 'SOUL.md'), ('empty', None), ('Bad Name', 'SOUL.md'), ('gone', 'SOUL.md')):
                (hermes / 'profiles' / name).mkdir(parents=True)
                if marker:
                    (hermes / 'profiles' / name / marker).write_text('x')
            (hermes / 'profiles/.deleted').mkdir()
            (hermes / 'profiles/.deleted/gone').write_text('deleted\n')
            self.assertEqual(list(profile_registry.scan(hermes)), ['default', 'work'])


if __name__ == '__main__':
    unittest.main()
