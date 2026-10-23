"""Hermetic installed-Hermes contract tests; no provider or live session access."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from hermes_env import needs_hermes, PYTHON, SOURCE


@needs_hermes
class NativeCompressionTests(unittest.TestCase):
    def test_native_compression_contract(self):
        with tempfile.TemporaryDirectory(prefix='compression-contract-') as home:
            env = {key: os.environ[key] for key in ('PATH', 'TMPDIR') if key in os.environ}
            env.update(HOME=home, HERMES_HOME=home, HERMES_DISABLE_MCP='1', HERMES_DISABLE_LAZY_INSTALLS='1',
                       PYTHONDONTWRITEBYTECODE='1',
                       PYTHONPATH=os.pathsep.join((SOURCE,
                                                 str(Path('backend').resolve()))))
            result = subprocess.run([PYTHON,
                                     'scripts/test_native_compression.py'], env=env,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Native compression contracts passed', result.stdout)

    def test_stale_head_native_worker_and_contracts(self):
        with tempfile.TemporaryDirectory(prefix='stale-head-contract-') as evidence:
            result = subprocess.run([PYTHON,
                'scripts/test_native_compression_head.py',
                '--source', SOURCE,
                '--python', PYTHON, '--evidence', evidence],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Stale-head worker regressions passed (8 cases)', result.stdout)
