"""Coconut writes Hermes config files that a strict YAML loader parses."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from hermes_env import needs_hermes, PYTHON, SOURCE

BACKEND = Path(__file__).resolve().parents[1]

# The form of the files that the settings migration saved on 2026-10-23: a profile created as JSON, with settings that
# Hermes's round-trip save added in flow style. libyaml rejects the plain URL in the flow mapping.
FLOW = ('{"terminal": {"cwd": "/Users/coconut/Coconut/.runtime/host/bots/41ba03b4-bac3-4927-8ddf-61579790ee9f/workspace",\n'
        'shell_init_files: [/Users/coconut/Coconut/.runtime/host/bots/41ba03b4-bac3-4927-8ddf-61579790ee9f/home/.profile,\n'
        '/Users/coconut/Developer/CoconutMaintenance/toolchains.sh]}, delegation: {max_concurrent_children: null,\n'
        'worktree_isolation: true}, toolsets: [kanban], approvals: {mode: "off"}, model: {\n'
        'default: gpt-6.1-sol, provider: openai-codex}, mcp_servers: {gumclaw: {url: https://gumclaw-mcp.antiwork.dev/mcp,\n'
        "headers: {Authorization: 'Bearer ${MCP_GUMCLAW_API_KEY}'}}}}\n")

CHECK = r"""
import json, sys
from pathlib import Path
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from hermes_yaml import roundtrip_yaml
from native_settings import raw_config, save_config
def _block(node):
    if isinstance(node, CommentedMap):
        return not node.fa.flow_style() and all(_block(value) for value in node.values())
    if isinstance(node, CommentedSeq):
        return not node.fa.flow_style() and all(_block(value) for value in node)
    return True
home = Path(sys.argv[1])
config = home / 'config.yaml'
config.write_text(sys.argv[2])
before = raw_config(home)
save_config(home, before)
unchanged = config.read_text()
changed = dict(before, approvals={'mode': 'smart'})
save_config(home, changed)
strict = YAML(typ='safe', pure=False)
results = dict(before=before, unchanged=strict.load(unchanged), changed=strict.load(config.read_text()), expected=changed,
               block=_block(roundtrip_yaml().load(unchanged)))
try:
    strict.load(sys.argv[2])
    results['flow_rejected'] = False
except Exception:
    results['flow_rejected'] = True
