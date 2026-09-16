"""Resume bot conversations from durable native process receipts."""
from __future__ import annotations

import json
import logging
import threading


PENDING_COMPLETIONS = """
    (COALESCE(json_extract(data, '$._process_delivery'), '') IN ('', 0)
     OR json_extract(data, '$._delegation_ack_pending')=1)
    AND ((json_extract(data, '$.state')='completed'
          AND CASE WHEN json_valid(json_extract(data, '$.result')) THEN
            ((json_extract(data, '$.action.name')='terminal'
              AND json_extract(json_extract(data, '$.result'), '$.notify_on_complete')=1)
             OR (json_extract(data, '$.action.name')='delegate_task'
                 AND json_extract(json_extract(data, '$.result'), '$.status')='dispatched'))
          ELSE 0 END)
         OR json_extract(data, '$._relay_task_id') IS NOT NULL
         OR json_extract(data, '$._wait_task_id') IS NOT NULL
         OR json_extract(data, '$._wait_process_receipt') IS NOT NULL
         OR json_extract(data, '$._wait_decision_id') IS NOT NULL)
"""


def result_value(record):
    try:
        value = record.get('result', {})
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}


def delegation_ids(record):
    result = result_value(record)
    if (record['state'] != 'completed' or record['action']['name'] != 'delegate_task'
            or not isinstance(result, dict) or result.get('status') != 'dispatched'):
        return []
    units = result.get('units') or [result]
    return list(dict.fromkeys(unit['delegation_id'] for unit in units
                             if isinstance(unit, dict) and isinstance(unit.get('delegation_id'), str)
                             and unit['delegation_id']))


def subscription(record):
    result = result_value(record)
    if (record['state'] == 'completed' and record['action']['name'] == 'terminal'
            and isinstance(result, dict) and result.get('notify_on_complete') is True
            and isinstance(result.get('session_id'), str)):
        return result['session_id']
    identities = delegation_ids(record)
    return identities[0] if identities else None


