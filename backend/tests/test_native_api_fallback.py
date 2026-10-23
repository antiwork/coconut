"""Check the pinned Hermes normalizer and Coconut worker boundary."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from hermes_env import needs_hermes, PYTHON, SOURCE


@needs_hermes
class NativeAPIFallbackTests(unittest.TestCase):
    def test_native_config_preserves_primary_and_other_fallbacks(self):
        with tempfile.TemporaryDirectory() as root:
            code = '''
import json, os, shlex
from pathlib import Path
from coconut.native_fallback import save, state, for_turn
root=Path(os.environ['HERMES_HOME']); bot=root/'bot'; bot.mkdir()
original={'model': {'default':'gpt-6-astra','provider':'openai-codex'},
          'fallback_model': {'provider':'anthropic','model':'other'}, 'terminal': {'cwd':'/tmp'}}
(root/'config.yaml').write_text(json.dumps(original))
(root/'.env').write_text('OPENAI_API_KEY=profile-secret\\n')
os.environ.pop('OPENAI_API_KEY',None)
(bot/'config.yaml').write_text('{}')
assert save(root, True)['enabled']
from coconut.native_settings import raw_config
saved=raw_config(root)
assert saved['model']==original['model'] and saved['terminal']==original['terminal']
assert 'fallback_model' not in saved and saved['fallback_providers'][0]['provider']=='anthropic'
assert len(saved['fallback_providers'])==2
assert saved['fallback_providers'][1]['provider']=='openai-api'
from hermes_cli.runtime_provider import resolve_runtime_provider
runtime=resolve_runtime_provider(requested='openai-api',target_model='gpt-6-astra',explicit_api_key='test-secret',explicit_base_url='https://api.openai.com/v1')
assert runtime['provider']=='openai-api' and runtime['api_mode']=='codex_responses'
assert runtime['base_url'].rstrip('/')=='https://api.openai.com/v1'
assert 'profile-secret' not in json.dumps(state(root))
os.environ['HERMES_HOME']=str(bot)
# Each profile uses its own fallback chain. The chain of the default home does not reach the bot.
assert for_turn() is None
(root/'.env').write_text('OPENAI_API_KEY=profile-secret\\nOPENAI_BASE_URL=https://api.openai.com/v1\\nOTHER_URL=https://example.com/v1\\n')
saved['fallback_providers'][1]['base_url']='${OPENAI_BASE_URL}'
saved['fallback_providers'][0]['base_url']='${OTHER_URL}'
(root/'config.yaml').write_text(json.dumps(saved))
assert save(root, True)['enabled']
assert len(raw_config(root)['fallback_providers'])==2
assert raw_config(root)['fallback_providers'][1]['base_url']=='${OPENAI_BASE_URL}'
assert for_turn() is None
(bot/'.env').write_text('OPENAI_API_KEY=bot-secret\\n')
(bot/'config.yaml').write_text(json.dumps({'fallback_providers':[{'provider':'openai-api','model':'gpt-6-astra','api_key':'${OPENAI_API_KEY}'}]}))
assert for_turn()[0]['api_key']=='bot-secret'
assert '${OPENAI_API_KEY}' in (bot/'config.yaml').read_text()
(bot/'.env').write_text('')
assert not state(bot)['available']
assert for_turn() is None
(bot/'config.yaml').write_text(json.dumps({'fallback_providers':[{'provider':'anthropic','model':'other'}]}))
assert [e['provider'] for e in for_turn()]==['anthropic']

(bot/'config.yaml').write_text('fallback_providers: []')
assert for_turn() is None
(bot/'config.yaml').write_text('{}')
assert save(root, False)['enabled'] is False
assert for_turn() is None
assert raw_config(root)['fallback_providers'][0]['base_url']=='${OTHER_URL}'
external=raw_config(root)
secret_file=root/'external.env'; secret_file.write_text('OPENAI_API_KEY=external-secret\\n')
external['secrets']={'command':{'enabled':True,'command':'cat '+shlex.quote(str(secret_file))}}
(root/'config.yaml').write_text(json.dumps(external))
(root/'.env').write_text('OPENAI_BASE_URL=https://api.openai.com/v1\\n')
assert state(root)['available']
assert save(root, True)['enabled']
# The default home reads its own chain, with the key from its secret command.
os.environ['HERMES_HOME']=str(root)
def openai():
    return next(entry for entry in for_turn() if entry['provider']=='openai-api')
assert openai()['api_key']=='external-secret'
assert 'external-secret' not in (root/'config.yaml').read_text()
assert 'OPENAI_API_KEY' not in os.environ
secret_file.write_text('OPENAI_API_KEY=rotated-secret\\nOPENAI_BASE_URL=https://rotated.example.com/v1\\n')
external=raw_config(root)
external['fallback_providers'][1].pop('base_url')
(root/'config.yaml').write_text(json.dumps(external))
(root/'.env').write_text('')
assert openai()['api_key']=='rotated-secret'
assert openai()['base_url']=='https://rotated.example.com/v1'
external.pop('secrets')
(root/'config.yaml').write_text(json.dumps(external))
assert [entry['provider'] for entry in for_turn()]==['anthropic']
assert not state(root)['available']
print('Native fallback settings passed')
'''
            env = dict(os.environ, HERMES_HOME=root, OPENAI_API_KEY='test-secret',
                       PYTHONPATH=os.pathsep.join([str(Path(SOURCE).resolve()), str(Path('backend').resolve())]))
            result = subprocess.run([PYTHON, '-c', code], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Native fallback settings passed', result.stdout)

    def test_native_titles_preserve_chat_work_and_compression(self):
        with tempfile.TemporaryDirectory() as root:
            code = """
