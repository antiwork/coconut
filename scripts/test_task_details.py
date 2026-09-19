#!/usr/bin/env python3
"""Check delegated task details and cancellation through iOS and native Hermes."""
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

    def prepare_kanban_worker(self, *args):
        raise AssertionError('Task details must not start a worker.')

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
        import os
        provider.hermes_root = Path(os.environ['COCONUT_HERMES_ROOT'])
        provider.python = os.environ['COCONUT_HERMES_PYTHON']
        service = Service(folder, provider, start_workers=False)
        def write(path, data):
            return service.mutate('POST', path, data, str(uuid4()), 'test')
        bot = write('/v1/bots', dict(name='Desk', role='Coordinates work', avatar='fruit.coconut'))
        executor = write('/v1/bots', dict(name='Dev', role='Implements work', avatar='fruit.apple'))
        provider.supports_native_outcomes = True
        original = write(f'/v1/bots/{executor["id"]}/messages', dict(text='Export account data', mode='queue'))
        with service.transaction():
            origin = service._get('tasks', original['id'])
            origin.update(status='completed', summary='Request recorded', conversation_bot_id=bot['id'], source_bot_id=bot['id'])
            service._put('tasks', origin)
        import os
        source_path = os.environ['COCONUT_HERMES_ROOT']
        python = os.environ['COCONUT_HERMES_PYTHON']
        def native(program, *values):
            result = subprocess.run([python, '-c', "import sys\nfrom coconut.kanban_runtime import Board\nboard=Board(sys.argv[1])\n" + program,
                str(service.root / 'kanban'), *values], env=dict(os.environ,
                PYTHONPATH=str(ROOT / 'backend') + os.pathsep + source_path), capture_output=True, text=True, check=True)
            return result.stdout.strip()
        identity = native("print(board.kb.create_task(board.conn,title='Export account data',body='Export data',assignee='default',tenant=sys.argv[2],session_id=sys.argv[3],initial_status='blocked'))", executor['id'], original['id'])
        service.kanban.call({'op': 'snapshot'})
        service._adopt_native_outcomes()
        owner = next(t for t in service._all('tasks') if t.get('_native_outcome_id') == identity)
        item = service._get('artifacts', owner['_work_context_id'])
        earlier = write(f'/v1/bots/{bot["id"]}/messages', dict(text='Earlier request', mode='queue'))
        with service.transaction():
            retired = service._get('tasks', earlier['id'])
            retired['status'] = 'cancelled'
            service._put('tasks', retired)
            legacy = dict(item, id=str(uuid4()), task_id=earlier['id'], title='Obsolete progress label', _native_outcome_id=None)
            service._put('artifacts', legacy)
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
                    COCONUT_WORK_CHAT_CODE=service.pairing_code(), COCONUT_WORK_CHAT_ID=item['id'],
                    COCONUT_TASK_BOT_ID=bot['id'])
        runfile = Path(folder) / 'WorkChat.xctestrun'; runfile.write_bytes(plistlib.dumps(document))
        try:
            result = subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                '-only-testing:CoconutUITests/TaskDetailsFlowTests', '-parallel-testing-enabled', 'NO',
                '-resultBundlePath', str(args.result)])
            if result.returncode: raise SystemExit(result.returncode)
            service.kanban.call({'op': 'snapshot'})
            assert service.kanban.tasks[owner['id']]['status'] == 'archived', 'Cancellation must reach native Hermes.'
            assert not provider.requests, 'Reading task details must not start a model turn.'
        finally:
            server.shutdown(); server.server_close(); service.close()


if __name__ == '__main__':
    main()
