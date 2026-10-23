"""Hermetic installed-Hermes contract tests; no provider or live session access."""
import subprocess
import tempfile
import unittest
from hermes_env import needs_hermes, PYTHON, SOURCE


@needs_hermes
class NativeCompressionTests(unittest.TestCase):
    def test_stale_head_native_worker_and_contracts(self):
        with tempfile.TemporaryDirectory(prefix='stale-head-contract-') as evidence:
            result = subprocess.run([PYTHON,
                'scripts/test_native_compression_head.py',
                '--source', SOURCE,
                '--python', PYTHON, '--evidence', evidence],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Stale-head worker regressions passed (8 cases)', result.stdout)
