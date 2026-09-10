#!/usr/bin/env python3
"""Verify conversation continuity with real Hermes in disposable Mac mini profiles."""
from pathlib import Path
import base64
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import remote
from test_hermes_live import LAUNCHER

PROGRAM = r'''
from pathlib import Path
from uuid import uuid4
import contextlib, io, json, os, sys, tempfile
root, code = map(Path, sys.argv[1:3])
sys.path[:0] = [str(code/'backend'), str(root/'scripts'), str(root/'.runtime/hermes/source')]
import host
from coconut.providers import HermesProvider
from coconut.service import Service
from hermes_state import SessionDB
os.umask(0o077)
report = {'passed': False, 'existing_bots_untouched': True}
stage = 'start'
with tempfile.TemporaryDirectory(prefix='continuity-', dir=root/'.runtime/checks') as temporary:
 data = Path(temporary)
 service = None
 def start():
  return Service(data, HermesProvider(data, python=root/'.runtime/hermes/venv/bin/python',
                                    hermes_root=root/'.runtime/hermes/source'), start_workers=False)
 def write(path, body=None, key=None):
  return service.mutate('POST', path, body or {}, key or str(uuid4()), 'continuity-check')
 def turn(bot, text):
  task = write('/v1/bots/'+bot['id']+'/messages', {'text': text})
  service._run_task(task['id'])
  current = service._get('tasks', task['id'])
  assert current['status'] == 'completed', current.get('summary')
  answer = ''.join(m['text'] for m in service.snapshot()['messages'] if m['task_id'] == task['id']
                   and m['role'] == 'assistant' and m['kind'] == 'text')
  assert not any(word in answer.lower() for word in ['context window', 'compaction', 'compressed', 'new session'])
  return answer
 try:
  with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
   host.load_credentials(root/'.runtime/host')
   service = start()
   bot = write('/v1/bots', {'name':'Continuity Check', 'instructions':
       'Reply briefly. Do not save these test exchanges in memory. Continue directly from the conversation without tools unless asked to search.'})
   other = write('/v1/bots', {'name':'Separate Check'})
   secret = 'parcel-' + uuid4().hex[:16]
   # This models saved app messages from before native history was connected.
   task = write('/v1/bots/'+bot['id']+'/messages', {'text':'The package label is '+secret+'. Reply OK.'})
   with service.transaction():
    saved = service._get('tasks', task['id']); saved['status']='completed'; service._put('tasks', saved)
    service._message(bot['id'], 'OK.', task_id=task['id'])
   path = data/'bots'/bot['id']/'home/state.db'
   with SessionDB(path) as db:
    db.create_session(bot['session_id'], source='coconut')
    db.append_messages_batch(bot['session_id'], [
        {'role':'user', 'content':'Reply with Ready.'}, {'role':'assistant', 'content':'Ready.'}])
   stage = 'legacy-import'
   assert secret in turn(bot, 'What package label did I give you earlier? Return only the label. Do not use tools.')
   with SessionDB(path) as db:
    assert not any(row.get('tool_calls') for row in db.get_messages(bot['session_id']))
   report['legacy_messages_in_active_context_without_search'] = True
   revision = 'revision-' + uuid4().hex[:16]
   turn(bot, 'The delivery revision is '+revision+'. Reply OK. Do not save it in memory.')
   with SessionDB(path) as db:
    assert db.get_session(bot['session_id'])
    original = db.get_messages_as_conversation(bot['session_id'])
    assert secret in str(original)
    assert revision in str(original), 'A new turn must enter native history after the one-time import.'
    assert original[0]['content'].startswith('The package label is ')
    assert any(row['content'] == 'Reply with Ready.' for row in original), 'The import must preserve existing native history.'
   report['partial_native_history_preserved'] = True
   report['new_turns_persist_in_hermes'] = True
   stage = 'service-restart'
   service.close(); service = start()
   assert revision in turn(bot, 'Give me the delivery revision I told you. Return only the revision. Do not use tools.')
   with SessionDB(path) as db:
    assert not any(row.get('tool_calls') for row in db.get_messages(bot['session_id']))
   report['active_context_after_service_restart_without_search'] = True
   stage = 'native-compaction'
   with SessionDB(path) as db:
    sid = db.resolve_resume_session_id(bot['session_id'])
    summary = {'role':'user', 'content':'Earlier conversation includes a package label. Retrieve the exact value with session_search when asked.',
               '_compressed_summary':True}
    db.archive_and_compact(sid, [summary])
    assert secret not in str(db.get_resume_conversations(sid)[0])
    assert secret in str(db.get_resume_conversations(sid)[1])
   assert secret in turn(bot, 'Search our earlier conversation for the package label I gave you. Return only its exact value.')
   report['native_archived_history_search'] = True
   stage = 'native-rotation'
   with SessionDB(path) as db:
    parent = db.resolve_resume_session_id(bot['session_id'])
    child, holder = str(uuid4()), 'continuity-check'
    assert db.try_acquire_compression_lock(parent, holder)
    db.publish_compression_child(parent_session_id=parent, child_session_id=child, source='coconut',
        messages=[summary], compression_lock_holder=holder)
    db.release_compression_lock(parent, holder)
    assert db.resolve_resume_session_id(bot['session_id']) == child
   assert secret in turn(bot, 'Find the package label from our earlier exchange. Return only its exact value.')
   report['native_rotation_resumed'] = True
   stage = 'global-restart'
   before = service.snapshot()
   key = str(uuid4()); receipt = write('/v1/bots/restart', key=key)
   assert write('/v1/bots/restart', key=key) == receipt
   assert service.snapshot()['messages'] == before['messages']
   assert secret in turn(bot, 'Search our past conversation for the package label. Return only its exact value.')
   report['global_restart_preserves_recall'] = True
   assert '2' == turn(other, 'Reply with only the number 2. Do not use tools.').strip()
   with SessionDB(data/'bots'/other['id']/'home/state.db') as db:
    assert secret not in str(db.get_messages_as_conversation(db.resolve_resume_session_id(other['session_id'])))
    assert not db.get_session(bot['session_id'])
   report['profile_history_isolated'] = True
   report['no_runtime_notices_in_replies'] = True
   report['passed'] = True
 except Exception as error:
  report.update(stage=stage, error_type=type(error).__name__, detail=str(error)[:300])
 finally:
  if service: service.close()
print('COCONUT_CONTINUITY_RESULT='+json.dumps(report), flush=True)
'''


def main():
    files = {str(path.relative_to(ROOT)): base64.b64encode(path.read_bytes()).decode()
             for path in (ROOT/'backend/coconut').glob('*.py')}
    payload = json.dumps({'program': PROGRAM, 'files': files}).encode()
    config = remote.configuration()
    result = remote.ssh(config, ['python3', '-c', LAUNCHER, config['directory']],
                        input_data=payload, timeout=480, allow_failure=True)
    lines = [line for line in result.stdout.decode(errors='replace').splitlines()
             if line.startswith('COCONUT_CONTINUITY_RESULT=')]
    report = json.loads(lines[0].split('=', 1)[1]) if len(lines) == 1 else {
        'passed': False, 'ssh_returncode': result.returncode,
        'error': result.stderr.decode(errors='replace')[-2000:]}
    path = ROOT/'evidence/conversation-continuity.json'
    path.write_text(json.dumps(report, indent=2)+'\n'); path.chmod(0o600)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['passed'] and result.returncode == 0 else 1)


if __name__ == '__main__':
    main()
