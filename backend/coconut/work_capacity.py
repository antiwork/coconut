"""Classify Coconut turns for dispatch: the person's turns go first, and one execution runs one turn at a time.

Hermes has no person or chat turn, so these rules stay in Coconut. Hermes config holds the limits (work_limits).
"""


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
    waits on them. An answer of the person to a card (`_resume_decision`) is a person's turn, also when a bot
    exchange asked the card: it keeps the task and its reply route. A person's turn goes first in dispatch and in
    capacity (native_outcomes._dispatch_chat).
    """
    return chat_turn(task) and not bot_input_turn(task)


def bot_input_turn(task):
    """Whether no person waits on a turn: its input is a bot exchange, the return of delegated work, or the result of a
    background process, and not an answer of the person to a card. Such a turn may end in silence (gateway_chat)."""
    return not task.get('_resume_decision') and bool(
        task.get('source_task_id') or task.get('_bot_messages') or task.get('_is_return')
        or task.get('_background_completion'))
