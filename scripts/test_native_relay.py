"""Verify native bot delivery and a pending decision on isolated Mac mini profiles."""
from test_hermes_live import main

PROGRAM = r'''
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
import contextlib, io, json, os, shutil, signal, sys, tempfile, threading, time
root, code = map(Path, sys.argv[1:3])
sys.path[:0] = [str(root / 'scripts'), str(code / 'backend')]
import host
from coconut.providers import HermesProvider
from coconut.service import Service
from coconut.work_capacity import execution_key
os.umask(0o077)
data = Path(tempfile.mkdtemp(prefix='native-relay-', dir=root / '.runtime/checks'))
service = None
busy = threading.Lock()
stage = 'configure'
report = dict(check='native-relay-and-decision', passed=False, primary_bots_untouched=True,
              phone_closed=True, created_at=datetime.now(timezone.utc).isoformat())
def timeout(*_): raise TimeoutError('The isolated check exceeded its time limit.')
signal.signal(signal.SIGALRM, timeout)
signal.alarm(330)
def provider():
    return HermesProvider(data, python=root / '.runtime/hermes/venv/bin/python', hermes_root=root / '.runtime/hermes/source')
def write(method, path, body):
    return service.mutate(method, path, body, str(uuid4()), 'native-relay-check')
def wait(predicate, seconds=90):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        state=service.snapshot()
        if predicate(state): return state
        failed=[t for t in state['tasks'] if t['status']=='failed']
        if failed: raise AssertionError('A task failed: '+failed[0].get('summary',''))
        time.sleep(.2)
    raise TimeoutError('The check did not reach the expected state.')
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        host.load_credentials(root / '.runtime/host')
        service=Service(data, provider())
        sender=write('POST','/v1/bots',dict(name='Relay Sender', instructions='Use native coconut_message_bot for a direct bot handoff. Keep replies under one sentence.'))
        recipient=write('POST','/v1/bots',dict(name='Relay Recipient', instructions='When asked to save the check, first call coconut_ask with title Save the check? and options save / Save it, hold / Do nothing. After the person chooses Save it, use terminal once to run python3 -c "from pathlib import Path; Path(\'relay-once.txt\').open(\'x\').write(\'native relay verified\')". Do not create the file before the choice. On context questions explain that this saves a private test file. Keep replies under one sentence.'))
        busy.acquire()
        recipient_ready=threading.Event()
        run_task=service._run_task
        def run_with_busy_recipient(task_id):
            with service.lock:
                task=service._get('tasks',task_id)
                if task['bot_id']==recipient['id']:
                    # Dispatch assigns the execution session before entering the worker.
                    assert task.get('_execution_session_id')
                    service.bot_locks.setdefault(execution_key(task),busy)
                    recipient_ready.set()
            return run_task(task_id)
        service._run_task=run_with_busy_recipient
        stage='native-send-while-busy'
        parent=write('POST','/v1/bots/'+sender['id']+'/messages',dict(text='Use coconut_message_bot to ask Relay Recipient to save the check. Then finish your turn without waiting.', mode='queue'))
        state=wait(lambda s:any(t['id']==parent['id'] and t['status']=='waiting' for t in s['tasks']))
        deliveries=[r for r in service._all('deliveries') if r['source_task_id']==parent['id']]
        assert len(deliveries)==1
        child_id=deliveries[0]['receipt']['task_id']
        assert recipient_ready.wait(10),'Recipient worker did not reach its execution lock'
        child=service._get('tasks',child_id)
        assert child['status']=='queued'
        assert execution_key(child)!=execution_key(service._get('tasks',parent['id']))
        assert service.bot_locks[execution_key(child)] is busy and busy.locked()
        records=[r for r in service._all('native_receipts') if r['task_id']==parent['id'] and r['action']['name']=='message_agent']
        assert len(records)==1 and records[0]['state']=='completed'
        report['native_message_agent_used']=True
        report['busy_recipient_queued_without_blocking_sender']=True
        busy.release()
        stage='pending-decision'
        state=wait(lambda s:any(d['task_id']==child_id and d['status']=='pending' for d in s['decisions']))
        decision=next(d for d in state['decisions'] if d['task_id']==child_id)
        independent=write('POST','/v1/bots/'+sender['id']+'/messages',dict(text='What is 3 + 4? Answer with the number only.',mode='queue'))
        state=wait(lambda s:any(t['id']==independent['id'] and t['status']=='completed' for t in s['tasks']))
        assert service._get('decisions',decision['id'])['status']=='pending'
        # Native dispatch owns the execution workspace, separately from the bot profile.
        workspace=data/'workspaces'/execution_key(child)
        assert Path(child['_kanban_claim']['workspace'])==workspace
        file=workspace/'relay-once.txt'
        assert not file.exists()
        report['sender_continues_while_decision_waits']=True
        stage='restart-with-decision'
        service.close(); service=None
        service=Service(data,provider())
        assert service._get('decisions',decision['id'])['status']=='pending'
        stage='context'
        write('POST','/v1/decisions/'+decision['id']+'/reply',dict(text='What exactly will this do?',revision=decision['revision']))
        state=wait(lambda s:any(m.get('decision_id')==decision['id'] and m['kind']=='text' and m['role']=='assistant' for m in s['messages']))
        assert service._get('decisions',decision['id'])['status']=='pending' and not file.exists()
        report['context_preserves_pending_decision']=True
        stage='approve-and-resume'
        option=next(o for o in decision['options'] if o['label']=='Save it')
        write('POST','/v1/decisions/'+decision['id']+'/answer',dict(option_id=option['id'],revision=decision['revision']))
        state=wait(lambda s:any(t['id']==child_id and t['status']=='completed' for t in s['tasks']))
        assert file.read_text()=='native relay verified'
        signature=(file.stat().st_ino,file.stat().st_mtime_ns)
        state=wait(lambda s:not any(t['status'] in ('working','queued') for t in s['tasks']))
        identities=[m['id'] for m in state['messages']]
        report['approval_resumes_native_conversation']=True
        stage='restart-after-result'
        service.close(); service=None
        service=Service(data,provider())
        time.sleep(2)
        assert file.read_text()=='native relay verified'
        assert (file.stat().st_ino,file.stat().st_mtime_ns)==signature
        assert [m['id'] for m in service.snapshot()['messages']]==identities
        assert len([r for r in service._all('deliveries') if r['source_task_id']==parent['id']])==1
        report['one_file_execution_and_stable_history']=True
        report['passed']=True
except BaseException as error:
    report.update(stage=stage,error_type=type(error).__name__,error=str(error)[:500])
finally:
    signal.alarm(0)
    if busy.locked(): busy.release()
    if service is not None: service.close()
    shutil.rmtree(data)
    report['scratch_removed']=not data.exists()
print('COCONUT_CREATION_RESULT='+json.dumps(report))
'''

if __name__ == '__main__':
    main(PROGRAM, 'mac-mini-native-relay.json')
