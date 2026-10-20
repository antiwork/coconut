import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coconut.runtime_environment import CLAUDE_PROFILE_OVERRIDES, prepare_profile, profile_environment
from coconut.profiles import ProfileError, merge_runtime_config
from coconut.terminal_setup import configure_terminal


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_account_home_does_not_depend_on_inherited_home(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'HOME': '/wrong/home', 'USER': 'wrong-user'}):
            profile = Path(directory)
            env = prepare_profile(profile)
            self.assertEqual(env['HOME'], pwd.getpwuid(os.getuid()).pw_dir)
            self.assertEqual(env['HERMES_REAL_HOME'], env['HOME'])
            self.assertEqual(env['USER'], pwd.getpwuid(os.getuid()).pw_name)
            for key in ('CODEX_HOME', 'GH_CONFIG_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME'):
                self.assertTrue(Path(env[key]).is_relative_to(profile))
                self.assertEqual(Path(env[key]).stat().st_mode & 0o777, 0o700)

    def test_claude_uses_the_account_default_without_moving_old_bot_state(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            config = '{"hasCompletedOnboarding": true, "mcpServers": {"custom": {}}}'
            (profile / '.claude.json').write_text(config)
            (profile / '.claude').mkdir()
            old = profile / '.claude/.claude.json'
            old.write_text('{"existing":true}')
            (profile / '.codex').mkdir()
            saved = profile / '.codex/auth.json'
            saved.write_text('existing login')
            env = prepare_profile(profile)
            self.assertEqual(saved.read_text(), 'existing login')
            for key in CLAUDE_PROFILE_OVERRIDES:
                self.assertNotIn(key, env)
            prepare_profile(profile)
            self.assertEqual(old.read_text(), '{"existing":true}')
            self.assertEqual((profile / '.claude.json').read_text(), config)

    def test_profile_preparation_does_not_create_or_modify_account_claude_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account, outside, profile = root / 'account', root / 'outside', root / 'profile'
            account.mkdir(); outside.mkdir()
            saved = outside / '.credentials.json'
            saved.write_text('native saved state')
            (account / '.claude').symlink_to(outside)
            with patch('coconut.runtime_environment.pwd.getpwuid', return_value=SimpleNamespace(pw_dir=str(account), pw_name='account')):
                env = prepare_profile(profile)
            self.assertEqual(env['HOME'], str(account))
            self.assertNotIn('CLAUDE_CONFIG_DIR', env)
            self.assertTrue((account / '.claude').is_symlink())
            self.assertEqual(saved.read_text(), 'native saved state')
            self.assertEqual(list(outside.iterdir()), [saved])

    def test_profile_links_cannot_redirect_tool_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile, outside = root / 'profile', root / 'outside'
            profile.mkdir(); outside.mkdir()
            (profile / '.codex').symlink_to(outside)
            with self.assertRaises(ProfileError):
                prepare_profile(profile)
            self.assertEqual(list(outside.iterdir()), [])

    def test_profile_credentials_cannot_replace_runtime_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            profile = Path(directory)
            env = prepare_profile(profile)
            os.environ.update(env)
            values = dict(CLAUDE_CONFIG_DIR='/other', CLAUDE_SECURESTORAGE_CONFIG_DIR='/other',
                          HOME='/other', USER='other-user', CODEX_HOME='/other', CUSTOM_API_KEY='profile-value')
            os.environ.update({key: '/old/profile' for key in CLAUDE_PROFILE_OVERRIDES})
            with patch.dict(sys.modules, {'dotenv': SimpleNamespace(dotenv_values=lambda _: values)}):
                merge_runtime_config(profile)
            for key, value in env.items():
                self.assertEqual(os.environ[key], value)
            for key in CLAUDE_PROFILE_OVERRIDES:
                self.assertNotIn(key, os.environ)
            self.assertEqual(os.environ['CUSTOM_API_KEY'], 'profile-value')

    def test_native_shell_init_restores_account_home_and_tool_scope(self):
        with tempfile.TemporaryDirectory(prefix="coconut's profile ") as directory, patch.dict(os.environ):
            profile = Path(directory)
            config = {'terminal': {'shell_init_files': ['~/.bashrc']}}
            with patch('coconut.native_settings.raw_config', return_value=config), patch('coconut.native_settings.save_config'):
                configure_terminal(profile)
            self.assertEqual(config['terminal']['shell_init_files'][0], str(profile / '.bashrc'))
            command = 'export HOME=/wrong CLAUDE_CONFIG_DIR=/wrong CLAUDE_SECURESTORAGE_CONFIG_DIR=/wrong CODEX_HOME=/wrong; source "$1"; /usr/bin/env'
            result = subprocess.run(['/bin/bash', '-c', command, 'test', str(profile / 'coconut-terminal.sh')],
                                    capture_output=True, text=True, check=True)
            values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            for key, value in profile_environment(profile).items():
                self.assertEqual(values[key], value)
            for key in CLAUDE_PROFILE_OVERRIDES:
                self.assertNotIn(key, values)
