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


def chat_turn(task):
    """Whether the chat dispatcher starts this task as a turn of the bot's conversation (`_chat_session`).

    A conversation turn that owns a native outcome is not one: its turn needs the claim of the outcome's card
    (service._run_task), so the Kanban scheduler dispatches it, as every turn of an outcome.
    """
    return bool(task.get('_chat_session')) and not task.get('_native_outcome_id')


def person_turn(task):
    """Whether a chat turn answers the person: a message, a reaction or a card answer of the person.

    A turn of a bot exchange (`source_task_id`, `_bot_messages`), the return of delegated work (`_is_return`) and the
    result of a background process (`_background_completion`) are turns of the bot's conversation too, but no person
    waits on them. A person's turn goes first in dispatch and in capacity (native_outcomes._dispatch_chat).
    """
    return (chat_turn(task) and not task.get('source_task_id') and not task.get('_bot_messages')
            and not task.get('_is_return') and not task.get('_background_completion'))
