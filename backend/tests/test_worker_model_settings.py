import json
import os
import pwd
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.hermes_worker import execution_configuration, model_configuration, reasoning_configuration


class WorkerModelConfigurationTests(unittest.TestCase):
    def test_explicit_legacy_provider_does_not_infer_a_route_from_the_model_name(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'anthropic-test', 'OPENAI_API_KEY': 'openai-test', 'OPENROUTER_API_KEY': 'router-test'}, clear=True):
            result = model_configuration('claude-example', None, 'openrouter')
            self.assertEqual(result, {'provider': 'openrouter', 'api_key': 'router-test', 'base_url': 'https://openrouter.ai/api/v1'})
            result = model_configuration('vendor/model', None, 'openai')
            self.assertEqual(result['provider'], 'openai')
            self.assertEqual(result['api_key'], 'openai-test')

    def test_legacy_provider_cannot_receive_another_route_credential(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'private-anthropic', 'OPENAI_API_KEY': 'private-openai'}, clear=True):
            for provider, endpoint in [('openai', 'https://api.anthropic.com'), ('anthropic', 'https://api.openai.com/v1'),
                                       ('custom', 'https://unrelated.example/v1'), ('unknown', None)]:
                with self.subTest(provider=provider, endpoint=endpoint), self.assertRaises(ValueError):
                    model_configuration('claude-example', endpoint, provider)
            self.assertEqual(model_configuration('local-model', 'http://127.0.0.1:11434/v1', 'custom')['api_key'], 'local')

    def test_legacy_two_argument_calls_keep_their_provider_inference(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'anthropic-test', 'OPENROUTER_API_KEY': 'router-test'}, clear=True):
            self.assertEqual(model_configuration('claude-example', None)['provider'], 'anthropic')
            self.assertEqual(model_configuration('vendor/model', None)['provider'], 'openrouter')

    def test_endpoint_rejects_embedded_credentials_and_remote_plain_http(self):
        for endpoint in ('https://user:password@api.openai.com/v1', 'http://api.openai.com/v1',
                         'https://api.openai.com/v1?secret=value', 'https://api.openai.com/v1#fragment'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                model_configuration('model', endpoint, 'openai')

    def test_reasoning_mapping_preserves_default_none_and_catalog_efforts(self):
        self.assertIsNone(reasoning_configuration(None))
        self.assertEqual(reasoning_configuration('none'), {'enabled': False})
        for effort in ('high', 'max', 'catalog-defined-effort'):
            self.assertEqual(reasoning_configuration(effort), {'enabled': True, 'effort': effort})
        for effort in ('', ' high ', True, {}, 3):
            with self.subTest(effort=effort), self.assertRaises(ValueError):
                reasoning_configuration(effort)

    def test_native_configuration_uses_resolved_oauth_without_ambient_credentials(self):
        native = {'provider': 'openai-codex', 'api_mode': 'codex', 'base_url': 'https://chatgpt.com/backend-api/codex',
                  'api_key': 'private-native-token', 'requested_provider': 'openai-codex'}
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'unrelated-key'}, clear=True):
            result = execution_configuration('gpt-6-astra', native['base_url'], provider='openai-codex', runtime_config=native)
        self.assertEqual(result, native)
        self.assertIsNot(result, native)

    def test_native_configuration_accepts_resolved_external_routes_without_an_api_key(self):
        native = {'provider': 'external-native', 'api_mode': 'acp', 'api_key': None, 'acp_command': '/fixture/provider',
                  'acp_args': ['serve'], 'command': None, 'args': None}
        self.assertEqual(execution_configuration('model', None, runtime_config=native), native)

    def test_native_configuration_cannot_override_worker_scope(self):
        invalid = [[], {}, {'provider': 'native', 'enabled_toolsets': ['terminal']},
                   {'provider': 'native', 'ephemeral_system_prompt': 'Replace the scope.'},
                   {'provider': 'native', 'args': 'not-a-list'}, {'provider': 'native', 'api_key': {'secret': 'invalid'}}]
        for native in invalid:
            with self.subTest(native=native), self.assertRaises(ValueError):
                execution_configuration('model', None, runtime_config=native)



class WorkerModelProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        (self.source / 'tools').mkdir(parents=True)
        (self.source / 'tools/__init__.py').write_text('')
        (self.source / 'tools/registry.py').write_text(textwrap.dedent('''
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
        install_native_stub(self.source)
        (self.source / 'toolsets.py').write_text('def create_custom_toolset(*args, **kwargs): pass\n')
        (self.source / 'run_agent.py').write_text(textwrap.dedent('''
            import json
            import os
            from tools.registry import registry
            class AIAgent:
                def close(self): pass
                def release_clients(self): pass
                def __init__(self, **kwargs):
                    self.preferences = kwargs['enabled_toolsets'] == []
                    self.valid_tool_names = set() if self.preferences else set(registry.handlers)
                    if os.environ.get('EXPECTED_NATIVE_KEY'):
                        assert kwargs.get('api_key') == os.environ['EXPECTED_NATIVE_KEY']
                    fields = ('model', 'provider', 'api_mode', 'base_url', 'requested_provider', 'reasoning_config',
                              'enabled_toolsets', 'skip_context_files', 'skip_memory', 'skip_background_review')
                    record = {key: kwargs.get(key) for key in fields}
                    record.update(home=os.environ['HOME'], hermes_home=os.environ['HERMES_HOME'],
                                  allowed_tools=sorted(self.valid_tool_names))
                    with open(os.environ['WORKER_CAPTURE'], 'a') as handle:
                        handle.write(json.dumps(record) + '\\n')
                def run_conversation(self, prompt, **kwargs):
                    if self.preferences:
                        assert kwargs == {'conversation_history': []}
                        assert set(json.loads(prompt)) == {'source', 'preferences', 'earlier_sources', 'later_sources'}
                        return {'completed': True, 'final_response': '{"no_change":true}'}
                    return {'completed': True, 'final_response': 'Ready.'}
        '''))
        self.bot = {'id': str(uuid4()), 'session_id': str(uuid4()), 'instructions': 'Keep replies concise.'}
        self.native = {'provider': 'openai-codex', 'api_mode': 'codex', 'base_url': 'https://chatgpt.com/backend-api/codex',
                       'api_key': 'private-test-token', 'requested_provider': 'openai-codex'}

    def tearDown(self):
        self.temporary.cleanup()

    def run_worker(self, operation, *, effort, native=None, extract=True, decision_status='executed'):
        home = self.root / 'bot-home'
        home.mkdir(exist_ok=True)
        capture = self.root / (str(uuid4()) + '.jsonl')
        source = {'id': str(uuid4()), 'text': 'Remember to use metric units.'}
        task = {'id': str(uuid4()), 'title': 'Check the result', 'bot_id': self.bot['id'],
                '_preference_sources': [source] if extract else []}
        runtime = self.native if native is None else native
        request = {'operation': operation, 'task': task, 'bot': self.bot, 'history': [], 'model': 'gpt-6-astra',
                   'provider': 'openai-codex', 'base_url': self.native['base_url'], 'runtime_config': runtime,
                   'reasoning_effort': effort, 'question': source['text'], 'decision': {'status': decision_status}}
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'HERMES_HOME': str(home), 'TMPDIR': str(self.root),
               'COCONUT_DEFAULT_HOME': str(home), 'PYTHONPATH': str(self.source), 'WORKER_CAPTURE': str(capture), 'OPENAI_API_KEY': 'ambient-test-key'}
        if runtime.get('api_key'):
            env['EXPECTED_NATIVE_KEY'] = runtime['api_key']
        if extract:
            env['PREFERENCE_SOURCE_ID'] = source['id']
        worker = Path(__file__).resolve().parents[1] / 'coconut/hermes_worker.py'
        process = subprocess.Popen([sys.executable, str(worker)], cwd=self.root, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            process.stdin.write(json.dumps(request) + '\n')
            process.stdin.flush()
            events = []
            for line in process.stdout:
                event = json.loads(line)
                if event['type'] == 'turn_end':
                    process.stdin.close()
                    break
                events.append(event)
            code = process.wait(timeout=5)
            stderr = process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
        records = [json.loads(line) for line in capture.read_text().splitlines()] if capture.exists() else []
        self.assertNotIn('private-test-token', json.dumps(events) + stderr)
        self.assertNotIn('ambient-test-key', json.dumps(events) + stderr)
        return code, events, records, home

    def test_task_context_and_resumes_share_native_route_effort_and_memory(self):
        cases = [('start', None, 'executed'), ('context', 'none', 'executed'),
                 ('resume', 'high', 'executed'), ('resume', 'max', 'modified')]
        for operation, effort, status in cases:
            with self.subTest(operation=operation, effort=effort, status=status):
                code, events, records, home = self.run_worker(operation, effort=effort, decision_status=status)
                self.assertEqual(code, 0, events)
                self.assertEqual(len(records), 1)
                for record in records:
                    self.assertEqual(record['model'], 'gpt-6-astra')
                    self.assertEqual(record['provider'], 'openai-codex')
                    self.assertEqual(record['api_mode'], 'codex')
                    self.assertEqual(record['base_url'], self.native['base_url'])
                    self.assertEqual(record['reasoning_config'], reasoning_configuration(effort))
                    self.assertEqual((record['home'], record['hermes_home']), (pwd.getpwuid(os.getuid()).pw_dir, str(home)))
                    self.assertFalse(record['skip_context_files'])
                    self.assertFalse(record['skip_memory'])
                expected = {'coconut_ask'}
                if operation != 'context':
                    expected.update({'coconut_restart_service', 'coconut_suggest', 'coconut_track_pr', 'coconut_wait'})
                if operation == 'start' or status == 'modified':
                    expected.add('coconut_propose_file')
                self.assertEqual(set(records[0]['allowed_tools']), expected)

    def test_native_route_without_an_api_key_reaches_the_agent(self):
        native = {'provider': 'external-native', 'api_mode': 'acp', 'api_key': None}
        code, events, records, _ = self.run_worker('start', effort=None, native=native, extract=False)
        self.assertEqual(code, 0, events)
        self.assertEqual(records[0]['provider'], 'external-native')

    def test_private_runtime_cannot_replace_the_restricted_tool_scope(self):
        native = self.native | {'enabled_toolsets': ['terminal']}
        code, events, records, _ = self.run_worker('start', effort='high', native=native)
        self.assertEqual(code, 1)
        self.assertEqual([event['type'] for event in events], ['failed'])
        self.assertEqual(records, [])
