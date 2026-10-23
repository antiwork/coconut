from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from coconut.hermes_contract import require_keywords
from coconut.hermes_runtime import CONTRACT


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
            result = subprocess.run([sys.executable, '-I', '-c', CONTRACT, temporary], cwd=temporary,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('cannot use the network', result.stderr)


if __name__ == '__main__':
    unittest.main()
