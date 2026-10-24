"""The one-time step that writes the inherited settings of each bot into its own profile."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hermes_env import needs_hermes, PYTHON, SOURCE

BACKEND = Path(__file__).resolve().parents[1]
sys.path.append(str(BACKEND / 'coconut'))
import settings_migration as migration  # noqa: E402


class SettingsPartsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'bots' / 'bot' / 'home'
        self.home.mkdir(parents=True)

    def test_only_coconut_scripts_leave_the_shell_setup(self):
        custom = '/Users/coconut/Developer/CoconutMaintenance/toolchains.sh'
        files = [str(self.home / '.profile'), custom, str(self.home / 'coconut-terminal.sh'), str(self.home / 'coconut-google.sh')]
        config = {'terminal': {'cwd': '/workspace', 'shell_init_files': list(files)}}
        removed = migration._shell_init(self.home, config)
        self.assertEqual(config['terminal']['shell_init_files'], [str(self.home / '.profile'), custom])
        self.assertEqual(removed, sorted(files[2:]))
        self.assertEqual(migration._shell_init(self.home, config), [])
        only = {'terminal': {'shell_init_files': [str(self.home / 'coconut-terminal.sh')], 'auto_source_bashrc': False}}
        migration._shell_init(self.home, only)
        self.assertEqual(only['terminal'], {'auto_source_bashrc': False})

    def test_approvals_fill_only_missing_settings(self):
        with patch.object(migration, 'mode', side_effect=lambda value: value):
            config = {'approvals': {'timeout': 30}}
            copied = migration._approvals(config, {'approvals': {'mode': 'off', 'timeout': 60}})
            self.assertEqual(copied, {'mode': 'off'})
            self.assertEqual(config['approvals'], {'mode': 'off', 'timeout': 30})
            self.assertEqual(migration._approvals(config, {'approvals': {'mode': 'smart'}}), {})
            self.assertEqual(migration._approvals({}, {}), {})

    def test_a_folder_with_files_keeps_its_login_and_an_empty_one_does_not(self):
        (self.home / '.config/gh').mkdir(parents=True)
        (self.home / '.codex').mkdir()
        (self.home / '.codex/auth.json').write_text('{}')
        saved = {}
        with patch.object(migration, '_save_env', side_effect=saved.__setitem__):
            kept = migration._redirects(self.home, {'XDG_DATA_HOME': '/already/set'})
        self.assertEqual(kept, {'CODEX_HOME': str(self.home / '.codex')})
        self.assertEqual(saved, kept)
        (self.home / '.config/gh/hosts.yml').write_text('github.com: {}\n')
        with patch.object(migration, '_save_env', side_effect=saved.__setitem__):
            kept = migration._redirects(self.home, {'CODEX_HOME': str(self.home / '.codex')})
        self.assertEqual(kept, {'GH_CONFIG_DIR': str(self.home / '.config/gh'), 'XDG_CONFIG_HOME': str(self.home / '.config')})

    def test_a_linked_login_folder_is_not_kept(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'auth.json').write_text('{}')
        (self.home / '.codex').symlink_to(outside, target_is_directory=True)
        with patch.object(migration, '_save_env') as save:
            self.assertEqual(migration._redirects(self.home, {}), {})
        save.assert_not_called()

    def test_legacy_defaults_change_nothing_in_a_new_profile(self):
        (self.home / '.coconut-native-config.json').write_text('{"version":1}\n')
        (self.home / '.coconut-kanban-tools').write_text('1\n')
        config = {'toolsets': ['kanban'], 'terminal': {'cwd': '/workspace'},
                  'delegation': {'max_concurrent_children': None, 'worktree_isolation': True}}
        backup = self.home.parent / 'settings-migration' / 'backup'
        backup.mkdir(parents=True)
        self.assertFalse(migration._legacy_defaults(self.home, config, backup))
        # After a stop, the markers can be in the backup already. The person's choices then stay.
        for name in ('.coconut-native-config.json', '.coconut-kanban-tools'):
            (self.home / name).rename(backup / name)
        chosen = {'toolsets': ['web'], 'tools': {'tool_search': {'enabled': 'off'}}, 'terminal': {'cwd': '/w'},
                  'delegation': {'max_concurrent_children': None, 'worktree_isolation': True}}
        self.assertFalse(migration._legacy_defaults(self.home, chosen, backup))
        self.assertEqual(chosen['toolsets'], ['web'])
        for name in ('.coconut-native-config.json', '.coconut-kanban-tools'):
            (backup / name).unlink()
        old = {'toolsets': ['web'], 'tools': {'tool_search': {'enabled': 'off'}}}
        self.assertTrue(migration._legacy_defaults(self.home, old, backup))
        self.assertEqual(old['toolsets'], ['web', 'kanban'])
        self.assertEqual(old['delegation'], {'max_concurrent_children': None, 'worktree_isolation': True})
        self.assertEqual(old['terminal'], {'cwd': str(self.home.parent / 'workspace')})
        self.assertEqual(old['tools'], {'tool_search': {}})

    def test_receipts_move_out_of_the_profile_without_replacing_newer_ones(self):
        old = self.home / 'coconut-runs'
        old.mkdir()
        (old / 'job.json').write_text('{"phase": "claimed"}')
        (old / 'other.json').write_text('{"phase": "old"}')
        runs = self.home.parent / 'runs'
        runs.mkdir()
        (runs / 'other.json').write_text('{"phase": "new"}')
        self.assertEqual(migration._move_runs(self.home), {'moved': 1, 'kept': ['other.json']})
        self.assertEqual(json.loads((runs / 'job.json').read_text()), {'phase': 'claimed'})
        self.assertEqual(json.loads((runs / 'other.json').read_text()), {'phase': 'new'})
        self.assertEqual(migration._move_runs(self.home), {'moved': 0, 'kept': ['other.json']})

    def test_a_done_bot_is_skipped_without_hermes(self):
        report = self.home.parent / 'settings-migration' / 'report.json'
        report.parent.mkdir()
        report.write_text('{"status": "done", "version": 1}')
        with patch.dict(sys.modules, {'hermes_constants': None, 'native_mcp': None, 'agent.secret_scope': None}):
            self.assertEqual(migration.migrate_bot(self.home.parent.parent / 'default', self.home)['status'], 'done')


MIGRATION = r'''
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
default, home = root / 'default', root / 'bots' / 'bot' / 'home'
from settings_migration import migrate
reports = migrate(default, {'bot': home})
first = reports['bot']
config_bytes = (home / 'config.yaml').read_bytes()
env_bytes = (home / '.env').read_bytes() if (home / '.env').exists() else None
again = migrate(default, {'bot': home})['bot']
assert again == first, (again, first)
# A step that stopped before its report runs again and finds nothing left to write.
(home.parent / 'settings-migration' / 'report.json').unlink()
rerun = migrate(default, {'bot': home})['bot']
assert rerun['mcp']['added'] == [] and rerun['approvals'] == {} and rerun['login_folders'] == {}, rerun
assert (home / 'config.yaml').read_bytes() == config_bytes
assert ((home / '.env').read_bytes() if (home / '.env').exists() else None) == env_bytes
(home.parent / 'settings-migration' / 'report.json').write_text(json.dumps(first))
from native_settings import raw_config
config = raw_config(home)
from agent.secret_scope import load_env_file
print(json.dumps({'report': first, 'config': config, 'env': load_env_file(home / '.env'),
                  'files': sorted(path.name for path in home.iterdir()),
                  'runs': sorted(path.name for path in (home.parent / 'runs').glob('*.json')),
                  'backup': sorted(path.name for path in (home.parent / 'settings-migration' / 'backup').iterdir())}))
'''


@needs_hermes
class NativeSettingsMigrationTests(unittest.TestCase):
    def test_existing_bot_keeps_what_it_inherited_and_a_second_run_changes_nothing(self):
        with tempfile.TemporaryDirectory(prefix='settings-migration-') as temporary:
            root = Path(temporary)
            default = root / 'default'
            home = root / 'bots' / 'bot' / 'home'
            default.mkdir()
            home.mkdir(parents=True)
            (default / 'config.yaml').write_text(json.dumps({
                'model': {'default': 'gpt-6-sol', 'provider': 'openai-api', 'base_url': 'https://api.openai.com/v1'},
                'approvals': {'mode': 'off'},
                'fallback_providers': [{'provider': 'openai-api', 'model': 'gpt-6-astra', 'key_env': 'OPENAI_API_KEY'},
                                       {'provider': 'anthropic', 'model': 'other'}],
                'mcp_servers': {'gumclaw': {'url': 'https://mcp.example.com/mcp',
                                            'headers': {'Authorization': 'Bearer ${MCP_GUMCLAW_API_KEY}'}}}}))
            (default / '.env').write_text('OPENAI_API_KEY=synthetic-openai\nMCP_GUMCLAW_API_KEY=synthetic-mcp\n')
            custom = str(root / 'toolchains.sh')
            original = {'toolsets': ['kanban'], 'terminal': {'cwd': str(home.parent / 'workspace'), 'shell_init_files': [
                str(home / '.profile'), custom, str(home / 'coconut-terminal.sh'), str(home / 'coconut-google.sh')]},
                'delegation': {'max_concurrent_children': None, 'worktree_isolation': True}}
            (home / 'config.yaml').write_text(json.dumps(original))
            for name in ('coconut-profile.json', '.coconut-kanban-tools', '.coconut-native-config.json',
                         'coconut-terminal.sh', 'coconut-google.sh'):
                (home / name).write_text('{}\n')
            (home / 'SOUL.md').write_text('Keep replies brief.')
            (home / '.config/gh').mkdir(parents=True)
            (home / '.config/gh/hosts.yml').write_text('github.com:\n  user: bot\n')
            (home / 'coconut-runs').mkdir()
            (home / 'coconut-runs/job.json').write_text('{"phase": "idle"}')
            env = dict(os.environ, HERMES_HOME=str(default),
                       PYTHONPATH=os.pathsep.join([str(Path(SOURCE).resolve()), str(BACKEND), str(BACKEND / 'coconut')]))
            for key in ('OPENAI_API_KEY', 'MCP_GUMCLAW_API_KEY', 'GH_CONFIG_DIR', 'XDG_CONFIG_HOME', 'CODEX_HOME'):
                env.pop(key, None)
            result = subprocess.run([PYTHON, '-c', MIGRATION, str(root)], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-6000:])
            state = json.loads(result.stdout.strip().splitlines()[-1])
            config, report = state['config'], state['report']
            self.assertEqual(config['approvals']['mode'], 'off')
            self.assertEqual(config['model']['default'], 'gpt-6-sol')
            self.assertEqual(config['model']['provider'], 'openai-api')
            self.assertEqual([entry['provider'] for entry in config['fallback_providers']], ['openai-api'])
            self.assertEqual(config['mcp_servers']['gumclaw']['url'], 'https://mcp.example.com/mcp')
            self.assertNotIn('synthetic-mcp', json.dumps(config))
            self.assertEqual(config['terminal']['shell_init_files'], [str(home / '.profile'), custom])
            self.assertEqual(config['toolsets'], ['kanban'])
            self.assertEqual(state['env']['OPENAI_API_KEY'], 'synthetic-openai')
            self.assertEqual(state['env']['MCP_GUMCLAW_API_KEY'], 'synthetic-mcp')
            self.assertEqual(state['env']['GH_CONFIG_DIR'], str(home / '.config/gh'))
            self.assertEqual(state['env']['XDG_CONFIG_HOME'], str(home / '.config'))
            self.assertNotIn('CODEX_HOME', state['env'])
            for name in ('coconut-profile.json', '.coconut-kanban-tools', '.coconut-native-config.json',
                         'coconut-terminal.sh', 'coconut-google.sh', 'coconut-runs'):
                self.assertNotIn(name, state['files'])
            self.assertIn('SOUL.md', state['files'])
            self.assertEqual(state['runs'], ['job.json'])
            self.assertIn('config.yaml', state['backup'])
            self.assertIn('coconut-profile.json', state['backup'])
            self.assertEqual(json.loads((home.parent / 'settings-migration/backup/config.yaml').read_text()), original)
            self.assertEqual(report['status'], 'done')
            self.assertEqual(report['mcp']['added'], ['gumclaw'])
            self.assertEqual(report['model']['status'], 'copied')
            self.assertEqual(report['login_folders'], {'GH_CONFIG_DIR': str(home / '.config/gh'),
                                                       'XDG_CONFIG_HOME': str(home / '.config')})
            self.assertEqual(report['runs'], {'moved': 1, 'kept': []})
            self.assertEqual(json.loads((home.parent / 'settings-migration/report.json').read_text()), report)

    def test_a_default_model_with_a_sign_in_stays_in_the_default_home(self):
        with tempfile.TemporaryDirectory(prefix='settings-migration-') as temporary:
            root = Path(temporary)
            default = root / 'default'
            home = root / 'bots' / 'bot' / 'home'
            default.mkdir()
            home.mkdir(parents=True)
            (default / 'config.yaml').write_text(json.dumps({'model': {'default': 'gpt-6-astra', 'provider': 'openai-codex'}}))
            (default / 'auth.json').write_text(json.dumps({'version': 1, 'providers': {'openai-codex': {
                'tokens': {'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh'}}}}))
            (home / 'config.yaml').write_text('{}')
            env = dict(os.environ, HERMES_HOME=str(default),
                       PYTHONPATH=os.pathsep.join([str(Path(SOURCE).resolve()), str(BACKEND), str(BACKEND / 'coconut')]))
            result = subprocess.run([PYTHON, '-c', MIGRATION, str(root)], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-6000:])
            state = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(state['report']['model'], {'status': 'uses_default_sign_in', 'provider': 'openai-codex'})
            self.assertNotIn('model', state['config'])
            self.assertNotIn('auth.json', state['files'])
            self.assertNotIn('synthetic', result.stdout)


def _yaml(path):
    """Read config.yaml after a stopped attempt. Hermes saves YAML, and the test process has no YAML package."""
    result = subprocess.run([PYTHON, '-c', 'import json, sys\nfrom ruamel.yaml import YAML\n'
                             'print(json.dumps(YAML(typ="safe", pure=True).load(open(sys.argv[1]))))', str(path)],
                            capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def _env_file(path):
    from coconut.runtime_environment import _env_values
    return dict(_env_values(path)) if path.exists() else {}


ATTEMPT = r"""
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
default, home = root / 'default', root / 'bots' / 'bot' / 'home'
folder = home.parent / 'settings-migration'
import model_catalog
model_catalog.apply_env_file(default / '.env')
STEP
from settings_migration import _fallback_keys, migrate
from agent.secret_scope import load_env_file
from native_settings import raw_config
report = migrate(default, {'bot': home})['bot']
config = raw_config(home)
print(json.dumps(dict(report=report, config=config, env=load_env_file(home / '.env'), done=(folder / 'report.json').exists(),
                      keys=_fallback_keys(home) if 'fallback_providers' in config else None, default_keys=_fallback_keys(default),
                      leftovers=sorted(path.name for path in folder.iterdir() if path.name not in ('backup', 'report.json')))))
