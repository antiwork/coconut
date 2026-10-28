"""Each service start sets Coconut's own entries in the Hermes profiles again. deploy.py does the same at each install
(test_deploy_tools.py). test_config_yaml_style.py checks the edit with the real config writer of Hermes."""
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from coconut import gateway_plugin
from coconut.profiles import SKILLS
from coconut.service import Service
from test_service import ControlledProvider

# Stand-ins for the config reader and writer of Hermes, which skill_dirs and gateway_plugin use.
NATIVE_SETTINGS = ('import json\nfrom pathlib import Path\n'
                   'def raw_config(home):\n    path = Path(home) / "config.yaml"\n'
                   '    return json.loads(path.read_text()) if path.exists() else {}\n'
                   'def save_config(home, config):\n    (Path(home) / "config.yaml").write_text(json.dumps(config))\n')
# Stand-ins for the env reader and writer of Hermes and its list of provider keys, which model_keys uses.
HERMES_CONFIG = '''import os
from pathlib import Path
OPTIONAL_ENV_VARS = {"GEMINI_API_KEY": {"category": "provider", "password": True},
                     "GEMINI_BASE_URL": {"category": "provider", "password": False},
                     "TELEGRAM_BOT_TOKEN": {"category": "messaging", "password": True}}
def save_env_value(key, value):
    path = Path(os.environ["HERMES_HOME"]) / ".env"
    lines = [line for line in (path.read_text().splitlines() if path.exists() else []) if not line.startswith(key + "=")]
    path.write_text("\\n".join(lines + [key + "=" + value]) + "\\n")
'''
SECRET_SCOPE = '''from pathlib import Path
def load_env_file(path):
    pairs = (line.split("=", 1) for line in Path(path).read_text().splitlines() if "=" in line and not line.startswith("#"))
    return {key: value for key, value in pairs}
'''
ENTRIES = {'skills': {'external_dirs': [str(SKILLS)]}, 'plugins': {'enabled': ['coconut']},
           'gateway': {'platforms': {'coconut': {'enabled': True}}}}


class HermesEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source, self.home = root / 'source', root / 'hermes'
        self.source.mkdir()
        (self.source / 'native_settings.py').write_text(NATIVE_SETTINGS)
        for name, text in (('hermes_cli/__init__.py', ''), ('hermes_cli/config.py', HERMES_CONFIG),
                           ('agent/__init__.py', ''), ('agent/secret_scope.py', SECRET_SCOPE)):
            (self.source / name).parent.mkdir(exist_ok=True)
            (self.source / name).write_text(text)
        (self.home / 'profiles/work').mkdir(parents=True)
        (self.home / 'config.yaml').write_text(json.dumps({'model': 'gpt', 'plugins': {'enabled': ['memory']}}))
        (self.home / 'profiles/work/config.yaml').write_text(json.dumps({'toolsets': ['kanban']}))
        self.provider = ControlledProvider()
        self.provider.python, self.provider.hermes_root = sys.executable, str(self.source)
        self.service = Service(root / 'data', self.provider, start_workers=False, hermes_home=self.home)
        self.addCleanup(self.service.close)

    def config(self, home):
        return json.loads((home / 'config.yaml').read_text())

    def test_a_start_adds_the_missing_entries_and_a_second_start_changes_nothing(self):
        self.assertTrue(self.service._set_hermes_entries(sys.executable, self.source))
        self.assertEqual(self.config(self.home), {'model': 'gpt', 'plugins': {'enabled': ['memory', 'coconut']},
                                                  'skills': ENTRIES['skills'], 'gateway': ENTRIES['gateway']})
        self.assertEqual(self.config(self.home / 'profiles/work'), {'toolsets': ['kanban'], **ENTRIES})
        for home in (self.home, self.home / 'profiles/work'):
            self.assertEqual((home / 'plugins/coconut').resolve(), gateway_plugin.PLUGIN)
        before = [(home / 'config.yaml').read_bytes() for home in (self.home, self.home / 'profiles/work')]
        with self.assertLogs('coconut.bot_profiles', logging.DEBUG) as logs:
            logging.getLogger('coconut.bot_profiles').debug('A second start.')
            self.assertTrue(self.service._set_hermes_entries(sys.executable, self.source))
        self.assertEqual([(home / 'config.yaml').read_bytes() for home in (self.home, self.home / 'profiles/work')], before)
        self.assertEqual(len(logs.records), 1)

    def env(self, home):
        path = home / '.env'
        lines = path.read_text().splitlines() if path.exists() else []
        return dict(line.split('=', 1) for line in lines if '=' in line and not line.startswith('#'))

    def test_each_profile_gets_the_default_provider_keys_and_coconut_moves_its_saved_keys_into_hermes(self):
        (self.home / '.env').write_text('ANTHROPIC_API_KEY=default-anthropic\nGEMINI_API_KEY=default-gemini\n'
                                        'GEMINI_BASE_URL=https://example.test\nTELEGRAM_BOT_TOKEN=default-bot\n')
        (self.home / 'profiles/work/.env').write_text('# Hermes seeds this file.\nGEMINI_API_KEY=own-gemini\n')
        saved = self.service.root / 'credentials.json'
        saved.write_text(json.dumps({'ANTHROPIC_API_KEY': 'saved-anthropic', 'OPENAI_API_KEY': 'saved-openai'}))
        with self.assertLogs('coconut.bot_profiles', logging.INFO) as logs:
            self.assertTrue(self.service._set_hermes_entries(sys.executable, self.source))
        # The person's own default key stays. The saved key that Hermes lacked moves into the default profile.
        self.assertEqual(self.env(self.home)['ANTHROPIC_API_KEY'], 'default-anthropic')
        self.assertEqual(self.env(self.home)['OPENAI_API_KEY'], 'saved-openai')
        self.assertFalse(saved.exists())
        # A named profile gets the provider keys that it lacks, keeps its own, and gets no other settings.
        self.assertEqual(self.env(self.home / 'profiles/work'), {
            'GEMINI_API_KEY': 'own-gemini', 'ANTHROPIC_API_KEY': 'default-anthropic', 'OPENAI_API_KEY': 'saved-openai'})
        # The log names keys, never values.
        text = '\n'.join(logs.output)
        self.assertIn('ANTHROPIC_API_KEY', text)
        for value in ('default-anthropic', 'saved-openai', 'default-gemini', 'own-gemini'):
            self.assertNotIn(value, text)
        before = (self.home / 'profiles/work/.env').read_bytes()
        self.assertTrue(self.service._set_hermes_entries(sys.executable, self.source))
        self.assertEqual((self.home / 'profiles/work/.env').read_bytes(), before)

    def test_another_plugin_named_coconut_stays_and_the_start_goes_on(self):
        (self.home / 'profiles/work/plugins/coconut').mkdir(parents=True)
        with self.assertLogs('coconut.bot_profiles', logging.WARNING) as logs:
            self.assertFalse(self.service._set_hermes_entries(sys.executable, self.source))
        self.assertIn('another plugin has this name', logs.output[0])
        self.assertTrue((self.home / 'profiles/work/plugins/coconut').is_dir())
        work = self.config(self.home / 'profiles/work')
        self.assertNotIn('plugins', work)
        self.assertNotIn('gateway', work)
        # The other profiles get their entries all the same.
        self.assertIn('coconut', self.config(self.home)['plugins']['enabled'])

    def test_the_gateway_starts_only_after_the_entries_and_never_after_the_stop(self):
        import threading
        from coconut import gateway_chat
        release = threading.Event()
        self.service.hermes_entries = threading.Thread(target=release.wait, daemon=True)
        self.service.hermes_entries.start()
        with self.service.transaction():
            self.service.set_chat_runtime({'chat_runtime': 'gateway'})
        with patch.object(gateway_chat, 'live_gateway_pid', return_value=None), \
                patch.object(self.service, '_start_gateway') as start:
            self.service._check_gateway()
            self.assertEqual(self.service.gateway_state['state'], 'starting')
            release.set()
            self.service.hermes_entries.join(5)
            self.service.stopping.set()
            self.service._check_gateway()
            start.assert_not_called()
            self.service.stopping.clear()
            self.service._check_gateway()
            start.assert_called_once_with(self.home)

    def test_a_failure_is_a_warning_and_the_service_starts(self):
        with self.assertLogs('coconut.bot_profiles', logging.WARNING):
            self.assertFalse(self.service._set_hermes_entries('/nonexistent/python', self.source))
        with patch.object(self.service, '_set_hermes_entries', side_effect=lambda *_: False) as entries:
            self.service._start_hermes_entries()
            self.service.hermes_entries.join(5)
        entries.assert_called_once_with(sys.executable, str(self.source))


if __name__ == '__main__':
    unittest.main()
