"""Continue a task with the results of the waits that its turns registered (coconut_wait).

A turn waits for another task of its exchange or for a card. Coconut records each wait as a receipt of the turn
(work_progress.wait_for_task). When the result exists, the task continues in a new turn with that result
(_background_completion). Hermes watches the background processes of a gateway turn itself, and delivers their results
in a later turn of the chat.
"""
from __future__ import annotations

import json
import logging


# The receipts of waits that have no delivery yet. The partial index native_receipts_pending_wait has this condition.
PENDING_COMPLETIONS = """
    COALESCE(json_extract(data, '$._process_delivery'), '') IN ('', 0)
    AND (json_extract(data, '$._wait_task_id') IS NOT NULL
         OR json_extract(data, '$._wait_decision_id') IS NOT NULL)
"""
# A work description that changed no longer shows after this.
WORK_DESCRIPTION_SECONDS = 120


def result_value(record):
    try:
        value = record.get('result', {})
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}


class BackgroundProcesses:
    def _wait_settled(self, record):
        """True when the wait of a receipt has its answer and only the delivery is missing.

        A task or decision wait is settled when its completion text exists.
        """
        from .service import ServiceError
        try:
            if record.get('_wait_task_id') or record.get('_wait_decision_id'):
                return self._wait_completion(record, self._get('tasks', record['task_id'])) is not None
        except (ServiceError, KeyError):
            return False
        return False

    def _background_pending(self, task_id, *, finishing_task_id=None, excluding_task_ids=(), ignore_settled=False,
                            ignored_receipt_ids=()):
        """True while a wait of the task, or of a turn of its assignment, has no delivery, or a turn that continues with
        such a result has not finished.

        ignore_settled skips the waits that already have their answer, and a queued turn that carries delivered answers
        of any kind. Such a turn does not run now: the caller holds the dispatch gate, which the dispatcher needs. The
        answer reaches an owner only when the owner runs, so it says nothing about work that runs now.

        ignored_receipt_ids skips those receipts only, such as a wait that a merge gate proved unrelated. Its owner still gates.
        """
        with self.lock:
            turns = [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM tasks WHERE id=? OR json_extract(data, '$._assignment_task_id')=?",
                (task_id, task_id))]
            receipts = self.db.execute("""SELECT data FROM native_receipts
                INDEXED BY native_receipts_pending_wait
                WHERE """ + PENDING_COMPLETIONS + """ AND json_extract(data, '$.task_id') IN
                    (SELECT id FROM tasks WHERE id=? OR json_extract(data, '$._assignment_task_id')=?)
                AND NOT EXISTS (SELECT 1 FROM contexts WHERE contexts.id=json_extract(native_receipts.data, '$.turn_id')
                    AND json_extract(contexts.data, '$._decision_refresh')=1)""",
                (task_id, task_id))
            return (any(not r.get('_process_delivery') and r['id'] not in ignored_receipt_ids
                        and r.get('_wait_task_id') not in excluding_task_ids
                        and not (ignore_settled and self._wait_settled(r))
                        for r in (json.loads(row[0]) for row in receipts))
                    or any(t['id'] != finishing_task_id and t.get('_background_completion')
                           and t['status'] in (('working', 'needs_you', 'paused') if ignore_settled else ('queued', 'working', 'needs_you', 'paused'))
                           for t in turns))

    def _pending_completion_receipts(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM native_receipts INDEXED BY native_receipts_pending_wait WHERE '
                + PENDING_COMPLETIONS + ' ORDER BY rowid')]

    def _poll_processes(self):
        while not self.stopping.is_set():
            try:
                with self.dispatch_gate:
                    if not self.restart_pending:
                        self._drain_task_completions()
                        self._expire_work_descriptions()
            except Exception:
                logging.getLogger(__name__).exception('Could not deliver the result of a wait.')
            self.stopping.wait(1)

    def _expire_work_descriptions(self):
        from datetime import datetime, timezone
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
                if (current - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds() <= WORK_DESCRIPTION_SECONDS:
                    continue
                task['_expired_work_description'] = stamp
                self._put('tasks', task)
                self._event('task.updated', self._public(task), task['bot_id'], task['id'])

    def _process_observation_current(self, task, bot):
        return (not bot.get('_deleted') and not bot['archived'] and task['status'] != 'cancelled'
                and not self._get('exchanges', task['exchange_id']).get('cancelled')
                and task['session_id'] == bot['session_id']
                and task.get('_conversation_epoch') == self._setting('conversation_epoch'))

    def _callbacks_ready(self, task, result_status, finishing=None):
        """Whether the callbacks of this task can return: no work that answers them still runs, and no outcome that
        they created still waits. A failed result returns at once. The caller is _prepare_return."""
        from .native_outcomes import outcome_owner_id
        ready = True
        owners = {task['id']} if task.get('_native_intake') else set()
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
            # The continuation that finishes this assignment is saved after its return. Use its final state.
            # Indexes find the owner and its callback turns. A parse of the whole task table took about 0.3 s here.
            turns = [finishing if finishing and t['id'] == finishing['id'] else t for t in (json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM tasks WHERE id=? OR json_extract(data,'$._relay_callback_owner')=? ORDER BY rowid",
                (owner_id, owner_id)))]
            # Creation finishes before the scheduler adopts the native task.
            adopted = {t['id'] for t in turns if t.get('_native_outcome_id')}
            for record in self._native_task_receipts(owner_id):
                if record['action']['name'] != 'kanban_create':
                    continue
                created = result_value(record)
                pending = pending or record['state'] == 'executing' or (
                    isinstance(created, dict) and created.get('task_id')
                    and outcome_owner_id(owner_id, created['task_id']) not in adopted)
            pending = pending or any(t['id'] != task['id'] and t.get('_native_outcome_id')
                                     and (self.kanban.tasks.get(t['id'], {}) if self.kanban else {}).get('status')
                                     not in ('done', 'archived') for t in turns)
            pending = pending or any(self._background_pending(t['id'], finishing_task_id=(finishing or task)['id'])
                                     or (t['id'] != task['id'] and t['status'] in ('queued', 'working', 'needs_you', 'paused'))
                                     for t in turns)
            if result_status != 'failed' and pending:
                ready = False
        return ready

    def _can_resume_process(self, task, bot):
        assignment = self._assignment(task)
        if any(item.get('_return_pending') for item in (task, assignment)):
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