"""


@needs_hermes
class NativeFallbackMigrationTests(unittest.TestCase):
    route = {'provider': 'openai-api', 'model': 'gpt-6-astra', 'base_url': 'https://api.openai.com/v1', 'key_env': 'OPENAI_API_KEY'}
    secrets = {'command': {'enabled': True, 'command': 'cat SECRET_FILE'}}
    # Stop the process between the variable writes and the config.yaml write of the profile.
    STOP = ("import settings_migration\n"
            "original = settings_migration.save_config\n"
            "settings_migration.save_config = lambda target, config: os._exit(3) if Path(target) == home else original(target, config)\n")

    def attempts(self, steps, default_config, default_env, bot_config, bot_env):
        """Run attempts of the step in separate processes. Each step is Python code that runs before migrate."""
        with tempfile.TemporaryDirectory(prefix='settings-migration-') as temporary:
            root = Path(temporary)
            default, home = root / 'default', root / 'bots' / 'bot' / 'home'
            default.mkdir()
            home.mkdir(parents=True)
            secret = root / 'secret.env'
            secret.write_text('OPENAI_API_KEY=synthetic-secret-source\n')
            (default / 'config.yaml').write_text(json.dumps(default_config).replace('SECRET_FILE', str(secret)))
            (default / '.env').write_text(default_env)
            (home / 'config.yaml').write_text(json.dumps(bot_config).replace('SECRET_FILE', str(secret)))
            if bot_env:
                (home / '.env').write_text(bot_env)
            env = {key: value for key, value in os.environ.items() if key != 'OPENAI_API_KEY'}
            env.update(HERMES_HOME=str(default),
                       PYTHONPATH=os.pathsep.join([str(Path(SOURCE).resolve()), str(BACKEND), str(BACKEND / 'coconut')]))
            results = []
            for step in steps:
                result = subprocess.run([PYTHON, '-c', ATTEMPT.replace('STEP', step), str(root)], env=env,
                                        capture_output=True, text=True, timeout=60)
                self.assertIn(result.returncode, (0, 3), result.stderr[-6000:])
                if result.returncode == 3:
                    results.append(dict(stopped=True, config=json.loads(json.dumps(_yaml(home / 'config.yaml'))),
                                        env=_env_file(home / '.env')))
                else:
                    results.append(json.loads(result.stdout.strip().splitlines()[-1]))
            return results if len(results) > 1 else results[0]

    def test_a_bot_with_its_own_key_keeps_it_and_the_route_keeps_the_default_key(self):
        state = self.attempts([''], {'fallback_providers': [self.route]}, 'OPENAI_API_KEY=synthetic-default\n',
                              {}, 'OPENAI_API_KEY=synthetic-bot\n')
        self.assertEqual(state['report']['fallback']['status'], 'copied')
        self.assertEqual(state['env']['OPENAI_API_KEY'], 'synthetic-bot')
        self.assertEqual(state['config']['fallback_providers'][0]['key_env'], 'COCONUT_FALLBACK_OPENAI_API_KEY')
        self.assertEqual(state['keys'], ['synthetic-default'])
        self.assertEqual(state['keys'], state['default_keys'])
        self.assertEqual(state['report']['fallback']['renamed_variables'], {'OPENAI_API_KEY': 'COCONUT_FALLBACK_OPENAI_API_KEY'})
        self.assertEqual(state['leftovers'], [])

    def test_a_key_from_secret_sources_keeps_its_source(self):
        state = self.attempts([''], {'fallback_providers': [self.route], 'secrets': self.secrets}, '', {}, '')
        self.assertEqual(state['report']['fallback']['status'], 'copied')
        self.assertIn('secrets', state['config'])
        self.assertEqual(state['keys'], ['synthetic-secret-source'])

    def test_the_key_alias_and_env_references_are_renamed(self):
        route = {'provider': 'openai-api', 'model': 'gpt-6-astra', 'base_url': 'https://api.openai.com/v1',
                 'api_key_env': 'FALLBACK_KEY', 'extra_headers': {'X-Key': '${env:FALLBACK_KEY}'}}
        state = self.attempts([''], {'fallback_providers': [route]}, 'FALLBACK_KEY=synthetic-default\n',
                              {}, 'FALLBACK_KEY=synthetic-bot\n')
        route = state['config']['fallback_providers'][0]
        self.assertEqual(route['api_key_env'], 'COCONUT_FALLBACK_FALLBACK_KEY')
        self.assertNotIn('key_env', route)
        self.assertEqual(route['extra_headers'], {'X-Key': '${COCONUT_FALLBACK_FALLBACK_KEY}'})
        self.assertEqual(state['keys'], ['synthetic-default'])

    def test_failed_checks_write_nothing_for_the_fallback_and_the_step_continues(self):
        own = {'secrets': {'command': {'enabled': False, 'command': 'true'}}}
        state = self.attempts([''], {'fallback_providers': [self.route], 'secrets': self.secrets,
                                     'approvals': {'mode': 'off'}}, '', own, 'OWN_VALUE=kept\n')
        fallback = state['report']['fallback']
        self.assertEqual(fallback['status'], 'not_copied')
        self.assertIn('would not resolve', fallback['reason'])
        self.assertNotIn('fallback_providers', state['config'])
        self.assertEqual(state['config']['secrets'], own['secrets'])
        self.assertEqual(state['env'], {'OWN_VALUE': 'kept'})
        # The other parts of the step still ran, and the bot is done.
        self.assertEqual(state['config']['approvals'], {'mode': 'off'})
        self.assertTrue(state['done'])
        self.assertEqual(state['leftovers'], [])

    def test_secret_sources_that_would_change_a_bot_key_are_not_copied(self):
        state = self.attempts([''], {'fallback_providers': [self.route], 'secrets': self.secrets}, '', {},
                              'OPENAI_API_KEY=synthetic-bot\n')
        self.assertEqual(state['report']['fallback']['status'], 'not_copied')
        self.assertNotIn('secrets', state['config'])
        self.assertNotIn('fallback_providers', state['config'])
        self.assertEqual(state['env'], {'OPENAI_API_KEY': 'synthetic-bot'})
        self.assertTrue(state['done'])

    def test_a_stop_between_the_two_writes_repeats_them_with_the_same_values(self):
        stopped, retried = self.attempts([self.STOP, ''], {'fallback_providers': [self.route]},
                                         'OPENAI_API_KEY=synthetic-default\n', {}, 'OPENAI_API_KEY=synthetic-bot\n')
        self.assertTrue(stopped['stopped'])
        self.assertEqual(stopped['env']['COCONUT_FALLBACK_OPENAI_API_KEY'], 'synthetic-default')
        self.assertNotIn('fallback_providers', stopped['config'])
        self.assertTrue(retried['done'])
        self.assertEqual(retried['env'], {'OPENAI_API_KEY': 'synthetic-bot', 'COCONUT_FALLBACK_OPENAI_API_KEY': 'synthetic-default'})
        self.assertEqual(retried['config']['fallback_providers'][0]['key_env'], 'COCONUT_FALLBACK_OPENAI_API_KEY')
        self.assertEqual(retried['keys'], ['synthetic-default'])

    def test_an_explicit_empty_fallback_list_stays(self):
        state = self.attempts([''], {'fallback_providers': [self.route]}, 'OPENAI_API_KEY=synthetic-default\n',
                              {'fallback_providers': []}, '')
        self.assertEqual(state['config']['fallback_providers'], [])
        self.assertEqual(state['report']['fallback'], {})
        self.assertNotIn('OPENAI_API_KEY', state['env'])

    def test_a_bot_with_its_own_key_keeps_using_it(self):
        with tempfile.TemporaryDirectory(prefix='settings-migration-') as temporary:
            root = Path(temporary)
            default, profile = root / 'default', root / 'profile'
            for folder in (default, profile):
                folder.mkdir()
            (default / '.anthropic_oauth.json').write_text('{}')
            (profile / '.env').write_text('ANTHROPIC_API_KEY=synthetic-bot\n')
            code = ('import json, sys\nfrom pathlib import Path\nfrom model_catalog import sign_in_home\n'
                    'd, p = Path(sys.argv[1]), Path(sys.argv[2])\n'
                    'print(json.dumps(str(sign_in_home("anthropic", p, (d,)))))')
            env = dict(os.environ, HERMES_HOME=str(default),
                       PYTHONPATH=os.pathsep.join([str(Path(SOURCE).resolve()), str(BACKEND / 'coconut')]))
            result = subprocess.run([PYTHON, '-c', code, str(default), str(profile)], env=env, capture_output=True,
                                    text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-6000:])
            self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]), str(profile))

    def test_a_route_with_a_sign_in_of_the_default_home_resolves_there(self):
        with tempfile.TemporaryDirectory(prefix='settings-migration-') as temporary:
            root = Path(temporary)
            default, profile = root / 'default', root / 'profile'
            for folder in (default, profile):
                folder.mkdir()
            (default / 'auth.json').write_text(json.dumps({'version': 1, 'providers': {'openai-codex': {
                'tokens': {'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh'}}}}))
            code = ('import json, sys\nfrom pathlib import Path\nfrom model_catalog import sign_in_home\n'
                    'd, p = Path(sys.argv[1]), Path(sys.argv[2])\n'
                    'print(json.dumps([str(sign_in_home("openai-codex", p, (d,))), str(sign_in_home("openai-api", p, (d,)))]))')
            env = dict(os.environ, HERMES_HOME=str(default),
                       PYTHONPATH=os.pathsep.join([str(Path(SOURCE).resolve()), str(BACKEND / 'coconut')]))
            result = subprocess.run([PYTHON, '-c', code, str(default), str(profile)], env=env, capture_output=True,
                                    text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-6000:])
            self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]), [str(default), str(profile)])
            self.assertFalse((profile / 'auth.json').exists())

if __name__ == '__main__':
    unittest.main()
