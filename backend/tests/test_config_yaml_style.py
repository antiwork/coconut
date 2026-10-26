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


if __name__ == '__main__':
    unittest.main()
