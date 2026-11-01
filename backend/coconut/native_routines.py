"""Project native Hermes cron jobs into the phone API."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid5


@contextmanager
def profile(home):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def unsupported(job, config=None):
    script_only = job.get('no_agent') is True and bool(job.get('script'))
    fields = ('monitor_script', 'monitor_url') if script_only else ('script', 'no_agent', 'monitor_script', 'monitor_url', 'base_url', 'enabled_toolsets')
    present = [field for field in fields if job.get(field)]
    cron_config = (config or {}).get('cron') or {}
    if not script_only:
        present.extend('cron.' + field for field in ('model', 'model_provider') if cron_config.get(field))
    if job.get('deliver') not in (None, '', 'local', 'origin'):
        present.append('deliver')
    return 'Run this routine through Hermes: ' + ', '.join(present) + '.' if present else None


def view(bot_id, job):
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly()
    schedule = job.get('schedule') or {}
    identity = str(uuid5(UUID(bot_id), 'cron:' + job['id']))
    # A one-time job runs once at run_at. Hermes keeps it for some days in state 'completed'. The phone uses
    # one_time, state, and run_at to show only the one-time jobs that still run. Old phones ignore these fields.
    one_time = schedule.get('kind') == 'once'
    return dict(id=identity, bot_id=bot_id, name=job.get('name') or 'Routine', prompt=job.get('prompt') or '',
                script=job.get('script') if job.get('no_agent') else None,
                cron=schedule.get('expr') or schedule.get('display') or '',
                timezone=config.get('timezone') or 'UTC',
                enabled=job.get('enabled', True) and not job.get('paused_at'),
                next_run_at=job.get('next_run_at'), last_run_at=job.get('last_run_at'), last_task_id=None,
                created_at=job.get('created_at') or '', updated_at=job.get('updated_at') or job.get('created_at') or '',
                _native_id=job['id'], _job=job, error=unsupported(job, config), one_time=one_time, state=job.get('state'),
                run_at=schedule.get('run_at') if one_time else None)


def worker(operation, request, homes):
    from cron import jobs
    if operation == 'routines_read':
        result = []
        for bot_id, home in homes.items():
            if not Path(home).is_dir():
                # A missing profile folder has no routines. Hermes would create its cron folder on a read.
                continue
            with profile(home):
                result.extend(view(bot_id, job) for job in jobs.list_jobs(include_disabled=True))
        return {'routines': result}
    bot_id = request['bot_id']
    with profile(homes[bot_id]):
        if operation == 'routine_create':
            data = request['changes']
            source = request['request_key']
            previous = next((j for j in jobs.list_jobs(include_disabled=True) if (j.get('origin') or {}).get('coconut_request') == source), None)
            if previous:
                return view(bot_id, previous)
            job = jobs.create_job(data['prompt'], data['cron'], name=data['name'], deliver='local',
                                  model=data.get('model'), provider=data.get('provider'),
                                  reasoning_effort=data.get('reasoning_effort'),
                                  origin={'platform': 'coconut', 'chat_id': bot_id, 'coconut_request': source},
                                  paused=not data.get('enabled', True))
            return view(bot_id, job)
        identity = request['native_id']
        if operation == 'routine_update':
            changes = request['changes']
            updates = {key: changes[key] for key in ('name', 'prompt') if key in changes}
            if 'cron' in changes:
                updates['schedule'] = jobs.parse_schedule(changes['cron'])
            if updates:
                jobs.update_job(identity, updates)
            if 'enabled' in changes:
                (jobs.resume_job if changes['enabled'] else jobs.pause_job)(identity)
            job = next(j for j in jobs.list_jobs(include_disabled=True) if j['id'] == identity)
            return view(bot_id, job)
        if operation == 'routine_delete':
            jobs.remove_job(identity)
            return {'ok': True}
        if operation == 'routine_trigger':
            # Hermes runs the job at the next tick of the gateway that serves the profile, as `hermes cron run` asks.
            job = jobs.trigger_job(identity)
            if job is None:
                raise ValueError('Hermes does not have this routine.')
            return view(bot_id, job)
    raise ValueError('Unknown routine operation.')
