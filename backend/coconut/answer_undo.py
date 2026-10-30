"""Hold each answer to a card for a short Undo window before the bot gets it.

When the person answers a card (an option or a typed reply), the Mac keeps the answer for ANSWER_UNDO_SECONDS. In that
time the card has the status `held`: the task does not resume, no action runs, and no context request starts. The
answer request returns the card with `answer_undo_until` and `answer_undo_id`.

- Undo (POST /v1/decisions/<id>/inbox with action `undo_answer`) inside the window returns the card to `pending`
  with the same revision, and records the undone answer.
- At the end of the window, finish_held_answers applies the answer as before. It also applies an answer that a restart
  interrupted. A later Undo gets 409.

An app before this change shows the card as answered and has no Undo. The window is short, so its answer reaches the
bot after a short wait. An app that gets no `answer_undo_until` from an older Mac shows no Undo.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

ANSWER_UNDO_SECONDS = 10
HELD = 'held'
# The decisions_status index holds this read.
HELD_DECISIONS = "SELECT rowid, data FROM decisions WHERE json_extract(data,'$.status')='held'"


def _deadline_passed(text, current=None):
    current = current or datetime.now(timezone.utc)
    return not text or datetime.fromisoformat(text.replace('Z', '+00:00')) <= current


class AnswerUndo:
    # Tests of what an answer does set 0: the answer then applies at once, as before the Undo window.
    answer_undo_seconds = ANSWER_UNDO_SECONDS

    def apply_answer(self, decision, task, data, device, callbacks):
        if decision.get('_native'):
            return self.answer_native(decision, task, data, device, callbacks)
        return self._answer(decision, task, data, device)

    def hold_answer(self, decision, task, data, device, callbacks):
        """Keep the answer of POST /v1/decisions/<id>/answer for the Undo window. The caller holds the transaction.

        The checks that need no side effect run now, so a wrong answer fails at once. The full answer runs at the end
        of the window, with its own checks."""
        from .service import ServiceError, now, uid
        if self.answer_undo_seconds <= 0:
            return self.apply_answer(decision, task, data, device, callbacks)
        if decision.get('_source_recheck'):
            raise ServiceError(409, 'The source changed. Coconut is updating this decision.')
        if type(data.get('revision')) is not int or data['revision'] != decision['revision']:
            raise ServiceError(409, 'This decision changed. Refresh it before answering.')
        option_id = data.get('option_id')
        if option_id is not None and not any(option['id'] == option_id for option in decision.get('options') or []):
            raise ServiceError(400, 'Choose a current option.')
        if option_id is None and data.get('choice') not in ('approve', 'reject', 'modify', 'reply'):
            raise ServiceError(400, 'Choose one of the current options.')
        if option_id is None and data.get('choice') in ('modify', 'reply') and not str(data.get('correction') or '').strip():
            raise ServiceError(400, 'Enter a reply or choose an option.')
        if self._task_paused(task) and (decision.get('_native') or data.get('choice') == 'approve' or (
                option_id is not None and next(o for o in decision['options'] if o['id'] == option_id)['answer'] == 'approve')):
            raise ServiceError(409, 'Restore the bot before answering.')
        deadline = datetime.now(timezone.utc) + timedelta(seconds=self.answer_undo_seconds)
        option = next((option for option in decision.get('options') or [] if option['id'] == option_id), None)
        decision['_held_answer'] = dict(data=data, device=device, key=self._request_key, held_at=now())
        # An app before the window shows a card with an unknown status as answered, with the selected label.
        decision.update(selected_option_id=option_id, selected_option_label=option['label'] if option else None)
        decision.update(status=HELD, answer_undo_until=deadline.isoformat().replace('+00:00', 'Z'), answer_undo_id=uid(),
                        held_answer_label=(option['label'] if option else str(data.get('correction') or data.get('choice')))[:200])
        self._put('decisions', decision)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        return self._public(decision)

    def undo_answer(self, decision, task, data, device):
        """Return a held answer's card to pending, with the same revision. The caller holds the transaction."""
        from .service import ServiceError, now
        if decision['status'] != HELD or not data.get('undo_id') or data['undo_id'] != decision.get('answer_undo_id'):
            raise ServiceError(409, 'This answer is no longer waiting. The bot already has it.')
        if _deadline_passed(decision.get('answer_undo_until')):
            raise ServiceError(409, 'The Undo window ended. The bot already has your answer.')
        held = decision.pop('_held_answer')
        decision.setdefault('_undone_answers', []).append(dict(held, undone_at=now(), undone_by=device))
        decision.update(status='pending', answer_undo_until=None, answer_undo_id=None, held_answer_label=None,
                        selected_option_id=None, selected_option_label=None)
        self._put('decisions', decision)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        self._event('decision.answer_undone', dict(decision_id=decision['id'], revision=decision['revision']),
                    task['bot_id'], task['id'])
        return self._public(decision)

    def finish_held_answers(self, current=None):
        """Apply each held answer whose Undo window ended, as the answer request did before this window existed."""
        from .service import ServiceError
        callbacks = []
        with self.transaction():
            due = [json.loads(row[1]) for row in sorted(self.db.execute(HELD_DECISIONS).fetchall())]
            for decision in due:
                if not _deadline_passed(decision.get('answer_undo_until'), current):
                    continue
                held = decision.pop('_held_answer', None) or {}
                decision.update(status='pending', answer_undo_until=None, answer_undo_id=None, held_answer_label=None,
                                selected_option_id=None, selected_option_label=None)
                task = self._get('tasks', decision['task_id'])
                if task['status'] == 'cancelled' or not held:
                    decision['status'] = 'cancelled' if task['status'] == 'cancelled' else 'pending'
                    self._put('decisions', decision)
                    self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
                    continue
                self._request_device_id, self._request_key = held.get('device'), held.get('key')
                self.db.execute('SAVEPOINT held_answer')
                try:
                    self.apply_answer(decision, task, held['data'], held['device'], callbacks)
                    self.db.execute('RELEASE held_answer')
                except ServiceError as error:
                    # The card changed in the window, for example a new revision. It goes back to Inbox.
                    self.db.execute('ROLLBACK TO held_answer')
                    self.db.execute('RELEASE held_answer')
                    # The kept tasks can hold changes that the rollback removed.
                    self._forget_cached_tasks()
                    logging.getLogger(__name__).warning('A held answer to decision %s did not apply: %s', decision['id'], error)
                    decision = self._get('decisions', decision['id'])
                    decision.pop('_held_answer', None)
                    decision.update(status='pending', answer_undo_until=None, answer_undo_id=None, held_answer_label=None,
                                    selected_option_id=None, selected_option_label=None)
                    self._put('decisions', decision)
                    self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
                finally:
                    self._request_device_id = self._request_key = None
        for callback in callbacks:
            callback()
