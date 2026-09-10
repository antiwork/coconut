import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from coconut.hermes_contract import require_keywords


SPEC = importlib.util.spec_from_file_location('hermes_compatibility_installer',
                                            Path(__file__).resolve().parents[2] / 'scripts/hermes.py')
hermes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hermes)


class KeywordContractTests(unittest.TestCase):
    def test_removed_keyword_fails_even_with_arbitrary_kwargs(self):
        def changed(model=None, **kwargs):
            pass
        with self.assertRaisesRegex(ValueError, 'reasoning_config'):
            require_keywords(changed, {'model', 'reasoning_config'})

    def test_positional_only_replacement_is_incompatible(self):
        def changed(model, /):
            pass
        with self.assertRaisesRegex(ValueError, 'model'):
            require_keywords(changed, {'model'})

    def test_supported_keywords_allow_unrelated_new_parameters(self):
        def current(model=None, reasoning_config=None, future_option=None):
            pass
        require_keywords(current, {'model', 'reasoning_config'})

    def test_check_process_blocks_network_before_loading_the_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / 'coconut'
            package.mkdir()
            (package / '__init__.py').write_text('')
            (package / 'hermes_contract.py').write_text(
                'import socket\n'
                'def verify_contract():\n'
                '    socket.getaddrinfo("network-must-not-run.invalid", 443)\n')
            result = subprocess.run([sys.executable, '-c', hermes.CHECK_CODE], cwd=temporary,
                                    env=dict(os.environ, PYTHONPATH=temporary), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('cannot use the network', result.stderr)


class SourceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        runtime = self.root / '.runtime'
        managed = runtime / 'hermes'
        self.source = managed / 'source'
        self.source.mkdir(parents=True)
        self.manifest = managed / 'install.json'
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        for name, value in dict(ROOT=self.root, RUNTIME=runtime, HERMES=managed, SOURCE=self.source,
                                VENV=managed / 'venv', PYTHON=Path(sys.executable), MANIFEST=self.manifest).items():
            replacement = patch.object(hermes, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.git('init', '--quiet')
        self.git('config', 'user.name', 'Compatibility test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('remote', 'add', 'origin', hermes.REPOSITORY)
        (self.source / 'run_agent.py').write_text('# Upstream fixture.\n')
        self.git('add', 'run_agent.py')
        self.git('commit', '--quiet', '-m', 'Create upstream fixture')
        self.revision = self.git('rev-parse', 'HEAD')
        self.manifest.write_text(json.dumps({'repository': hermes.REPOSITORY, 'commit': self.revision}))

    def git(self, *arguments):
        return subprocess.run(['git', *arguments], cwd=self.source, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_clean_official_checkout_matches_recorded_revision(self):
        self.assertEqual(hermes.check_source(self.env), self.revision)

    def test_tracked_source_edit_is_rejected_without_reverting_it(self):
        path = self.source / 'run_agent.py'
        path.write_text('# Local change.\n')
        with self.assertRaisesRegex(SystemExit, 'local changes'):
            hermes.check_source(self.env)
        self.assertEqual(path.read_text(), '# Local change.\n')

    def test_staged_patch_is_rejected(self):
        (self.source / 'run_agent.py').write_text('# Staged change.\n')
        self.git('add', 'run_agent.py')
        with self.assertRaisesRegex(SystemExit, 'local changes'):
            hermes.check_source(self.env)

    def test_coconut_file_inside_checkout_is_rejected(self):
        (self.source / 'coconut_tool.py').write_text('# Integration in the wrong folder.\n')
        with self.assertRaisesRegex(SystemExit, 'outside its checkout'):
            hermes.check_source(self.env)

    def test_different_origin_is_rejected(self):
        self.git('remote', 'set-url', 'origin', 'https://example.invalid/hermes-fork.git')
        with self.assertRaisesRegex(SystemExit, 'remote does not match'):
            hermes.check_source(self.env)

    def test_unrecorded_commit_is_rejected(self):
        self.git('commit', '--quiet', '--allow-empty', '-m', 'Unverified revision')
        with self.assertRaisesRegex(SystemExit, 'differs from the recorded'):
            hermes.check_source(self.env)

    def test_missing_or_symlinked_receipt_is_rejected(self):
        self.manifest.unlink()
        with self.assertRaisesRegex(SystemExit, 'receipt is missing'):
            hermes.check_source(self.env)
        other = self.root / 'other.json'
        other.write_text('{}')
        self.manifest.symlink_to(other)
        with self.assertRaisesRegex(SystemExit, 'receipt is missing'):
            hermes.check_source(self.env)

    def test_invalid_source_stops_before_importing_hermes(self):
        (self.source / 'run_agent.py').write_text('# Local change.\n')
        original = hermes.run
        def run(command, **kwargs):
            self.assertNotEqual(command[:2], [str(hermes.PYTHON), '-c'])
            return original(command, **kwargs)
        with patch.object(hermes, 'run', side_effect=run), self.assertRaisesRegex(SystemExit, 'local changes'):
            hermes.check()

    def test_contract_failure_cannot_report_compatible(self):
        original = hermes.run
        def run(command, **kwargs):
            if command[:2] == [str(hermes.PYTHON), '-c']:
                raise subprocess.CalledProcessError(1, command)
            return original(command, **kwargs)
        with patch.object(hermes, 'run', side_effect=run), self.assertRaises(subprocess.CalledProcessError):
            hermes.check()

    def test_check_detects_a_source_write_during_the_contract_probe(self):
        original = hermes.run
        def run(command, **kwargs):
            if command[:2] == [str(hermes.PYTHON), '-c']:
                self.assertIn(str(self.root / 'backend'), kwargs['env']['PYTHONPATH'].split(os.pathsep))
                (self.source / 'unexpected.py').write_text('# A contract probe must never write here.\n')
                return subprocess.CompletedProcess(command, 0)
            return original(command, **kwargs)
        with patch.object(hermes, 'run', side_effect=run), self.assertRaisesRegex(SystemExit, 'local changes'):
            hermes.check()


if __name__ == '__main__':
    unittest.main()
