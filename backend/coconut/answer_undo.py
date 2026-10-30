"""Hold each answer to a card for a short Undo window before the bot gets it.

When the person answers a card (an option or a typed reply), the Mac keeps the answer for ANSWER_UNDO_SECONDS in
`_held_answer`. The stored card stays `pending`, so every part of the Mac that waits for the card still waits: the task
does not resume, no action runs, and no follow-up or merge treats the card as answered. The public card has the status
`held`, with `answer_undo_until` and `answer_undo_id`, so Inbox drops it and an app can offer Undo.

- Undo (POST /v1/decisions/<id>/inbox with action `undo_answer`) inside the window removes the held answer. The card
  keeps its revision, and the Mac records the undone answer.
- At the end of the window, finish_held_answers applies the answer as before. It also applies an answer that a restart
  interrupted. A later Undo gets 409.
- A change of the card in the window (a new revision, a cancelled task, another answer) wins. The held answer then
  does not apply, and the card stays as that change left it.

An app before this change shows the card as answered and has no Undo. The window is short, so its answer reaches the
bot after a short wait. An app that gets no `answer_undo_until` from an older Mac shows no Undo.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

ANSWER_UNDO_SECONDS = 10
HELD = 'held'
# The cards with a held answer. The partial index decisions_held holds them.
HELD_DECISIONS = "SELECT rowid, data FROM decisions WHERE json_extract(data,'$._held_answer') IS NOT NULL"
HOLD_FIELDS = dict(answer_undo_until=None, answer_undo_id=None, held_answer_label=None,
                   selected_option_id=None, selected_option_label=None)


def _deadline_passed(text, current=None):
    current = current or datetime.now(timezone.utc)
    return not text or datetime.fromisoformat(text.replace('Z', '+00:00')) <= current


HELD_CHANGE = ('The person answered this card. The answer waits for its Undo window, '
               'so this result does not change the card.')


def require_not_held(decision):
    """Refuse an answer, a revision or a resolution of a card while the Mac holds the person's answer: it wins."""
    if decision.get('_held_answer'):
        from .service import ServiceError
        raise ServiceError(409, HELD_CHANGE)


def public_status(decision):
    """The status that apps see. A held answer shows as `held`; the stored card stays `pending`."""
    return HELD if decision.get('_held_answer') and decision.get('status') == 'pending' else decision.get('status')


