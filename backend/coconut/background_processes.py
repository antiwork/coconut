"""Resume bot conversations from durable native process receipts."""
from __future__ import annotations

import json
import logging
import threading


def result_value(record):
    try:
        value = record.get('result', {})
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}


def subscription(record):
    result = result_value(record)
    if (record['state'] == 'completed' and record['action']['name'] == 'terminal'
            and isinstance(result, dict) and result.get('notify_on_complete') is True
            and isinstance(result.get('session_id'), str)):
        return result['session_id']
    return None


class BackgroundProcesses:
    def _migrate_process_deliveries(self):
        records = self._all('native_receipts')
        tasks = {task['id']: task for task in self._all('tasks')}
        observed = set()
        for record in records:
            value = result_value(record)
            if (record['state'] == 'completed' and record['action']['name'] in ('process', 'process_manage')
                    and isinstance(value, dict) and value.get('status') in ('exited', 'killed')
                    and tasks[record['task_id']]['status'] in ('completed', 'waiting')):
                identity = value.get('session_id') or record['action']['arguments'].get('session_id')
                observed.add((tasks[record['task_id']]['bot_id'], identity))
        for record in records:
            identity = subscription(record)
            if identity and not record.get('_process_delivery'):
                # A completed model tool call already delivered this result to a finished turn.
                if (tasks[record['task_id']]['bot_id'], identity) in observed:
                    record['_process_delivery'] = 'consumed'
                record['_process_registered'] = True
                self._put('native_receipts', record)

    def _background_pending(self, task_id, *, finishing_task_id=None):
        turns = [t for t in self._all('tasks') if t['id'] == task_id or t.get('_assignment_task_id') == task_id]
        owners = {t['id'] for t in turns}
        return (any(r['task_id'] in owners and subscription(r) and not r.get('_process_delivery')
                    for r in self._all('native_receipts'))
                or any(t['id'] != finishing_task_id and t.get('_background_completion')
                       and t['status'] in ('queued', 'working', 'needs_you', 'paused') for t in turns))

    def _poll_processes(self):
        while not self.stopping.is_set():
            try:
                self._drain_process_completions()
            except Exception:
                logging.getLogger(__name__).exception('Could not deliver a Hermes process result.')
            self.stopping.wait(1)

    def _drain_process_completions(self):
        if not hasattr(self.provider, 'process_completions'):
            return
        with self.lock:
            bot_ids = {self._get('tasks', r['task_id'])['bot_id'] for r in self._all('native_receipts')
                       if subscription(r) and not r.get('_process_delivery')}
        for bot_id in bot_ids:
            with self.lock:
                bot_lock = self.bot_locks.setdefault(bot_id, threading.Lock())
            if not bot_lock.acquire(blocking=False):
                continue
            try:
                with self.lock:
                    if self.stopping.is_set() or self.restart_pending:
                        return
                    bot = self._get('bots', bot_id)
                    if bot.get('_deleted') or bot['archived']:
                        continue
                    records = [r for r in self._all('native_receipts') if subscription(r) and not r.get('_process_delivery')
                               and self._get('tasks', r['task_id'])['bot_id'] == bot_id]
                    processes = []
                    for record in records:
                        task = self._get('tasks', record['task_id'])
                        if not self._can_resume_process(task, bot):
                            continue
                        processes.append(dict(id=record['id'], session_id=subscription(record), task_id=task['id'],
                                              epoch=task.get('_conversation_epoch')))
                if not processes:
                    continue
                results = self.provider.process_completions(bot, processes)
                with self.transaction():
                    if self.stopping.is_set() or self.restart_pending:
                        return
                    grouped = {}
                    expected = {item['id'] for item in processes}
                    for result in results:
                        if result.get('id') not in expected:
                            continue
                        record = self._get('native_receipts', result['id'])
                        task = self._get('tasks', record['task_id'])
                        if record.get('_process_delivery') or not self._can_resume_process(task, self._get('bots', bot_id)):
                            continue
                        if result.get('consumed'):
                            record['_process_delivery'] = 'consumed'
                            self._put('native_receipts', record)
                            continue
                        if isinstance(result.get('text'), str) and result['text'].strip():
                            grouped.setdefault(task['id'], []).append((record, result['text']))
                    for task_id, completions in grouped.items():
                        self._resume_process_task(task_id, completions)
            finally:
                bot_lock.release()

    def _can_resume_process(self, task, bot):
        assignment = self._assignment(task)
        if any(item.get('_return_pending') or item.get('_native_reply_pending') for item in (task, assignment)):
            return False
        if assignment['id'] != task['id']:
            if assignment['status'] == 'cancelled':
                return False
            if assignment['status'] == 'failed' and (assignment.get('_result_task_id') != task['id']
                    or self._has_uncertain_receipt(assignment['id'], assignment.get('_native_turn_id'))):
                return False
        return (not bot.get('_deleted') and not bot['archived']
                and task['status'] in ('completed', 'waiting', 'failed')
                and not self._get('exchanges', task['exchange_id']).get('cancelled')
                and task['session_id'] == bot['session_id']
                and task.get('_conversation_epoch') == self._setting('conversation_epoch')
                and not self._has_uncertain_receipt(task['id'], task.get('_native_turn_id'))
                and not self._continuation_blocked(task))

    def _resume_process_task(self, task_id, completions):
        from .service import now, uid
        task = self._get('tasks', task_id)
        epoch = task.get('_conversation_epoch')
        assignment = self._assignment(task)
        if assignment['id'] != task['id'] and assignment['status'] == 'failed':
            assignment.update(status='waiting', summary='Continuing with the result.', updated_at=now())
            self._put('tasks', assignment)
            self._event('task.updated', self._public(assignment), assignment['bot_id'], assignment['id'])
        self._advance_input(task)
        task.update(status='queued', updated_at=now(), summary='Continuing with the result.',
                    _conversation_epoch=epoch, _background_completion=[text for _, text in completions],
                    _background_return_after=now(), _background_return_id=uid())
        for record, _ in completions:
            record['_process_delivery'] = task['_native_turn_id']
            self._put('native_receipts', record)
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task_id)
        self._refresh_bot(task['bot_id'])

    def _record_consumed_processes(self, record, payload):
        consumed = payload.get('consumed_process_ids', [])
        consumed = list(consumed) if isinstance(consumed, list) else []
        value = result_value(payload)
        action = record.get('action', {})
        # Only a model tool receipt acknowledges output. The service poller creates no receipt.
        if (action.get('name') in ('process', 'process_manage') and isinstance(value, dict)
                and value.get('status') in ('exited', 'killed')):
            consumed.append(action.get('arguments', {}).get('session_id'))
        bot_id = self._get('tasks', record['task_id'])['bot_id']
        for original in self._all('native_receipts'):
            if (not original.get('_process_delivery') and subscription(original) in consumed
                    and self._get('tasks', original['task_id'])['bot_id'] == bot_id):
                original['_process_delivery'] = 'consumed'
                self._put('native_receipts', original)
