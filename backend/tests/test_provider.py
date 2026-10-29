import base64
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from coconut.hermes_worker import (BOT_CREATION_SCHEMA, PROPOSAL_SCHEMA, bot_creation_payload,
                                   bot_creation_receipt, conversation_input,
                                   file_proposal, model_configuration)
from coconut.providers import FixtureProvider, HermesProvider
from coconut.service import ServiceError


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

    def resolve_runtime(self, selection, bot_id=None):
        return model_configuration(selection['model'], selection['base_url'])

    def worker_limit(self):
        return 4


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

    @staticmethod
    def proposal_arguments():
        return {
            'title': 'Which note should I save?',
            'explanation': 'Choose the file content.', 'recommended_option_id': 'full', 'predicted_option_id': 'change',
            'options': [
                {'id': 'full', 'label': 'Full version', 'explanation': 'Keep the complete note.', 'answer': 'approve', 'action': {'type': 'create_file', 'filename': 'note.md', 'content': 'Exact bytes'}},
                {'id': 'brief', 'label': 'Brief version', 'explanation': 'Keep the short note.', 'answer': 'approve', 'action': {'type': 'create_file', 'filename': 'note.md', 'content': 'Brief bytes'}},
                {'id': 'change', 'label': 'Revise the note', 'explanation': 'Request simpler text.', 'answer': 'modify', 'modification': 'Use fewer words.'},
            ],
        }

    def test_options_preserve_exact_actions_and_predicted_selection(self):
        proposal = file_proposal(self.proposal_arguments())
        self.assertEqual(proposal['title'], 'Which note should I save?')
        self.assertEqual(proposal['options'][1]['action']['content'], 'Brief bytes')
        self.assertEqual(proposal['action']['content'], 'Exact bytes')
        self.assertEqual(proposal['recommended_option_id'], 'full')
        self.assertEqual(proposal['predicted_option_id'], 'change')
        self.assertEqual(proposal['predicted_choice'], 'modify')
        self.assertEqual(proposal['options'][2]['modification'], 'Use fewer words.')

    def test_proposal_rejects_an_oversized_frame_before_emitting_it(self):
        for first, second in [('a' * 700_000, 'b' * 700_000), ('\x00' * 200_000, 'Short alternative')]:
            arguments = self.proposal_arguments()
            arguments['options'][0]['action']['content'] = first
            arguments['options'][1]['action']['content'] = second
            with self.subTest(escaped=first.startswith('\x00')), self.assertRaisesRegex(ValueError, 'combined proposal'):
                file_proposal(arguments)

    def test_proposal_accepts_one_or_eight_distinct_choices_without_templates(self):
        for count in (1, 8):
            with self.subTest(count=count):
                arguments = self.proposal_arguments()
                arguments['options'] = [
                    {'id': f'choice_{index}', 'label': f'Keep {index} samples', 'explanation': f'Include {index} measured samples.', 'answer': 'approve', 'action': {'type': 'create_file', 'filename': 'samples.md', 'content': f'Samples: {index}'}}
                    for index in range(count)
                ]
                arguments.update(title='How many samples should the report include?', recommended_option_id='choice_0', predicted_option_id='choice_0')
                proposal = file_proposal(arguments)
                self.assertEqual(proposal['options'], arguments['options'])
                self.assertEqual(proposal['title'], arguments['title'])
        schema = PROPOSAL_SCHEMA['parameters']['properties']['options']
        self.assertEqual((schema['minItems'], schema['maxItems']), (1, 8))

    def test_proposal_requires_a_bounded_model_title_and_choice_count(self):
        for title in (None, '', ' ' * 3, 'x' * 161):
            arguments = self.proposal_arguments() | {'title': title}
            with self.subTest(title=title), self.assertRaisesRegex(ValueError, 'title'):
                file_proposal(arguments)
        for count in (0, 9):
            arguments = self.proposal_arguments()
            arguments['options'] = arguments['options'][:1] * count
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, 'one to eight'):
                file_proposal(arguments)

    def test_options_cannot_hide_the_same_action_behind_different_labels(self):
        arguments = self.proposal_arguments()
        arguments['options'][1]['action'] = arguments['options'][0]['action'].copy()
        with self.assertRaisesRegex(ValueError, 'different outcome'):
            file_proposal(arguments)

    def test_reject_option_cannot_carry_an_ignored_action(self):
        arguments = self.proposal_arguments()
        arguments['options'][1]['answer'] = 'reject'
        with self.assertRaisesRegex(ValueError, 'cannot carry an executable action'):
            file_proposal(arguments)

    def test_modify_option_requires_an_exact_correction(self):
        arguments = self.proposal_arguments()
        del arguments['options'][2]['modification']
        with self.assertRaisesRegex(ValueError, 'exact requested change'):
            file_proposal(arguments)

    def test_prediction_must_identify_an_existing_choice(self):
        arguments = self.proposal_arguments()
        arguments['predicted_option_id'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'listed choices'):
            file_proposal(arguments)

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

    def test_custom_endpoint_never_receives_ambient_provider_key(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'private'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'credential binding'):
                model_configuration('claude-test', 'https://unrelated.example/v1')
            self.assertEqual(model_configuration('local-model', 'http://127.0.0.1:11434/v1')['api_key'], 'local')
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                model_configuration('claude-test', 'http://api.anthropic.com')

    def test_context_contains_exact_pending_proposal(self):
        request = {'operation': 'context', 'task': {'pending_decision': {'proposal_preview': 'Exact approved-visible bytes'}}, 'history': [], 'question': 'What does this file contain?'}
        prompt, _ = conversation_input(request)
        self.assertIn('Exact approved-visible bytes', prompt)
        self.assertIn('Do not execute it', prompt)
        self.assertIn('Do not change the pending proposal', prompt)

    def test_restarted_handoff_receipts_and_return_state_reach_the_prompt(self):
        self.task['_outgoing_bot_messages'] = [{'target_bot_id': 'recipient', 'text': 'The exact request already delivered.', 'task_id': 'receipt-task', 'status': 'completed', 'memory': 'PRIVATE_RECEIPT_MEMORY'}]
        self.task['_is_return'] = True
        prompt, _ = conversation_input({'operation': 'start', 'task': self.task, 'history': []})
        self.assertIn('The exact request already delivered.', prompt)
        self.assertIn('Do not resend or rephrase', prompt)
        self.assertIn('Do not send an acknowledgement back', prompt)
        self.assertNotIn('PRIVATE_RECEIPT_MEMORY', prompt)

    def test_attachments_supply_text_and_mark_binary_limits(self):
        self.task['attachments'] = [{'title': 'note.txt', 'content_base64': base64.b64encode(b'Private task input').decode()}, {'title': 'photo.png', 'content_base64': base64.b64encode(b'\xff\xff').decode()}]
        prompt, _ = conversation_input({'operation': 'start', 'task': self.task, 'history': []})
        self.assertIn('Private task input', prompt)
        self.assertIn('This attachment is binary', prompt)

    def fake_hermes(self, behavior, *, preference_result=None, forbid_preference_derivation=False, expected_preference_input=None, preference_cases=None):
        source = self.root / 'source'
        (source / 'tools').mkdir(parents=True)
        (source / 'tools' / '__init__.py').write_text('')
        (source / 'tools' / 'registry.py').write_text(textwrap.dedent('''
            class Registry:
                handlers = {}
                def register(self, name, toolset, schema, handler):
                    self.handlers[name] = handler
                def deregister(self, name): self.handlers.pop(name, None)
                def dispatch(self, name, args, **kwargs):
                    return self.handlers[name](args)
            registry = Registry()
        '''))
        from native_stub import install_native_stub
        install_native_stub(source)
        (source / 'toolsets.py').write_text('def create_custom_toolset(*args, **kwargs): pass\n')
        (source / 'run_agent.py').write_text(textwrap.dedent('''
            from tools.registry import registry
            import json
            class TodoStore:
                def __init__(self): self.todos, self.revision = [], 0
                def restore(self, todos, revision): self.todos, self.revision = todos, revision
                def snapshot(self): return {'todos': self.todos, 'revision': self.revision}
            class AIAgent:
                def redirect(self, text):
                    self.pending_live = getattr(self, 'pending_live', '') + text
                    return True
                def _drain_pending_redirect(self):
                    text, self.pending_live = getattr(self, 'pending_live', ''), ''
                    return text
                def _drain_pending_steer(self): return None
                def close(self): pass
                def release_clients(self): pass
                def __init__(self, **kwargs):
                    from tools.bot_relay import message_agent
                    registry.handlers['message_agent'] = message_agent
                    self._todo_store = TodoStore()
                    self.preference_check = kwargs.get('enabled_toolsets') == []
                    self.valid_tool_names = set() if self.preference_check else set(registry.handlers)
                    self.system_prompt = kwargs.get('ephemeral_system_prompt', '')
                    self.skip_memory = kwargs.get('skip_memory')
                    self.interim_assistant_callback = kwargs.get('interim_assistant_callback')
                def run_conversation(self, prompt, **kwargs):
                    if self.preference_check:
                        assert not FORBID_PREFERENCE_DERIVATION
                        assert set(__import__('json').loads(prompt)) == {'source', 'preferences', 'earlier_sources', 'later_sources'}
                        if EXPECTED_PREFERENCE_INPUT is not None:
                            assert __import__('json').loads(prompt) == EXPECTED_PREFERENCE_INPUT
                        assert kwargs.get('conversation_history') == []
                        if PREFERENCE_CASES is not None:
                            data = __import__('json').loads(prompt)
                            case = PREFERENCE_CASES[data['source']['id']]
                            assert data == case['input']
                            return {'completed': True, 'final_response': __import__('json').dumps(case['result'])}
                        return {'completed': True, 'final_response': PREFERENCE_RESULT}
                    print('This noisy Hermes output must never become an API event.')
        ''').replace('PREFERENCE_RESULT', repr(json.dumps(preference_result or {'no_change': True}))).replace('FORBID_PREFERENCE_DERIVATION', repr(forbid_preference_derivation)).replace('EXPECTED_PREFERENCE_INPUT', repr(expected_preference_input)).replace('PREFERENCE_CASES', repr(preference_cases)) + textwrap.indent(textwrap.dedent(behavior), '        '))
        # Bot lifecycle tests acknowledge tool journaling. Service tests check its durability.
        class ReceiptProvider(HermesProvider):
            def _run(self, operation, task, bot, history, emit, **extra):
                def acknowledge(kind, payload):
                    if kind == 'steering_ready': return None
                    if kind == 'native_call': return {'status': 'recorded', 'id': str(uuid4())}
                    if kind == 'native_result': return {'status': 'recorded'}
                    return emit(kind, payload)
                return super()._run(operation, task, bot, history, acknowledge, **extra)
        provider = ReceiptProvider(self.root / 'data', python=sys.executable, hermes_root=source, catalog=FakeNativeCatalog(self.root / 'data'))
        # Each bot home is <data>/bots/<id>/home, as these tests lay it out. A service keeps the real rows.
        from profile_layout import LayoutRows
        provider.profile_rows = LayoutRows(provider.data_dir)
        self.addCleanup(provider.close)
        return provider

    def test_context_does_not_receive_the_source_claim(self):
        provider = self.fake_hermes("""
            import os
            assert not os.environ.get('HERMES_KANBAN_TASK')
            return {'completed': True, 'final_response': 'Context remains separate.'}
        """)
        task = dict(self.task, _kanban_claim=dict(native_id='t_source', run_id=17, claim_lock='claim-source'))
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.answer_context(task, self.bot, [], 'Explain the result.', lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events, [('text_delta', {'text': 'Context remains separate.'})])

    def test_source_worker_receives_its_native_claim(self):
        provider = self.fake_hermes("""
            import os
            assert os.environ.get('HERMES_KANBAN_TASK') == 't_source'
            assert os.environ.get('HERMES_KANBAN_RUN_ID') == '17'
            assert os.environ.get('HERMES_KANBAN_CLAIM_LOCK') == 'claim-source'
            return {'completed': True, 'final_response': 'Claim retained.'}
        """)
        task = dict(self.task, _kanban_claim=dict(native_id='t_source', run_id=17, claim_lock='claim-source'))
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertTrue(any(kind == 'completed' for kind, _ in events), events)

    def test_nested_tool_on_a_native_context_thread_finishes(self):
        provider = self.fake_hermes("""
            import contextvars, threading
            registry.handlers['read_file'] = lambda args: 'nested result'
            def execute(args):
                results = []
                context = contextvars.copy_context()
                worker = threading.Thread(target=lambda: context.run(
                    lambda: results.append(registry.dispatch('read_file', {'path': 'note.txt'}))), daemon=True)
                worker.start()
                worker.join(timeout=1)
                assert not worker.is_alive(), 'Nested tool blocked on the parent execution lock'
                return results[0]
            registry.handlers['execute_code'] = execute
            result = registry.dispatch('execute_code', {'code': 'read_file()'})
            return {'completed': True, 'final_response': result}
        """)
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events, [('text_delta', {'text': 'nested result'}), ('completed', {'summary': 'Complete.'})])

    def test_nested_routine_tools_share_the_parent_claim_fence(self):
        self.task['_native_job'] = {'id': 'routine', 'fire_claim': {'by': 'owner'}}
        provider = self.fake_hermes("""
            import contextvars, threading, contextlib, sys, types
            entered = []
            @contextlib.contextmanager
            def fence(job_id, *, expected_owner):
                assert not entered, 'Nested tool tried to reacquire its parent claim fence'
                assert (job_id, expected_owner) == ('routine', 'owner')
                entered.append(True)
                try: yield True
                finally: entered.pop()
            sys.modules['cron'] = types.ModuleType('cron')
            jobs = types.ModuleType('cron.jobs')
            jobs.fire_claim_fence = fence
            sys.modules['cron.jobs'] = jobs
            def leaf(args):
                assert entered, 'Nested tool lost the routine claim fence'
                return 'fenced result'
            registry.handlers['read_file'] = leaf
            def execute(args):
                results = []
                context = contextvars.copy_context()
                worker = threading.Thread(target=lambda: context.run(
                    lambda: results.append(registry.dispatch('read_file', {}))), daemon=True)
                worker.start()
                worker.join(timeout=1)
                assert not worker.is_alive()
                return results[0]
            registry.handlers['execute_code'] = execute
            result = registry.dispatch('execute_code', {})
            assert not entered
            return {'completed': True, 'final_response': result}
        """)
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events, [('text_delta', {'text': 'fenced result'}), ('completed', {'summary': 'Complete.'})])

    def test_background_terminal_survives_reply_and_accepts_input_next_turn(self):
        provider = self.fake_hermes("""
            import os, pty, select, subprocess, sys
            import run_agent
            if not hasattr(run_agent, 'login'):
                master, slave = pty.openpty()
                child = subprocess.Popen([sys.executable, '-u', '-c',
                    "print('Authorize on your phone', flush=True); input(); print('Saved', flush=True)"],
                    stdin=slave, stdout=slave, stderr=slave)
                os.close(slave)
                run_agent.login = (child, master)
                assert select.select([master], [], [], 2)[0]
                os.read(master, 4096)
                return {'completed': True, 'final_response': 'Authorize on your phone.'}
            child, master = run_agent.login
            assert child.poll() is None, 'The reply killed the terminal'
            os.write(master, b'authorized\\n')
            output = b''
            while b'Saved' not in output:
                assert select.select([master], [], [], 2)[0], 'The terminal lost its output'
                output += os.read(master, 4096)
            child.wait(timeout=2)
            os.close(master)
            return {'completed': True, 'final_response': 'Saved.'}
        """)
        self.addCleanup(lambda: getattr(provider, 'close', lambda: None)())
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
            followup = dict(self.task, id=str(uuid4()), title='Done')
            provider.start_task(followup, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual([data['text'] for kind, data in events if kind == 'text_delta'],
                         ['Authorize on your phone.', 'Saved.'])

    def test_decision_retains_runtime_and_removes_previous_turn_tools(self):
        provider = self.fake_hermes("""
            import run_agent
            if not hasattr(run_agent, 'asked'):
                run_agent.asked = True
                registry.dispatch('coconut_ask', {'title': 'Continue?', 'options': [{'id': 'yes', 'label': 'Yes'}]})
                raise AssertionError('A paused turn must not execute more tools')
            assert 'coconut_create_bot' not in registry.handlers
            return {'completed': True, 'final_response': 'Same runtime.'}
        """)
        class Paused(Exception): pass
        def pending(kind, data):
            if kind == 'native_question': raise Paused()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            with self.assertRaises(Paused):
                provider.start_task(self.task, self.bot, [], pending)
            pid = provider._runtimes[self.bot['id']].pid
            events = []
            provider.answer_context(self.task, self.bot, [], 'Explain', lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(provider._runtimes[self.bot['id']].pid, pid)
        self.assertEqual(events, [('text_delta', {'text': 'Same runtime.'})])
        process = provider._runtimes[self.bot['id']]
        provider.cancel_task(self.task['id'])
        self.assertIsNone(process.poll())

    def test_rejected_source_check_keeps_the_same_worker_running_without_a_card(self):
        provider = self.fake_hermes("""
            import json
            assert 'If the choice is already settled' in self.system_prompt
            result = json.loads(registry.dispatch('coconut_ask', {
                'title': 'Repair?', 'options': [{'id': 'fix', 'label': 'Repair'}]}))
            assert result['status'] == 'validation_failed'
            assert 'No card was created' in result['error']
            return {'completed': True, 'final_response': 'The source already settles this choice.'}
        """)
        events = []
        def receipt(kind, data):
            events.append((kind, data))
            if kind == 'native_call':
                return {'status': 'recorded', 'id': str(uuid4())}
            if kind == 'native_question':
                raise ServiceError(400, 'No card was created. The GitHub source changed.')
            if kind == 'native_result':
                return {'status': 'recorded'}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            HermesProvider._run(provider, 'start', self.task, self.bot, [], receipt)
        self.assertEqual([data['text'] for kind, data in events if kind == 'text_delta'],
                         ['The source already settles this choice.'])
        self.assertFalse(any(kind == 'failed' for kind, _ in events))
        self.assertEqual(sum(kind == 'native_question' for kind, _ in events), 1)

    def test_queued_tool_cannot_run_after_a_question_pauses_the_turn(self):
        side_effect = self.root / 'unexpected-write'
        provider = self.fake_hermes("""
            from pathlib import Path
            registry.register(name='terminal', toolset='terminal', schema={},
                              handler=lambda args: Path(SIDE_EFFECT).touch() or 'wrote')
            try:
                registry.dispatch('coconut_ask', {'title': 'Continue?', 'options': [{'id': 'yes', 'label': 'Yes'}]})
            except BaseException:
                pass
            registry.dispatch('terminal', {'command': 'queued work'})
            return {'completed': True, 'final_response': 'This must not run.'}
        """.replace('SIDE_EFFECT', repr(str(side_effect))))
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertFalse(side_effect.exists())
        self.assertEqual([kind for kind, _ in events], ['native_question'])

    def test_retained_runtime_does_not_repeat_old_tool_receipts(self):
        provider = self.fake_hermes("""
            registry.register(name='terminal', toolset='terminal', schema={}, handler=lambda args: 'ok')
            result = registry.dispatch('terminal', {'command': 'check'})
            return {'completed': True, 'final_response': result}
        """)
        # Use the real receipt path instead of the lifecycle-test auto-ack wrapper.
        records = []
        def receipt(kind, data):
            if kind == 'native_call':
                records.append(data)
                return {'status': 'recorded', 'id': str(uuid4())}
            if kind == 'native_result': return {'status': 'recorded'}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            for _ in range(3):
                HermesProvider._run(provider, 'start', dict(self.task, id=str(uuid4())), self.bot, [], receipt)
        self.assertEqual(len(records), 3)

    def test_live_input_crosses_worker_pipe_and_reports_consumption(self):
        marker = self.root / 'live-ready'
        provider = self.fake_hermes("""
            import time
            from pathlib import Path
            Path(MARKER).write_text('ready')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                text = self._drain_pending_redirect()
                if text:
                    return {'completed': True, 'final_response': text,
                            'messages': [{'role': 'user', 'content': text}]}
                time.sleep(.01)
            return {'completed': True, 'final_response': 'missed input'}
        """.replace('MARKER', repr(str(marker))))
        events = []
        task = dict(self.task, _native_turn_id=str(uuid4()))
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            worker = threading.Thread(target=lambda: provider.start_task(task, self.bot, [],
                                      lambda kind, data: kind == 'phase' or events.append((kind, data))))
            worker.start()
            deadline = time.monotonic() + 4
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            provider.steer_task(task['id'], task['_native_turn_id'],
                                [dict(id='live-message', text='Unrelated: answer this now.')])
            worker.join(6)
            self.assertFalse(worker.is_alive())
        self.assertIn(('steering_consumed', dict(message_ids=['live-message'],
                                               turn_id=task['_native_turn_id'])), events)
        self.assertTrue(any('Unrelated: answer this now.' in data.get('text', '')
                            for kind, data in events if kind == 'text_delta'))

    def test_live_input_during_worker_initialization_reaches_first_turn(self):
        marker = self.root / 'initializing'
        gate = self.root / 'continue-initializing'
        provider = self.fake_hermes("""
            text = prompt
            return {'completed': True, 'final_response': text or 'missed input',
                    'messages': [{'role': 'user', 'content': text}]}
        """)
        source = provider.hermes_root / 'run_agent.py'
        content = source.read_text()
        content = content.replace('from tools.bot_relay import message_agent',
            "import time; from pathlib import Path\n"
            f"        Path({str(marker)!r}).write_text('ready')\n"
            f"        while not Path({str(gate)!r}).exists(): time.sleep(.01)\n"
            "        from tools.bot_relay import message_agent")
        source.write_text(content)
        events = []
        task = dict(self.task, _native_turn_id=str(uuid4()))
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            worker = threading.Thread(target=lambda: HermesProvider._run(provider, 'start', task, self.bot, [],
                                      lambda kind, data: kind == 'phase' or events.append((kind, data))))
            worker.start()
            try:
                deadline = time.monotonic() + 4
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(marker.exists())
                provider.steer_task(task['id'], task['_native_turn_id'],
                                    [dict(id='early-message', text='Answer this first.')])
                time.sleep(.1)
            finally:
                gate.touch()
                worker.join(6)
            self.assertFalse(worker.is_alive())
        self.assertIn(('steering_ready', dict(turn_id=task['_native_turn_id'])), events)
        self.assertIn(('steering_consumed', dict(message_ids=['early-message'],
                                               turn_id=task['_native_turn_id'])), events)

    def test_delayed_service_receipts_do_not_abandon_or_repeat_tools(self):
        output = self.root / 'tool-output'
        provider = self.fake_hermes("""
            from pathlib import Path
            def write(args):
                with Path(OUTPUT).open('a') as output: output.write('once\n')
                return 'saved'
            registry.register(name='terminal', toolset='terminal', schema={}, handler=write)
            result = registry.dispatch('terminal', {'command': 'write once'})
            return {'completed': True, 'final_response': result}
        """.replace('OUTPUT', repr(str(output))).replace("'once\n'", "'once\\n'"))
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            for delayed in ('native_call', 'native_result'):
                with self.subTest(delayed=delayed):
                    output.unlink(missing_ok=True)
                    events = []
                    def receipt(kind, data):
                        events.append(kind)
                        if kind == delayed:
                            time.sleep(31)
                        if kind == 'native_call':
                            return {'status': 'recorded', 'id': str(uuid4())}
                        if kind == 'native_result':
                            return {'status': 'recorded'}
                    HermesProvider._run(provider, 'start', dict(self.task, id=str(uuid4())), self.bot, [], receipt)
                    self.assertEqual(output.read_text() if output.exists() else '', 'once\n')
                    self.assertEqual(events.count('native_call'), 1)
                    self.assertEqual(events.count('native_result'), 1)
                    self.assertIn('completed', events)
                    self.assertNotIn('failed', events)

    def test_bots_do_not_share_runtime_and_idle_runtimes_stop(self):
        provider = self.fake_hermes("return {'completed': True, 'final_response': str(__import__('os').getpid())}")
        other = dict(self.bot, id=str(uuid4()), session_id=str(uuid4()))
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            for bot in (self.bot, other):
                provider.start_task(dict(self.task, id=str(uuid4())), bot, [], lambda *_: None)
        first, second = [provider._runtimes[bot['id']] for bot in (self.bot, other)]
        self.assertNotEqual(first.pid, second.pid)
        provider.stop_bot(self.bot['id'])
        self.assertIsNotNone(first.poll())
        self.assertIsNone(second.poll())
        provider.close()
        self.assertIsNotNone(second.poll())
        self.assertEqual(provider._runtimes, {})

    def test_cancelling_a_paused_task_preserves_a_newer_turn(self):
        child_path, started, release = [self.root / name for name in ('child-pid', 'started', 'release')]
        provider = self.fake_hermes("""
            import subprocess, sys, time
            from pathlib import Path
            import run_agent
            from tools.process_registry import process_registry
            if hasattr(run_agent, 'background'):
                Path(STARTED).touch()
                while not Path(RELEASE).exists(): time.sleep(0.01)
                return {'completed': True, 'final_response': 'Newer turn finished.'}
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
            run_agent.background = [{'session_id': 'test-child', 'owner_task_id': kwargs['task_id'], 'status': 'running', 'pid': child.pid}]
            Path(CHILD_PATH).write_text(str(child.pid))
            registry.dispatch('coconut_ask', {'title': 'Continue?', 'options': [{'id': 'yes', 'label': 'Yes'}]})
        """.replace('CHILD_PATH', repr(str(child_path))).replace('STARTED', repr(str(started))).replace('RELEASE', repr(str(release))))
        events, errors = [], []
        def next_turn():
            try:
                provider.start_task(dict(self.task, id=str(uuid4())), self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
            except Exception as error: errors.append(error)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda *_: None)
            process = provider._runtimes[self.bot['id']]
            child_pid = int(child_path.read_text())
            os.kill(child_pid, 0)
            thread = threading.Thread(target=next_turn)
            thread.start()
            try:
                self.wait_for_path(started)
                provider.cancel_task(self.task['id'])
                self.assertIsNone(process.poll())
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try: os.kill(child_pid, 0)
                    except ProcessLookupError: break
                    time.sleep(0.01)
                else: self.fail('The cancelled task left its background process running')
            finally:
                release.touch()
                thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(events[0], ('text_delta', {'text': 'Newer turn finished.'}))

    def test_runtime_shutdown_cleans_a_separate_child_process_group(self):
        provider = self.fake_hermes("""
            import subprocess, sys, run_agent
            from tools.process_registry import process_registry
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
            run_agent.background = [{'session_id': 'test-child', 'owner_task_id': kwargs['task_id'], 'status': 'running', 'pid': child.pid}]
            return {'completed': True, 'final_response': str(child.pid)}
        """)
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        child_pid = int(events[0][1]['text'])
        os.kill(child_pid, 0)
        provider.close()
        with self.assertRaises(ProcessLookupError): os.kill(child_pid, 0)

    def test_explicit_bot_restart_replaces_the_retained_runtime(self):
        provider = self.fake_hermes("return {'completed': True, 'final_response': 'Ready.'}")
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda *_: None)
            previous = provider._runtimes[self.bot['id']]
            restarted = dict(self.task, id=str(uuid4()), _conversation_epoch=str(uuid4()))
            provider.start_task(restarted, self.bot, [], lambda *_: None)
        self.assertIsNotNone(previous.poll())
        self.assertNotEqual(previous.pid, provider._runtimes[self.bot['id']].pid)

    def test_ordinary_reply_emits_only_answer_and_completion(self):
        provider = self.fake_hermes("kwargs['stream_callback']('Concise result.')\nreturn {'final_response': 'Concise result.', 'completed': True}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual([data['text'] for kind, data in events if kind == 'text_delta'], ['Concise result.'])
        self.assertNotIn('noisy', json.dumps(events))
        self.assertEqual([kind for kind, _ in events], ['text_delta', 'completed'])
        self.assertFalse(provider._processes)
        home = Path(provider.profile_rows.get(self.bot['id'])['home'])
        self.assertEqual((home / 'skill-sync-called').read_text(), 'True')

    def wait_for_path(self, path):
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), 'The model did not reach the test checkpoint.')

    def test_silent_final_without_stream_does_not_emit_text(self):
        provider = self.fake_hermes("return {'completed': True, 'final_response': '[SILENT]'}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events, [('completed', {'summary': 'Complete.', 'silent_response': True})])

    def test_reply_waits_for_confirmed_completion(self):
        started, release = self.root / 'started', self.root / 'release'
        provider = self.fake_hermes(
            "from pathlib import Path\nimport time\n"
            "kwargs['stream_callback']('An unfinished **')\n"
            f"Path({str(started)!r}).touch()\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
            "kwargs['stream_callback']('draft')\n"
            "return {'completed': True, 'final_response': 'The **finished reply**.'}\n")
        events, errors = [], []
        def run():
            try:
                provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
            except Exception as error:
                errors.append(error)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.wait_for_path(started)
                self.assertEqual(events, [])
            finally:
                release.touch()
                thread.join(timeout=3)
                if thread.is_alive(): provider.cancel_task(self.task['id'])
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(events, [('text_delta', {'text': 'The **finished reply**.'}),
                                  ('completed', {'summary': 'Complete.'})])

    def test_failed_turn_never_shows_its_unfinished_reply(self):
        provider = self.fake_hermes("kwargs['stream_callback']('Unfinished answer')\nraise RuntimeError('failed')\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual([kind for kind, _ in events], ['failed'])

    def test_completed_commentary_reaches_the_phone_before_work_finishes(self):
        started, release = self.root / 'started', self.root / 'release'
        provider = self.fake_hermes(
            "from pathlib import Path\nimport time\n"
            "kwargs['stream_callback']('I will ')\n"
            "kwargs['stream_callback']('check the failing test.')\n"
            "self.interim_assistant_callback('I will check the failing test.', already_streamed=True)\n"
            f"Path({str(started)!r}).touch()\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
            "kwargs['stream_callback']('An unfinished **')\n"
            "return {'completed': True, 'final_response': 'The test passes.'}\n")
        events, errors, replied = [], [], threading.Event()
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            if kind == 'interim_message':
                replied.set()
        def run():
            try:
                provider.start_task(self.task, self.bot, [], emit)
            except Exception as error:
                errors.append(error)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(replied.wait(3), 'Commentary waits for the whole turn.')
                self.wait_for_path(started)
                self.assertTrue(thread.is_alive())
                self.assertEqual(events, [('interim_message', {'text': 'I will check the failing test.'})])
            finally:
                release.touch()
                thread.join(timeout=3)
                if thread.is_alive(): provider.cancel_task(self.task['id'])
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(events, [('interim_message', {'text': 'I will check the failing test.'}),
                                  ('text_delta', {'text': 'The test passes.'}),
                                  ('completed', {'summary': 'Complete.'})])

    def test_streamed_commentary_does_not_repeat_as_a_fallback_final_reply(self):
        provider = self.fake_hermes(
            "kwargs['stream_callback']('I will check the test.')\n"
            "self.interim_assistant_callback('I will check the test.', already_streamed=True)\n"
            "return {'completed': True, 'final_response': ''}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events, [('interim_message', {'text': 'I will check the test.'}),
                                  ('completed', {'summary': 'Complete.'})])

    def test_codex_final_callback_confirms_the_existing_interim_message(self):
        provider = self.fake_hermes(
            "self.interim_assistant_callback('I will check the test.', already_streamed=False)\n"
            "self.interim_assistant_callback('The test passes.', already_streamed=False)\n"
            "return {'completed': True, 'final_response': 'The test passes.'}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events, [('interim_message', {'text': 'I will check the test.'}),
                                  ('interim_message', {'text': 'The test passes.'}),
                                  ('text_delta', {'text': 'The test passes.', 'finalize_interim': True}),
                                  ('completed', {'summary': 'Complete.'})])

    def test_failure_keeps_completed_commentary_and_withholds_the_draft(self):
        provider = self.fake_hermes(
            "self.interim_assistant_callback('I found the failing test.', already_streamed=False)\n"
            "kwargs['stream_callback']('Unverified success')\nraise RuntimeError('failed')\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual([kind for kind, _ in events], ['interim_message', 'failed'])
        self.assertEqual(events[0][1]['text'], 'I found the failing test.')
        self.assertNotIn('Unverified success', json.dumps(events))

    def test_interim_silence_marker_does_not_emit_text(self):
        provider = self.fake_hermes(
            "self.interim_assistant_callback('[SILENT]', already_streamed=False)\n"
            "return {'completed': True, 'final_response': '[SILENT]'}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events, [('completed', {'summary': 'Complete.', 'silent_response': True})])

    def test_context_keeps_interim_commentary_out_of_the_answer(self):
        provider = self.fake_hermes(
            "self.interim_assistant_callback('I will read the proposal.', already_streamed=False)\n"
            "return {'completed': True, 'final_response': 'The proposal saves a report.'}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.answer_context(self.task, self.bot, [], 'What changes?', lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events, [('text_delta', {'text': 'The proposal saves a report.'})])

    def configure_peer(self, *, budget=8):
        peer = {'id': str(uuid4()), 'name': 'Lin', 'role': 'Check calculations'}
        self.task['_available_bots'] = [peer]
        self.task['_handoff_budget_remaining'] = budget
        return peer

    def test_bot_message_receipt_precedes_independent_work(self):
        peer = self.configure_peer()
        payload = repr({'target': peer['id'], 'message': 'Check whether 17 multiplied by 19 equals 323.'})
        provider = self.fake_hermes("import json\nreceipt = json.loads(registry.dispatch('message_agent', " + payload + "))\nassert receipt['status'] == 'queued'\nassert receipt['task_id'] == 'durable-task'\nassert 'private_memory' not in receipt\nkwargs['stream_callback']('Independent work complete.')\nreturn {'completed': True, 'final_response': 'Independent work complete.'}\n")
        events = []
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            if kind == 'native_bot_message':
                return {'status': 'queued', 'task_id': 'durable-task', 'target_bot_id': peer['id'], 'thread_id': 'shared-thread', 'private_memory': 'Do not forward this.'}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        delivery_index = next(index for index, (kind, _) in enumerate(events) if kind == 'native_bot_message')
        text_index = next(index for index, (kind, _) in enumerate(events) if kind == 'text_delta')
        self.assertLess(delivery_index, text_index)
        self.assertEqual(events[-1][0], 'completed')
        self.assertEqual(set(events[delivery_index][1]), {'target_bot_id', 'text', 'native_envelope_id', 'native_call_id'})

    def test_repeated_bot_message_keeps_the_same_semantic_payload(self):
        peer = self.configure_peer()
        arguments = repr({'target': peer['id'], 'message': 'Send one calculation result.'})
        provider = self.fake_hermes("import json\nfirst = json.loads(registry.dispatch('message_agent', " + arguments + "))\nsecond = json.loads(registry.dispatch('message_agent', " + arguments + "))\nassert [first['status'], second['status']] == ['queued', 'duplicate']\nreturn {'completed': True, 'final_response': 'Done.'}\n")
        deliveries = []
        terminal = []
        def emit(kind, data):
            if kind in ('completed', 'failed'):
                terminal.append(kind)
            if kind == 'native_bot_message':
                deliveries.append(data)
                return {'status': 'queued' if len(deliveries) == 1 else 'duplicate', 'target_bot_id': peer['id']}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(len(deliveries), 2)
        self.assertEqual(deliveries[0]['text'], deliveries[1]['text'])
        self.assertEqual(deliveries[0]['target_bot_id'], deliveries[1]['target_bot_id'])
        self.assertNotEqual(deliveries[0]['native_envelope_id'], deliveries[1]['native_envelope_id'])
        self.assertEqual(set(deliveries[0]), {'target_bot_id', 'text', 'native_envelope_id', 'native_call_id'})
        self.assertEqual(terminal, ['completed'])

    def test_native_relay_delivers_past_the_old_limit(self):
        peer = self.configure_peer(budget=0)
        arguments = repr({'target': peer['id'], 'message': 'Check this result.'})
        provider = self.fake_hermes("import json\nassert 'Remaining automatic deliveries' not in self.system_prompt\nfor index in range(12):\n    result = json.loads(registry.dispatch('message_agent', " + arguments + "))\n    assert result['status'] == 'queued'\nreturn {'completed': True, 'final_response': 'Requests delivered.'}\n")
        deliveries = []
        terminal = []
        def emit(kind, data):
            if kind in ('completed', 'failed'):
                terminal.append(kind)
            if kind == 'native_bot_message':
                deliveries.append(data)
                return {'status': 'queued', 'task_id': str(uuid4())}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(len(deliveries), 12)
        self.assertEqual(terminal, ['completed'])

    def test_bot_message_cannot_address_an_unlisted_bot(self):
        self.configure_peer()
        arguments = repr({'target': str(uuid4()), 'message': 'An invalid destination.'})
        provider = self.fake_hermes("import json\nresult = json.loads(registry.dispatch('message_agent', " + arguments + "))\nassert 'error' in result\nreturn {'completed': True, 'final_response': 'Target unavailable.'}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertNotIn('native_bot_message', [kind for kind, _ in events])
        self.assertEqual(events[-1][0], 'completed')

    def test_context_requests_expose_no_bot_message_tool(self):
        self.configure_peer()
        provider = self.fake_hermes("assert set(registry.handlers) == {'coconut_ask', 'coconut_read_receipts', 'message_agent'}\nkwargs['stream_callback']('The proposal remains pending.')\nreturn {'completed': True, 'final_response': 'The proposal remains pending.'}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.answer_context(self.task, self.bot, [], 'Explain the proposal.', lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual([kind for kind, _ in events], ['text_delta'])

    def configure_preference_source(self):
        source = {'id': str(uuid4()), 'text': 'Remember to include an option labelled Not now when you offer a decision.'}
        self.task['_preference_sources'] = [source]
        return source

    def preference_receipt(self, source, *, status='saved'):
        preference_id = str(uuid4())
        return {
            'status': status, 'bot_id': self.bot['id'], 'source_message_id': source['id'],
            'preference_ids': [preference_id], 'deleted_ids': [],
            'preferences': [{'id': preference_id, 'text': 'Include an option labelled Not now in decisions.', 'source_message_id': source['id'], 'source_text': source['text'], 'created_at': '2026-09-08T12:00:00Z', 'updated_at': '2026-09-08T12:00:00Z'}],
        }






















    def test_worker_start_reads_native_instructions_before_hermes_import(self):
        provider = self.fake_hermes("from pathlib import Path\nimport os\nassert (Path(os.environ['HERMES_HOME']) / 'SOUL.md').read_text() == 'Keep replies brief.'\nassert self.skip_memory is False\nreturn {'completed': True, 'final_response': 'Native profile ready.'}\n")
        # Hermes owns SOUL.md of the profile. Coconut does not write it during a turn.
        (Path(provider.profile_rows.get(self.bot['id'])['home']) / 'SOUL.md').write_text('Keep replies brief.')
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events[-1][0], 'completed')

    def test_restart_tool_returns_durable_receipt_before_final_reply(self):
        provider = self.fake_hermes("import json\nreceipt = json.loads(registry.dispatch('coconut_restart_service', {}))\nassert receipt['status'] == 'queued'\nreturn {'completed': True, 'final_response': 'Restart requested.'}\n")
        events = []
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            return {'status': 'queued'} if kind == 'service_restart' else None
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(events[0], ('service_restart', {}))
        self.assertEqual(events[-1][0], 'completed')

    def test_zero_budget_keeps_native_tool_but_blocks_new_deliveries(self):
        self.configure_peer(budget=0)
        provider = self.fake_hermes("assert set(registry.handlers) == {'coconut_propose_file', 'coconut_ask', 'coconut_suggest', 'coconut_restart_service', 'coconut_track_pr', 'coconut_track_work', 'coconut_wait', 'coconut_continue_work', 'coconut_unlink_dependency', 'coconut_yield_test_services', 'coconut_read_receipts', 'message_agent'}\nreturn {'completed': True, 'final_response': 'No new bot deliveries.'}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events[-1][0], 'completed')

    def test_delivery_failure_does_not_expose_service_error_text(self):
        peer = self.configure_peer()
        arguments = repr({'target': peer['id'], 'message': 'Check this result.'})
        provider = self.fake_hermes("result = registry.dispatch('message_agent', " + arguments + ")\nkwargs['stream_callback'](result)\nreturn {'completed': True, 'final_response': result}\n")
        events = []
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            if kind == 'native_bot_message':
                raise RuntimeError('PRIVATE_SERVICE_DETAIL')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(events[-1][0], 'failed')
        output = ''.join(data['text'] for kind, data in events if kind == 'text_delta')
        self.assertEqual(output, '')
        self.assertNotIn('PRIVATE_SERVICE_DETAIL', output)

    def test_delivered_followup_rules_require_a_missing_human_choice(self):
        # Exercise provider serialization and worker assembly, not model compliance.
        provider = self.fake_hermes("kwargs['stream_callback'](self.system_prompt)\nreturn {'completed': True, 'final_response': ''}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events[-1][0], 'completed')
        output = ''.join(data['text'] for kind, data in events if kind == 'text_delta')
        self.assertIn('continue the next step without a card when existing permission covers it', output)
        self.assertIn('Preserve explicit human holds and rejections', output)
        self.assertIn('Only when a new human choice or permission is missing, offer the next step in a card', output)
        self.assertIn('Link it to the approval card with previous_decision_id when that approval was a card', output)
        self.assertIn('when only the design was approved', output)
        self.assertNotIn('outcome is not complete, offer the next step', output)

    def test_delivered_merge_rules_point_to_the_bot_skills_and_keep_the_card_contract(self):
        provider = self.fake_hermes("kwargs['stream_callback'](self.system_prompt + '\\n' + prompt)\nreturn {'completed': True, 'final_response': ''}\n")
        for checks in ('running', 'passed'):
            with self.subTest(checks=checks):
                task = dict(self.task, _pr_review=dict(url='https://github.com/example/repo/pull/42',
                                                      head_sha='current-head', checks_state=checks))
                events = []
                with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
                    provider.start_task(task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
                self.assertEqual(events[-1][0], 'completed')
                output = ''.join(data['text'] for kind, data in events if kind == 'text_delta')
                # How to verify a PR is the person's workflow in the bot's skills. The card contract stays.
                for rule in ('verify the PR as your skills for pull request review and merge say',
                             'If merge permission is missing, ask once',
                             'Preserve explicit human holds, rejected decisions',
                             'Do not bypass its unanswered choice',
                             'use a merge operation that rejects a changed head',
                             'current-head', checks):
                    self.assertIn(rule, output)
                self.assertNotIn('offer the merge decision without starting new local work', output)
                self.assertNotIn('merge without another card', output)
                self.assertNotIn('Greptile', output)

    def test_bot_contact_metadata_and_explicit_messages_exclude_private_fields(self):
        peer = self.configure_peer()
        peer.update(instructions='PRIVATE_PEER_INSTRUCTIONS', memory='PRIVATE_PEER_MEMORY', home='/private/peer/home')
        self.task['_bot_messages'] = [{'sender_bot_id': peer['id'], 'sender_name': peer['name'], 'text': 'EXPLICIT_SHARED_RESULT', 'memory': 'PRIVATE_MESSAGE_METADATA'}]
        provider = self.fake_hermes("kwargs['stream_callback'](self.system_prompt + '\\n' + prompt)\nreturn {'completed': True, 'final_response': ''}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        output = ''.join(data['text'] for kind, data in events if kind == 'text_delta')
        self.assertIn(peer['id'], output)
        self.assertIn('EXPLICIT_SHARED_RESULT', output)
        for value in ('PRIVATE_PEER_INSTRUCTIONS', 'PRIVATE_PEER_MEMORY', '/private/peer/home', 'PRIVATE_MESSAGE_METADATA'):
            self.assertNotIn(value, output)

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


    def test_committed_decision_stops_and_reaps_worker(self):
        arguments = repr(self.proposal_arguments())
        provider = self.fake_hermes("registry.dispatch('coconut_propose_file', " + arguments + ")\nraise AssertionError('The worker continued after a proposal.')\n")
        class CommittedDecision(RuntimeError):
            pass
        seen = []
        def emit(kind, data):
            if kind == 'phase':
                return
            seen.append((kind, data))
            if kind == 'decision':
                raise CommittedDecision()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}), self.assertRaises(CommittedDecision):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(seen[-1][1]['action']['content'], 'Exact bytes')
        self.assertEqual(seen[-1][1]['predicted_choice'], 'modify')
        self.assertEqual(seen[-1][1]['predicted_option_id'], 'change')
        self.assertEqual(seen[-1][1]['options'][1]['action']['content'], 'Brief bytes')
        self.assertFalse(provider._processes)

    def test_worker_exits_when_the_service_pipe_closes(self):
        started = self.root / 'started'
        provider = self.fake_hermes(f"import threading\nfrom pathlib import Path\nPath({str(started)!r}).touch()\nthreading.Event().wait()\n")
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            env, cwd = provider.environment(self.bot['id'])
        from coconut.profiles import HermesProfiles
        provider.profile_rows.get(self.bot['id'])
        worker = Path(__file__).resolve().parents[1] / 'coconut' / 'hermes_worker.py'
        process = subprocess.Popen([sys.executable, str(worker)], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            request = {'operation': 'start', 'task': self.task, 'bot': self.bot, 'history': [], 'model': provider.model}
            process.stdin.write(json.dumps(request) + '\n')
            process.stdin.flush()
            self.wait_for_path(started)
            process.stdin.write('[]\n')
            process.stdin.flush()
            process.stdin.close()
            self.assertEqual(process.wait(timeout=3), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            process.stdout.close()

    def test_cancel_stops_a_blocked_model_call(self):
        started = self.root / 'started'
        provider = self.fake_hermes(f"import threading\nfrom pathlib import Path\nPath({str(started)!r}).touch()\nthreading.Event().wait()\n")
        errors = []
        def run():
            try:
                provider.start_task(self.task, self.bot, [], lambda *_: None)
            except Exception as error:
                errors.append(error)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.wait_for_path(started)
            finally:
                provider.cancel_task(self.task['id'])
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(provider._processes)
        self.assertEqual(errors, [])

    def test_native_hermes_tools_are_available_to_the_model(self):
        provider = self.fake_hermes("assert 'terminal' in self.valid_tool_names\nreturn {'completed': True, 'final_response': 'Native tools available.'}\n")
        source = provider.hermes_root / 'run_agent.py'
        source.write_text(source.read_text().replace('set(registry.handlers)', "set(registry.handlers) | {'terminal'}"))
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual([kind for kind, _ in events], ['text_delta', 'completed'])

    def test_incomplete_hermes_turn_does_not_create_completed_card(self):
        provider = self.fake_hermes("return {'completed': False, 'failed': False, 'partial': True, 'final_response': 'Compression deferred'}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual(events[-1][0], 'failed')
        self.assertNotIn('completed', [kind for kind, _ in events])
        self.assertNotIn('artifact', [kind for kind, _ in events])

    def test_shared_checkout_credentials_prevent_worker_start(self):
        provider = self.fake_hermes("raise AssertionError('No model call may start.')\n")
        (provider.hermes_root / '.env').write_text('GITHUB_TOKEN=unrelated-test-value\n')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            with self.assertRaisesRegex(RuntimeError, 'shared .env'):
                provider.start_task(self.task, self.bot, [], lambda *_: None)
        self.assertFalse(provider._processes)

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

    def test_create_bot_from_empty_directory_then_handoff(self):
        arguments, created = self.creation_request()
        provider = self.fake_hermes("import json\nassert 'coconut_create_bot' in registry.handlers\nassert 'message_agent' in registry.handlers\nreceipt = json.loads(registry.dispatch('coconut_create_bot', " + repr(arguments) + "))\nassert receipt['status'] == 'created'\nassert 'private_memory' not in receipt\nmessage = {'target': receipt['bot_id'], 'message': 'Check 17 times 19.'}\nassert json.loads(registry.dispatch('message_agent', message))['status'] == 'queued'\nassert json.loads(registry.dispatch('message_agent', message))['status'] == 'queued'\nkwargs['stream_callback']('Scout is ready and has the check.')\nreturn {'completed': True, 'final_response': ''}\n")
        events = []
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            if kind == 'bot_create':
                return created | {'private_memory': 'PRIVATE_RECEIPT_DATA'}
            if kind == 'native_bot_message':
                return {'status': 'queued', 'task_id': str(uuid4()), 'target_bot_id': created['bot_id'], 'thread_id': str(uuid4())}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual([kind for kind, _ in events], ['bot_create', 'native_bot_message', 'native_bot_message', 'text_delta', 'completed'])
        self.assertEqual(events[0][1], arguments)
        self.assertEqual(events[1][1]['target_bot_id'], created['bot_id'])

    def test_creation_schema_requires_only_the_four_supported_fields(self):
        fields = {'source_message_id', 'name', 'role', 'instructions'}
        self.assertEqual(set(BOT_CREATION_SCHEMA['parameters']['required']), fields)
        self.assertEqual(set(BOT_CREATION_SCHEMA['parameters']['properties']), fields)
        arguments, unused = self.creation_request()
        for changes in ({'source_message_id': 'foreign-source'}, {'name': ' '}, {'role': ''},
                        {'name': 'x' * 81}, {'role': 'x' * 161}, {'instructions': 'x' * 20001},
                        {'model': 'unrequested-model'}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(ValueError):
                bot_creation_payload(arguments | changes, {arguments['source_message_id']})
        self.assertEqual(bot_creation_payload(arguments | {'instructions': ''}, {arguments['source_message_id']})['instructions'], '')

    def test_creation_receipt_accepts_native_name_normalization_and_strips_extras(self):
        arguments, receipt = self.creation_request(name='  Ｓｃｏｕｔ  Team ')
        receipt.update(name='scout team', private_token='PRIVATE')
        confirmed = bot_creation_receipt(receipt, arguments, self.bot)
        self.assertEqual(confirmed['bot_id'], receipt['bot_id'])
        self.assertNotIn('private_token', confirmed)

    def test_repeated_creation_reaches_service_with_the_exact_payload(self):
        arguments, receipt = self.creation_request()
        provider = self.fake_hermes("import json\nargs = " + repr(arguments) + "\nfirst = json.loads(registry.dispatch('coconut_create_bot', args))\nsecond = json.loads(registry.dispatch('coconut_create_bot', args))\nassert [first['status'], second['status']] == ['created', 'duplicate']\nassert first['bot_id'] == second['bot_id']\nreturn {'completed': True, 'final_response': 'Scout is ready.'}\n")
        calls, events = [], []
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            if kind == 'bot_create':
                calls.append(data)
                return receipt | {'status': 'created' if len(calls) == 1 else 'duplicate'}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(calls, [arguments, arguments])
        self.assertEqual(events[-1][0], 'completed')

    def test_failed_creation_adds_no_target_and_suppresses_success_prose(self):
        arguments, receipt = self.creation_request()
        provider = self.fake_hermes("import json\nresult = json.loads(registry.dispatch('coconut_create_bot', " + repr(arguments) + "))\nassert result['status'] == 'unconfirmed'\nmessage = {'target': " + repr(receipt['bot_id']) + ", 'message': 'Unconfirmed target must not receive this.'}\nassert 'error' in json.loads(registry.dispatch('message_agent', message))\nkwargs['stream_callback']('UNSUPPORTED_CREATION_SUCCESS')\nreturn {'completed': True, 'final_response': 'UNSUPPORTED_CREATION_SUCCESS'}\n")
        events = []
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            if kind == 'bot_create':
                raise RuntimeError('PRIVATE_SERVICE_DETAIL')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual([kind for kind, _ in events], ['bot_create', 'failed'])
        self.assertEqual(events[-1][1]['error_code'], 'bot_creation_receipt_unconfirmed')
        self.assertNotIn('PRIVATE_SERVICE_DETAIL', json.dumps(events))
        self.assertNotIn('UNSUPPORTED_CREATION_SUCCESS', json.dumps(events))

    def test_creation_receipt_rejects_wrong_source_identity_name_and_role(self):
        arguments, receipt = self.creation_request()
        for changes in ({'source_message_id': 'wrong'}, {'name': 'Someone else'}, {'role': 'Different role'},
                        {'bot_id': 'not-a-uuid'}, {'profile_id': str(uuid4())},
                        {'session_id': receipt['bot_id']}, {'session_id': self.bot['session_id']},
                        {'bot_id': self.bot['id'], 'profile_id': self.bot['id']}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(ValueError):
                bot_creation_receipt(receipt | changes, arguments, self.bot)

    def test_malformed_success_receipt_becomes_unconfirmed_in_worker(self):
        arguments, receipt = self.creation_request()
        provider = self.fake_hermes("import json\nresult = json.loads(registry.dispatch('coconut_create_bot', " + repr(arguments) + "))\nassert result['status'] == 'unconfirmed'\nreturn {'completed': True, 'final_response': 'UNSUPPORTED_SUCCESS'}\n")
        events = []
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            if kind == 'bot_create':
                return receipt | {'profile_id': str(uuid4())}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual([kind for kind, _ in events], ['bot_create', 'failed'])

    def test_changed_creation_retry_does_not_reuse_an_old_receipt(self):
        arguments, receipt = self.creation_request()
        self.task['_bot_creation_receipts'] = [receipt]
        changed = arguments | {'instructions': 'Different instructions must not be treated as already saved.'}
        provider = self.fake_hermes("import json\nassert 'Do not create replacements or rephrase a creation after a restart' in prompt\nresult = json.loads(registry.dispatch('coconut_create_bot', " + repr(changed) + "))\nassert result['status'] == 'validation_failed'\nreturn {'completed': True, 'final_response': 'UNSUPPORTED_SUCCESS'}\n")
        events = []
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            if kind == 'bot_create':
                raise ServiceError(409, 'PRIVATE_CONFLICT_BODY')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(events[0], ('bot_create', changed))
        self.assertEqual(events[-1][1]['error_code'], 'bot_creation_rejected')
        self.assertNotIn('PRIVATE_CONFLICT_BODY', json.dumps(events))

    def test_context_never_exposes_bot_creation_even_with_source_metadata(self):
        self.creation_request()
        provider = self.fake_hermes("assert 'coconut_create_bot' not in registry.handlers\nassert 'error' in json.loads(registry.dispatch('message_agent', {'target': 'unlisted', 'message': 'Must not send'}))\nassert 'No direct human source authorizes bot creation in this turn' in prompt\nreturn {'completed': True, 'final_response': 'This decision stays pending.'}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.answer_context(self.task, self.bot, [], 'What can you do?', lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual([kind for kind, _ in events], ['text_delta'])

    def test_capability_question_uses_current_tools_without_creating_a_bot(self):
        self.creation_request()
        self.task['_bot_creation_sources'][0]['text'] = 'Can you create another bot?'
        provider = self.fake_hermes("assert 'coconut_create_bot' in registry.handlers\nassert 'Earlier assistant messages may describe an older build' in self.system_prompt\nassert 'Answer capability questions directly without creating a bot until the person requests one' in self.system_prompt\nreturn {'completed': True, 'final_response': 'Yes. I can create another bot here.'}\n")
        events = []
        history = [{'id': str(uuid4()), 'role': 'assistant', 'text': 'Earlier build: I cannot create bots.'}]
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, history, lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual([kind for kind, _ in events], ['text_delta', 'completed'])

    def test_creation_receipt_in_restart_prompt_excludes_private_metadata(self):
        arguments, receipt = self.creation_request()
        self.task['_bot_creation_receipts'] = [receipt | {'private_memory': 'PRIVATE_CREATION_MEMORY'}]
        self.task['_bot_creation_sources'][0]['private_memory'] = 'PRIVATE_SOURCE_MEMORY'
        provider = self.fake_hermes("assert " + repr(receipt['bot_id']) + " in prompt\nassert 'PRIVATE_CREATION_MEMORY' not in self.system_prompt + prompt\nassert 'PRIVATE_SOURCE_MEMORY' not in self.system_prompt + prompt\nreturn {'completed': True, 'final_response': 'Scout already exists.'}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertNotIn('bot_create', [kind for kind, _ in events])
        self.assertEqual(events[-1][0], 'completed')

    def test_lost_creation_receipt_recovers_by_exact_retry_then_one_handoff(self):
        arguments, receipt = self.creation_request()
        provider = self.fake_hermes("import json\nargs = " + repr(arguments) + "\nassert json.loads(registry.dispatch('coconut_create_bot', args))['status'] == 'unconfirmed'\nconfirmed = json.loads(registry.dispatch('coconut_create_bot', args))\nassert confirmed['status'] == 'duplicate'\nmessage = {'target': confirmed['bot_id'], 'message': 'Check the requested calculation.'}\nassert json.loads(registry.dispatch('message_agent', message))['status'] == 'queued'\nkwargs['stream_callback']('Scout is ready and has the check.')\nreturn {'completed': True, 'final_response': ''}\n")
        events, creations = [], []
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            if kind == 'bot_create':
                creations.append(data)
                if len(creations) == 1:
                    raise RuntimeError('The first receipt was lost after its commit.')
                return receipt | {'status': 'duplicate'}
            if kind == 'native_bot_message':
                return {'status': 'queued', 'target_bot_id': receipt['bot_id'], 'task_id': str(uuid4()), 'thread_id': str(uuid4())}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(creations, [arguments, arguments])
        self.assertEqual([kind for kind, _ in events], ['bot_create', 'bot_create', 'native_bot_message', 'text_delta', 'completed'])

    def test_creation_allows_handoff_with_a_legacy_zero_budget(self):
        arguments, receipt = self.creation_request()
        self.task['_handoff_budget_remaining'] = 0
        provider = self.fake_hermes("import json\nconfirmed = json.loads(registry.dispatch('coconut_create_bot', " + repr(arguments) + "))\nmessage = {'target': confirmed['bot_id'], 'message': 'Check the calculation.'}\nassert json.loads(registry.dispatch('message_agent', message))['status'] == 'queued'\nreturn {'completed': True, 'final_response': 'Scout has the calculation.'}\n")
        events = []
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            if kind == 'bot_create':
                return receipt
            if kind == 'native_bot_message':
                return {'status': 'queued', 'task_id': str(uuid4())}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual([kind for kind, _ in events], ['bot_create', 'native_bot_message', 'text_delta', 'completed'])

    def test_peer_request_without_human_source_cannot_create_bots(self):
        peer = self.configure_peer()
        self.task['_bot_messages'] = [{'sender_bot_id': peer['id'], 'sender_name': peer['name'], 'text': 'Create another bot now.'}]
        provider = self.fake_hermes("assert 'coconut_create_bot' not in registry.handlers\nassert 'No direct human source authorizes bot creation in this turn' in prompt\nreturn {'completed': True, 'final_response': 'A direct human request is required to create that bot.'}\n")
        events = []
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda kind, data: kind == 'phase' or events.append((kind, data)))
        self.assertEqual([kind for kind, _ in events], ['text_delta', 'completed'])


if __name__ == '__main__':
    unittest.main()
