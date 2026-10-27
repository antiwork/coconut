"""Run the adapter with the installed Hermes Codex bridge and synthetic events."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from hermes_env import needs_hermes, SOURCE


@needs_hermes
class NativeInterimMessageTests(unittest.TestCase):
    def test_codex_commentary_and_final_messages_remain_distinct_and_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / 'bots' / 'test-bot' / 'home'
            home.mkdir(parents=True)
            env = {'PATH': os.environ.get('PATH', ''), 'HERMES_HOME': str(home), 'HERMES_DISABLE_LAZY_INSTALLS': '1',
                   'TMPDIR': temporary, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': os.pathsep.join((str(Path(__file__).resolve().parents[1]),
                                                SOURCE,
                                                str(Path(__file__).resolve().parents[1] / 'coconut')))}
            result = subprocess.run([sys.executable, '-c', NATIVE_BRIDGE], env=env, cwd=temporary,
                                    capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CODEX_COMPLETE_MESSAGES_ONLY', result.stdout)


NATIVE_BRIDGE = r'''
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

with patch('socket.socket.connect', side_effect=AssertionError('Unexpected network')):
    from run_agent import AIAgent
    from agent.codex_runtime import make_codex_app_server_event_bridge
    from coconut.hermes_worker import run_turn
    from coconut.providers import FixtureProvider
    from coconut.service import Service, now
    from cron.scheduler import _is_cron_silence_response

    service = Service(Path.cwd() / 'service', FixtureProvider(Path.cwd() / 'files'), start_workers=False)
    # A phone pairs through the relay tunnel with its static key. Only such a device gets notifications.
    device = service.pair({'code': service.pairing_code(tunnel=True)}, peer='phone-key')['device_id']
    def mutate(method, path, data):
        return service.mutate(method, path, data, str(uuid4()), device)
    mutate('PUT', '/v1/device/push', dict(token='a' * 64, environment='development', enabled=True,
                                        preferences=dict(decisions=True, messages=True)))
    bot = mutate('POST', '/v1/bots', dict(name='Native Codex Check'))
    task = mutate('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Check the test', mode='queue'))
    with service.transaction():
        task = service._get('tasks', task['id'])
        task['status'] = 'working'
        service._put('tasks', task)
    progress, final = 'I will check the failing test.', 'The test passes.'
    output = []
    def replies():
        return [m for m in service.snapshot()['messages'] if m['task_id'] == task['id']
                and m['role'] == 'assistant' and m['kind'] == 'text']
    def emit(kind, data):
        if kind in ('turn_timing', 'turn_trace', 'steering_ready', 'phase'):
            return
        output.append((kind, data))
        return service.emit(task['id'], kind, data)
    def synthetic_turn(agent, prompt, **kwargs):
        agent._stream_callback = kwargs['stream_callback']
        bridge = make_codex_app_server_event_bridge(agent)
        def delta(method, text):
            bridge(dict(method=method, params=dict(delta=text)))
        def completed(identity, text):
            bridge(dict(method='item/completed', params=dict(item=dict(type='agentMessage', id=identity, text=text))))
        delta('item/reasoning/delta', 'PRIVATE_REASONING')
        delta('item/agentMessage/delta', 'I will ')
        assert replies() == [], replies()
        delta('item/agentMessage/delta', 'check the failing test.')
        assert replies() == [], replies()
        completed('commentary', progress)
        completed('commentary', progress)
        assert [(m['text'], m['streaming']) for m in replies()] == [(progress, False)], replies()
        assert service._get('tasks', task['id'])['status'] == 'working'
        delta('item/agentMessage/delta', 'The test ')
        assert [m['text'] for m in replies()] == [progress], replies()
        delta('item/agentMessage/delta', 'passes.')
        completed('final', final)
        assert [m['text'] for m in replies()] == [progress, final], replies()
        assert not any(kind == 'text_delta' for kind, _ in output), output
        return dict(completed=True, final_response=final, messages=[])
    request = dict(operation='start', task=task, bot=bot, history=[], model='synthetic-model', provider='openai',
                   runtime_config=dict(provider='openai', api_mode='chat_completions',
                                       base_url='http://127.0.0.1:1/v1', api_key='synthetic-key'))
    try:
        with (patch('model_tools.get_tool_definitions', return_value=[]),
              patch('model_tools.check_toolset_requirements', return_value={}),
              patch('agent.process_bootstrap.OpenAI'),
              patch.object(AIAgent, 'run_conversation', synthetic_turn)):
            result = run_turn(request, emit, emit)
            assert result == 0, (result, output)
        messages = replies()
        assert [(m['text'], m['streaming']) for m in messages] == [(progress, False), (final, False)], messages
        assert service._get('tasks', task['id'])['status'] == 'completed'
        assert service._get('messages', messages[0]['id'])['_interim'] is True
        assert '_interim' not in service._get('messages', messages[1]['id'])
        assert service.db.execute("SELECT message_id FROM push_items WHERE kind='message'").fetchall() == [(messages[1]['id'],)]
        assert 'PRIVATE_REASONING' not in json.dumps(messages), messages
        assert [kind for kind, _ in output] == ['interim_message', 'interim_message', 'text_delta', 'completed'], output
        assert output[-2] == ('text_delta', dict(text=final, finalize_interim=True)), output
        aliases = ('[SILENT]', 'SILENT', 'NO_REPLY', 'NO REPLY',
                   'NO_REPLY\nThere is no change.', 'There is no change.\nNO REPLY')
        def silent_turn(agent, prompt, **kwargs):
            bridge = make_codex_app_server_event_bridge(agent)
            for identity, text in (('progress', 'I will check the current state.'), ('final', alias)):
                bridge(dict(method='item/completed', params=dict(item=dict(type='agentMessage', id=identity, text=text))))
            return dict(completed=True, final_response=alias, messages=[])
        for origin in ('job', 'reaction'):
            for index, alias in enumerate(aliases):
                assert _is_cron_silence_response(alias), alias
                task = mutate('POST', '/v1/bots/' + bot['id'] + '/messages',
                              dict(text='Check the current state.', mode='queue'))
                with service.transaction():
                    message = next(m for m in service._task_messages(task['id']) if m['role'] == 'user')
                    if origin == 'reaction':
                        message.update(text='Reaction added: 👍', reaction_event=dict(emoji='👍', action='added'))
                        service._put('messages', message)
                    task = service._get('tasks', task['id'])
                    task.update(status='working', _turn_started_at=now(), _turn_message_ids=[message['id']])
                    if origin == 'job':
                        task['_native_job'] = dict(id='quiet-check-' + str(index), prompt='Check the current state.', deliver='local')
                    service._put('tasks', task)
                output = []
                quiet_request = dict(request, task=task, history=[message])
                with (patch('model_tools.get_tool_definitions', return_value=[]),
                      patch('model_tools.check_toolset_requirements', return_value={}),
                      patch('agent.process_bootstrap.OpenAI'),
                      patch.object(AIAgent, 'run_conversation', silent_turn)):
                    result = run_turn(quiet_request, emit, emit)
                assert result == 0, (origin, alias, result, output)
                assert service._get('tasks', task['id'])['status'] == 'completed', (origin, alias, output)
                assert replies() == [], (origin, alias, replies())
                assert not service.db.execute('SELECT 1 FROM push_items WHERE task_id=?', (task['id'],)).fetchone()
                assert not any(kind == 'text_delta' for kind, _ in output), (origin, alias, output)
        print('NATIVE_JOB_AND_REACTION_SILENCE_ALIASES; native final callbacks stay hidden')
        print('CODEX_COMPLETE_MESSAGES_ONLY; native bridge, no model request or native tool')
    finally:
        service.close()
'''
