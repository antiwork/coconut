"""The saved state of a routine run that an earlier Coconut claimed in Hermes itself.

Coconut no longer claims routines: the Hermes gateway fires each one. Tasks of earlier claims stay in the store with
their native job (`_native_job`), and the continuation guards still read them. This helper saves that state as the
claim saved it: the routine task, its job and its routine.triggered event.
"""


def claimed(service, routine, job):
    """Save the task of a claimed run of `routine` with the native `job`. The caller holds the transaction."""
    bot = service._get('bots', routine['bot_id'])
    task = service._send(bot, {'text': routine['name'], 'mode': 'queue'}, record_message=False, human_authored=False)
    task = service._get('tasks', task['id'])
    task.update(routine_id=routine['id'], _native_job=job)
    service._put('tasks', task)
    service._put('routines', dict(routine, last_task_id=task['id'], next_run_at=job.get('next_run_at')))
    service._event('routine.triggered', {'routine_id': routine['id'], 'task_id': task['id']}, bot['id'], task['id'])
    return service._public(task)
