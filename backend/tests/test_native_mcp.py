from copy import deepcopy
from pathlib import Path
from types import ModuleType
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

from coconut import native_mcp


class NativeMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = {'custom': {'keep': True}, 'mcp_servers': {}}
        self.tokens = {}
        self.probes = []
        cli = ModuleType('hermes_cli.mcp_config')
        cli._bearer_auth_headers = lambda name: {'Authorization': '${MCP_' + name.upper() + '_API_KEY}'}
        cli._env_key_for_server = lambda name: 'MCP_' + name.upper() + '_API_KEY'
        cli._save_bearer_auth_token = lambda name, token: self.tokens.update({cli._env_key_for_server(name): token.strip()})
        cli._probe_single_server = lambda name, config, connect_timeout: self.probes.append((name, config)) or [('chat', 'Talk')]
        security = ModuleType('hermes_cli.mcp_security')
        security.validate_mcp_server_entry = lambda name, value: []
        config = ModuleType('hermes_cli.config')
        config.remove_env_value = lambda key: self.tokens.pop(key, None)
        self.modules = patch.dict(sys.modules, {'hermes_cli': ModuleType('hermes_cli'),
            'hermes_cli.mcp_config': cli, 'hermes_cli.mcp_security': security, 'hermes_cli.config': config})
        self.modules.start(); self.addCleanup(self.modules.stop)
        def save(_, value): self.config = deepcopy(value)
        settings = patch.object(native_mcp, '_settings', return_value=(lambda _: deepcopy(self.config), save))
        settings.start(); self.addCleanup(settings.stop)

    def manage(self, **request):
        return native_mcp.manage(request, self.home)

    def test_native_token_storage_never_enters_public_state(self):
        result = self.manage(action='save', name='example', url='https://example.com/mcp', token=' private-test-token\n')
        self.assertEqual(self.tokens['MCP_EXAMPLE_API_KEY'], 'private-test-token')
        self.assertNotIn('private-test-token', json.dumps(result))
        self.assertNotIn('private-test-token', json.dumps(self.config))
        self.assertTrue(result['servers'][0]['has_token'])
        self.assertEqual(self.config['custom'], {'keep': True})

    def test_update_preserves_token_and_native_fields(self):
        self.manage(action='save', name='example', url='https://example.com/mcp', token='saved')
        self.config['mcp_servers']['example']['tools'] = {'exclude': ['dangerous']}
        self.manage(action='save', name='example', url='https://example.com/mcp')
        self.assertEqual(self.tokens['MCP_EXAMPLE_API_KEY'], 'saved')
        self.assertEqual(self.config['mcp_servers']['example']['tools'], {'exclude': ['dangerous']})

    def test_changed_endpoint_cannot_reuse_saved_auth(self):
        self.manage(action='save', name='example', url='https://example.com/mcp', token='saved')
        with self.assertRaises(ValueError):
            self.manage(action='save', name='example', url='https://other.example/mcp')
        self.assertEqual(self.config['mcp_servers']['example']['url'], 'https://example.com/mcp')

    def test_clearing_authentication_removes_owned_token(self):
        self.manage(action='save', name='example', url='https://example.com/mcp', token='saved')
        self.manage(action='save', name='example', url='https://example.com/mcp', token='')
        self.assertEqual(self.tokens, {})
        self.assertNotIn('headers', self.config['mcp_servers']['example'])

    def test_remove_is_idempotent_and_removes_owned_token(self):
        self.manage(action='save', name='example', url='https://example.com/mcp', token='saved')
        self.manage(action='remove', name='example')
        result = self.manage(action='remove', name='example')
        self.assertEqual(result['servers'], [])
        self.assertEqual(self.tokens, {})

    def test_probe_returns_tools_without_server_secrets(self):
        self.manage(action='save', name='example', url='https://example.com/mcp', token='saved')
        result = self.manage(action='test', name='example')
        self.assertEqual(result['tools'], ['chat'])
        self.assertEqual(self.probes[0][0], 'example')
        with patch.object(sys.modules['hermes_cli.mcp_config'], '_probe_single_server', side_effect=RuntimeError('secret-token')):
            result = self.manage(action='test', name='example')
        self.assertFalse(result['ok'])
        self.assertNotIn('secret-token', json.dumps(result))

    def test_invalid_names_and_credential_urls_are_rejected(self):
        for name in ('A', 'a-b', '../a', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.manage(action='save', name=name, url='https://example.com/mcp')
        for url in ('http://example.com', 'https://user:secret@example.com', 'https://example.com?token=secret', 'https://[bad'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.manage(action='save', name='example', url=url)
        self.assertEqual(self.config['mcp_servers'], {})

    def test_native_entries_do_not_expose_secrets_or_break_snapshot(self):
        self.config['mcp_servers'] = {
            'native': {'url': 'https://example.com/mcp?secret=private', 'headers': {'X-Key': 'private'}},
            'stdio': {'command': 'server', 'args': ['--token', 'private']},
            'broken': {'url': 'https://[broken'},
        }
        result = native_mcp.public_servers(self.home)
        self.assertEqual(len(result), 3)
        self.assertNotIn('private', json.dumps(result))
        self.assertTrue(all(not row['editable'] for row in result))


class ImportServersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source, self.home = Path(self.temp.name) / 'source', Path(self.temp.name) / 'home'
        self.configs = {
            self.source: {'mcp_servers': {
                'linear': {'url': 'https://linear.example/mcp'},
                'github': {'command': 'github-mcp', 'env': {'GITHUB_TOKEN': '${GITHUB_PAT}', 'ROOT': '${userHome}'}},
                'search': {'url': 'https://search.example/mcp', 'headers': {'Authorization': 'Bearer ${env:SEARCH_KEY}'}},
                'notion': {'url': 'https://notion.example/mcp', 'auth': 'oauth'},
                'shared': {'url': 'https://shared.example/mcp', 'headers': {'X-Key': '${SHARED_KEY}'}},
                'unsafe': {'command': 'bash', 'args': ['-c', 'curl evil']},
            }},
            self.home: {'custom': {'keep': True}, 'mcp_servers': {'linear': {'url': 'https://own.example/mcp'}}},
        }
        self.env = {self.source: {'GITHUB_PAT': 'source-pat', 'SHARED_KEY': 'source-shared', 'OTHER': 'stays-behind'},
                    self.home: {'SHARED_KEY': 'coconut-own'}}
        self.saved = {}
        scope = ModuleType('agent.secret_scope')
        scope.load_env_file = lambda path: dict(self.env[Path(path).parent])
        config = ModuleType('hermes_cli.config')
        config.save_env_value = lambda key, value: self.saved.update({key: value})
        security = ModuleType('hermes_cli.mcp_security')
        security.validate_mcp_server_entry = lambda name, value: ['unsafe'] if value.get('command') == 'bash' else []
        tool_config = ModuleType('tools.mcp_tool_config')
        tool_config._ENV_VAR_PATTERN = __import__('re').compile(r'\$\{([^}]+)\}')
        tool_config._CONTEXT_VAR_RESOLVERS = {'userHome': None}
        common = ModuleType('tools.mcp_tool_common')
        common._env_ref_name = lambda ref: ref.strip().removeprefix('env:').strip()
        self.modules = patch.dict(sys.modules, {
            'agent': ModuleType('agent'), 'agent.secret_scope': scope, 'hermes_cli': ModuleType('hermes_cli'),
            'hermes_cli.config': config, 'hermes_cli.mcp_security': security, 'tools': ModuleType('tools'),
            'tools.mcp_tool_config': tool_config, 'tools.mcp_tool_common': common})
        self.modules.start(); self.addCleanup(self.modules.stop)
        def save(path, value): self.configs[Path(path)] = deepcopy(value)
        settings = patch.object(native_mcp, '_settings', return_value=(lambda path: deepcopy(self.configs[Path(path)]), save))
        settings.start(); self.addCleanup(settings.stop)

    def test_adds_only_new_servers_and_copies_only_referenced_variables(self):
        result = native_mcp.import_servers(self.source, self.home)
        self.assertEqual(result['added'], ['github', 'notion', 'search', 'shared'])
        self.assertEqual(result['needs_setup'], ['notion', 'search'])
        self.assertEqual(result['skipped'], ['linear', 'unsafe'])
        servers = self.configs[self.home]['mcp_servers']
        # An existing server and an existing value always win.
        self.assertEqual(servers['linear'], {'url': 'https://own.example/mcp'})
        self.assertEqual(self.saved, {'GITHUB_PAT': 'source-pat'})
        self.assertEqual(servers['github']['env'], {'GITHUB_TOKEN': '${GITHUB_PAT}', 'ROOT': '${userHome}'})
        self.assertNotIn('enabled', servers['github'])
        self.assertNotIn('enabled', servers['shared'])
        # A missing variable or an OAuth sign-in turns the server off until the person sets it up.
        self.assertIs(servers['search']['enabled'], False)
        self.assertIs(servers['notion']['enabled'], False)
        self.assertNotIn('unsafe', servers)
        self.assertEqual(self.configs[self.home]['custom'], {'keep': True})
        self.assertNotIn('source-pat', json.dumps(result))

    def test_a_second_import_changes_nothing(self):
        native_mcp.import_servers(self.source, self.home)
        before = deepcopy(self.configs[self.home])
        self.saved.clear()
        result = native_mcp.import_servers(self.source, self.home)
        self.assertEqual(result['added'], [])
        self.assertEqual(self.configs[self.home], before)
        self.assertEqual(self.saved, {})
