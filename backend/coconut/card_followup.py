"""Text that a gateway turn writes after its card. Callers hold the service transaction.

A card of coconut_ask ends the turn (card_tools.PAUSED), and the task waits for the answer. Hermes can still write
text after the card. Mostly it only points to the card ("I asked you on a card."), and that stays out of the chat. But
a bot can also answer the person after its card, and that answer was lost. So the text is kept while the turn ends,
and a longer text shows as a message when the turn finishes.
"""
from .silent_responses import is_silent_response

TEXT_KINDS = ('text_delta', 'interim_message', 'interim_seal')
# Longer than a pointer to the card.
MIN_ANSWER = 160


def add(task, kind, payload):
    """Keep the text of an event that comes after the card."""
    text = str(payload.get('text', payload.get('delta', '')) or '')
    if kind == 'text_delta' and payload.get('replace') is True:
        # A replacement replaces the text of the current segment, which follows the earlier complete segments.
        task['_after_card'] = task.get('_after_card_sealed', '') + text
    elif kind == 'interim_seal':
        task['_after_card_sealed'] = task.get('_after_card', '').rstrip() + '\n\n'
        task['_after_card'] = task['_after_card_sealed']
    elif kind == 'interim_message':
        task['_after_card'] = task.get('_after_card', '').rstrip() + '\n\n' + text
        task['_after_card_sealed'] = task['_after_card'] + '\n\n'
    else:
        task['_after_card'] = task.get('_after_card', '') + text


def show(service, task):
    """At the end of the turn, show the text after the card when it is an answer, not a pointer to the card."""
    text = task.pop('_after_card', '').strip()
    task.pop('_after_card_sealed', None)
    if len(text) > MIN_ANSWER and not is_silent_response(text):
        service._message(task['bot_id'], text, task_id=task['id'])
