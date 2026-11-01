import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from coconut.bot_changes import bot_creation_receipt
from coconut.providers import FixtureProvider, HermesProvider
from turn_text import turn_input


class FakeNativeCatalog:
    def __init__(self, root):
        self.data_dir = root
        self.root = root / 'default'
    def _profile_home(self, identity):
        return self.data_dir / 'bots' / identity / 'home'
    def _call(self, operation, **payload):
        return {'profiles': {identity: {'instructions': (Path(home)/'SOUL.md').read_text() if (Path(home)/'SOUL.md').exists() else 'Keep replies brief.'} for identity, home in payload.get('homes', {}).items()}, 'approvals': {'default': 'off', 'modes': []}}
    def get(self):
        return {'default': {'model': 'claude-test', 'provider': 'anthropic', 'base_url': None, 'reasoning_effort': None},
                'models': [], 'default_configured': True}

    def resolve(self, selection, catalog=None):
        return dict((catalog or self.get())['default'])

    def read_profiles(self, identities):
        return {identity: {'model_override': None} for identity in identities}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bot = {'id': str(uuid4()), 'session_id': str(uuid4()), 'instructions': 'Keep replies brief.'}
        self.task = {'id': str(uuid4()), 'title': 'Check the total', 'bot_id': self.bot['id']}
        # Each bot home is <root>/bots/<id>/home, as these tests lay it out. The service keeps the real rows.
        from profile_layout import LayoutRows
        rows = patch.object(HermesProvider, 'profile_rows', LayoutRows(self.root), create=True)
        rows.start()
        self.addCleanup(rows.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_fixture_independent_task_does_not_propose_file(self):
        events = []
        history = [{'role': 'user', 'task_id': self.task['id'], 'text': 'Check that 17 multiplied by 19 equals 323. Give a brief answer. Do not create a file.'}]
        FixtureProvider(self.root).start_task(self.task, self.bot, history, lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual([kind for kind, _ in events], ['text_delta', 'completed'])
        self.assertIn('323', events[0][1]['text'])

    def test_fixture_proposal_has_prediction_and_does_not_write(self):
        events = []
        history = [{'role': 'user', 'task_id': self.task['id'], 'text': 'Create morning-note.md containing a short morning review.'}]
        FixtureProvider(self.root).start_task(self.task, self.bot, history, lambda kind, data: kind == 'phase' or events.append((kind, data)))
        proposal = events[-1][1]
        self.assertEqual(events[-1][0], 'decision')
        self.assertEqual(proposal['action']['filename'], 'morning-note.md')
        self.assertEqual(proposal['predicted_choice'], 'approve')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_resume_requires_execution_receipt_state(self):
        provider = FixtureProvider(self.root)
        with self.assertRaisesRegex(RuntimeError, 'not confirmed'):
            provider.resume_task(self.task, self.bot, [], {'status': 'approved'}, lambda *_: None)

    def test_bot_homes_and_model_credentials_are_scoped(self):
        provider = HermesProvider(self.root)
        managed_home = self.root / 'bots' / self.bot['id'] / 'home'
        managed_home.mkdir(parents=True)
        existing_config = {'model': {'default': 'profile-model'}, 'tools': {'tool_search': {'enabled': 'on'}}, 'custom': {'keep': True}}
        (managed_home / 'config.yaml').write_text(json.dumps(existing_config))
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'excluded', 'AWS_SECRET_ACCESS_KEY': 'excluded', 'SSH_AUTH_SOCK': 'excluded', 'ANTHROPIC_API_KEY': 'model-only'}, clear=True):
            first, first_cwd = provider.environment(self.bot['id'])
            second, second_cwd = provider.environment(str(uuid4()))
        self.assertEqual(first['HOME'], second['HOME'])
        # Tools use the Mac account folders. A profile .env can still select its own.
        for key in ('CODEX_HOME', 'GH_CONFIG_DIR', 'XDG_CONFIG_HOME', 'CLAUDE_CONFIG_DIR'):
            self.assertNotIn(key, first)
            self.assertNotIn(key, second)
        self.assertNotEqual(first_cwd, second_cwd)
        self.assertNotEqual(first['COCONUT_BROWSER_PROFILE'], second['COCONUT_BROWSER_PROFILE'])
        self.assertNotEqual(first['HOME'], first['HERMES_HOME'])
        self.assertNotIn('GITHUB_TOKEN', first)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', first)
        self.assertNotIn('SSH_AUTH_SOCK', first)
        self.assertEqual(first['ANTHROPIC_API_KEY'], 'model-only')
        config = json.loads((Path(first['HERMES_HOME']) / 'config.yaml').read_text())
        self.assertEqual(config, existing_config)
        with self.assertRaises(ValueError):
            provider.environment('../../outside')

    def test_each_bot_worker_reads_the_tool_store_of_its_own_home(self):
        provider = HermesProvider(self.root)
        # A store that the service environment names never reaches a bot.
        with patch.dict(os.environ, {'HERMES_RUNTIME_DIR': '/Users/person/.hermes/tools', 'HERMES_HOME': '/Users/person/.hermes'},
                        clear=True):
            first, _ = provider.environment(self.bot['id'])
            second, _ = provider.environment(str(uuid4()))
        for env in (first, second):
            self.assertEqual(env['HERMES_RUNTIME_DIR'], str(Path(env['HERMES_HOME']) / 'tools'))
            self.assertTrue(Path(env['HERMES_RUNTIME_DIR']).is_relative_to(self.root.resolve() / 'bots'))
        self.assertEqual(Path(first['HERMES_HOME']), self.root.resolve() / 'bots' / self.bot['id'] / 'home')
        self.assertNotEqual(first['HERMES_RUNTIME_DIR'], second['HERMES_RUNTIME_DIR'])
        # The Mac account home stays the HOME of each bot.
        self.assertEqual(first['HOME'], second['HOME'])
        self.assertNotEqual(first['HOME'], first['HERMES_HOME'])

    def test_every_bot_process_uses_the_login_folders_of_the_bot_env(self):
        provider = HermesProvider(self.root)
        home = self.root / 'bots' / self.bot['id'] / 'home'
        home.mkdir(parents=True)
        (home / '.env').write_text(f'GH_CONFIG_DIR={home}/.config/gh\nXDG_CONFIG_HOME="{home}/.config"\nOPENAI_API_KEY=not-a-folder\n')
        with patch.dict(os.environ, {'GH_CONFIG_DIR': '/service/gh'}, clear=True):
            env, _ = provider.environment(self.bot['id'])
            other, _ = provider.environment(str(uuid4()))
        # Work sources and process checks run gh outside a turn. They must use the bot's own GitHub login.
        self.assertEqual(env['GH_CONFIG_DIR'], str(home / '.config/gh'))
        self.assertEqual(env['XDG_CONFIG_HOME'], str(home / '.config'))
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('GH_CONFIG_DIR', other)

    def test_homebrew_trust_is_shared_without_sharing_bot_settings(self):
        provider = HermesProvider(self.root)
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': '/private/settings',
                                     'HOMEBREW_XDG_CONFIG_HOME': '/private/brew',
                                     'HOMEBREW_NO_REQUIRE_TAP_TRUST': '1'}, clear=True):
            first, _ = provider.environment(self.bot['id'])
            second, _ = provider.environment(str(uuid4()))
        first_config = Path(first['HOMEBREW_XDG_CONFIG_HOME']) / 'homebrew'
        first_config.mkdir()
        trust = {'trustedformulae': ['example/tools/tool']}
        (first_config / 'trust.json').write_text(json.dumps(trust))
        second_file = Path(second['HOMEBREW_XDG_CONFIG_HOME']) / 'homebrew' / 'trust.json'
        self.assertEqual(json.loads(second_file.read_text()), trust)
        self.assertTrue(second_file.is_relative_to(self.root.resolve()))
        self.assertEqual(first_config.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(first['HOME'], second['HOME'])
        self.assertNotEqual(first['HERMES_HOME'], second['HERMES_HOME'])
        for environment in (first, second):
            # The service's own XDG folder does not reach the bots. Bots use the Mac account folders.
            self.assertNotIn('XDG_CONFIG_HOME', environment)
            self.assertNotIn('CLAUDE_CONFIG_DIR', environment)
            self.assertNotIn('HOMEBREW_NO_REQUIRE_TAP_TRUST', environment)
            self.assertNotEqual(environment['HOMEBREW_XDG_CONFIG_HOME'], environment['HOME'])

    def test_context_contains_exact_pending_proposal(self):
        request = {'operation': 'context', 'task': {'pending_decision': {'proposal_preview': 'Exact approved-visible bytes'}}, 'history': [], 'question': 'What does this file contain?'}
        prompt, _ = turn_input(request)
        self.assertIn('Exact approved-visible bytes', prompt)
        self.assertIn('Do not execute it', prompt)
        self.assertIn('Do not change the pending proposal', prompt)

    def test_restarted_handoff_receipts_and_return_state_reach_the_prompt(self):
        self.task['_outgoing_bot_messages'] = [{'target_bot_id': 'recipient', 'text': 'The exact request already delivered.', 'task_id': 'receipt-task', 'status': 'completed', 'memory': 'PRIVATE_RECEIPT_MEMORY'}]
        self.task['_is_return'] = True
        prompt, _ = turn_input({'operation': 'start', 'task': self.task, 'history': []})
        self.assertIn('The exact request already delivered.', prompt)
        self.assertIn('Do not resend or rephrase', prompt)
        self.assertIn('Do not send an acknowledgement back', prompt)
        self.assertNotIn('PRIVATE_RECEIPT_MEMORY', prompt)

    def test_attachments_supply_text_and_mark_binary_limits(self):
        self.task['attachments'] = [{'title': 'note.txt', 'content_base64': base64.b64encode(b'Private task input').decode()}, {'title': 'photo.png', 'content_base64': base64.b64encode(b'\xff\xff').decode()}]
        prompt, _ = turn_input({'operation': 'start', 'task': self.task, 'history': []})
        self.assertIn('Private task input', prompt)
        self.assertIn('This attachment is binary', prompt)

    def configure_peer(self, *, budget=8):
        peer = {'id': str(uuid4()), 'name': 'Lin', 'role': 'Check calculations'}
        self.task['_available_bots'] = [peer]
        self.task['_handoff_budget_remaining'] = budget
        return peer






















    def test_fixture_team_exchange_returns_once_without_an_acknowledgement_loop(self):
        provider = FixtureProvider(self.root)
        first = self.bot | {'name': 'Ada'}
        second = {'id': str(uuid4()), 'name': 'Lin', 'role': 'Calculations', 'session_id': str(uuid4())}
        deliveries = []
        outputs = []
        def emit(kind, data):
            if kind == 'bot_message':
                deliveries.append(data)
                return {'status': 'queued', 'target_bot_id': data['target_bot_id']}
            if kind == 'text_delta':
                outputs.append(data['text'])
        start = self.task | {'title': 'TEAM_MILESTONE_HANDOFF: Ask Lin for 17 multiplied by 19.', '_available_bots': [second], '_handoff_budget_remaining': 8}
        provider.start_task(start, first, [], emit)
        reply = self.task | {'id': str(uuid4()), 'bot_id': second['id'], '_available_bots': [first], '_handoff_budget_remaining': 7, '_bot_messages': [{'sender_bot_id': first['id'], 'sender_name': 'Ada', 'text': deliveries[0]['text']}]}
        provider.start_task(reply, second, [], emit)
        result = self.task | {'id': str(uuid4()), '_available_bots': [second], '_handoff_budget_remaining': 6, '_bot_messages': [{'sender_bot_id': second['id'], 'sender_name': 'Lin', 'text': deliveries[1]['text']}]}
        provider.start_task(result, first, [], emit)
        self.assertEqual([delivery['target_bot_id'] for delivery in deliveries], [second['id'], first['id']])
        self.assertEqual(len(deliveries), 2)
        self.assertIn('323', outputs[-1])


    def creation_request(self, name='Scout'):
        source = {'id': str(uuid4()), 'text': 'Create Scout to check calculations, then ask it to check 17 times 19.'}
        self.task['_bot_creation_sources'] = [source]
        self.task['_handoff_budget_remaining'] = 1
        arguments = {'source_message_id': source['id'], 'name': name, 'role': 'Check calculations',
                     'instructions': 'Check calculations and return a concise result.'}
        identifier = str(uuid4())
        receipt = {'status': 'created', 'bot_id': identifier, 'profile_id': identifier,
                   'session_id': str(uuid4()), 'name': name, 'role': arguments['role'],
                   'source_message_id': source['id']}
        return arguments, receipt

    def test_creation_receipt_accepts_native_name_normalization_and_strips_extras(self):
        arguments, receipt = self.creation_request(name='  Ｓｃｏｕｔ  Team ')
        receipt.update(name='scout team', private_token='PRIVATE')
        confirmed = bot_creation_receipt(receipt, arguments, self.bot)
        self.assertEqual(confirmed['bot_id'], receipt['bot_id'])
        self.assertNotIn('private_token', confirmed)

    def test_creation_receipt_rejects_wrong_source_identity_name_and_role(self):
        arguments, receipt = self.creation_request()
        for changes in ({'source_message_id': 'wrong'}, {'name': 'Someone else'}, {'role': 'Different role'},
                        {'bot_id': 'not-a-uuid'}, {'profile_id': str(uuid4())},
                        {'session_id': receipt['bot_id']}, {'session_id': self.bot['session_id']},
                        {'bot_id': self.bot['id'], 'profile_id': self.bot['id']}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(ValueError):
                bot_creation_receipt(receipt | changes, arguments, self.bot)


if __name__ == '__main__':
    unittest.main()
