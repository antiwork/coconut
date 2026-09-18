"""Keep native kanban outcomes separate from phone conversation turns."""
import json
import threading
import time
from uuid import UUID, uuid5


CHAT_INSTRUCTIONS = (
    'Reply directly to this conversation’s sender. '
    'For work with an outcome beyond this reply, create one native kanban task before starting it. '
    'Use kanban_create with a clear title, the complete request, scope, approval limits, and acceptance criteria in body. '
    'Coconut supplies the bot, dispatch state, and a stable creation key. Hermes gives each outcome its own workspace. '
    'Use kanban_list and kanban_show to find existing work before creating another task. '
    'For an existing outcome, add new facts with kanban_comment. Do not create a second task for a retry or status question. '
    'A task comment is context, not new permission. Preserve human decisions. '
    'Create a separate native outcome for new work after a completed PR. Keep the earlier PR as context. '
    'Use coconut_ask only for a concrete decision. Ordinary local setup and planning need no card. '
)
WORK_INSTRUCTIONS = (
    'The native kanban task below owns this outcome. Its comments and previous attempts are the work record. '
    'Use kanban_heartbeat for a short progress description at phase changes. '
    'Use kanban_complete only after verifying every accepted outcome. A reply or plan does not complete the task. '
    'A question about a decision does not complete its unfinished outcome. Answer it and retain the remaining work. '
    'If a decision is still needed, create an updated card with the concrete next action. '
    'Use coconut_ask when a human decision is required. Coconut connects the card to this same native task. '
    'Use coconut_wait for an existing process or bot result. Finish the turn after registering the wait. '
    'Before repeating an action from an earlier attempt, check its receipt and destination. '
    'An absent result does not prove the action failed. Preserve approval limits and unresolved effects. '
    'Use a native dependency or scheduled check for a valid external wait. Do not create a recovery task. '
)

CONTINUE_SCHEMA = {
    'name': 'coconut_continue_work',
    'description': 'Continue a blocked or completed Hermes outcome with new facts. Use the native task ID from kanban_show. '
        'You must own the work or be its requesting bot. This keeps its task, workspace, decisions, and result route. '
        'Hermes preserves completed results and creates a linked follow-up with the same workspace. '
        'This does not approve actions. The result goes to the original requester.',
    'parameters': {'type': 'object', 'additionalProperties': False,
        'properties': {'task_id': {'type': 'string'}, 'message': {'type': 'string', 'maxLength': 16000}},
        'required': ['task_id', 'message']},
}


