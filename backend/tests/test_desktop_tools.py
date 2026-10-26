import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('desktop_tools', SCRIPTS / 'desktop.py')
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


class DesktopToolsTests(unittest.TestCase):
    def test_simulator_uses_the_main_loopback_listener(self):
        # The simulator shares the Mac's loopback, and the service has no network address.
        self.assertEqual(desktop.SIMULATOR_ADDRESS, 'http://127.0.0.1:8960')


if __name__ == '__main__':
    unittest.main()
