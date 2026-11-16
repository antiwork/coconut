import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coconut.profiles import ProfileError
from coconut.terminal_setup import SHELL_INIT, command_path, configure_terminal


def native_shell(files):
    """A stand-in for Hermes's tools.environments.local, which reads the profile's own shell_init_files."""
    local = SimpleNamespace(_resolve_shell_init_files=lambda: list(files))
    environments = SimpleNamespace(local=local)
    return local, {'tools': SimpleNamespace(environments=environments), 'tools.environments': environments,
                   'tools.environments.local': local}


class TerminalSetupTests(unittest.TestCase):
    def configure(self, home, files=()):
        local, modules = native_shell(files)
        with patch.dict(os.environ), patch.dict(sys.modules, modules):
            configure_terminal(home)
            return local._resolve_shell_init_files()

    def test_the_profile_stays_unchanged_and_its_own_files_come_first(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'bots' / 'bot' / 'home'
            home.mkdir(parents=True)
            config = home / 'config.yaml'
            config.write_text('terminal:\n  shell_init_files: [/custom/startup.sh]\n')
            before = config.read_bytes()
            script = home.parent / 'terminal' / 'coconut-terminal.sh'
            for _ in range(2):
                self.assertEqual(self.configure(home, ['/custom/startup.sh']), ['/custom/startup.sh', str(script)])
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(sorted(path.name for path in home.iterdir()), ['config.yaml'])
            self.assertTrue(script.read_text().startswith(SHELL_INIT))

    def test_a_listed_script_is_not_added_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            home.mkdir()
            script = str(Path(directory) / 'terminal' / 'coconut-terminal.sh')
            self.assertEqual(self.configure(home, [script]), [script])

    def test_a_linked_terminal_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'home').mkdir()
            (root / 'outside').mkdir()
            (root / 'terminal').symlink_to(root / 'outside', target_is_directory=True)
            with self.assertRaises(ProfileError):
                self.configure(root / 'home')
            self.assertEqual(list((root / 'outside').iterdir()), [])

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
            with patch.dict(os.environ, {'COCONUT_GITHUB_TOKEN_FILE': str(token)}):
                folder = home.parent / 'terminal'
                files = self.configure(home, ['/custom/startup.sh'])
                self.assertEqual(files, ['/custom/startup.sh', str(folder / 'coconut-terminal.sh'), str(folder / 'coconut-github.sh')])
                script = (folder / 'coconut-github.sh').read_text()
                self.assertIn(f"cat '{token}'", script)
                self.assertNotIn('gh auth', script)
                self.assertEqual(self.configure(home, ['/custom/startup.sh']), files)
                self.assertFalse((home / 'coconut-github.sh').exists())
            self.assertFalse(token.exists())

    def test_all_bots_get_the_same_google_hook_before_connection(self):
        with tempfile.TemporaryDirectory(prefix='google profile ') as directory:
            root = Path(directory)
            google_home = root / 'connections/google/home'
            for name in ('desk', 'tuner'):
                home = root / 'bots' / name / 'home'
                home.mkdir(parents=True)
                folder = home.parent / 'terminal'
                with patch.dict(os.environ, {'COCONUT_GOOGLE_HOME': str(google_home)}):
                    files = self.configure(home, ['/custom/startup.sh'])
                self.assertEqual(files, ['/custom/startup.sh', str(folder / 'coconut-terminal.sh'), str(folder / 'coconut-google.sh')])
                script = (folder / 'coconut-google.sh').read_text()
                self.assertIn('google_connection.py', script)
                self.assertIn(str(google_home), script)
            self.assertFalse(google_home.exists())
