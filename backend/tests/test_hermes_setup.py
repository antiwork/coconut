import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.hermes_setup import copy_logins, probe, source_home
from coconut.service import Service, ServiceError
from test_service import ControlledProvider


def hermes_home(root: Path) -> Path:
    home = root / 'user-hermes'
    (home / 'memories').mkdir(parents=True)
    (home / 'SOUL.md').write_text('You are the research bot.\n')
    (home / 'memories' / 'MEMORY.md').write_text('The user ships on Fridays.\n')
    (home / 'memories' / 'USER.md').write_text('')
    skill = home / 'skills' / 'research' / 'deep-dive'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('# Deep dive\n')
    (skill / 'notes.txt').write_text('Read the sources first.\n')
    (skill / 'run.sh').write_text('#!/bin/sh\necho ok\n')
    (skill / 'run.sh').chmod(0o755)
    (skill / '.env').write_text('SECRET=skill-only\n')
    nested = home / 'skills' / 'tools' / 'nested'
    (nested / 'a' / 'b' / 'c' / 'd' / '.cache').mkdir(parents=True)
    (nested / 'SKILL.md').write_text('# Nested\n')
    (nested / 'a' / 'b' / 'c' / 'd' / 'file.txt').write_text('Deep but small.\n')
    crowded = home / 'skills' / 'tools' / 'node-helper'
    (crowded / 'node_modules').mkdir(parents=True)
    (crowded / 'SKILL.md').write_text('# Node helper\n')
    for index in range(501):
        (crowded / 'node_modules' / f'{index}.js').write_text('')
    large = home / 'skills' / 'media' / 'big'
    large.mkdir(parents=True)
    (large / 'SKILL.md').write_text('# Big\n')
    (large / 'video.bin').write_bytes(b'0' * (5 * 1024 * 1024 + 1))
    (home / 'config.yaml').write_text(json.dumps({'model': {'default': 'claude-opus-5-5', 'provider': 'anthropic'},
                                                  'mcp_servers': {
        'linear': {'url': 'https://example.com', 'headers': {'Authorization': 'Bearer ${MCP_LINEAR_API_KEY}'}},
        'browser': {'command': 'browser-mcp', 'env': {'KEY': '${BROWSERBASE_API_KEY}', 'HOME': '${userHome}'}},
        'search': {'url': 'https://search.example/mcp', 'headers': {'X-Key': '${env:SEARCH_KEY}'}},
        'notion': {'url': 'https://notion.example/mcp', 'auth': 'oauth'}}}))
    (home / '.env').write_text('ANTHROPIC_API_KEY=sk-test-only\nTELEGRAM_BOT_TOKEN=not-a-model-login\nOPENAI_API_KEY=\n'
                               'MCP_LINEAR_API_KEY=tool-secret\nBROWSERBASE_API_KEY=tool-secret\n')
    (home / 'auth.json').write_text(json.dumps({'version': 1, 'providers': {'nous': {'access_token': 'test-only'}}}))
    (home / 'sessions').mkdir()
    (home / 'sessions' / 'private.json').write_text('{}')
    writer = home / 'profiles' / 'writer'
    writer.mkdir(parents=True)
    (writer / 'SOUL.md').write_text('You write release notes.\n')
    (home / 'profiles' / '..hidden').mkdir()
    return home


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = hermes_home(Path(self.directory.name))

    def tearDown(self):
        self.directory.cleanup()

    def test_probe_reports_counts_and_names_without_paths_or_secrets(self):
        result = probe(self.home)
        self.assertTrue(result['found'])
        self.assertEqual([item['id'] for item in result['profiles']], ['default', 'profile:writer'])
        default = result['profiles'][0]
        self.assertEqual((default['soul'], default['memories'], default['skills']), (True, 1, 4))
        self.assertEqual(result['model'], {'model': 'claude-opus-5-5', 'provider': 'anthropic'})
        self.assertEqual(result['logins'], [{'id': 'ANTHROPIC_API_KEY', 'label': 'Anthropic API key'}])
        self.assertEqual(result['sign_ins'], [{'id': 'nous', 'label': 'Nous Portal'}])
        # A server needs setup when .env does not define a variable that it refers to, or when it signs in with OAuth.
        self.assertEqual(result['mcp_servers'], [
            {'id': 'browser', 'transport': 'stdio', 'needs_setup': False},
            {'id': 'linear', 'transport': 'http', 'needs_setup': False},
            {'id': 'notion', 'transport': 'http', 'needs_setup': True},
            {'id': 'search', 'transport': 'http', 'needs_setup': True}])
        text = json.dumps(result)
        for secret in ('sk-test-only', 'test-only', 'tool-secret', str(self.home), 'TELEGRAM', 'MCP_LINEAR', 'BROWSERBASE', 'SEARCH_KEY'):
            self.assertNotIn(secret, text)

    def test_a_linked_or_missing_home_is_not_a_setup(self):
        link = Path(self.directory.name) / 'linked'
        link.symlink_to(self.home)
        self.assertIsNone(source_home({'COCONUT_HERMES_SOURCE': str(link)}))
        self.assertIsNone(source_home({'COCONUT_HERMES_SOURCE': str(Path(self.directory.name) / 'missing')}))
        self.assertEqual(probe(None), {'found': False, 'profiles': [], 'logins': [], 'sign_ins': [], 'model': None, 'mcp_servers': []})

    def test_copied_keys_never_replace_existing_coconut_keys_or_sign_ins(self):
        target = Path(self.directory.name) / 'coconut'
        target.mkdir()
        (target / '.env').write_text('ANTHROPIC_API_KEY=coconut-own\n')
        (target / 'auth.json').write_text(json.dumps({'version': 1, 'providers': {'codex': {'token': 'own'}}}))
        self.assertEqual(copy_logins(self.home, target, target), [])
        self.assertEqual((target / '.env').read_text(), 'ANTHROPIC_API_KEY=coconut-own\n')
        # Shared OAuth sign-ins log each other out, so they are never copied.
        self.assertEqual(sorted(json.loads((target / 'auth.json').read_text())['providers']), ['codex'])

    def test_keys_replace_a_blank_placeholder(self):
        target = Path(self.directory.name) / 'coconut'
        target.mkdir()
        (target / '.env').write_text('# Coconut\nANTHROPIC_API_KEY=\nOTHER=1\n')
        self.assertEqual(copy_logins(self.home, target, target), ['ANTHROPIC_API_KEY'])
        self.assertEqual((target / '.env').read_text(), "# Coconut\nOTHER=1\nANTHROPIC_API_KEY='sk-test-only'\n")
        self.assertEqual((target / '.env').stat().st_mode & 0o777, 0o600)
        self.assertFalse((target / 'auth.json').exists())


