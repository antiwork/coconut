"""Hermetic installed-Hermes contract tests; no provider or live session access."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('COCONUT_HERMES_ROOT') and os.environ.get('COCONUT_HERMES_PYTHON'),
                     'Set Coconut Hermes paths for native integration checks.')
class NativeCompressionTests(unittest.TestCase):
    def test_native_compression_contract(self):
        with tempfile.TemporaryDirectory(prefix='compression-contract-') as home:
            env = {key: os.environ[key] for key in ('PATH', 'TMPDIR') if key in os.environ}
            env.update(HOME=home, HERMES_HOME=home, HERMES_DISABLE_MCP='1',
                       PYTHONDONTWRITEBYTECODE='1',
                       PYTHONPATH=os.pathsep.join((os.environ['COCONUT_HERMES_ROOT'],
                                                 str(Path('backend').resolve()))))
            result = subprocess.run([os.environ['COCONUT_HERMES_PYTHON'],
                                     'scripts/test_native_compression.py'], env=env,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Native compression contracts passed', result.stdout)

    def test_stale_head_native_worker_and_contracts(self):
        with tempfile.TemporaryDirectory(prefix='stale-head-contract-') as evidence:
            result = subprocess.run([os.environ['COCONUT_HERMES_PYTHON'],
                'scripts/test_native_compression_head.py',
                '--source', os.environ['COCONUT_HERMES_ROOT'],
                '--python', os.environ['COCONUT_HERMES_PYTHON'], '--evidence', evidence],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Stale-head worker regressions passed (8 cases)', result.stdout)
