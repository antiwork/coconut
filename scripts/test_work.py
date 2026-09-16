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
                    activity='Reviewing the change', activity_state='working', work_state='working', activity_observed_at=stamp,
                    created_at=stamp, checked_at=stamp, observed_at=stamp, draft=True, pr_state='open', checks_state=checks, **extra)
    first = pr(42, 'Fix the refund email', 'running')
    duplicate = dict(first, id=str(uuid4()))
    closed = pr(45, 'Already merged', 'passed'); closed.update(pr_state='merged', work_state='completed')
    decision.update(status='pending')
    state.update(bots=[bot], tasks=[task], decisions=[decision], messages=[],
                 artifacts=[first, duplicate, pr(43, 'Restore failed downloads', 'failed'),
                            pr(44, 'Update account settings', 'passed', refresh_error='GitHub is unreachable.'), closed],
                 server_time=stamp, cursor=50, push_configured=False)
    state['artifacts'] += [
        dict(id='export-active', bot_id=bot['id'], task_id=task['id'], kind='work',
             title='Export account data', status='working', preview='', url=None, created_at=stamp,
             work_state='working', activity='Preparing the export', activity_state='working', activity_observed_at=stamp),
        dict(id='report-complete', bot_id=bot['id'], task_id=task['id'], kind='work',
             title='Prepare the weekly report', status='completed', preview='', url=None, created_at=stamp,
             activity='Report saved', activity_state='completed', activity_observed_at=stamp),
    ]
    state['artifacts'].append(dict(id='long-queued', bot_id=bot['id'], task_id=task['id'], kind='work',
        title='Detect failed UPI purchases with late payments and verify that recovery does not charge the buyer again',
        status='waiting', work_state='queued', activity='Queued', work_queue_position=3, activity_bot_id=bot['id'],
        activity_state='waiting', activity_observed_at=stamp, preview='', created_at=stamp))
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
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            url = urlsplit(self.path)
            if url.path == '/test/order':
                for item in state['artifacts']:
                    if item['kind'] == 'pr' and item.get('pr_state') == 'open':
                        item.update(work_state='queued' if body['export_working'] else 'working',
                                    work_queue_position=1 if body['export_working'] else None)
                    if item['id'] == 'export-active':
                        item.update(work_state='working', status='working', activity='Export moved into execution' if body['export_working'] else 'Preparing the export')
                self.respond(dict(ok=True))
            elif url.path == '/test/work-state':
                item = next(a for a in state['artifacts'] if a['id'] == 'export-active')
                item.update(status=body['status'], work_state=body['status'], activity_state=body['status'], activity=body['activity'])
                if body['status'] == 'needs_decision':
                    item['work_decision_id'] = decision['id']
                    state['decisions'] = [decision]
                else:
                    item.pop('work_decision_id', None)
                if body.get('resolved'): state['decisions'] = []
                self.respond(dict(ok=True))
            elif url.path == '/test/complete':
                state['artifacts'] = []
                self.respond(dict(ok=True))
            elif url.path.startswith('/v1/work/items/') and url.path.endswith('/dismiss'):
                item = next(a for a in state['artifacts'] if a['id'] == url.path.split('/')[-2])
                item['work_dismissal_id'] = None if body.get('undo') else body['dismissal_id']
                self.respond(dict(ok=True))
            elif url.path.startswith('/v1/work/items/') and url.path.endswith('/cancel'):
                item = next(a for a in state['artifacts'] if a['id'] == url.path.split('/')[-2])
                item.update(status='cancelled', work_state='cancelled', activity='Stopped', activity_state='cancelled')
                self.respond(dict(ok=True))
            elif url.path == '/v1/work/refresh':
                self.respond(dict(ok=True))
            else:
                self.respond(dict(device_id=device, token='work-fixture-token', snapshot=state))

        def do_GET(self):
            if urlsplit(self.path).path == '/v1/events':
                time.sleep(1)
                self.respond(dict(events=[], cursor=50, server_time=state['server_time']))
            else:
                observed = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                for item in state['artifacts']:
                    if item['kind'] == 'work': item['activity_observed_at'] = observed
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
            target.setdefault('EnvironmentVariables', {})['COCONUT_WORK_TEST_URL'] = f'http://127.0.0.1:{server.server_port}'
    try:
        with tempfile.TemporaryDirectory(prefix='coconut-work-') as folder:
            runfile = Path(folder) / 'Work.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            result = subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                '-only-testing:CoconutUITests/WorkFlowTests', '-parallel-testing-enabled', 'NO',
                '-resultBundlePath', str(args.result)])
            raise SystemExit(result.returncode)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__': main()
