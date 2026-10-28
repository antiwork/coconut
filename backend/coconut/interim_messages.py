"""Store complete native messages while their turn continues. Callers hold the service transaction."""
from .silent_responses import is_silent_response
from .turn_timings import TurnTrace


def store(service, task, text):
    if task.get('_native_job') or not isinstance(text, str) or not text.strip() or is_silent_response(text):
        return
    message = service._message(task['bot_id'], text, task_id=task['id'], interim=True)
    TurnTrace(task, 'delivery').mark('reply_created', message_id=message['id'])


def seal(service, task):
    """Keep the streaming reply of this turn as a complete interim message.

    The Hermes gateway sends a reply in segments (gateway_bridge.TurnEvents). When a new segment starts, the earlier one
    is complete. Without a push: the phone notifies only for the final reply, as for the worker's interim messages.
    """
    message = next((m for m in reversed(service._task_messages(task['id'])) if m['streaming']), None)
    if message is None:
        return
    message.update(streaming=False, _interim=True)
    service._put('messages', message)
    service._event('message.updated', service._public(message), message['bot_id'], task['id'])


def confirm(service, task, text):
    """Confirm a final answer that Codex already sent through its interim callback."""
    last = next((m for m in reversed(service._task_messages(task['id']))
                 if m['role'] == 'assistant' and m['kind'] == 'text'
                 and not m.get('_hidden') and not m.get('_deleted')
                 and m['created_at'] >= task.get('_turn_started_at', task['created_at'])), None)
    if not last or not last.get('_interim') or ' '.join(last['text'].split()) != ' '.join(text.split()):
        return False
    last.update(text=text, streaming=False)
    last.pop('_interim')
    service._put('messages', last)
    service._event('message.updated', service._public(last), last['bot_id'], task['id'])
    service.push.record('message.completed', last, last['bot_id'], task['id'])
    bot = service._get('bots', last['bot_id'])
    bot['last_message'] = text[:160]
    service._put('bots', bot)
    service._put('tasks', task)
    service._refresh_bot(task['bot_id'])
    return True
