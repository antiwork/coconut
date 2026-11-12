import dataclasses
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from coconut.hermes_contract import require_keywords, require_registration_fields
from coconut.hermes_runtime import CONTRACT
from hermes_env import PYTHON, SOURCE, needs_hermes


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

    def test_a_platform_registration_without_a_field_of_coconut_fails(self):
        @dataclasses.dataclass
        class OlderEntry:
            name: str
            display_tier: str = ''

        @dataclasses.dataclass
        class Entry(OlderEntry):
            trusted_inbound: bool = False
            display_overrides: dict = dataclasses.field(default_factory=dict)
        with self.assertRaisesRegex(ValueError, 'registration fields of the coconut platform: trusted_inbound, '
                                                'display_overrides$'):
            require_registration_fields(OlderEntry)
        require_registration_fields(Entry)

    @needs_hermes
    def test_the_gateway_contract_passes_with_the_installed_hermes(self):
        program = ('import sys; sys.path.insert(0, sys.argv[2]); sys.path.insert(0, sys.argv[1]); '
                   'from coconut.hermes_contract import verify_gateway_contract; '
                   'verify_gateway_contract(); print("checked")')
        with tempfile.TemporaryDirectory() as temporary:
            environment = dict(PATH=os.environ.get('PATH', '/usr/bin:/bin'), HOME=temporary,
                               HERMES_HOME=temporary, HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_DISABLE_TELEMETRY='1')
            result = subprocess.run([PYTHON, '-I', '-B', '-c', program, str(Path(__file__).resolve().parents[1]),
                                     SOURCE], cwd=temporary, env=environment, capture_output=True, text=True,
                                    timeout=300)
        self.assertEqual((result.returncode, result.stdout), (0, 'checked\n'), result.stderr[-3000:])

    def test_check_process_blocks_network_before_loading_the_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / 'coconut'
            package.mkdir()
            (package / '__init__.py').write_text('')
            (package / 'hermes_contract.py').write_text(
                'import socket\n'
                'def verify_contract():\n'
                '    socket.getaddrinfo("network-must-not-run.invalid", 443)\n')
            result = subprocess.run([sys.executable, '-I', '-c', CONTRACT, temporary, ''], cwd=temporary,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('cannot use the network', result.stderr)


if __name__ == '__main__':
    unittest.main()
