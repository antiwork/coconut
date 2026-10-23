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
env_bytes = (home / '.env').read_bytes()
again = migrate(default, {'bot': home})['bot']
assert again == first, (again, first)
# A step that stopped before its report runs again and finds nothing left to write.
(home.parent / 'settings-migration' / 'report.json').unlink()
rerun = migrate(default, {'bot': home})['bot']
assert rerun['mcp']['added'] == [] and rerun['approvals'] == {} and rerun['login_folders'] == {}, rerun
assert (home / 'config.yaml').read_bytes() == config_bytes
assert (home / '.env').read_bytes() == env_bytes
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


if __name__ == '__main__':
    unittest.main()
