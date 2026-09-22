"""Dispatch native schedules while Coconut owns their durable conversation tasks."""
from datetime import datetime, timezone
import time
import json
from uuid import UUID, uuid5


class NativeRoutines:
    def native_routine_completion_input(self, task):
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
        rows = self.db.execute('SELECT type,data FROM events WHERE task_id=? AND bot_id=? ORDER BY seq',
                               (task['id'], task['bot_id']))
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

    def _routine_bot_authority(self, bot_id, job):
        # Reuse the native creation receipt. A changed prompt needs a new request.
        rows = self.db.execute("SELECT data FROM native_receipts WHERE "
            "json_extract(data,'$.action.name')='cronjob_manage' AND "
            "json_extract(data,'$.state')='completed' ORDER BY rowid DESC")
        for (raw,) in rows:
            record = json.loads(raw)
            arguments = record['action']['arguments']
            if arguments.get('action') != 'create' or not record.get('_bot_source_ids'):
                continue
            try:
                result = json.loads(record['result']) if isinstance(record['result'], str) else record['result']
            except (ValueError, TypeError):
                continue
            if not isinstance(result, dict) or result.get('success') is not True or result.get('job_id') != job['id']:
                continue
            owner = self._get('tasks', record['task_id'])
            if (owner['bot_id'] != bot_id or owner.get('routine_id')
                    or not arguments.get('prompt')
                    or arguments['prompt'].strip() != job.get('prompt', '').strip()):
                continue
            from .service import ServiceError
            sources = []
            for identity in record['_bot_source_ids']:
                try:
                    self._human_source(owner, identity, {'_preference_source_ids': record['_bot_source_ids']}, 'schedule bot changes')
                except ServiceError:
                    continue
                sources.append(identity)
            return dict(_preference_source_ids=sources, _routine_source_ids=sources,
                        _routine_source_thread_id=owner['thread_id'],
                        _bot_change_scope=job['id'] + ':' + job['fire_claim']['by'])
        return {}

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
        task.update(routine_id=routine['id'], _native_job=job, **self._routine_bot_authority(bot['id'], job))
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
        try:
            self.refresh_native_routines()
            with self.lock:
                tasks = self._all('tasks')
            for candidate in tasks:
                with self.lock:
                    task = self._get('tasks', candidate['id'])
                    job = task.get('_native_job')
                    if not job or task.get('_native_job_finished'):
                        continue
                    bot = self._get('bots', task['bot_id'])
                    args = dict(bot_id=bot['id'], native_id=job['id'], owner=job['fire_claim']['by'])
                    active = task['status'] in self.ACTIVE
                    if not active:
                        finished.add(task['routine_id'])
                        output = '\n\n'.join(m['text'] for m in self._all('messages') if m.get('task_id') == task['id'] and m['role'] == 'assistant' and m['kind'] == 'text')
                # Native locks can wait for tool receipts that need the service lock.
                if active:
                    result = self.provider.native_settings('routine_heartbeat', [bot], **args)
                else:
                    result = self.provider.native_settings('routine_finish', [bot], **args, success=task['status'] == 'completed',
                                                          output='' if task.get('_routine_silent') else output or task.get('summary', ''), failure=task.get('summary') if task['status'] != 'completed' else None)
                with self.transaction():
                    current = self._get('tasks', task['id'])
                    if current.get('_native_job') != job or current.get('_native_job_finished') or current['status'] != task['status']:
                        continue
                    task = current
                    if active:
                        if not result['ok']:
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
            with self.transaction():
                for routine in self._all('routines'):
                    bot = self._get('bots', routine['bot_id'])
                    due = routine.get('next_run_at')
                    if routine['id'] in finished or routine.get('error') or not routine['enabled'] or bot['archived'] or self._active_routine_task(routine['id']):
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
