#!/usr/bin/env python3
"""Check navigation in the real app with a long history and an old decision."""
import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    state = json.loads((ROOT / 'ios/Coconut/Fixtures/PreviewState.json').read_text())
    bot = dict(state['bots'][1], pinned=False, status='idle', unread_count=0)
    task = dict(state['tasks'][0], bot_id=bot['id'], status='completed')
    decision = dict(state['decisions'][0], bot_id=bot['id'], task_id=task['id'],
                    title='Review the old report?', status='pending')
    state.update(bots=[bot], tasks=[task], decisions=[decision], messages=[], artifacts=[], routines=[], cursor=50)
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    examples = json.loads((ROOT / 'ios/CoconutUITests/Fixtures/ConversationLayoutMessages.json').read_text())
    for index in range(1_000):
        example = examples[index % len(examples)]
        row = dict(id=f'history-{index}', bot_id=bot['id'], task_id=task['id'], role=example['role'],
                   kind='text', text=example['text'], created_at=(start + timedelta(seconds=index)).isoformat(), streaming=False)
        if index == 4:
            row.update(kind='decision', decision_id=decision['id'], role='assistant', text='')
        if index == 999:
            row.update(role='assistant', text='Latest response.')
        state['messages'].append(row)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def send(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass

        def do_GET(self):
            if self.path == '/v1/state': self.send(state)
            elif self.path.startswith('/v1/events'):
                time.sleep(1)
                self.send(dict(events=[], cursor=state['cursor'], server_time=state['server_time']))
            elif self.path == '/v1/work': self.send(dict(artifacts=[]))
            else: self.send({})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
            if self.path == '/v1/pair':
                self.send(dict(token='navigation-test', device_id='navigation-test', server_name='Test Mac'))
            elif self.path == '/v1/bots/' + bot['id'] + '/messages':
                state['messages'].append(dict(state['messages'][-1], id='sent-reply', role='user',
                    text=payload['text'], client_request_id=self.headers.get('Idempotency-Key'),
                    created_at=(start + timedelta(seconds=1_001)).isoformat()))
                state['messages'].append(dict(state['messages'][999], id='fresh-answer',
                    text='Reply from the old card received.', created_at=(start + timedelta(seconds=1_002)).isoformat()))
                state['cursor'] += 1
                self.send(task)
            else: self.send(dict(ok=True))

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        source = args.xctestrun.resolve()
        def absolute(value):
            if isinstance(value, str): return value.replace('__TESTROOT__', str(source.parent))
            if isinstance(value, list): return [absolute(item) for item in value]
            if isinstance(value, dict): return {key: absolute(item) for key, item in value.items()}
            return value
        document = absolute(plistlib.loads(source.read_bytes()))
        targets = ([target for config in document['TestConfigurations'] for target in config['TestTargets']]
                   if 'TestConfigurations' in document else [value for value in document.values() if isinstance(value, dict)])
        for target in targets:
            if target.get('BlueprintName') == 'CoconutUITests':
                target.setdefault('EnvironmentVariables', {})['COCONUT_NAVIGATION_TEST_URL'] = f'http://127.0.0.1:{server.server_port}'
        with tempfile.TemporaryDirectory(prefix='coconut-navigation-') as folder:
            runfile = Path(folder) / 'Navigation.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                            '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                            '-only-testing:CoconutUITests/NavigationPerformanceTests', '-parallel-testing-enabled', 'NO',
                            '-resultBundlePath', str(args.result)], check=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
