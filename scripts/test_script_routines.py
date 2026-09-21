"""Run native script routines through Coconut without model access."""
from test_hermes_live import main

PROGRAM = r'''
from pathlib import Path
import os, sys, tempfile, json, threading
from uuid import uuid4
root, code = map(Path, sys.argv[1:3])
sys.path[:0] = [str(code/'backend'), str(code/'backend/coconut'), str(root/'.runtime/hermes/source')]
report = dict(check='native-script-routines', passed=False, model_requests=0)
try:
    with tempfile.TemporaryDirectory(prefix='script-routines-', dir=root/'.runtime/checks') as folder:
        home = Path(folder)
        os.environ['HERMES_HOME'] = str(home)
        (home/'config.yaml').write_text('model:\n  default: unavailable-model\n  provider: unavailable-provider\n')
        scripts = home/'scripts'
        scripts.mkdir()
        from cron import jobs
        from coconut.native_routines import view
        from coconut.native_routine_receipts import claim, finish
        from coconut import hermes_worker
        import native_fallback
        native_fallback.for_turn = lambda: (_ for _ in ()).throw(AssertionError('Script reached model setup'))
        cases = [('output', 'print("cleanup receipt")', True, False),
                 ('silent', 'pass', True, True),
                 ('gated', 'print(\'{"wakeAgent":false}\')', True, True),
                 ('failure', 'raise SystemExit(7)', False, False)]
        for name, source, success, silent in cases:
            (scripts/(name+'.py')).write_text(source)
            job = jobs.create_job('', 'every 1h', name=name, script=name+'.py', no_agent=True, deliver='local')
            projection = view(str(uuid4()), job)
            assert projection['error'] is None and projection['script'] == name+'.py', projection
            claimed = claim(home, job['id'], jobs, manual=True)['job']
            events = []
            task = {'id':str(uuid4()), 'title':name, '_native_job':claimed}
            result = hermes_worker.run_turn({'task':task, 'operation':'start'},
                lambda kind, payload: events.append((kind,payload)), lambda *args: None)
            assert result == (0 if success else 1), events
            kind, payload = events[-1]
            assert kind == ('completed' if success else 'failed'), events
            assert payload['routine_silent'] == silent, events
            output = '\n'.join(p['text'] for k,p in events if k == 'text_delta')
            if name == 'output': assert output == 'cleanup receipt', events
            if silent: assert not output, events
            if name == 'failure': assert 'code 7' in output, events
            assert finish(home, job['id'], jobs, owner=claimed['fire_claim']['by'], success=success,
                          output=output, failure=None if success else output)['ok']
            saved = jobs.get_job(job['id'])
            assert saved['last_run_at'] and saved['next_run_at'], saved
        report['stdout_silence_failure_and_native_completion'] = True
        from coconut.native_routine_execution import run_script_job
        (scripts/'cancel.py').write_text('import time; time.sleep(30)')
        cancel = threading.Event()
        cancel.set()
        events = []
        result = run_script_job(dict(id='cancel-check', name='cancel', no_agent=True, script='cancel.py'), cancel,
                                lambda kind,payload: events.append((kind,payload)))
        assert result == 1 and events[-1][0] == 'failed', events
        report['native_cancellation'] = True
        from coconut.providers import HermesProvider
        from unittest.mock import Mock
        catalog = Mock(root=home)
        catalog.resolve_runtime.side_effect = AssertionError('Script requested model credentials')
        provider = HermesProvider(home, python=sys.executable, hermes_root=root/'.runtime/hermes/source', catalog=catalog)
        provider.observe_bot = lambda bot: bot
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(code/'backend/coconut'), str(root/'.runtime/hermes/source'))))
        provider.environment = lambda bot_id: (env, str(home))
        events = []
        try:
            provider._run_turn('start', {'id':str(uuid4()), 'title':'output', '_native_job':dict(
                id='transport-check', name='output', no_agent=True, script='output.py')},
                {'id':str(uuid4()), 'session_id':str(uuid4()), '_model_error':'No model available'}, [],
                lambda kind,payload: events.append((kind,payload)), isolated=True)
            assert any(k == 'completed' for k,p in events), events
            assert any(k == 'text_delta' and p['text'] == 'cleanup receipt' for k,p in events), events
            catalog.resolve_runtime.assert_not_called()
            report['provider_transport_without_model_credentials'] = True
        finally:
            provider.close()
    report.update(passed=True, scratch_removed=not home.exists())
except Exception as error:
    report.update(error_type=type(error).__name__, error=str(error)[:1000])
print('COCONUT_CREATION_RESULT=' + json.dumps(report))
'''

if __name__ == '__main__':
    main(PROGRAM, 'mac-mini-script-routines.json')
