#!/usr/bin/env python3
"""Verify native Hermes profiles and restart recovery on the Mac mini."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import remote

PROGRAMS = (
r"""from pathlib import Path
import sys,os,tempfile,json,uuid
root=Path(sys.argv[1]).resolve();sys.path[:0]=[str(root/'backend'),str(root/'backend/coconut'),str(root/'.runtime/hermes/source')]
with tempfile.TemporaryDirectory(prefix='coconut-native-check-') as temporary:
 home=Path(temporary);os.environ['HERMES_HOME']=str(home/'default');(home/'default').mkdir()
 (home/'default/config.yaml').write_text('{}')
 from coconut.profiles import HermesProfiles
 from coconut.native_settings import worker
 bot=dict(id=str(uuid.uuid4()),session_id=str(uuid.uuid4()),name='Native check',role='Checks',instructions='Keep it brief',memory='Retain this native memory.',preferences=[{'text':'Always offer do nothing.'}])
 ph=home/'profiles'/'native-check';ph.mkdir(parents=True);(ph/'SOUL.md').write_text(bot['instructions'])
 profiles=HermesProfiles(home,{bot['id']:dict(bot_id=bot['id'],name='native-check',home=str(ph),state='active')})
 assert profiles.sync(bot)['status']=='active'
 request=dict(data_root=str(home),homes={bot['id']:str(ph)})
 def call(op,**kwargs):return worker(dict(request,native_operation=op,**kwargs),home/'default')
 state=call('read')
 assert state['profiles'][bot['id']]['approval_override'] is None
 state=call('update_profile',bot_id=bot['id'],changes={'name':'Changed','role':'','approval_override':'manual'})
 assert state['name']=='Changed' and state['effective_approval']=='manual',state
 call('set_default_approval',mode='smart')
 state=call('update_profile',bot_id=bot['id'],changes={'approval_override':None})
 assert state['effective_approval']=='smart',state
 r=call('routine_create',bot_id=bot['id'],request_key='one',changes={'name':'Check','prompt':'Return 2','cron':'every 10m','enabled':False})
 r2=call('routine_create',bot_id=bot['id'],request_key='one',changes={'name':'Check','prompt':'Return 2','cron':'every 10m','enabled':False})
 assert r['id']==r2['id'] and not r['enabled'],(r,r2)
 rows=call('routines_read')['routines'];assert len(rows)==1,rows
 call('routine_update',bot_id=bot['id'],native_id=r['_native_id'],changes={'enabled':True})
 claim=call('routine_claim',bot_id=bot['id'],native_id=r['_native_id'],manual=True)['job'];assert claim
 call('routine_finish',bot_id=bot['id'],native_id=r['_native_id'],owner=claim['fire_claim']['by'],success=True,output='2')
 call('routine_delete',bot_id=bot['id'],native_id=r['_native_id'])
 print('PASS: native profile metadata, memory import, approval inheritance, cron idempotency and execution receipts.')
