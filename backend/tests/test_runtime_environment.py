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
from coconut.terminal_setup import configure_terminal


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_account_home_does_not_depend_on_inherited_home(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'HOME': '/wrong/home', 'USER': 'wrong-user'}):
            profile = Path(directory)
            env = prepare_profile(profile)
            self.assertEqual(env['HOME'], pwd.getpwuid(os.getuid()).pw_dir)
            self.assertEqual(env['HERMES_REAL_HOME'], env['HOME'])
            self.assertEqual(env['USER'], pwd.getpwuid(os.getuid()).pw_name)

    def test_tools_use_the_account_folders_and_the_profile_stays_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            env = prepare_profile(profile)
            for key in ('CODEX_HOME', 'GH_CONFIG_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME'):
                self.assertNotIn(key, env)
            self.assertEqual(list(profile.iterdir()), [])

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

    def test_native_shell_init_restores_account_home_and_tool_scope(self):
        with tempfile.TemporaryDirectory(prefix="coconut's profile ") as directory, patch.dict(os.environ):
            profile = Path(directory) / 'home'
            profile.mkdir()
            local = SimpleNamespace(_resolve_shell_init_files=lambda: [])
            environments = SimpleNamespace(local=local)
            with patch.dict(sys.modules, {'tools': SimpleNamespace(environments=environments),
                                          'tools.environments': environments, 'tools.environments.local': local}):
                configure_terminal(profile)
            script = Path(directory) / 'terminal' / 'coconut-terminal.sh'
            command = 'export HOME=/wrong CLAUDE_CONFIG_DIR=/wrong CLAUDE_SECURESTORAGE_CONFIG_DIR=/wrong; source "$1"; /usr/bin/env'
            result = subprocess.run(['/bin/bash', '-c', command, 'test', str(script)],
                                    capture_output=True, text=True, check=True)
            values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            for key, value in profile_environment(profile).items():
                self.assertEqual(values[key], value)
            for key in CLAUDE_PROFILE_OVERRIDES:
                self.assertNotIn(key, values)



ENV_FILE = """# A comment line
export GH_CONFIG_DIR=${BOT_HOME}/.config/gh   # an inline comment
CODEX_HOME="${BOT_HOME}/.codex"
XDG_CONFIG_HOME='/quoted${BOT_HOME}'
XDG_DATA_HOME=${MISSING_FOLDER:-/fallback/share}
XDG_CACHE_HOME=relative/cache
OPENAI_API_KEY=not-a-folder
"""
# Cases where a small parser often differs from python-dotenv. The keys are not login folders, so the test compares
# the parser itself.
EDGE_CASES = """C="/a\\"b"
E=
F=${E:-/f}
G="/first
second"
'K'=/k
H=#not-a-comment-value
I = /spaced   # comment
J='/single\\'s'
"""


class LoginFolderTests(unittest.TestCase):
    def expected(self, home):
        return {'GH_CONFIG_DIR': str(home / '.config/gh'), 'CODEX_HOME': str(home / '.codex'),
                'XDG_CONFIG_HOME': '/quoted' + str(home), 'XDG_DATA_HOME': '/fallback/share'}

    def test_the_service_parser_follows_the_dotenv_rules(self):
        from coconut.runtime_environment import _env_values, login_folders
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BOT_HOME': directory}), \
                patch.dict(sys.modules, {'dotenv': None}):
            os.environ.pop('MISSING_FOLDER', None)
            home = Path(directory)
            (home / '.env').write_text(ENV_FILE)
            self.assertEqual(login_folders(home), self.expected(home))
            (home / 'edge.env').write_text(EDGE_CASES)
            values = _env_values(home / 'edge.env')
            self.assertEqual(values['C'], '/a"b')
            self.assertEqual(values['F'], '')
            self.assertEqual(values['G'], '/first\nsecond')
            self.assertEqual(values['K'], '/k')
            self.assertEqual(values['I'], '/spaced')
            self.assertEqual(values['H'], '#not-a-comment-value')

    def test_the_service_parser_matches_python_dotenv(self):
        try:
            from dotenv import dotenv_values
        except ImportError:
            self.skipTest('python-dotenv is part of the Hermes environment only.')
        from coconut.runtime_environment import LOGIN_FOLDERS, _env_values, login_folders
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BOT_HOME': directory}):
            os.environ.pop('MISSING_FOLDER', None)
            home = Path(directory)
            (home / '.env').write_text(ENV_FILE)
            (home / 'edge.env').write_text(EDGE_CASES)
            native = {key: value for key, value in dotenv_values(home / '.env').items()
                      if key in LOGIN_FOLDERS and value.startswith('/')}
            self.assertEqual(native, self.expected(home))
            with patch.dict(sys.modules, {'dotenv': None}):
                self.assertEqual(login_folders(home), native)
                self.assertEqual(_env_values(home / 'edge.env'), dict(dotenv_values(home / 'edge.env')))
