"""A later turn starts its model request with the exact earlier request, so the provider can reuse its cache."""
import subprocess
import unittest
from hermes_env import needs_hermes, PYTHON, SOURCE


@needs_hermes
class NativeTurnContextReplayTests(unittest.TestCase):
    def test_second_request_starts_with_the_exact_first_request(self):
        result = subprocess.run([PYTHON, 'scripts/test_native_turn_context_replay.py',
                                 '--source', SOURCE, '--python', PYTHON],
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Turn context replay passed', result.stdout)
