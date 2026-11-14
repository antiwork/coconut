"""Keep delegated work distinct from a completed Hermes reply."""
import copy
import json
import re
import threading

ROOT_ID = 'coconut-assignment'
WORK_INSTRUCTIONS = (
    'Track this assignment with the native todo_list tool. The coconut-assignment item represents the whole request. '
    'Keep that item until the assignment ends. Add steps for every requested outcome, including validation and authorized publishing. '
    'Track only this assignment\'s authorized scope. For a review or status request, complete the root after delivering the requested findings and limits. '
    'Do not extend that assignment to include the requester\'s later approval, publication, or merge unless they assigned those actions to you. '
    'Mark coconut-assignment completed with todo_list when all assigned outcomes are verified. '
    'Mark work completed only after verifying the outcome. A plan, status reply, or handoff is not completion. '
    'Keep blocked or delegated steps pending. Launch a real process or message_agent handoff before saying work is running or queued. '
    'If another existing task owns the next step, use coconut_wait to record that dependency, then finish this turn. '
    'Call coconut_wait without arguments to list the tasks and decisions in this exchange. '
    'For an existing human decision in this exchange, use decision_id to wait for its answer without waiting for its owner task. '
    'Do not start a duplicate review or watcher. '
    'Do not add later work outside this assignment to its task list. The completed coconut-assignment item closes this assignment. '
    'Continue independent authorized work. Use coconut_ask if a human decision is required. '
    'Cancel an item only when the person cancels or changes its scope. Preserve all existing approval limits and skill requirements. '
    'Coconut retains unfinished items and continues the assignment after a reply ends. '
)


def snapshot(value):
    if not isinstance(value, dict) or not isinstance(value.get('todos'), list) or len(value['todos']) > 256:
        raise ValueError('Invalid native task list.')
    items = value['todos']
    if any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
           or not isinstance(item.get('content'), str) or len(item['content']) > 4000
           or item.get('status') not in ('pending', 'in_progress', 'completed', 'cancelled') for item in items):
        raise ValueError('Invalid native task item.')
    return {'todos': copy.deepcopy(items), 'revision': max(0, int(value.get('revision', 0)))}


def work_enabled(operation, task):
    """One predicate decides native todo restoration and the dependency prompt for a turn."""
    if task.get('_native_intake') and not task.get('_work_todos') and not task.get('_work_context_id'):
        return False
    return operation != 'context' and not task.get('_native_outcome_id') and not task.get('_pr_review') and bool(
        task.get('_work_todos') or (task.get('source_bot_id') and not task.get('_is_return'))
        or task.get('_handoff_assignment', {}).get('return_to_bot_id'))


class NativeWork:
    def __init__(self, request):
        task = request['task']
        self.enabled = work_enabled(request['operation'], task)
        self.capture = request['operation'] != 'context' and not task.get('_native_outcome_id') and not task.get('_pr_review')
        if task.get('_native_intake') and not self.enabled:
            self.capture = False
        self.saved = task.get('_work_todos')
        self.request = (task.get('_handoff_assignment', {}).get('request')
                        or '\n'.join(m['text'] for m in task.get('_bot_messages', []) if m.get('text'))
                        or task['title'])[:4000]

    def restore(self, agent):
        if self.enabled:
            state = snapshot(self.saved) if self.saved else self.initial()
            agent._todo_store.restore(state['todos'], revision=state['revision'])

    def initial(self):
        return {'todos': [{'id': ROOT_ID, 'content': self.request, 'status': 'pending'}], 'revision': 1}

    def payload(self, agent):
        if not self.capture or agent is None or not hasattr(agent, '_todo_store'):
            return {}
        return self.from_state(snapshot(agent._todo_store.snapshot()))

    def from_state(self, state, *, inferred_root=False):
        """The saved work of a todo list (a snapshot): the assignment root comes first. {} when nothing to save.

        With `inferred_root`, the list never holds the root: Coconut adds it, as for the gateway, whose Hermes list
        Coconut does not change. The root is then completed when every step is completed or cancelled. The worker
        restores the root into the agent's list, so its list keeps the root.
        """
        if not self.capture:
            return {}
        if inferred_root and state['todos'] and not any(t['id'] == ROOT_ID for t in state['todos']):
            saved = next((t for t in (self.saved or {}).get('todos', []) if t['id'] == ROOT_ID), None)
            root = dict(saved or self.initial()['todos'][0])
            root['status'] = ('completed' if all(t['status'] in ('completed', 'cancelled') for t in state['todos'])
                              else 'pending' if root['status'] in ('completed', 'cancelled') else root['status'])
            state['todos'] = [root] + state['todos'][:255]
            return {'_work_todos': state}
        if not self.enabled:
            if not state['todos']:
                return {}
            # Adopt a checklist Hermes actually created; a reply alone creates no assignment.
            if not any(t['id'] == ROOT_ID for t in state['todos']):
                root = self.initial()['todos'][0]
                if all(t['status'] in ('completed', 'cancelled') for t in state['todos']):
                    root['status'] = 'completed'
                state['todos'] = [root] + state['todos'][:255]
            return {'_work_todos': state}
        if not any(t['id'] == ROOT_ID for t in state['todos']):
            state['todos'] = self.initial()['todos'] + state['todos'][:255]
        return {'_work_todos': state}


