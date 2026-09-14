"""Use native Hermes job context inside Coconut's durable worker."""
from contextlib import contextmanager
from importlib import import_module
import json
from pathlib import Path


def create_native_job(arguments, task_id):
    from tools.cronjob_tools import cronjob, _HANDLER_FORWARDED_ARGS, _split_monitor_arg
    monitor_script, monitor_url = _split_monitor_arg(arguments.get('monitor'), arguments.get('monitor_script'), arguments.get('monitor_url'))
    # The model-facing native handler omits route pins; its programmatic API accepts them.
    return cronjob(action='create', task_id=task_id, session_id=task_id,
                   include_disabled=arguments.get('include_disabled', True), paused=arguments.get('paused', False),
                   monitor_script=monitor_script, monitor_url=monitor_url,
                   **{key: arguments.get(key) for key in (*_HANDLER_FORWARDED_ARGS, 'model', 'provider', 'base_url', 'reasoning_effort')})


def scheduler_result(result, status):
    from cron import jobs
    from native_routines import unsupported
    from native_routine_receipts import issue
    from hermes_constants import get_hermes_home
    from hermes_cli.config import load_config_readonly
    try:
        value = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return result
    if not isinstance(value, dict) or value.get('success') is not True:
        return result
    value = dict(value)
    if 'gateway_running' in value:
        value.pop('gateway_running')
        value.pop('warning', None)
    running = status.get('scheduler') == 'coconut' and status.get('scheduler_running') is True
    value.update(scheduler='coconut', scheduler_running=running)
    config = load_config_readonly()
    rows = value.get('jobs', []) + ([value['job']] if isinstance(value.get('job'), dict) else [])
    for row in rows:
        job = jobs.get_job(row.get('job_id') or row.get('id'))
        error = (unsupported(job, config) or issue(get_hermes_home(), job)) if job else 'The saved job could not be found.'
        row['scheduler_error'] = error
        row['scheduler_active'] = bool(running and not error and job and job.get('enabled', True) and not job.get('paused_at'))
    if not running:
        value['warning'] = 'Coconut has not confirmed a healthy scheduler tick. The saved jobs remain available; check Coconut before claiming they are active.'
    value['scheduler_note'] = 'Coconut runs supported jobs in this bot. No standalone Hermes gateway is required. Check each job\'s scheduler_active and scheduler_error.'
    return json.dumps(value) if isinstance(result, str) else value


@contextmanager
def job_context(request, runtime, config):
    job = request['task'].get('_native_job')
    if not job:
        yield ''
        return
    from native_routines import unsupported
    error = unsupported(job, config)
    if error:
        raise ValueError(error)
    scheduler = import_module('cron.scheduler')
    from cron.scheduler_prompt import _build_job_prompt, _guard_job_credential_exfil
    from gateway.session_context import set_session_vars, clear_session_vars
    from tools.terminal_tool import record_session_cwd, clear_session_cwd
    if hasattr(scheduler, '_check_model_drift'):
        # Keep the installed runtime usable while the release update is staged.
        scheduler._check_model_drift(job, job['id'], config, runtime, runtime.get('provider'), request['model'])
    else:
        for axis, current in (('model', request['model']), ('provider', request.get('provider') or runtime.get('provider'))):
            expected = job.get(axis) or scheduler._snapshot_pin(job, axis, current, job['id']) or current
            if expected != current:
                raise ValueError('The scheduled ' + axis + ' differs from its saved route.')
    _guard_job_credential_exfil(job)
    prompt = _build_job_prompt(job)
    workdir = job.get('workdir') or ''
    if workdir and not Path(workdir).is_dir():
        raise ValueError('The scheduled working directory does not exist.')
    task_id = request['task']['id']
    # Coconut can pause scheduled turns for approvals and resume them.
    tokens = set_session_vars(cwd=workdir, cron_session=False, session_key=task_id, platform='coconut')
    try:
        if workdir:
            record_session_cwd(task_id, workdir)
        yield prompt
    finally:
        clear_session_cwd(task_id)
        clear_session_vars(tokens)
