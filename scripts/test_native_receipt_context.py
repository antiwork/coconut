#!/usr/bin/env python3
"""Exercise the real Coconut worker/native Hermes path against a local fixture model."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def check(source, python, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=evidence, prefix='native-receipts-') as temporary:
        home = Path(temporary)
        env = dict(PATH=os.environ.get('PATH', ''), HOME=str(home), HERMES_HOME=str(home), HERMES_DISABLE_LAZY_INSTALLS='1',
                   TMPDIR=str(home), PYTHONPATH=str(source),
                   HERMES_SKIP_SKILL_SYNC='1')
        (home / 'config.yaml').write_text(json.dumps({'toolsets': [], 'fallback_providers': [],
            'agent': {'max_turns': 5}, 'compression': {'enabled': False}}))
        bot = dict(id=str(uuid4()), session_id=str(uuid4()), name='Receipt fixture', instructions='Use the fixture tools.')
        seed = '''
import json, os
from hermes_state import SessionDB
session = os.environ['FIXTURE_SESSION']
db = SessionDB()
db.create_session(session, source='coconut')
db.set_meta('coconut.history-context.' + session, 'complete')
db.replace_messages(session, [
    {'role': 'user', 'content': 'Approve only this exact action. Do not publish.', 'platform_message_id': 'coconut:human'},
    {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'unknown-call', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{"command":"unknown effect"}'}}]},
    {'role': 'tool', 'content': 'Effect unknown; do not repeat.', 'tool_call_id': 'unknown-call', 'tool_name': 'terminal', 'effect_disposition': 'unknown'},
    {'role': 'assistant', 'content': 'Waiting for evidence.'}])
print(json.dumps(db.get_messages(session)))
db.close()
'''
        setup = subprocess.run([python, '-c', seed], env=dict(env, FIXTURE_SESSION=bot['session_id']),
                               text=True, capture_output=True, timeout=30)
        assert setup.returncode == 0, setup.stderr
        before = json.loads(setup.stdout)
        raw = [dict(id=str(uuid4()), state='completed', action_hash=str(uuid4()),
                    action={'name': 'terminal', 'arguments': {'command': 'saved exact command ' + str(i)}},
                    result='large-result-marker-' + 'x' * 6000) for i in range(305)]
        requests, errors = [], []
        class Model(BaseHTTPRequestHandler):
            def log_message(self, format, *args): pass
            def do_POST(self):
                if not self.path.endswith('/chat/completions'):
                    self.send_error(404)
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append(body)
                    latest = [m for m in body['messages'] if m['role'] == 'user'][-1]['content']
                    assert len(str(latest)) < 15000, len(str(latest))
                    assert 'large-result-marker-' not in str(latest)
                    if len(requests) in (1, 3):
                        offered = {t.get('function', t).get('name') for t in body.get('tools', [])}
                        name = 'coconut_read_receipts'
                        arguments = {'receipt_index': 0} if len(requests) == 1 else {}
                        if name not in offered:
                            assert 'tool_call' in offered, offered
                            name, arguments = 'tool_call', {'calls': [{'name': name, 'arguments': arguments}]}
                        message: dict = dict(role='assistant', content=None, tool_calls=[dict(id='read-receipt', type='function',
                            function=dict(name=name, arguments=json.dumps(arguments)))])
                        finish = 'tool_calls'
                    else:
                        results = [m for m in body['messages'] if m['role'] == 'tool']
                        if len(requests) == 2:
                            assert any('saved exact command 0' in str(m.get('content')) for m in results), results
                        else:
                            assert '\"total\": 0' in str(results[-1].get('content')), results[-1]
                        message, finish = dict(role='assistant', content='Receipt read without repeating its effect.'), 'stop'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
                    self.end_headers()
                    data: dict = dict(id='fixture', created=int(time.time()), model=body['model'])
                    if body.get('stream'):
                        if message.get('tool_calls'): message['tool_calls'][0]['index'] = 0
                        data.update(object='chat.completion.chunk', choices=[dict(index=0, delta=message, finish_reason=finish)])
                        self.wfile.write(('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode())
                    else:
                        data.update(object='chat.completion', choices=[dict(index=0, message=message, finish_reason=finish)],
                                    usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
                        self.wfile.write(json.dumps(data).encode())
                except Exception as error:
                    errors.append(repr(error))
                    self.send_error(500)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        address = f'http://127.0.0.1:{server.server_port}/v1'
        task = dict(id=str(uuid4()), title='Resume retained work', _native_receipts=raw)
        request = dict(operation='resume', task=task, bot=bot, history=[], model='fixture', provider='custom',
                       runtime_config=dict(provider='custom', api_mode='chat_completions', base_url=address, api_key='local-fixture'),
                       decision=dict(status='approved', action_type='native_approval', answer='Only read this receipt. Do not publish.',
                                     action={'name': 'terminal', 'arguments': {'command': 'exact approved command'}}))
        log = (evidence / 'native-worker-stderr.log').open('w')
        worker = subprocess.Popen([python, str(ROOT / 'backend/coconut/hermes_worker.py')], cwd=home, env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True)
        assert worker.stdin is not None and worker.stdout is not None
        stdin, stdout = worker.stdin, worker.stdout
        lines = queue.Queue()
        def output():
            for line in stdout: lines.put(line)
            lines.put(None)
        threading.Thread(target=output, daemon=True).start()
        events = []
        try:
            empty_request = dict(request, task=dict(task, id=str(uuid4()), _native_receipts=[]))
            for turn_request in (request, empty_request):
                stdin.write(json.dumps(turn_request) + '\n'); stdin.flush()
                deadline = time.monotonic() + 90
                turn_events = []
                while time.monotonic() < deadline:
                    try:
                        line = lines.get(timeout=min(1, max(0.1, deadline - time.monotonic())))
                    except queue.Empty:
                        assert not errors, errors
                        continue
                    assert line is not None, 'Worker exited before turn_end'
                    event = json.loads(line); events.append(event); turn_events.append(event)
                    if event['type'] == 'turn_end': break
                    if event.get('request_id'):
                        raise AssertionError('Read-only receipt lookup unexpectedly requested an effect: ' + event['type'])
                assert not errors, errors
                assert any(e['type'] == 'completed' for e in turn_events), turn_events
            assert len(requests) == 4, len(requests)
            stdin.close(); worker.wait(timeout=15)
        finally:
            if worker.poll() is None: worker.kill(); worker.wait(timeout=5)
            stdout.close()
            if not stdin.closed: stdin.close()
            log.close(); server.shutdown(); server.server_close()
            (evidence / 'native-events.json').write_text(json.dumps(events, indent=2))
            (evidence / 'model-observations.json').write_text(json.dumps({'errors': errors, 'calls': len(requests)}, indent=2))
            error_log = home / 'logs/errors.log'
            if error_log.exists():
                (evidence / 'native-errors.log').write_text(error_log.read_text())
        readback = '''
import json, os
from hermes_state import SessionDB
db=SessionDB(read_only=True)
session=os.environ['FIXTURE_SESSION']
print(json.dumps({'session':db.resolve_resume_session_id(session), 'messages':db.get_messages(session), 'all':db.get_messages(session, include_inactive=True)}))
db.close()
'''
        after = subprocess.run([python, '-c', readback], env=dict(env, FIXTURE_SESSION=bot['session_id']),
                               text=True, capture_output=True, timeout=30)
        assert after.returncode == 0, after.stderr
        saved = json.loads(after.stdout)
        assert saved['session'] == bot['session_id']
        for old in before:
            assert old in saved['all'], 'Original history row changed'
        assert any(m.get('effect_disposition') == 'unknown' for m in saved['messages'])
        print(json.dumps({'native_model_calls': len(requests), 'original_rows_preserved': len(before),
                          'receipt_count': len(raw), 'same_session': True, 'legacy_recovery': 'not attempted; provenance required'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    check(args.source, args.python, args.evidence)
