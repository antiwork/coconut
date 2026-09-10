#!/usr/bin/env python3
"""Exercise the real iOS chat against an isolated API with varied message heights."""
import argparse
import copy
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--exchange', action='store_true')
    parser.add_argument('--instant-send', action='store_true')
    parser.add_argument('--transcript', type=Path, default=ROOT/'ios/CoconutUITests/Fixtures/ConversationLayoutMessages.json')
    args = parser.parse_args()
    state = json.loads((ROOT/'ios/Coconut/Fixtures/PreviewState.json').read_text())
    peer = dict(state['bots'][0], pinned=False, status='idle', unread_count=0)
    bot = state['bots'][1]
    bot.update(status='idle', unread_count=0, pinned=False)
    state.update(bots=[bot], messages=[], decisions=[], artifacts=[], tasks=[], routines=[], cursor=50)
    task = dict(id=str(uuid4()), bot_id=bot['id'], session_id=bot['session_id'], title='Conversation layout',
                status='completed', summary='', created_at=state['server_time'], updated_at=state['server_time'])
    task['thread_id'] = task['id']
    state['tasks'].append(task)
    def message(role, text):
        return dict(id=str(uuid4()), bot_id=bot['id'], task_id=task['id'], thread_id=task['id'], role=role,
                    kind='text', text=text, created_at=state['server_time'], streaming=False)
    paragraph = 'The plan keeps your instructions and prior decisions together. Each update continues the same conversation. '
    state['messages'] = [message(row['role'], row['text']) for row in json.loads(args.transcript.read_text())]
    latest = state['messages'][-1]
    latest['text'] = 'Latest response stays visible.\n\n' + latest['text']
    latest['streaming'] = True
    if args.exchange:
        state['bots'].append(peer)
        own = message('assistant', 'My own work stays here.')
        outward = message('assistant', '**Check the report.**\n\n- Keep the useful findings.\n- Return a short answer.')
        outward.update(kind='bot_exchange', sender_bot_id=bot['id'], recipient_bot_ids=[peer['id']], created_at='2026-09-10T10:00:00Z')
        inward = message('assistant', '## Result\n\nThe check **passed**.\n\n```swift\nlet answer = 42\n```\n\n[Open reference](https://example.com)')
        inward.update(kind='text', bot_id=peer['id'], sender_bot_id=peer['id'], recipient_bot_ids=[], exchange_recipient_bot_ids=[bot['id']], created_at='2026-09-10T10:00:01Z')
        state['messages'] = [own, outward, inward]
    changed = threading.Condition()
    events = []
    sends = 0
    streaming = threading.Event()
    release_send = threading.Event()
    send_keys = []
    rejected_keys = set()
    responses = {}

    def publish(kind, row):
        state['cursor'] += 1
        events.append(dict(seq=state['cursor'], type=kind, bot_id=bot['id'], task_id=task['id'],
                           created_at=state['server_time'], data=dict(row)))
        changed.notify_all()

    def reply(number):
        if streaming.wait(0.4): return
        with changed:
            row = message('assistant', '')
            row['streaming'] = True
            state['messages'].append(row)
            publish('message.created', row)
        for chunk in ['Reply ', 'after ', 'message ', f'{number}.']:
            if streaming.wait(0.3): return
            with changed:
                row['text'] += chunk
                publish('message.updated', row)
        with changed:
            row['streaming'] = False
            task['status'] = 'completed'
            publish('message.updated', row)
            publish('task.updated', task)

    def stream():
        for index in range(36):
            if streaming.wait(0.15): return
            with changed:
                latest['text'] += '\n\n' + paragraph if index % 6 == 0 else ' More detail.'
                state['cursor'] += 1
                events.append(dict(seq=state['cursor'], type='message.updated', bot_id=bot['id'],
                                   task_id=task['id'], created_at=latest['created_at'], data=dict(latest)))
                changed.notify_all()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def respond(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass

        def do_POST(self):
            nonlocal sends
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
            if self.path == '/v1/pair':
                self.respond(dict(token='layout-fixture-only', device_id=str(uuid4())))
            elif args.exchange and self.path == '/test/exchange':
                with changed:
                    row = message('assistant', 'A new peer reply.')
                    row.update(bot_id=peer['id'], sender_bot_id=peer['id'], recipient_bot_ids=[],
                               exchange_recipient_bot_ids=[bot['id']], created_at='2026-09-10T10:00:02Z', streaming=True)
                    state['messages'].append(row)
                    publish('message.created', row)
                    row['streaming'] = False
                    publish('message.updated', row)
                    publish('task.updated', task)
                self.respond(dict(ok=True))
            elif self.path == '/test/release-send':
                release_send.set()
                self.respond(dict(ok=True))
            elif self.path.endswith('/messages'):
                key = self.headers.get('Idempotency-Key')
                if args.instant_send:
                    send_keys.append(key)
                    if body['text'] == 'Immediate message.':
                        release_send.wait(20)
                    if body['text'] == 'Retry this message.' and key not in rejected_keys:
                        rejected_keys.add(key)
                        self.respond(dict(error='Temporary test failure.'), status=503)
                        return
                    if body['text'] == 'Rejected message.':
                        self.respond(dict(error='The selected task has finished.'), status=409)
                        return
                    if key in responses:
                        self.respond(responses[key])
                        return
                with changed:
                    sends += 1
                    task['status'] = 'working'
                    publish('task.updated', task)
                    row = message('user', body['text'])
                    row['client_request_id'] = key
                    responses[key] = dict(task)
                    state['messages'].append(row)
                    publish('message.created', row)
                threading.Thread(target=reply, args=(sends,), daemon=True).start()
                self.respond(task)
            else: self.respond(dict(ok=True))

        def do_PUT(self): self.respond(dict(registered=True, configured=False))
        def do_DELETE(self): self.respond(dict(registered=False, configured=False))
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == '/test/send-status':
                self.respond(dict(keys=send_keys, sends=sends))
                return
            with changed:
                if url.path == '/v1/events':
                    after = int(parse_qs(url.query).get('after', ['0'])[0])
                    if state['cursor'] <= after: changed.wait(1)
                    value = dict(events=[e for e in events if e['seq'] > after], cursor=state['cursor'], server_time=state['server_time'])
                elif url.path == '/v1/state': value = copy.deepcopy(state)
                else: value = dict(ok=True)
            self.respond(value)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        source = args.xctestrun.resolve()
        def absolute(value):
            if isinstance(value, str): return value.replace('__TESTROOT__', str(source.parent))
            if isinstance(value, list): return [absolute(v) for v in value]
            if isinstance(value, dict): return {k: absolute(v) for k, v in value.items()}
            return value
        document = absolute(plistlib.loads(source.read_bytes()))
        targets = ([t for c in document['TestConfigurations'] for t in c['TestTargets']]
                   if 'TestConfigurations' in document else
                   [v for v in document.values() if isinstance(v, dict)])
        for target in targets:
            if target.get('BlueprintName') == 'CoconutUITests':
                target.setdefault('EnvironmentVariables', {})['COCONUT_LAYOUT_TEST_URL'] = f'http://127.0.0.1:{server.server_port}'
        with tempfile.TemporaryDirectory(prefix='coconut-layout-test-') as folder:
            runfile = Path(folder)/'Layout.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            command = ['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                            '-destination', 'platform=iOS Simulator,id='+args.simulator,
                            '-only-testing:CoconutUITests/' + ('BotExchangeFlowTests' if args.exchange else 'InstantSendFlowTests' if args.instant_send else 'ConversationLayoutFlowTests'), '-parallel-testing-enabled', 'NO',
                            '-resultBundlePath', str(args.result)]
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as process:
                for line in process.stdout:
                    print(line, end='', flush=True)
                    if 'COCONUT_LAYOUT_STAGE=stream' in line:
                        threading.Thread(target=stream, daemon=True).start()
                result = process.wait()
            if result: raise SystemExit(result)
    finally:
        streaming.set()
        release_send.set()
        server.shutdown()
        server.server_close()


if __name__ == '__main__': main()