class AnswerUndo:
    # Tests of what an answer does set 0: the answer then applies at once, as before the Undo window.
    answer_undo_seconds = ANSWER_UNDO_SECONDS

    def apply_answer(self, decision, task, data, device, callbacks):
        if decision.get('_native'):
            return self.answer_native(decision, task, data, device, callbacks)
        return self._answer(decision, task, data, device)

    def check_answer(self, decision, task, data):
        """The checks of the answer handlers that have no side effect, so that a wrong answer fails at once."""
        from .service import ServiceError, decision_correction
        if decision.get('_source_recheck'):
            raise ServiceError(409, 'The source changed. Coconut is updating this decision.')
        if type(data.get('revision')) is not int or data['revision'] != decision['revision']:
            raise ServiceError(409, 'This decision changed. Refresh it before answering.')
        if data.get('option') is not None:
            raise ServiceError(400, 'Send the choice ID as option_id.')
        option_id = data.get('option_id')
        if option_id is not None and not isinstance(option_id, str):
            raise ServiceError(400, 'option_id must contain a choice ID.')
        options = decision.get('options') or []
        option = next((item for item in options if item['id'] == option_id), None)
        if option_id is not None and option is None:
            raise ServiceError(400, 'Choose a current option.')
        if option and data.get('choice') is not None and data['choice'] != option['answer']:
            raise ServiceError(400, 'The response does not match this option.')
        choice = option['answer'] if option else data.get('choice')
        if choice not in ('approve', 'reject', 'modify', 'reply'):
            raise ServiceError(400, 'Choose one of the current options.')
        correction = decision_correction(data)
        if choice in ('modify', 'reply') and not (correction.strip() or (option and option.get('modification'))):
            raise ServiceError(400, 'Enter a reply or choose an option.')
        if choice == 'approve' and not option and (decision.get('_native') or options):
            raise ServiceError(400, 'Choose one of the current options.')
        if self._task_paused(task) and (decision.get('_native') or choice == 'approve'):
            raise ServiceError(409, 'Restore the bot before answering.')
        if decision.get('previews') and choice == 'approve' and data.get('previews_hash') != decision.get('previews_hash'):
            raise ServiceError(409, 'Review the images in the latest app before choosing this option.')
        if decision.get('draft') is not None and choice == 'approve' and data.get('draft_hash') != decision.get('draft_hash'):
            raise ServiceError(409, 'Open the full draft in the latest app before selecting Send.')
        if decision.get('draft') is not None and choice == 'modify':
            raise ServiceError(400, 'Edit the draft before selecting Send.')
        if decision.get('_native') and choice == 'approve' and decision['action_type'] in ('question', 'draft_message'):
            self._require_current_decision_profile(decision)
        return option

    def hold_answer(self, decision, task, data, device, callbacks):
        """Keep the answer of POST /v1/decisions/<id>/answer for the Undo window. The caller holds the transaction.

        The full answer runs at the end of the window, with all its checks."""
        from .service import now, uid
        if self.answer_undo_seconds <= 0:
            return self.apply_answer(decision, task, data, device, callbacks)
        option = self.check_answer(decision, task, data)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=self.answer_undo_seconds)
        decision['_held_answer'] = dict(data=data, device=device, key=self._request_key, held_at=now())
        # An app before the window shows a card with an unknown status as answered, with the selected label.
        decision.update(answer_undo_until=deadline.isoformat().replace('+00:00', 'Z'), answer_undo_id=uid(),
                        held_answer_label=(option['label'] if option else str(data.get('correction') or data.get('choice')))[:200],
                        selected_option_id=option['id'] if option else None,
                        selected_option_label=option['label'] if option else None)
        self._put('decisions', decision)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        return self._public(decision)

    def undo_answer(self, decision, task, data, device):
        """Remove the held answer of a card, so it is pending again with the same revision. The caller holds the transaction."""
        from .service import ServiceError, now
        if (not decision.get('_held_answer') or decision['status'] != 'pending' or not data.get('undo_id')
                or data['undo_id'] != decision.get('answer_undo_id')):
            raise ServiceError(409, 'This answer is no longer waiting. The bot already has it.')
        if _deadline_passed(decision.get('answer_undo_until')):
            raise ServiceError(409, 'The Undo window ended. The bot already has your answer.')
        held = decision.pop('_held_answer')
        decision.setdefault('_undone_answers', []).append(dict(held, undone_at=now(), undone_by=device))
        decision.update(HOLD_FIELDS)
        self._put('decisions', decision)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        self._event('decision.answer_undone', dict(id=decision['id'], revision=decision['revision']),
                    task['bot_id'], task['id'])
        return self._public(decision)

    def finish_held_answers(self, current=None):
        """Apply each held answer whose Undo window ended, as the answer request did before this window existed."""
        from .service import ServiceError
        callbacks = []
        with self.transaction():
            held_cards = [json.loads(row[1]) for row in sorted(self.db.execute(HELD_DECISIONS).fetchall())]
            for decision in held_cards:
                if not _deadline_passed(decision.get('answer_undo_until'), current):
                    continue
                held = decision.pop('_held_answer')
                decision.update(HOLD_FIELDS)
                task = self._get('tasks', decision['task_id'])
                if decision['status'] != 'pending' or task['status'] == 'cancelled':
                    # The person closed the card in another way in the window, for example a cancelled task or an archived
                    # bot. That change wins, and the held answer is kept as superseded.
                    decision.setdefault('_superseded_answers', []).append(held)
                    self._put('decisions', decision)
                    self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
                    continue
                self._request_device_id, self._request_key = held.get('device'), held.get('key')
                self.db.execute('SAVEPOINT held_answer')
                try:
                    self.apply_answer(decision, task, held['data'], held['device'], callbacks)
                    self.db.execute('RELEASE held_answer')
                except ServiceError as error:
                    # The card changed in the window, for example a new revision. It stays in Inbox.
                    self.db.execute('ROLLBACK TO held_answer')
                    self.db.execute('RELEASE held_answer')
                    # The kept tasks can hold changes that the rollback removed.
                    self._forget_cached_tasks()
                    logging.getLogger(__name__).warning('A held answer to decision %s did not apply: %s', decision['id'], error)
                    decision = self._get('decisions', decision['id'])
                    decision.pop('_held_answer', None)
                    decision.update(HOLD_FIELDS)
                    self._put('decisions', decision)
                    self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
                finally:
                    self._request_device_id = self._request_key = None
        for callback in callbacks:
            callback()
