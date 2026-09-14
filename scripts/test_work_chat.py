#!/usr/bin/env python3
"""Exercise work questions through the iOS app and the real Coconut API."""
import argparse
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import threading
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from coconut.service import Service
from coconut.server import APIServer


class Provider:
    name, ready, model = 'work-chat-test', True, 'fixture'

    def __init__(self):
        self.requests = []

    def start_task(self, task, bot, history, emit):
        self.requests.append(task)
        assert task['_work_context']['title'] == 'Export account data'
        text = ('The export is prepared. Its final check is next.' if task['_work_status_only']
                else 'The export needs its final check.')
        emit('text_delta', {'text': text})
        emit('completed', {'summary': 'Complete.'})

    def cancel_task(self, task_id):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='coconut-work-chat-') as folder:
        provider = Provider()
        service = Service(folder, provider, start_workers=False)
        def write(path, data):
            return service.mutate('POST', path, data, str(uuid4()), 'test')
        bot = write('/v1/bots', dict(name='Desk', role='Coordinates work', avatar='fruit.coconut'))
        original = write(f'/v1/bots/{bot["id"]}/messages', dict(text='Export account data', mode='queue'))
        with service.transaction():
            task = service._get('tasks', original['id']); task['status'] = 'working'
            service._put('tasks', task)
        item = service._register_work(task['id'], dict(key='export', title='Export account data',
            activity='Waiting for the final check', status='waiting'))
        with service.transaction():
            task['status'] = 'waiting'; service._put('tasks', task)
        before = json.dumps(service._get('tasks', original['id']), sort_keys=True)
        server = APIServer(('127.0.0.1', 0), service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        service.start()
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
                target.setdefault('EnvironmentVariables', {}).update(
                    COCONUT_WORK_CHAT_URL=f'http://127.0.0.1:{server.server_port}',
                    COCONUT_WORK_CHAT_CODE=service.pairing_code(), COCONUT_WORK_CHAT_ID=item['id'])
        runfile = Path(folder) / 'WorkChat.xctestrun'; runfile.write_bytes(plistlib.dumps(document))
        try:
            result = subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                '-only-testing:CoconutUITests/WorkChatTests', '-parallel-testing-enabled', 'NO',
                '-resultBundlePath', str(args.result)])
            if result.returncode: raise SystemExit(result.returncode)
            assert len(provider.requests) == 2, 'Expected one question and one update.'
            assert len([m for m in service._all('messages') if m['role'] == 'assistant' and m['kind'] == 'text']) == 2
            assert json.dumps(service._get('tasks', original['id']), sort_keys=True) == before
            assert len([a for a in service._all('artifacts') if a['kind'] == 'work']) == 1
        finally:
            server.shutdown(); server.server_close(); service.close()


if __name__ == '__main__':
    main()
