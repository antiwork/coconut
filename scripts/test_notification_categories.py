#!/usr/bin/env python3
"""Check notification taps against an isolated API and real simulator notifications."""
import argparse
import copy
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--slow-open', action='store_true')
    args = parser.parse_args()
    state = json.loads((ROOT / 'ios/Coconut/Fixtures/PreviewState.json').read_text())
    bot, task, decision = state['bots'][0], state['tasks'][0], state['decisions'][0]
    bot.update(pinned=False, status='idle')
    task.update(status='completed')
    deferred = copy.deepcopy(decision)
    deferred.update(id=str(uuid4()), title='Review the deferred notification?', inbox_deferred_at='2026-09-12T12:00:00Z')
    message = dict(id=str(uuid4()), bot_id=bot['id'], task_id=task['id'], thread_id=task['id'],
                   role='assistant', kind='text', text='The notification opens this exact reply.',
                   streaming=False, created_at=state['server_time'])
    state.update(bots=[bot], tasks=[task], decisions=[decision, deferred], messages=[message], artifacts=[],
                 cursor=50, push_configured=False)
    device = str(uuid4())
    hold_state = threading.Event()
    release_state = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def respond(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try: self.wfile.write(data)
            except BrokenPipeError: pass

        def do_POST(self):
            url = urlsplit(self.path)
            if url.path == '/test/push':
                kind = parse_qs(url.query)['kind'][0]
                if args.slow_open: hold_state.set()
                payload = dict(aps=dict(alert=dict(title=bot['name'], body='Notification fixture ' + kind), sound='default'),
                    coconut=dict(version=1, device_id=device, bot_id=bot['id'], task_id=task['id'], kind=kind))
                payload['coconut']['decision_id' if kind == 'decision' else 'message_id'] = deferred['id'] if kind == 'decision' else message['id']
                def push():
                    time.sleep(2)
                    subprocess.run(['xcrun', 'simctl', 'push', args.simulator, 'com.gumroad.coconut', '-'],
                                   input=json.dumps(payload), text=True, check=True)
                threading.Thread(target=push, daemon=True).start()
                self.respond(dict(ok=True))
            elif url.path == '/test/release-state':
                release_state.set()
                self.respond(dict(ok=True))
            else:
                self.respond(dict(device_id=device, token='notification-fixture-token', snapshot=state))

        def do_GET(self):
            if urlsplit(self.path).path == '/v1/events':
                time.sleep(1)
                self.respond(dict(events=[], cursor=50, server_time=state['server_time']))
            else:
                if hold_state.is_set(): release_state.wait(20)
                self.respond(state)

        def do_PUT(self): self.respond(dict(registered=True, configured=False))
        def do_DELETE(self): self.respond(dict(registered=False, configured=False))

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    source = args.xctestrun.resolve()
    def absolute(value):
        if isinstance(value, str): return value.replace('__TESTROOT__', str(source.parent))
        if isinstance(value, list): return [absolute(v) for v in value]
        if isinstance(value, dict): return {k: absolute(v) for k, v in value.items()}
        return value
    document = absolute(plistlib.loads(source.read_bytes()))
    targets = ([t for c in document['TestConfigurations'] for t in c['TestTargets']]
               if 'TestConfigurations' in document else [v for v in document.values() if isinstance(v, dict)])
    for target in targets:
        if target.get('BlueprintName') == 'CoconutUITests':
            target.setdefault('EnvironmentVariables', {})['COCONUT_NOTIFICATION_TEST_URL'] = f'http://127.0.0.1:{server.server_port}'
    try:
        with tempfile.TemporaryDirectory(prefix='coconut-notifications-') as folder:
            runfile = Path(folder) / 'Notifications.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            result = subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                '-only-testing:CoconutUITests/' + ('NotificationOpenPerformanceTests' if args.slow_open else 'NotificationCategoryFlowTests'), '-parallel-testing-enabled', 'NO',
                '-resultBundlePath', str(args.result)])
            raise SystemExit(result.returncode)
    finally:
        release_state.set()
        server.shutdown()
        server.server_close()


if __name__ == '__main__': main()
