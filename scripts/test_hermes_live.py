"""Run real Hermes creation, delegation, deletion, and restart checks on the Mac mini."""
from pathlib import Path
import base64
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import remote

PROGRAM = r'''
from pathlib import Path
from datetime import datetime, timezone
import contextlib, hashlib, io, json, os, shutil, signal, sys, tempfile, time
from uuid import uuid4
root = Path(sys.argv[1]).resolve()
code = Path(sys.argv[2]).resolve()
sys.path[:0] = [str(root / 'scripts'), str(code / 'backend')]
import host
from coconut.model_catalog import native_home
from coconut.providers import HermesProvider
from coconut.service import Service
os.umask(0o077)
checks = root / '.runtime/checks'
checks.mkdir(exist_ok=True, mode=0o700)
data = Path(tempfile.mkdtemp(prefix='bot-creation-', dir=checks)).resolve()
service = None
started = time.monotonic()
report = dict(version=2, check='hermes-chat-bot-lifecycle', passed=False,
              primary_bots_untouched=True, created_at=datetime.now(timezone.utc).isoformat())
stage = 'configure'
def expire(signum, frame):
    raise TimeoutError('The scratch check reached its time limit.')
signal.signal(signal.SIGALRM, expire)
signal.alarm(330)
def mutate(method, path, body):
    return service.mutate(method, path, body, str(uuid4()), 'chat-creation-check')
def wait_task(task_id, seconds=100):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = service.snapshot()
        task = next(item for item in state['tasks'] if item['id'] == task_id)
        if task['status'] in ('completed', 'failed', 'cancelled', 'needs_you'):
            if task['status'] != 'completed':
                report['task_status'] = task['status']
                report['task_summary'] = task.get('summary', '')[:500]
                raise AssertionError('The live task did not complete.')
            return state
        time.sleep(.2)
    raise TimeoutError('The live task did not finish in time.')
def provider():
    return HermesProvider(data, python=root / '.runtime/hermes/venv/bin/python',
                          hermes_root=root / '.runtime/hermes/source')
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        host.load_credentials(root / '.runtime/host')
        execution = provider()
        native_config = native_home(execution.catalog.root) / 'config.yaml'
        config_before = hashlib.sha256(native_config.read_bytes()).hexdigest()
        report['native_default'] = execution.model_catalog()['default']['model']
        service = Service(data, execution)
        parent = mutate('POST', '/v1/bots', dict(name='Creation Check', role='Coordination',
            instructions='Keep replies concise. Use available tools to carry out direct requests.',
            memory='Private parent note: cobalt-fern-731.'))
        stage = 'capability-question'
        question = mutate('POST', '/v1/bots/' + parent['id'] + '/messages',
                          {'text': 'Can you create bots yourself?', 'mode': 'queue'})
        state = wait_task(question['id'])
        assert len(state['bots']) == 1, 'A capability question must not create a bot.'
        answers = [row['text'] for row in state['messages'] if row['task_id'] == question['id']
                   and row['role'] == 'assistant' and row['kind'] == 'text']
        report['capability_answer'] = '\n'.join(answers)
        report['capability_question_creates_nothing'] = True
        stage = 'create-and-delegate'
        task = mutate('POST', '/v1/tasks/' + question['id'] + '/replies', {'text':
            'Create a new bot named Quartz Smoke with role Arithmetic and instructions '
            '"Answer arithmetic questions with only the number. Do not message other bots." '
            'Then ask Quartz Smoke what 1 + 1 is. Do not create any other bot.'})
        state = wait_task(task['id'], 130)
        assert len(state['bots']) == 2, 'The human request must create exactly one bot.'
        child = next(row for row in state['bots'] if row['id'] != parent['id'])
        assert child['name'] == 'Quartz Smoke'
        assert child['role'] == 'Arithmetic'
        assert child['profile_id'] == child['id'] != parent['profile_id']
        assert child['session_id'] != parent['session_id']
        assert child['model_override'] is None
        assert 'memory' not in child and 'preferences' not in child
        assert child['effective_model']['model'] == report['native_default']
        assert child['instructions'] == 'Answer arithmetic questions with only the number. Do not message other bots.'
        child_home = data / 'bots' / child['profile_id'] / 'home'
        assert (child_home / 'SOUL.md').read_text() == child['instructions']
        assert all('cobalt-fern-731' not in p.read_text() for p in (child_home / 'memories').glob('*.md'))
        assert not (child_home / 'auth.json').exists()
        assert child['approval_override'] is None
        assert len(state['decisions']) == 0
        report['created_from_chat'] = True
        report['independent_profile_and_session'] = True
        report['native_model_inherited'] = True
        report['private_memory_not_copied'] = True
        report['native_access_inherited'] = True
        report['no_approval_card'] = True
        child_tasks = [row for row in state['tasks'] if row['bot_id'] == child['id']]
        assert len(child_tasks) == 1, 'The new bot must receive the requested delegation once.'
        stage = 'child-execution'
        state = wait_task(child_tasks[0]['id'])
        answer = ''.join(row['text'] for row in state['messages'] if row['task_id'] == child_tasks[0]['id']
                         and row['sender_bot_id'] == child['id'] and row['role'] == 'assistant'
                         and row['kind'] == 'text').strip()
        assert answer == '2', 'The new bot must answer through real Hermes execution.'
        report['same_turn_delegation'] = True
        report['child_answer'] = answer
        # Let the durable return settle before checking restart recovery.
        deadline = time.monotonic() + 65
        while time.monotonic() < deadline:
            state = service.snapshot()
            if not any(row['status'] in service.ACTIVE for row in state['tasks']):
                break
            time.sleep(.2)
        assert not any(row['status'] in service.ACTIVE for row in state['tasks'])
        events = service.events(0)['events']
        creations = [row for row in events if row['type'] == 'bot.created' and row['bot_id'] == child['id']]
        assert len(creations) == 1
        assert creations[0]['data']['source_task_id'] == task['id']
        assert creations[0]['data']['created_by'] == 'chat-creation-check'
        report['attributed_creation_event'] = True
        before = dict(bot_ids=sorted(row['id'] for row in state['bots']),
                      message_ids=sorted(row['id'] for row in state['messages']),
                      profile_id=child['profile_id'], session_id=child['session_id'])
        stage = 'restart'
        service.close()
        service = None
        service = Service(data, provider(), start_workers=False)
        state = service.snapshot()
        saved = next(row for row in state['bots'] if row['id'] == child['id'])
        assert sorted(row['id'] for row in state['bots']) == before['bot_ids']
        assert sorted(row['id'] for row in state['messages']) == before['message_ids']
        assert saved['profile_id'] == before['profile_id'] and saved['session_id'] == before['session_id']
        assert len(service._all('bot_creation_receipts')) == 1
        report['restart_preserves_bot_profile_receipt_and_history'] = True
        service.start()
        stage = 'deletion-capability-question'
        question = mutate('POST', '/v1/bots/' + parent['id'] + '/messages',
                          {'text': 'Can you delete other bots yourself?', 'mode': 'queue'})
        state = wait_task(question['id'])
        assert len(state['bots']) == 2, 'A capability question must not delete a bot.'
        report['deletion_capability_answer'] = '\n'.join(row['text'] for row in state['messages']
            if row['task_id'] == question['id'] and row['role'] == 'assistant' and row['kind'] == 'text')
        report['capability_question_deletes_nothing'] = True
        stage = 'delete-from-chat'
        deletion = mutate('POST', '/v1/tasks/' + question['id'] + '/replies',
                          {'text': 'Delete Quartz Smoke now. Keep yourself.'})
        state = wait_task(deletion['id'])
        assert [bot['id'] for bot in state['bots']] == [parent['id']], 'Only the requested bot must disappear.'
        assert json.loads((child_home / 'coconut-profile.json').read_text())['status'] == 'deleted'
        assert len(service._all('bot_deletion_receipts')) == 1
        recorded = service._all('bot_deletion_receipts')[0]
        assert recorded['receipt']['bot_id'] == child['id']
        assert recorded['source_task_id'] == deletion['id']
        assert recorded['deleted_by'] == 'chat-creation-check'
        assert len(state['decisions']) == 0
        report['deleted_from_chat'] = True
        report['deleted_profile_retained'] = True
        report['deletion_answer'] = '\n'.join(row['text'] for row in state['messages']
            if row['task_id'] == deletion['id'] and row['role'] == 'assistant' and row['kind'] == 'text')
        stage = 'restart-after-deletion'
        service.close()
        service = None
        service = Service(data, provider(), start_workers=False)
        assert [bot['id'] for bot in service.snapshot()['bots']] == [parent['id']]
        assert service._all('bot_deletion_receipts') == [recorded]
        assert json.loads((child_home / 'coconut-profile.json').read_text())['status'] == 'deleted'
        report['restart_preserves_deletion'] = True
        report['native_default_unchanged'] = config_before == hashlib.sha256(native_config.read_bytes()).hexdigest()
        assert report['native_default_unchanged']
        report['passed'] = True
except BaseException as error:
    report.update(failed_stage=stage, error_type=type(error).__name__, error=str(error)[:500])
finally:
    signal.alarm(0)
    try:
        if service is not None:
            service.close()
        shutil.rmtree(data)
        report['scratch_removed'] = not data.exists()
    except BaseException as error:
        report.update(passed=False, scratch_removed=False, cleanup_error_type=type(error).__name__)
    report['elapsed_seconds'] = round(time.monotonic() - started, 2)
print('COCONUT_CREATION_RESULT=' + json.dumps(report, sort_keys=True))
'''

