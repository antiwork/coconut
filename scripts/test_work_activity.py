#!/usr/bin/env python3
"""Check Work against an isolated API in the real app."""
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
    first = pr(42, 'Review the refund change', 'passed', activity='Reviewing with Fable',
               activity_state='working', activity_observed_at=stamp, activity_bot_id=bot['id'])
    closed = pr(43, 'Update the help article', 'passed'); closed.update(pr_state='merged', status='merged')
    state.update(bots=[bot], tasks=[task], decisions=[],
                 messages=[dict(id=str(uuid4()), bot_id=bot['id'], task_id=task['id'], role='assistant', kind='artifact', streaming=False, text=first['title'], artifact_id=first['id'], created_at=stamp)],
                 artifacts=[first, closed], server_time=stamp, cursor=50, push_configured=False)
    fail_next = [False]
    delay_next = [1]
    device = str(uuid4())

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
            url = urlsplit(self.path)
            if url.path == '/test/activity':
                first.update({key: payload[key] for key in ('title', 'activity', 'activity_state') if key in payload})
                first['activity_observed_at'] = datetime.now(timezone.utc).isoformat()
                self.respond(dict(ok=True))
            elif url.path == '/test/delay-next':
                delay_next[0] = 8
                self.respond(dict(ok=True))
            elif url.path == '/test/fail-next':
                fail_next[0] = True
                self.respond(dict(ok=True))
            elif url.path.endswith('/dismiss'):
                delay = delay_next[0]; delay_next[0] = 1
                time.sleep(delay)
                if fail_next[0]:
                    fail_next[0] = False
                    self.send_response(409); self.send_header('Content-Type', 'application/json'); self.end_headers()
                    self.wfile.write(b'{"error":"The work changed. Try again."}')
                    return
                closed['work_dismissal_id'] = None if payload.get('undo') else payload['dismissal_id']
                self.respond(dict(ok=True))
            elif url.path == '/test/complete':
                state['artifacts'] = []
                self.respond(dict(ok=True))
            elif url.path == '/v1/work/refresh':
                self.respond(dict(ok=True))
            else:
                self.respond(dict(device_id=device, token='work-fixture-token', snapshot=state))

        def do_GET(self):
            if urlsplit(self.path).path == '/v1/events':
                time.sleep(1)
                self.respond(dict(events=[], cursor=50, server_time=state['server_time']))
            else: self.respond(state)

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
            target.setdefault('EnvironmentVariables', {})['COCONUT_WORK_ACTIVITY_URL'] = f'http://127.0.0.1:{server.server_port}'
    try:
        with tempfile.TemporaryDirectory(prefix='coconut-work-') as folder:
            runfile = Path(folder) / 'Work.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            result = subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                '-only-testing:CoconutUITests/WorkActivityFlowTests', '-parallel-testing-enabled', 'NO',
                '-resultBundlePath', str(args.result)])
            raise SystemExit(result.returncode)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__': main()
