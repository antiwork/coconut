"""Persist native tool receipts and human decisions across turns and restarts."""
from __future__ import annotations

import json
from uuid import uuid4


class NativeExecution:
    def _has_uncertain_receipt(self, task_id, turn_id):
        with self.lock:
            return self.db.execute(
                "SELECT 1 FROM native_receipts WHERE json_extract(data, '$.state')='executing' "
                "AND json_extract(data, '$.task_id')=? AND json_extract(data, '$.turn_id') IS ? LIMIT 1",
                (task_id, turn_id)).fetchone() is not None

    def native_event(self, task_id, kind, payload, context_id=None):
        from .service import ServiceError, YieldForDecision, now, digest, encoded
        source_facts, source_seen = {}, {}
        if kind in ('native_question', 'native_proposal'):
            with self.lock:
                task = self._get('tasks', task_id)
                duplicate = kind == 'native_proposal' and any(
                    d.get('inbox_kind') == 'proposal' and d['task_id'] == task_id
                    and d.get('_proposal_key') == payload.get('key') for d in self._all('decisions'))
                context = self._get('contexts', context_id) if context_id else None
                active = context['status'] == 'working' and context['task_id'] == task_id if context else task['status'] == 'working'
            if active and not duplicate:
                # Source reads must not hold the service transaction or block phone requests.
                from .decision_sources import check_new_decision_sources
                source_facts, source_seen = check_new_decision_sources(self, task_id, payload)
        with self.transaction():
            task = self._get('tasks', task_id)
            context = self._get('contexts', context_id) if context_id else None
            active = context['status'] == 'working' and context['task_id'] == task_id if context else task['status'] == 'working'
            late_result = False
            if context and context.get('_decision_refresh') and context['status'] == 'cancelled' and kind == 'native_result':
                receipt = self._get('native_receipts', payload['id'])
                late_result = receipt['task_id'] == task_id and receipt.get('turn_id') == context_id
            if (not active or self._task_paused(task)) and not late_result:
                raise ServiceError(409, 'This task cannot execute a tool now.')
            if kind == 'native_call' and not context:
                self._refuse_technical_tool(task, {'name': payload['name'], 'arguments': payload['arguments']})
            if not context:
                self._save_work_progress(task, payload)
            if kind == 'native_call':
                action = {'name': payload['name'], 'arguments': payload['arguments']}
                record = dict(id=str(uuid4()), task_id=task_id, turn_id=context_id or task.get('_native_turn_id'), action=action, action_hash=digest(encoded(action)),
                              state='executing', created_at=now())
                record['_worker_profile_revision'] = (payload['_worker_profile_revision'] if '_worker_profile_revision' in payload
                    else self.profiles.prompt_revision(self._get('bots', task['bot_id'])))
                if action['name'] == 'kanban_create':
                    record['_attachment_ids'] = list(task.get('_turn_attachment_ids', task.get('_attachment_ids', [])))
                self._put('native_receipts', record)
                return {'status': 'recorded', 'id': record['id']}
            if kind == 'native_result':
                record = self._get('native_receipts', payload['id'])
                if record['task_id'] == task_id and record['state'] == 'completed' and record['result'] == payload['result']:
                    return {'status': 'recorded'}
                if record['task_id'] != task_id or record['state'] != 'executing':
                    raise ServiceError(409, 'The tool receipt does not match this task.')
                record.update(state='completed', result=payload['result'], completed_at=now())
                self._put('native_receipts', record)
                if record['action']['name'] == 'kanban_comment':
                    try:
                        result = json.loads(record['result'])
                    except (ValueError, TypeError):
                        result = {}
                    identity = record['action']['arguments'].get('task_id')
                    if (isinstance(result, dict) and result.get('ok') is True
                            and result.get('task_id') == identity and result.get('comment_id') is not None):
                        task['_source_native_ids'] = sorted(set(task.get('_source_native_ids', [])) | {identity})
                        self._put('tasks', task)
                # Every tool result reaches this point. Read only this task's approved decisions through the task index.
                approved = (json.loads(row[0]) for row in self.db.execute(
                    "SELECT data FROM decisions WHERE json_extract(data,'$.task_id')=? "
                    "AND json_extract(data,'$.status')='approved' ORDER BY rowid", (task_id,)))
                for decision in approved:
                    if decision.get('_action_hash') == record['action_hash']:
                        try:
                            result = json.loads(payload['result']) if isinstance(payload['result'], str) else payload['result']
                        except (ValueError, TypeError):
                            result = {}
                        failed = isinstance(result, dict) and (bool(result.get('error')) or result.get('success') is False or result.get('is_error') is True or result.get('exit_code', 0) not in (0, None))
                        decision.update(status='failed' if failed else 'executed', outcome='Hermes could not complete this action.' if failed else 'Hermes returned the result of this action.')
                        self._put('decisions', decision)
                        self._event('decision.updated', self._public(decision), task['bot_id'], task_id)
                return {'status': 'recorded'}
            action = payload.get('action', {})
            approval = kind == 'native_approval'
            optional = kind == 'native_proposal'
            if optional:
                existing = next((d for d in self._all('decisions') if d.get('inbox_kind') == 'proposal' and d['task_id'] == task_id and d.get('_proposal_key') == payload.get('key')), None)
                if existing:
                    return {'status': 'recorded', 'decision_id': existing['id']}
                if not isinstance(payload.get('key'), str) or not 1 <= len(payload['key']) <= 100:
                    raise ServiceError(400, 'Supply a stable proposal key.')
            draft = None
            if payload.get('draft') is not None:
                if not optional:
                    raise ServiceError(400, 'Create message drafts with coconut_suggest.')
                from .message_drafts import message_draft
                draft = message_draft(payload['draft'])
            if approval:
                record = self._get('native_receipts', payload['receipt_id'])
                if record['task_id'] != task_id or record['state'] != 'executing':
                    raise ServiceError(409, 'The approval does not match this tool call.')
                action = record['action']
                record['state'] = 'awaiting_approval'
                self._put('native_receipts', record)
                options = [dict(id='once', label='Allow once', answer='approve', explanation='Run this action once. Future actions still require their own permission.'), dict(id='deny', label='Do not run', answer='reject', explanation='Block this action and let the bot know you declined.')]
            else:
                if payload.get('receipt_id'):
                    record = self._get('native_receipts', payload['receipt_id'])
                    if record['task_id'] != task_id or record['state'] != 'executing':
                        raise ServiceError(409, 'The question does not match this tool call.')
                    record['state'] = 'awaiting_answer'
                    self._put('native_receipts', record)
                    action = record['action']
                from .decision_inbox import decision_options, require_recommended_first
                from .message_drafts import draft_options
                options = draft_options() if draft is not None else decision_options(payload.get('options'))
                for field in ('recommended_option_id', 'predicted_option_id'):
                    if payload.get(field) is not None and payload[field] not in {o['id'] for o in options}:
                        raise ServiceError(400, 'Recommendations must refer to a current choice.')
                if draft is None:
                    require_recommended_first(options, payload.get('recommended_option_id'))
            from .decision_inbox import card_text, decision_risk, needs_attention_now
            assessment = dict(decision_risk(payload), **needs_attention_now(payload))
            text = card_text(payload)
            decision = dict(id=str(uuid4()), bot_id=task['bot_id'], task_id=task_id,
                            title=str(payload.get('title', 'Allow this action?'))[:160],
                            explanation=str(payload.get('explanation', ''))[:2000],
                            action_type='draft_message' if draft is not None else 'native_approval' if approval else 'question',
                            resource=str(payload.get('resource', ''))[:4000], consequences='', recommended='approve',
                            options=options, recommended_option_id=payload.get('recommended_option_id'),
                            status='pending', urgency='normal', created_at=now(), decided_at=None, decided_by=None,
                            decided_by_name=None, correction=None, outcome=None, revision=1,
                            inbox_kind='proposal' if optional else 'decision',
                            **text, **assessment,
                            _proposal_key=payload.get('key') if optional else None,
                            _action=action, _action_hash=digest(encoded(action)), _native=True,
                            _predicted_option_id=payload.get('predicted_option_id'), _receipt_id=payload.get('receipt_id'), _answers=[])
            from .decision_previews import attach_previews
            from .decision_sources import attach_sources
            attach_sources(decision, payload)
            if source_facts:
                decision['_source_facts'] = source_facts
            if source_seen:
                # This field never changes. The next turn shows only source activity after these revisions.
                decision['_source_seen'] = source_seen
            attach_previews(self, decision, payload)
            if draft is not None:
                decision['draft'] = draft
                decision['draft_hash'] = digest(encoded(draft))
            decision.update(self.decision_origin(task))
            if not approval:
                # A resumed task can also prepare an unrelated draft. Require its explicit link.
                decision.update(self.decision_followup(task, payload, allow_resume=draft is None))
            if not approval and draft is None:
                decision['merge_pr_url'] = self._merge_decision_url(payload, task)
                merged = self._merge_decision_artifact(decision['merge_pr_url'], task)
                decision['_merge_pr_head'] = merged.get('head_sha')
                decision['merge_pr_diff'] = merged.get('diff')
                decision['_merge_request_at'] = self._validate_merge_decision(decision, task)
                from .decision_inbox import require_merge_risk
                require_merge_risk(decision)
            freshness = {}
            if not approval and payload.get('receipt_id'):
                # A later worker cannot relabel an earlier tool intent with its own prompt snapshot.
                freshness['profile_revision'] = record.get('_worker_profile_revision')
            elif '_worker_profile_revision' in payload:
                freshness['profile_revision'] = payload['_worker_profile_revision']
            self._put('decisions', decision)
            if optional:
                self._initialize_decision_freshness(decision, **freshness)
                self._message(task['bot_id'], decision['title'], task_id=task_id, kind='decision', decision_id=decision['id'])
                self._event('decision.created', self._public(decision), task['bot_id'], task_id)
                return {'status': 'recorded', 'decision_id': decision['id']}
            task.update(status='needs_you', summary=decision['title'])
            self._put('tasks', task)
            self._initialize_decision_freshness(decision, **freshness)
            self._message(task['bot_id'], decision['title'], task_id=task_id, kind='decision', decision_id=decision['id'])
            self._event('decision.created', self._public(decision), task['bot_id'], task_id)
            self._refresh_bot(task['bot_id'])
        raise YieldForDecision('The native decision is durable.', decision_id=decision['id'])

    def answer_native(self, decision, task, data, device, callbacks):
        from .service import ServiceError, now, decision_correction
        if decision.get('_source_recheck'):
            raise ServiceError(409, 'The source changed. Coconut is updating this decision.')
        if self._task_paused(task):
            raise ServiceError(409, 'Restore the bot before answering.')
        if data.get('revision') != decision['revision']:
            raise ServiceError(409, 'This decision changed. Review it before answering.')
        option_id = data.get('option_id')
        option = next((item for item in decision['options'] if item['id'] == option_id), None)
        if option_id is not None and option is None:
            raise ServiceError(400, 'Choose a current option.')
        if option and data.get('choice') is not None and data['choice'] != option['answer']:
            raise ServiceError(400, 'The response does not match this option.')
        choice = option['answer'] if option else data.get('choice')
        if choice not in ('approve', 'reject', 'modify', 'reply') or (choice == 'approve' and not option):
            raise ServiceError(400, 'Choose one of the current options.')
        # A change of the bot instructions does not block an answer. The person answers the card that the app shows,
        # the choice keeps its own scope, and the next turn reads the current instructions.
        if decision.get('previews') and choice == 'approve' and data.get('previews_hash') != decision.get('previews_hash'):
            raise ServiceError(409, 'Review the images in the latest app before choosing this option.')
        if decision.get('draft') is not None and choice == 'approve' and data.get('draft_hash') != decision.get('draft_hash'):
            raise ServiceError(409, 'Open the full draft in the latest app before selecting Send.')
        if decision.get('draft') is not None and choice == 'modify':
            raise ServiceError(400, 'Edit the draft before selecting Send.')
        correction = decision_correction(data)
        if choice in ('modify', 'reply') and not correction.strip():
            raise ServiceError(400, 'Enter a reply or choose an option.')
        decision.update(status={'approve': 'approved', 'reject': 'rejected', 'modify': 'modified', 'reply': 'replied'}[choice],
                        selected_option_id=option_id, selected_option_label=option['label'] if option else None,
                        correction=correction, decided_at=now(), decided_by=device, decided_by_name=self._actor_name(device),
                        outcome='Answer recorded. Hermes will continue.')
        decision['_answers'].append(dict(choice=choice, option_id=option_id, correction=correction,
                                          predicted_option_id=decision.get('_predicted_option_id'), decided_by=device))
        if choice == 'reply':
            decision['outcome'] = 'Reply received. The bot will continue.'
            reply_message_id = self._record_decision_correction(task, decision, correction)
            self._put('tasks', task)
        if decision.get('draft') is not None and choice != 'reply':
            decision['outcome'] = 'Approved for sending.' if choice == 'approve' else 'Draft discarded. Nothing was sent.'
            decision['_answers'][-1].update(draft=decision['draft'], revision=decision['revision'])
        self._put('decisions', decision)
        if decision['action_type'] == 'question' and decision.get('_receipt_id'):
            record = self._get('native_receipts', decision['_receipt_id'])
            record.update(state='completed', result=json.dumps({'response': decision.get('selected_option_label') or correction or 'The person declined.'}))
            self._put('native_receipts', record)
        if decision.get('inbox_kind') == 'proposal':
            if choice != 'reject':
                continuation = self._send(self._get('bots', task['bot_id']),
                           {'text': 'Continue this proposal using the recorded reply. Proposal: ' + decision['title'] + '\nReply: ' + (decision.get('selected_option_label') or correction), 'mode': 'queue', 'attachment_ids': task.get('_attachment_ids', [])},
                           thread_id=task['thread_id'], parent_task_id=task['id'],
                           conversation_bot_id=task['conversation_bot_id'], record_message=False, human_authored=False)
                stored = self._get('tasks', continuation['id'])
                stored['_resume_decision'] = decision['id']
                if task.get('_chat_session'):
                    # With the gateway, a proposal of the bot's conversation continues there.
                    self.join_conversation(stored)
                if choice == 'reply':
                    stored['_direct_reply_message_id'] = reply_message_id
                stored['title'] = decision['title'][:120]
                self._put('tasks', stored)
                decision['_execution_task_id'] = stored['id']
                self._put('decisions', decision)
            self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
            return self._public(decision)
        if task.get('_self_recovery'):
            task['_self_recovery']['attempt'] = 0
        if choice == 'reply':
            task['_direct_reply_message_id'] = reply_message_id
        task.update(status='queued', _resume_decision=decision['id'], updated_at=now())
        if correction.strip() and choice != 'reply':
            self._record_decision_correction(task, decision, correction)
        self._put('tasks', task)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
        self._refresh_bot(task['bot_id'])
        return self._public(decision)