LAUNCHER = r'''
import base64, json, subprocess, sys, tempfile
from pathlib import Path
root = Path.home() / sys.argv[1]
payload = json.load(sys.stdin)
# Use the installed check before and after the live test. It sends no model requests.
command = [str(root / '.runtime/hermes/venv/bin/python'), str(root / 'scripts/hermes.py'), 'check']
subprocess.run(command, check=True, timeout=60)
with tempfile.TemporaryDirectory(prefix='bot-creation-code-', dir=root / '.runtime') as temporary:
    stage = Path(temporary)
    for name, content in payload['files'].items():
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(content))
    sys.path.insert(0, str(root / 'scripts'))
    import hermes, os
    env = hermes.environment()
    env['PYTHONPATH'] = os.pathsep.join((str(stage / 'backend'), str(root / '.runtime/hermes/source')))
    subprocess.run([str(root / '.runtime/hermes/venv/bin/python'), '-c', hermes.CHECK_CODE],
                   cwd=stage, env=env, check=True, timeout=60)
    path = stage / 'trial.py'
    path.write_text(payload['program'])
    result = subprocess.run([str(root / '.runtime/hermes/venv/bin/python'), str(path), str(root), str(stage)], timeout=345)
    subprocess.run(command, check=True, timeout=60)
    sys.exit(result.returncode)
'''


def main(program=PROGRAM, evidence_name='mac-mini-chat-bot-lifecycle.json'):
    files = {str(path.relative_to(ROOT)): base64.b64encode(path.read_bytes()).decode()
             for path in (ROOT / 'backend/coconut').glob('*.py')}
    payload = json.dumps({'program': program, 'files': files}).encode()
    config = remote.configuration()
    result = remote.ssh(config, ['python3', '-c', LAUNCHER, config['directory']],
                        input_data=payload, timeout=480, allow_failure=True)
    lines = [line for line in result.stdout.decode(errors='replace').splitlines()
             if line.startswith('COCONUT_CREATION_RESULT=')]
    if result.returncode or len(lines) != 1:
        print(json.dumps({'passed': False, 'ssh_returncode': result.returncode, 'receipt_present': len(lines) == 1,
                          'error': result.stderr.decode(errors='replace')[:1500]}))
        raise SystemExit(1)
    report = json.loads(lines[0].split('=', 1)[1])
    report['hermes_contract_and_source_checked'] = True
    path = ROOT / 'evidence' / evidence_name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + '\n')
    path.chmod(0o600)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
