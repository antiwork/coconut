"""Adapt Coconut requests to the installed Hermes dispatcher."""
from __future__ import annotations

import json
import contextlib
import os
import queue
import time
from pathlib import Path
import subprocess
import threading


class KanbanRequestError(RuntimeError):
    pass


class KanbanScheduler:
    def __init__(self, provider, root):
        self.provider, self.root = provider, Path(root)
        self.lock = threading.Lock()
        self.process = None
        self.tasks = {}
        self.observed_at = None
        self.log = None
        self.closed = False
        self.frames = None

    @contextlib.contextmanager
    def _request_fence(self, request):
        try:
            yield
        except KanbanRequestError:
            # The adapter confirmed it finished handling this request.
            raise
        except BaseException:
            # Unlink's service-state fence must outlive the adapter, including
            # a lost or malformed response. Stop/reap it before releasing the
            # request lock; the durable native event reconciles partial success.
            if request['op'] == 'unlink_dependency' and self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            raise

    def call(self, request, spawn=None):
        with self.lock, self._request_fence(request):
            if self.closed:
                raise RuntimeError('The Hermes Kanban adapter is closed.')
            if self.process is None or self.process.poll() is not None:
                if self.process is not None:
                    for pipe in (self.process.stdin, self.process.stdout):
                        if pipe and not pipe.closed:
                            with contextlib.suppress(OSError):
                                pipe.close()
                home = self.root / 'kanban'
                home.mkdir(exist_ok=True, mode=0o700)
                env = dict(os.environ, PYTHONPATH=str(self.provider.hermes_root), PYTHONUNBUFFERED='1',
                           HERMES_DISABLE_TELEMETRY='1')
                if self.log:
                    self.log.close()
                self.log = (home / 'adapter.log').open('a')
                self.process = subprocess.Popen(
                    [self.provider.python, str(Path(__file__).with_name('kanban_runtime.py')), str(home)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                    text=True, env=env, start_new_session=True)
                self.frames = queue.Queue()
                threading.Thread(target=self._read, args=(self.process, self.frames), daemon=True,
                                 name='kanban-events').start()
            self.process.stdin.write(json.dumps(request) + '\n')
            self.process.stdin.flush()
            while True:
                try:
                    frame = self.frames.get(timeout=30)
                except queue.Empty:
                    self.process.terminate()
                    raise RuntimeError('The Hermes Kanban adapter did not respond.')
                if isinstance(frame, BaseException):
                    raise frame
                if 'spawn' in frame:
                    try:
                        response = {'pid': spawn(frame['spawn'])}
                    except Exception as error:
                        response = {'error': str(error)}
                    self.process.stdin.write(json.dumps(response) + '\n')
                    self.process.stdin.flush()
                    continue
                if 'error' in frame:
                    raise KanbanRequestError(frame['error'])
                result = frame['result']
                if 'tasks' in result:
                    from .service import now
                    self.tasks = {t['external_id']: t for t in result['tasks']}
                    self.observed_at = now()
                return result

    @staticmethod
    def _read(process, frames):
        try:
            for line in process.stdout:
                frames.put(json.loads(line))
        except Exception as error:
            frames.put(error)
        finally:
            frames.put(RuntimeError('The Hermes Kanban adapter stopped.'))

    def close(self):
        with self.lock:
            self.closed = True
            if self.process and self.process.poll() is None:
                self.process.stdin.close()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    self.process.wait(timeout=5)
            if self.process:
                for pipe in (self.process.stdin, self.process.stdout):
                    if pipe and not pipe.closed:
                        with contextlib.suppress(OSError):
                            pipe.close()
            if self.log:
                self.log.close()


def native_state(status):
    return {'queued': 'ready', 'scheduled': 'scheduled', 'waiting': 'scheduled', 'paused': 'scheduled', 'working': 'running',
            'completed': 'done', 'cancelled': 'archived', 'needs_you': 'review'}.get(status, 'blocked')


from .native_outcomes import NativeOutcomes


class NativeKanban(NativeOutcomes):
    def _init_kanban(self):
        self._kanban_visible = None
        self.kanban = (KanbanScheduler(self.provider, self.root)
                       if hasattr(self.provider, 'prepare_kanban_worker') else None)

    def _kanban_rows(self):
        from .work_capacity import execution_key
        known = self.kanban.tasks
        retained = json.dumps([i for i, task in known.items() if task.get('status') not in ('done', 'archived')])
        tasks = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM tasks WHERE json_extract(data,'$.status') IN (SELECT value FROM json_each(?)) "
            "OR id IN (SELECT value FROM json_each(?)) ORDER BY rowid", (json.dumps(sorted(self.ACTIVE)), retained))]
        active_keys = {execution_key(self._get('tasks', identity)) for identity in self.running}
        active_keys.update(execution_key(self._get('tasks', self._get('contexts', identity)['task_id']))
                           for identity in self.context_threads)
        selected = set(active_keys)
        refresh_active = any(self._get('contexts', i).get('_decision_refresh') for i in self.context_threads)
        rows = []
        for context in (json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM contexts WHERE json_extract(data,'$.status') IN ('queued','working') "
                "OR id IN (SELECT value FROM json_each(?)) ORDER BY rowid", (retained,))):
            if context['status'] not in ('queued', 'working') and known.get(context['id'], {}).get('status', 'done') in ('done', 'archived'):
                continue
            task = self._get('tasks', context['task_id'])
            self._ensure_execution_session(task)
            key = execution_key(task)
            state = native_state(context['status'])
            if context['status'] == 'queued':
                available = not (context.get('_decision_refresh') and refresh_active)
                state = 'ready' if available and key not in selected and not self._task_paused(task) and task['status'] != 'cancelled' else 'scheduled'
                if state == 'ready':
                    selected.add(key)
                    refresh_active |= bool(context.get('_decision_refresh'))
            rows.append(self._kanban_row(context, task, state, priority=30))
        for task in self._dispatch_order(tasks):
            if task.get('_work_status_only') or task.get('_chat_session'):
                continue
            if task['status'] not in self.ACTIVE and known.get(task['id'], {}).get('status', 'done') in ('done', 'archived'):
                continue
            self._ensure_execution_session(task)
            if task.get('_native_outcome_id') and not task.get('_native_turn_id'):
                from .service import uid
                task['_native_turn_id'] = uid()
                self._put('tasks', task)
            key = execution_key(task)
            state = native_state(task['status'])
            if task['status'] in ('queued', 'scheduled'):
                state = 'ready' if self._dispatch_eligible(task, time.time()) and key not in selected else 'scheduled'
                if state == 'ready':
                    selected.add(key)
            priority = 20 if task.get('_resume_decision') or task.get('_is_return') or task.get('_background_completion') else 10
            if self._assignment(task).get('_source_audit_prompt'):
                priority = 0
            rows.append(self._kanban_row(task, task, state, priority))
        return rows

    def _dispatch_status_request(self):
        # A read-only progress question keeps its own bounded session when work capacity is full.
        if any(self._get('tasks', identity).get('_work_status_only') or self._get('tasks', identity).get('_chat_session') for identity in self.running):
            return
        for task in self._all('tasks'):
            if not task.get('_work_status_only') or not self._dispatch_eligible(task, time.time()):
                continue
            self._ensure_execution_session(task)
            worker = threading.Thread(target=self._run_task, args=(task['id'],), daemon=True, name='work-status')
            self.running[task['id']] = worker
            worker.start()
            return

    def _kanban_row(self, source, task, state, priority):
        from .work_capacity import execution_key
        from .work_workspace import preferred
        # Read a path hint here. The provider verifies Git outside the service lock before execution.
        workspace = preferred(self.root / 'workspaces', execution_key(task), verify=False)
        changed = workspace != self.root / 'workspaces' / execution_key(task)
        native = self.kanban.tasks.get(task['id']) or next((n for n in self.kanban.tasks.values()
            if n['id'] == task.get('_native_outcome_id')), {})
        if task.get('_native_outcome_id') and native.get('workspace_path') and not changed:
            workspace = Path(native['workspace_path'])
        return dict(id=source['id'], bot_id=task['bot_id'], title=task['title'], state=state,
                    input=str(source.get('_native_turn_id') or source.get('_message_id') or source['id']),
                    previous_input=(source.get('_kanban_claim') or {}).get('input'),
                    recovering=bool(source['id'] == task['id'] and task.get('_native_outcome_id') and task.get('_self_recovery')),
                    workspace=str(workspace), workspace_changed=changed, priority=priority, summary=source.get('summary', ''),
                    execution_id=execution_key(task), native_id=task.get('_native_outcome_id') if source['id'] == task['id'] else None,
                    created_at=source.get('created_at', task.get('created_at', '')))

    def _dispatch_kanban(self):
        from .work_capacity import execution_key, state as capacity_state
        if self.restart_pending or self.stopping.is_set():
            # Finish committed turns, but neither adopt work nor claim/start any
            # new turn while the operator drain owns the dispatch gate.
            self.kanban.call({'op': 'snapshot'})
            self._reconcile_kanban()
            with self.changed:
                self._kanban_dispatch_uncertain = False
                self.changed.wait(.2)
            return
        self.finish_inbox_swipes()
        self._drain_returns()
        # Read committed claims first. A lost adapter response cannot create another turn.
        self.kanban.call({'op': 'snapshot'})
        with self.transaction():
            self._migrate_native_work()
        self._adopt_native_outcomes()
        self._reconcile_kanban()
        with self.changed:
            self._kanban_dispatch_uncertain = False
            if self.restart_pending or self.stopping.is_set():
                self.changed.wait(.2)
                return
            self._cancel_stale_decision_refreshes()
            self.running = {k: v for k, v in self.running.items() if v.is_alive()}
            self.context_threads = {k: v for k, v in self.context_threads.items() if v.is_alive()}
            self._dispatch_status_request()
            capacity = capacity_state(self._setting('work_capacity'))['effective_limit']
            self._dispatch_chat(capacity)
            rows = self._kanban_rows()
            by_id = {r['id']: r for r in rows}
            contexts = {c['id']: c for c in self._all('contexts')}
            foreground = sum(bool(self._get('tasks', i).get('_chat_session') or
                self._get('tasks', i).get('_work_status_only')) for i in self.running)
            capacity = max(0, capacity - max(0, foreground - 1))
            # READY continuation must not change input between row capture and
            # native claim/spawn. Never wait for the adapter under the service lock.
            self._kanban_claiming += 1
            # A lost response stays fenced until a later snapshot is reconciled.
            self._kanban_dispatch_uncertain = True
        def spawn(claim):
            row = by_id.get(claim['external_id'])
            if row is None or row['state'] != 'ready':
                raise ValueError('This request is no longer ready.')
            with self.lock:
                context = contexts.get(row['id'])
                task = self._get('tasks', context['task_id'] if context else row['id'])
                if (self._task_paused(task) or self.restart_pending or self.stopping.is_set() or task['status'] == 'cancelled'
                        or (context and self._get('contexts', context['id'])['status'] != 'queued')
                        or (not context and task['status'] not in ('queued', 'scheduled'))
                        or row['id'] in self.running or row['id'] in self.context_threads):
                    raise ValueError('This request stopped before dispatch.')
                bot = self._get('bots', task['bot_id'])
            pid = self.provider.prepare_kanban_worker(row['id'], bot, claim['workspace'],
                                                      execution_key(task), claim['native_id'])
            with self.transaction():
                table = 'contexts' if context else 'tasks'
                item = self._get(table, row['id'])
                item['_kanban_claim'] = dict(claim, pid=pid, input=row['input'], generation=self.generation)
                self._put(table, item)
            return pid
        try:
            self.kanban.call({'op': 'tick', 'rows': rows, 'capacity': capacity}, spawn)
            self._reconcile_kanban()
            with self.lock:
                self._kanban_dispatch_uncertain = False
        finally:
            with self.lock:
                self._kanban_claiming -= 1
        if hasattr(self.provider, 'trim_kanban_workers'):
            with self.lock:
                keep = set()
                for record in self._pending_completion_receipts():
                    if self._get('tasks', record['task_id'])['status'] == 'cancelled':
                        continue
                    identity = self._receipt_native_worker_id(record)
                    if identity:
                        keep.add(identity)
            self.provider.trim_kanban_workers(keep)
        self._publish_kanban_changes()
        with self.changed:
            self.changed.wait(1)

    def _publish_kanban_changes(self):
        visible = {t['id']: tuple(t.get(key) for key in ('status', 'priority', 'current_run_id', 'title',
            'progress_note', 'progress_at', 'last_heartbeat_at', 'outcome_summary', 'last_failure_error', 'dispatch_blocker', 'block_kind', 'dependencies'))
            for t in self.kanban.tasks.values()}
        with self.changed:
            if visible != self._kanban_visible:
                previous = self._kanban_visible or {}
                affected = {identity for identity in visible.keys() | previous.keys()
                            if visible.get(identity) != previous.get(identity)}
                # A changed claim or priority can change every ready task's queue position.
                affected.update(t['id'] for t in self.kanban.tasks.values() if t['status'] == 'ready')
                self._kanban_visible = visible
                self._event('work.updated', {'native_task_ids': sorted(affected)})

    def _reconcile_kanban(self):
        from .service import now
        finishes = []
        with self.changed:
            for table, workers in (('tasks', self.running), ('contexts', self.context_threads)):
                records = {item['id']: item for item in (json.loads(row[0]) for row in self.db.execute(
                    'SELECT data FROM ' + table + ' WHERE id IN (SELECT value FROM json_each(?))',
                    (json.dumps(list(self.kanban.tasks)),)))}
                for identity, native in self.kanban.tasks.items():
                    if identity in workers and workers[identity].is_alive():
                        continue
                    item = records.get(identity)
                    if item is None:
                        continue
                    claim = item.get('_kanban_claim')
                    if self.restart_pending:
                        # Only finish our own already-started, completed turn.
                        # Do not reclaim preparations/orphans during maintenance.
                        if (not claim or claim.get('generation') != self.generation
                                or item.get('_kanban_started') != claim.get('run_id')
                                or item['status'] == 'working'):
                            continue
                    if item.get('_native_outcome_id'):
                        if item['_native_outcome_id'] != native['id']:
                            item['_native_outcome_id'] = native['id']
                            self._put(table, item)
                            for artifact in self._all('artifacts'):
                                if artifact.get('_native_outcome_id') and artifact.get('task_id') == identity:
                                    artifact['_native_outcome_id'] = native['id']
                                    self._put('artifacts', artifact)
                                    self._event('artifact.updated', self._public(artifact), artifact['bot_id'], identity)
                        accepted = str(item.get('_native_turn_id') or item.get('_message_id') or item['id'])
                        if (native['status'] in ('done', 'archived') and native.get('input_id') == accepted
                                and item['status'] not in ('completed', 'cancelled')):
                            if native['status'] == 'done' and item.get('_steering'):
                                self._advance_input(item)
                                item.update(status='queued', summary='Applying your follow-up')
                            else:
                                item.update(status='completed' if native['status'] == 'done' else 'cancelled',
                                    summary=native.get('outcome_summary') or 'Completed')
                            item['updated_at'] = now()
                            self._put(table, item)
                            self._event('task.updated', self._public(item), item['bot_id'], identity)
                            self._refresh_bot(item['bot_id'])
                        if (native['status'] == 'done' and native.get('input_id') == accepted
                                and item['status'] == 'completed' and not item.get('_steering')):
                            self._prepare_return(item, native.get('outcome_summary') or item['summary'], 'completed')
                            self._put(table, item)
                        self._native_retry(item, native)
                        if table == 'tasks':
                            self._native_block_check(item, native)
                    refused = native.get('dispatch_blocker') == 'blocker_auth'
                    failed = native['status'] in ('blocked', 'triage') and native.get('consecutive_failures', 0)
                    if not item.get('_native_outcome_id') and (refused or failed) and item['status'] in ('queued', 'scheduled'):
                        item.update(status='failed', summary='The worker could not start. Coconut will check the saved task before retrying.', error_code='native_dispatch_failed')
                        if table == 'tasks':
                            self._finish_assignment_turn(item, 'Task failed: ' + item['summary'], 'failed')
                        self._put(table, item)
                        self._event('task.updated' if table == 'tasks' else 'context.failed', self._public(item), item.get('bot_id'), item.get('task_id', identity))
                        self._refresh_bot(item['bot_id'])
                    if native.get('claim_source_status') == 'review':
                        continue
                    if (native['status'] == 'running' and not native.get('worker_pid')
                            and item['status'] != 'working'):
                        # No turn starts before the native PID commit. Reclaim an interrupted preparation.
                        if hasattr(self.provider, 'release_kanban_preparation'):
                            self.provider.release_kanban_preparation(identity)
                        finishes.append(dict(op='finish', id=native['id'], run_id=native['current_run_id'],
                                             state=native_state(item['status']), summary='Worker preparation stopped before dispatch.'))
                        continue
                    if not claim or native['current_run_id'] != claim['run_id'] or native['status'] != 'running':
                        continue
                    same_process = claim.get('generation') == self.generation
                    pending_start = (item.get('_kanban_started') != claim['run_id'] and same_process
                                     and native.get('worker_pid') == claim.get('pid'))
                    status = item['status']
                    if pending_start and status in ('queued', 'scheduled') and not self.stopping.is_set() and not self.restart_pending:
                        item['_kanban_started'] = claim['run_id']
                        self._put(table, item)
                        if table == 'contexts':
                            self._start_context(identity, completed=self._kanban_finished)
                        else:
                            def run(identity=identity):
                                try:
                                    self._run_task(identity)
                                finally:
                                    self._kanban_finished()
                            worker = threading.Thread(target=run, daemon=True, name='kanban-task-' + identity[:8])
                            workers[identity] = worker
                            worker.start()
                    else:
                        if hasattr(self.provider, 'release_kanban_preparation'):
                            self.provider.release_kanban_preparation(identity)
                        state = native_state(status)
                        if state == 'running':
                            state = 'blocked'
                        finishes.append(dict(op='finish', id=claim['native_id'], run_id=claim['run_id'], state=state,
                                             summary=item.get('summary') or 'The turn stopped.'))
        # The durable Coconut result is the outbox. Retry a lost finish acknowledgement on the next tick.
        for request in finishes:
            self.kanban.call(request)
        if finishes:
            self.kanban.call({'op': 'snapshot'})
            with self.transaction():
                for identity, native in self.kanban.tasks.items():
                    row = self.db.execute('SELECT data FROM tasks WHERE id=?', (identity,)).fetchone()
                    if native['status'] == 'ready' and row:
                        task = json.loads(row[0])
                        if task.get('_native_outcome_id'):
                            self._native_retry(task, native)

    def _kanban_finished(self):
        with self.changed:
            self.changed.notify_all()

    def _native_queue_positions(self, tasks, timestamp):
        native = self.kanban.tasks
        ordered = sorted((n for i, n in native.items() if i in tasks and n['status'] == 'ready'
                          and not n.get('dispatch_blocker')),
                         key=lambda n: (-n['priority'], n['created_at']))
        return {n['external_id']: index + 1 for index, n in enumerate(ordered)}
