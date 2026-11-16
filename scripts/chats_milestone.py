#!/usr/bin/env python3
"""Run native iPhone flows for To-dos, work navigation, and Unread against an isolated local fixture."""
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

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    source = args.xctestrun.resolve()
    state = json.loads((ROOT / 'ios/Coconut/Fixtures/PreviewState.json').read_text())
    state['cursor'] = 70
    state['decisions'] = []
    for index, bot in enumerate(state['bots']):
        bot.update(unread_count=0 if index == 0 else 3, pinned=index == 1)
    archived = dict(state['bots'][0], id='archived-agent', archived=True)
    state['bots'].append(archived)
    coconut = dict(state['bots'][0], id='coconut-agent', name='Coconut', coconut=True, unread_count=0)
    state['bots'].append(coconut)
    state['artifacts'] = [dict(id=id, bot_id=bot, title=title, kind='work', status=status,
                               preview='', created_at=state['server_time'])
                          for id, bot, title, status in (
        ('working', state['bots'][0]['id'], 'Prepare the next release', 'working'),
        ('blocked', state['bots'][1]['id'], 'Review the release notes', 'blocked'),
        ('coconut-work', coconut['id'], 'Check Coconut updates', 'working'),
        ('completed', state['bots'][0]['id'], 'Finished work', 'completed'),
        ('archived', archived['id'], 'Work of an archived agent', 'working'))]
    room = dict(room='a' * 64, name='Example Team', state='active', unread=1,
                created_at=state['server_time'], chats=[
        dict(id=id, name=name, unread=unread, pinned=True, notifications=not muted,
             created_at=state['server_time'])
        for id, name, unread, muted in (
            ('notices', 'Notices', 5, True), ('reports', 'Reports', 2, False), ('design', 'Design', 0, False))])
    changed = threading.Condition()
    events = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def respond(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if self.path == '/v1/pair':
                self.respond(dict(token='fixture-only-token', device_id='chats-test-device'))
                return
            if self.path.endswith('/read'):
                bot_id = self.path.split('/')[-2]
                with changed:
                    for bot in state['bots']:
                        if bot['id'] == bot_id:
                            bot['unread_count'] = 0
                            state['cursor'] += 1
                            events.append(dict(seq=state['cursor'], type='bot.updated', bot_id=bot_id,
                                               created_at=state['server_time'], data=copy.deepcopy(bot)))
                    changed.notify_all()
            self.respond(dict(ok=True))

        def do_DELETE(self): self.respond(dict(registered=False, configured=False))
        def do_PUT(self): self.respond(dict(registered=True, configured=False))

        def do_GET(self):
            url = urlsplit(self.path)
            with changed:
                if url.path == '/v1/events':
                    after = int(parse_qs(url.query).get('after', ['0'])[0])
                    deadline = time.monotonic() + 20
                    while state['cursor'] <= after and time.monotonic() < deadline:
                        changed.wait(max(0, deadline - time.monotonic()))
                    value = dict(events=[event for event in events if event['seq'] > after],
                                 cursor=state['cursor'], server_time=state['server_time'])
                elif url.path == '/v1/state': value = state
                elif url.path == '/v1/team/rooms': value = dict(rooms=[room], helper=True)
                elif url.path.startswith('/v1/history/'):
                    value = dict(messages=[], tasks=[], artifacts=[], decisions=[], has_more=False)
                else: value = {}
                self.respond(value)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='coconut-chats-test-') as directory:
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
                    target.setdefault('EnvironmentVariables', {})['COCONUT_CHATS_TEST_URL'] = f'http://127.0.0.1:{server.server_port}'
            runfile = Path(directory) / 'Chats.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                            '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                            '-only-testing:CoconutUITests/ArchiveAgentFlowTests', '-parallel-testing-enabled', 'NO',
                            '-resultBundlePath', str(args.result.resolve())], cwd=ROOT, check=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__': main()
