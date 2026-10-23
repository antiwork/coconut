"""Run the installed Hermes conversation loop with a synthetic model transport."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from hermes_env import needs_hermes, SOURCE


@needs_hermes
class NativeInitialInputTests(unittest.TestCase):
    def test_buffered_input_reaches_first_model_request_without_tool_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = {'PATH': os.environ.get('PATH', ''), 'HERMES_HOME': temporary,
                   'TMPDIR': temporary, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': os.pathsep.join((str(Path(__file__).resolve().parents[1]),
                                                SOURCE))}
            result = subprocess.run([sys.executable, '-c', NATIVE_LOOP], env=env, cwd=temporary,
                                    capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('FIRST_REQUEST_INCLUDES_BUFFERED_INPUT', result.stdout)


NATIVE_LOOP = r'''
import copy
from unittest.mock import MagicMock, patch
from openai.types.chat import ChatCompletion
from coconut.live_messages import LiveMessages

# Imports, session state and logs use a scratch HERMES_HOME. No network or
# native tool is executed; the real initialisation and conversation loop run.
with patch('socket.socket.connect', side_effect=AssertionError('Unexpected network')):
    from run_agent import AIAgent
    with (patch('model_tools.get_tool_definitions', return_value=[]),
          patch('model_tools.check_toolset_requirements', return_value={}),
          patch('agent.process_bootstrap.OpenAI')):
        agent = AIAgent(model='synthetic-model', api_key='synthetic-key',
                        provider='openai', api_mode='chat_completions',
                        base_url='http://127.0.0.1:1/v1', enabled_toolsets=[],
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        skip_background_review=True, max_iterations=2)
    agent.client = MagicMock()
    agent._cached_system_prompt = 'Follow the latest human input.'
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    requests = []
    def complete(**kwargs):
        requests.append(copy.deepcopy(kwargs['messages']))
        return ChatCompletion(id='synthetic', created=0, model='synthetic-model', object='chat.completion',
            choices=[dict(index=0, finish_reason='stop', message=dict(role='assistant', content='Stopped.'))],
            usage=dict(prompt_tokens=20, completion_tokens=2, total_tokens=22))
    agent.client.chat.completions.create.side_effect = complete
    consumed = []
    live = LiveMessages(None, consumed.append)
    live.deliver([dict(id='early', text='Stop the investigation. Answer this first.')])
    live.bind(agent)
    live.deliver([dict(id='later', text='Also use the corrected value: 42.')])
    assert consumed == []
    # On PR34, the worker called the native method directly. Keep that exact
    # path available so the regression executes the real loop on the base too.
    run = getattr(live, 'run_conversation', agent.run_conversation)
    result = run('Investigate the issue.', conversation_history=[],
                 persist_user_message='Investigate the issue.', persist_user_platform_id='coconut:original')
    assert result['completed'] is True, result
    assert len(requests) == 1, requests
    wire = str(requests[0])
    assert 'Stop the investigation. Answer this first.' in wire, requests[0]
    assert wire.index('Stop the investigation.') < wire.index('Also use the corrected value: 42.'), wire
    assert wire.count('Stop the investigation.') == 1, wire
    assert consumed == []
    live.finish(result)
    assert consumed == [['early', 'later']], (consumed, result)
    assert not result.get('pending_steer'), result
    agent.close()
    print('FIRST_REQUEST_INCLUDES_BUFFERED_INPUT; real Hermes loop, one synthetic response, no tools')
'''
