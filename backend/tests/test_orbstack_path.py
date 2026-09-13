import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from coconut.terminal_setup import SHELL_INIT, command_path


class OrbStackPathTests(unittest.TestCase):
    def test_installed_cli_is_available_in_processes_and_native_shells(self):
        with tempfile.TemporaryDirectory(prefix='account home ') as directory:
            home = Path(directory)
            binary = home / '.orbstack/bin/coconut-orb-path-check'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nprintf orb-path-ok')
            binary.chmod(0o700)
            with patch.object(Path, 'home', return_value=home):
                process_path = command_path('/usr/bin:/bin')
            env = dict(os.environ, HOME=directory, PATH=process_path)
            result = subprocess.run([binary.name], env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, 'orb-path-ok')
            env['PATH'] = '/usr/bin:/bin'
            result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c',
                                     SHELL_INIT + SHELL_INIT + '\ncoconut-orb-path-check'],
                                    env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, 'orb-path-ok')

    def test_missing_install_does_not_add_a_path_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(Path, 'home', return_value=home):
                self.assertNotIn(str(home / '.orbstack/bin'), command_path('/usr/bin:/bin').split(':'))
            result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c',
                                     SHELL_INIT + '\nprintf "%s" "$PATH"'],
                                    env=dict(os.environ, HOME=directory, PATH='/usr/bin:/bin'),
                                    text=True, capture_output=True, check=True)
            self.assertNotIn(str(home / '.orbstack/bin'), result.stdout.split(':'))
