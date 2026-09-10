import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from coconut.push import APNsTransport

spec = importlib.util.spec_from_file_location('push_tools', Path(__file__).resolve().parents[2] / 'scripts/push.py')
push_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push_tools)


class APNsTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.transport = APNsTransport(self.root)
        self.directory = self.root / 'push'
        self.directory.mkdir(mode=0o700)
        self.private_file(self.directory / 'apns-provider', b'sender')

    def private_file(self, path, content=b'credential'):
        path.write_bytes(content)
        path.chmod(0o600)

    def credentials(self, directory):
        directory.mkdir(exist_ok=True, mode=0o700)
        for name in ('config.json', 'AuthKey.p8'):
            self.private_file(directory / name)

    def send(self, environment):
        return self.transport.send('a' * 64, environment, {'aps': {}}, 'id', 'collapse', 1)

    def test_development_key_never_serves_production(self):
        self.credentials(self.directory)
        self.assertTrue(self.transport.configured)
        with patch('coconut.push.subprocess.run') as run:
            self.assertEqual(self.send('production'), (0, 'PushNotConfigured'))
            run.assert_not_called()

    def test_each_environment_uses_its_own_key_and_token_cache(self):
        self.credentials(self.directory)
        self.credentials(self.directory / 'production')
        response = subprocess.CompletedProcess([], 0, b'{"status":200,"reason":"Accepted"}')
        with patch('coconut.push.subprocess.run', return_value=response) as run:
            for environment, folder in [('development', self.directory), ('production', self.directory / 'production')]:
                self.assertEqual(self.send(environment), (200, 'Accepted'))
                args = run.call_args
                self.assertEqual(args.args[0], [str(self.directory / 'apns-provider'), str(folder)])
                self.assertEqual(json.loads(args.kwargs['input'])['environment'], environment)

    def test_missing_or_exposed_production_key_stops_before_send(self):
        production = self.directory / 'production'
        self.credentials(production)
        self.assertTrue(self.transport.configured_for('production'))
        for mode in (0o644, 0o660):
            (production / 'AuthKey.p8').chmod(mode)
            with patch('coconut.push.subprocess.run') as run:
                self.assertEqual(self.send('production'), (0, 'PushNotConfigured'))
                run.assert_not_called()
        self.assertFalse(self.transport.configured_for('../development'))

    def test_production_configuration_preserves_development_credentials(self):
        self.credentials(self.directory)
        key = self.root / 'input.p8'
        key.write_bytes(b'-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----')
        args = argparse.Namespace(data_dir=self.root, environment='production', team='66LJACJ2U7',
                                  key_id='PRODKEY123', topic='com.gumroad.coconut', key=str(key))
        with patch.object(push_tools, 'build_sender') as build:
            push_tools.configure(args)
            build.assert_called_once_with(self.directory)
        self.assertEqual((self.directory / 'AuthKey.p8').read_bytes(), b'credential')
        self.assertEqual((self.directory / 'production/AuthKey.p8').read_bytes(), key.read_bytes())
        self.assertTrue(self.transport.configured_for('production'))


if __name__ == '__main__':
    unittest.main()
