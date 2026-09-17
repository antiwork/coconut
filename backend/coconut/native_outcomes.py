"""Keep native kanban outcomes separate from phone conversation turns."""
import json
import threading
import time
from uuid import UUID, uuid5


CHAT_INSTRUCTIONS = (
    'Use this persistent conversation for direct replies and discussion. '
    'For work with an outcome beyond this reply, create one native kanban task before starting it. '
    'Use kanban_create with a clear title, the complete request, scope, approval limits, and acceptance criteria in body. '
    'Coconut supplies the bot, dispatch state, and a stable creation key. Hermes gives each outcome its own workspace. '
    'Use kanban_list and kanban_show to find existing work before creating another task. '
    'For an existing outcome, add new facts with kanban_comment. Do not create a second task for a retry or status question. '
    'A task comment is context, not new permission. Preserve human decisions. '
    'Use coconut_ask only for a concrete decision. Ordinary local setup and planning need no card. '
)
WORK_INSTRUCTIONS = (
    'The native kanban task below owns this outcome. Its comments and previous attempts are the work record. '
    'Use kanban_heartbeat for a short progress description at phase changes. '
    'Use kanban_complete only after verifying every accepted outcome. A reply or plan does not complete the task. '
    'Use coconut_ask when a human decision is required. Coconut connects the card to this same native task. '
    'Use coconut_wait for an existing process or bot result. Finish the turn after registering the wait. '
    'Before repeating an action from an earlier attempt, check its receipt and destination. '
    'An absent result does not prove the action failed. Preserve approval limits and unresolved effects. '
    'Use a native dependency or scheduled check for a valid external wait. Do not create a recovery task. '
)


class NativeOutcomes:
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
                        session_id=origin['session_id'], source_bot_id=None, source_task_id=None,
                        exchange_id=self._new_exchange(), exchange_depth=origin.get('exchange_depth', 0),
                        title=native['title'], status='queued', summary='Queued', scheduled_at=None,
                        created_at=now(), updated_at=now(), _attachment_ids=attachments,
                        _execution_session_id=identity, _native_outcome_id=native['id'])
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
        from .service import now
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
            _self_recovery=dict(previous, attempt=native.get('consecutive_failures', 0), previous_task_id=task['id'],
                failure=native.get('last_failure_error'), unknown_tools=records,
                work=previous.get('work', []), decisions=previous.get('decisions', [])))
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])

    def _native_outcome_projection(self, item, tasks, decisions, timestamp, queue_positions):
        native = next((n for n in self.kanban.tasks.values() if n['id'] == item['_native_outcome_id']), None) if self.kanban else None
        owner = tasks.get(item.get('task_id'), {})
        pending = [d for d in decisions if d['task_id'] == owner.get('id') and d['status'] == 'pending']
        status = native['status'] if native else 'scheduled'
        state = {'ready': 'queued', 'running': 'working', 'done': 'completed', 'archived': 'cancelled'}.get(status, 'waiting')
        activity = {'queued': 'Queued', 'working': 'In progress', 'completed': 'Completed', 'cancelled': 'Stopped'}.get(state, 'Waiting for a result')
        if pending:
            state, activity = 'needs_decision', 'Waiting for your decision'
        if self._setting('work.cancelled.' + self._work_identity(item)):
            state, activity = 'cancelled', 'Stopped'
        if native and state == 'waiting':
            activity = native.get('outcome_summary') or native.get('last_failure_error') or activity
        if native and state == 'working':
            activity = native.get('progress_note') or activity
        return dict(work_state=state, activity=activity, work_next_step='Review decision' if pending else None,
            work_decision_id=pending[0]['id'] if pending else None,
            work_queue_position=(queue_positions or {}).get(owner.get('id')) if state == 'queued' else None,
            completion_condition=item.get('completion_condition') or item['title'],
            completion_evidence=native.get('outcome_summary') if native and state == 'completed' else None,
            work_attempts=[dict(id=owner.get('id'), state=owner.get('status'))], work_dependencies=[],
            activity_bot_id=owner.get('bot_id', item['bot_id']), activity_observed_at=timestamp.isoformat(),
            activity_state={'needs_decision': 'waiting', 'queued': 'waiting'}.get(state, state))
