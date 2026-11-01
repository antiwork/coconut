"""The text of a turn as the gateway gets it, for the tests that read the context of a turn.

run_gateway_chat, run_gateway_resume and run_gateway_context build the same text from the same parts. The cards that
wait for the person now (GatewayChat.cards_note) come from the store, so this text leaves them out. Hermes keeps the
conversation of a chat, so the turn sends no history. The text has every record of the turn, as the first turn of a
Hermes session gets them (native_conversation.turn_records). `turn_input` also returns the visible history of the request for
the tests that check it.
"""
from coconut.gateway_chat import gateway_turn_text, work_notes
from coconut.native_conversation import (attachment_context, context_prompt, message_content, records_text,
                                         resume_prompt, turn_records, visible_history)


def turn_input(request):
    """Return the text of the turn and the visible messages before it, as (text, history)."""
    rows = visible_history(request.get('history', []))
    history = [{'role': message['role'], 'content': message_content(message)} for message in rows]
    operation = request['operation']
    task = request['task']
    if operation == 'context':
        text = context_prompt(request, rows) + attachment_context(task.get('attachments', []))
    elif operation == 'resume':
        text = resume_prompt(request) + work_notes(request) + records_text(turn_records(task))
    else:
        text = gateway_turn_text(request)
        if text is None:
            text = task.get('title', '') + work_notes(request)
        # The first turn of a Hermes session gets every record of the turn (hooks.add_records).
        text += records_text(turn_records(task))
    return text, history
