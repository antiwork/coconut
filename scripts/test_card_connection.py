#!/usr/bin/env python3
"""Verify card answers during sync failure and a lost answer receipt."""
import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import plistlib
import socket
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    state = json.loads((ROOT / 'ios/Coconut/Fixtures/PreviewState.json').read_text())
    bot = dict(state['bots'][1], pinned=False, status='idle', unread_count=0)
    task = dict(state['tasks'][0], bot_id=bot['id'], status='needs_you')
    first = dict(state['decisions'][0], bot_id=bot['id'], task_id=task['id'],
        title='Confirm the sample choice?', status='pending', action_type='question',
        options=[dict(id='confirm', label='Confirm choice', explanation='Record the sample choice.', answer='approve')],
        recommended_option_id='confirm')
    second = dict(first, id='00000000-0000-4000-8000-000000000077', title='Check the revised choice?',
        created_at='2026-09-09T11:42:00Z', options=[dict(id='stale', label='Old choice', explanation='The server has a newer revision.', answer='approve')],
        recommended_option_id='stale')
    delegate = dict(state['bots'][0], pinned=False, status='working')
    delegated_task = dict(task, id='00000000-0000-4000-8000-000000000088', bot_id=delegate['id'],
        conversation_bot_id=bot['id'], status='working')
    state.update(bots=[bot, delegate], tasks=[task, delegated_task], decisions=[first, second], artifacts=[], routines=[], cursor=50,
        messages=[dict(id='decision-message', bot_id=bot['id'], task_id=task['id'], role='assistant',
            kind='decision', text='', decision_id=first['id'], created_at=state['server_time'], streaming=False)])
    requests = []
    offline = threading.Event()
    sync_failures = []
    receipt = dict(first, status='approved', selected_option_id='confirm', selected_option_label='Confirm choice')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def send(self, data, status=200):
            print(self.command, self.path, status, flush=True)
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass

        def do_GET(self):
            if self.path == '/v1/state' and not offline.is_set():
                pass
                self.send(copy.deepcopy(state))
            elif self.path.startswith('/v1/events') and not offline.is_set():
                offline.wait(1)
                self.send(dict(events=[], cursor=state['cursor'], server_time=state['server_time']))
            elif self.path == '/v1/state' or self.path.startswith('/v1/events'):
                sync_failures.append(True)
                self.send(dict(error='Sync unavailable'), 503)
            else: self.send({})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
            if self.path == '/test/offline':
                offline.set()
                self.send(dict(ok=True))
            elif self.path == '/v1/pair':
                self.send(dict(token='fixture-only', device_id='00000000-0000-4000-8000-000000000099'))
            elif self.path.endswith('/answer'):
                requests.append((self.path, payload, self.headers.get('Idempotency-Key')))
                if self.path == '/v1/decisions/' + first['id'] + '/answer':
                    if len(requests) == 1:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                    else: self.send(receipt)
                else: self.send(dict(error='This card changed. Review the latest options.'), 409)
            else: self.send(dict(ok=True))

        def do_PUT(self): self.send(dict(registered=False, configured=False))
        def do_DELETE(self): self.send(dict(registered=False, configured=False))

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
                   if 'TestConfigurations' in document else [v for k, v in document.items() if not k.startswith('__')])
        for target in targets:
            if target.get('BlueprintName') == 'CoconutUITests':
                target.setdefault('EnvironmentVariables', {})['COCONUT_CARD_CONNECTION_URL'] = f'http://127.0.0.1:{server.server_port}'
        with tempfile.TemporaryDirectory(prefix='coconut-card-connection-') as folder:
            runfile = Path(folder) / 'Connection.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                '-only-testing:CoconutUITests/DecisionConnectionTests', '-parallel-testing-enabled', 'NO',
                '-resultBundlePath', str(args.result)], check=True)
        assert sync_failures
        assert len(requests) == 3, requests
        assert requests[0] == requests[1], 'A lost receipt must retry the exact request and key'
        assert requests[0][2] and requests[0][1]['revision'] == first['revision']
        assert requests[2][1]['revision'] == second['revision']
        print('PASS: sync failure permits answers; lost receipt reuses request; stale choice returns to Inbox.')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
