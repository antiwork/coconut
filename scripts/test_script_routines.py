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
            if name == 'failure':
                assert 'code 7' in output and 'code 7' in payload['summary'], events
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
            from coconut.providers import FixtureProvider
            from coconut.service import Service
            from coconut.worker_protocol import event_frame, MAX_EVENT_CHARACTERS
            service = Service(home/'service', FixtureProvider(home/'service'), start_workers=False)
            bot = service.mutate('POST', '/v1/bots', {'name': 'Script check'}, str(uuid4()))
            try:
                for name, output, failed in [('large', 'x' * 2_100_000, False),
                                             ('escaped', '\x00😀"\\' * 400_000, False),
                                             ('error', '\x00😀"\\' * 400_000, True)]:
                    (scripts/(name+'.py')).write_text('import sys\nsys.stdout.write(' + repr(output) + ')\n' +
                                                     ('raise SystemExit(7)\n' if failed else ''))
                    task = service.mutate('POST', '/v1/bots/' + bot['id'] + '/messages',
                                          {'text': name}, str(uuid4()))
                    with service.transaction():
                        saved = service._get('tasks', task['id'])
                        saved['status'] = 'working'
                        service._put('tasks', saved)
                    events = []
                    def receive(kind, payload):
                        assert len(event_frame(kind, payload)) < MAX_EVENT_CHARACTERS
                        events.append((kind, payload))
                        return service.emit(task['id'], kind, payload)
                    provider._run_turn('start', dict(task, _native_job=dict(
                        id=name, name=name, no_agent=True, script=name+'.py')),
                        dict(bot, session_id=str(uuid4()), _model_error='No model available'), [], receive, isolated=True)
                    terminal = [(kind,payload) for kind,payload in events if kind in ('completed','failed')]
                    assert len(terminal) == 1 and terminal[0][0] == ('failed' if failed else 'completed'), terminal
                    text = ''.join(p['text'] for k,p in events if k == 'text_delta')
                    assert output in text
                    saved = service._get('tasks', task['id'])
                    assert saved['status'] == ('failed' if failed else 'completed'), saved['status']
                    if not failed:
                        stored = [m for m in service._task_messages(task['id'])
                                  if m.get('role') == 'assistant' and not m.get('_hidden')]
                        assert any(m['text'] == output for m in stored), 'Complete successful output was not persisted'
                        assert len([1 for k,p in events if k == 'text_delta']) <= 20, 'Excessive persistence updates'
                    if failed:
                        assert 'code 7' in saved['summary'], saved['summary']
                        assert '[Error details truncated.]' in terminal[0][1]['summary']
                        assert len(terminal[0][1]['summary']) < 17_000
                report['large_escaped_output_and_durable_terminal_outcomes'] = True
            finally:
                service.close()
        finally:
            provider.close()
    report.update(passed=True, scratch_removed=not home.exists())
except Exception as error:
    report.update(error_type=type(error).__name__, error=str(error)[:1000])
print('COCONUT_CREATION_RESULT=' + json.dumps(report))
'''

if __name__ == '__main__':
    import os, sys, tempfile, subprocess, json
    from pathlib import Path
    if '--local' in sys.argv:
        with tempfile.TemporaryDirectory(prefix='native-script-check-') as folder:
            root = Path(folder)
            (root/'.runtime/hermes').mkdir(parents=True)
            (root/'.runtime/checks').mkdir()
            (root/'.runtime/hermes/source').symlink_to(Path(os.environ['COCONUT_HERMES_ROOT']).resolve())
            result = subprocess.run([os.environ['COCONUT_HERMES_PYTHON'], '-c', PROGRAM, str(root),
                                     str(Path(__file__).resolve().parents[1])], capture_output=True, text=True, timeout=180)
            print(result.stdout)
            reports = [line.split('=', 1)[1] for line in result.stdout.splitlines() if line.startswith('COCONUT_CREATION_RESULT=')]
            if result.returncode or len(reports) != 1 or not json.loads(reports[0])['passed']:
                print(result.stderr, file=sys.stderr)
                raise SystemExit(1)
    else:
        main(PROGRAM, 'mac-mini-script-routines.json')