class BackgroundProcesses:
    def _migrate_process_deliveries(self):
        records = self._all('native_receipts')
        tasks = {task['id']: task for task in self._all('tasks')}
        maintenance = {c['id'] for c in self._all('contexts') if c.get('_decision_refresh')}
        observed = set()
        for record in records:
            value = result_value(record)
            if (record.get('turn_id') not in maintenance and record['state'] == 'completed' and record['action']['name'] in ('process', 'process_manage')
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
        with self.lock:
            turns = [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM tasks WHERE id=? OR json_extract(data, '$._assignment_task_id')=?",
                (task_id, task_id))]
            receipts = self.db.execute("""SELECT data FROM native_receipts
                WHERE json_extract(data, '$.task_id') IN
                    (SELECT id FROM tasks WHERE id=? OR json_extract(data, '$._assignment_task_id')=?)
                AND NOT EXISTS (SELECT 1 FROM contexts WHERE contexts.id=json_extract(native_receipts.data, '$.turn_id')
                    AND json_extract(contexts.data, '$._decision_refresh')=1)""",
                (task_id, task_id))
            return (any((subscription(r) or r.get('_relay_task_id') or r.get('_wait_task_id') or r.get('_wait_process_receipt') or r.get('_wait_decision_id')) and not r.get('_process_delivery')
                        for r in (json.loads(row[0]) for row in receipts))
                    or any(t['id'] != finishing_task_id and t.get('_background_completion')
                           and t['status'] in ('queued', 'working', 'needs_you', 'paused') for t in turns))

    def _pending_completion_receipts(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM native_receipts INDEXED BY native_receipts_pending_completion WHERE '
                + PENDING_COMPLETIONS + ' ORDER BY rowid')]

    def _poll_processes(self):
        while not self.stopping.is_set():
            try:
                self._drain_process_completions()
                self._expire_work_descriptions()
            except Exception:
                logging.getLogger(__name__).exception('Could not deliver a Hermes process result.')
            self.stopping.wait(1)

    def _expire_work_descriptions(self):
        from datetime import datetime, timezone
        from .worker_activity import FRESH_SECONDS
        current = datetime.now(timezone.utc)
        with self.transaction():
            rows = self.db.execute("SELECT data FROM tasks WHERE json_extract(data, '$.status') IN ('working','waiting') "
                                   "AND json_extract(data, '$._expired_work_description') IS NOT "
                                   "COALESCE(json_extract(data, '$._work_progress_at'), json_extract(data, '$.created_at'))").fetchall()
            for row in rows:
                task = json.loads(row[0])
                stamp = task.get('_work_progress_at', task['created_at'])
                if task.get('_expired_work_description') == stamp:
                    continue
                if (current - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds() <= FRESH_SECONDS:
                    continue
                task['_expired_work_description'] = stamp
                self._put('tasks', task)
                self._event('task.updated', self._public(task), task['bot_id'], task['id'])

    def _process_observation_current(self, task, bot):
        return (not bot.get('_deleted') and not bot['archived'] and task['status'] != 'cancelled'
                and not self._get('exchanges', task['exchange_id']).get('cancelled')
                and task['session_id'] == bot['session_id']
                and task.get('_conversation_epoch') == self._setting('conversation_epoch'))

    def _drain_process_completions(self):
        self._drain_relay_completions()
        self._drain_task_completions()
        if not hasattr(self.provider, 'process_completions'):
            return
        with self.lock:
            bot_ids = {self._get('tasks', r['task_id'])['bot_id'] for r in self._pending_completion_receipts()
                       if subscription(r) and (not r.get('_process_delivery') or r.get('_delegation_ack_pending'))}
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
                    records = [r for r in self._pending_completion_receipts() if subscription(r) and (not r.get('_process_delivery') or r.get('_delegation_ack_pending'))
                               and self._get('tasks', r['task_id'])['bot_id'] == bot_id]
                    processes, cached = [], []
                    for record in records:
                        task = self._get('tasks', record['task_id'])
                        if record.get('_delegation_ack_pending'):
                            processes.append(dict(id=record['id'], task_id=task['id'],
                                                  delegation_ids=delegation_ids(record), epoch=task.get('_conversation_epoch'),
                                                  execution_session_id=task.get('_execution_session_id'), acknowledge=True))
                            continue
                        if not self._process_observation_current(task, bot):
                            continue
                        resumable = self._can_resume_completion(task, bot, record)
                        if record.get('_completion_text'):
                            if resumable:
                                cached.append(dict(id=record['id'], text=record['_completion_text']))
                            continue
                        processes.append(dict(id=record['id'], session_id=subscription(record), task_id=task['id'],
                                              epoch=task.get('_conversation_epoch'), execution_session_id=task.get('_execution_session_id'),
                                              delegation_ids=delegation_ids(record)))
                if not processes and not cached:
                    continue
                results = cached + (self.provider.process_completions(bot, processes) if processes else [])
                with self.transaction():
                    if self.stopping.is_set() or self.restart_pending:
                        return
                    grouped = {}
                    expected = {item['id'] for item in processes + cached}
                    for result in results:
                        if result.get('id') not in expected:
                            continue
                        record = self._get('native_receipts', result['id'])
                        task = self._get('tasks', record['task_id'])
                        if result.get('acknowledged') and record.get('_delegation_ack_pending'):
                            record.pop('_delegation_ack_pending', None)
                            self._put('native_receipts', record)
                            continue
                        if record.get('_process_delivery') or not self._process_observation_current(task, self._get('bots', bot_id)):
                            continue
                        if 'observation' in result:
                            from .service import now
                            record.update(_process_status=result['observation'], _process_observed_at=now())
                            from .worker_activity import LABELS
                            progress = result.get('progress')
                            if (isinstance(progress, dict) and progress.get('phase') in LABELS
                                    and isinstance(progress.get('at'), (int, float))):
                                record['_worker_progress'] = progress
                            from datetime import datetime, timezone
                            from .worker_activity import progress_label
                            label = progress_label(record, datetime.now(timezone.utc))
                            changed = record.get('_worker_display') != label
                            record['_worker_display'] = label
                            self._put('native_receipts', record)
                            if changed:
                                self._event('task.updated', self._public(task), task['bot_id'], task['id'])
                            continue
                        maintenance = {c['id'] for c in self._all('contexts') if c.get('_decision_refresh')}
                        read_by_maintenance = any(r.get('turn_id') in maintenance and r.get('turn_id') != record.get('turn_id')
                            and self._get('tasks', r['task_id'])['bot_id'] == bot_id
                            and r.get('action', {}).get('name') in ('process', 'process_manage')
                            and r['action'].get('arguments', {}).get('session_id') == subscription(record)
                            for r in self._all('native_receipts'))
                        if result.get('consumed') and not read_by_maintenance:
                            record['_process_delivery'] = 'consumed'
                            record['_completion_text'] = result.get('text', 'The process result was already received.')
                            self._put('native_receipts', record)
                            continue
                        if isinstance(result.get('text'), str) and result['text'].strip():
                            record['_completion_text'] = result['text']
                            if delegation_ids(record):
                                record['_delegation_ack_pending'] = True
                            self._put('native_receipts', record)
                            if self._can_resume_completion(task, self._get('bots', bot_id), record):
                                context = self._completion_context(record)
                                grouped.setdefault((task['id'], context['id'] if context else None), []).append((record, result['text']))
                    for (task_id, context_id), completions in grouped.items():
                        if context_id:
                            self._resume_process_context(context_id, completions)
                        else:
                            self._resume_process_task(task_id, completions)
            finally:
                bot_lock.release()

    def _completion_context(self, record):
        row = self.db.execute('SELECT data FROM contexts WHERE id=?', (record.get('turn_id'),)).fetchone()
        return json.loads(row[0]) if row else None

    def _can_resume_completion(self, task, bot, record):
        context = self._completion_context(record)
        if context is None:
            return self._can_resume_process(task, bot)
        decision = self._get('decisions', context['decision_id'])
        return (context['task_id'] == task['id'] and context['status'] in ('completed', 'failed')
                and decision['status'] == 'pending' and decision['task_id'] == task['id']
                and self._process_observation_current(task, bot) and not self._task_paused(task)
                and self._assignment(task)['status'] != 'cancelled'
                and context.get('_conversation_epoch') == task.get('_conversation_epoch')
                and not self._has_uncertain_receipt(task['id'], context['id']))

    def _resume_process_context(self, context_id, completions):
        from .service import uid, now
        source = self._get('contexts', context_id)
        decision = self._get('decisions', source['decision_id'])
        # Keep the human source separate from tool output and keep the task behind its decision.
        context = dict(source, id=uid(), status='queued', _decision_revision=decision['revision'],
                       _background_completion=[text for _, text in completions])
        context.pop('error_code', None)
        context.pop('_refresh_recorded', None)
        if context.get('_decision_refresh'):
            context['_refresh_origin_id'] = source.get('_refresh_origin_id', source['id'])
            decision['_freshness'] = dict(decision.get('_freshness', {}), context_id=context['id'], queued_at=now())
            self._put('decisions', decision)
        for record, text in completions:
            record.update(_completion_text=text, _process_delivery=context['id'])
            self._put('native_receipts', record)
        if context.get('_decision_refresh'):
            self._set_refresh_recovery(context, source, self._all('contexts'), self._all('native_receipts'))
            tasks = {t['id']: t for t in self._all('tasks')}
            fingerprint, work = self._decision_evidence(decision, tasks, self._all('native_receipts'), self._all('artifacts'))
            task = tasks[context['task_id']]
            context.update(_evidence_fingerprint=fingerprint,
                           _human_input_ids=[m['id'] for m in self._decision_human_messages(task)],
                           question=self._decision_refresh_prompt(task, work, self._all('decisions')))
        self._put('contexts', context)
        self._event('context.queued', {'id': context['id'], 'decision_id': context['decision_id']},
                    context['bot_id'], context['task_id'])

    def _drain_relay_completions(self):
        # The recipient receipt is durable. It also covers a restart after Hermes
        # queues an envelope but before Coconut receives the waiter process ID.
        with self.lock:
            bot_ids = {self._get('tasks', r['task_id'])['bot_id'] for r in self._pending_completion_receipts()
                       if r.get('_relay_task_id') and not r.get('_process_delivery')}
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
                        if not record.get('_relay_task_id') or record.get('_process_delivery'):
                            continue
                        task = self._get('tasks', record['task_id'])
                        if task['bot_id'] != bot_id or not self._can_resume_process(task, self._get('bots', bot_id)):
                            continue
                        recipient = self._get('tasks', record['_relay_task_id'])
                        result = recipient.get('_native_relay_result')
                        if not result:
                            continue
                        text = ('The other bot could not complete its turn: ' if result['failed'] else 'Reply from the other bot: ') + result['text']
                        grouped.setdefault(task['id'], []).append((record, text))
                    for task_id, completions in grouped.items():
                        self._resume_process_task(task_id, completions)
            finally:
                bot_lock.release()

    def _prepare_native_reply(self, task, text, result_status):
        owners = {task['id']} if task.get('_native_relay') else set()
        if task.get('_relay_callback_owner'):
            owners.add(task['_relay_callback_owner'])
        for owner_id in owners:
            owner = task if owner_id == task['id'] else self._get('tasks', owner_id)
            assignment = self._assignment(owner)
            waiting = [identity for identity in assignment.get('_waiting_on', [])
                       if identity != task.get('_resolved_dependency_id')]
            # Only work started to answer this message belongs to its callback.
            # The parent's other dependencies can include the sender itself.
            pending = any(self._get('tasks', identity).get('_requester_reply_task_id') == owner_id
                          for identity in waiting)
            turns = [t for t in self._all('tasks')
                     if t['id'] == owner_id or t.get('_relay_callback_owner') == owner_id]
            pending = pending or any(self._background_pending(t['id'], finishing_task_id=task['id'])
                                     or (t['id'] != task['id'] and t['status'] in ('queued', 'working', 'needs_you', 'paused'))
                                     for t in turns)
            if result_status != 'failed' and pending:
                continue
            owner['_native_reply_pending'] = dict(owner['_native_relay'], text=text, failed=result_status == 'failed')
            owner['_native_relay_result'] = dict(owner['_native_reply_pending'])
            if owner_id != task['id']:
                self._put('tasks', owner)

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
        for record, text in completions:
            record['_completion_text'] = text
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
        consumed = [identity for identity in consumed if isinstance(identity, str) and identity]
        if not consumed:
            return
        bot_id = self._get('tasks', record['task_id'])['bot_id']
        context = next((c for c in self._all('contexts') if c['id'] == record.get('turn_id')), {})
        for original in self._pending_completion_receipts():
            if (not original.get('_process_delivery') and subscription(original) in consumed
                    and self._get('tasks', original['task_id'])['bot_id'] == bot_id):
                if not context.get('_decision_refresh') or original.get('turn_id') == record.get('turn_id'):
                    original['_process_delivery'] = 'consumed'
                # Hermes may consume its notification on read. Keep the result for the owner.
                original['_completion_text'] = json.dumps(value)
                self._put('native_receipts', original)