print(json.dumps(results))
"""


@needs_hermes
class StrictYamlTests(unittest.TestCase):
    def _save(self, text):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'home'
            home.mkdir()
            env = dict(os.environ, HERMES_HOME=str(home),
                       PYTHONPATH=os.pathsep.join([str(Path(SOURCE).resolve()), str(BACKEND), str(BACKEND / 'coconut')]))
            result = subprocess.run([PYTHON, '-c', CHECK, str(home), text], env=env, capture_output=True, text=True,
                                    timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-6000:])
            state = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(state['block'])
        self.assertEqual(state['unchanged'], state['before'])
        self.assertEqual(state['changed'], state['expected'])
        return state

    def test_a_saved_flow_style_config_parses_with_a_strict_loader(self):
        state = self._save(FLOW)
        # The loader is strict: it rejects the file as the migration saved it.
        self.assertTrue(state['flow_rejected'])
        self.assertEqual(state['before']['mcp_servers']['gumclaw'],
                         {'url': 'https://gumclaw-mcp.antiwork.dev/mcp',
                          'headers': {'Authorization': 'Bearer ${MCP_GUMCLAW_API_KEY}'}})
        self.assertEqual(state['before']['model'], {'default': 'gpt-6.1-sol', 'provider': 'openai-codex'})
        self.assertEqual(state['before']['approvals'], {'mode': 'off'})

    def test_a_new_profile_config_is_saved_in_block_style(self):
        # A new profile starts as JSON. The first save adds settings, which must not stay in flow style.
        from coconut.profiles import _json
        state = self._save(_json({'toolsets': ['kanban'], 'terminal': {'cwd': '/workspace'},
                                  'delegation': {'max_concurrent_children': None, 'worktree_isolation': True}}))
        self.assertEqual(state['before']['delegation'], {'max_concurrent_children': None, 'worktree_isolation': True})


PERSON = '''# The settings of the person.
model:
  default: gpt-6.1-sol  # The main model.
  provider: openai-codex
skills:
  disabled:
    - old-skill
'''


@needs_hermes
class SkillFolderTests(unittest.TestCase):
    """Setup adds the skill folder to `skills.external_dirs` through the config writer of Hermes. See skill_dirs.py."""

    def run_edit(self, action, home, *others):
        # As scripts/hermes_skills.py runs it: isolated, with the Hermes source folder as an argument.
        env = {key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ}
        env.update(HERMES_HOME=str(home), HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_DISABLE_TELEMETRY='1')
        result = subprocess.run([PYTHON, '-I', '-B', str(BACKEND / 'coconut/skill_dirs.py'), action,
                                 str(Path(SOURCE).resolve()), str(self.folder), str(home), *map(str, others)],
                                env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-6000:])
        return result.stdout

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / 'Coconut/skills'
        self.home = self.root / 'hermes'
        self.home.mkdir()

    def load(self, path):
        # Only the Python of Hermes has ruamel.yaml.
        result = subprocess.run([PYTHON, '-c', 'import json, sys; from ruamel.yaml import YAML; '
                                 'print(json.dumps(YAML(typ="safe", pure=True).load(open(sys.argv[1]))))', str(path)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_add_keeps_comments_and_other_keys_and_runs_once(self):
        config = self.home / 'config.yaml'
        config.write_text(PERSON)
        self.assertIn('config.yaml: added', self.run_edit('add', self.home))
        text = config.read_text()
        self.assertIn('# The settings of the person.', text)
        self.assertIn('# The main model.', text)
        self.assertEqual(self.load(config), {'model': {'default': 'gpt-6.1-sol', 'provider': 'openai-codex'},
                                             'skills': {'disabled': ['old-skill'], 'external_dirs': [str(self.folder)]}})
        self.assertIn('config.yaml: unchanged', self.run_edit('add', self.home))
        self.assertEqual(config.read_text(), text)
        self.assertIn('config.yaml: removed', self.run_edit('remove', self.home))
        self.assertEqual(self.load(config), {'model': {'default': 'gpt-6.1-sol', 'provider': 'openai-codex'},
                                             'skills': {'disabled': ['old-skill']}})
        self.assertIn('# The main model.', config.read_text())

    def test_add_reaches_named_profiles_and_bots_that_have_a_config(self):
        (self.home / 'config.yaml').write_text('model: gpt\n')
        for name in ('work', 'empty'):
            (self.home / 'profiles' / name).mkdir(parents=True)
        (self.home / 'profiles/work/config.yaml').write_text('# Work.\nmodel: gpt\n')
        bot = self.root / 'bots/one/home'
        bot.mkdir(parents=True)
        # A bot profile starts as JSON (profiles._create). The writer saves it in block style.
        (bot / 'config.yaml').write_text('{"toolsets": ["kanban"], "terminal": {"cwd": "/workspace"}}\n')
        self.run_edit('add', self.home, bot)
        for path in (self.home / 'config.yaml', self.home / 'profiles/work/config.yaml', bot / 'config.yaml'):
            with self.subTest(path=path):
                self.assertEqual(self.load(path)['skills'], {'external_dirs': [str(self.folder)]})
        self.assertFalse((self.home / 'profiles/empty/config.yaml').exists())
        self.assertIn('# Work.', (self.home / 'profiles/work/config.yaml').read_text())
        self.assertFalse((bot / 'config.yaml').read_text().startswith('{'))

    def test_add_creates_the_default_config_and_works_without_profiles(self):
        self.assertIn('config.yaml: added', self.run_edit('add', self.home))
        self.assertEqual(self.load(self.home / 'config.yaml'), {'skills': {'external_dirs': [str(self.folder)]}})
        # A removal never creates a config.
        (self.home / 'config.yaml').unlink()
        self.assertEqual(self.run_edit('remove', self.home), '')
        self.assertFalse((self.home / 'config.yaml').exists())

    def test_hermes_lists_the_skill_from_the_added_folder(self):
        skill = self.folder / 'coconut'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text((BACKEND.parent / 'skills/coconut/SKILL.md').read_text())
        self.run_edit('add', self.home)
        env = {key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ}
        env.update(HERMES_HOME=str(self.home), HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_DISABLE_TELEMETRY='1',
                   PYTHONPATH=str(Path(SOURCE).resolve()))
        check = ('import json; from agent.skill_utils import get_external_skills_dirs; '
                 'print(json.dumps([str(path) for path in get_external_skills_dirs()]))')
        result = subprocess.run([PYTHON, '-c', check], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-6000:])
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]), [str(self.folder.resolve())])


if __name__ == '__main__':
    unittest.main()