from uuid import uuid4
from coconut.native_conversation import NativeConversation
from coconut.native_relay import NativeRelay
from hermes_state import SessionDB
bot = {'id': str(uuid4()), 'session_id': str(uuid4()), 'name': 'Research check'}
request = {'bot': bot, 'model': 'probe', 'operation': 'start', 'history': [], 'task': {'id': str(uuid4())}}
db = SessionDB()
def install():
    conversation = NativeConversation(request, db)
    NativeRelay(request, None).install(conversation)
    return conversation.session_id
try:
    canonical = install()
    title = db.get_session_title(canonical)
    assert title == 'Bot Chat'
    db.end_session(canonical, 'compression')
    tip = str(uuid4())
    db.create_session(tip, source='coconut', parent_session_id=canonical)
    assert install() == tip
    assert db.resolve_session_by_title('Bot Chat') == tip
    identities = set()
    for _ in range(2):
        request['task']['_conversation_epoch'] = str(uuid4())
        for execution in (None, 'same-work'):
            request['task']['_execution_session_id'] = execution
            current = install()
            assert install() == current
            assert current not in identities
            identities.add(current)
            assert db.get_session_title(current).startswith('Work ' if execution else 'Chat ')
            assert db.resolve_session_by_title('Bot Chat') == tip
    assert db.get_session(canonical) and db.get_session(tip)
finally:
    db.close()
"""
            env = dict(os.environ, HERMES_HOME=root,
                       PYTHONPATH=os.pathsep.join([SOURCE, str(Path('backend').resolve())]))
            result = subprocess.run([PYTHON, '-c', code], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_chat_restart_and_repeated_quota_fallback_through_service(self):
        result = subprocess.run([PYTHON, 'scripts/test_native_chat_fallback.py', '--source', SOURCE, '--python', PYTHON],
                                capture_output=True, text=True, timeout=150)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-6000:])
        self.assertIn('Chat restart and repeated native fallback passed', result.stdout)

    def test_start_resume_and_context_pass_the_native_chain_to_hermes(self):
        from test_worker_model_settings import WorkerModelProcessTests
        fixture = WorkerModelProcessTests('runTest')
        fixture.setUp()
        try:
            # Mock only the native configuration boundary; the test above uses Hermes itself.
            (fixture.source / 'sitecustomize.py').write_text(
                "import sys\n" + f"sys.path.insert(0, {str(Path('backend/coconut').resolve())!r})\n"
                + "import native_fallback\n"
                + "native_fallback.for_turn = lambda: [{'provider':'openai-api','model':'gpt-6-astra'}]\n")
            agent = fixture.source / 'run_agent.py'
            code = agent.read_text().replace("self.preferences = kwargs['enabled_toolsets'] == []", "assert kwargs['fallback_model'][0]['provider'] == 'openai-api'\n        self.preferences = kwargs['enabled_toolsets'] == []")
            agent.write_text(code)
            home = fixture.root / 'bot-home'; home.mkdir()
            (home/'config.yaml').write_text(json.dumps({'fallback_providers':[{'provider':'openai-api','model':'gpt-6-astra','key_env':'OPENAI_API_KEY'}]}))
            for operation in ('start','resume','context'):
                code, events, records, _ = fixture.run_worker(operation, effort='high', extract=False)
                self.assertEqual(code, 0, events)
                self.assertTrue(records, events)
                self.assertTrue(any(e['type']=='text_delta' for e in events), events)
        finally:
            fixture.tearDown()