""",
r"""from pathlib import Path
import sys,tempfile,time,uuid,json,threading
root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root/'backend'))
from coconut.providers import HermesProvider
from coconut.service import Service
with tempfile.TemporaryDirectory(prefix='coconut-native-restart-') as temporary:
 def start():
  p=HermesProvider(temporary,python=str(root/'.runtime/hermes/venv/bin/python'),hermes_root=root/'.runtime/hermes/source')
  return Service(temporary,p,start_workers=False)
 service=start()
 def write(method,path,data):return service.mutate(method,path,data,str(uuid.uuid4()))
 def drive(task_id):
  # Mirror dispatch ownership and continue queued turns without changing task state.
  for _ in range(8):
   task=service._get('tasks',task_id)
   if task['status']!='queued':return
   assert service._dispatch_eligible(task,time.time()),task
   service._ensure_execution_session(task)
   service._run_task(task_id)
  assert service._get('tasks',task_id)['status']!='queued','Continuation limit exceeded'
 try:
  bot=write('POST','/v1/bots',{'name':'Approval check','instructions':'Use the exact terminal command requested. Do not substitute tools. Reply briefly.'})
  write('PATCH',f"/v1/bots/{bot['id']}",{'approval_override':'manual'})
  workspace=Path(temporary)/'bots'/bot['id']/'workspace';workspace.mkdir(parents=True,exist_ok=True)
  (workspace/'approval-fixture').mkdir();(workspace/'approval-fixture/test.txt').write_text('Disposable verification file.')
  task=write('POST',f"/v1/bots/{bot['id']}/messages",{'text':'First run the exact terminal command printf first-step >> before-count.txt. Then run the exact terminal command rm -rf approval-fixture. These files are disposable test files in your current workspace. Report the result.','mode':'queue'})
  drive(task['id'])
  state=service.snapshot();decisions=[d for d in state['decisions'] if d['task_id']==task['id']]
  print('Before restart:',[(d['action_type'],d['status']) for d in decisions],flush=True)
  assert decisions and decisions[-1]['status']=='pending',[(t['status'],t['summary']) for t in state['tasks']]
  decision=decisions[-1]
  assert (workspace/'approval-fixture/test.txt').read_text()=='Disposable verification file.'
  assert (workspace/'before-count.txt').read_text()=='first-step'
  other=write('POST','/v1/bots',{'name':'Independent check'})
  second=write('POST',f"/v1/bots/{other['id']}/messages",{'text':'Reply with 2. Use only todo_list for required work bookkeeping; do not use other tools.','mode':'queue'})
  drive(second['id']);assert service._get('tasks',second['id'])['status']=='completed',service._get('tasks',second['id'])
  service.close();service=start()
  assert service._get('decisions',decision['id'])['status']=='pending'
  context=write('POST',f"/v1/decisions/{decision['id']}/reply",{'text':'Which disposable folder does this action remove? Do not run it.','revision':decision['revision']})
  context=service._all('contexts')[-1]
  service._start_context(context['id'])
  deadline=time.time()+90
  while service._get('contexts',context['id'])['status'] in ('queued','working') and time.time()<deadline:time.sleep(.2)
  print('Context:',service._get('contexts',context['id']),flush=True)
  assert service._get('contexts',context['id'])['status']=='completed'
  assert service._get('decisions',decision['id'])['status']=='pending'
  assert (workspace/'approval-fixture').exists()
  write('POST',f"/v1/decisions/{decision['id']}/answer",{'option_id':'once','revision':decision['revision']})
  drive(task['id'])
  print('After approval:',service._get('tasks',task['id'])['status'],service._get('decisions',decision['id'])['status'],flush=True)
  assert service._get('tasks',task['id'])['status']=='completed'
  assert not (workspace/'approval-fixture').exists()
  assert (workspace/'before-count.txt').read_text()=='first-step'
  service.close();service=start()
  assert service._get('decisions',decision['id'])['status']=='executed'
  assert service._get('tasks',second['id'])['status']=='completed'
  assert service._get('tasks',task['id'])['status']=='completed'
  assert not (workspace/'approval-fixture').exists()
  assert (workspace/'before-count.txt').read_text()=='first-step'
  print('PASS: native manual approval, independent bot, restart, context, approval, exact-once prior step, retained result.')
 finally:service.close()
""",
)


def main():
    config = remote.configuration()
    root = config['directory']
    # Find the installed Hermes and run the contract check against it. It sends no model requests.
    check = 'import sys; sys.path.insert(0, sys.argv[1] + "/scripts"); import host; m = host.hermes_runtime(); m.check(m.find())'
    remote.ssh(config, ['python3', '-c', check, root], timeout=420)
    for program in PROGRAMS:
        result = remote.ssh(config, [root + '/.runtime/hermes/venv/bin/python', '-c', program, root], timeout=360, allow_failure=True)
        print(result.stdout.decode(), flush=True)
        if result.returncode:
            print(result.stderr.decode(), file=sys.stderr)
            raise SystemExit(result.returncode)
    print('PASS: native profile and restart milestone.')


if __name__ == '__main__':
    main()