class KeyValueTests(unittest.TestCase):
    def test_a_referenced_key_is_written_with_its_resolved_value(self):
        try:
            import dotenv  # noqa: F401
        except ImportError:
            self.skipTest('The Hermes environment provides python-dotenv.')
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            source.mkdir()
            target.mkdir()
            (source / '.env').write_text('SHARED_KEY=sk-shared\nOPENAI_API_KEY=${SHARED_KEY}\n')
            self.assertEqual(copy_logins(source, target, target), ['OPENAI_API_KEY'])
            self.assertEqual((target / '.env').read_text(), "OPENAI_API_KEY='sk-shared'\n")

    def test_values_with_quotes_stay_exact(self):
        from coconut.hermes_setup import _env_line
        self.assertEqual(_env_line('OPENAI_API_KEY', 'a$b'), "OPENAI_API_KEY='a$b'")
        self.assertEqual(_env_line('OPENAI_API_KEY', 'it\'s "x"'), 'OPENAI_API_KEY="it\'s \\"x\\""')


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.home = hermes_home(root)
        self.environment = patch.dict(os.environ, {'COCONUT_HERMES_SOURCE': str(self.home)})
        self.environment.start()
        self.service = Service(root / 'data', ControlledProvider(), start_workers=False)
        self.default_home = root / 'coconut-default'
        self.service.provider.catalog = SimpleNamespace(root=self.default_home)
        self.device = self.service.pair({'code': self.service.pairing_code()})['device_id']

    def tearDown(self):
        self.service.close()
        self.environment.stop()
        self.directory.cleanup()

    def run_import(self, data, key=None):
        return self.service.mutate('POST', '/v1/setup/import', data, key or str(uuid4()), self.device)

    def test_import_copies_personality_memory_and_skills_but_not_sessions(self):
        result = self.run_import({'profile_ids': ['default'], 'logins': False})
        bot = result['bots'][0]
        self.assertEqual((bot['name'], bot['source_profile']), ('Hermes', 'default'))
        home = self.service.profiles.home(bot)
        self.assertEqual((home / 'SOUL.md').read_text(), 'You are the research bot.\n')
        self.assertIn('The user ships on Fridays.', (home / 'memories' / 'MEMORY.md').read_text())
        skill = home / 'skills' / 'research' / 'deep-dive'
        self.assertTrue((skill / 'notes.txt').exists())
        self.assertEqual((skill / 'run.sh').stat().st_mode & 0o777, 0o700)
        self.assertFalse((skill / '.env').exists())
        self.assertFalse((home / 'skills' / 'media').exists())
        self.assertFalse((home / 'skills' / 'tools' / 'node-helper').exists())
        # An ignored dot folder does not make a small skill too deep.
        self.assertTrue((home / 'skills' / 'tools' / 'nested' / 'a' / 'b' / 'c' / 'd' / 'file.txt').exists())
        self.assertFalse((home / 'skills' / 'tools' / 'nested' / 'a' / 'b' / 'c' / 'd' / '.cache').exists())
        self.assertEqual(result['skipped_skills'], ['media/big', 'tools/node-helper'])
        for private in ('.env', 'auth.json', 'sessions'):
            self.assertFalse((home / private).exists(), private)
        self.assertEqual(list((self.service.profiles.root / 'imports').iterdir()), [])
        self.assertTrue((home / 'coconut-profile.json').exists())
        self.assertEqual(result['logins'], [])
        self.assertFalse(self.default_home.exists())
        self.assertEqual(self.service.hermes_setup()['profiles'][0]['bot_id'], bot['id'])

    def test_import_is_idempotent_and_does_not_duplicate_bots(self):
        key = str(uuid4())
        first = self.run_import({'profile_ids': ['profile:writer'], 'logins': True}, key)
        self.assertEqual(self.run_import({'profile_ids': ['profile:writer'], 'logins': True}, key), first)
        again = self.run_import({'profile_ids': ['profile:writer'], 'logins': False})
        self.assertEqual(again['bots'][0]['id'], first['bots'][0]['id'])
        self.assertEqual(len([b for b in self.service.snapshot()['bots'] if b.get('source_profile')]), 1)
        self.assertEqual(first['logins'], ['ANTHROPIC_API_KEY'])
        self.assertIn("ANTHROPIC_API_KEY='sk-test-only'", (self.default_home / '.env').read_text())
        with self.assertRaises(ServiceError) as reused:
            self.run_import({'profile_ids': ['default'], 'logins': True}, key)
        self.assertEqual(reused.exception.status, 409)

    def test_unknown_profiles_and_bad_input_are_rejected(self):
        for data in ({'profile_ids': 'default'}, {'profile_ids': ['default'], 'logins': 'yes'}, {'profile_ids': [1]}):
            with self.assertRaises(ServiceError) as bad:
                self.run_import(data)
            self.assertEqual(bad.exception.status, 400)
        with self.assertRaises(ServiceError) as missing:
            self.run_import({'profile_ids': ['profile:../../etc'], 'logins': False})
        self.assertEqual(missing.exception.status, 409)
        self.assertEqual([b for b in self.service.snapshot()['bots'] if b.get('source_profile')], [])

    def test_mcp_servers_are_added_through_native_settings_only_with_consent(self):
        calls = []
        def native_settings(operation='read', bots=(), **payload):
            # Other writes to the default Hermes config wait for the import.
            self.assertTrue(self.service.model_settings_lock.locked())
            calls.append((operation, payload))
            return {'added': ['browser', 'notion'], 'needs_setup': ['notion'], 'skipped': ['linear'], 'servers': []}
        self.service.provider.native_settings = native_settings
        result = self.run_import({'profile_ids': [], 'logins': False})
        self.assertEqual((result['mcp_servers'], result['mcp_needs_setup']), ([], []))
        self.assertEqual(calls, [])
        result = self.run_import({'profile_ids': [], 'logins': False, 'mcp_servers': True})
        self.assertEqual(calls, [('mcp_import', {'source': str(self.home.resolve())})])
        self.assertEqual((result['mcp_servers'], result['mcp_needs_setup']), (['browser', 'notion'], ['notion']))
        self.assertIn('settings.updated', [row[0] for row in self.service.db.execute('SELECT type FROM events')])

    def test_mcp_writes_wait_for_other_settings_writes_and_replay_once(self):
        locked = []
        def native_settings(operation='read', bots=(), **payload):
            locked.append((payload['action'], self.service.model_settings_lock.locked()))
            return {'ok': True, 'tools': [], 'servers': []}
        self.service.provider.native_settings = native_settings
        key = str(uuid4())
        for action in ('save', 'test'):
            self.service.mutate('POST', '/v1/settings/mcp', {'action': action, 'name': 'linear'}, key if action == 'save' else str(uuid4()), self.device)
        self.service.mutate('POST', '/v1/settings/mcp', {'action': 'save', 'name': 'linear'}, key, self.device)
        self.assertEqual(locked, [('save', True), ('test', True)])

    def test_mcp_import_failures_are_reported(self):
        def native_settings(operation='read', bots=(), **payload):
            raise ValueError('Hermes could not read its model settings.')
        with self.assertRaises(ServiceError) as unavailable:
            self.run_import({'profile_ids': [], 'logins': False, 'mcp_servers': True})
        self.assertEqual(unavailable.exception.status, 503)
        self.service.provider.native_settings = native_settings
        with self.assertRaises(ServiceError) as failed:
            self.run_import({'profile_ids': [], 'logins': False, 'mcp_servers': True})
        self.assertEqual(failed.exception.status, 409)
        with self.assertRaises(ServiceError) as bad:
            self.run_import({'profile_ids': [], 'mcp_servers': 'yes'})
        self.assertEqual(bad.exception.status, 400)

    def test_coconut_does_not_import_its_own_hermes_home(self):
        with patch.dict(os.environ, {'COCONUT_HERMES_SOURCE': str(self.default_home)}):
            self.default_home.mkdir()
            (self.default_home / 'SOUL.md').write_text('Coconut default.\n')
            self.assertFalse(self.service.hermes_setup()['found'])


if __name__ == '__main__':
    unittest.main()
