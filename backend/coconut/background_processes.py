"""Resume bot conversations from durable native process receipts."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
import threading
import time


# The poller checks interrupted tool calls at most this often, because lsof scans every process.
ENDED_TOOL_CHECK_SECONDS = 60
# macOS hides the environment of system programs, such as /bin/sh and git. Such an orphan that left its workspace
# escapes both checks, so a call counts as ended only after it started this long ago.
ENDED_TOOL_MIN_AGE_SECONDS = 60 * 60
# A call that names a privileged program anywhere never ends here. `bash -c 'sudo ...'` or `xargs sudo` hide the
# program from a command-position match, so a call that only mentions one also stays blocked. A script can still
# run sudo without naming it, but the Mac mini user has no passwordless sudo, so no root child starts unattended.
PRIVILEGED = re.compile(r'\b(?:sudo|doas|su|pkexec|runas)\b')
# A call whose workspace stays busy is checked again after this delay, which doubles up to an hour.
BUSY_TOOL_BACKOFF_SECONDS = 10 * 60
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
        # NativeExecution registers and consumes new receipts when their results arrive (commit ee99eba), so this
        # repair of older receipts needs one run. A setting records it, because each run scans the receipt journal.
        if self._setting('process_deliveries_migrated'):
            return
        # Read only the receipts that the two loops below can use, and write only the receipts that change.
        def receipts(where):
            return [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM native_receipts WHERE json_extract(data, '$.state')='completed' AND " + where + ' ORDER BY rowid')]
        tasks = {task['id']: task for task in self._all('tasks')}
        maintenance = {c['id'] for c in self._all('contexts') if c.get('_decision_refresh')}
        observed = set()
        for record in receipts("json_extract(data, '$.action.name') IN ('process', 'process_manage')"):
            value = result_value(record)
            if (record.get('turn_id') not in maintenance and isinstance(value, dict) and value.get('status') in ('exited', 'killed')
                    and tasks[record['task_id']]['status'] in ('completed', 'waiting')):
                identity = value.get('session_id') or record['action']['arguments'].get('session_id')
                observed.add((tasks[record['task_id']]['bot_id'], identity))
        # subscription() needs a terminal result with notify_on_complete or a delegate_task result.
        for record in receipts("(json_extract(data, '$._process_delivery') IS NULL OR json_extract(data, '$._process_delivery') IN ('', 0)) "
                               "AND (json_extract(data, '$.action.name')='delegate_task' OR (json_extract(data, '$.action.name')='terminal' "
                               "AND instr(json_extract(data, '$.result'), 'notify_on_complete') > 0))"):
            identity = subscription(record)
            if identity and not record.get('_process_delivery'):
                before = dict(record)
                # A completed model tool call already delivered this result to a finished turn.
                if (tasks[record['task_id']]['bot_id'], identity) in observed:
                    record['_process_delivery'] = 'consumed'
                record['_process_registered'] = True
                if record != before:
                    self._put('native_receipts', record)
        self.db.execute("INSERT INTO settings VALUES ('process_deliveries_migrated','true')")

    def _wait_settled(self, record):
        """True when the wait of a receipt has its answer and only the delivery is missing.

        A relay wait is settled when the other bot returned its result. A task, process, or decision wait is settled
        when its completion text exists. A process subscription is never settled here.
        """
        from .service import ServiceError
        try:
            if record.get('_relay_task_id'):
                return bool(self._get('tasks', record['_relay_task_id']).get('_native_relay_result'))
            if record.get('_wait_task_id') or record.get('_wait_process_receipt') or record.get('_wait_decision_id'):
                return self._wait_completion(record, self._get('tasks', record['task_id'])) is not None
        except (ServiceError, KeyError):
            return False
        return False

    def _background_pending(self, task_id, *, finishing_task_id=None, excluding_task_ids=(), ignore_settled=False,
                            ignored_receipt_ids=()):
        """True while a process, a relay, or a wait of the task, or of a turn of its assignment, has no delivery.

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
                INDEXED BY native_receipts_pending_completion
                WHERE """ + PENDING_COMPLETIONS + """ AND json_extract(data, '$.task_id') IN
                    (SELECT id FROM tasks WHERE id=? OR json_extract(data, '$._assignment_task_id')=?)
                AND NOT EXISTS (SELECT 1 FROM contexts WHERE contexts.id=json_extract(native_receipts.data, '$.turn_id')
                    AND json_extract(contexts.data, '$._decision_refresh')=1)""",
                (task_id, task_id))
            return (any((subscription(r) or r.get('_relay_task_id') or r.get('_wait_task_id') or r.get('_wait_process_receipt') or r.get('_wait_decision_id')) and not r.get('_process_delivery')
                        and r['id'] not in ignored_receipt_ids
                        and not (r.get('_relay_task_id') in excluding_task_ids or r.get('_wait_task_id') in excluding_task_ids)
                        and not (ignore_settled and self._wait_settled(r))
                        for r in (json.loads(row[0]) for row in receipts))
                    or any(t['id'] != finishing_task_id and t.get('_background_completion')
                           and t['status'] in (('working', 'needs_you', 'paused') if ignore_settled else ('queued', 'working', 'needs_you', 'paused'))
                           for t in turns))

    def _pending_completion_receipts(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM native_receipts INDEXED BY native_receipts_pending_completion WHERE '
                + PENDING_COMPLETIONS + ' ORDER BY rowid')]

    def _poll_processes(self):
        while not self.stopping.is_set():
            try:
                with self.dispatch_gate:
                    if not self.restart_pending:
                        self._drain_process_completions()
                        self._expire_work_descriptions()
            except Exception:
                logging.getLogger(__name__).exception('Could not deliver a Hermes process result.')
            self.stopping.wait(1)

    def _check_ended_tool_calls(self):
        while not self.stopping.is_set():
            try:
                if not self.restart_pending:
                    self._mark_ended_tool_calls()
            except Exception:
                logging.getLogger(__name__).exception('Could not check interrupted tool calls.')
            self.stopping.wait(ENDED_TOOL_CHECK_SECONDS)

    def _mark_ended_tool_calls(self, current=None):
        """Mark a local command without a result as ended when no process of its execution remains.

        A worker can stop before a terminal call returns. Its receipt then stays executing, and the writer guard
        keeps other work out. This check runs at most once a minute and holds the service lock only to read and write.
        A call has ended when its task has no live worker or native run, no process carries its execution's
        COCONUT_WORKSPACE_OUTCOME, and lsof shows no process in its workspaces. Only a kanban worker sets that
        variable, so Coconut checks only native tasks, and not the calls of a context job. The receipt keeps its
        state, so the owner's next turn still sees the unknown effect.
        """
        from datetime import datetime, timezone
        from .native_outcomes import LOCAL_COMMAND_TOOLS
        from .service import now
        from .work_capacity import execution_key
        from . import work_workspace
        current = time.monotonic() if current is None else current
        if current < getattr(self, '_ended_tool_check_at', 0):
            return []
        self._ended_tool_check_at = current + ENDED_TOOL_CHECK_SECONDS
        if not hasattr(self, '_busy_tool_calls'):
            self._busy_tool_calls = {}
        native = dict(self.kanban.tasks) if self.kanban else {}
        with self.lock:
            records = [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM native_receipts WHERE json_extract(data,'$.state')='executing'")]
            # Forget the back-off of a call that no longer runs.
            executing = {record['id'] for record in records}
            self._busy_tool_calls = {k: v for k, v in self._busy_tool_calls.items() if k in executing}
            candidates = []
            for record in records:
                if (record.get('action') or {}).get('name') not in LOCAL_COMMAND_TOOLS or record.get('_process_ended_at'):
                    continue
                # A privileged child runs as another user. Neither ps nor lsof can see it, so its call never ends here.
                if PRIVILEGED.search(json.dumps((record.get('action') or {}).get('arguments'))):
                    continue
                if current < self._busy_tool_calls.get(record['id'], (0, 0))[0]:
                    continue
                try:
                    started = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                if (datetime.now(timezone.utc) - started).total_seconds() < ENDED_TOOL_MIN_AGE_SECONDS:
                    continue
                row = self.db.execute('SELECT data FROM tasks WHERE id=?', (record['task_id'],)).fetchone()
                if not row or self.db.execute('SELECT 1 FROM contexts WHERE id=?', (record.get('turn_id'),)).fetchone():
                    continue
                task = json.loads(row[0])
                if not (task.get('_kanban_claim') or {}).get('native_id'):
                    continue
                worker = self.running.get(task['id'])
                card = native.get(task['id']) or {}
                if (worker and worker.is_alive()) or card.get('current_run_id') or card.get('worker_pid'):
                    continue
                candidates.append((record['id'], task['id'], execution_key(task),
                                   (task.get('_kanban_claim') or {}).get('workspace')))
        if not candidates:
            return []
        outcomes = work_workspace.process_outcomes(getattr(self.provider, 'python', None))
        if outcomes is None:
            return []
        # A live outcome proves the call can still run. Scan open files only for the other calls.
        candidates = [c for c in candidates if c[2] not in outcomes]
        if not candidates:
            return []
        root = self.root / 'workspaces'
        saved = work_workspace.unlocked_registry(root)
        paths = {}
        for identity, task_id, key, workspace in candidates:
            places = {str(root / key)} | {b['path'] for b in saved.values() if isinstance(b, dict) and b.get('outcome') == key
                                          and isinstance(b.get('path'), str)}
            if workspace:
                places.add(workspace)
            paths[identity] = {p for p in places if Path(p).exists()}
        try:
            busy = work_workspace.busy_paths(set().union(*paths.values())) if any(paths.values()) else set()
        except ValueError as error:
            # Without a complete listing, no call can end. Log it so a lasting failure is visible.
            logging.getLogger(__name__).warning('Coconut could not check interrupted tool calls: %s', error)
            return []
        ended = []
        for identity, _, _, _ in candidates:
            if paths[identity] & busy:
                # An operator shell can hold a workspace for days. Back off instead of scanning every minute.
                delay = min(self._busy_tool_calls.get(identity, (0, BUSY_TOOL_BACKOFF_SECONDS / 2))[1] * 2, 60 * 60)
                self._busy_tool_calls[identity] = (current + delay, delay)
        with self.lock:
            if self.stopping.is_set():
                # close() closes the database under this lock, so check again after taking it.
                return []
            with self.transaction():
                for identity, task_id, key, _ in candidates:
                    if key in outcomes or paths[identity] & busy:
                        continue
                    record = self._get('native_receipts', identity)
                    worker = self.running.get(task_id)
                    if record['state'] != 'executing' or (worker and worker.is_alive()):
                        continue
                    record['_process_ended_at'] = now()
                    self._put('native_receipts', record)
                    self._busy_tool_calls.pop(identity, None)
                    ended.append(identity)
        if ended:
            logging.getLogger(__name__).warning('Coconut found no process for interrupted tool calls %s.', ended)
        return ended

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
                        native_worker_id = self._receipt_native_worker_id(record, task)
                        if record.get('_delegation_ack_pending'):
                            processes.append(dict(id=record['id'], task_id=task['id'],
                                                  delegation_ids=delegation_ids(record), epoch=task.get('_conversation_epoch'),
                                                  execution_session_id=task.get('_execution_session_id'),
                                                  native_worker_id=native_worker_id, acknowledge=True))
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
                                              native_worker_id=native_worker_id,
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
                            for identity in {c['task_id'] for c in self._all('contexts') if c['id'] in maintenance}
                            for r in self._native_task_receipts(identity))
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

    def _receipt_native_worker_id(self, record, task=None):
        owner = self._completion_context(record) or task or self._get('tasks', record['task_id'])
        claim = owner.get('_kanban_claim') or {}
        native = self.kanban.tasks.get(owner['id'], {}) if self.kanban else {}
        return claim.get('native_id') or owner.get('_native_outcome_id') or native.get('id')

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
        from .profiles import ProfileError
        from .service import uid, now
        source = self._get('contexts', context_id)
        decision = self._get('decisions', source['decision_id'])
        if source.get('_decision_refresh'):
            try:
                profile_revision = self.profiles.prompt_revision(self._get('bots', source['bot_id']))
            except (OSError, ProfileError):
                # Keep this result undelivered until its profile is readable. Other bots can still resume.
                logging.getLogger(__name__).exception('Could not read the native profile for process context %s.', context_id)
                return
        # Keep the human source separate from tool output and keep the task behind its decision.
        context = dict(source, id=uid(), status='queued', _decision_revision=decision['revision'],
                       _background_completion=[text for _, text in completions])
        context.pop('error_code', None)
        context.pop('_refresh_recorded', None)
        context.pop('_failed_at', None)
        if context.get('_decision_refresh'):
            context['_refresh_origin_id'] = source.get('_refresh_origin_id', source['id'])
            decision['_freshness'] = dict(decision.get('_freshness', {}), context_id=context['id'], queued_at=now())
            self._put('decisions', decision)
        for record, text in completions:
            record.update(_completion_text=text, _process_delivery=context['id'])
            self._put('native_receipts', record)
        if context.get('_decision_refresh'):
            tasks = {t['id']: t for t in self._all('tasks')}
            artifacts = self._all('artifacts')
            records = self._decision_records(decision, tasks, artifacts)
            self._set_refresh_recovery(context, source, self._all('contexts'), records)
            fingerprint, work = self._decision_evidence(decision, tasks, records, artifacts, profile_revision=profile_revision)
            task = tasks[context['task_id']]
            context.update(_evidence_fingerprint=fingerprint,
                           _external_fingerprint=self._decision_evidence(decision, tasks, records, artifacts,
                               own_receipts=False, profile_revision=profile_revision)[0],
                           _human_input_ids=[m['id'] for m in self._decision_human_messages(task, decision)],
                           question=self._decision_refresh_prompt(decision, task, work, self._all('decisions')))
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

    def _prepare_native_reply(self, task, text, result_status, finishing=None):
        from .native_outcomes import outcome_owner_id
        ready = True
        owners = {task['id']} if task.get('_native_relay') or task.get('_native_intake') else set()
        if task.get('_relay_callback_owner'):
            owners.add(task['_relay_callback_owner'])
        for owner_id in owners:
            owner = task if owner_id == task['id'] else self._get('tasks', owner_id)
            if owner.get('_native_explicit_reply'):
                continue
            assignment = self._assignment(owner)
            waiting = [identity for identity in assignment.get('_waiting_on', [])
                       if identity != task.get('_resolved_dependency_id')]
            # Only work started to answer this message belongs to its callback.
            # The parent's other dependencies can include the sender itself.
            pending = any(self._get('tasks', identity).get('_requester_reply_task_id') == owner_id
                          for identity in waiting)
            # The continuation that finishes this assignment is saved after its return. Use its final state.
            turns = [finishing if finishing and t['id'] == finishing['id'] else t for t in self._all('tasks')
                     if t['id'] == owner_id or t.get('_relay_callback_owner') == owner_id]
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
                continue
            if not owner.get('_native_relay'):
                continue
            owner['_native_reply_pending'] = dict(owner['_native_relay'], text=text, failed=result_status == 'failed')
            owner['_native_relay_result'] = dict(owner['_native_reply_pending'])
            if owner_id != task['id']:
                self._put('tasks', owner)
        return ready

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
