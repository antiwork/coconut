#!/usr/bin/env python3
"""Check decision images and gesture containment through the real Coconut API."""
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--images', type=Path, nargs='+', required=True)
    args = parser.parse_args()
    if len(args.images) < 2: parser.error('Supply at least two PNG or JPEG previews.')
    from coconut.providers import FixtureProvider
    from coconut.service import YieldForDecision
    with tempfile.TemporaryDirectory(prefix='coconut-followup-') as folder:
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
        decision = question('Approve the nationality form?',
            'Review the desktop and mobile form. These previews use synthetic data.',
            [dict(id='approve', label='Approve appearance'), dict(id='revise', label='Revise appearance')],
            previews=[dict(path=str(path.resolve()), caption=path.stem.replace('-', ' ')) for path in args.images])
        server = APIServer(('127.0.0.1', 0), service)
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
                    COCONUT_PREVIEW_URL=f'http://127.0.0.1:{server.server_port}',
                    COCONUT_PREVIEW_CODE=service.pairing_code(), COCONUT_PREVIEW_DECISION=decision['id'],
                    COCONUT_PREVIEW_FIRST=decision['previews'][0]['artifact_id'],
                    COCONUT_PREVIEW_SECOND=decision['previews'][1]['artifact_id'])
        runfile = Path(folder) / 'Followup.xctestrun'; runfile.write_bytes(plistlib.dumps(document))
        try:
            result = subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                '-only-testing:CoconutUITests/DecisionPreviewFlowTests', '-parallel-testing-enabled', 'NO', '-collect-test-diagnostics', 'never',
                '-resultBundlePath', str(args.result)])
            if result.returncode: raise SystemExit(result.returncode)
            saved = service._get('decisions', decision['id'])
            assert saved['status'] == 'pending'
            assert not saved.get('inbox_deferred_at')
        finally:
            server.shutdown(); server.server_close(); service.close()


if __name__ == '__main__':
    main()
