#!/usr/bin/env python3
"""Check native sync recovery against an isolated local API fixture."""
import argparse
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
TEST = 'StateSyncFlowTests/testConnectionRecoveryKeepsConversationAndStreamsUpdates'


class Fixture:
    def __init__(self):
        self.changed = threading.Condition()
        self.state = json.loads((ROOT / 'ios/Coconut/Fixtures/PreviewState.json').read_text())
        self.state['cursor'] = 50
        self.mode = 'connected'
        self.events = []
        self.state_reads_during_stream = 0
        self.message = next(m for m in self.state['messages'] if m['bot_id'].endswith('000002') and m['kind'] == 'text' and m['role'] == 'assistant')
        for bot in self.state['bots']:
            bot['unread_count'] = 0
            bot['pinned'] = False

    def stage(self, stage):
        with self.changed:
            self.mode = stage
            if stage in ('recover', 'stream'):
                self.message['text'] = 'Connection restored.' if stage == 'recover' else 'Streamed without reloading the conversation.'
                self.message['streaming'] = True
                self.state['cursor'] += 1
                self.events.append(dict(seq=self.state['cursor'], type='message.updated', bot_id=self.message['bot_id'],
                                        task_id=self.message['task_id'], created_at=self.message['created_at'], data=dict(self.message)))
            self.changed.notify_all()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    args = parser.parse_args()
    source = args.xctestrun.resolve()
    fixture = Fixture()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def respond(self, status, value):
            print(f'Fixture {self.command} {urlsplit(self.path).path}: {status}', flush=True)
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass

        def do_POST(self):
            if self.path == '/v1/pair':
                self.respond(200, dict(token='fixture-only-token', device_id='00000000-0000-4000-8000-000000000099'))
            else: self.respond(200, dict(ok=True))

        def do_DELETE(self): self.respond(200, dict(registered=False, configured=False))

        def do_PUT(self): self.respond(200, dict(registered=True, configured=False))

        def do_GET(self):
            url = urlsplit(self.path)
            with fixture.changed:
                if url.path == '/v1/events':
                    after = int(parse_qs(url.query).get('after', ['0'])[0])
                    deadline = time.monotonic() + 20
                    while fixture.state['cursor'] <= after and fixture.mode != 'offline' and time.monotonic() < deadline:
                        fixture.changed.wait(max(0, deadline - time.monotonic()))
                    value = dict(events=[e for e in fixture.events if e['seq'] > after], cursor=fixture.state['cursor'], server_time=fixture.state['server_time'])
                elif url.path == '/v1/state':
                    if fixture.mode == 'stream': fixture.state_reads_during_stream += 1
                    value = fixture.state
                else: value = {}
                if fixture.mode == 'offline' or (url.path == '/v1/state' and fixture.mode == 'stream'):
                    self.respond(503, dict(error='Fixture temporarily unavailable.'))
                else: self.respond(200, value)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='coconut-sync-test-') as directory:
            def absolute(value):
                if isinstance(value, str): return value.replace('__TESTROOT__', str(source.parent))
                if isinstance(value, list): return [absolute(v) for v in value]
                if isinstance(value, dict): return {k:absolute(v) for k,v in value.items()}
                return value
            document = absolute(plistlib.loads(source.read_bytes()))
            targets = [t for c in document['TestConfigurations'] for t in c['TestTargets']] if 'TestConfigurations' in document else [t for k,t in document.items() if not k.startswith('__')]
            for target in targets:
                if target.get('BlueprintName') == 'CoconutUITests':
                    target.setdefault('EnvironmentVariables', {})['COCONUT_SYNC_TEST_URL'] = f'http://127.0.0.1:{server.server_port}'
            runfile = Path(directory) / 'Sync.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            command = ['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                       '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                       '-only-testing:CoconutUITests/' + TEST, '-parallel-testing-enabled', 'NO']
            with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as process:
                for line in process.stdout:
                    print(line, end='', flush=True)
                    if 'COCONUT_SYNC_STAGE=' in line:
                        fixture.stage(line.strip().split('COCONUT_SYNC_STAGE=')[-1])
                result = process.wait()
            if result: raise SystemExit(result)
            if fixture.state_reads_during_stream:
                raise SystemExit('Streaming unexpectedly requested the full state.')
            print('Verified recovery, draft retention, and streaming without a full-state request.')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__': main()
