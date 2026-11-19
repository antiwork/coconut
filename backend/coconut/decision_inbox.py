"""Share one decision record between chat and Inbox."""
from __future__ import annotations

import json


# Hard limits back up the writing rules. Recent cards used a median of 324
# context characters; the rules aim for about 300 and reject longer text.
CARD_TEXT_LIMITS = dict(context=400, uncertainty=200)
RISK_LIMIT = 200
OPTION_EXPLANATION_LIMIT = 200
# The cards with an open or expired Undo window. The partial index decisions_undo holds them.
# No ORDER BY: it makes SQLite scan the table. The few rows sort in Python.
SWIPED_DECISIONS = "SELECT rowid, data FROM decisions WHERE json_extract(data,'$.inbox_undo_until') IS NOT NULL"
# A dismissed card closes with this outcome. A party that waited for it gets the response.
DISMISSED_OUTCOME = 'Dismissed. No answer was sent.'
DISMISSED_RESPONSE = ('The person dismissed this card without an answer. This is not a rejection and not an approval. '
                      'Do not act on the card, and do not ask the same question again while its facts are unchanged.')


def decision_risk(payload):
    from .service import ServiceError
    level, reason = payload.get('risk_level'), payload.get('risk', '')
    if (level not in (None, 'low', 'medium', 'high', 'not_assessed')
            or not isinstance(reason, str) or (level is not None and not reason.strip())):
        raise ServiceError(400, 'Use low, medium, high, or not_assessed for risk_level, with a concrete reason in risk.')
    if level is None and reason.strip():
        # The card shows the level as the heading of the risk. A reason alone shows only "Risk".
        raise ServiceError(400, 'Set risk_level to low, medium, high, or not_assessed whenever you state a risk.')
    if len(reason) > RISK_LIMIT:
        raise ServiceError(400, f'State the risk in one sentence of at most {RISK_LIMIT} characters.')
    return dict(risk_level=level, risk=reason.strip())


MERGE_RISK_LEVELS = ('low', 'medium', 'high')


def require_merge_risk(decision):
    """A merge card always states an assessed risk: on the card, or on each choice that merges.

    A choice merges when its label names a merge. A choice such as Leave open needs no risk of its own.
    """
    from .service import ServiceError
    merges = [option for option in decision.get('options', []) if 'merge' in option.get('label', '').lower()]
    if decision.get('merge_pr_url') and not (
            decision.get('risk_level') in MERGE_RISK_LEVELS
            or (merges and all(option.get('risk_level') in MERGE_RISK_LEVELS for option in merges))):
        raise ServiceError(400, 'A merge card needs risk_level low, medium, or high, with one sentence in risk '
                                'that names what the merge could break.')


def needs_attention_now(payload):
    """Whether the bot says this card must reach the person before their next work hours. Missing is false."""
    from .service import ServiceError
    value = payload.get('needs_attention_now', False)
    if type(value) is not bool:
        raise ServiceError(400, 'Use true or false for needs_attention_now.')
    return dict(needs_attention_now=value)


def migrate_notification_importance(db):
    """Replace the retired notification_importance of stored cards. Only urgent becomes needs_attention_now."""
    db.execute("""UPDATE decisions SET data=json_set(data, '$.needs_attention_now', json('true'))
                  WHERE json_extract(data, '$.notification_importance')='urgent'""")
    db.execute("""UPDATE decisions SET data=json_remove(data, '$.notification_importance')
                  WHERE json_type(data, '$.notification_importance') IS NOT NULL""")


def card_text(payload):
    from .service import ServiceError
    text = {}
    for field, limit in CARD_TEXT_LIMITS.items():
        value = payload.get(field, '')
        if not isinstance(value, str):
            raise ServiceError(400, f'Supply the {field} as text.')
        if len(value) > limit:
            raise ServiceError(400, f'Shorten the {field} to at most {limit} characters. '
                                    'Keep only the facts needed to decide, and put detail in chat.')
        text[field] = value
    return text


