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
 profiles=HermesProfiles(home);profiles.sync(bot);profiles.sync(bot)
 ph=profiles.home(bot)
 assert (ph/'memories/USER.md').read_text().count('Always offer do nothing.')==1
 request=dict(data_root=str(home),homes={bot['id']:str(ph)})
 def call(op,**kwargs):return worker(dict(request,native_operation=op,**kwargs),home/'default')
 call('migrate')
 state=call('read');assert state['approvals']['default']=='off',state
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
 try:
  bot=write('POST','/v1/bots',{'name':'Approval check','instructions':'Use the exact terminal command requested. Do not substitute tools. Reply briefly.'})
  write('PATCH',f"/v1/bots/{bot['id']}",{'approval_override':'manual'})
  workspace=Path(temporary)/'bots'/bot['id']/'workspace';workspace.mkdir(parents=True,exist_ok=True)
  (workspace/'approval-fixture').mkdir();(workspace/'approval-fixture/test.txt').write_text('Disposable verification file.')
  task=write('POST',f"/v1/bots/{bot['id']}/messages",{'text':'First run the exact terminal command printf first-step >> before-count.txt. Then run the exact terminal command rm -rf approval-fixture. These files are disposable test files in your current workspace. Report the result.','mode':'queue'})
  service._run_task(task['id'])
  state=service.snapshot();decisions=[d for d in state['decisions'] if d['task_id']==task['id']]
  print('Before restart:',[(d['action_type'],d['status']) for d in decisions],flush=True)
  assert decisions and decisions[-1]['status']=='pending',[(t['status'],t['summary']) for t in state['tasks']]
  decision=decisions[-1]
  other=write('POST','/v1/bots',{'name':'Independent check'})
  second=write('POST',f"/v1/bots/{other['id']}/messages",{'text':'Reply with 2. Do not use tools.','mode':'queue'})
  service._run_task(second['id']);assert service._get('tasks',second['id'])['status']=='completed'
  service.close();service=start()
  assert service._get('decisions',decision['id'])['status']=='pending'
  context=write('POST',f"/v1/decisions/{decision['id']}/context",{'question':'Which disposable folder does this action remove? Do not run it.'})
  context=service._all('contexts')[-1]
  service._start_context(context['id'])
  deadline=time.time()+90
  while service._get('contexts',context['id'])['status'] in ('queued','working') and time.time()<deadline:time.sleep(.2)
  print('Context:',service._get('contexts',context['id']),flush=True)
  assert service._get('contexts',context['id'])['status']=='completed'
  assert service._get('decisions',decision['id'])['status']=='pending'
  assert (workspace/'approval-fixture').exists()
  write('POST',f"/v1/decisions/{decision['id']}/answer",{'option_id':'once'})
  service._run_task(task['id'])
  print('After approval:',service._get('tasks',task['id'])['status'],service._get('decisions',decision['id'])['status'],flush=True)
  assert not (workspace/'approval-fixture').exists()
  assert (workspace/'before-count.txt').read_text()=='first-step'
  service.close();service=start()
  assert service._get('decisions',decision['id'])['status']=='executed'
  assert service._get('tasks',second['id'])['status']=='completed'
  print('PASS: native manual approval, independent bot, restart, context, approval, exact-once prior step, retained result.')
 finally:service.close()
""",
)


PROGRAMS += (r"""
from pathlib import Path
import sys, tempfile, uuid
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / 'backend'))
from coconut.providers import HermesProvider
from coconut.service import Service
with tempfile.TemporaryDirectory(prefix='coconut-native-routine-') as temporary:
    def start():
        provider = HermesProvider(temporary, python=root/'.runtime/hermes/venv/bin/python', hermes_root=root/'.runtime/hermes/source')
        return Service(temporary, provider, start_workers=False)
    service = start()
    def write(method, path, data):
        return service.mutate(method, path, data, str(uuid.uuid4()))
    try:
        bot = write('POST', '/v1/bots', {'name': 'Routine check'})
        routine = write('POST', f"/v1/bots/{bot['id']}/routines", {'name': 'Native check', 'prompt': 'Use terminal to run exactly printf routine-once >> routine-count.txt, then reply Done. Do not use other tools.', 'cron': 'every 30m', 'enabled': True})
        task = write('POST', f"/v1/routines/{routine['id']}/run", {})
        service._run_task(task['id'])
        assert service._get('tasks', task['id'])['status'] == 'completed'
        service.tick_native_routines()
        assert len(service._all('tasks')) == 1
        assert service._get('tasks', task['id'])['_native_job_finished']
        service.close(); service = start()
        service.tick_native_routines()
        assert len(service._all('tasks')) == 1
        assert (Path(temporary)/'bots'/bot['id']/'workspace/routine-count.txt').read_text() == 'routine-once'
        native = service.provider.native_settings('routines_read', [bot])['routines'][0]
        assert native['_job']['last_status'] == 'ok', native['_job']['last_status']
        print('PASS: native routine tool execution, completion output, restart, and no immediate repeat.')
    finally:
        service.close()
""",)


def main():
    config = remote.configuration()
    root = config['directory']
    remote.ssh(config, ['python3', root + '/scripts/hermes.py', 'check'], timeout=120)
    for program in PROGRAMS:
        result = remote.ssh(config, [root + '/.runtime/hermes/venv/bin/python', '-c', program, root], timeout=360, allow_failure=True)
        print(result.stdout.decode(), flush=True)
        if result.returncode:
            print(result.stderr.decode(), file=sys.stderr)
            raise SystemExit(result.returncode)
    print('PASS: native profile and restart milestone.')


if __name__ == '__main__':
    main()
