"""Dispatch native schedules while Coconut owns their durable conversation tasks."""
from datetime import datetime, timedelta, timezone
import time
import json
from uuid import UUID, uuid5

from .native_failure import TRANSIENT_ERROR_CODES
from .service_base import instant
from .work_sources import ANSWERED, STALE_NOTE, source_origin


# Statements of the routine pass. Each needs an index and no scan.
ROUTINE_EVENTS = 'SELECT type,data,created_at FROM events WHERE task_id=? AND bot_id=? ORDER BY seq'
COMPLETION_EVENTS = 'SELECT type,data FROM events WHERE task_id=? AND bot_id=? ORDER BY seq'
REQUEST_EVENTS = 'SELECT bot_id,type,data FROM events WHERE task_id=? ORDER BY seq'
REQUEST_EVENT_TIMES = 'SELECT bot_id,type,data,created_at FROM events WHERE task_id=? ORDER BY seq'
# The state of every work source. The key range uses the primary key, so the few rows need no scan.
WORK_SOURCE_STATES = "SELECT value FROM settings WHERE key > 'work-source:' AND key < 'work-source;'"
ROUTINE_DELIVERIES = "SELECT data FROM deliveries WHERE json_extract(data,'$.receipt.task_id')=? ORDER BY rowid DESC"
# The first delivery that names a request. The creation of a relayed request saves it.
CREATION_DELIVERY = "SELECT data FROM deliveries WHERE json_extract(data,'$.receipt.task_id')=? ORDER BY rowid LIMIT 1"
# Open intake of a waiting occurrence. Any of these means that the occurrence still works or has input to take.
OPEN_INTAKE = ('_steering', '_live_messages', '_blocked_followup', '_return_pending',
               '_resume_decision', '_background_completion', '_pr_review', '_work_resume', '_routine_pending_bot_result')
# Hermes outcomes of a run that ended without success. A completed run, or one that an archive reclaimed, is not one.
# An archive reclaims only the current run, so an earlier reclaimed run comes from a restart.
FAILED_RUN_OUTCOMES = ('blocked', 'crashed', 'gave_up', 'spawn_failed', 'stale', 'timed_out', 'worker_failed')
# Inputs that a failed turn selected but may never have received. continue_native_work hands them to the retry.
FAILED_TURN_INPUT = ('_background_completion', '_pr_review')
# The reason of a routine that does not run, because no Hermes gateway serves the bot's profile
# (RoutineCoordinator._pause_unserved_routines). The phone shows it with the routine, as a chat shows its refusal.
ROUTINES_PARKED = ('The Hermes profile of this agent is parked ({path}), so no Hermes gateway runs its routines. Remove '
                   'that file to run them again on their schedule.')
# A run that the person starts waits for the next tick of the gateway's cron (RoutineCoordinator._trigger_routine).
TRIGGER_SECONDS = 5 * 60
TRIGGER_WAITING = 'Waiting for the Hermes gateway to run this routine.'
# The results of a run that started no agent turn (_expire_routine_triggers).
TRIGGER_QUIET = 'Hermes ran the script check of this routine. It found no work, so the agent did not run.'
TRIGGER_NO_AGENT = 'Hermes ran the script of this routine.'
TRIGGER_SCRIPT_FAILED = 'Hermes ran this routine, and it failed: {error}'
TRIGGER_EXPIRED = ('The Hermes gateway did not run this routine within 5 minutes. Check that the gateway runs, then run '
                   'the routine again.')
ROUTINES_UNSERVED = ('No Hermes gateway serves the profile of this agent now, so its routines are paused. They run again on '
                     'their schedule when the gateway serves the profile.')
# Earlier versions wrote these reasons with "bot" in place of "agent", and a routine keeps its reason. The pause
# recognizes both texts (_pause_unserved_routines).
EARLIER_UNSERVED = ('No Hermes gateway serves the profile of this bot now, so its routines are paused. They run again on '
                    'their schedule when the gateway serves the profile.')
EARLIER_PARKED = 'The Hermes profile of this bot is parked ('
UNSERVED_REASONS = (ROUTINES_UNSERVED, EARLIER_UNSERVED)
PARKED_PREFIXES = (ROUTINES_PARKED.split('{', 1)[0], EARLIER_PARKED)
# Without a Hermes gateway (fixture mode), nothing runs a routine.
ROUTINES_NEED_GATEWAY = 'Only the Hermes gateway runs routines, and this Mac has none.'
# Records that only a routine occurrence has. A request that has any of them, even an empty one, follows the routine
# rules alone. Only the routine code writes them, so an ordinary request never has one.
ROUTINE_PROVENANCE = ('routine_id', 'routine_occurrence_id', '_native_job', '_native_job_finished', '_native_finish_unconfirmed',
                      '_native_job_completion_receipt', '_native_job_completion', '_native_occurrence')
# The pending decisions of a bot. Their tasks lead to the source items that the cards ask about.
PENDING_DECISIONS = ("SELECT json_extract(data,'$.task_id') FROM decisions WHERE json_extract(data,'$.status')='pending' "
                     "AND json_extract(data,'$.bot_id')=?")
# A routine task that has not finished its native job. The partial index tasks_routine_open holds these tasks.
OPEN_ROUTINE_TASK = "json_type(data,'$.routine_id') IS NOT NULL AND json_type(data,'$._native_job_finished') IS NOT 'true'"
# The tasks that can keep a routine busy: open routine tasks, and queued or working ones that can continue.
OPEN_ROUTINE_TASKS = 'SELECT rowid, data FROM tasks WHERE ' + OPEN_ROUTINE_TASK
RUNNING_ROUTINE_TASKS = ("SELECT rowid, data FROM tasks WHERE json_extract(data,'$.status') IN ('queued','working') "
                         "AND json_type(data,'$.routine_id') IS NOT NULL")
# The tasks that these tasks started. The index tasks_source finds them.
CHILD_TASKS = "SELECT rowid, data FROM tasks WHERE json_extract(data,'$.source_task_id') IN (SELECT value FROM json_each(?))"
# An answered proposal of a parent task that the child task carries out. See work_sources.source_origin.
EXECUTED_PROPOSAL = ("SELECT 1 FROM decisions WHERE json_extract(data,'$.task_id')=? "
                     "AND json_extract(data,'$._execution_task_id')=? AND json_extract(data,'$.inbox_kind')='proposal' "
                     "AND json_extract(data,'$.bot_id')=? "
                     "AND json_extract(data,'$.status') IN (" + ','.join('?' * len(ANSWERED)) + ") LIMIT 1")

