"""Bound Coconut work independently of native Hermes delegated helpers."""
from .parallel_work import automatic_limit, hardware, validate_limit


def state(limit=None):
    validate_limit(limit)
    automatic = automatic_limit(*hardware())
    return dict(limit=limit, automatic_limit=automatic, effective_limit=limit or automatic)


def execution_key(task):
    if task.get('_work_status_only'):
        return task['id']
    return task.get('_execution_session_id') or task['bot_id']
