import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from coconut.terminal_setup import SHELL_INIT, command_path, configure_terminal


class TerminalSetupTests(unittest.TestCase):
    def configure(self, root, config):
        with patch.dict(os.environ), patch('coconut.native_settings.raw_config', return_value=config), \
             patch('coconut.native_settings.save_config') as save:
            configure_terminal(root)
        return save

    def test_new_install_preserves_native_default_init_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = {'terminal': {'cwd': '/workspace'}, 'model': 'native-model'}
            save = self.configure(home, config)
            self.assertEqual(config['terminal']['shell_init_files'],
                             [str(home / name) for name in ('.profile', '.bash_profile', '.bashrc', 'coconut-terminal.sh')])
            self.assertEqual(config['model'], 'native-model')
            self.assertEqual(config['terminal']['cwd'], '/workspace')
            save.assert_called_once_with(home, config)
            self.assertTrue((home / 'coconut-terminal.sh').read_text().startswith(SHELL_INIT))

    def test_upgrade_preserves_custom_init_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = {'terminal': {'shell_init_files': ['/custom/startup.sh'], 'auto_source_bashrc': False}}
            self.configure(home, config)
            self.assertEqual(config['terminal']['shell_init_files'], ['/custom/startup.sh', str(home / 'coconut-terminal.sh')])
            self.configure(home, config).assert_not_called()

    def test_null_terminal_settings_use_native_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'terminal': None}
            self.configure(Path(directory), config)
            self.assertEqual(config['terminal']['shell_init_files'][0], str(Path(directory) / '.profile'))

    def test_disabled_auto_source_stays_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'terminal': {'auto_source_bashrc': False}}
            self.configure(Path(directory), config)
            self.assertEqual(len(config['terminal']['shell_init_files']), 1)

    def test_shell_restores_package_priority_after_system_login_and_preserves_custom_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            # Real shell execution verifies quoting, order, and repeat sourcing.
            home = Path(directory)
            brew = home / 'package tools'
            brew.mkdir()
            script = home / 'init.sh'
            script.write_text(SHELL_INIT.replace('/opt/homebrew/bin', '"' + str(brew) + '"'))
            result = subprocess.run(['/bin/bash', '-lc', 'PATH="/usr/bin:/bin:$1:/custom/tools"; source "$2"; source "$2"; printf "%s" "$PATH"', 'test', str(brew), str(script)],
                                    capture_output=True, text=True, check=True, env={'HOME': directory, 'PATH': '/usr/bin:/bin'})
            entries = result.stdout.split(':')
            self.assertEqual(entries[0], str(brew))
            self.assertEqual(entries.count(str(brew)), 1)
            self.assertIn('/custom/tools', entries)
            self.assertIn('/usr/bin', entries)

    def test_command_path_keeps_custom_entries_and_removes_empty_entries(self):
        with patch.object(Path, 'is_dir', return_value=True):
            result = command_path('/usr/bin:/custom/bin::/opt/homebrew/bin:/custom/bin')
        self.assertEqual(result.split(':')[0], '/opt/homebrew/bin')
        self.assertEqual(result.split(':').count('/custom/bin'), 1)
        self.assertNotIn('', result.split(':'))

    def test_all_bots_get_the_shared_github_hook(self):
        with tempfile.TemporaryDirectory(prefix='github profile ') as directory:
            root = Path(directory)
            token = root / 'connections/github/token'
            home = root / 'bots/desk/home'
            home.mkdir(parents=True)
            config = {'terminal': {'shell_init_files': ['/custom/startup.sh']}}
            with patch.dict(os.environ, {'COCONUT_GITHUB_TOKEN_FILE': str(token)}):
                self.configure(home, config)
                self.assertEqual(config['terminal']['shell_init_files'],
                                 ['/custom/startup.sh', str(home / 'coconut-terminal.sh'), str(home / 'coconut-github.sh')])
                script = (home / 'coconut-github.sh').read_text()
                self.assertIn(f"cat '{token}'", script)
                self.assertNotIn('gh auth', script)
                self.configure(home, config).assert_not_called()
            self.assertFalse(token.exists())

    def test_all_bots_get_the_same_google_hook_before_connection(self):
        with tempfile.TemporaryDirectory(prefix='google profile ') as directory:
            root = Path(directory)
            google_home = root / 'connections/google/home'
            for name in ('desk', 'tuner'):
                home = root / 'bots' / name / 'home'
                home.mkdir(parents=True)
                config = {'terminal': {'shell_init_files': ['/custom/startup.sh']}}
                with patch.dict(os.environ, {'COCONUT_GOOGLE_HOME': str(google_home)}):
                    self.configure(home, config)
                    self.assertEqual(config['terminal']['shell_init_files'][-2:],
                                     [str(home / 'coconut-terminal.sh'), str(home / 'coconut-google.sh')])
                    script = (home / 'coconut-google.sh').read_text()
                    self.assertIn('google_connection.py', script)
                    self.assertIn(str(google_home), script)
                    self.configure(home, config).assert_not_called()
            self.assertFalse(google_home.exists())
