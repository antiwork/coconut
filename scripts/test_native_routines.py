"""Check routine ownership and saved routes against real Hermes without model calls."""
from test_hermes_live import main


PROGRAM = r'''
from pathlib import Path
import json, os, subprocess, sys, tempfile
from uuid import uuid4
root, code = map(Path, sys.argv[1:3])
sys.path[:0] = [str(code/'backend'), str(code/'backend/coconut'), str(root/'.runtime/hermes/source')]
from coconut.model_catalog import HermesModelCatalog
from coconut.providers import HermesProvider
from coconut.native_routine_execution import job_context

report = dict(check='native-routine-ownership-and-route', passed=False, model_requests=0)
try:
    with tempfile.TemporaryDirectory(prefix='routine-ownership-', dir=root/'.runtime/checks') as folder:
        data = Path(folder)
        bot = str(uuid4())
        home = data/'bots'/bot/'home'
        home.mkdir(parents=True)
        (home/'config.yaml').write_text('model:\n  default: fixture-model\n  provider: openrouter\n')
        os.environ['HERMES_HOME'] = str(home)
        catalog = HermesModelCatalog(data, python=sys.executable, source=root/'.runtime/hermes/source')
        def call(operation, **payload):
            return catalog._call('native', native_operation=operation, homes={bot:str(home)}, bot_id=bot, **payload)
        routine = call('routine_create', request_key='check', changes=dict(
            prompt='Return 2.', cron='every 30m', name='Ownership check', enabled=True,
            model='fixture-model', provider='openrouter'))
        identity = routine['_native_id']
        job = call('routine_claim', native_id=identity, manual=True)['job']
        assert job, 'Coconut did not claim the routine.'
        owner = job['fire_claim']['by']
        env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=str(root/'.runtime/hermes/source'))
        rival_owner = 'rival-' + str(uuid4())
        assert not owner.startswith(rival_owner + ':')
        env['HERMES_MACHINE_ID'] = rival_owner
        rival = subprocess.run([sys.executable, '-c',
            'import json,sys;from cron import jobs;print(json.dumps(bool(jobs.claim_job_for_fire(sys.argv[1],force=True,return_job=True))))',
            identity], env=env, text=True, capture_output=True, check=True, timeout=30)
        assert json.loads(rival.stdout) is False, 'A second process reclaimed a running Coconut job.'
        assert call('routine_heartbeat', native_id=identity, owner=owner)['ok']
        report['claim_survives_helper_exit'] = True
        report['concurrent_claim_rejected'] = True

        # Exercise the installed scheduler with a saved route and a changed default.
        saved = dict(job, model=None, provider=None, model_snapshot='fixture-model', provider_snapshot='openrouter')
        provider = HermesProvider(data, catalog=catalog)
        provider.resolve_model = lambda _: dict(model='changed-default', provider='anthropic')
        route = provider.resolve_task_model({}, {'_native_job': saved})
        assert route['model'] == 'fixture-model' and route['provider'] == 'openrouter'
        request = dict(task={'id':str(uuid4()), '_native_job':saved}, **route)
        with job_context(request, {'provider':'openrouter'}, {}) as prompt:
            assert 'Return 2.' in prompt
        request['model'] = 'changed-default'
        try:
            with job_context(request, {'provider':'openrouter'}, {}):
                raise AssertionError('A changed route reached execution.')
        except ValueError:
            pass
        report['saved_route_preserved'] = True
        report['changed_route_rejected'] = True
        for _ in range(2):
            assert call('routine_finish', native_id=identity, owner=owner, success=True, output='2')['ok']
        report['finish_retry'] = True
    report.update(passed=True, scratch_removed=not data.exists())
except Exception as error:
    report.update(error_type=type(error).__name__, error=str(error)[:500])
print('COCONUT_CREATION_RESULT=' + json.dumps(report))
'''


if __name__ == '__main__':
    main(PROGRAM, 'mac-mini-native-routines.json')
