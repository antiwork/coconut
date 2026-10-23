"""Current native files must replace a saved prompt without changing its conversation."""
import subprocess
import unittest
from hermes_env import needs_hermes, PYTHON, SOURCE


@needs_hermes
class NativePromptRefreshTests(unittest.TestCase):
    def test_current_files_preserve_history_receipts_and_the_exact_card_choice(self):
        result = subprocess.run([PYTHON, 'scripts/test_native_prompt_refresh.py',
                                 '--source', SOURCE, '--python', PYTHON],
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Native prompt refresh passed', result.stdout)
