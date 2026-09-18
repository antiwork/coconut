#!/usr/bin/env python3
"""Exercise chat activity transitions against an isolated loopback fixture."""
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
TEST = 'ChatActivityFlowTests/testThinkingFollowsRunnableAndStreamingTransitions'
BOT_ID = '00000000-0000-4000-8000-000000000002'
TASK_ID = '00000000-0000-4000-8000-000000000023'


class Fixture:
    def __init__(self):
        self.changed = threading.Condition()
        self.state = json.loads((ROOT / 'ios/Coconut/Fixtures/PreviewState.json').read_text())
        self.state['cursor'] = 70
        self.events = []
        self.bot = next(bot for bot in self.state['bots'] if bot['id'] == BOT_ID)
        self.task = next(task for task in self.state['tasks'] if task['id'] == TASK_ID)
        self.message = next(message for message in self.state['messages']
                            if message['bot_id'] == BOT_ID and message['role'] == 'assistant')
        self.bot['status'] = 'idle'
        self.task['status'] = 'queued'
        self.message['streaming'] = False
        for bot in self.state['bots']:
            bot['unread_count'] = 0
            bot['pinned'] = False

    def emit(self, kind, data, task_id=TASK_ID):
        self.state['cursor'] += 1
        self.events.append(dict(seq=self.state['cursor'], type=kind, bot_id=BOT_ID, task_id=task_id,
                                created_at=self.state['server_time'], data=dict(data)))

    def stage(self, stage):
        with self.changed:
            self.message['streaming'] = stage == 'streaming'
            self.task['status'] = {'working': 'working', 'waiting': 'waiting', 'streaming': 'waiting',
                                   'completed': 'completed', 'failed': 'failed'}[stage]
            self.bot['status'] = 'working' if stage == 'working' else 'failed' if stage == 'failed' else 'idle'
            self.emit('task.updated', self.task)
            self.emit('bot.updated', self.bot, task_id=None)
            self.emit('message.updated', self.message)
            self.changed.notify_all()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    source = args.xctestrun.resolve()
    fixture = Fixture()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def respond(self, status, value):
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
                    while fixture.state['cursor'] <= after and time.monotonic() < deadline:
                        fixture.changed.wait(max(0, deadline - time.monotonic()))
                    value = dict(events=[event for event in fixture.events if event['seq'] > after],
                                 cursor=fixture.state['cursor'], server_time=fixture.state['server_time'])
                elif url.path == '/v1/state': value = fixture.state
                else: value = {}
                self.respond(200, value)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='coconut-chat-activity-') as directory:
            def absolute(value):
                if isinstance(value, str): return value.replace('__TESTROOT__', str(source.parent))
                if isinstance(value, list): return [absolute(item) for item in value]
                if isinstance(value, dict): return {key: absolute(item) for key, item in value.items()}
                return value
            document = absolute(plistlib.loads(source.read_bytes()))
            targets = ([target for config in document['TestConfigurations'] for target in config['TestTargets']]
                       if 'TestConfigurations' in document else
                       [target for key, target in document.items() if not key.startswith('__')])
            for target in targets:
                if target.get('BlueprintName') == 'CoconutUITests':
                    target.setdefault('EnvironmentVariables', {})['COCONUT_CHAT_ACTIVITY_TEST_URL'] = f'http://127.0.0.1:{server.server_port}'
            runfile = Path(directory) / 'ChatActivity.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            command = ['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                       '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                       '-only-testing:CoconutUITests/' + TEST, '-parallel-testing-enabled', 'NO',
                       '-resultBundlePath', str(args.result.resolve())]
            with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1) as process:
                for line in process.stdout:
                    print(line, end='', flush=True)
                    if 'COCONUT_CHAT_ACTIVITY_STAGE=' in line:
                        fixture.stage(line.strip().split('COCONUT_CHAT_ACTIVITY_STAGE=')[-1])
                result = process.wait()
            if result: raise SystemExit(result)
            print('Verified queued, working, waiting, streaming, completed, and failed chat activity transitions.')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__': main()
