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
    'Call coconut_wait without arguments to list tasks and processes in this exchange. '
    'For an existing background process, use process_id to wait for its result without waiting for its owner task. '
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


class NativeWork:
    def __init__(self, request):
        task = request['task']
        self.enabled = request['operation'] != 'context' and not task.get('_pr_review') and bool(
            task.get('_work_todos') or (task.get('source_bot_id') and not task.get('_is_return'))
            or task.get('_handoff_assignment', {}).get('return_to_bot_id'))
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
        if not self.enabled or agent is None:
            return {}
        state = snapshot(agent._todo_store.snapshot())
        if not any(t['id'] == ROOT_ID for t in state['todos']):
            state['todos'] = self.initial()['todos'] + state['todos'][:255]
        return {'_work_todos': state}


class WorkProgress:
    @staticmethod
    def _unfinished_work(task):
        items = task.get('_work_todos', {}).get('todos', [])
        root = next((t for t in items if t['id'] == ROOT_ID), None)
        if root and root['status'] in ('completed', 'cancelled'):
            return False
        return any(t['status'] in ('pending', 'in_progress') for t in items)

    def wait_for_task(self, task_id, payload):
        from .service import ServiceError, digest, encoded, now, require_text
        with self.transaction():
            task = self._get('tasks', task_id)
            if task['status'] != 'working' or self._task_paused(task) or self.stopping.is_set():
                raise ServiceError(409, 'This task cannot register a wait now.')
            peers = {t['id']: t for t in self._all('tasks')
                     if t['id'] != task_id and t['exchange_id'] == task['exchange_id']
                     and t['conversation_bot_id'] == task['conversation_bot_id']
                     and not self._get('bots', t['bot_id']).get('_deleted')
                     and t['session_id'] == self._get('bots', t['bot_id'])['session_id']
                     and (t.get('_conversation_epoch') == task.get('_conversation_epoch')
                          or (t['status'] == 'queued' and not t.get('_native_turn_id') and '_conversation_epoch' not in t))}
            from .background_processes import subscription
            processes = {}
            for record in self._all('native_receipts'):
                process_id = subscription(record)
                owner = peers.get(record['task_id'])
                if process_id and owner and owner['bot_id'] == task['bot_id']:
                    processes[process_id] = record
            if payload.get('process_id'):
                process_id = require_text(payload, 'process_id', 100)
                reason = require_text(payload, 'reason', 80)
                if payload.get('task_id') or process_id not in processes or '\n' in reason:
                    raise ServiceError(400, 'Choose one process from this exchange and give one short reason.')
                source = processes[process_id]
                identity = digest(json.dumps([task_id, source['id']]))
                existing = next((r for r in self._all('native_receipts') if r['id'] == identity), None)
                if source.get('_completion_text') or (existing and existing.get('_process_delivery')):
                    return {'status': 'completed', 'process_id': process_id,
                            'result': source.get('_completion_text') or existing.get('_completion_text', 'The result was already received.')}
                if not existing:
                    action = {'name': 'coconut_wait', 'arguments': {'process_id': process_id, 'reason': reason}}
                    self._put('native_receipts', dict(id=identity, task_id=task_id, turn_id=task.get('_native_turn_id'),
                        action=action, action_hash=digest(encoded(action)), state='completed', result='{}',
                        created_at=now(), completed_at=now(), _wait_process_receipt=source['id'],
                        _wait_session_id=peers[source['task_id']]['session_id']))
                return {'status': 'waiting', 'process_id': process_id, 'reason': reason}
            if not payload.get('task_id'):
                return {'processes': [{'process_id': identity, 'task_id': r['task_id']} for identity, r in processes.items()], 'tasks': [{k: t.get(k) for k in ('id', 'bot_id', 'title', 'status')}
                                  for t in peers.values()]}
            target_id = require_text(payload, 'task_id', 100)
            reason = require_text(payload, 'reason', 80)
            if target_id not in peers or '\n' in reason:
                raise ServiceError(400, 'Choose a task from this exchange and give one short reason.')
            target = peers[target_id]
            tasks = {t['id']: t for t in self._all('tasks')}
            if task_id in self._work_chain(target, tasks, self._all('native_receipts'), include_continuations=True):
                raise ServiceError(409, 'This task already depends on your result. Do not create a circular wait.')
            assignment = self._assignment(task)
            turn_ids = {t['id'] for t in tasks.values() if self._assignment(t)['id'] == assignment['id']}
            existing_dependency = (target_id in assignment.get('_waiting_on', [])
                or any(r['task_id'] in turn_ids and target_id in (r.get('_relay_task_id'), r.get('_wait_task_id'))
                       and not r.get('_process_delivery') for r in self._all('native_receipts')))
            if existing_dependency:
                return {'status': 'waiting', 'task_id': target_id, 'reason': reason}
            identity = digest(json.dumps([task_id, task.get('_native_turn_id'), target_id]))
            existing = next((r for r in self._all('native_receipts') if r['id'] == identity), None)
            if not existing:
                action = {'name': 'coconut_wait', 'arguments': {'task_id': target_id, 'reason': reason}}
                self._put('native_receipts', dict(id=identity, task_id=task_id, turn_id=task.get('_native_turn_id'),
                    action=action, action_hash=digest(encoded(action)),
                    state='completed', result='{}', created_at=now(), completed_at=now(),
                    _wait_task_id=target_id, _wait_session_id=target['session_id']))
            return {'status': 'waiting', 'task_id': target_id, 'reason': reason}

    def work_dependencies(self, task):
        """Expose existing owners before a bot can start duplicate work."""
        peers = self.wait_for_task(task['id'], {})['tasks']
        tasks = {t['id']: t for t in self._all('tasks')}
        records = self._all('native_receipts')
        dependencies = []
        for peer in peers:
            target = tasks[peer['id']]
            pending = self._background_pending(target['id'])
            if target['status'] not in self.ACTIVE and not pending:
                continue
            if task['id'] in self._work_chain(target, tasks, records, include_continuations=True):
                continue
            dependencies.append(dict(peer, status='waiting' if pending and target['status'] == 'completed' else target['status']))
        return dependencies

    def _drain_task_completions(self):
        with self.lock:
            bot_ids = {self._get('tasks', r['task_id'])['bot_id'] for r in self._all('native_receipts')
                       if (r.get('_wait_task_id') or r.get('_wait_process_receipt')) and not r.get('_process_delivery')}
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
                    for record in self._all('native_receipts'):
                        if not (record.get('_wait_task_id') or record.get('_wait_process_receipt')) or record.get('_process_delivery'):
                            continue
                        task = self._get('tasks', record['task_id'])
                        if task['bot_id'] != bot_id or not self._can_resume_process(task, self._get('bots', bot_id)):
                            continue
                        source = self._get('native_receipts', record['_wait_process_receipt']) if record.get('_wait_process_receipt') else None
                        target = self._get('tasks', source['task_id'] if source else record['_wait_task_id'])
                        target_bot = self._get('bots', target['bot_id'])
                        reset = (target_bot.get('_deleted') or target['session_id'] != target_bot['session_id']
                                 or target['session_id'] != record['_wait_session_id']
                                 or (target.get('_conversation_epoch') is not None
                                     and target.get('_conversation_epoch') != task.get('_conversation_epoch')))
                        if reset:
                            text = 'The task you waited for is no longer in the current conversation.'
                        elif source:
                            text = source.get('_completion_text')
                            if not text and source.get('_process_delivery'):
                                text = 'The process finished earlier. Its output is unavailable in this saved record. Check the existing result before repeating work.'
                            if not text:
                                continue
                        elif target['status'] in ('failed', 'cancelled'):
                            text = f"Task {target['id']} {target['status']}: {target['summary']}"
                        elif target['status'] == 'completed' and not self._background_pending(target['id']) and not target.get('_waiting_on'):
                            text = f"Task {target['id']} completed: {target['summary']}"
                        else:
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
        task['_work_todos'] = state
        self._put('tasks', task)
        if assignment['id'] != task['id']:
            assignment['_work_todos'] = state
            self._put('tasks', assignment)

    def _continue_unfinished_work(self, task):
        from .service import now, uid
        state = task.get('_work_todos')
        if not state or task.get('_pr_review') or not self._unfinished_work(task):
            return None
        assignment = self._assignment(task)
        waiting_on = [identity for identity in assignment.get('_waiting_on', []) if identity != task.get('_resolved_dependency_id')]
        if waiting_on or self._background_pending(assignment['id'], finishing_task_id=task['id']):
            return None  # Existing durable dependencies own the next wakeup.
        attempts = task.get('_work_retry', 0)
        if attempts >= 2:
            title = task['title']
            label = 'This follow-up' if re.search(r'(?:^|\s)(?:/|[A-Za-z]:[\\/])', title) else f'“{title}”'
            return f'{label} is unfinished. The bot could not confirm the next step. Ask for an update to check the current work.'
        epoch = task.get('_conversation_epoch')
        self._advance_input(task)
        task.update(status='queued', summary='Continuing unfinished work.', updated_at=now(),
                    _conversation_epoch=epoch, _work_retry=attempts + 1, _work_resume=True,
                    _background_return_after=now(), _background_return_id=uid())
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
        self._refresh_bot(task['bot_id'])
        return 'queued'
