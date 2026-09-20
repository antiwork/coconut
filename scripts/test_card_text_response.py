#!/usr/bin/env python3
"""Check card text replies through the real Coconut API."""
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
from coconut.server import APIServer, Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    from coconut.providers import FixtureProvider
    from coconut.service import YieldForDecision
    with tempfile.TemporaryDirectory(prefix='coconut-card-reply-') as folder:
        service = Service(folder, FixtureProvider(Path(folder)), start_workers=False)
        def write(path, data):
            return service.mutate('POST', path, data, str(uuid4()), 'test')
        bot = write('/v1/bots', dict(name='Desk', role='Coordinates work'))
        task = write(f'/v1/bots/{bot["id"]}/messages', dict(text='Review the proposed report.', mode='queue'))
        with service.transaction():
            task = service._get('tasks', task['id']); task['status'] = 'working'
            service._put('tasks', task)
        def question(title, context, options, **extra):
            with service.transaction():
                current = service._get('tasks', task['id'])
                current['status'] = 'working'
                service._put('tasks', current)
            try:
                service.emit(task['id'], 'native_question', dict(title=title, context=context, options=options, **extra))
            except YieldForDecision as result:
                return service._get('decisions', result.decision_id)
        original = question('Save the report?', 'The report is ready.',
            [dict(id='save', label='Save report'), dict(id='leave', label='Leave unchanged')])
        attempted = []
        accepted = threading.Event()
        class ReplyHandler(Handler):
            def do_POST(self):
                if self.path == f'/v1/decisions/{original["id"]}/answer':
                    attempted.append(self.headers.get('Idempotency-Key'))
                    if len(attempted) == 1:
                        self._body()
                        self._json(503, {'error': 'The test connection failed. Try again.'})
                        return
                if self.path == '/test/continue':
                    assert accepted.wait(10), 'The saved reply must retry after restart.'
                    def resume(task, bot, history, decision, emit):
                        assert decision['status'] == 'replied'
                        assert decision['correction'] == 'What did you verify?'
                        emit('text_delta', dict(text='I verified the local report.'))
                        emit('native_question', dict(title='Save the verified report?', context='The local report passed its checks.',
                            options=[dict(id='save', label='Save verified report')]))
                    service.provider.resume_task = resume
                    service._run_task(task['id'])
                    self._json(200, {'ok': True})
                    return
                super().do_POST()
                if self.path == f'/v1/decisions/{original["id"]}/answer':
                    accepted.set()
        server = APIServer(('127.0.0.1', 0), service)
        server.RequestHandlerClass = ReplyHandler
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
                target.setdefault('EnvironmentVariables', {}).update(
                    COCONUT_CARD_REPLY_URL=f'http://127.0.0.1:{server.server_port}',
                    COCONUT_CARD_REPLY_CODE=service.pairing_code(), COCONUT_CARD_REPLY_ID=original['id'])
        runfile = Path(folder) / 'CardReply.xctestrun'; runfile.write_bytes(plistlib.dumps(document))
        try:
            result = subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                '-only-testing:CoconutUITests/DecisionInboxTests/testTextReplyThroughAPIClosesCardAndShowsFreshFollowup', '-parallel-testing-enabled', 'NO', '-collect-test-diagnostics', 'never',
                '-resultBundlePath', str(args.result)])
            if result.returncode: raise SystemExit(result.returncode)
            assert service._get('decisions', original['id'])['status'] == 'replied'
            assert len([d for d in service._all('decisions') if d['status'] == 'pending']) == 1
            assert len([m for m in service._all('messages') if m['role'] == 'user' and m.get('decision_id') == original['id']]) == 1
            assert not service._all('contexts')
            assert len(attempted) >= 2 and len(set(attempted)) == 1, 'Retry must reuse the saved answer.'
            print('Verified one text reply, one fresh card, and no nested conversation.')
        finally:
            print('Answer attempts:', len(attempted), 'Decision states:', [d['status'] for d in service._all('decisions')], flush=True)
            server.shutdown(); server.server_close(); service.close()


if __name__ == '__main__':
    main()
