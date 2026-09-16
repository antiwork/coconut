"""Share one decision record between chat and Inbox."""
from __future__ import annotations


def decision_options(options):
    from .service import ServiceError
    if (not isinstance(options, list) or not 1 <= len(options) <= 8
            or any(not isinstance(option, dict)
                   or any(not isinstance(option.get(key), str) or not option[key].strip() or len(option[key]) > limit
                          for key, limit in (('id', 80), ('label', 300)))
                   or not isinstance(option.get('explanation', ''), str)
                   or len(option.get('explanation', '')) > 500 for option in options)
            or len({option['id'] for option in options}) != len(options)):
        raise ServiceError(400, 'Supply one to eight distinct choices with concise explanations.')
    return [dict(id=option['id'], label=option['label'], answer='approve',
                 explanation=option.get('explanation', '')) for option in options]


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
        self._put('decisions', decision)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        return self._public(decision)

    def decision_followup(self, task, payload):
        from .service import ServiceError
        explicit = payload.get('previous_decision_id')
        if explicit is not None and (not isinstance(explicit, str) or not explicit.strip()):
            raise ServiceError(400, 'Link an answered question from this conversation.')
        previous_id = explicit or task.get('_resume_decision')
        if not previous_id:
            return {}
        previous = next((d for d in self._all('decisions') if d['id'] == previous_id), None)
        previous_task = next((t for t in self._all('tasks') if previous and t['id'] == previous['task_id']), None)
        valid = (previous_task is not None and previous['bot_id'] == previous_task['bot_id']
                 and previous_task.get('conversation_bot_id', previous_task['bot_id'])
                 == task.get('conversation_bot_id', task['bot_id'])
                 and previous.get('action_type') in ('question', 'draft_message', 'native_approval')
                 and previous['status'] in ('approved', 'rejected', 'modified', 'executed', 'failed')
                 and bool(previous.get('selected_option_label') or previous.get('correction')))
        if not valid:
            if explicit is not None:
                raise ServiceError(400, 'Link an answered question from this conversation.')
            return {}
        return dict(previous_decision_id=previous_id)

    def decision_origin(self, task, *, messages=None):
        seen = set()
        if messages is None:
            messages = self._history(task['bot_id'])
        while task and task['id'] not in seen:
            seen.add(task['id'])
            shared = set(task.get('_shared_message_ids', []))
            source = next((m for m in messages if (m.get('task_id') == task['id'] or m['id'] in shared) and m['role'] == 'user'
                           and m['kind'] == 'text' and not m.get('sender_bot_id')
                           and not m.get('decision_id') and not m.get('_hidden') and not m.get('_deleted')), None)
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
            raise ServiceError(409, 'This decision changed. Refresh it first.')
        if action == 'restore':
            deadline = decision.get('inbox_undo_until')
            if not deadline or datetime.fromisoformat(deadline.replace('Z', '+00:00')) <= datetime.now(timezone.utc):
                raise ServiceError(409, 'The Undo window ended.')
            if not data.get('swipe_id') or data['swipe_id'] != decision.get('inbox_swipe_id'):
                raise ServiceError(409, 'Another swipe changed this decision.')
            if decision['status'] not in ('pending', 'dismissed'):
                raise ServiceError(409, 'This decision is no longer pending.')
            if decision.pop('_inbox_reject_device', None) is not None:
                decision['revision'] += 1
            decision.update(status='pending', inbox_deferred_at=None, inbox_undo_until=None, inbox_swipe_id=None)
        elif decision['status'] != 'pending':
            raise ServiceError(409, 'This decision is no longer pending.')
        elif action in ('keep', 'later', 'dismiss'):
            decision['inbox_swipe_id'] = uid()
            decision['inbox_undo_until'] = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
            if action == 'dismiss':
                decision['status'] = 'dismissed'
                if decision.get('inbox_kind') != 'proposal':
                    decision['_inbox_reject_device'] = self._request_device_id or 'local'
            else:
                decision['inbox_deferred_at'] = now()
        else:
            raise ServiceError(400, 'Choose later, dismiss, or restore.')
        self._put('decisions', decision)
        self._event('decision.updated', self._public(decision), decision['bot_id'], decision['task_id'])
        return self._public(decision)

    def finish_inbox_swipes(self):
        from datetime import datetime, timezone
        callbacks = []
        with self.transaction():
            for decision in self._all('decisions'):
                deadline = decision.get('inbox_undo_until')
                if not deadline or datetime.fromisoformat(deadline.replace('Z', '+00:00')) > datetime.now(timezone.utc):
                    continue
                task = self._get('tasks', decision['task_id'])
                device = decision.get('_inbox_reject_device')
                if device and task['status'] != 'cancelled' and self._task_paused(task):
                    continue
                decision['inbox_undo_until'] = None
                decision['inbox_swipe_id'] = None
                decision.pop('_inbox_reject_device', None)
                if device and decision['status'] == 'dismissed' and task['status'] != 'cancelled':
                    # Keep the rejection reversible until Undo expires. No worker resumes before this transaction.
                    decision['status'] = 'pending'
                    data = dict(choice='reject', revision=decision['revision'])
                    if decision.get('_native'):
                        self.answer_native(decision, task, data, device, callbacks)
                    else:
                        self._answer(decision, task, data, device)
                else:
                    if device and task['status'] == 'cancelled':
                        decision['status'] = 'cancelled'
                    self._put('decisions', decision)
                    self._event('decision.updated', self._public(decision), decision['bot_id'], decision['task_id'])
        for callback in callbacks:
            callback()

    def decision_chat_input(self, task):
        from .service import ServiceError
        sources = []
        for identity in task.get('_turn_message_ids', []):
            try:
                source = self._human_source(task, identity, task, 'answer a decision')
            except ServiceError:
                continue
            sources.append(dict(id=source['id'], text=source['text']))
        decisions = [self._public(d) for d in self._all('decisions')
                     if d['status'] == 'pending' and self._decision_visible_to_task(d, task)]
        return dict(_pending_decisions=decisions, _decision_reply_sources=sources)

    def _decision_visible_to_task(self, decision, task):
        owner = self._get('tasks', decision['task_id'])['conversation_bot_id']
        if owner != task['conversation_bot_id']:
            return False
        # The conversation owner sees delegated cards. Delegates see only shared messages.
        return task['bot_id'] == owner or any(
            message.get('decision_id') == decision['id'] for message in self._history(task['bot_id']))

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
                raise ServiceError(409, 'Use a current decision from this conversation.')
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
            raise ServiceError(409, 'The reply no longer matches a pending decision.')
        if context.get('_background_completion') and payload.get('choice') != 'modify':
            return {'status': 'pending', 'error': 'Background results can update the card but cannot answer it for the person.'}
        quote = payload.get('quote')
        if not isinstance(quote, str) or not quote.strip() or quote not in context['question']:
            raise ServiceError(400, 'Quote the exact instruction from this reply.')
        # Native approval is bound to an exact action and requires its visible choice.
        if decision['action_type'] == 'native_approval' and payload.get('choice') != 'reject':
            return {'status': 'pending', 'error': 'Ask the person to select the exact action on its card. Do not execute it from this follow-up.'}
        if decision['action_type'] == 'draft_message':
            if payload.get('choice') == 'approve':
                return {'status': 'pending', 'error': 'Ask the person to select Send on the exact draft.'}
            if payload.get('choice') == 'modify':
                revision = payload.get('revision', {})
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
        return {'status': 'recorded', 'decision': result}

    def _revise_question(self, decision, task, revision, reason, actor=None):
        from .service import ServiceError
        options = decision_options(revision.get('options'))
        if decision.get('_recovery_work_ids') and options != decision_options(decision['options']):
            raise ServiceError(400, 'Recovery actions are fixed. Keep the existing options, or withdraw an obsolete recovery question.')
        title = revision.get('title')
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise ServiceError(400, 'Supply the revised question.')
        recommended = revision.get('recommended_option_id')
        if recommended is not None and recommended not in {o['id'] for o in options}:
            raise ServiceError(400, 'Recommend a current choice.')
        if 'previous_decision_id' in revision:
            decision.update(self.decision_followup(task, revision))
        merge_url = self._merge_decision_url({'merge_pr_url': revision.get('merge_pr_url', decision.get('merge_pr_url'))}, task)
        decision.update(title=title, options=options,
                        merge_pr_url=merge_url, _merge_pr_head=self._merge_decision_head(merge_url, task),
                        recommended_option_id=recommended, revision=decision['revision'] + 1,
                        inbox_deferred_at=None)
        for field, limit in (('context', 2000), ('explanation', 2000), ('evidence', 8000), ('uncertainty', 2000), ('risk', 2000)):
            value = revision.get(field, '')
            if not isinstance(value, str) or len(value) > limit:
                raise ServiceError(400, 'Use concise text for decision details.')
            decision[field] = value
        decision.setdefault('_answers', []).append({'choice': 'revise', 'correction': reason, 'decided_by': actor})
        self._put('decisions', decision)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        return {'status': 'recorded', 'decision': self._public(decision)}


