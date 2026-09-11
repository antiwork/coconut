from contextlib import closing
import fcntl
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / 'backend'))
import terminal
from coconut.model_catalog import HermesModelCatalog, ModelCatalogError, resolve_selection
from coconut.profiles import HermesProfiles


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "Coconut user's project"
        self.root.mkdir()
        self.user_home = self.base / 'account'
        self.user_home.mkdir()
        self.data = self.root / '.runtime/host'
        self.data.mkdir(parents=True)
        self.bot = dict(id=str(uuid4()), name='Ada', session_id=str(uuid4()), role='Research',
                        instructions='Keep answers brief.', memory='', preferences=[])
        self.bot['profile_id'] = self.bot['id']
        with closing(sqlite3.connect(self.data / 'coconut.sqlite3')) as connection, connection:
            connection.execute('CREATE TABLE bots(id TEXT PRIMARY KEY,data TEXT)')
            connection.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
            connection.execute('INSERT INTO bots VALUES (?,?)', (self.bot['id'], json.dumps(self.bot)))
        HermesProfiles(self.data).sync(self.bot)
        source = self.root / '.runtime/hermes/source/hermes_cli'
        source.mkdir(parents=True)
        (source / 'main.py').write_text('# Synthetic CLI entry point')
        python = self.root / '.runtime/hermes/venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('# Managed Python placeholder')
        self.credentials = self.data / 'credentials.json'
        self.credentials.write_text(json.dumps({'ANTHROPIC_API_KEY': 'test-only-model-key', 'GITHUB_TOKEN': 'never-forward'}))
        self.credentials.chmod(0o600)
        self.catalog = {
            'default': {'model': 'gpt-6-astra', 'provider': 'openai-codex', 'base_url': None, 'reasoning_effort': 'high'},
            'models': [
                {'id': 'gpt-6-astra', 'provider': 'openai-codex', 'reasoning_efforts': ['low', 'high'],
                 'default_reasoning_effort': 'low'},
                {'id': 'local-model', 'provider': 'custom', 'base_url': 'http://127.0.0.1:12345/v1',
                 'reasoning_efforts': [], 'default_reasoning_effort': None},
            ],
        }
        catalog = patch.object(HermesModelCatalog, 'get', return_value=self.catalog)
        catalog.start()
        self.addCleanup(catalog.stop)
        validation = patch.object(HermesModelCatalog, 'get_for_validation', return_value=self.catalog)
        validation.start()
        self.addCleanup(validation.stop)
        defaults = patch.object(HermesModelCatalog, 'get_default', side_effect=lambda: dict(self.catalog['default']))
        defaults.start()
        self.addCleanup(defaults.stop)
        self.native_profile = {'model_override': None, 'effective_model': None}
        native = patch.object(HermesModelCatalog, 'read_profile', side_effect=lambda bot_id: dict(self.native_profile))
        native.start()
        self.addCleanup(native.stop)
        raw_profile = patch('coconut.model_catalog._read_profile', side_effect=lambda home, inherited: dict(self.native_profile))
        raw_profile.start()
        self.addCleanup(raw_profile.stop)
        replacement = patch.object(terminal, 'ROOT', self.root)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.cwd = Path.cwd()
        self.addCleanup(os.chdir, self.cwd)

    def capture_launch(self, arguments):
        captured = {}
        def execute(path, command, environment):
            captured.update(path=path, command=command, environment=environment, cwd=Path.cwd())
        with patch.object(terminal.os, 'execve', side_effect=execute):
            terminal.launch(arguments)
        return captured

    def test_installed_shell_wrapper_preserves_quoted_arguments_and_needs_no_shell_edits(self):
        script = self.root / 'scripts/terminal.py'
        script.parent.mkdir()
        script.write_text('import json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        profile = self.user_home / '.zprofile'
        profile.write_text('# Existing shell settings\n')
        wrapper = terminal.install_command(self.user_home)
        values = ['--profile', "name with ' quotes", 'chat', '-q', '$(touch forbidden) `false` $HOME']
        result = subprocess.check_output([str(wrapper), *values], text=True)
        self.assertEqual(json.loads(result), ['launch', '--', *values])
        self.assertEqual(wrapper.stat().st_mode & 0o777, 0o700)
        self.assertEqual(profile.read_text(), '# Existing shell settings\n')
        self.assertEqual(terminal.install_command(self.user_home), wrapper)
        self.assertFalse(list(wrapper.parent.glob('.coconut-hermes-*')))

    def test_install_and_uninstall_keep_other_commands_and_symlinks(self):
        wrapper = self.user_home / '.local/bin/hermes'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('Another Hermes installation')
        with self.assertRaisesRegex(SystemExit, 'different hermes'):
            terminal.install_command(self.user_home)
        self.assertFalse(terminal.uninstall_command(self.user_home))
        self.assertEqual(wrapper.read_text(), 'Another Hermes installation')
        wrapper.unlink()
        protected = self.base / 'protected'
        protected.write_bytes(terminal.wrapper_bytes())
        wrapper.symlink_to(protected)
        with self.assertRaises(terminal.deploy.DeployError):
            terminal.install_command(self.user_home)
        self.assertFalse(terminal.uninstall_command(self.user_home))
        self.assertTrue(protected.exists())

    def test_uninstall_removes_only_the_exact_owned_wrapper(self):
        wrapper = terminal.install_command(self.user_home)
        neighbor = wrapper.parent / 'other-command'
        neighbor.write_text('Keep')
        self.assertTrue(terminal.uninstall_command(self.user_home))
        self.assertFalse(wrapper.exists())
        self.assertEqual(neighbor.read_text(), 'Keep')
        self.assertFalse(terminal.uninstall_command(self.user_home))

    def test_terminal_launch_uses_managed_runtime_private_homes_and_only_saved_model_keys(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'ambient-key', 'SSH_AUTH_SOCK': 'agent',
                                     'ANTHROPIC_API_KEY': 'unrelated-key', 'HERMES_HOME': '/outside',
                                     'HERMES_REAL_HOME': '/outside', 'PYTHONPATH': '/outside'}):
            result = self.capture_launch(['--profile', 'work', '--help'])
        environment = result['environment']
        self.assertEqual(environment['ANTHROPIC_API_KEY'], 'test-only-model-key')
        self.assertNotIn('GITHUB_TOKEN', environment)
        self.assertNotIn('SSH_AUTH_SOCK', environment)
        import pwd
        self.assertEqual(environment['HOME'], pwd.getpwuid(os.getuid()).pw_dir)
        self.assertEqual(environment['CLAUDE_CONFIG_DIR'], str(self.root / '.runtime/hermes/terminal/home/.claude'))
        self.assertEqual(environment['CODEX_HOME'], str(self.root / '.runtime/hermes/terminal/home/.codex'))
        self.assertEqual(environment['HERMES_REAL_HOME'], environment['HOME'])
        self.assertEqual(environment['HERMES_HOME'], str(self.root / '.runtime/hermes/terminal/hermes'))
        self.assertEqual(result['command'][-3:], ['--profile', 'work', '--help'])
        self.assertEqual(result['cwd'], self.root / '.runtime/hermes/terminal/workspace')
        self.assertFalse((Path(environment['HERMES_HOME']) / 'config.yaml').exists())

    def test_existing_terminal_model_config_is_preserved(self):
        self.capture_launch(['--help'])
        config = self.root / '.runtime/hermes/terminal/hermes/config.yaml'
        content = '{"model":{"default":"chosen","provider":"openrouter"}}'
        config.write_text(content)
        self.capture_launch(['--help'])
        self.assertEqual(config.read_text(), content)

    def test_bare_terminal_preserves_active_profile_and_ignores_old_app_model_settings(self):
        self.capture_launch(['--help'])
        home = self.root / '.runtime/hermes/terminal/hermes'
        active = home / 'active_profile'
        active.write_text('native-work\n')
        profile = home / 'profiles/native-work'
        profile.mkdir(parents=True)
        config = profile / 'config.yaml'
        content = '{"model":{"default":"gpt-6-astra","provider":"openai-codex"},"reasoning_effort":"high"}'
        config.write_text(content)
        with closing(sqlite3.connect(self.data / 'coconut.sqlite3')) as connection, connection:
            connection.executemany('INSERT INTO settings VALUES (?,?)', [
                ('model', 'malformed old setting'), ('base_url', json.dumps('http://unsafe.example/v1'))])
        result = self.capture_launch(['config', 'show'])
        self.assertEqual(result['command'][-2:], ['config', 'show'])
        self.assertEqual(active.read_text(), 'native-work\n')
        self.assertEqual(config.read_text(), content)
        self.assertFalse((home / 'config.yaml').exists())
        self.assertNotIn('COCONUT_TERMINAL_MODEL', result['environment'])

    def test_bare_terminal_does_not_depend_on_the_app_database(self):
        (self.data / 'coconut.sqlite3').write_bytes(b'Invalid app database')
        result = self.capture_launch(['--help'])
        self.assertEqual(result['command'][-1], '--help')

    def test_bot_launch_keeps_exact_profile_and_holds_inherited_service_lock(self):
        lock = self.data / 'service.lock'
        def execute(path, command, environment):
            self.assertEqual(environment['HERMES_HOME'], str(self.data / 'bots' / self.bot['id'] / 'home'))
            self.assertEqual(command[3:5], ['--profile', 'default'])
            descriptor = os.open(lock, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
        with patch.object(terminal.os, 'execve', side_effect=execute), patch('sys.stderr', io.StringIO()):
            terminal.launch(['--coconut-bot', 'Ada', '--help'])
        descriptor = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

    def test_bot_service_lease_survives_actual_exec_and_releases_on_exit(self):
        executable = self.root / '.runtime/hermes/venv/bin/python'
        executable.write_text("""#!/usr/bin/python3
import fcntl,json,os
from pathlib import Path
lock=Path(os.environ['HERMES_HOME']).parents[2]/'service.lock'
descriptor=os.open(lock,os.O_RDWR)
held=False
try:
    fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:
    held=True
finally:
    os.close(descriptor)
print(json.dumps({'service_lock_held':held}))
""")
        executable.chmod(0o700)
        program = ("import sys; from pathlib import Path; "
                   "sys.path.insert(0,sys.argv[1]); sys.path.insert(0,sys.argv[2]); "
                   "import json; from coconut.model_catalog import HermesModelCatalog; "
                   "catalog=json.loads(sys.argv[4]); HermesModelCatalog.get=lambda self: catalog; "
                   "HermesModelCatalog.get_default=lambda self: catalog['default']; "
                   "HermesModelCatalog.read_profile=lambda self,bot_id: {'model_override':None,'effective_model':None}; "
                   "import terminal; terminal.ROOT=Path(sys.argv[3]); "
                   "terminal.launch(['--coconut-bot','Ada','--help'])")
        result = subprocess.run(['/usr/bin/python3', '-B', '-c', program, str(SCRIPTS),
                                 str(SCRIPTS.parent / 'backend'), str(self.root), json.dumps(self.catalog)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {'service_lock_held': True})
        descriptor = os.open(self.data / 'service.lock', os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

    def test_running_service_and_moved_source_block_bot_access(self):
        descriptor = os.open(self.data / 'service.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(SystemExit, 'Stop Coconut'):
                terminal.launch(['--coconut-bot', self.bot['id']])
        finally:
            os.close(descriptor)
        (self.data / 'moved-to.json').write_text('{}')
        with self.assertRaisesRegex(SystemExit, 'moved to another Mac'):
            terminal.launch(['--coconut-bot', 'Ada'])
        self.capture_launch(['--help'])

    def test_bot_profile_cannot_accidentally_select_another_native_profile(self):
        for arguments in (['--profile', 'other'], ['-p', 'other'], ['--profile=other']):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(SystemExit, 'either a Coconut bot'):
                terminal.launch(['--coconut-bot', 'Ada', *arguments])

    def test_credentials_and_shared_dotenv_fail_without_exposing_secret_values(self):
        self.credentials.chmod(0o644)
        with self.assertRaisesRegex(SystemExit, 'private permissions') as failure:
            self.capture_launch(['--help'])
        self.assertNotIn('test-only-model-key', str(failure.exception))
        self.credentials.chmod(0o600)
        (self.root / '.runtime/hermes/source/.env').write_text('PRIVATE=not-read')
        with self.assertRaisesRegex(SystemExit, 'shared credentials'):
            self.capture_launch(['--help'])

    def test_bot_uses_the_native_default_and_effort_instead_of_old_app_settings(self):
        with closing(sqlite3.connect(self.data / 'coconut.sqlite3')) as connection, connection:
            connection.executemany('INSERT INTO settings VALUES (?,?)', [
                ('model', json.dumps('local-model')), ('base_url', json.dumps('http://127.0.0.1:12345/v1'))])
        with patch('sys.stderr', io.StringIO()):
            result = self.capture_launch(['--coconut-bot', 'Ada', '--help'])
        selected = json.loads(result['environment']['COCONUT_TERMINAL_MODEL'])
        self.assertEqual(selected, {'default': 'gpt-6-astra', 'provider': 'openai-codex'})
        self.assertEqual(result['command'][3:], ['--profile', 'default', '--model', 'gpt-6-astra',
                                               '--provider', 'openai-codex', '--reasoning', 'high', '--help'])
        self.assertEqual(result['environment']['ANTHROPIC_API_KEY'], 'test-only-model-key')

    def save_override(self, value):
        self.bot['model_override'] = value
        self.native_profile = {'model_override': value,
                               'effective_model': resolve_selection(value, self.catalog) if value is not None else None}
        with closing(sqlite3.connect(self.data / 'coconut.sqlite3')) as connection, connection:
            connection.execute('UPDATE bots SET data=? WHERE id=?', (json.dumps(self.bot), self.bot['id']))

    def test_bot_override_uses_the_catalog_provider_endpoint_and_effort(self):
        self.save_override({'model': 'local-model', 'provider': 'custom', 'reasoning_effort': None})
        with patch('sys.stderr', io.StringIO()):
            result = self.capture_launch(['--coconut-bot', 'Ada', '--help'])
        selected = json.loads(result['environment']['COCONUT_TERMINAL_MODEL'])
        self.assertEqual(selected, {'default': 'local-model', 'provider': 'custom', 'base_url': 'http://127.0.0.1:12345/v1'})
        self.assertNotIn('--reasoning', result['command'])
        self.assertNotIn('api_key', selected)
        self.assertNotIn('test-only-model-key', json.dumps(selected))

    def test_opening_native_override_does_not_discover_or_validate_against_sqlite(self):
        self.save_override({'model': 'local-model', 'provider': 'custom'})
        with patch.object(HermesModelCatalog, 'get_for_validation', side_effect=AssertionError('No discovery on reopen')) as fetch, \
             patch('sys.stderr', io.StringIO()):
            self.capture_launch(['--coconut-bot', 'Ada', '--help'])
        fetch.assert_not_called()

    def test_bot_reopen_preserves_native_profile_errors_without_falling_back(self):
        self.native_profile = {'error': 'This native profile cannot be read.'}
        with self.assertRaisesRegex(ModelCatalogError, 'cannot be read'), patch.object(terminal.os, 'execve') as execute:
            terminal.launch(['--coconut-bot', 'Ada'])
        execute.assert_not_called()

    def test_reset_bot_override_follows_the_latest_native_default(self):
        self.save_override({'model': 'gpt-6-astra', 'provider': 'openai-codex', 'reasoning_effort': 'low'})
        with patch('sys.stderr', io.StringIO()):
            first = self.capture_launch(['--coconut-bot', 'Ada', '--help'])
            self.save_override(None)
            self.catalog['default']['reasoning_effort'] = 'high'
            second = self.capture_launch(['--coconut-bot', 'Ada', '--help'])
        self.assertIn('low', first['command'])
        self.assertIn('high', second['command'])

    def test_external_native_model_edit_wins_when_the_bot_reopens(self):
        self.save_override({'model': 'gpt-6-astra', 'provider': 'openai-codex', 'reasoning_effort': 'high'})
        path = self.data / 'bots' / self.bot['id'] / 'home/config.yaml'
        def read_profile(bot_id):
            raw = json.loads(path.read_text())
            selected = {'model': raw['model']['default'], 'provider': raw['model']['provider'],
                        'reasoning_effort': raw['agent']['reasoning_effort'], 'base_url': None}
            descriptor = os.open(self.data / 'service.lock', os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            return {'model_override': selected, 'effective_model': selected}
        path.write_text(json.dumps({'model': {'default': 'gpt-6-astra', 'provider': 'openai-codex'},
                                    'agent': {'reasoning_effort': 'high'}}))
        with patch.object(HermesModelCatalog, 'read_profile', side_effect=read_profile), patch('sys.stderr', io.StringIO()):
            first = self.capture_launch(['--coconut-bot', 'Ada', '--help'])
            content = json.dumps({'model': {'default': 'native-new-model', 'provider': 'openai-codex',
                                           'extra_headers': {'x-native-option': 'preserve'}},
                                  'agent': {'reasoning_effort': 'low'}})
            path.write_text(content)
            second = self.capture_launch(['--coconut-bot', 'Ada', '--help'])
        self.assertIn('gpt-6-astra', first['command'])
        self.assertIn('native-new-model', second['command'])
        self.assertIn('low', second['command'])
        self.assertEqual(path.read_text(), content)
        with closing(sqlite3.connect(self.data / 'coconut.sqlite3')) as connection:
            stale = json.loads(connection.execute('SELECT data FROM bots WHERE id=?', (self.bot['id'],)).fetchone()[0])
        self.assertEqual(stale['model_override']['model'], 'gpt-6-astra')

    def test_bot_cli_shares_canonical_auth_without_copying_tokens_or_unrelated_keys(self):
        root = self.root / '.runtime/hermes/terminal/hermes'
        native = root / 'profiles/native-work'
        native.mkdir(parents=True)
        (root / 'active_profile').write_text('native-work\n')
        for directory in (root, native):
            path = directory / 'auth.json'
            path.write_text('{"test-token":"keep-only-in-native-auth"}')
            path.chmod(0o600)
        (native / '.env').write_text('OPENAI_API_KEY=test-native-model-key\nGITHUB_TOKEN=never-forward\n')
        (native / '.env').chmod(0o600)
        with patch('sys.stderr', io.StringIO()):
            result = self.capture_launch(['--coconut-bot', 'Ada', '--help'])
        bot_home = Path(result['environment']['HERMES_HOME'])
        package = ModuleType('hermes_cli')
        auth = ModuleType('hermes_cli.auth')
        auth.PROVIDER_REGISTRY = {'openai-codex': SimpleNamespace(api_key_env_vars=('OPENAI_API_KEY',))}
        package.auth = auth
        agent = ModuleType('agent')
        anthropic = ModuleType('agent.anthropic_credentials')
        agent.anthropic_credentials = anthropic
        config = ModuleType('hermes_cli.config')
        config.load_config = lambda: {'agent': {'reasoning_effort': 'low', 'reasoning_overrides': {'gpt-6-astra': 'low'}}}
        config.save_config = lambda value: (bot_home / 'config.yaml').write_text(json.dumps(value))
        main = ModuleType('hermes_cli.main')
        expected_key = 'test-native-model-key'
        def entered():
            self.assertEqual(auth._auth_file_path(), native / 'auth.json')
            self.assertEqual(auth._global_auth_file_path(), root / 'auth.json')
            self.assertEqual(anthropic._get_hermes_oauth_file(), native / '.anthropic_oauth.json')
            self.assertEqual(anthropic._root_hermes_oauth_file(), root / '.anthropic_oauth.json')
            self.assertEqual(os.environ['HERMES_HOME'], str(bot_home))
            self.assertEqual(os.environ['OPENAI_API_KEY'], expected_key)
            self.assertNotIn('GITHUB_TOKEN', os.environ)
            self.assertFalse(any(key.startswith('COCONUT_TERMINAL_') for key in os.environ))
            self.assertEqual(config.load_config()['model']['default'], 'gpt-6-astra')
        main.main = entered
        dotenv = ModuleType('dotenv')
        dotenv.dotenv_values = lambda path: dict(line.split('=', 1) for line in path.read_text().splitlines())
        with patch.dict(sys.modules, {'hermes_cli': package, 'hermes_cli.auth': auth,
                                     'hermes_cli.config': config, 'hermes_cli.main': main, 'dotenv': dotenv,
                                     'agent': agent, 'agent.anthropic_credentials': anthropic}), \
             patch.dict(os.environ, result['environment'], clear=True):
            terminal.run_bot_cli()
        self.assertNotIn('model', json.loads((bot_home / 'config.yaml').read_text()))
        for path in bot_home.rglob('*'):
            if path.is_file():
                self.assertNotIn('keep-only-in-native-auth', path.read_text())
                self.assertNotIn('test-native-model-key', path.read_text())
        self.assertFalse((bot_home / 'auth.json').exists())
        (native / '.env').write_text('OPENAI_API_KEY=\n')
        expected_key = ''
        blank_environment = dict(result['environment'], OPENAI_API_KEY='legacy-model-key')
        with patch.dict(sys.modules, {'hermes_cli': package, 'hermes_cli.auth': auth,
                                     'hermes_cli.config': config, 'hermes_cli.main': main, 'dotenv': dotenv,
                                     'agent': agent, 'agent.anthropic_credentials': anthropic}), \
             patch.dict(os.environ, blank_environment, clear=True):
            config.load_config = lambda: {'agent': {}}
            terminal.run_bot_cli()

    def test_named_custom_provider_resolves_native_credentials_only_in_memory(self):
        row = {'id': 'local-model', 'provider': 'custom:studio', 'base_url': 'http://127.0.0.1:12345/v1',
               'reasoning_efforts': [], 'default_reasoning_effort': None}
        self.catalog['models'].append(row)
        self.save_override({'model': 'local-model', 'provider': 'custom:studio'})
        with patch('sys.stderr', io.StringIO()):
            result = self.capture_launch(['--coconut-bot', 'Ada', '--help'])
        package, auth = ModuleType('hermes_cli'), ModuleType('hermes_cli.auth')
        auth.PROVIDER_REGISTRY = {}
        package.auth = auth
        agent, anthropic = ModuleType('agent'), ModuleType('agent.anthropic_credentials')
        agent.anthropic_credentials = anthropic
        config = ModuleType('hermes_cli.config')
        config.load_config = lambda: {}
        saved = []
        config.save_config = saved.append
        runtime = ModuleType('hermes_cli.runtime_provider')
        runtime.resolve_runtime_provider = lambda **kwargs: {'provider': 'explicit-request'}
        package.runtime_provider = runtime
        main = ModuleType('hermes_cli.main')
        private = {'provider': 'custom', 'api_key': 'native-only-test-key', 'api_mode': 'chat_completions',
                   'base_url': row['base_url']}
        def entered():
            self.assertEqual(runtime.resolve_runtime_provider(requested='custom:studio', target_model='local-model'), private)
            self.assertEqual(runtime.resolve_runtime_provider(requested='custom:studio',
                             explicit_base_url='https://different.example/v1'), {'provider': 'explicit-request'})
        main.main = entered
        with patch.dict(sys.modules, {'hermes_cli': package, 'hermes_cli.auth': auth,
                                     'hermes_cli.config': config, 'hermes_cli.main': main,
                                     'hermes_cli.runtime_provider': runtime,
                                     'agent': agent, 'agent.anthropic_credentials': anthropic}), \
             patch.dict(os.environ, result['environment'], clear=True), \
             patch.object(HermesModelCatalog, 'resolve_runtime', return_value=private) as resolve:
            terminal.run_bot_cli()
        resolve.assert_called_once_with({'model': 'local-model', 'provider': 'custom:studio', 'reasoning_effort': None})
        self.assertNotIn('native-only-test-key', json.dumps(saved))

    def test_bot_cli_rejects_changed_native_profile_before_loading_credentials(self):
        with patch('sys.stderr', io.StringIO()):
            result = self.capture_launch(['--coconut-bot', 'Ada', '--help'])
        root = self.root / '.runtime/hermes/terminal/hermes'
        (root / 'profiles/changed').mkdir(parents=True)
        (root / 'active_profile').write_text('changed\n')
        with patch.dict(os.environ, result['environment'], clear=True), \
             self.assertRaisesRegex(SystemExit, 'profile changed'):
            terminal.run_bot_cli()

    def test_native_auth_rejects_links_and_nonprivate_files(self):
        path = self.root / 'auth.json'
        path.write_text('test-only-secret')
        path.chmod(0o644)
        with self.assertRaisesRegex(SystemExit, 'private permissions'):
            terminal.private_auth_path(path)
        path.unlink()
        path.symlink_to(self.credentials)
        with self.assertRaises(terminal.deploy.DeployError):
            terminal.private_auth_path(path)

    def test_app_local_endpoint_does_not_replace_keys_for_existing_native_cloud_profile(self):
        self.capture_launch(['--help'])
        config = self.root / '.runtime/hermes/terminal/hermes/config.yaml'
        content = '{"model":{"default":"native-model","provider":"openai"}}'
        config.write_text(content)
        self.credentials.write_text(json.dumps({'OPENAI_API_KEY': 'test-only-openai'}))
        with closing(sqlite3.connect(self.data / 'coconut.sqlite3')) as connection, connection:
            connection.execute('INSERT INTO settings VALUES (?,?)', ('base_url', json.dumps('http://localhost:12345/v1')))
        result = self.capture_launch(['--profile', 'native', '--help'])
        self.assertEqual(result['environment']['OPENAI_API_KEY'], 'test-only-openai')
        self.assertEqual(config.read_text(), content)

    def test_bot_rechecks_migration_fence_after_acquiring_service_lock(self):
        original = terminal.fcntl.flock
        def fence_after_lock(descriptor, operation):
            result = original(descriptor, operation)
            if operation == (fcntl.LOCK_EX | fcntl.LOCK_NB):
                (self.data / 'moved-to.json').write_text('{}')
            return result
        with patch.object(terminal.fcntl, 'flock', side_effect=fence_after_lock):
            with self.assertRaisesRegex(SystemExit, 'moved to another Mac'):
                self.capture_launch(['--coconut-bot', 'Ada'])

    def test_active_terminal_session_blocks_cleanup_lease_and_shared_sessions_coexist(self):
        with terminal.runtime_lease():
            with terminal.runtime_lease():
                with self.assertRaisesRegex(SystemExit, 'Close Hermes Terminal'):
                    with terminal.runtime_lease(exclusive=True):
                        self.fail('Cleanup must not remove an active runtime.')
        with terminal.runtime_lease(exclusive=True):
            with self.assertRaisesRegex(SystemExit, 'Close Hermes Terminal'):
                with terminal.runtime_lease():
                    self.fail('A Terminal session must not start during cleanup.')

    def test_bot_listing_never_loads_model_credentials(self):
        with patch.object(terminal, 'model_access', side_effect=AssertionError('Credentials must remain unread.')), \
             patch('sys.stdout', io.StringIO()) as output:
            terminal.launch(['--coconut-bots'])
        self.assertEqual(output.getvalue().strip(), 'Ada\t' + self.bot['id'])


if __name__ == '__main__':
    unittest.main()