class WorkProgress:
    def _native_wait_cycle(self, task_id, target_id):
        if not self.kanban:
            return False
        snapshots = self.kanban.tasks
        source = snapshots.get(task_id, {}).get('id')
        target = snapshots.get(target_id, {})
        if not source or not target:
            return False
        native = {row['id']: row for row in snapshots.values()}
        pending, visited = [target['id']], set()
        while pending:
            identity = pending.pop()
            if identity == source:
                return True
            if identity in visited:
                continue
            visited.add(identity)
            pending.extend(parent['id'] for parent in native.get(identity, {}).get('dependencies', []))
        return False

    @staticmethod
    def _unfinished_work(task):
        if task.get('_native_outcome_id'):
            return False
        items = task.get('_work_todos', {}).get('todos', [])
        root = next((t for t in items if t['id'] == ROOT_ID), None)
        if root and root['status'] in ('completed', 'cancelled'):
            return False
        return any(t['status'] in ('pending', 'in_progress') for t in items)

    def _exchange_peers(self, task):
        """Read tasks in this exchange and the assignment's recorded Work history."""
        tasks = self._task_map()
        linked = set()
        assignment = self._assignment(task)
        records = self._shared_pending_receipts()
        linked.update(self._work_chain(assignment, tasks, records, include_continuations=True))
        work_id = task.get('_work_context_id') or assignment.get('_work_context_id')
        if work_id:
            item = self._canonical_work(self._get('artifacts', work_id))
            if not item.get('_hidden') and not self._setting('work.cancelled.' + self._work_identity(item)):
                identities = [item.get(key) for key in ('task_id', '_work_origin_task_id', '_review_task_id', '_activity_task_id')]
                recovery = task.get('_self_recovery') or assignment.get('_self_recovery') or {}
                identities += [recovery.get('previous_task_id')] + [t['id'] for t in recovery.get('saved_tasks', [])]
                for identity in identities + item.get('_work_previous_task_ids', []):
                    if identity in tasks:
                        linked.update(self._work_chain(tasks[identity], tasks, records, include_continuations=True))
        # A manual routine run returns its own exchange. Its saved receipt links the caller.
        callers = linked | {t['id'] for t in tasks.values()
                            if (t.get('_assignment_task_id') or t['id']) == assignment['id']}
        rows = self.db.execute("SELECT data FROM native_receipts WHERE json_extract(data, '$.task_id') IN "
            "(SELECT value FROM json_each(?)) AND json_extract(data, '$.state')='completed' "
            "AND json_extract(data, '$.action.name')='cronjob_manage' ORDER BY rowid", (json.dumps(sorted(callers)),))
        for row in rows:
            record = json.loads(row[0])
            action = record.get('action', {})
            arguments = action.get('arguments', {})
            if (record['task_id'] not in callers or record['state'] != 'completed'
                    or action.get('name') != 'cronjob_manage'
                    or str(arguments.get('action', '')).strip().lower() not in ('run', 'run_now', 'trigger')):
                continue
            try:
                result = json.loads(record.get('result', '{}'))
            except (TypeError, ValueError):
                continue
            target = tasks.get(result.get('task_id')) if isinstance(result, dict) else None
            if (target and result.get('success') is True and target['bot_id'] == task['bot_id']
                    and target.get('_native_job', {}).get('id') == arguments.get('job_id')):
                linked.add(target['id'])
        return {t['id']: t for t in tasks.values()
                if t['id'] != task['id'] and (t['exchange_id'] == task['exchange_id'] or t['id'] in linked)
                and t['conversation_bot_id'] == task['conversation_bot_id']
                and not self._get('bots', t['bot_id']).get('_deleted')
                and t['session_id'] == self._get('bots', t['bot_id'])['session_id']
                and (t.get('_conversation_epoch') == task.get('_conversation_epoch')
                     or (t['status'] == 'queued' and not t.get('_native_turn_id') and '_conversation_epoch' not in t))}

    def wait_for_task(self, task_id, payload):
        from .service import ServiceError, digest, encoded, now, require_text
        if self.kanban and isinstance(payload.get('task_id'), str):
            # Dispatch callbacks acquire the service lock while holding the adapter lock.
            self.kanban.call({'op': 'snapshot', 'external_ids': [task_id, payload['task_id']]})
        # Inside _shared_reads the task map is the kept one, not a parse of the whole table under the service lock. Its
        # objects are shared and only read here. The writes below read their rows with _get.
        with self.transaction(), self._shared_reads():
            task = self._get('tasks', task_id)
            if task['status'] != 'working' or self._task_paused(task) or self.stopping.is_set():
                raise ServiceError(409, 'This task cannot register a wait now.')
            if payload.get('no_card'):
                return self.record_no_card(task, payload)
            peers = self._exchange_peers(task)
            process_owners = peers | {task_id: task}
            decisions = {d['id']: d for d in (json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM decisions WHERE json_extract(data,'$.task_id') IN (SELECT value FROM json_each(?)) ORDER BY rowid",
                (json.dumps(sorted(process_owners)),)))}
            if sum(bool(payload.get(key)) for key in ('task_id', 'decision_id')) > 1:
                raise ServiceError(400, 'Choose one task or decision.')
            if payload.get('decision_id'):
                identity = require_text(payload, 'decision_id', 100)
                reason = require_text(payload, 'reason', 80)
                if identity not in decisions or '\n' in reason:
                    raise ServiceError(400, 'Choose a decision from this exchange and give one short reason.')
                decision = decisions[identity]
                if not self._decision_wait_pending(decision):
                    return {'status': 'completed', 'decision_id': identity, 'result': self._decision_wait_result(decision)}
                receipt_id = digest(json.dumps([task_id, task.get('_native_turn_id'), 'decision', identity]))
                if not self.db.execute('SELECT 1 FROM native_receipts WHERE id=?', (receipt_id,)).fetchone():
                    action = {'name': 'coconut_wait', 'arguments': {'decision_id': identity, 'reason': reason}}
                    self._put('native_receipts', dict(id=receipt_id, task_id=task_id, turn_id=task.get('_native_turn_id'),
                        action=action, action_hash=digest(encoded(action)), state='completed', result='{}',
                        created_at=now(), completed_at=now(), _wait_decision_id=identity,
                        _wait_session_id=process_owners[decision['task_id']]['session_id']))
                return {'status': 'waiting', 'decision_id': identity, 'reason': reason}
            if not payload.get('task_id'):
                return {'decisions': [{k: d.get(k) for k in ('id', 'task_id', 'title', 'status')} for d in decisions.values()],
                        'tasks': [{k: t.get(k) for k in ('id', 'bot_id', 'title', 'status')} for t in peers.values()]}
            target_id = require_text(payload, 'task_id', 100)
            reason = require_text(payload, 'reason', 80)
            if target_id not in peers or '\n' in reason:
                raise ServiceError(400, 'Choose a task from this exchange and give one short reason.')
            target = peers[target_id]
            tasks = self._task_map()
            records = self._shared_pending_receipts()
            if (task_id in self._work_chain(target, tasks, records, include_continuations=True)
                    or self._native_wait_cycle(task_id, target_id)):
                raise ServiceError(409, 'This task already depends on your result. Do not create a circular wait.')
            assignment = self._assignment(task)
            # The assignment of a task is the task that its _assignment_task_id names, else the task itself.
            turn_ids = {t['id'] for t in tasks.values() if (t.get('_assignment_task_id') or t['id']) == assignment['id']}
            existing_dependency = (target_id in assignment.get('_waiting_on', [])
                or any(r['task_id'] in turn_ids and r.get('_wait_task_id') == target_id
                       and not r.get('_process_delivery') for r in records))
            if existing_dependency:
                assignment['_explicit_work_waits'] = list(dict.fromkeys(assignment.get('_explicit_work_waits', []) + [target_id]))
                self._put('tasks', assignment)
                return {'status': 'waiting', 'task_id': target_id, 'reason': reason}
            # A saved wait would only resume a new turn with this same result, and that turn can wait again.
            result = self._final_task_result(target)
            if result:
                return {'status': 'completed', 'task_id': target_id, 'result': result}
            identity = digest(json.dumps([task_id, task.get('_native_turn_id'), target_id]))
            existing = self.db.execute('SELECT 1 FROM native_receipts WHERE id=?', (identity,)).fetchone()
            if not existing:
                action = {'name': 'coconut_wait', 'arguments': {'task_id': target_id, 'reason': reason}}
                self._put('native_receipts', dict(id=identity, task_id=task_id, turn_id=task.get('_native_turn_id'),
                    action=action, action_hash=digest(encoded(action)),
                    state='completed', result='{}', created_at=now(), completed_at=now(),
                    _wait_task_id=target_id, _wait_session_id=target['session_id']))
            return {'status': 'waiting', 'task_id': target_id, 'reason': reason}

    @staticmethod
    def _decision_wait_pending(decision):
        # Inbox finalizes a swipe before any worker can act on the dismissal.
        return decision['status'] == 'pending' or (decision['status'] == 'dismissed' and bool(decision.get('inbox_undo_until')))

    @staticmethod
    def _decision_wait_result(decision):
        return ('Existing decision changed. Apply only its recorded scope and selected option. '
                'A dismissal or a request for changes does not approve publication.\n' + json.dumps(
                    {key: decision.get(key) for key in ('id', 'title', 'status', 'selected_option_id',
                                                       'selected_option_label', 'correction', 'context')}, ensure_ascii=False))

    def work_dependencies(self, task):
        """Expose existing owners before a bot can start duplicate work."""
        # A listing is read-only. The wait-registration guard applies only to an explicit coconut_wait.
        with self.lock:
            tasks = self._task_map()
            records = self._shared_work_receipts()
            dependencies = []
            # The peers whose chain holds this task, found once (see _work_reach).
            reach = self._work_reach(task['id'], tasks, records, include_continuations=True)
            for target in self._exchange_peers(task).values():
                pending = self._background_pending(target['id'])
                if target['status'] not in self.ACTIVE and not pending:
                    continue
                if target['id'] in reach:
                    continue
                dependencies.append({key: target.get(key) for key in ('id', 'bot_id', 'title')}
                                    | {'status': 'waiting' if pending and target['status'] == 'completed' else target['status']})
            return dependencies

    def _final_task_result(self, target):
        """The result of a completed task when no wait, dependency, unfinished step, or native run can change it."""
        if (target['status'] != 'completed' or self._background_pending(target['id']) or target.get('_waiting_on')
                or self._unfinished_work(target)):
            return None
        if target.get('_native_outcome_id'):
            native = self.kanban.tasks.get(target['id'], {}) if self.kanban else {}
            accepted = str(target.get('_native_turn_id') or target.get('_message_id') or target['id'])
            if (native.get('id'), native.get('status'), native.get('input_id')) != (target['_native_outcome_id'], 'done', accepted):
                return None
        return f"Task {target['id']} completed: {target['summary']}"

    def _wait_completion(self, record, task):
        decision = self._get('decisions', record['_wait_decision_id']) if record.get('_wait_decision_id') else None
        target = self._get('tasks', decision['task_id'] if decision else record['_wait_task_id'])
        target_bot = self._get('bots', target['bot_id'])
        reset = (target_bot.get('_deleted') or target['session_id'] != target_bot['session_id']
                 or target['session_id'] != record['_wait_session_id']
                 or (target.get('_conversation_epoch') is not None
                     and target.get('_conversation_epoch') != task.get('_conversation_epoch')))
        if reset:
            return 'The task you waited for is no longer in the current conversation.'
        if decision:
            return None if self._decision_wait_pending(decision) else self._decision_wait_result(decision)
        if self._native_wait_cycle(task['id'], target['id']):
            return ('This saved wait conflicts with a Hermes dependency: the target task waits for your result. '
                    'Read your saved results and finish your assigned scope, or wait for the specific decision. '
                    'Do not wait for that task to finish. This does not approve any new action.')
        if target['status'] in ('failed', 'cancelled'):
            return f"Task {target['id']} {target['status']}: {target['summary']}"
        if target['status'] == 'completed' and not self._background_pending(target['id']) and not target.get('_waiting_on'):
            return f"Task {target['id']} completed: {target['summary']}"
        return None

    def _drain_task_completions(self):
        with self.lock:
            bot_ids = {self._get('tasks', r['task_id'])['bot_id'] for r in self._pending_completion_receipts()
                       if (r.get('_wait_task_id') or r.get('_wait_decision_id')) and not r.get('_process_delivery')}
        for bot_id in bot_ids:
            with self.lock:
                bot_lock = self.bot_locks.setdefault(bot_id, threading.Lock())
            if not bot_lock.acquire(blocking=False):
                continue
            try:
                with self.transaction():
                    if self.stopping.is_set() or self.restart_pending:
                        return
                    grouped = {}
                    for record in self._pending_completion_receipts():
                        if not (record.get('_wait_task_id') or record.get('_wait_decision_id')) or record.get('_process_delivery'):
                            continue
                        task = self._get('tasks', record['task_id'])
                        if task['bot_id'] != bot_id or not self._can_resume_process(task, self._get('bots', bot_id)):
                            continue
                        text = self._wait_completion(record, task)
                        if text is None:
                            continue
                        grouped.setdefault(task['id'], []).append((record, text))
                    for task_id, completions in grouped.items():
                        self._resume_process_task(task_id, completions)
            finally:
                bot_lock.release()

    def _save_work_progress(self, task, payload):
        if '_work_todos' not in payload:
            return
        state = snapshot(payload['_work_todos'])
        assignment = self._assignment(task)
        from .service import now
        if task.get('_work_todos') != state:
            task['_work_progress_at'] = now()
        task['_work_todos'] = state
        self._put('tasks', task)
        if assignment['id'] != task['id']:
            assignment['_work_todos'] = state
            assignment['_work_progress_at'] = task.get('_work_progress_at', task['created_at'])
            self._put('tasks', assignment)

    def _continue_unfinished_work(self, task):
        from .service import now, uid
        state = task.get('_work_todos')
        if not state or task.get('_pr_review') or not self._unfinished_work(task):
            return None
        if self._task_work_is_paused(task):
            return None
        assignment = self._assignment(task)
        waiting_on = [identity for identity in assignment.get('_waiting_on', []) if identity != task.get('_resolved_dependency_id')]
        if waiting_on or self._background_pending(assignment['id'], finishing_task_id=task['id']):
            return None  # Existing durable dependencies own the next wakeup.
        if task.get('_self_recovery'):
            return 'Recovery ended without a verified outcome or a saved next step.'
        attempts = task.get('_work_retry', 0)
        if attempts >= 2:
            title = task['title']
            label = 'This follow-up' if re.search(r'(?:^|\s)(?:/|[A-Za-z]:[\\/])', title) else f'“{title}”'
            return f'{label} is unfinished. The agent could not confirm the next step. Ask for an update to check the current work.'
        epoch = task.get('_conversation_epoch')
        self._advance_input(task)
        task.update(status='queued', summary='Continuing unfinished work.', updated_at=now(),
                    _conversation_epoch=epoch, _work_retry=attempts + 1, _work_resume=True,
                    _background_return_after=now(), _background_return_id=uid())
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
        self._refresh_bot(task['bot_id'])
        return 'queued'
