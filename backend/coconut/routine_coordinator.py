"""Dispatch native schedules while Coconut owns their durable conversation tasks."""
from datetime import datetime, timezone
import time
import json
from uuid import UUID, uuid5


# Statements of the routine pass. Each needs an index and no scan. A test reads the plan of each one.
ROUTINE_EVENTS = 'SELECT type,data,created_at FROM events WHERE task_id=? AND bot_id=? ORDER BY seq'
COMPLETION_EVENTS = 'SELECT type,data FROM events WHERE task_id=? AND bot_id=? ORDER BY seq'
ROUTINE_DELIVERIES = "SELECT data FROM deliveries WHERE json_extract(data,'$.receipt.task_id')=? ORDER BY rowid DESC"
# Open intake of a waiting occurrence. Any of these means that the occurrence still works or has input to take.
OPEN_INTAKE = ('_steering', '_live_messages', '_blocked_followup', '_native_reply_pending', '_return_pending',
               '_resume_decision', '_background_completion', '_pr_review', '_work_resume', '_routine_pending_bot_result')
# Hermes outcomes of a run that ended without success. A completed run, or one that an archive reclaimed, is not one.
# An archive reclaims only the current run, so an earlier reclaimed run comes from a restart.
FAILED_RUN_OUTCOMES = ('blocked', 'crashed', 'gave_up', 'spawn_failed', 'stale', 'timed_out', 'worker_failed')


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
            source = self.db.execute('SELECT data FROM tasks WHERE id=?', (delivery.get('source_task_id'),)).fetchone()
            if not source:
                continue
            source = json.loads(source[0])
            if source['bot_id'] not in (task['bot_id'], task.get('source_bot_id')):
                continue
            if not any(m.get('sender_bot_id') == source['bot_id'] and m.get('text') == delivery.get('text')
                       for m in task.get('_bot_messages', [])):
                continue
            return dict(job_id=job['id'], owner=owner,
                        continuation=dict(task_id=task['id'], turn_id=task['_native_turn_id'],
                                          native_task_id=task['_native_outcome_id'], delivery_id=delivery['id']))
        return {}

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
        cancels the request instead, and the phone's idle archive takes only a task that waited for IDLE_AGE. Hermes
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

    def keeps_settled_failure(self, task, native):
        """Whether reconciliation keeps a failure whose native task Coconut's settle pass archived.

        The reconciler cancels a request when Hermes archives the task of its accepted input. The settle pass archives
        the task of every failed request, though, and no person stopped this one, so its failure stays and the
        occurrence can continue. The reconciler sees the archive within a pass, while the age check still tells it
        from the phone's idle archive. The task keeps that answer, so a later pass does not cancel it either.
        Only Coconut's records and the snapshot are read, because the reconciler holds the service lock.
        """
        if task['status'] != 'failed' or native.get('status') != 'archived':
            return False
        if task.get('_native_settled_failure') == native['id']:
            return True
        if not self._settled_failure(task, native):
            return False
        task['_native_settled_failure'] = native['id']
        self._put('tasks', task)
        return True

    def _settled_failure(self, task, card):
        """The checks of settled_failed_occurrence that read only Coconut's records and the snapshot card."""
        from .kanban_runtime import IDLE_AGE
        job = task.get('_native_job') or {}
        owner = (job.get('fire_claim') or {}).get('by')
        claim = task.get('_kanban_claim') or {}
        last = card.get('last_activity_at')
        keys = {'coconut:' + task['id']}
        if task.get('_native_outcome_id') == card.get('id') and claim.get('input'):
            keys.add('coconut-followup:' + task['id'] + ':' + claim['input'])
        if (task['status'] != 'failed' or not owner or not job.get('id') or not task.get('_native_job_finished')
                or task.get('_native_finish_unconfirmed')
                or task.get('routine_id') != str(uuid5(UUID(task['bot_id']), 'cron:' + job['id']))
                or task.get('_native_job_completion_receipt') != dict(job_id=job['id'], owner=owner, success=False)
                or task.get('_native_outcome_id') not in (None, card.get('id'))
                or claim.get('native_id') != card.get('id') or claim.get('external_id') != task['id']
                or any(task.get(key) for key in OPEN_INTAKE)
                or card.get('status') != 'archived' or card.get('tenant') != task['bot_id']
                or card.get('created_by') != 'coconut' or card.get('idempotency_key') not in keys
                or card.get('current_run_id') or card.get('worker_pid')
                or not isinstance(last, (int, float)) or time.time() - last >= IDLE_AGE):
            return False
        worker = self.running.get(task['id'])
        if worker and worker.is_alive():
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
        if (not triggered or self._native_task_receipts(task['id'], state='executing')
                or self.db.execute("SELECT 1 FROM decisions WHERE ? IN (json_extract(data,'$.task_id'), "
                                   "json_extract(data,'$._execution_task_id')) AND json_extract(data,'$.decided_by')='archive' "
                                   "LIMIT 1", (task['id'],)).fetchone()):
            return False
        return True

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

    def _capture_occurrence(self, task, record):
        """Keep the card and run that ended this occurrence. A later turn overwrites _kanban_claim with its own."""
        from .routine_reports import value
        claim, result = task.get('_kanban_claim') or {}, value(record)
        if ((task.get('_native_job') or {}).get('id') and not task.get('_native_job_finished')
                and not task.get('_native_occurrence') and record.get('turn_id')
                and result.get('ok') is True and claim.get('external_id') == task['id']
                and result.get('task_id') == claim.get('native_id') and result.get('run_id') == claim.get('run_id')
                and task.get('_native_outcome_id') in (None, claim.get('native_id'))):
            task['_native_occurrence'] = dict(native_id=claim['native_id'], run_id=claim['run_id'],
                                              turn_id=record['turn_id'], receipt_id=record['id'], input=claim.get('input'))
            self._put('tasks', task)

    def _occurrence_candidates(self, task):
        """The cards that this waiting occurrence reported done, from Coconut's own records. Native state is not read here.

        The claim of the task names its latest turn, so it cannot say which card ended the occurrence. The receipt of
        the tool call can: it is saved with the task, the turn, the native card and its run. Any guard that keeps
        the occurrence active, or any doubt about its history, gives no candidate.
        """
        from .routine_reports import kanban_completions
        job = task.get('_native_job') or {}
        owner = (job.get('fire_claim') or {}).get('by')
        if (task['status'] != 'waiting' or not owner or not job.get('id') or task.get('_native_job_finished')
                or task.get('routine_id') != str(uuid5(UUID(task['bot_id']), 'cron:' + job['id']))
                or task.get('_assignment_task_id') or self._task_paused(task)
                or any(task.get(key) for key in OPEN_INTAKE)
                or (task.get('_self_recovery') or {}).get('unknown_tools')):
            return []
        worker = self.running.get(task['id'])
        if worker and worker.is_alive():
            return []
        triggered = False
        for kind, raw in self.db.execute(COMPLETION_EVENTS, (task['id'], task['bot_id'])):
            data = json.loads(raw)
            if kind == 'routine.triggered':
                if triggered or data != dict(routine_id=task['routine_id'], task_id=task['id']):
                    return []
                triggered = True
            if kind == 'task.updated' and triggered and data.get('status') in ('failed', 'cancelled'):
                return []
        if not triggered:
            return []
        receipts = self._native_task_receipts(task['id'])
        if any(r.get('state') == 'executing' for r in receipts):
            return []
        turns = {}
        for receipt, result in kanban_completions([r for r in receipts if r.get('state') == 'completed']):
            if (isinstance(result.get('task_id'), str) and result['task_id'] and receipt.get('turn_id')
                    and type(result.get('run_id')) is int):
                found = turns.setdefault((result['task_id'], result['run_id']), {})
                found.setdefault(receipt['turn_id'], receipt['id'])
        saved = task.get('_native_occurrence')
        if saved:
            turns = {key: found for key, found in turns.items() if key == (saved.get('native_id'), saved.get('run_id'))
                     and found.get(saved.get('turn_id')) == saved.get('receipt_id')}
        # A card that two turns claim to have ended is not an unambiguous completion.
        candidates = [dict(native_id=key[0], run_id=key[1], turn_id=next(iter(found)), receipt_id=next(iter(found.values())))
                      for key, found in turns.items() if len(found) == 1]
        if (self._background_pending(task['id'])
                or self.db.execute("SELECT 1 FROM contexts WHERE json_extract(data,'$.status') IN ('queued','working') "
                                   "AND json_extract(data,'$.task_id')=? LIMIT 1", (task['id'],)).fetchone()
                or self.db.execute("SELECT 1 FROM decisions WHERE json_extract(data,'$.task_id')=? "
                                   "AND json_extract(data,'$.status')='pending' LIMIT 1", (task['id'],)).fetchone()):
            return []
        return candidates

    def _authenticate_occurrence(self, task, candidates):
        """The one candidate that native state confirms. Run this without the service lock.

        The card is read by its native id, because Coconut's binding may now name a successor. It must be the
        card that Coconut created for this task and tenant, finished by exactly the run in the receipt, with no
        later run or live worker. Anything else, including an adapter that cannot answer, confirms nothing.
        """
        if not getattr(self, 'kanban', None) or len(candidates) > 4:
            return None
        # A card that stays unproven, or an adapter that stays silent, is asked again with growing pauses, not every tick.
        # The pause belongs to the exact candidates of the task, so a new receipt is read at once.
        probes = self.__dict__.setdefault('_release_probes', {})
        key = tuple((c['native_id'], c['run_id'], c['receipt_id']) for c in candidates)
        probe = probes.get(task['id'])
        if probe and probe['key'] == key and time.monotonic() < probe['due']:
            return None
        proof = self._read_occurrence_proof(task, candidates)
        if proof:
            probes.pop(task['id'], None)
        else:
            failures = probe['failures'] + 1 if probe and probe['key'] == key else 1
            probes[task['id']] = dict(key=key, failures=failures, due=time.monotonic() + min(15 * 2 ** failures, 900))
        return proof

    def _read_occurrence_proof(self, task, candidates):
        proven = []
        for candidate in candidates:
            try:
                card = self.kanban.call({'op': 'completion', 'id': candidate['native_id']}).get('card') or {}
            except Exception:
                continue
            runs = card.get('runs')
            if (card.get('id') == candidate['native_id'] and card.get('tenant') == task['bot_id']
                    and card.get('idempotency_key') == 'coconut:' + task['id'] and card.get('status') == 'done'
                    and not card.get('current_run_id') and not card.get('worker_pid')
                    and card.get('input_id') in (None, candidate['turn_id'])
                    and isinstance(runs, list) and all(isinstance(r, dict) and r.get('ended') for r in runs)
                    and [r.get('id') for r in runs if r.get('outcome') == 'completed'] == [candidate['run_id']]):
                proven.append(candidate)
        return proven[0] if len(proven) == 1 else None

    def _settle_release(self, task):
        """Ask the native job what became of a release that was tried. True when this pass is done with the occurrence.

        Hermes may have cleared the claim before the answer was lost, and new intake may since have made the occurrence
        look unfinished. Only the native job and its receipts say. A claim that is still ours is no release, so the
        pass goes on. Hermes' confirmed completion of this exact job and owner always ends the tracking. A claim that is
        gone or owned by another, with no such confirmation, ends it only for the occurrence that still is the one that
        ended natively, as the heartbeat does. Any other occurrence is active, and the normal pass decides its fate.
        A refreshed job that is missing says nothing about the claim, so the pass goes on.
        """
        job, tried = task['_native_job'], task.get('_native_release_tried')
        owner = (job.get('fire_claim') or {}).get('by')
        if not tried or not owner or (tried.get('job_id'), tried.get('owner')) != (job['id'], owner):
            return False
        routine = next((r for r in self._all('routines') if r.get('_native_id') == job['id'] and r['bot_id'] == task['bot_id']), None)
        native = (routine or {}).get('_job') or {}
        # Either record of Hermes names this exact job and owner. Which outcome it holds is its own business.
        confirmed = any(isinstance(proof, dict) and (proof.get('job_id'), proof.get('owner')) == (job['id'], owner)
                        for proof in (task.get('_native_job_completion_receipt'), native.get('_coconut_completion')))
        if not confirmed and (not native or (native.get('fire_claim') or {}).get('by') == owner
                              or not self._ended_occurrence(task)):
            return False
        with self.transaction():
            self._end_native_claim(task, confirmed)
        return True

    def _ended_occurrence(self, task):
        """Whether the occurrence is still the one whose ended card was authenticated for a release that was tried.

        New intake, or a card that the attempt did not authenticate, makes it a normal active occurrence again.
        """
        tried = task.get('_native_release_tried') or {}
        return any(all(tried.get(key) == candidate[key] for key in candidate)
                   for candidate in self._occurrence_candidates(task))

    def _end_native_claim(self, task, confirmed):
        """Stop reconciling this occurrence with Hermes. Nothing about the task itself changes."""
        first = not task.get('_native_finish_unconfirmed')
        task['_native_job_finished'] = True
        task['_native_finish_unconfirmed'] = not confirmed
        self._put('tasks', task)
        if not confirmed and first:
            self._event('routine.reconciliation_required', {'routine_id': task['routine_id'], 'message': 'Hermes no longer holds this routine claim, and did not confirm its completion.'}, task['bot_id'], task['id'])

    def _migrate_native_routine_prompts(self):
        tasks = {task['id']: task for task in self._all('tasks')
                 if task.get('routine_id') and task.get('_native_job')}
        for message in self._all('messages'):
            task = tasks.get(message.get('task_id'))
            if (task and message['role'] == 'user' and message['kind'] == 'text'
                    and not message.get('_human_device_id') and not message.get('_hidden')
                    and message['text'] == task['_native_job'].get('prompt')):
                message['_hidden'] = True
                self._put('messages', message)

    def native_scheduler_status(self):
        stamp = getattr(self, '_native_cron_last_success', None)
        running = (stamp is not None and time.monotonic() - stamp < 60
                   and not getattr(self, '_native_cron_error', False) and not self.stopping.is_set())
        return {'scheduler': 'coconut', 'scheduler_running': running}

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
        snapshot = self.provider.native_settings('routines_read', bots)
        native = snapshot['routines']
        completions = snapshot.get('completions', {})
        with self.transaction():
            from .native_routine_receipts import completion_id
            for task in self._all('tasks'):
                job = task.get('_native_job') or {}
                owner = (job.get('fire_claim') or {}).get('by')
                if job.get('id') and owner:
                    proof = completions.get(task['bot_id'], {}).get(completion_id(job['id'], owner))
                    if proof:
                        task['_native_job_completion_receipt'] = proof
                        self._put('tasks', task)
                        # An in-flight request still holds its pre-migration copy.
                        if task['id'] in self.running or task['status'] == 'working':
                            completions[task['bot_id']].pop(completion_id(job['id'], owner))
            previous = {r['id']: r for r in self._all('routines')}
            self.db.execute('DELETE FROM routines')
            for routine in native:
                routine['last_task_id'] = previous.get(routine['id'], {}).get('last_task_id')
                self._put('routines', routine)

        # Persist proof with its task before removing the redundant native file.
        # Unowned captured receipts can go too; later completions are untouched.
        for bot in bots:
            candidates = completions.get(bot['id'], {})
            if candidates:
                self.provider.native_settings('routine_prune_completions', [bot], bot_id=bot['id'],
                                              completions=candidates)

    def mutate_native_routine(self, method, parts, data, key):
        from .service import ServiceError
        if parts[1] == 'bots':
            bot = self._get('bots', parts[2])
            if bot['archived'] or bot.get('_deleted'):
                raise ServiceError(409, 'Restore this bot before creating a routine.')
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
                raise ServiceError(404, 'This bot was deleted.')
            if parts[3:] == ['run']:
                active = self._active_routine_task(routine['id'])
                if active:
                    return self._public(active)
                return self.claim_native_routine(routine, manual=True)
            operation = 'routine_delete' if method == 'DELETE' else 'routine_update'
            result = self.provider.native_settings(operation, [bot], bot_id=bot['id'], native_id=routine['_native_id'], changes=data)
        if method == 'DELETE':
            self.db.execute('DELETE FROM routines WHERE id=?', (parts[2],))
        else:
            self._put('routines', result)
        self._event('routine.updated', self._public(result), bot['id'])
        return self._public(result)

    def run_native_routine_tool(self, task, arguments):
        from .service import ServiceError
        identity = arguments.get('job_id')
        routine = next((r for r in self._all('routines')
                        if r.get('_native_id') == identity and r['bot_id'] == task['bot_id']), None)
        if not routine:
            return {'success': False, 'error': 'This routine is not synchronized. List routines and try again.'}
        try:
            active = self._active_routine_task(routine['id'])
            queued = self._public(active) if active else self.claim_native_routine(routine, manual=True)
        except ServiceError as error:
            return {'success': False, 'error': str(error)}
        return {'success': True, 'scheduler': 'coconut', 'job_id': identity,
                'task_id': queued['id'], 'status': queued['status']}

    def claim_native_routine(self, routine, manual=False):
        from .service import ServiceError, now
        bot = self._get('bots', routine['bot_id'])
        if bot['archived'] or bot.get('_deleted'):
            raise ServiceError(409, 'Restore the bot before running this routine.')
        if routine.get('error'):
            raise ServiceError(409, routine['error'])
        result = self.provider.native_settings('routine_claim', [bot], bot_id=bot['id'], native_id=routine['_native_id'], manual=manual)
        job = result['job']
        if not job:
            if manual:
                raise ServiceError(409, 'Hermes has already claimed this routine, or it cannot run now.')
            return None
        owner = job['fire_claim']['by']
        previous = next((t for t in self._all('tasks') if (t.get('_native_job', {}).get('fire_claim') or {}).get('by') == owner), None)
        if previous:
            return self._public(previous)
        task = self._send(bot, {'text': routine['name'], 'mode': 'queue'}, record_message=False, human_authored=False)
        task = self._get('tasks', task['id'])
        task.update(routine_id=routine['id'], _native_job=job)
        self._put('tasks', task)
        routine.update(last_task_id=task['id'], next_run_at=job.get('next_run_at'))
        self._put('routines', routine)
        self._event('routine.triggered', {'routine_id': routine['id'], 'task_id': task['id']}, bot['id'], task['id'])
        return self._public(task)

    def tick_native_routines(self):
        from .service import now
        if time.monotonic() < getattr(self, '_native_cron_next', 0):
            return
        self._native_cron_next = time.monotonic() + 15
        callbacks = []
        finished = set()
        awaiting = set()
        try:
            self.refresh_native_routines()
            with self.lock:
                tasks = self._all('tasks')
            for candidate in tasks:
                if not candidate.get('_native_job') or candidate.get('_native_job_finished'):
                    continue
                proof = None
                with self.lock:
                    task = self._get('tasks', candidate['id'])
                    if not task.get('_native_job') or task.get('_native_job_finished'):
                        continue
                    # A release that was tried is settled by native state first, whatever the task looks like now.
                    if self._settle_release(task):
                        finished.add(task['routine_id'])
                        continue
                    candidates = self._occurrence_candidates(task)
                if candidates:
                    awaiting.add(task['id'])
                    # Native state is read without the service lock, as the adapter can wait for it.
                    proof = self._authenticate_occurrence(task, candidates)
                with self.lock:
                    task = self._get('tasks', candidate['id'])
                    job = task.get('_native_job')
                    if not job or task.get('_native_job_finished'):
                        continue
                    bot = self._get('bots', task['bot_id'])
                    args = dict(bot_id=bot['id'], native_id=job['id'], owner=job['fire_claim']['by'])
                    # The occurrence ended itself natively and now only waits. Its checks run again with the lock held, and the
                    # saved intent lets a continuation that starts during the native call treat the claim as released.
                    closing = bool(proof) and proof in self._occurrence_candidates(task)
                    if closing:
                        with self.transaction():
                            task['_native_release'] = dict(proof, job_id=job['id'], owner=args['owner'])
                            task['_native_release_tried'] = dict(proof, job_id=job['id'], owner=args['owner'])
                            self._put('tasks', task)
                    elif task.get('_native_release'):
                        # An earlier attempt was abandoned, and its proof no longer holds. Nothing is being released.
                        with self.transaction():
                            task.pop('_native_release')
                            self._put('tasks', task)
                    active = task['status'] in self.ACTIVE and not closing
                    if not active:
                        finished.add(task['routine_id'])
                        output = '\n\n'.join(m['text'] for m in self._all('messages') if m.get('task_id') == task['id'] and m['role'] == 'assistant' and m['kind'] == 'text')
                succeeded = closing or task['status'] == 'completed'
                # Native locks can wait for tool receipts that need the service lock.
                if active:
                    result = self.provider.native_settings('routine_heartbeat', [bot], **args)
                else:
                    result = self.provider.native_settings('routine_finish', [bot], **args, success=succeeded,
                                                          output='' if task.get('_routine_silent') else output or ('' if closing else task.get('summary', '')), failure=None if succeeded else task.get('summary'))
                with self.transaction():
                    current = self._get('tasks', task['id'])
                    if current.get('_native_job') != job or current.get('_native_job_finished'):
                        continue
                    # A continuation can queue while Hermes releases the claim. Confirmed release is recorded regardless.
                    if closing and not result['ok'] and current.get('_native_release'):
                        # Hermes did not confirm, so nothing is being released. The next pass reads native state to see whether the
                        # claim is still held, and the intent waits for a pass that proves it again.
                        current.pop('_native_release')
                        self._put('tasks', current)
                    if current['status'] != task['status'] and not (closing and result['ok']):
                        continue
                    task = current
                    if active:
                        if not result['ok'] and self._ended_occurrence(task):
                            # The occurrence still is the one that ended natively, so it is retained Work. Hermes lost the claim, which is the routine's loss.
                            self._end_native_claim(task, False)
                            finished.add(task['routine_id'])
                        elif not result['ok']:
                            self._cancel(task, callbacks)
                            task = self._get('tasks', task['id'])
                            task.update(status='failed', summary='The routine lost its native execution claim.', _native_job_finished=True)
                            self._put('tasks', task)
                            self._event('task.updated', self._public(task), bot['id'], task['id'])
                    else:
                        task['_native_job_finished'] = bool(result['ok'])
                        if not result['ok'] and not task.get('_native_finish_unconfirmed'):
                            self._event('routine.reconciliation_required', {'routine_id': task['routine_id'], 'message': 'Hermes did not confirm this routine completion.'}, bot['id'], task['id'])
                        task['_native_finish_unconfirmed'] = not result['ok']
                        self._put('tasks', task)
            probes = getattr(self, '_release_probes', {})
            for identity in set(probes) - awaiting:
                del probes[identity]
            with self.transaction():
                # One grouping of the tasks serves every routine. A task that a claim adds belongs to its own routine,
                # so it cannot change the result for another routine.
                index = None
                for routine in self._all('routines'):
                    bot = self._get('bots', routine['bot_id'])
                    due = routine.get('next_run_at')
                    if routine['id'] in finished or routine.get('error') or not routine['enabled'] or bot['archived'] or self._active_routine_task(routine['id'], index := index or self._routine_index()):
                        continue
                    if routine.get('_recover_claim') or (due and datetime.fromisoformat(due.replace('Z', '+00:00')) <= datetime.now(timezone.utc)):
                        self.claim_native_routine(routine)
            self._native_cron_last_success = time.monotonic()
            self._native_cron_error = False
        except Exception:
            self._native_cron_error = True
            with self.transaction():
                self._event('routines.unavailable', {'message': 'Hermes routines could not synchronize. The Mac will retry.'})
        for callback in callbacks:
            callback()
