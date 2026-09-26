import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from coconut.model_catalog import ENV_KEYS

SOURCE = os.environ.get('COCONUT_HERMES_ROOT')
PYTHON = os.environ.get('COCONUT_HERMES_PYTHON')
WORKER = Path(__file__).resolve().parents[1] / 'coconut' / 'model_catalog.py'


@unittest.skipUnless(SOURCE and PYTHON and Path(PYTHON).is_file() and Path(SOURCE).is_dir(),
                     'Set COCONUT_HERMES_ROOT and COCONUT_HERMES_PYTHON for the native catalog check.')
class NativeCatalogTests(unittest.TestCase):
    def test_the_worker_lists_models_with_the_real_hermes_providers_package(self):
        # Hermes imports its top-level providers package while it lists models. The worker runs from the folder
        # that also holds Coconut's providers.py, so the import order decides which module Hermes gets.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, private = root / 'hermes', root / 'home'
            for path in (home, private / 'tmp'):
                path.mkdir(parents=True)
            (home / 'config.yaml').write_text('model:\n  default: gpt-5\n  provider: openai\n')
            # The same environment and time limit as HermesModelCatalog._call for a 'get'.
            env = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
            env.update(HOME=str(private), HERMES_REAL_HOME=str(private), HERMES_HOME=str(home), PYTHONPATH=SOURCE,
                       XDG_CONFIG_HOME=str(private / '.config'), XDG_CACHE_HOME=str(private / '.cache'),
                       TMPDIR=str(private / 'tmp'), HERMES_DISABLE_TELEMETRY='1', PYTHONUNBUFFERED='1')
            result = subprocess.run([PYTHON, str(WORKER), '--worker'], input=json.dumps(dict(operation='get', data_root=str(root))),
                                    capture_output=True, text=True, env=env, cwd=private, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            response = json.loads(result.stdout)
            self.assertNotIn('error', response, response)
            self.assertIn('models', response['result'])


if __name__ == '__main__':
    unittest.main()
