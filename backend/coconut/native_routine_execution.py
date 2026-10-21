"""Use native Hermes job context inside Coconut's durable worker."""
from contextlib import contextmanager
from importlib import import_module
import json
import os
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


def finished_occurrence(task, job):
    """The service's proof that this task's own occurrence finished. The finished flag alone is not proof."""
    owner = (job.get('fire_claim') or {}).get('by')
    proof = task.get('_native_job_completion') or {}
    if (not task.get('_native_job_finished') or not owner or not job.get('id')
            or proof.get('job_id') != job['id'] or proof.get('owner') != owner):
        return False
    continuation = proof.get('continuation')
    if continuation is not None:
        # A failed occurrence continues only through its authenticated delivery to this exact turn.
        return (isinstance(continuation, dict)
                and all(isinstance(continuation.get(key), str) and continuation[key]
                        for key in ('delivery_id', 'task_id', 'turn_id', 'native_task_id'))
                and continuation['task_id'] == task['id']
                and continuation.get('turn_id') == task.get('_native_turn_id')
                and continuation.get('native_task_id') == task.get('_native_outcome_id'))
    return proof.get('success') is True


def retired_checkout(workdir):
    """Return the saved binding when `workdir` is exactly the absent path of a worktree that Coconut retired, else None.

    Retirement records the binding before it removes the checkout, so the registry is the proof. A missing directory
    that no binding names, a binding that is not retired, and a path that holds anything stay unexplained.
    """
    root = os.environ.get('COCONUT_WORKSPACE_ROOT')
    if not root or not workdir or os.path.lexists(workdir):
        return None
    try:
        saved = json.loads((Path(root) / 'worktrees.json').read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict):
        return None
    # Name the saved path exactly, as the handoff does. A link or another spelling of the path does not count.
    found = [binding for binding in saved.values() if isinstance(binding, dict) and binding.get('path') == workdir
             and isinstance(binding.get('retired'), dict) and isinstance(binding.get('outcome'), str) and binding['outcome']]
    return found[0] if len(found) == 1 else None


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
            # New Hermes releases removed drift snapshots. Retain saved Coconut
            # routes without depending on that removed scheduler helper.
            expected = job.get(axis)
            if not expected:
                expected = (scheduler._snapshot_pin(job, axis, current, job['id'])
                            if hasattr(scheduler, '_snapshot_pin') else job.get(axis + '_snapshot'))
            expected = expected or current
            if expected != current:
                raise ValueError('The scheduled ' + axis + ' differs from its saved route.')
    _guard_job_credential_exfil(job)
    prompt = _build_job_prompt(job)
    workdir = job.get('workdir') or ''
    if workdir and not Path(workdir).is_dir():
        workspace = request.get('workspace') or ''
        workspace = workspace if workspace and Path(workspace).is_dir() else ''
        if not finished_occurrence(request['task'], job):
            # A job often names the worktree of the outcome that scheduled it. Coconut retires that worktree while
            # the outcome is idle, and only a turn of that outcome restores it. This occurrence works in its own
            # execution workspace and leaves the retired path and its binding unchanged. A completion proof that
            # names another occurrence, owner, or card still refuses, as before.
            binding = None if request['task'].get('_native_job_completion') else retired_checkout(workdir)
            if not binding or not workspace:
                raise ValueError('The scheduled working directory does not exist.')
            prompt += (f'\n\nCoconut retired the scheduled working directory {workdir}. It is the worktree of '
                       f'outcome {binding["outcome"]}, and a turn of that outcome restores it. This run uses its own '
                       f'execution workspace {workspace}. Do not recreate the retired path.')
            if binding['retired'].get('restore_failed'):
                # The failure mark fences the path. Bot maintenance recovers it from the saved archive and branch.
                prompt += (' Its earlier restore failed, so the path stays fenced: do not restore it here. Report the '
                           'failure on that outcome for bot maintenance. The archive '
                           f'{binding["retired"].get("archive") or "(none)"} and the branch {binding.get("branch") or "(unknown)"} '
                           'keep its work.')
        # The scheduled run is over and its directory can be retired. Its continuation works in
        # this task's own execution workspace. The bridge still confirms the claim before each tool.
        workdir = workspace
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


def run_script_job(job, cancel_event, emit):
    """Use Hermes execution and silence rules without constructing an agent."""
    from native_routines import unsupported
    error = unsupported(job)
    if error or job.get('no_agent') is not True:
        raise ValueError(error or 'This routine requires a model.')
    from cron.scheduler import run_job, _is_cron_silence_response
    success, document, response, failure = run_job(job, cancel_event=cancel_event)
    silent = _is_cron_silence_response(response or '')
    # Non-BMP characters use up to twelve JSON characters; leave frame headroom
    # without making the service persist the accumulated message too often.
    chunk_size = 128_000
    if response and not silent:
        for start in range(0, len(response), chunk_size):
            emit('text_delta', {'text': response[start:start + chunk_size]})
    detail = failure or response or 'No error details returned.'
    if len(detail) > 16_000:
        detail = detail[:16_000] + '\n[Error details truncated.]'
    emit('completed' if success else 'failed', {
        'summary': 'Complete.' if success else 'The routine script failed: ' + detail,
        'routine_silent': silent, 'silent_response': silent,
    })
    return 0 if success else 1
