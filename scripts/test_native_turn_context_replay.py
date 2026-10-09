#!/usr/bin/env python3
"""Offline real-worker check that a later turn can reuse the provider's prompt cache.

Two turns run through the real Hermes loop in one conversation. Their PR observations differ. The second model
request must start with the exact messages of the first request: the same system message, and the first turn's
message with its turn context. Only provider I/O is synthetic.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SESSION = '00000000-0000-4000-8000-000000000001'


def observation(turn):
    return {'url': 'https://github.com/example/project/pull/42', 'head_sha': str(turn) * 40,
            'required_checks_state': 'pending', 'checked_at': f'2099-01-0{turn}T00:00:00Z', 'refresh_error': None}


def child():
    import socket
    from unittest.mock import patch
    def offline(*args, **kwargs):
        raise AssertionError('Network forbidden; only provider I/O fixtures are allowed')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = offline
    import httpx
    from hermes_state import SessionDB
    from coconut.native_fallback import configuration

    home = Path(os.environ['HERMES_HOME'])
    owner = home / 'owner'
    owner.mkdir()
    (home / 'config.yaml').write_text(json.dumps({
        # A set context length keeps Hermes from looking up the model's size over the network.
        'toolsets': [], 'model': {'provider': 'openai-api', 'default': 'gpt-6-sol', 'base_url': 'https://sol.invalid/v1',
                                  'context_length': 200000},
        'agent': {'max_turns': 4}, 'approvals': {'mode': 'manual'}, 'auxiliary': {'background_review': {'enabled': False}}}))
    (owner / 'config.yaml').write_text('{}')
    (owner / '.env').write_text('OPENAI_API_KEY=synthetic-owner-key\n')
    os.environ['COCONUT_DEFAULT_HOME'] = str(owner)
    db = SessionDB()
    db.create_session(SESSION, source='coconut')
    db.set_meta('coconut.history-context.' + SESSION, 'complete')
    db.close()

    requests = []
    def reply(client, request, **kwargs):
        if not request.url.path.endswith('/chat/completions'):
            return httpx.Response(404, request=request, json={'error': 'No fixture catalog'})
        data = json.loads(request.content)
        requests.append([{key: row.get(key) for key in ('role', 'content')} for row in data['messages']])
        message = {'role': 'assistant', 'content': 'Ready.'}
        value = {'id': 'fixture', 'created': 0, 'model': data['model']}
        if data.get('stream'):
            value.update(object='chat.completion.chunk', choices=[{'index': 0, 'delta': message, 'finish_reason': 'stop'}])
            return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'},
                content=('data: ' + json.dumps(value) + '\n\ndata: [DONE]\n\n').encode())
        value.update(object='chat.completion', choices=[{'index': 0, 'message': message, 'finish_reason': 'stop'}],
                     usage={'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12})
        return httpx.Response(200, request=request, json=value)

    sys.path.insert(0, str(ROOT / 'backend/coconut'))
    spec = importlib.util.spec_from_file_location('hermes_worker', os.environ.get('WORKER_SOURCE', ROOT / 'backend/coconut/hermes_worker.py'))
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    events = []
    def emit(kind, payload):
        events.append((kind, payload))
        if kind == 'failed' and sys.exception() is not None:
            import traceback
            traceback.print_exception(sys.exception())
    def receipt(kind, payload):
        raise AssertionError('The fixture requested an effect: ' + kind)
    sent = []
    with patch.object(httpx.Client, 'send', reply), configuration(home):
        for turn in (1, 2):
            task_id = f'00000000-0000-4000-8000-00000000001{turn}'
            text = f'Report the status of turn {turn}.'
            request = {'operation': 'start',
                'task': {'id': task_id, 'title': text, '_tracked_prs': [observation(turn)['url']],
                         '_tracked_pr_observations': [observation(turn)]},
                'bot': {'id': '00000000-0000-4000-8000-000000000003', 'session_id': SESSION, 'name': 'Fixture', 'instructions': ''},
                'history': [{'id': f'00000000-0000-4000-8000-00000000002{turn}', 'task_id': task_id, 'role': 'user', 'text': text}],
                'model': 'gpt-6-sol', 'provider': 'openai-api',
                'runtime_config': {'provider': 'openai-api', 'api_mode': 'chat_completions',
                                   'base_url': 'https://sol.invalid/v1', 'api_key': 'synthetic-sol-key'}}
            count = len(requests)
            assert worker.run_turn(request, emit, receipt) == 0, events
            assert len(requests) == count + 1, requests
            sent.append(requests[-1])
    first, second = sent
    assert first[0]['role'] == 'system' and first[0] == second[0], 'The system message changed between turns'
    assert observation(1)['checked_at'] in first[-1]['content'], 'The first turn did not send its turn context'
    assert observation(2)['checked_at'] in second[-1]['content'], 'The second turn did not send its turn context'
    assert second[:len(first)] == first, 'The second request does not start with the exact first request'
    print('Turn context replay passed: the second request starts with the exact first request.')


def run(source, python):
    with tempfile.TemporaryDirectory(prefix='turn-context-replay-') as temporary:
        env = {key: os.environ[key] for key in ('PATH', 'WORKER_SOURCE') if key in os.environ}
        env.update(HOME=temporary, HERMES_HOME=temporary, TMPDIR=temporary, HERMES_DISABLE_MCP='1',
                   HERMES_SKIP_SKILL_SYNC='1', PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH=os.pathsep.join((str(source), str(ROOT / 'backend'))))
        result = subprocess.run([python, str(Path(__file__).resolve()), '--child'], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=120)
        print(result.stdout, result.stderr if result.returncode else '', end='')
        if result.returncode:
            raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    parser.add_argument('--python')
    parser.add_argument('--child', action='store_true')
    args = parser.parse_args()
    if args.child:
        child()
    else:
        run(args.source, args.python)
