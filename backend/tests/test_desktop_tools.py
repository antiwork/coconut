import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


script = Path(__file__).resolve().parents[2] / 'scripts/desktop.py'
spec = importlib.util.spec_from_file_location('desktop_tools', script)
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


class DesktopToolsTests(unittest.TestCase):
    def test_unconfigured_simulator_uses_the_default_port(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(desktop, 'DATA', Path(directory)):
            self.assertEqual(desktop.simulator_address(), 'https://localhost:8960')

    def test_simulator_uses_the_saved_port_without_remote_dns(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(desktop, 'DATA', Path(directory)):
            connection = Path(directory) / 'connection.json'
            for address, expected in [
                ('https://coconut.example.ts.net:9443', 'https://localhost:9443'),
                ('https://100.70.80.90:8960', 'https://localhost:8960'),
                ('https://coconut.example.ts.net', 'https://localhost:443'),
            ]:
                with self.subTest(address=address):
                    connection.write_text(json.dumps({'address': address}))
                    self.assertEqual(desktop.simulator_address(), expected)


if __name__ == '__main__':
    unittest.main()
