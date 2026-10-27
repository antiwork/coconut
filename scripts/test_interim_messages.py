#!/usr/bin/env python3
"""Check complete progress messages through real Hermes on isolated Mac mini data."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import remote

PROGRAM = r'''
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import contextlib, hashlib, io, json, os, shlex, shutil, signal, sys, tempfile, time
root, code, runtime = map(Path, sys.argv[1:4])
sys.path[:0] = [str(root / 'scripts'), str(code / 'backend')]
import host
from coconut.providers import HermesProvider
from coconut.service import Service
os.umask(0o077)
checks = root / '.runtime/checks'
checks.mkdir(mode=0o700, exist_ok=True)
data = Path(tempfile.mkdtemp(prefix='interim-messages-', dir=checks)).resolve()
service = None
started = time.monotonic()
stage = 'configure'
report = dict(check='complete-interim-messages', passed=False,
              primary_bots_untouched=True, created_at=datetime.now(timezone.utc).isoformat())
def expire(*_):
    raise TimeoutError('The isolated check reached its time limit.')
signal.signal(signal.SIGALRM, expire)
signal.alarm(240)
def provider():
    return HermesProvider(data, python=runtime / 'venv/bin/python', hermes_root=runtime / 'source')
def mutate(method, path, body):
    return service.mutate(method, path, body, str(uuid4()), 'interim-message-check')
def assistant_messages(task_id):
    state = service.snapshot(content_only=True)
    return [m for m in state['messages'] if m['task_id'] == task_id
            and m['role'] == 'assistant' and m['kind'] == 'text']
def visible(task_id):
    return [m for m in assistant_messages(task_id) if not m['streaming']]
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        host.load_credentials(root / '.runtime/host')
        service = Service(data, provider())
        assert not service.push.configured, 'The scratch check must not send push notifications.'
        with service.transaction():
            # Only a phone paired through the relay tunnel, with its static key, gets notifications.
            service.db.execute('INSERT INTO devices(id,name,token_hash,created_at,peer_key) VALUES (?,?,?,?,?)',
                               ('interim-check-device', 'iPhone', 'interim-check-token', '2026-01-01T00:00:00Z', 'interim-check-key'))
            service.db.execute('INSERT INTO push_devices (device_id,token,environment,enabled,updated) VALUES (?,?,?,?,?)',
                               ('interim-check-device', '0' * 64, 'development', 1, time.time()))
            service.db.execute('INSERT INTO push_preferences VALUES (?,?)',
                               ('interim-check-device', json.dumps(dict(messages=True, decisions=True))))
        bot = mutate('POST', '/v1/bots', dict(name='Cobalt Timer Check',
            instructions='Keep each reply concise. Before you use a tool, send one complete sentence that describes the current task.'))
        progress = 'I will check the cobalt scratch timer.'
        final = 'The cobalt scratch timer is complete.'
        begin, end = data / 'timer-started.json', data / 'timer-finished.json'
        script = ('from pathlib import Path; import json,time; '
                  + 'Path(' + repr(str(begin)) + ').open("x").write(json.dumps({"at":time.monotonic()})); '
                  + 'time.sleep(12); '
                  + 'Path(' + repr(str(end)) + ').open("x").write(json.dumps({"at":time.monotonic()})); '
                  + 'print("The cobalt scratch timer finished.")')
        command = shlex.quote(str(runtime / 'venv/bin/python')) + ' -c ' + shlex.quote(script)
        prompt = ('Check the cobalt scratch timer. First, send exactly this complete sentence: '
                  + progress + '\nThen run this exact terminal command once:\n' + command
                  + '\nAfter the command finishes, reply with exactly this sentence: ' + final
                  + '\nUse only this command. Do not contact another bot or service.')
        stage = 'run-timer'
        submitted = time.monotonic()
        task = mutate('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text=prompt, mode='queue'))
        report['model'] = service.provider.model
        progress_seen, final_seen = None, None
        while time.monotonic() - submitted < 150:
            raw_messages = assistant_messages(task['id'])
            assert all(m['text'] in (progress, final) for m in raw_messages), 'A token fragment reached the transcript.'
            messages = [m for m in raw_messages if not m['streaming']]
            progress_messages = [m for m in messages if m['text'] == progress]
            final_messages = [m for m in messages if m['text'] == final]
            assert len(progress_messages) <= 1 and len(final_messages) <= 1, 'A reply appeared twice.'
            current = service._get('tasks', task['id'])
            if progress_messages and progress_seen is None:
                assert not end.exists(), 'The first reply arrived after the command finished.'
                assert current['status'] == 'working', 'The first reply must appear during work.'
                progress_seen = time.monotonic()
                progress_id = progress_messages[0]['id']
                assert service._get('messages', progress_id).get('_interim') is True, 'Progress must retain its delivery type.'
                report['first_complete_reply_seconds'] = round(progress_seen - submitted, 3)
                report['first_reply_before_command_finished'] = True
                report['first_reply_while_working'] = True
                with service.lock:
                    assert service.db.execute('SELECT COUNT(*) FROM push_items WHERE message_id=?',
                                              (progress_id,)).fetchone()[0] == 0, 'Progress must not queue a push notification.'
                report['progress_has_no_push_job'] = True
            if final_messages and final_seen is None:
                assert end.exists(), 'The final reply arrived before the command finished.'
                final_seen = time.monotonic()
                final_id = final_messages[0]['id']
            if end.exists() and progress_seen is None:
                raise AssertionError('The first reply did not arrive before the command finished.')
            if current['status'] in ('completed', 'failed', 'cancelled', 'needs_you'):
                assert current['status'] == 'completed', 'The task did not complete: ' + current.get('summary', '')
                if progress_seen is not None and final_seen is not None:
                    break
            time.sleep(.05)
        else:
            raise TimeoutError('The real model did not finish the timer task.')
        assert progress_seen is not None and final_seen is not None, 'Both complete replies must appear.'
        timer_started = json.loads(begin.read_text())['at']
        timer_finished = json.loads(end.read_text())['at']
        assert timer_finished - timer_started >= 12, 'The slow command did not complete its delay.'
        assert progress_seen < timer_finished <= final_seen, 'Reply timing must surround command completion.'
        messages = visible(task['id'])
        assert [m['text'] for m in messages] == [progress, final], 'The transcript must contain each reply once, in order.'
        with service.lock:
            assert service.db.execute('SELECT COUNT(*) FROM push_items WHERE message_id=?',
                                      (progress_id,)).fetchone()[0] == 0, 'Progress must not queue a push notification.'
            assert service.db.execute('SELECT COUNT(*) FROM push_items WHERE message_id=?',
                                      (final_id,)).fetchone()[0] == 1, 'The final reply must queue one push notification.'
        report.update(final_reply_seconds=round(final_seen - submitted, 3),
                      command_started_seconds=round(timer_started - submitted, 3),
                      command_finished_seconds=round(timer_finished - submitted, 3),
                      complete_bubbles_only=True, distinct_final_reply=True,
                      no_duplicate_progress=True, final_has_one_push_job=True)
        stage = 'restart'
        before = [(m['id'], m['text'], m['streaming']) for m in messages]
        service.close()
        service = None
        service = Service(data, provider(), start_workers=False)
        after = [(m['id'], m['text'], m['streaming']) for m in visible(task['id'])]
        assert after == before, 'Restart changed the complete replies.'
        assert service._get('tasks', task['id'])['status'] == 'completed'
        report.update(restart_preserves_reply_ids_and_text=True, passed=True)
except BaseException as error:
    report.update(failed_stage=stage, error_type=type(error).__name__, error=str(error)[:600])
finally:
    signal.alarm(0)
    try:
        if service is not None:
            service.close()
        shutil.rmtree(data)
        report['scratch_removed'] = not data.exists()
    except BaseException as error:
        report.update(passed=False, scratch_removed=False, cleanup_error_type=type(error).__name__)
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
print('COCONUT_INTERIM_RESULT=' + json.dumps(report, sort_keys=True))
'''

LAUNCHER = r'''
import base64, hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path
root = Path.home() / sys.argv[1]
payload = json.load(sys.stdin)
runtime = root / payload['runtime']
runtime.resolve().relative_to(root.resolve())
checks = root / '.runtime/checks'
checks.mkdir(mode=0o700, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='interim-code-', dir=checks) as temporary:
    stage = Path(temporary)
    for name, content in payload['files'].items():
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = base64.b64decode(content)
        assert hashlib.sha256(raw).hexdigest() == payload['source_hashes'][name]
        path.write_bytes(raw)
    path = stage / 'trial.py'
    path.write_text(payload['program'])
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([str(runtime / 'venv/bin/python'), str(path), str(root), str(stage), str(runtime)],
                            env=env, capture_output=True, text=True, timeout=270)
    print(result.stdout, end='')
    if result.returncode:
        print(json.dumps(dict(launcher_failed=True, returncode=result.returncode,
                              error_type='RuntimeFailure')), file=sys.stderr)
    sys.exit(result.returncode)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', default='.runtime/hermes')
    parser.add_argument('--evidence', type=Path, default=ROOT / '.runtime/checks/mac-mini-interim-messages.json')
    parser.add_argument('--disable-interim-callback', action='store_true')
    args = parser.parse_args()
    runtime = Path(args.runtime)
    if runtime.is_absolute() or '..' in runtime.parts:
        parser.error('--runtime must select a folder below the remote Coconut folder.')
    source = {str(path.relative_to(ROOT)): path.read_bytes()
              for path in (ROOT / 'backend/coconut').glob('*.py')}
    if args.disable_interim_callback:
        name = 'backend/coconut/hermes_worker.py'
        before = b'interim_assistant_callback=publish_interim,'
        assert source[name].count(before) == 1, 'The control must disable exactly one callback.'
        source[name] = source[name].replace(before, b'interim_assistant_callback=None,')
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in source.items()}
    payload = dict(program=PROGRAM, runtime=str(runtime), source_hashes=hashes,
                   files={name: base64.b64encode(raw).decode() for name, raw in source.items()})
    config = remote.configuration()
    result = remote.ssh(config, ['python3', '-c', LAUNCHER, config['directory']],
                        input_data=json.dumps(payload).encode(), timeout=300, allow_failure=True)
    lines = [line for line in result.stdout.decode(errors='replace').splitlines()
             if line.startswith('COCONUT_INTERIM_RESULT=')]
    if result.returncode or len(lines) != 1:
        print(json.dumps(dict(passed=False, ssh_returncode=result.returncode,
                              receipt_present=len(lines) == 1,
                              error=result.stderr.decode(errors='replace')[:1500])))
        raise SystemExit(1)
    report = json.loads(lines[0].split('=', 1)[1])
    report['source_hashes'] = hashes
    report['source_package_sha256'] = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    report['check_program_sha256'] = hashlib.sha256(PROGRAM.encode()).hexdigest()
    report['runtime'] = str(runtime)
    report['interim_callback_disabled'] = args.disable_interim_callback
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(report, indent=2) + '\n')
    args.evidence.chmod(0o600)
    print(json.dumps({key: value for key, value in report.items() if key != 'source_hashes'}, indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