class NativeRoutines:
    def _routine_continuation_input(self, task, job, owner):
        # The immutable native outcome is checked in the worker. Here require
        # the exact later delivery from the authenticated continuation route.
        if (task.get('status') not in ('queued', 'working', 'completed')
                or not task.get('_native_outcome_id') or not task.get('_native_turn_id')
                or task.get('_self_recovery', {}).get('previous_task_id') != task['id']):
            return {}
        triggered, failed_at = False, None
        for kind, raw, stamp in self.db.execute(ROUTINE_EVENTS, (task['id'], task['bot_id'])):
            data = json.loads(raw)
            if kind == 'routine.triggered':
                if triggered or data != dict(routine_id=task['routine_id'], task_id=task['id']):
                    return {}
                triggered = True
                failed_at = stamp
            if kind == 'task.updated' and triggered:
                status = data.get('status')
                if status == 'cancelled':
                    return {}
                if status == 'failed':
                    failed_at = stamp
        if not triggered or not failed_at:
            return {}
        successor = self._input_successor(task)
        rows = self.db.execute(ROUTINE_DELIVERIES, (task['id'],))
        for row in rows:
            delivery = json.loads(row[0])
            expected = dict(task_id=task['id'], native_task_id=task['_native_outcome_id'], status='queued')
            receipt = delivery.get('receipt') or {}
            # A continuation of an archived task runs in the successor that Hermes made for this input.
            if (successor and isinstance(receipt.get('native_task_id'), str)
                    and receipt == dict(expected, native_task_id=receipt['native_task_id'])):
                expected = receipt
            # Old deliveries without an input binding require a new explicit
            # continuation. Recovery metadata alone cannot authorize a new turn.
            if (receipt != expected or delivery.get('target_bot_id') != task['bot_id']
                    or delivery.get('turn_id') != task['_native_turn_id']
                    or delivery.get('created_at', '') <= failed_at):
                continue
            if not self._delivered(task, delivery):
                continue
            return dict(job_id=job['id'], owner=owner,
                        continuation=dict(task_id=task['id'], turn_id=task['_native_turn_id'],
                                          native_task_id=task['_native_outcome_id'], delivery_id=delivery['id']))
        return {}

    def _delivered(self, task, delivery):
        """Whether the owner or its requesting bot sent this delivery, and the task holds its message."""
        source = self.db.execute('SELECT data FROM tasks WHERE id=?', (delivery.get('source_task_id'),)).fetchone()
        if not source:
            return False
        source = json.loads(source[0])
        return (source['bot_id'] in (task['bot_id'], task.get('source_bot_id'))
                and any(m.get('sender_bot_id') == source['bot_id'] and m.get('text') == delivery.get('text')
                        for m in task.get('_bot_messages', [])))

    def _continuation_turns(self, task, native_id=None):
        """The turns that an authenticated coconut_continue_work delivery gave this task, on the card `native_id` if given."""
        turns = set()
        for row in self.db.execute(ROUTINE_DELIVERIES, (task['id'],)):
            delivery = json.loads(row[0])
            receipt = delivery.get('receipt') or {}
            if (isinstance(receipt.get('native_task_id'), str) and isinstance(delivery.get('turn_id'), str)
                    and native_id in (None, receipt['native_task_id'])
                    and receipt == dict(task_id=task['id'], native_task_id=receipt['native_task_id'], status='queued')
                    and delivery.get('target_bot_id') == task['bot_id'] and self._delivered(task, delivery)):
                turns.add(delivery['turn_id'])
        return turns

    def _input_successor(self, task):
        """Whether the native outcome of the task is the successor that Board.sync made for its current input.

        Hermes keeps an archived task. New input to it gets a child task whose key names the request and the input.
        The key does not name the parent. The delivery names it. The reconciler binds the successor before Hermes
        claims it, so the claim of the previous input can remain until dispatch. A claim of this input names the successor.
        """
        card = (self.kanban.tasks if getattr(self, 'kanban', None) else {}).get(task['id']) or {}
        claim = task.get('_kanban_claim') or {}
        return (card.get('id') == task['_native_outcome_id']
                and (claim.get('input') != task['_native_turn_id'] or claim.get('native_id') == card['id'])
                and card.get('tenant') == task['bot_id'] and card.get('created_by') == 'coconut'
                and card.get('idempotency_key') == 'coconut-followup:' + task['id'] + ':' + task['_native_turn_id'])

    def settled_failed_occurrence(self, task, card, completion):
        """Whether Coconut's own settle pass archived the native task of this failed routine occurrence.

        _settle_native_orphans archives the waiting task of a failed request after a few minutes. A person's Stop
        cancels the request instead, and an old idle archive takes only a task that waited for IDLE_AGE. Hermes
        must have confirmed the failure of this exact job and owner, and the occurrence must hold no input, decision,
        or tool call. The archived task must be the one that Coconut created for this request, ended by the run of
        its claim without success, with no run or worker left. completion() reads that card from the adapter.
        A continuation that failed again has its successor archived the same way. That card is the successor of the
        claim's input. Anything else stays final.
        """
        if not self._settled_failure(task, card):
            return False
        claim = task.get('_kanban_claim') or {}
        native = completion()
        runs = native.get('runs')
        return bool(native.get('id') == card['id'] and native.get('tenant') == task['bot_id']
                    and native.get('idempotency_key') == card['idempotency_key'] and native.get('status') == 'archived'
                    and not native.get('current_run_id') and not native.get('worker_pid')
                    and isinstance(runs, list) and runs
                    and all(isinstance(r, dict) and r.get('ended') and r.get('outcome') != 'completed' for r in runs)
                    and runs[-1].get('outcome') in FAILED_RUN_OUTCOMES and runs[-1].get('id') == claim.get('run_id'))

    def settled_scheduled_continuation(self, task, card, completion):
        """Whether Coconut's settle pass archived the scheduled card of a continuation that a finished request still owns.

        A continuation of a finished request runs in a successor card. Its worker can park that card with
        kanban_schedule to check again later. Hermes then ends the run as scheduled, nothing re-gates the card, the task
        completes, and the settle pass archives the card a day later. The work is unfinished, though.
        For a routine, Hermes must have confirmed the success of this exact job and owner, and the original card of the
        occurrence stays as it is. An ordinary request has no routine record at all; its completed input proves nothing
        more. Either way the archived card must be the successor that Board.sync made for an authenticated
        continuation of this task, holding the current input, also authenticated. The worker of an ordinary request
        that another bot relayed can park the original card too, before any continuation. _settled_original_card proves
        that card and its current input instead. Migration gives a retained Work item its own card, and a continuation
        of that card reuses it until it is done or archived. _settled_migrated_card proves that card and its current
        input instead. Its last run must be the run of the claim of that input, ended as
        scheduled. No run may have completed, and no run or worker may be left. The guards of settled_failed_occurrence
        hold too. completion() reads the card from the adapter. Anything else stays final.
        """
        claim = task.get('_kanban_claim') or {}
        turn = task.get('_native_turn_id')
        key = card.get('idempotency_key')
        prefix = 'coconut-followup:' + task['id'] + ':'
        routine = any(field in task for field in ROUTINE_PROVENANCE)
        original = not routine and key == 'coconut:' + task['id']
        work = task.get('_work_context_id')
        migrated = not routine and isinstance(work, str) and bool(work) and key == 'coconut-outcome:' + work
        if (not turn or claim.get('input') != turn or type(claim.get('run_id')) is not int
                or task.get('_kanban_started') != claim['run_id'] or card.get('input_id') != turn
                or task.get('_native_outcome_id') != card.get('id')
                or (routine and (task.get('_native_occurrence') or {}).get('native_id') in (None, card.get('id')))
                or not isinstance(key, str) or not (key.startswith(prefix) or original or migrated)
                or (task.get('_self_recovery') or {}).get('unknown_tools')
                or not (self._settled_occurrence(task, card, success=True) if routine
                        else self._settled_request(task, card))
                or not (self._settled_original_card(task, card) if original
                        else self._settled_migrated_card(task, card) if migrated
                        else {turn, key[len(prefix):]} <= self._continuation_turns(task))):
            return False
        native = completion()
        runs = native.get('runs')
        return bool(native.get('id') == card['id'] and native.get('tenant') == task['bot_id']
                    and native.get('idempotency_key') == key and native.get('status') == 'archived'
                    and native.get('input_id') == turn and not native.get('current_run_id') and not native.get('worker_pid')
                    and isinstance(runs, list) and runs
                    and all(isinstance(r, dict) and r.get('ended') and r.get('outcome') != 'completed' for r in runs)
                    and runs[-1].get('outcome') == 'scheduled' and runs[-1].get('id') == claim['run_id'])

    def settled_failed_request(self, task, card, completion):
        """Whether Coconut's settle pass archived the native task of this failed ordinary request.

        _settle_native_orphans archives the triage or blocked task of a failed request after a few minutes. Before
        that archive, the owner or its requesting bot could continue the request. This keeps that continuation, and
        nothing else. The checks of _settled_request_failure hold. Hermes must also report the same task: no run or
        worker left, no completed run, and the last run is the claim's run, ended without success. completion() reads
        that card from the adapter. A continuation that failed again has its successor archived the same way, and that
        successor continues the same way.
        """
        if not self._settled_request_failure(task, card):
            return False
        claim = task['_kanban_claim']
        native = completion()
        runs = native.get('runs')
        return bool(native.get('id') == card['id'] and native.get('tenant') == task['bot_id']
                    and native.get('idempotency_key') == card['idempotency_key'] and native.get('status') == 'archived'
                    and native.get('input_id') in (None, task['_native_turn_id'])
                    and not native.get('current_run_id') and not native.get('worker_pid')
                    and isinstance(runs, list) and runs
                    and all(isinstance(r, dict) and r.get('ended') and r.get('outcome') != 'completed' for r in runs)
                    and runs[-1].get('outcome') in FAILED_RUN_OUTCOMES and runs[-1].get('id') == claim['run_id']
                    and (runs[-1].get('outcome') == 'blocked' or not self._provider_successor(task, card)))

    @staticmethod
    def _provider_successor(task, card):
        """Whether the card is the successor that Board.sync made for the current input of a request without a retained
        native outcome, and the model provider stopped that input's turn for a while.

        Board.sync gives new input to such a request a card under the key coconut:<id>:<input>. When the provider stops a
        turn for a while (TRANSIENT_ERROR_CODES), Hermes retries a card that Coconut owns (Board.finish). This card is
        not owned, so the stop blocked it, the request failed, and the settle pass archived the card. Only that stop
        continues. Any other failure of such a card stays final.
        """
        claim = task.get('_kanban_claim') or {}
        return bool(not task.get('_native_outcome_id') and claim.get('input') and task.get('error_code') in TRANSIENT_ERROR_CODES
                    and card.get('idempotency_key') == 'coconut:' + task['id'] + ':' + claim['input'])

    def _settled_request_failure(self, task, card):
        """The checks of settled_failed_request that read only Coconut's records and the snapshot card.

        The request has no routine record, and no person stopped it. Coconut created the request once. Its claim names
        the archived task and the current input, and Coconut started the run of that claim. No tool call is unknown.
        Its only open input is the input that the failed turn never received, which the continuation hands to the
        retry. The checks of _settled_card hold too, so an old idle archive is not it. The card is the request's
        original card, the successor of its retained outcome for the claim's input, or the successor that a provider
        stop ended (_provider_successor).
        """
        claim = task.get('_kanban_claim') or {}
        turn = task.get('_native_turn_id')
        keys = {'coconut:' + task['id']}
        if task.get('_native_outcome_id') == card.get('id') and claim.get('input'):
            keys.add('coconut-followup:' + task['id'] + ':' + claim['input'])
        if self._provider_successor(task, card):
            keys.add(card['idempotency_key'])
        if (task['status'] != 'failed' or any(field in task for field in ROUTINE_PROVENANCE)
                or task.get('_native_outcome_id') not in (None, card.get('id')) or card.get('idempotency_key') not in keys
                or not turn or claim.get('input') != turn or type(claim.get('run_id')) is not int
                or task.get('_kanban_started') != claim['run_id']
                or (task.get('_self_recovery') or {}).get('unknown_tools')
                or not self._settled_card(task, card, carried=FAILED_TURN_INPUT)):
            return False
        return self._created_once(task)

    def _created_once(self, task):
        """Whether Coconut created this request once, and no routine or person ever triggered or stopped it."""
        # A relay records the creation under the requesting bot, so read the events of every bot.
        created = []
        for bot, kind, raw in self.db.execute(REQUEST_EVENTS, (task['id'],)):
            if kind == 'routine.triggered' or (kind == 'task.updated' and json.loads(raw).get('status') == 'cancelled'):
                return False
            if kind == 'task.created':
                created.append(bot)
        return created in ([task['bot_id']], [task.get('conversation_bot_id')])

    def settled_closed_source(self, task, card, completion):
        """Whether the assignment pass ended this request because every source that it works on closed.

        work_sources.stale_cards picks an idle card, Board.archive_stale archives it and comments STALE_NOTE at once,
        and the reconciler then cancels the request of the archived input. The request can still continue, but only
        when its owner or requesting bot asks for it explicitly (continue_work's continue_closed_source).

        The checks read Coconut's records and the snapshot card first. The request has no routine record. The card is
        its retained outcome, which is the only kind that the reconciler cancels. Its claim names the card, Coconut
        started the run of that claim, and the card holds the request's current input. The Coconut card in its tenant
        has no run, worker, or open parent. The request has no worker, unknown tool call, or open input, no archive
        closed any of its cards, and no person cancelled or dismissed its Work. The turn's PR or process check is the
        only input that may stay, and the continuation replaces it. completion() then reads the card from the adapter:
        the same card and input, no completed run, the claim's run last and ended without success, and STALE_NOTE as
        the next event after the latest archive, with only comments after it. Coconut created the request once, no Stop
        or routine touched it, and its last cancellation came after that archive. Every source that links the card or
        the request is still closed, left before the archive, and has no open card. Anything else stays final.

        The worker can also park the card with kanban_schedule. Hermes then ends the claim's run as scheduled and the
        request completes, so the reconciler has nothing to cancel after the archive. The work is unfinished, though
        (see settled_scheduled_continuation). That completed request continues the same way: its claim's run ended as
        scheduled, and no person or routine touched it, nothing cancelled it, and it completed before the archive.
        """
        claim = task.get('_kanban_claim') or {}
        turn = task.get('_native_turn_id')
        parked = task['status'] == 'completed'
        if (task['status'] not in ('cancelled', 'completed') or any(field in task for field in ROUTINE_PROVENANCE)
                or not card.get('id') or task.get('_native_outcome_id') != card['id']
                or claim.get('native_id') != card.get('id') or claim.get('external_id') != task['id']
                or not turn or card.get('input_id') != turn
                or type(claim.get('run_id')) is not int or task.get('_kanban_started') != claim['run_id']
                or (task.get('_self_recovery') or {}).get('unknown_tools')
                or any(task.get(key) for key in OPEN_INTAKE if key not in FAILED_TURN_INPUT)
                or card.get('status') != 'archived' or card.get('tenant') != task['bot_id']
                or card.get('created_by') != 'coconut' or card.get('current_run_id') or card.get('worker_pid')
                or card.get('dependencies')):
            return False
        worker = self.running.get(task['id'])
        if (worker and worker.is_alive()) or self._native_task_receipts(task['id'], state='executing') or self.db.execute(
                "SELECT 1 FROM decisions WHERE ? IN (json_extract(data,'$.task_id'), json_extract(data,'$._execution_task_id')) "
                "AND json_extract(data,'$.decided_by')='archive' LIMIT 1", (task['id'],)).fetchone():
            return False
        # A person who cancelled or dismissed the request's Work after the archive ended it too.
        items = [a for a in self._all('artifacts') if a['kind'] in ('work', 'pr')
                 and (a['task_id'] == task['id'] or a['id'] == task.get('_work_context_id'))]
        if any(self._setting(prefix + self._work_identity(item)) for item in items
               for prefix in ('work.cancelled.', 'work.dismissed.')):
            return False
        native = completion()
        runs = native.get('runs')
        archive = native.get('archive')
        if not (native.get('id') == card['id'] and native.get('tenant') == task['bot_id']
                and native.get('idempotency_key') == card.get('idempotency_key') and native.get('status') == 'archived'
                and native.get('input_id') == turn and not native.get('current_run_id') and not native.get('worker_pid')
                and isinstance(runs, list) and runs
                and all(isinstance(r, dict) and r.get('ended') and r.get('outcome') != 'completed' for r in runs)
                and (runs[-1].get('outcome') == 'scheduled' if parked else runs[-1].get('outcome') in FAILED_RUN_OUTCOMES)
                and runs[-1].get('id') == claim['run_id']
                and isinstance(archive, dict) and archive.get('author') == 'coconut' and archive.get('note') == STALE_NOTE
                and archive.get('run_id') is None and type(archive.get('at')) is int
                and isinstance(archive.get('later'), list) and set(archive['later']) <= {'commented'}):
            return False
        ended = self._completed_before_archive if parked else self._cancelled_by_archive
        return ended(task, archive['at']) and self._sources_closed(task, card, archive['at'])

    def _cancelled_by_archive(self, task, archived_at):
        """Whether Coconut created the request once, no person stopped it or routine triggered it, and its last
        cancellation came after the archive at `archived_at`. A Stop records task.cancelled; the reconciler does not."""
        created, cancelled = [], None
        for bot, kind, raw, stamp in self.db.execute(REQUEST_EVENT_TIMES, (task['id'],)):
            if kind in ('routine.triggered', 'task.cancelled'):
                return False
            if kind == 'task.created':
                created.append(bot)
            if kind == 'task.updated' and json.loads(raw).get('status') == 'cancelled':
                cancelled = stamp
        try:
            after = datetime.fromisoformat(cancelled.replace('Z', '+00:00')).timestamp() >= archived_at
        except (AttributeError, ValueError):
            return False
        return after and created in ([task['bot_id']], [task.get('conversation_bot_id')])

    def _completed_before_archive(self, task, archived_at):
        """Whether Coconut created the request once, no person stopped it or routine triggered it, nothing ever
        cancelled it, and its last update completed it no later than the archive at `archived_at`."""
        created, completed = [], None
        for bot, kind, raw, stamp in self.db.execute(REQUEST_EVENT_TIMES, (task['id'],)):
            if kind in ('routine.triggered', 'task.cancelled'):
                return False
            if kind == 'task.created':
                created.append(bot)
            if kind == 'task.updated':
                status = json.loads(raw).get('status')
                if status == 'cancelled':
                    return False
                completed = stamp if status == 'completed' else None
        try:
            before = int(datetime.fromisoformat(completed.replace('Z', '+00:00')).timestamp()) <= archived_at
        except (AttributeError, ValueError):
            return False
        return before and created in ([task['bot_id']], [task.get('conversation_bot_id')])

    def _sources_closed(self, task, card, archived_at):
        """Whether every source item that links the card or the request is still closed and left before the archive.

        Each work source saves its links and the time each linked item left its open list (work_sources.source_left).
        An item that opened again has no such time. At least one link must exist, and no card that it names may wait.
        """
        linked = False
        for (raw,) in self.db.execute(WORK_SOURCE_STATES):
            state = json.loads(raw)
            links = state.get('native_links') if isinstance(state, dict) else None
            left = state.get('source_left') if isinstance(state, dict) else None
            for item, binding in (links.items() if isinstance(links, dict) else ()):
                names = binding.get('task_ids') if isinstance(binding, dict) else None
                if not isinstance(names, list) or not any(name in (card['id'], task['id']) for name in names):
                    continue
                when = left.get(item) if isinstance(left, dict) else None
                cards = binding.get('decision_ids', [])
                if not isinstance(when, (int, float)) or int(when) > archived_at or not isinstance(cards, list):
                    return False
                for key in cards:
                    row = self.db.execute('SELECT data FROM decisions WHERE id=?', (key,)).fetchone() if isinstance(key, str) else None
                    if row and json.loads(row[0]).get('status') in ('pending', 'approved'):
                        return False
                linked = True
        return linked

    def keeps_settled_failure(self, task, native):
        """Whether reconciliation keeps a failure whose native task Coconut's settle pass archived.

        The reconciler cancels a request when Hermes archives the task of its accepted input. The settle pass archives
        the task of every failed request, though, and no person stopped this one, so its failure stays and the
        occurrence or request can continue. The reconciler sees the archive within a pass, while the age check still
        tells it from an old idle archive. The task keeps that answer, so a later pass does not cancel it either.
        Only Coconut's records and the snapshot are read, because the reconciler holds the service lock. A continuation
        still reads the runs from the adapter before it starts (settled_failed_occurrence, settled_failed_request).
        """
        if task['status'] != 'failed' or native.get('status') != 'archived':
            return False
        if task.get('_native_settled_failure') == native['id']:
            return True
        if not (self._settled_failure(task, native) or self._settled_request_failure(task, native)):
            return False
        task['_native_settled_failure'] = native['id']
        self._put('tasks', task)
        return True

    def _settled_failure(self, task, card):
        """The checks of settled_failed_occurrence that read only Coconut's records and the snapshot card."""
        claim = task.get('_kanban_claim') or {}
        keys = {'coconut:' + task['id']}
        if task.get('_native_outcome_id') == card.get('id') and claim.get('input'):
            keys.add('coconut-followup:' + task['id'] + ':' + claim['input'])
        return (task.get('_native_outcome_id') in (None, card.get('id')) and card.get('idempotency_key') in keys
                and self._settled_occurrence(task, card, success=False))

    def _settled_occurrence(self, task, card, success):
        """Whether the settle pass archived the card of this finished occurrence, from Coconut's records and the snapshot.

        Hermes confirmed the outcome of this job and owner, and no person stopped the occurrence. See _settled_card.
        """
        job = task.get('_native_job') or {}
        owner = (job.get('fire_claim') or {}).get('by')
        if (task['status'] != ('completed' if success else 'failed') or not owner or not job.get('id')
                or not task.get('_native_job_finished') or task.get('_native_finish_unconfirmed')
                or task.get('routine_id') != str(uuid5(UUID(task['bot_id']), 'cron:' + job['id']))
                or task.get('_native_job_completion_receipt') != dict(job_id=job['id'], owner=owner, success=success)
                or not self._settled_card(task, card)):
            return False
        triggered = False
        for kind, raw in self.db.execute(COMPLETION_EVENTS, (task['id'], task['bot_id'])):
            data = json.loads(raw)
            if kind == 'routine.triggered':
                if triggered or data != dict(routine_id=task['routine_id'], task_id=task['id']):
                    return False
                triggered = True
            if kind == 'task.updated' and triggered and data.get('status') == 'cancelled':
                return False
        return triggered

    def _settled_request(self, task, card):
        """Whether the settle pass archived the card of this completed ordinary request.

        The request has no routine record, and no person ever stopped it. See _settled_card.
        """
        if task['status'] != 'completed' or any(field in task for field in ROUTINE_PROVENANCE) or not self._settled_card(task, card):
            return False
        return not any(kind == 'routine.triggered' or (kind == 'task.updated' and json.loads(raw).get('status') == 'cancelled')
                       for kind, raw in self.db.execute(COMPLETION_EVENTS, (task['id'], task['bot_id'])))

    def _settled_original_card(self, task, card):
        """Whether the archived card is the original card of this relayed request, and its worker parked it on this input.

        Board.sync makes one card for a request, under the key coconut:<id>, and later input reuses that card until it is
        done or archived. The key proves nothing more, so Coconut's own records must prove the rest. Another bot sent the
        request with coconut_message_bot, or an earlier worker relayed it through its native envelope (then the task holds
        that bot's envelope). That bot's message comes first.
        Coconut created the task once, and its first delivery is that message, from the requesting task to this bot.
        A return callback is no such request. The current input has its own proof: in this turn, while the task worked,
        the bridge saved a completed kanban_schedule call on this card, and Hermes reported that the run of the claim
        scheduled it. A card that someone else scheduled has no such receipt.
        """
        source = task.get('source_task_id')
        relay = task.get('_native_relay') or {}
        first = (task.get('_bot_messages') or [None])[0] or {}
        if (task.get('_is_return') or task.get('_native_intake') is not True
                or not isinstance(source, str) or not source or task.get('parent_task_id') != source
                or not task.get('source_bot_id') or task['source_bot_id'] == task['bot_id']
                or (relay and (relay.get('sender_bot_id') != task['source_bot_id']
                               or not isinstance(relay.get('envelope_id'), str) or not relay['envelope_id']))
                or first.get('sender_bot_id') != task['source_bot_id']):
            return False
        requester = self.db.execute('SELECT data FROM tasks WHERE id=?', (source,)).fetchone()
        delivery = self.db.execute(CREATION_DELIVERY, (task['id'],)).fetchone()
        if not requester or not delivery or json.loads(requester[0]).get('bot_id') != task['source_bot_id']:
            return False
        delivery = json.loads(delivery[0])
        receipt = delivery.get('receipt') or {}
        if (delivery.get('source_task_id') != source or delivery.get('target_bot_id') != task['bot_id']
                or delivery.get('turn_id') is not None or delivery.get('text') != first.get('text')
                or receipt.get('task_id') != task['id'] or receipt.get('target_bot_id') != task['bot_id']
                or not self._created_once(task)):
            return False
        return self._scheduled_by_claim(task, card)

    def _settled_migrated_card(self, task, card):
        """Whether the archived card is the card that migration made for this request's Work, parked on this input.

        Board.promote makes that card under the key coconut-outcome:<Work id>, and the request and its Work item both
        name it. The Work item belongs to this request and bot, and no person hid, merged, cancelled, or dismissed it.
        The key proves nothing more. The current input must come from an authenticated continuation of this card, and
        the claim's run must have scheduled the card in that turn (_scheduled_by_claim).
        """
        row = self.db.execute('SELECT data FROM artifacts WHERE id=?', (task['_work_context_id'],)).fetchone()
        item = json.loads(row[0]) if row else {}
        if (item.get('kind') != 'work' or item.get('task_id') != task['id'] or item.get('bot_id') != task['bot_id']
                or item.get('_native_outcome_id') != card['id'] or item.get('status') == 'cancelled'
                or item.get('_hidden') or item.get('_deleted') or item.get('_same_outcome_as')
                or any(self._setting(prefix + self._work_identity(item)) for prefix in ('work.cancelled.', 'work.dismissed.'))
                or task['_native_turn_id'] not in self._continuation_turns(task, card['id'])):
            return False
        return self._scheduled_by_claim(task, card)

    def _scheduled_by_claim(self, task, card):
        """Whether the bridge saved a completed kanban_schedule call on the card in the current turn, made by the claim's run."""
        run = task['_kanban_claim']['run_id']
        for record in self._native_task_receipts(task['id'], turn_id=task['_native_turn_id'], state='completed'):
            action = record.get('action') or {}
            try:
                result = json.loads(record.get('result'))
            except (TypeError, ValueError):
                continue
            if (action.get('name') == 'kanban_schedule' and (action.get('arguments') or {}).get('task_id') == card['id']
                    and isinstance(result, dict) and result.get('ok') is True and result.get('task_id') == card['id']
                    and result.get('status') == 'scheduled' and result.get('run_id') == run):
                return True
        return False

    def pending_source_decision(self, task):
        """Whether a decision about the work source item of this ordinary request is still pending.

        The work source audit records the item that it gives a task in _source_assignment. A later audit of the same
        item runs in a separate execution, and its card about that item can wait while the request's own card is
        archived. So can a card of a turn that carries out an answered proposal of that audit, or of a task that the
        audit assigned. Continuing the request then would judge the same item again before the person answers. So any
        pending decision of this bot whose task leads to the same source and item holds it. The request and each card's
        task resolve their item the same way, through work_sources.source_origin, and the nearest assignment wins.
        A request with no source item has no such decision. A malformed source assignment of the request holds it.
        A card whose item has no source, as in the oldest audit prompts, holds it when the item matches.
        Routine occurrences keep their own rules.
        """
        if any(field in task for field in ROUTINE_PROVENANCE):
            return False
        bot = task['bot_id']

        def read(identity):
            if identity == task['id']:
                return task
            row = self.db.execute('SELECT data FROM tasks WHERE id=?', (identity,)).fetchone()
            return json.loads(row[0]) if row else None

        def executed(child, parent):
            return bool(self.db.execute(EXECUTED_PROPOSAL, (parent, child, bot, *ANSWERED)).fetchone())

        if '_source_assignment' in task:
            item = task['_source_assignment']
        else:
            item = source_origin(task['id'], bot, read, executed)
            if not item:
                return False
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']
                or not isinstance(item.get('source_id'), str) or not item['source_id']):
            return True
        for (identity,) in self.db.execute(PENDING_DECISIONS, (bot,)).fetchall():
            origin = source_origin(identity, bot, read, executed)
            if origin.get('id') == item['id'] and origin.get('source_id') in (item['source_id'], None):
                return True
        return False

    def _settled_card(self, task, card, carried=()):
        """The checks of a settled archive that hold for every finished request.

        The claim names the card, which Coconut created in this tenant, and which has no run or worker left. Its last
        activity is within IDLE_AGE, so an old idle archive is not it. The request has no open input, worker,
        or unknown tool call, and no idle archive closed any of its cards. `carried` names open input that the caller
        hands to the continuation.
        """
        from .kanban_runtime import IDLE_AGE
        claim = task.get('_kanban_claim') or {}
        last = card.get('last_activity_at')
        if (claim.get('native_id') != card.get('id') or claim.get('external_id') != task['id']
                or any(task.get(key) for key in OPEN_INTAKE if key not in carried)
                or card.get('status') != 'archived' or card.get('tenant') != task['bot_id']
                or card.get('created_by') != 'coconut'
                or card.get('current_run_id') or card.get('worker_pid')
                or not isinstance(last, (int, float)) or time.time() - last >= IDLE_AGE):
            return False
        worker = self.running.get(task['id'])
        if worker and worker.is_alive():
            return False
        return not (self._native_task_receipts(task['id'], state='executing')
                    or self.db.execute("SELECT 1 FROM decisions WHERE ? IN (json_extract(data,'$.task_id'), "
                                       "json_extract(data,'$._execution_task_id')) AND json_extract(data,'$.decided_by')='archive' "
                                       "LIMIT 1", (task['id'],)).fetchone())

    def native_routine_completion_input(self, task):
        completion = self._native_routine_completion_input(task)
        job = task.get('_native_job') or {}
        owner = (job.get('fire_claim') or {}).get('by')
        if (not owner or not job.get('id') or not task.get('_native_job_finished')
                or task.get('routine_id') != str(uuid5(UUID(task['bot_id']), 'cron:' + job['id']))):
            return completion
        continuation = self._routine_continuation_input(task, job, owner)
        if not continuation:
            return completion
        if task.get('_native_job_completion_receipt') == dict(job_id=job['id'], owner=owner, success=False):
            completion = dict(job_id=job['id'], owner=owner, success=False, native_confirmed=True)
        return dict(continuation, completion=completion)

    def _native_routine_completion_input(self, task):
        job = task.get('_native_job') or {}
        owner = (job.get('fire_claim') or {}).get('by')
        if (not owner or not job.get('id') or not task.get('_native_job_finished')
                or task.get('status') in ('failed', 'cancelled')):
            return {}
        routine_id = str(uuid5(UUID(task['bot_id']), 'cron:' + job['id']))
        if task.get('routine_id') != routine_id:
            return {}
        expected = dict(job_id=job['id'], owner=owner, success=True)
        if task.get('_native_job_completion_receipt') == expected:
            return dict(expected, native_confirmed=True)
        triggered, succeeded = False, False
        rows = self.db.execute(COMPLETION_EVENTS, (task['id'], task['bot_id']))
        for kind, raw in rows:
            data = json.loads(raw)
            if kind == 'routine.triggered':
                if data.get('routine_id') != routine_id or data.get('task_id') != task['id'] or triggered:
                    return {}
                triggered = True
            if kind == 'task.updated' and triggered:
                # Reject ambiguous histories: a later successful turn cannot
                # establish that an earlier native finish was successful.
                if data.get('status') in ('failed', 'cancelled'):
                    return {}
                if data.get('status') == 'completed':
                    if data.get('routine_id') != routine_id or data.get('id') != task['id']:
                        return {}
                    succeeded = True
        return dict(job_id=job['id'], owner=owner, success=True) if triggered and succeeded else {}

    def _migrate_native_routine_prompts(self):
        # Only a visible user text of a routine task with a native job can change. SQLite joins them, so the start
        # parses neither whole table. The selection is wider than the check below, which decides as before.
        rows = self.db.execute(
            "SELECT m.rowid, m.data, t.data FROM messages m JOIN tasks t ON t.id = json_extract(m.data,'$.task_id') "
            "WHERE json_extract(m.data,'$.role')='user' AND json_extract(m.data,'$.kind')='text' "
            "AND coalesce(json_extract(t.data,'$.routine_id'),'') NOT IN ('', 0) "
            "AND coalesce(json_extract(t.data,'$._native_job'),'') NOT IN ('', 0)").fetchall()
        for message, task in ((json.loads(data), json.loads(task)) for _, data, task in sorted(rows)):
            task = task if task.get('routine_id') and task.get('_native_job') else None
            if (task and message['role'] == 'user' and message['kind'] == 'text'
                    and not message.get('_human_device_id') and not message.get('_hidden')
                    and message['text'] == task['_native_job'].get('prompt')):
                message['_hidden'] = True
                self._put('messages', message)

    def refresh_native_routines(self):
        if not hasattr(self.provider, 'native_settings'):
            return
        with self.lock:
            bots = [b for b in self._all('bots') if not b.get('_deleted')]
            legacy = [r for r in self._all('routines') if not r.get('_native_id') and not r.get('_deleted')]
        for routine in legacy:
            bot = next((b for b in bots if b['id'] == routine['bot_id']), None)
            if bot:
                self.provider.native_settings('routine_create', [bot], bot_id=bot['id'], changes=routine, request_key=routine['id'])
        read = getattr(self.provider, 'native_routines', None)
        snapshot = read(bots) if read else self.provider.native_settings('routines_read', bots)
        native = snapshot['routines']
        with self.transaction():
            from .service import encoded
            stored = [row[0] for row in self.db.execute('SELECT data FROM routines ORDER BY rowid')]
            previous = {r['id']: r for r in map(json.loads, stored)}
            for routine in native:
                routine['last_task_id'] = previous.get(routine['id'], {}).get('last_task_id')
            # Before the comparison, so that a pause that holds writes nothing. A change of the pause is an event, so
            # the phone shows it at once.
            self._pause_unserved_routines(native)
            for routine in native:
                if routine.get('error') != previous.get(routine['id'], {}).get('error') and routine['id'] in previous:
                    self._event('routine.updated', self._public(routine), routine['bot_id'])
            # Most passes find the same routines. Then the table already holds them, in the same order.
            if [encoded(routine) for routine in native] != stored:
                self.db.execute('DELETE FROM routines')
                for routine in native:
                    self._put('routines', routine)


    def _pause_unserved_routines(self, routines, served=None):
        """Mark each routine of a bot whose profile no gateway serves as paused, with the reason, and return the
        routines that changed. The caller holds the service lock.

        With the gateway on, only the gateway fires routines: Hermes runs an agent job of a profile only in that
        profile's gateway. A routine of a profile that no gateway serves, for example a profile that the person parked
        in Hermes, does not run. It runs again on its schedule when a gateway serves the profile, without a catch-up.
        The reason is the routine's `error`, which the phone shows and which keeps Coconut from a claim. A service
        without a gateway (fixture mode) runs routines with its provider, as before."""
        if not (hasattr(self, 'gateway_enabled') and self.gateway_enabled()):
            return []
        if served is None:
            served = self.gateway_served_bots()
        changed = []
        for routine in routines:
            reason = self._routine_pause(routine['bot_id'], served)
            paused = (routine.get('error') in UNSERVED_REASONS
                      or str(routine.get('error') or '').startswith(PARKED_PREFIXES))
            if reason and not routine.get('error'):
                routine['error'] = reason
                changed.append(routine)
            elif paused and routine.get('error') != reason:
                # The gateway serves the profile again, or the profile's state changed.
                routine['error'] = reason
                changed.append(routine)
        return changed

    def _routine_pause(self, bot_id, served=None):
        """The reason why the routines of this bot do not run now, or None (_pause_unserved_routines)."""
        if not (hasattr(self, 'gateway_enabled') and self.gateway_enabled()):
            return None
        if bot_id in (self.gateway_served_bots() if served is None else served):
            return None
        parked = self.profiles.home(self._get('bots', bot_id)) / 'gateway.parked'
        return ROUTINES_PARKED.format(path=parked) if parked.exists() else ROUTINES_UNSERVED

    def mutate_native_routine(self, method, parts, data, key):
        from .service import ServiceError
        if parts[1] == 'bots':
            bot = self._get('bots', parts[2])
            if bot['archived'] or bot.get('_deleted'):
                raise ServiceError(409, 'Restore this agent before creating a routine.')
            result = self.provider.native_settings('routine_create', [bot], bot_id=bot['id'], changes=data, request_key=key)
        else:
            try:
                routine = self._get('routines', parts[2])
            except ServiceError as error:
                if error.status != 404:
                    raise
                # The app can observe a native routine before the scheduler imports it.
                bots = [b for b in self._all('bots') if not b.get('_deleted')]
                routines = self.provider.native_settings('routines_read', bots)['routines']
                routine = next((r for r in routines if r['id'] == parts[2]
                                and r['bot_id'] in {b['id'] for b in bots} and not r.get('_deleted')), None)
                if routine is None:
                    raise error
                self._put('routines', routine)
            bot = self._get('bots', routine['bot_id'])
            if bot.get('_deleted'):
                raise ServiceError(404, 'This agent was deleted.')
            if parts[3:] == ['run']:
                active = self._active_routine_task(routine['id'])
                if active:
                    return self._public(active)
                return self.run_native_routine(routine)
            operation = 'routine_delete' if method == 'DELETE' else 'routine_update'
            result = self.provider.native_settings(operation, [bot], bot_id=bot['id'], native_id=routine['_native_id'], changes=data)
        if method == 'DELETE':
            self.db.execute('DELETE FROM routines WHERE id=?', (parts[2],))
        else:
            self._put('routines', result)
        self._event('routine.updated', self._public(result), bot['id'])
        return self._public(result)

    def run_native_routine(self, routine):
        """Run a routine now for the person or a bot. The gateway's cron runs it (_trigger_routine)."""
        from .service import ServiceError
        bot = self._get('bots', routine['bot_id'])
        if bot['archived'] or bot.get('_deleted'):
            raise ServiceError(409, 'Restore the agent before running this routine.')
        if routine.get('error'):
            raise ServiceError(409, routine['error'])
        pause = self._routine_pause(bot['id'])
        if pause:
            raise ServiceError(409, pause)
        if not (hasattr(self, 'gateway_enabled') and self.gateway_enabled()):
            raise ServiceError(409, ROUTINES_NEED_GATEWAY)
        # Only the gateway runs routines (_pause_unserved_routines). It fires a scheduled routine itself, and a run that
        # the person starts is a trigger in Hermes, which the gateway's cron runs at its next tick.
        return self._trigger_routine(routine, bot)

    def _trigger_routine(self, routine, bot):
        """Ask Hermes to run the routine now, and return the routine task that waits for the run. The caller holds the
        transaction.

        The gateway's cron runs the job at its next tick, and its cron run takes this task as its Coconut turn
        (gateway_chat.gateway_cron). A task that no run takes in TRIGGER_SECONDS fails with the reason
        (_expire_routine_triggers)."""
        from .service import now
        waiting = next((t for t in self._waiting_triggers() if t.get('routine_id') == routine['id']), None)
        if waiting:
            return self._public(waiting)
        job = self.provider.native_settings('routine_trigger', [bot], bot_id=bot['id'], native_id=routine['_native_id'])
        task = self._send(bot, {'text': routine['name'], 'mode': 'queue'}, record_message=False, human_authored=False)
        task = self._get('tasks', task['id'])
        task.update(routine_id=routine['id'], status='waiting', summary=TRIGGER_WAITING, updated_at=now(),
                    _native_job_finished=True, _gateway_trigger=dict(job_id=routine['_native_id'], at=now()))
        self._put('tasks', task)
        routine.update(last_task_id=task['id'], next_run_at=job.get('next_run_at') or routine.get('next_run_at'))
        self._put('routines', routine)
        self._event('task.updated', self._public(task), bot['id'], task['id'])
        self._event('routine.triggered', {'routine_id': routine['id'], 'task_id': task['id']}, bot['id'], task['id'])
        return self._public(task)

    def _waiting_triggers(self):
        """The routine tasks that wait for a run of the gateway (_trigger_routine). The status index finds them.

        A task that a gateway cron run took (`_gateway_cron`) no longer waits for a run. It can wait for its own work,
        for example for another bot, and its run ends it."""
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM tasks WHERE json_extract(data,'$.status')='waiting' "
            "AND json_extract(data,'$._gateway_trigger') IS NOT NULL "
            "AND json_extract(data,'$._gateway_cron') IS NULL ORDER BY rowid")]

    def _expire_routine_triggers(self):
        """End each routine task that waits for a run of the gateway. The caller holds the transaction.

        A run without an agent turn never takes the task: a script check that found no work ({"wakeAgent": false}),
        or a script-only routine. When Hermes ran the job after the trigger, the task ends with that run's result.
        A task that no run took in TRIGGER_SECONDS fails."""
        from .service import now
        limit = datetime.now(timezone.utc) - timedelta(seconds=TRIGGER_SECONDS)
        routines = {r['id']: r for r in self._all('routines')}
        for task in self._waiting_triggers():
            triggered = datetime.fromisoformat(task['_gateway_trigger']['at'].replace('Z', '+00:00'))
            job = routines.get(task.get('routine_id'), {}).get('_job') or {}
            ran = instant(job.get('last_run_at'))
            if ran is not None and ran >= triggered:
                failed = job.get('last_status') == 'error'
                task.update(status='failed' if failed else 'completed', updated_at=now(), summary=(
                    TRIGGER_SCRIPT_FAILED.format(error=str(job.get('last_error') or 'unknown')[:300]) if failed
                    else TRIGGER_NO_AGENT if job.get('no_agent') else TRIGGER_QUIET))
                self._put('tasks', task)
                self._event('task.updated', self._public(task), task['bot_id'], task['id'])
                continue
            if triggered > limit:
                continue
            task.update(status='failed', summary=TRIGGER_EXPIRED, updated_at=now())
            self._put('tasks', task)
            self._event('task.updated', self._public(task), task['bot_id'], task['id'])

    def tick_native_routines(self):
        """Read the routines of Hermes, and keep the pause of each routine that no gateway serves.

        A gateway that serves a bot's profile fires its routines, and its runs reach Coconut as turns
        (hermes_plugin/coconut/cron.py). The routines of a bot that no gateway serves are paused, with the reason.
        Coconut fires no routine itself."""
        if time.monotonic() < getattr(self, '_native_cron_next', 0):
            return
        self._native_cron_next = time.monotonic() + 15
        try:
            self.refresh_native_routines()
            served = self.gateway_served_bots() if hasattr(self, 'gateway_served_bots') else set()
            with self.transaction():
                self._expire_routine_triggers()
                # A change of the gateway since the last read of Hermes shows here.
                for routine in self._pause_unserved_routines(self._all('routines'), served):
                    self._put('routines', routine)
                    self._event('routine.updated', self._public(routine), routine['bot_id'])
        except Exception:
            with self.transaction():
                self._event('routines.unavailable', {'message': 'Hermes routines could not synchronize. The Mac will retry.'})
