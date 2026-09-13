"""Project native Hermes cron jobs into the phone API."""
from __future__ import annotations

from contextlib import contextmanager
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
    fields = ('script', 'no_agent', 'monitor_script', 'monitor_url', 'base_url', 'enabled_toolsets')
    present = [field for field in fields if job.get(field)]
    cron_config = (config or {}).get('cron') or {}
    present.extend('cron.' + field for field in ('model', 'model_provider') if cron_config.get(field))
    if job.get('deliver') not in (None, '', 'local', 'origin'):
        present.append('deliver')
    return 'Run this routine through Hermes: ' + ', '.join(present) + '.' if present else None


def view(bot_id, job):
    from hermes_cli.config import load_config_readonly
    from hermes_constants import get_hermes_home
    import native_routine_receipts as receipts
    home = get_hermes_home()
    config = load_config_readonly()
    receipt = receipts.read(home, job['id'])
    schedule = job.get('schedule') or {}
    identity = str(uuid5(UUID(bot_id), 'cron:' + job['id']))
    return dict(id=identity, bot_id=bot_id, name=job.get('name') or 'Routine', prompt=job.get('prompt') or '',
                cron=schedule.get('expr') or schedule.get('display') or '',
                timezone=config.get('timezone') or 'UTC',
                enabled=job.get('enabled', True) and not job.get('paused_at'),
                next_run_at=job.get('next_run_at'), last_run_at=job.get('last_run_at'), last_task_id=None,
                created_at=job.get('created_at') or '', updated_at=job.get('updated_at') or job.get('created_at') or '',
                _native_id=job['id'], _job=job, error=unsupported(job, config) or receipts.issue(home, job),
                _recover_claim=receipt.get('phase') == 'claimed')


def worker(operation, request, homes):
    from cron import jobs
    if operation == 'routines_read':
        result = []
        for bot_id, home in homes.items():
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
        if operation == 'routine_claim':
            job = jobs.get_job(identity)
            from hermes_cli.config import load_config_readonly
            error = unsupported(job, load_config_readonly()) if job else None
            if error:
                raise ValueError(error)
            from native_routine_receipts import claim
            return claim(homes[bot_id], identity, jobs, manual=request.get('manual', False))
        if operation == 'routine_heartbeat':
            return {'ok': jobs.heartbeat_fire_claim(identity, expected_owner=request['owner'])}
        if operation == 'routine_finish':
            from native_routine_receipts import finish
            return finish(homes[bot_id], identity, jobs, owner=request['owner'], success=request['success'],
                          output=request.get('output') or '', failure=request.get('failure'))
    raise ValueError('Unknown routine operation.')
