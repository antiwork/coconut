"""The deploy package carries the tunnel sidecar's sources, and a deploy installs or rebuilds the sidecar."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import remote  # noqa: E402
import tunnel  # noqa: E402

spec = importlib.util.spec_from_file_location('coconut_tunnel_deploy', SCRIPTS / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)


class TunnelPackageTests(unittest.TestCase):
    def test_every_sidecar_source_is_in_the_deploy_package(self):
        files = remote.source_files()
        for source in tunnel.SOURCES:
            name = source.relative_to(remote.ROOT).as_posix()
            with self.subTest(name=name):
                self.assertIn(name, files)
                self.assertTrue(deploy.allowed_path(name))

    def test_only_swift_files_in_the_tunnel_folder_are_allowed(self):
        for name in ('ios/Tunnel/Noise.py', 'ios/Coconut/APIClient.swift', 'ios/Tunnel/../Coconut/App.swift'):
            with self.subTest(name=name):
                self.assertFalse(deploy.allowed_path(name))

    def test_a_failed_sidecar_build_keeps_the_complete_update(self):
        root = Path('/Coconut')
        commands = []

        def command(self, arguments, *rest, **options):
            commands.append(arguments[1:])
            raise deploy.DeployError('Tunnel sidecar build failed (exit 1). No command output was exposed.')

        with patch.object(deploy.Operations, 'command', command):
            result = deploy.Operations.tunnel(object.__new__(deploy.Operations), root)
        self.assertEqual(commands, [[str(root / 'scripts/tunnel.py'), 'rebuild']])
        self.assertEqual(result, {'ok': False, 'error': 'Tunnel sidecar build failed (exit 1). No command output was exposed.'})


class TunnelRebuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def launchctl(self, *arguments, loaded=True, kickstart=0):
        self.calls.append(arguments[0])
        code = (0 if loaded else 113) if arguments[0] == 'print' else kickstart
        return subprocess.CompletedProcess(arguments, code, '', '')

    def rebuild(self, *, changed, **options):
        with patch.object(tunnel, 'launchctl', lambda *arguments: self.launchctl(*arguments, **options)), \
             patch.object(tunnel, 'compile_sidecar', return_value=changed) as compile_sidecar:
            tunnel.rebuild(self.data)
        return compile_sidecar.called

    def install(self):
        (self.data / 'tunnel').mkdir()
        (self.data / 'tunnel/route.json').write_text('{}')

    def test_a_mac_that_never_had_the_sidecar_gets_it(self):
        # The relay is the only way from the phone to the Mac.
        with patch.object(tunnel, 'install') as install:
            self.assertFalse(self.rebuild(changed=True, loaded=False))
        install.assert_called_once_with(self.data, tunnel.RELAY_URL)

    def test_a_failed_install_is_retried(self):
        # The install saved the route, but launchctl did not load the sidecar.
        self.install()
        with patch.object(tunnel, 'install') as install:
            self.rebuild(changed=True, loaded=False)
        install.assert_called_once_with(self.data, tunnel.RELAY_URL)

    def test_an_uninstalled_sidecar_stays_uninstalled(self):
        self.install()
        with patch.object(tunnel, 'launchctl', lambda *arguments: self.launchctl(*arguments)), \
                patch.object(tunnel, 'agent_path', return_value=self.data / 'agent.plist'):
            tunnel.uninstall(self.data)
        with patch.object(tunnel, 'install') as install:
            self.assertFalse(self.rebuild(changed=True, loaded=False))
        install.assert_not_called()
        self.assertNotIn('kickstart', self.calls)

    def test_new_sources_restart_the_sidecar(self):
        self.install()
        self.assertTrue(self.rebuild(changed=False))
        self.assertNotIn('kickstart', self.calls)
        self.assertTrue(self.rebuild(changed=True))
        self.assertIn('kickstart', self.calls)

    def test_a_failed_restart_is_retried_by_the_next_rebuild(self):
        # The new binary is in place, but the old process still runs. Without its receipt, the next rebuild restarts it.
        self.install()
        receipt = self.data / 'tunnel/sidecar.sha256'
        receipt.write_text('fingerprint')
        with self.assertRaisesRegex(SystemExit, 'did not restart'):
            self.rebuild(changed=True, kickstart=1)
        self.assertFalse(receipt.exists())


if __name__ == '__main__':
    unittest.main()
