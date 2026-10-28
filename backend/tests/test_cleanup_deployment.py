from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location('cleanup_deployment', SCRIPTS / 'cleanup.py')
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


class DeploymentCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / '.runtime'
        for module, name, value in ((cleanup, 'ROOT', self.root), (cleanup, 'RUNTIME', self.runtime)):
            replacement = patch.object(module, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        for name in ('hermes/source/run_agent.py', 'host/bots/test/home/SOUL.md', 'cache/data',
                     'milestone-hermes-test/data', 'ssh/coconut_ed25519', 'remote.json', 'iphone.json', 'iphone.lock'):
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('Test ' + name).encode())
        self.plist = self.root / 'agent.plist'
        self.plist.write_text('Owned LaunchAgent.')
        self.build = self.root / '.build/App'
        self.build.parent.mkdir()
        self.build.write_bytes(b'Keep the iOS build.')

    def run_cleanup(self, *arguments, stop_error=None):
        with patch.object(sys, 'argv', ['cleanup', '--keep-deployment', *arguments]), \
             patch.object(cleanup, 'stop_for_cleanup', return_value=self.plist, side_effect=stop_error), \
             patch.object(Path, 'home', return_value=self.root):
            cleanup.main()

    def test_execution_removal_preserves_exact_ssh_and_phone_settings(self):
        before = {str(path.relative_to(self.runtime)): path.read_bytes() for path in self.runtime.rglob('*')
                  if path.is_file() and path.relative_to(self.runtime).parts[0] in cleanup.DEPLOYMENT_FILES}
        self.run_cleanup('--yes')
        after = {str(path.relative_to(self.runtime)): path.read_bytes() for path in self.runtime.rglob('*') if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((self.runtime / 'hermes').exists())
        self.assertFalse((self.runtime / 'host').exists())
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.build.read_bytes(), b'Keep the iOS build.')
        self.run_cleanup('--yes')
        self.assertFalse((self.runtime / 'hermes').exists())

    def test_failed_stop_preserves_runtime_and_update_access(self):
        with self.assertRaisesRegex(SystemExit, 'Could not stop'):
            self.run_cleanup('--yes', stop_error=SystemExit('Could not stop the owned service.'))
        self.assertTrue((self.runtime / 'hermes/source/run_agent.py').is_file())
        self.assertTrue((self.runtime / 'ssh/coconut_ed25519').is_file())
        self.assertTrue(self.plist.exists())

    def test_preview_keeps_every_file(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        self.run_cleanup()
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')), before)

    def test_symlink_rejects_before_any_removal(self):
        outside = self.root / 'keep'
        outside.mkdir()
        (self.runtime / 'external').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, 'symbolic link'):
            self.run_cleanup('--yes')
        self.assertTrue((self.runtime / 'hermes/source/run_agent.py').is_file())
        self.assertTrue(outside.exists())


if __name__ == '__main__':
    unittest.main()
