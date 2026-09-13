#!/usr/bin/env python3
"""Test draft cards in the real app against an isolated Coconut service."""
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
from coconut.providers import FixtureProvider
from coconut.server import APIServer, create_pairing_code
from coconut.service import Service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='coconut-drafts-') as folder:
        root = Path(folder)
        service = Service(root, FixtureProvider(root), start_workers=False)
        def write(path, data): return service.mutate('POST', path, data, str(uuid4()))
        bot = write('/v1/bots', dict(name='Desk', role=''))
        task = write('/v1/bots/' + bot['id'] + '/messages', dict(text='Draft a message to Sarah about Friday.', mode='queue'))
        with service.transaction():
            stored = service._get('tasks', task['id'])
            stored['status'] = 'working'
            service._put('tasks', stored)
        service.emit(task['id'], 'native_proposal', dict(key='friday-review', title='Move Friday’s review?',
            draft=dict(channel='Gmail', sender='alex@example.com', recipient='sarah@example.com', subject='Friday review',
                       body='Hi Sarah,\n\nCould we move Friday’s review to 2 PM?\n\nThanks,\nAlex')))
        service.emit(task['id'], 'native_proposal', dict(key='notes', title='Share the meeting notes?',
            draft=dict(channel='GitHub', recipient='https://github.com/example/project/issues/42',
                       body='The updated notes are ready.')))
        service.emit(task['id'], 'completed', dict(summary='Drafts prepared.'))
        code = create_pairing_code(root, lifetime=900)
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
                target.setdefault('EnvironmentVariables', {}).update(COCONUT_DRAFT_TEST_URL=f'http://127.0.0.1:{server.server_port}',
                    COCONUT_DRAFT_TEST_CODE=code)
        runfile = root / 'Drafts.xctestrun'
        runfile.write_bytes(plistlib.dumps(document))
        try:
            result = subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                '-only-testing:CoconutUITests/MessageDraftFlowTests', '-parallel-testing-enabled', 'NO',
                '-resultBundlePath', str(args.result)])
            if result.returncode: raise SystemExit(result.returncode)
            decisions = service._all('decisions')
            email = next(d for d in decisions if d['_proposal_key'] == 'friday-review')
            comment = next(d for d in decisions if d['_proposal_key'] == 'notes')
            assert email['status'] == 'approved' and email['revision'] == 2
            assert email['draft']['subject'] == 'Updated Friday review'
            assert comment['status'] == 'rejected'
            assert len(service._all('tasks')) == 2, 'Only Send may create a continuation.'
            assert not service._all('native_receipts'), 'The fixture must never send a real message.'
            print('Verified: shared draft, edit stays pending, exact approval, one continuation, discard sends nothing.')
        finally:
            server.shutdown(); server.server_close(); service.close()


if __name__ == '__main__': main()