DECISION_INSTRUCTIONS = (
    'Coconut interaction rules apply to every bot, including delegated work and old conversations. '
    'Whenever you need a human choice, permission, confirmation, or missing information, call coconut_ask. '
    'A question in chat does not create a card and does not put the request in Inbox. '
    'Do not finish with a prose request such as "Should I proceed?", "Which do you prefer?", or "Let me know when ready." '
    'For optional next steps, call coconut_suggest and continue the authorized work. '
    'For an outbound email, message, or comment that needs review, call coconut_suggest with a draft object and a stable key. '
    'This creates an editable draft card in chat and Inbox. Omit options; Coconut supplies Send and Discard. '
    'Use a short title and only essential context. Put the complete exact text in draft.body, with the exact destination and sending account. '
    'Check the connected service can send before offering Send. Resolve missing access or ambiguous destinations first. '
    'Draft cards support text only. Do not hide attachments, CC, BCC, or extra actions behind Send. '
    'Do not send before the person selects Send on the current draft. Edits and reactions do not approve it. '
    'When asked to revise a draft, use coconut_resolve_reply with revision.draft. It stays pending for review. '
    'After approval, send only the approved draft through the connected service and verify its result. Never call approval a completed send. '

    'An offer such as "I can investigate this if you want" belongs in an optional card. '
    'Do not tell the person to remind each bot to use cards or save these app rules in each bot memory. '
    'If the person must type an answer, ask through coconut_ask with meaningful choices when available; the card accepts a typed reply. '
    'For a manual step, explain the required step and ask for its result in a card. Never invent a completed result. '
    'For visual approval, provide the actual preview in chat before presenting the approval card. '
    'Do not ask again for work already authorized. Native Hermes approval checks still apply. '
    'For an authorized code change, handle ordinary local setup and debugging yourself. '
    'Use separate git worktrees and disposable OrbStack containers with task-owned databases, caches, and storage. '
    'Install pinned public dependencies, inspect local diagnostics, and run local tests within that request. '
    'Do not turn each setup step, missing package, failed test, or recovery attempt into a human decision. '
    'Preserve explicit human limits. Ask only for missing credentials, material cost, destructive shared changes, '
    'production access, or a real product choice that the person has not already settled. '
    'When a delegated task needs a human answer, create its card; do not relay a prose question for another bot to repeat. '
    'If a card already asks the same question, refer to it without creating another card. '
    'When answering a question about an existing card, explain in chat and leave that card pending. '
    'Before ending a turn, check whether your reply asks the human to choose or offers optional work. '
    'Use the appropriate card tool first. Ordinary answers, progress reports, and completed results stay in chat. '
    'Write decision cards in Simplified Technical English. Use familiar words, active voice, and one idea per sentence. '
    'Keep the tone calm and neutral. Use the same name for the same thing throughout the card. '
    'A card must make sense on its own in Inbox. Use this order without forcing headings or empty fields: '
    'title: the specific action and affected thing, phrased as a short question; '
    'context: what is wrong or needed, why it matters, and the practical result of approval; '
    'risk: what could go wrong with this action and any verified limit on its impact; '
    'uncertainty: a remaining unknown only when it could change this decision; '
    'options: clear actions with distinct results. '
    'Usually use two or three short context sentences. Add words only for facts needed to decide. '
    'Prefer "Correct refunds in the tax report?" to "Fix the tax-report refund error?" '
    'Explain the effect: "The proposed report counts refunds that should be excluded, making tax totals too low." '
    'Name the project or record once so the person knows which work this concerns. '
    'Describe approval in user terms, such as "Update PR #7662 and request review. Live reports stay unchanged." '
    'This example does not authorize any action. Use only scope and facts verified for the actual decision. '
    'Keep exact targets, evidence, and execution limits in the underlying request and chat. '
    'The visible card must still disclose public changes, external messages, cost, data loss, and other material approval limits. '
    'Do not replace these effects with a list of tools, tests, review steps, commit IDs, or permission jargon. '
    'Use specific option labels such as "Fix the PR" and "Leave unchanged", rather than "Fix" and "Leave blocked". '
    'Use an option explanation only to add a consequence or tradeoff. Do not repeat the label or context. '
    'State material risk in one concrete sentence. Describe the failure and who or what it affects. '
    'Do not use unsupported risk ratings, confidence percentages, or blanket claims that an action is safe. '
    'Omit an empty risk field. Do not invent a warning merely to fill the structure. '
    'Keep risk and uncertainty distinct and avoid repeating either in context. '
    'Match them to the action being approved. A future release caveat belongs here only if it changes this choice. '
    'Preserve material risk and approval limits when shortening text. '
    'Before creating or revising a card, read it as the person: Can they identify the action, reason, consequence, and material risk? '
    'Never invent a fixed set of choices or limit useful choices to three. '
    'Before asking, verify the decisive facts, access, and scope using current evidence. Resolve material uncertainty within existing authorization. '
    'Prefer a smaller reversible action when it meets the goal and reduces risk. '
    'If missing evidence prevents a sound recommendation, complete the allowed check or ask for the specific missing input. '
    'Do not ask the person to approve an unclear plan because the investigation is unfinished. '
    'After a requested check, explain what you checked, what changed, and why the next choice is needed. '
    'Link the answered card with previous_decision_id when the follow-up occurs in a later turn. '
    'A request to investigate is not a ruling or permission for the proposed action. '
    'For past decisions, treat summaries as leads. Read the original decision and discussion, including later reversals. '
    'Cite the original records in evidence or chat. Separate verified facts from inference. '
    'If original records are missing or inaccessible, state that limit once in uncertainty. '
    'Do not call that check complete or remove a useful read-only next step while material evidence remains missing. '
    'Recommend a choice when verified facts support it. State the decisive reason once, in plain language. '
    'Never claim 100 percent confidence, verified access, or completed work without relevant evidence. '
    'Set recommended_option_id when evidence and the user goal give you a clear reason to prefer one choice. '
    'The card adds the Recommended badge. Do not repeat the badge or a reason already stated in context. '
    'Omit recommended_option_id when the choice depends on an unknown preference or the options have no clear advantage. '
    'Do not mark an option just because it comes first. A recommendation is not permission to act. '
    'Mention uncertainty when it could change the choice. Offer a useful way to resolve it when needed. '
    'Use specific action labels. Respect standing preferences when generating options. '
    'Use coconut_ask for a required answer. Use coconut_suggest for a useful optional proposal while continuing the current work. '
    'Do not turn ordinary replies, activity, or completed work into Inbox entries. '
    'In Inbox, Later leaves a decision pending. Dismiss rejects a required decision after the Undo window, or removes an optional suggestion. '
    'Do not recreate dismissed proposals. A proposal is not authorization to perform it. '
    'A response received is not an action completed. Report outcomes only after verification. '
)