def has_assessment(decision):
    return bool(decision.get('risk') or decision.get('uncertainty')
                or any(option.get('risk') for option in decision.get('options', [])))


def invalidate_assessment(decision):
    if has_assessment(decision):
        changed = not decision.get('assessment_stale')
        decision['assessment_stale'] = True
        return changed
    return False


def decision_options(options):
    from .service import ServiceError
    if (not isinstance(options, list) or not 1 <= len(options) <= 8
            or any(not isinstance(option, dict)
                   or any(not isinstance(option.get(key), str) or not option[key].strip() or len(option[key]) > limit
                          for key, limit in (('id', 80), ('label', 300)))
                   or not isinstance(option.get('explanation', ''), str) for option in options)
            or len({option['id'] for option in options}) != len(options)):
        raise ServiceError(400, 'Supply one to eight distinct choices with concise explanations.')
    if any(len(option.get('explanation', '')) > OPTION_EXPLANATION_LIMIT for option in options):
        raise ServiceError(400, f'Keep each option explanation to one short sentence of at most {OPTION_EXPLANATION_LIMIT} characters.')
    return [dict(id=option['id'], label=option['label'], answer='approve',
                 explanation=option.get('explanation', ''), **decision_risk(option)) for option in options]


def require_recommended_first(options, recommended_id):
    """A card lists its recommended choice first. The bot must put it there: Coconut does not reorder the choices,
    because a bot often letters them and quotes the letter in the text that a choice posts."""
    from .service import ServiceError
    if recommended_id is not None and options and options[0]['id'] != recommended_id:
        raise ServiceError(400, RECOMMENDED_FIRST.format(recommended=recommended_id))


def closed_reason(decision, text):
    """The text of a refusal for a card that is no longer pending. For a card that Coconut closed because its GitHub
    source ended, the outcome follows, so that the bot and the person learn why. The first sentence stays as it is."""
    if decision.get('status') == 'resolved' and decision.get('resolution') == 'source_changed' and decision.get('outcome'):
        return text + ' ' + decision['outcome']
    return text


