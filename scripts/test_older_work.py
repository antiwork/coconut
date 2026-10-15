#!/usr/bin/env python3
"""Check that Task details loads older finished Work on request, through iOS and the Coconut server."""
import argparse
import plistlib
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from coconut.providers import FixtureProvider
from coconut.server import APIServer
from coconut.service import Service


def stamp(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace('+00:00', 'Z')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='coconut-older-work-') as folder:
        service = Service(folder, FixtureProvider(folder), start_workers=False)
        service.provider.supports_native_outcomes = True
        # A Kanban snapshot with one Hermes task that finished two days ago and one that finished ten days ago.
        service.kanban = SimpleNamespace(tasks={}, observed_at=stamp(0), close=lambda: None)
        bot = service.mutate('POST', '/v1/bots', dict(name='Desk', role='Coordinates work', avatar='fruit.coconut'), str(uuid4()), 'test')
        for title, days in (('Recent export', 2), ('Archived export', 10)):
            identity = str(uuid4())
            service.kanban.tasks[identity] = dict(id='native-' + identity, external_id=identity, title=title, status='done',
                last_activity_at=time.time() - days * 86_400, dependencies=[], diagnostics=[])
            with service.transaction():
                service._put('tasks', dict(id=identity, bot_id=bot['id'], conversation_bot_id=bot['id'], session_id=identity,
                    thread_id=identity, title=title, status='completed', summary='', created_at=stamp(days), updated_at=stamp(days)))
                service._put('artifacts', dict(id=str(uuid4()), kind='work', bot_id=bot['id'], task_id=identity, status='working',
                    title=title, preview='', created_at=stamp(days), _native_outcome_id='native-' + identity))
        server = APIServer(('127.0.0.1', 0), service)
        try:
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
                        COCONUT_OLDER_WORK_URL=f'http://127.0.0.1:{server.server_port}', COCONUT_OLDER_WORK_CODE=service.pairing_code(),
                        COCONUT_OLDER_WORK_BOT_ID=bot['id'], COCONUT_OLDER_WORK_TITLE='Archived export')
            runfile = Path(folder) / 'OlderWork.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            result = subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                '-only-testing:CoconutUITests/OlderWorkFlowTests', '-parallel-testing-enabled', 'NO',
                '-resultBundlePath', str(args.result)])
            if result.returncode:
                raise SystemExit(result.returncode)
        finally:
            server.shutdown()
            server.server_close()
            service.close()


if __name__ == '__main__':
    main()
