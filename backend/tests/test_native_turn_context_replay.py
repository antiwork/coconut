"""A later turn starts its model request with the exact earlier request, so the provider can reuse its cache."""
import os
import subprocess
import unittest


@unittest.skipUnless(os.environ.get('COCONUT_HERMES_ROOT') and os.environ.get('COCONUT_HERMES_PYTHON'),
                     'Set Coconut Hermes paths for native integration checks.')
class NativeTurnContextReplayTests(unittest.TestCase):
    def test_second_request_starts_with_the_exact_first_request(self):
        result = subprocess.run([os.environ['COCONUT_HERMES_PYTHON'], 'scripts/test_native_turn_context_replay.py',
                                 '--source', os.environ['COCONUT_HERMES_ROOT'], '--python', os.environ['COCONUT_HERMES_PYTHON']],
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Turn context replay passed', result.stdout)
