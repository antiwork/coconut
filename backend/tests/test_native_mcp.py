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
