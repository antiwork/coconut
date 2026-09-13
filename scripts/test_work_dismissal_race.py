#!/usr/bin/env python3
"""Reproduce stale refreshes during rapid Work dismissals in the real app."""
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
    args = parser.parse_args()
    state = json.loads((ROOT / 'ios/Coconut/Fixtures/PreviewState.json').read_text())
    bot, task, decision = state['bots'][0], state['tasks'][0], state['decisions'][0]
    bot.update(pinned=False, status='idle')
    task.update(status='completed')
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    def pr(number, title, checks, **extra):
        return dict(id=str(uuid4()), bot_id=bot['id'], task_id=task['id'], kind='pr',
                    title=title, status='preparing', preview='', url=f'https://github.com/example/project/pull/{number}',
                    created_at=stamp, checked_at=stamp, observed_at=stamp, draft=True, pr_state='open', checks_state=checks, **extra)
    first = pr(42, 'First completed change', 'passed'); first.update(pr_state='merged', status='merged')
    closed = pr(43, 'Second completed change', 'passed'); closed.update(pr_state='merged', status='merged')
    state.update(bots=[bot], tasks=[task], decisions=[],
                 messages=[dict(id=str(uuid4()), bot_id=bot['id'], task_id=task['id'], role='assistant', kind='artifact', streaming=False, text=first['title'], artifact_id=first['id'], created_at=stamp)],
                 artifacts=[first, closed], server_time=stamp, cursor=50, push_configured=False)
    device = str(uuid4())
    lock = threading.Lock()
    pending = [None]
    commits = [0]
    stale_reads = [0]
    replayed = [False]

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
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            path = urlsplit(self.path).path
            if path.endswith('/dismiss'):
                target = next(a for a in state['artifacts'] if a['id'] == path.split('/')[-2])
                request = dict(captured=threading.Event(), committed=threading.Event(), announced=False)
                with lock: pending[0] = request
                if not request['captured'].wait(10):
                    self.send_error(500, 'The test did not capture an overlapping snapshot.'); return
                with lock:
                    target['work_dismissal_id'] = None if payload.get('undo') else payload['dismissal_id']
                    commits[0] += 1
                    pending[0] = None
                self.respond(dict(ok=True))
                request['committed'].set()
            elif path == '/v1/work/refresh': self.respond(dict(ok=True))
            else: self.respond(dict(device_id=device, token='race-fixture-token', snapshot=state))

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/test/proof':
                self.respond(dict(commits=commits[0], stale_reads=stale_reads[0], replayed=replayed[0])); return
            if path == '/v1/events':
                time.sleep(0.2)
                with lock:
                    request = pending[0]
                    if request and not request['announced']:
                        request['announced'] = True
                        state['cursor'] += 1
                        events = [dict(seq=state['cursor'], type='work.updated', created_at=stamp, data={})]
                    elif commits[0] >= 2 and not replayed[0]:
                        replayed[0] = True
                        state['cursor'] += 1
                        events = [dict(seq=state['cursor'], type='message.updated', created_at=stamp, data=state['messages'][0])]
                    else: events = []
                    value = dict(events=events, cursor=state['cursor'], server_time=stamp)
                if events and events[0]['type'] == 'message.updated': time.sleep(2)
                self.respond(value)
            else:
                with lock:
                    request = pending[0]
                    value = copy.deepcopy(state)
                    capture = request and not request['captured'].is_set()
                    if capture:
                        stale_reads[0] += 1
                        request['captured'].set()
                if capture:
                    request['committed'].wait(10)
                    time.sleep(0.5)
                self.respond(value)

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
            target.setdefault('EnvironmentVariables', {})['COCONUT_WORK_RACE_URL'] = f'http://127.0.0.1:{server.server_port}'
    try:
        with tempfile.TemporaryDirectory(prefix='coconut-work-') as folder:
            runfile = Path(folder) / 'Work.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            result = subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                '-only-testing:CoconutUITests/WorkDismissalRaceTests', '-parallel-testing-enabled', 'NO',
                '-resultBundlePath', str(args.result)])
            raise SystemExit(result.returncode)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__': main()
