"""The settings process writes the default model into the profiles that follow it, with the config writer of Hermes.

test_bot_model_follow.py checks which bots follow the default. This file checks the write itself.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from hermes_env import needs_hermes, PYTHON, SOURCE

WORKER = Path(__file__).resolve().parents[1] / 'coconut' / 'model_catalog.py'
DEFAULT = ('# The default profile.\n'
           'model:\n  default: anthropic/claude-sonnet-4\n  provider: openrouter\n'
           'agent:\n  reasoning_effort: high\n')
FOLLOWER = ('# Kept comment.\n'
            'model:\n  default: gpt-5\n  provider: openai\n  base_url: https://api.openai.com/v1\n  api_key: sk-old\n'
            'toolsets:\n- kanban\n'
            'agent:\n  max_turns: 40\n  reasoning_overrides:\n    anthropic/claude-sonnet-4: low\n')
OWN = 'model:\n  default: grok-4\n  provider: xai-oauth\n'


@needs_hermes
class FollowDefaultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.hermes, self.private, self.data = self.root / 'hermes', self.root / 'private', self.root / 'data'
        for path in (self.hermes / 'profiles', self.private / 'tmp', self.data):
            path.mkdir(parents=True)
        (self.hermes / 'config.yaml').write_text(DEFAULT)
        self.homes = {}
        for bot_id, text in (('follower', FOLLOWER), ('own', OWN), ('empty', None), ('broken', 'model: [\n')):
            home = self.hermes / 'profiles' / bot_id
            home.mkdir()
            if text is not None:
                (home / 'config.yaml').write_text(text)
            self.homes[bot_id] = home
        self.homes['default'] = self.hermes

    def follow(self, bot_ids, classify=()):
        # The environment of HermesModelCatalog._call for the default profile.
        env = dict(HOME=str(self.private), HERMES_REAL_HOME=str(self.private), HERMES_HOME=str(self.hermes),
                   PYTHONPATH=SOURCE, TMPDIR=str(self.private / 'tmp'), HERMES_DISABLE_TELEMETRY='1',
                   HERMES_DISABLE_LAZY_INSTALLS='1', PATH=os.environ.get('PATH', ''))
        request = dict(operation='follow_default', data_root=str(self.data), profile_roots=[str(self.hermes)],
                       base_home=True, homes={bot_id: str(self.homes[bot_id]) for bot_id in bot_ids},
                       classify=list(classify))
        result = subprocess.run([PYTHON, str(WORKER), '--worker'], input=json.dumps(request), capture_output=True,
                                text=True, env=env, cwd=self.private, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        response = json.loads(result.stdout)
        self.assertNotIn('error', response, response)
        return response['result']

    def load(self, bot_id):
        """Load a config with a strict YAML loader (libyaml), as test_config_yaml_style.py does."""
        from ruamel.yaml import YAML
        return YAML(typ='safe', pure=False).load((self.homes[bot_id] / 'config.yaml').read_text())

    def test_a_following_profile_gets_the_default_model_and_keeps_its_other_settings(self):
        own_before = (self.homes['own'] / 'config.yaml').read_bytes()
        result = self.follow(['follower', 'own', 'empty', 'broken', 'default'], classify=['own', 'empty', 'default'])
        self.assertEqual(result['follower'], {'changed': True})
        self.assertEqual(result['own'], {'follows': False})
        self.assertEqual(result['empty'], {'follows': True, 'changed': True})
        self.assertEqual(result['default'], {'follows': True, 'changed': False})
        # A profile that Hermes cannot read fails alone.
        self.assertIn('error', result['broken'])
        follower = self.load('follower')
        self.assertEqual(follower['model'], {'default': 'anthropic/claude-sonnet-4', 'provider': 'openrouter'})
        # The effort of the default replaces the override for this model. The other keys stay.
        self.assertEqual(follower['agent'], {'max_turns': 40, 'reasoning_effort': 'high'})
        self.assertEqual(follower['toolsets'], ['kanban'])
        self.assertIn('# Kept comment.', (self.homes['follower'] / 'config.yaml').read_text())
        self.assertEqual(self.load('empty')['model'], {'default': 'anthropic/claude-sonnet-4', 'provider': 'openrouter'})
        self.assertEqual(self.load('empty')['agent'], {'reasoning_effort': 'high'})
        # A bot with its own model and the default profile itself do not change.
        self.assertEqual((self.homes['own'] / 'config.yaml').read_bytes(), own_before)
        self.assertEqual((self.hermes / 'config.yaml').read_text(), DEFAULT)
        # A second run changes nothing.
        before = {bot_id: (self.homes[bot_id] / 'config.yaml').read_bytes() for bot_id in ('follower', 'empty')}
        again = self.follow(['follower', 'empty'])
        self.assertEqual(again, {'follower': {'changed': False}, 'empty': {'changed': False}})
        self.assertEqual({bot_id: (self.homes[bot_id] / 'config.yaml').read_bytes() for bot_id in before}, before)

    def test_a_new_default_reaches_the_profile_and_a_classified_write_keeps_a_backup(self):
        (self.homes['follower'] / 'config.yaml').write_text('model:\n  default: anthropic/claude-sonnet-4\n'
                                                            '  provider: openrouter\ntoolsets:\n- kanban\n')
        before = (self.homes['follower'] / 'config.yaml').read_bytes()
        self.assertEqual(self.follow(['follower'], classify=['follower']), {'follower': {'follows': True, 'changed': True}})
        self.assertEqual((self.data / 'backups/model-follow/follower.config.yaml').read_bytes(), before)
        (self.hermes / 'config.yaml').write_text('model:\n  default: gpt-5\n  provider: openai\n'
                                                 '  base_url: https://api.openai.com/v1\n')
        self.assertEqual(self.follow(['follower']), {'follower': {'changed': True}})
        follower = self.load('follower')
        self.assertEqual(follower['model'], {'default': 'gpt-5', 'provider': 'openai',
                                             'base_url': 'https://api.openai.com/v1'})
        self.assertNotIn('reasoning_effort', follower.get('agent') or {})
        self.assertEqual(follower['toolsets'], ['kanban'])
        # The backup keeps the first file only.
        self.assertEqual((self.data / 'backups/model-follow/follower.config.yaml').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