class DecisionInbox:
    def edit_message_draft(self, decision, task, data, device):
        from .service import ServiceError, now, digest, encoded
        from .message_drafts import message_draft
        if (decision.get('action_type') != 'draft_message' or decision['status'] != 'pending'
                or type(data.get('revision')) is not int or data['revision'] != decision['revision']
                or task['status'] == 'cancelled' or self._task_paused(task)):
            raise ServiceError(409, 'This draft changed. Open it again before editing.')
        draft = message_draft(data.get('draft'))
        decision.setdefault('_draft_edits', []).append(dict(draft=decision['draft'], revision=decision['revision'],
                                                          edited_by=device, edited_at=now()))
        decision.update(draft=draft, draft_hash=digest(encoded(draft)), revision=decision['revision'] + 1,
                        inbox_deferred_at=None, inbox_undo_until=None, inbox_swipe_id=None)
        invalidate_assessment(decision)
        self._put('decisions', decision)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        return self._public(decision)

    def decision_followup(self, task, payload, *, allow_resume=True):
        from .service import ServiceError
        explicit = payload.get('previous_decision_id')
        if explicit is not None and (not isinstance(explicit, str) or not explicit.strip()):
            raise ServiceError(400, 'Link an answered question from this conversation.')
        previous_id = explicit or (task.get('_resume_decision') if allow_resume else None)
        if not previous_id:
            return {}
        # Read the two rows by ID. A parse of every card and every task took seconds under the service lock on the mini.
        row = self.db.execute('SELECT data FROM decisions WHERE id=?', (previous_id,)).fetchone()
        previous = json.loads(row[0]) if row else None
        row = self.db.execute('SELECT data FROM tasks WHERE id=?', (previous['task_id'],)).fetchone() if previous else None
        previous_task = json.loads(row[0]) if row else None
        valid = (previous_task is not None and previous['bot_id'] == previous_task['bot_id']
                 and previous_task.get('conversation_bot_id', previous_task['bot_id'])
                 == task.get('conversation_bot_id', task['bot_id'])
                 and previous.get('action_type') in ('question', 'draft_message', 'native_approval')
                 and previous['status'] in ('approved', 'rejected', 'modified', 'replied', 'executed', 'failed')
                 and bool(previous.get('selected_option_label') or previous.get('correction')))
        if not valid:
            if explicit is not None:
                raise ServiceError(400, 'Link an answered question from this conversation.')
            return {}
        return dict(previous_decision_id=previous_id)

    def decision_origin(self, task):
        # Read only the messages of each task in the chain, in the history order of the first task's bot.
        # Reading the whole history of the bot cost about 0.1 s for each bot with a card.
        # Cards of one read share the chains of their parent tasks.
        bot_id, seen, cache = task['bot_id'], set(), getattr(self, '_read_cache', None)
        while task and task['id'] not in seen:
            seen.add(task['id'])
            key = ('decision_origin', bot_id, task['id'])
            if cache is not None and key in cache:
                source = cache[key]
            else:
                source = next((m for m in self._history(bot_id, task_id=task['id'],
                                                        shared_message_ids=task.get('_shared_message_ids', []))
                               if m['role'] == 'user' and m['kind'] == 'text' and not m.get('sender_bot_id')
                               and not m.get('decision_id')), None)
                if cache is not None:
                    cache[key] = source
            if source:
                return dict(request_text=source['text'][:4000], request_message_id=source['id'],
                            request_bot_id=source['bot_id'], request_created_at=source['created_at'])
            parent = task.get('source_task_id') or task.get('parent_task_id')
            task = self._get('tasks', parent) if parent else None
        return {}

    def update_inbox(self, decision, task, data):
        from datetime import datetime, timedelta, timezone
        from .service import ServiceError, now, uid
        action = data.get('action')
        if (type(data.get('revision')) is not int or data['revision'] != decision['revision']
                or task['status'] == 'cancelled'):
            raise ServiceError(409, closed_reason(decision, 'This decision changed. Refresh it first.'))
        if action == 'restore':
            deadline = decision.get('inbox_undo_until')
            if not deadline or datetime.fromisoformat(deadline.replace('Z', '+00:00')) <= datetime.now(timezone.utc):
                raise ServiceError(409, 'The Undo window ended.')
            if not data.get('swipe_id') or data['swipe_id'] != decision.get('inbox_swipe_id'):
                raise ServiceError(409, 'Another swipe changed this decision.')
            if decision['status'] not in ('pending', 'dismissed'):
                raise ServiceError(409, closed_reason(decision, 'This decision is no longer pending.'))
            # A required card gets a new revision, so an answer sent before the swipe cannot land after Undo.
            # A dismissal saved before this release also kept the device that would reject it.
            legacy = decision.pop('_inbox_reject_device', None) is not None
            if legacy or decision.get('inbox_kind') != 'proposal':
                decision['revision'] += 1
            decision.pop('_inbox_dismissed_at', None)
            decision.update(status='pending', inbox_deferred_at=None, inbox_undo_until=None, inbox_swipe_id=None)
        elif decision['status'] != 'pending':
            raise ServiceError(409, closed_reason(decision, 'This decision is no longer pending.'))
        elif action in ('keep', 'later', 'dismiss'):
            decision['inbox_swipe_id'] = uid()
            decision['inbox_undo_until'] = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
            if action == 'dismiss':
                # A dismissal is no answer. finish_inbox_swipes closes the card after the Undo window.
                decision['status'] = 'dismissed'
                decision['_inbox_dismissed_at'] = now()
            else:
                decision['inbox_deferred_at'] = now()
        else:
            raise ServiceError(400, 'Choose later, dismiss, or restore.')
        self._put('decisions', decision)
        self._event('decision.updated', self._public(decision), decision['bot_id'], decision['task_id'])
        return self._public(decision)

    def finish_inbox_swipes(self):
        from datetime import datetime, timezone
        with self.transaction():
            for decision in (json.loads(row[1]) for row in sorted(self.db.execute(SWIPED_DECISIONS).fetchall())):
                deadline = decision.get('inbox_undo_until')
                if not deadline or datetime.fromisoformat(deadline.replace('Z', '+00:00')) > datetime.now(timezone.utc):
                    continue
                task = self._get('tasks', decision['task_id'])
                decision['inbox_undo_until'] = None
                decision['inbox_swipe_id'] = None
                # A dismissal saved before this release kept the device that would reject the card. It rejects nothing now.
                decision.pop('_inbox_reject_device', None)
                if decision['status'] == 'dismissed' and decision.get('inbox_kind') != 'proposal':
                    if task['status'] == 'cancelled':
                        decision['status'] = 'cancelled'
                    else:
                        self._close_dismissed_card(decision, task)
                self._put('decisions', decision)
                self._event('decision.updated', self._public(decision), decision['bot_id'], decision['task_id'])

    def _close_dismissed_card(self, decision, task):
        """Close a required card that the person dismissed. The caller holds the transaction and saves the card.

        A dismissal is no answer: it is not a rejection and not an approval. The card keeps the status `dismissed`,
        with no entry in `_answers` and no `decided_at`. Nothing goes to a source, and no follow-up turn starts.
        Only a waiting party learns that the card closed, so that it does not wait forever:
        - A gateway turn that waits for the card gets the dismissal (gateway_chat.dismiss_gateway_question).
        - The tool call that asked the card records the resolution `dismissed`. A dismissed approval never runs its tool.
        - A task that only waited for this card ends without a turn. A bot that assigned the task gets its result.
        A dismissed card about a source covers that source until the source changes (work_sources.coverage).
        """
        from .service import now, encoded
        decision['outcome'] = DISMISSED_OUTCOME
        if decision.get('_gateway_question'):
            self.dismiss_gateway_question(decision)
            return
        if decision.get('_receipt_id'):
            record = self._get('native_receipts', decision['_receipt_id'])
            if record['state'] in ('awaiting_answer', 'awaiting_approval'):
                # A dismissed approval never runs its tool.
                result = ({'response': DISMISSED_RESPONSE} if record['state'] == 'awaiting_answer'
                          else {'error': DISMISSED_RESPONSE + ' The tool did not run.'})
                record.update(state='completed', completed_at=now(), result=encoded(dict(result, resolution='dismissed')))
                self._put('native_receipts', record)
        # Another required card keeps the task open while it is pending, and while its own Undo window is open:
        # Undo can still return it to pending. Its own window end then closes the task.
        if task['status'] != 'needs_you' or any(
                d['id'] != decision['id'] and d['task_id'] == task['id'] and d.get('inbox_kind') != 'proposal'
                and (d['status'] == 'pending' or (d['status'] == 'dismissed' and d.get('inbox_undo_until')))
                for d in self._all('decisions')):
            return
        task.update(status='completed', summary=DISMISSED_OUTCOME, updated_at=now())
        self._finish_assignment_turn(task, DISMISSED_RESPONSE, 'completed')
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
        self._refresh_bot(task['bot_id'])

    def decision_chat_input(self, task):
        from .service import ServiceError
        sources = []
        for identity in task.get('_turn_message_ids', []):
            try:
                source = self._human_source(task, identity, task, 'answer a decision')
            except ServiceError:
                continue
            sources.append(dict(id=source['id'], text=source['text']))
        decisions = [self._public(d) for row in self.db.execute(
            "SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending' ORDER BY rowid")
            for d in [json.loads(row[0])] if self._decision_visible_to_task(d, task)]
        return dict(_pending_decisions=decisions, _decision_reply_sources=sources)

    def _decision_visible_to_task(self, decision, task):
        owner = self._get('tasks', decision['task_id'])['conversation_bot_id']
        if owner != task['conversation_bot_id']:
            return False
        # The conversation owner sees delegated cards. Delegates see only shared messages.
        return task['bot_id'] == owner or decision['id'] in self._history_decision_ids(task['bot_id'])

    def _history_decision_ids(self, bot_id):
        """Return the IDs of the cards that messages in the bot's history name. This is the _history filter.

        Inside _shared_reads, Coconut reads each bot once, and a write to messages drops the answer.
        """
        cache = self._shared_cache()
        key = ('history decisions', bot_id)
        if cache is not None and key in cache:
            return cache[key]
        ids = {row[0] for row in self.db.execute(
            "SELECT DISTINCT json_extract(m.data, '$.decision_id') FROM message_audience a "
            "JOIN messages m ON m.id=a.message_id WHERE a.bot_id=? "
            "AND COALESCE(json_extract(m.data, '$._hidden'),0)=0 AND COALESCE(json_extract(m.data, '$._deleted'),0)=0 "
            "AND json_extract(m.data, '$.decision_id') IS NOT NULL", (bot_id,))}
        if cache is not None:
            cache[key] = ids
        return ids

    def resolve_chat_decision(self, task_id, payload):
        callbacks = []
        from .service import ServiceError, uid
        with self.transaction():
            source_task = self._get('tasks', task_id)
            if source_task['status'] != 'working' or self.stopping.is_set():
                raise ServiceError(409, 'This conversation is no longer working.')
            source_id = payload.get('source_message_id')
            if source_id not in source_task.get('_turn_message_ids', []):
                raise ServiceError(409, 'Use a human message from the current turn.')
            source = self._human_source(source_task, source_id, source_task, 'answer a decision')
            decision = self._get('decisions', payload.get('decision_id'))
            task = self._get('tasks', decision['task_id'])
            if (not self._decision_visible_to_task(decision, source_task)
                    or type(payload.get('decision_revision')) is not int or payload['decision_revision'] != decision['revision']):
                raise ServiceError(409, closed_reason(decision, 'Use a current decision from this conversation.'))
            context = dict(id=uid(), task_id=task['id'], bot_id=task['bot_id'], decision_id=decision['id'],
                           question=source['text'], status='working', _reply_device=source['_human_device_id'],
                           _decision_revision=decision['revision'], _source_message_id=source_id)
            self._put('contexts', context)
            result = self._resolve_decision_reply(context['id'], payload, callbacks)
            context['status'] = 'completed'
            self._put('contexts', context)
        for callback in callbacks:
            callback()
        return result

    def resolve_decision_reply(self, context_id, payload):
        callbacks = []
        with self.transaction():
            result = self._resolve_decision_reply(context_id, payload, callbacks)
        for callback in callbacks:
            callback()
        return result

    def _resolve_decision_reply(self, context_id, payload, callbacks):
        from .service import ServiceError
        context = self._get('contexts', context_id)
        decision = self._get('decisions', context['decision_id'])
        task = self._get('tasks', context['task_id'])
        if (not context.get('_reply_device') or context['status'] != 'working'
                or decision['status'] != 'pending' or task['status'] == 'cancelled'
                or decision['revision'] != context['_decision_revision']):
            raise ServiceError(409, closed_reason(decision, 'The reply no longer matches a pending decision.'))
        if context.get('_background_completion') and payload.get('choice') != 'modify':
            return {'status': 'pending', 'error': 'Background results can update the card but cannot answer it for the person.'}
        quote = payload.get('quote')
        if not isinstance(quote, str) or not quote.strip() or quote not in context['question']:
            raise ServiceError(400, 'Quote the exact instruction from this reply.')
        # Native approval is bound to an exact action and requires its visible choice.
        if decision['action_type'] == 'native_approval' and payload.get('choice') != 'reject':
            return {'status': 'pending', 'error': 'Ask the person to select the exact action on its card. Do not execute it from this follow-up.'}
        if decision.get('previews') and payload.get('choice') == 'approve':
            return {'status': 'pending', 'error': 'Ask the person to review the images and select a choice on the card.'}
        if decision['action_type'] == 'draft_message':
            if payload.get('choice') == 'approve':
                return {'status': 'pending', 'error': 'Ask the person to select Send on the exact draft.'}
            if payload.get('choice') == 'modify':
                revision = payload.get('revision', {})
                from .decision_previews import attach_previews
                from .decision_sources import attach_sources
                attach_sources(decision, revision)
                attach_previews(self, decision, revision)
                result = self.edit_message_draft(decision, task,
                    dict(revision=decision['revision'], draft=revision.get('draft')), context['_reply_device'])
                return {'status': 'recorded', 'decision': result}
        choice = payload.get('choice')
        if choice not in ('approve', 'reject', 'modify'):
            raise ServiceError(400, 'Choose approve, reject, or modify.')
        if not decision.get('_native'):
            if choice == 'approve':
                return {'status': 'pending', 'error': 'Use the visible choice to approve this file action.'}
            result = self._answer(decision, task, dict(choice=choice, revision=decision['revision'],
                correction=context['question'] if choice == 'modify' else ''), context['_reply_device'])
            return {'status': 'recorded', 'decision': result}
        if choice == 'modify' and decision['action_type'] == 'question':
            revision = payload.get('revision')
            if not isinstance(revision, dict):
                return {'status': 'pending', 'error': 'Supply the revised question and choices, or ask for clarification.'}
            return self._revise_question(decision, task, revision, context['question'], context['_reply_device'])
        result = self.answer_native(decision, task, {
            'choice': choice, 'option_id': payload.get('option_id'),
            'correction': context['question'] if choice == 'modify' else '',
            'revision': decision['revision'],
        }, context['_reply_device'], callbacks)
        reply = {'status': 'recorded', 'decision': result}
        if decision['_answers'][-1].get('instructions_changed'):
            # This turn can act on the answer before the turn that continues the card, so it gets the note too.
            from .native_conversation import INSTRUCTIONS_CHANGED
            reply['instruction'] = INSTRUCTIONS_CHANGED.strip()
        return reply

    def _revise_question(self, decision, task, revision, reason, actor=None):
        from .service import ServiceError
        options = decision_options(revision.get('options'))
        assessment = decision_risk(revision)
        title = revision.get('title')
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise ServiceError(400, 'Supply the revised question.')
        text = card_text(revision)
        explanation = revision.get('explanation', '')
        if not isinstance(explanation, str) or len(explanation) > 2000:
            raise ServiceError(400, 'Use concise text for decision details.')
        recommended = revision.get('recommended_option_id')
        if recommended is not None and recommended not in {o['id'] for o in options}:
            raise ServiceError(400, 'Recommend a current choice.')
        require_recommended_first(options, recommended)
        if 'previous_decision_id' in revision:
            decision.update(self.decision_followup(task, revision))
        merge_url = self._merge_decision_url({'merge_pr_url': revision.get('merge_pr_url', decision.get('merge_pr_url'))}, task)
        merged = self._merge_decision_artifact(merge_url, task)
        merge_head = merged.get('head_sha')
        requested_at = self._validate_merge_decision(dict(decision, merge_pr_url=merge_url, _merge_pr_head=merge_head), task)
        require_merge_risk(dict(assessment, options=options, merge_pr_url=merge_url))
        decision.update(title=title, options=options, **assessment, assessment_stale=False,
                        merge_pr_url=merge_url, _merge_pr_head=merge_head, _merge_request_at=requested_at,
                        merge_pr_diff=merged.get('diff'),
                        recommended_option_id=recommended, revision=decision['revision'] + 1,
                        inbox_deferred_at=None)
        decision.update(text, explanation=explanation)
        decision.pop('evidence', None)
        from .decision_previews import attach_previews
        from .decision_sources import attach_sources
        attach_sources(decision, revision)
        attach_previews(self, decision, revision)
        decision.setdefault('_answers', []).append({'choice': 'revise', 'correction': reason, 'decided_by': actor})
        self._put('decisions', decision)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        return {'status': 'recorded', 'decision': self._public(decision)}


# The refusal of a card whose recommended option is not first. It lives with the other card texts.
from .card_tools import RECOMMENDED_FIRST  # noqa: E402
