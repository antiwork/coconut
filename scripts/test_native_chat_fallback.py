#!/usr/bin/env python3
"""Check chat restarts and native fallback through Coconut with local HTTP endpoints."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from coconut.providers import HermesProvider
from coconut.service import Service


def check(source, python):
    calls, errors, handoffs = [], [], []
    quota = threading.Event()
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            if not self.path.endswith('/chat/completions'):
                self.send_error(404)
                return
            try:
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fallback = self.path.startswith('/fallback/')
                assert self.headers['Authorization'] == 'Bearer ' + ('fallback-key' if fallback else 'primary-key')
                calls.append('fallback' if fallback else 'primary')
                if not fallback and quota.is_set():
                    self.send_response(429)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'error': {'type': 'usage_limit_reached',
                        'message': 'The usage limit has been reached'}}).encode())
                    return
                message = dict(role='assistant', content='Fallback reply.' if fallback else 'Primary reply.')
                messages = request['messages']
                last_user = max(i for i, row in enumerate(messages) if row['role'] == 'user')
                target = re.search(r'relay-probe:([0-9a-f-]{36})', str(messages[last_user].get('content', '')))
                if target and not handoffs and not any(row['role'] == 'tool' for row in messages[last_user:]):
                    name, arguments = 'coconut_message_bot', dict(target=target[1], message='Read this local check.')
                    offered = {tool.get('function', tool).get('name') for tool in request.get('tools', [])}
                    if name not in offered:
                        assert 'tool_call' in offered, offered
                        name, arguments = 'tool_call', {'calls': [{'name': name, 'arguments': arguments}]}
                    handoffs.append(target[1])
                    message = dict(role='assistant', content=None, tool_calls=[dict(id='relay-test', type='function',
                        function=dict(name=name, arguments=json.dumps(arguments)))])
                finish = 'tool_calls' if message.get('tool_calls') else 'stop'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if request.get('stream') else 'application/json')
                self.end_headers()
                data = dict(id='probe', created=int(time.time()), model=request['model'])
                if request.get('stream'):
                    if message.get('tool_calls'): message['tool_calls'][0]['index'] = 0
                    chunks = [message] if message.get('tool_calls') else [
                        dict(role='assistant', content=message['content'][:4]), dict(content=message['content'][4:])]
                    for index, chunk in enumerate(chunks):
                        data.update(object='chat.completion.chunk', choices=[dict(index=0, delta=chunk,
                            finish_reason=finish if index == len(chunks) - 1 else None)])
                        self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
                        self.wfile.flush()
                    self.wfile.write(b'data: [DONE]\n\n')
                else:
                    data.update(object='chat.completion', choices=[dict(index=0, message=message, finish_reason=finish)],
                                usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
                    self.wfile.write(json.dumps(data).encode())
            except Exception as error:
                errors.append(str(error))
                self.send_error(500)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f'http://127.0.0.1:{server.server_port}'
    try:
        with tempfile.TemporaryDirectory(prefix='coconut-chat-fallback-') as temporary:
            home = Path(temporary)
            class Catalog:
                root = home / 'default'
                def resolve_runtime(self, *_, **__):
                    return dict(provider='custom', api_mode='chat_completions',
                                base_url=address + '/primary/v1', api_key='primary-key')
                def worker_limit(self):
                    return None
            Catalog.root.mkdir()
            (Catalog.root / 'config.yaml').write_text(json.dumps({'fallback_providers': [dict(
                provider='openai-api', model='fallback-probe', base_url=address + '/fallback/v1',
                api_mode='chat_completions', key_env='OPENAI_API_KEY')]}))
            (Catalog.root / '.env').write_text('OPENAI_API_KEY=fallback-key\n')
            class Provider(HermesProvider):
                def migrate_model_default(self, *_): pass
                def subscribe_model_catalog(self, *_): pass
                def native_settings(self, operation='read', bots=(), **_kwargs):
                    return {'routines': []}
                def observe_bot(self, bot): return bot
                def model_catalog(self): return {'default': self.resolve_task_model({}, {}), 'models': []}
                def resolve_model(self, bot, catalog=None): return self.resolve_task_model(bot, {})
                def resolve_task_model(self, bot, task):
                    return dict(model='primary-probe', provider='custom', base_url=address + '/primary/v1', reasoning_effort=None)
            provider = Provider(home / 'data', python=python, hermes_root=source, catalog=Catalog())
            service = Service(home / 'data', provider, start_workers=False)
            def write(path, data):
                return service.mutate('POST', path, data, str(uuid4()))
            def create(name):
                # Each bot is a Hermes profile with its own settings, as `hermes -p <name> config` writes them.
                bot = write('/v1/bots', {'name': name, 'role': ''})
                profile = service.profiles.home(bot)
                (profile / 'config.yaml').write_text((Catalog.root / 'config.yaml').read_text())
                (profile / '.env').write_text((Catalog.root / '.env').read_text())
                return bot
            def reply(bot, expected, text='Reply with one short sentence.'):
                task = write('/v1/bots/' + bot['id'] + '/messages', {'text': text, 'mode': 'queue'})
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    service._dispatch_pending()
                    if errors: raise AssertionError(errors)
                    row = service._get('tasks', task['id'])
                    if row['status'] in ('completed', 'failed'):
                        assert row['status'] == 'completed', row.get('summary')
                        messages = [m['text'] for m in service._all('messages')
                                    if m.get('task_id') == task['id'] and m.get('role') == 'assistant' and m.get('kind') == 'text']
                        assert any(expected in text for text in messages), messages
                        events = [json.loads(row[0]) for row in service.db.execute(
                            "SELECT data FROM events WHERE task_id=? AND type IN ('message.created','message.updated')",
                            (task['id'],))]
                        replies = [m['text'] for m in events if m.get('role') == 'assistant' and m.get('kind') == 'text']
                        assert replies and all(text == expected for text in replies), replies
                        return task
                    time.sleep(.05)
                raise AssertionError('The chat did not finish.')
            try:
                bot = create('Research check')
                reply(bot, 'Primary reply.')
                assert calls == ['primary'], calls
                write('/v1/bots/restart', {})
                quota.set()
                reply(bot, 'Fallback reply.')
                reply(bot, 'Fallback reply.')
                assert calls.count('fallback') == 2, calls
                assert calls.count('primary') >= 2, calls
                quota.clear()
                peer = create('Recipient')
                task = reply(bot, 'Primary reply.', 'Send this local check to relay-probe:' + peer['id'])
                deliveries = [row for row in service._all('deliveries') if row['source_task_id'] == task['id']]
                assert len(deliveries) == 1, deliveries
                assert deliveries[0]['target_bot_id'] == peer['id']
                assert handoffs == [peer['id']]
                assert not service._all('decisions')
                print('Chat restart and repeated native fallback passed:', calls)
            finally:
                service.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--python', required=True)
    args = parser.parse_args()
    check(args.source, args.python)
