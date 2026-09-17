#!/usr/bin/env python3
"""Check chat dismissal, failed requests, and the Inbox in the real app."""
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
    parser.add_argument('--position', action='store_true')
    args = parser.parse_args()
    state = json.loads((ROOT / 'ios/Coconut/Fixtures/PreviewState.json').read_text())
    bot = dict(state['bots'][1], pinned=False, status='idle', unread_count=0)
    task = dict(state['tasks'][0], bot_id=bot['id'], status='completed')
    decision = dict(state['decisions'][0], bot_id=bot['id'], task_id=task['id'],
                    title='Delete the sample export?', status='pending')
    state.update(bots=[bot], tasks=[task], decisions=[decision], messages=[], artifacts=[], routines=[], cursor=50)
    state['messages'] = [dict(id='decision-message', bot_id=bot['id'], task_id=task['id'],
        role='assistant', kind='decision', text='', decision_id=decision['id'],
        created_at=state['server_time'], streaming=False)]
    if args.position:
        decision.update(title='Keep this report?', context='The report is ready.', explanation='',
                        consequences='', resource='', proposal_preview=None, action_type='question',
                        created_at='2026-09-17T09:00:00Z',
                        options=[dict(id='keep', label='Keep', answer='approve', explanation=''),
                                 dict(id='discard', label='Discard', answer='reject', explanation='')])
        state['messages'] = [dict(id=identity, bot_id=bot['id'], task_id=task['id'], role='assistant',
            kind='text', text=text, created_at=stamp, streaming=False) for identity,text,stamp in [
                ('before', 'Before the card.', '2026-09-17T08:00:00Z'),
                ('after', 'After the card.', '2026-09-17T10:00:00Z')]]
        earlier_message = state['messages'].pop(0)
        state['history_before'] = {bot['id']: 10}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def send(self, data, status=200):
            body = json.dumps(data).encode()
            self.send_response(status)
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
            elif args.position and self.path.startswith('/v1/history/' + bot['id']):
                self.send(dict(messages=[earlier_message, dict(id='original-card-message', bot_id=bot['id'], task_id=task['id'],
                    role='assistant', kind='decision', text='', decision_id=decision['id'],
                    created_at=decision['created_at'], streaming=False)], before=None))
            elif self.path == '/v1/work': self.send(dict(artifacts=[]))
            else: self.send({})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
            if self.path == '/v1/pair':
                self.send(dict(token='navigation-test', device_id='navigation-test', server_name='Test Mac'))
            elif self.path == '/v1/decisions/' + decision['id'] + '/inbox':
                requests.append((payload, self.headers.get('Idempotency-Key')))
                time.sleep(3)
                if len(requests) == 1:
                    self.send(dict(error='Temporary failure'), status=503)
                else:
                    decision.update(status='dismissed', inbox_swipe_id='chat-dismiss')
                    state['cursor'] += 1
                    self.send(decision)
            else:
                requests.append((dict(unexpected_path=self.path), None))
                self.send(dict(ok=True))

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
                target.setdefault('EnvironmentVariables', {})['COCONUT_DISMISS_TEST_URL'] = f'http://127.0.0.1:{server.server_port}'
        with tempfile.TemporaryDirectory(prefix='coconut-dismiss-') as folder:
            runfile = Path(folder) / 'Dismiss.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                            '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                            '-only-testing:CoconutUITests/' + ('ChatCardPositionTests' if args.position else 'ChatCardDismissTests'), '-parallel-testing-enabled', 'NO',
                            '-resultBundlePath', str(args.result)], check=True)
        if args.position:
            assert not requests, requests
            print('Verified: card stays between messages before and after history loading.')
            return
        assert len(requests) == 2, requests
        assert all(payload.get('action') == 'dismiss' for payload, key in requests), requests
        assert all(key for payload, key in requests), 'Dismiss requests need idempotency keys'
        assert requests[0][1] != requests[1][1], 'A confirmed failure permits a new request'
        print('Verified: two dismiss requests, no answer request, failed request restored the card.')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