class NativeOutcomes:
    def _native_continuation_item(self, task):
        from .service import ServiceError
        if task.get('_work_context_id'):
            return self._get('artifacts', task['_work_context_id'])
        # _send retains ownership history before moving the artifact; native
        # reconciliation then replaces its native ID with the successor's ID.
        # Neither mutable ID alone can resolve a reply to the original owner.
        tasks = {t['id']: t for t in self._all('tasks')}
        candidates = []
        for item in self._all('artifacts'):
            if item['kind'] not in ('work', 'pr') or item['bot_id'] != task['bot_id']:
                continue
            if item['task_id'] == task['id']:
                if item.get('_native_outcome_id') == task['_native_outcome_id']:
                    candidates.append(item)
                continue
            owner = tasks.get(item['task_id'], {})
            history = [item.get('_work_origin_task_id'), *item.get('_work_previous_task_ids', [])]
            if (task['id'] in history and owner.get('_work_context_id') == item['id']
                    and owner.get('bot_id') == task['bot_id']
                    and owner.get('conversation_bot_id') == task['conversation_bot_id']
                    and owner.get('_execution_session_id') == (task.get('_execution_session_id') or task['id'])
                    and item.get('_native_outcome_id')
                    and owner.get('_native_outcome_id') == item['_native_outcome_id']):
                candidates.append(item)
        if len(candidates) != 1:
            raise ServiceError(409, 'The native outcome needs one unambiguous retained Work or PR owner.')
        return candidates[0]

    def _restore_native_work_owner(self, owner):
        """An older progress report could replace the visible owner without moving its execution."""
        from .service import ServiceError
        from .background_processes import subscription
        from .work_capacity import execution_key
        if not owner.get('_work_context_id'):
            return
        item = self._get('artifacts', owner['_work_context_id'])
        if item['task_id'] == owner['id']:
            return
        reporter = self._get('tasks', item['task_id'])
        related = [t for t in self._all('tasks')
                   if t.get('session_id') == reporter.get('session_id')
                   and (t.get('_conversation_epoch') == reporter.get('_conversation_epoch')
                        or (t['status'] == 'queued' and '_conversation_epoch' not in t))
                   and (execution_key(t) == execution_key(reporter) or t.get('_assignment_task_id') == reporter['id'])]
        # A saved wait observes another task. It does not own this execution.
        executing = any(t['status'] in ('queued', 'working', 'needs_you', 'paused') for t in related)
        executing = executing or any(r['state'] == 'executing'
            or (not r.get('_process_delivery') and (subscription(r)
                or (r.get('_wait_decision_id') and self._decision_wait_pending(self._get('decisions', r['_wait_decision_id'])))))
            for t in related for r in self._native_task_receipts(t['id']))
        history = [item.get('_work_origin_task_id'), *item.get('_work_previous_task_ids', [])]
        if (item['kind'] != 'work' or item['bot_id'] != owner['bot_id'] or owner['id'] not in history
                or reporter.get('_native_outcome_id') or reporter.get('_work_context_id') == item['id']
                or any(reporter.get(k) != owner.get(k) for k in ('bot_id', 'session_id', 'conversation_bot_id', '_conversation_epoch'))
                or reporter['status'] not in ('completed', 'failed', 'waiting')
                or executing
                or any(d['task_id'] == reporter['id'] and d['status'] == 'pending' for d in self._all('decisions'))):
            raise ServiceError(409, 'This Work item has another execution. Continue its current owner.')
        self._carry_work_history(item, dict(item))
        item.update(task_id=owner['id'], _native_outcome_id=owner['_native_outcome_id'])
        self._put('artifacts', item)
        self._event('artifact.updated', self._public(item), item['bot_id'], owner['id'])

    def continue_native_work(self, source_id, payload):
        from .service import ServiceError, now, uid, digest, encoded, require_text
        from .work_capacity import execution_key
        with self.transaction():
            source = self._get('tasks', source_id)
            if source['status'] != 'working' or self._task_paused(source):
                raise ServiceError(409, 'This task cannot continue work now.')
            identity = require_text(payload, 'task_id', 100)
            text = require_text(payload, 'message', 16000)
            key = digest(encoded(['continue_work', source_id, source.get('_native_turn_id'), identity, text]))
            previous = self.db.execute('SELECT data FROM deliveries WHERE id=?', (key,)).fetchone()
            if previous:
                return json.loads(previous[0])['receipt']
            owners = [t for t in self._all('tasks') if t.get('_native_outcome_id') == identity]
            if len(owners) != 1 or not self.kanban:
                raise ServiceError(409, 'Choose one existing native outcome from kanban_show.')
            owner = owners[0]
            if source['bot_id'] not in (owner['bot_id'], owner.get('source_bot_id')):
                raise ServiceError(403, 'Only the owner or its requesting bot can continue this work.')
            bot = self._get('bots', owner['bot_id'])
            if (self._task_paused(owner) or owner['session_id'] != bot['session_id']
                    or owner.get('_conversation_epoch') != self._setting('conversation_epoch')
                    or self._continuation_blocked(owner)
                    or self._get('exchanges', owner['exchange_id']).get('cancelled')
                    or self._task_work_is_paused(owner)):
                raise ServiceError(409, 'This work is paused, cancelled, or belongs to an earlier conversation.')
            native = self.kanban.tasks.get(owner['id'], {})
            if native.get('id') != identity or native.get('status') not in ('triage', 'blocked', 'done'):
                raise ServiceError(409, 'This native outcome is not blocked or completed. Read its current result or activity.')
            if owner['status'] not in ('completed', 'failed', 'waiting') or owner.get('_waiting_on'):
                raise ServiceError(409, 'Wait for this task and its existing dependencies.')
            if owner.get('_return_pending'):
                raise ServiceError(409, 'Wait for the previous result to be delivered before continuing this work.')
            related = [t for t in self._all('tasks') if execution_key(t) == execution_key(owner)]
            if any((t['id'] != owner['id'] and t['status'] in self.ACTIVE)
                   or self._background_pending(t['id']) for t in related):
                raise ServiceError(409, 'This execution has active work. Wait for its result.')
            if any(d['status'] == 'pending' and d['task_id'] in {t['id'] for t in related}
                   for d in self._all('decisions')):
                raise ServiceError(409, 'The existing decision must be answered before work continues.')
            self._restore_native_work_owner(owner)
            related_ids = {t['id'] for t in related}
            recovery = owner.setdefault('_self_recovery', {})
            recovery['previous_task_id'] = owner['id']
            unknown = [r for identity in related_ids for r in self._native_task_receipts(identity, state='executing')]
            recovery['unknown_tools'] = list({r['id']: r for r in recovery.get('unknown_tools', []) + unknown}.values())
            decisions = [self._public(d) for d in self._all('decisions') if d['task_id'] in related_ids]
            recovery['decisions'] = list({d['id']: d for d in recovery.get('decisions', []) + decisions}.values())
            completed = []
            if owner['status'] == 'failed':
                completed = owner.get('_continuation_receipt_ids', []) + [r['id'] for r in
                    self._native_task_receipts(owner['id'], state='completed') if r.get('turn_id') == owner.get('_native_turn_id')]
            pending = owner.pop('_blocked_followup', None)
            if pending:
                for field, source_key in (('_preference_source_ids', 'source_ids'), ('_attachment_ids', 'attachment_ids')):
                    owner[field] = list(dict.fromkeys(owner.get(field, []) + pending[source_key]))
                owner.update(_steering=[], steering_status='continued')
            # A failed provider may never have received the selected human input.
            retry_input = {field: owner[field] for field in
                ('_preference_source_ids', '_background_completion', '_pr_review') if field in owner}
            if owner['status'] == 'failed' or pending:
                retry_input['_input_after_message_id'] = owner.get('_input_after_message_id')
            else:
                retry_input = {}
            self._advance_input(owner)
            owner.update(retry_input)
            owner['_continuation_receipt_ids'] = list(dict.fromkeys(completed))
            owner.setdefault('_bot_messages', []).append(dict(sender_bot_id=source['bot_id'],
                sender_name=self._get('bots', source['bot_id'])['name'], text=text,
                result_status=None, progress_only=False))
            owner.update(status='queued', summary='Continuing with the new information.', updated_at=now(),
                _background_return_after=now(), _background_return_id=uid())
            self._put('tasks', owner)
            receipt = dict(task_id=owner['id'], native_task_id=identity, status='queued')
            self._put('deliveries', dict(id=key, source_task_id=source_id, target_bot_id=owner['bot_id'],
                text=text, receipt=receipt, created_at=now()))
            self._event('task.updated', self._public(owner), owner['bot_id'], owner['id'])
            self._refresh_bot(owner['bot_id'])
            return receipt

    def _migrate_native_work(self):
        if not self.kanban or not getattr(self.provider, 'supports_native_outcomes', False):
            return
        from .service import now
        candidates = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM artifacts WHERE json_extract(data,'$.kind')='work' "
            "AND json_extract(data,'$._native_outcome_id') IS NULL "
            "AND json_extract(data,'$.status') NOT IN ('completed','cancelled') "
            "AND json_extract(data,'$._same_outcome_as') IS NULL "
            "AND coalesce(json_extract(data,'$._hidden'),0)=0")]
        for item in candidates:
            owner = self._get('tasks', item['task_id'])
            if (owner['status'] in ('working', 'cancelled') or owner.get('_assignment_task_id')
                    or self._task_paused(owner) or self._automatic_work_is_paused(owner) or self._work_pause(item)
                    or owner.get('_native_outcome_id') or self._external_work_routine(item)
                    or sum(other['task_id'] == owner['id'] for other in candidates) != 1
                    or self._setting('work.cancelled.' + self._work_identity(item))):
                continue
            peers = [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM artifacts WHERE json_extract(data,'$._same_outcome_as')=? "
                "OR json_extract(data,'$._work_parent_id')=?", (item['id'], item['id']))]
            if any(self._external_work_routine(peer) for peer in peers):
                continue
            bot = self._get('bots', owner['bot_id'])
            if not self._process_observation_current(owner, bot):
                continue
            pending = any(d['status'] == 'pending' and d['task_id'] == owner['id'] for d in self._all('decisions'))
            waiting = pending or self._background_pending(owner['id']) or bool(owner.get('_waiting_on'))
            waiting = waiting or owner['status'] in ('scheduled', 'paused')
            tasks = {t['id']: t for t in self._all('tasks')}
            records = self._work_receipts()
            attempts = {owner['id'], *(peer['task_id'] for peer in peers)}
            for previous in [item, *peers]:
                attempts.update(previous.get('_work_previous_task_ids', []))
            for task_id in tuple(attempts):
                if task_id in tasks:
                    attempts.update(self._work_chain(tasks[task_id], tasks, records, include_continuations=True))
            attempts.update(t['id'] for t in tasks.values() if t.get('_assignment_task_id') in attempts)
            related = attempts - {owner['id']}
            if (any(tasks[i]['status'] in self.ACTIVE or self._background_pending(i) for i in related if i in tasks)
                    or any(d['task_id'] in related and d['status'] == 'pending' for d in self._all('decisions'))
                    or any(c['task_id'] in attempts and c['status'] in ('queued', 'working') for c in self._all('contexts'))):
                continue
            unknown = list({r['id']: r for r in (owner.get('_self_recovery') or {}).get('unknown_tools', [])
                + [r for r in records if r['task_id'] in attempts and r['state'] == 'executing']}.values())
            if waiting and unknown:
                continue
            state = 'review' if pending else 'scheduled' if waiting else 'ready'
            self._ensure_execution_session(owner)
            if not owner.get('_native_turn_id'):
                self._advance_input(owner)
            row = self._kanban_row(owner, owner, state, priority=10)
            row.update(title=item['title'], work_id=item['id'], body=json.dumps(dict(
                outcome=item['title'], acceptance=item.get('completion_condition'),
                last_report=item.get('activity'), request=owner.get('title'),
                instructions='Continue the saved outcome within its recorded approvals. '
                    'Read the existing conversation, decisions, and receipts before acting. '
                    'Verify unknown tool results before repeating an action. '
                    'Use kanban_complete only after verifying the accepted outcome.'), ensure_ascii=False))
            identity = self.kanban.call({'op': 'promote', 'row': row})['id']
            if not identity:
                continue
            owner.update(_native_outcome_id=identity, _work_context_id=item['id'])
            if unknown:
                owner['_self_recovery'] = dict(owner.get('_self_recovery') or {},
                    previous_task_id=owner['id'], failure=owner.get('summary'), unknown_tools=unknown)
            if not waiting and owner['status'] in ('completed', 'failed', 'waiting'):
                self._advance_input(owner)
                owner.update(status='queued', summary='Checking the saved result', updated_at=now())
            item['_native_outcome_id'] = identity
            self._put('tasks', owner)
            self._put('artifacts', item)
            self._event('task.updated', self._public(owner), owner['bot_id'], owner['id'])
            self._event('artifact.updated', self._public(item), item['bot_id'], owner['id'])

    def _dispatch_chat(self, capacity):
        from .work_capacity import execution_key
        active = {execution_key(self._get('tasks', i)) for i in self.running}
        active.update(execution_key(self._get('tasks', self._get('contexts', i)['task_id'])) for i in self.context_threads)
        # One foreground slot is separate from native work capacity.
        if any(self._get('tasks', i).get('_chat_session') or self._get('tasks', i).get('_work_status_only') for i in self.running):
            return
        for task in self._dispatch_order(self._all('tasks')):
            if not task.get('_chat_session') or execution_key(task) in active:
                continue
            bot = self._get('bots', task['bot_id'])
            if bot['archived'] or bot.get('_deleted') or not self._dispatch_eligible(task, time.time()):
                continue
            worker = threading.Thread(target=self._run_task, args=(task['id'],), daemon=True, name='conversation-' + task['id'][:8])
            self.running[task['id']] = worker
            active.add(execution_key(task))
            worker.start()
            return

    def _adopt_native_outcomes(self):
        from .service import now
        bindings = []
        with self.transaction():
            for native in self.kanban.tasks.values():
                if not native.get('unbound'):
                    continue
                # The origin is supplied by Coconut's native tool middleware, not by model text.
                origin_id = native.get('session_id')
                row = self.db.execute('SELECT data FROM tasks WHERE id=?', (origin_id,)).fetchone()
                if not row:
                    continue
                origin = json.loads(row[0])
                if native.get('tenant') != origin['bot_id']:
                    continue
                identity = str(uuid5(UUID(origin_id), 'kanban:' + native['id']))
                if not self.db.execute('SELECT 1 FROM tasks WHERE id=?', (identity,)).fetchone():
                    receipt = self.db.execute(
                        "SELECT data FROM native_receipts WHERE json_extract(data,'$.task_id')=? "
                        "AND json_extract(data,'$.action.name')='kanban_create' "
                        "AND json_extract(data,'$.action.arguments.idempotency_key')=? LIMIT 1",
                        (origin_id, native.get('idempotency_key'))).fetchone()
                    attachments = json.loads(receipt[0]).get('_attachment_ids', []) if receipt else []
                    task = dict(id=identity, thread_id=identity, parent_task_id=origin_id,
                        bot_id=origin['bot_id'], conversation_bot_id=origin['conversation_bot_id'],
                        session_id=origin['session_id'], source_bot_id=origin.get('source_bot_id'),
                        source_task_id=origin.get('source_task_id'),
                        exchange_id=self._new_exchange(), exchange_depth=origin.get('exchange_depth', 0),
                        title=native['title'], status='queued', summary='Queued', scheduled_at=None,
                        created_at=now(), updated_at=now(), _attachment_ids=attachments,
                        _execution_session_id=identity, _native_outcome_id=native['id'])
                    callback = origin.get('_relay_callback_owner') or (origin_id if origin.get('source_bot_id') else None)
                    if callback:
                        task['_relay_callback_owner'] = callback
                        task['_return_dependency_id'] = origin.get('_return_dependency_id', origin_id)
                    if origin.get('_reply_to_task_id'):
                        task['_reply_to_task_id'] = origin['_reply_to_task_id']
                    artifact_id = str(uuid5(UUID(identity), 'work'))
                    task['_work_context_id'] = artifact_id
                    self._put('tasks', task)
                    item = dict(id=artifact_id, bot_id=task['bot_id'], task_id=identity, kind='work',
                        title=native['title'], status='queued', activity='Queued', preview='', url=None,
                        created_at=now(), observed_at=now(), _work_key='native-' + native['id'],
                        _native_outcome_id=native['id'], completion_condition=native['title'])
                    self._put('artifacts', item)
                    self._event('task.created', self._public(task), task['bot_id'], identity)
                    self._event('artifact.created', self._public(item), task['bot_id'], identity)
                bindings.append(dict(op='bind', id=native['id'], external_id=identity))
        for binding in bindings:
            self.kanban.call(binding)
        if bindings:
            self.kanban.call({'op': 'snapshot'})

    def _native_retry(self, task, native):
        from .service import now, uid
        if (task['status'] not in ('completed', 'failed') or native['status'] != 'ready'
                or self._task_paused(task) or self._automatic_work_is_paused(task)
                or any(d['task_id'] == task['id'] and d['status'] == 'pending' for d in self._all('decisions'))):
            return
        records = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM native_receipts WHERE json_extract(data,'$.task_id')=? "
            "AND json_extract(data,'$.state')='executing'", (task['id'],))]
        previous = task.get('_self_recovery') or {}
        records = list({r['id']: r for r in previous.get('unknown_tools', []) + records}.values())
        self._advance_input(task)
        task.update(status='queued', summary='Checking the previous result', updated_at=now(),
            _background_return_after=now(), _background_return_id=uid(),
            _self_recovery=dict(previous, attempt=native.get('consecutive_failures', 0), previous_task_id=task['id'],
                failure=native.get('last_failure_error'), unknown_tools=records,
                work=previous.get('work', []), decisions=previous.get('decisions', [])))
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])

    def _native_work_view(self, artifacts):
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc)
        groups, legacy = {}, []
        for item in artifacts:
            if item['kind'] not in ('work', 'pr'):
                continue
            root = self._work_outcome_artifact(item)
            if not root.get('_native_outcome_id'):
                legacy.append(item)
                continue
            if item.get('work_outcome_id'):
                item['work_outcome_id'] = root['id']
            groups.setdefault(root['id'], (root, []))[1].append(item)
        if groups:
            identities = {root['task_id'] for root, _ in groups.values()} - {None}
            identities.update(n['external_id'] for n in self.kanban.tasks.values())
            tasks = {t['id']: t for t in (json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM tasks WHERE id IN (SELECT value FROM json_each(?))', (json.dumps(sorted(identities)),)))}
            decisions = [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending' "
                "AND NOT coalesce(json_extract(data,'$._hidden'),0) AND NOT coalesce(json_extract(data,'$._deleted'),0)")]
            reports = {}
            for row in self.db.execute(
                    "SELECT data FROM artifacts WHERE json_extract(data,'$.kind')='pr' "
                    "AND NOT coalesce(json_extract(data,'$._hidden'),0)"):
                linked = json.loads(row[0])
                root_id = self._work_outcome_artifact(linked)['id']
                if root_id in groups:
                    reports.setdefault(root_id, []).append(linked)
            positions = self._native_queue_positions(tasks, timestamp.timestamp())
            for root, peers in groups.values():
                outcome = self._native_outcome_projection(root, tasks, [], decisions, timestamp, positions,
                    peers + reports.get(root['id'], []))
                for item in peers:
                    item.update(outcome)
                    if item['kind'] == 'work':
                        item['status'] = outcome['activity_state']
                    item['work_dismissal_id'] = self._setting('work.dismissed.' + self._work_identity(root))
        # Retained work must finish migration with its original waits and decisions.
        if legacy:
            self._project_work_outcomes(legacy, {t['id']: t for t in self._all('tasks')},
                self._pending_completion_receipts(), self._all('decisions'), timestamp)
        return artifacts

    def _native_outcome_projection(self, item, tasks, records, decisions, timestamp, queue_positions, peers=None):
        native = next((n for n in self.kanban.tasks.values() if n['id'] == item['_native_outcome_id']), None) if self.kanban else None
        owner = tasks.get(item.get('task_id'), {})
        peers = peers or [item]
        owners = {t['id'] for t in tasks.values() if t.get('_native_outcome_id') == item['_native_outcome_id']}
        owners.add(item.get('task_id'))
        pending = [d for d in decisions if d['status'] == 'pending' and (d['task_id'] in owners or any(
            p['kind'] == 'pr' and d.get('merge_pr_url') == p.get('url') for p in peers))]
        status = native['status'] if native else None
        state = {'ready': 'queued', 'running': 'working', 'done': 'completed', 'archived': 'cancelled'}.get(status, 'waiting')
        activity = {'queued': 'Queued', 'working': 'In progress', 'completed': 'Completed', 'cancelled': 'Stopped'}.get(state, 'Waiting for a result')
        if native is None:
            activity = 'Status unavailable'
        terminal = state in ('completed', 'cancelled')
        if pending and not terminal:
            state, activity = 'needs_decision', 'Waiting for your decision'
        if self._setting('work.cancelled.' + self._work_identity(item)):
            state, activity = 'cancelled', 'Stopped'
        if native and state == 'waiting':
            activity = native.get('outcome_summary') or native.get('last_failure_error') or activity
        if native and state == 'working':
            activity = native.get('progress_note') or activity
        return dict(title=native['title'] if native else item['title'],
            work_state=state, activity=activity, work_next_step='Review decision' if state == 'needs_decision' else None,
            work_decision_id=pending[0]['id'] if state == 'needs_decision' else None,
            work_queue_position=(queue_positions or {}).get(owner.get('id')) if state == 'queued' else None,
            completion_condition=item.get('completion_condition') or item['title'],
            completion_evidence=native.get('outcome_summary') if native and state == 'completed' else None,
            work_attempts=[dict(id=identity, state=tasks[identity]['status']) for identity in sorted(owners) if identity in tasks], work_dependencies=[],
            activity_bot_id=owner.get('bot_id', item['bot_id']), activity_observed_at=timestamp.isoformat(),
            activity_state={'needs_decision': 'waiting', 'queued': 'waiting'}.get(state, state))
