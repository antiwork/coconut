"""Current native files must replace a saved prompt without changing its conversation."""
import os
import subprocess
import unittest


@unittest.skipUnless(os.environ.get('COCONUT_HERMES_ROOT') and os.environ.get('COCONUT_HERMES_PYTHON'),
                     'Set Coconut Hermes paths for native integration checks.')
class NativePromptRefreshTests(unittest.TestCase):
    def test_current_files_preserve_history_receipts_and_the_exact_card_choice(self):
        result = subprocess.run([os.environ['COCONUT_HERMES_PYTHON'], 'scripts/test_native_prompt_refresh.py',
                                 '--source', os.environ['COCONUT_HERMES_ROOT'], '--python', os.environ['COCONUT_HERMES_PYTHON']],
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Native prompt refresh passed', result.stdout)
