"""Bound the queue pause before one launchd-managed service restart."""
import time

RESTART_DRAIN_SECONDS = 60
# An update stops the service. Turns that are still running would lose their tool results, and Coconut fails such a task at the
# next start so that it never repeats an action. So the updater first asks the service to drain (see `poll_service_drain`).
DRAIN_REQUEST_FILE = 'drain-request'
DRAINED_FILE = 'drained'
# A restart for a new Hermes waits for the running turns as long as an update does (host.DRAIN_SECONDS).
HERMES_DRAIN_SECONDS = 600

RESTART_SCHEMA = {
    'name': 'coconut_restart_service',
    'description': 'Request one Coconut service restart after active turns finish. Dispatch pauses for at most 60 seconds, then resumes while the restart waits for idle workers. Finish your reply after requesting it; do not wait for the restart inside this turn. This preserves chats, queued work, and Hermes sessions. It does not restart the Mac. Never create launchctl reload jobs.',
    'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
}


class ServiceRestart:
    def poll_service_drain(self):
        """Pause new turns while the updater asks for it, and say when no turn is running.

        The updater writes the time at which the request ends into `drain-request`. Until then, no new turn starts and the
        running turns finish. When none is left, this writes `drained`, and the updater stops the service. A request that
        nobody removes ends by itself, so a failed update cannot leave the service paused.
        """
        request, drained = self.root / DRAIN_REQUEST_FILE, self.root / DRAINED_FILE
        try:
            until = float(request.read_text()) if request.is_file() else 0.0
        except (OSError, ValueError):
            until = 0.0
        if time.time() >= until:
            self._draining = False
            if getattr(self, '_drain_holds_dispatch', False):
                self._drain_holds_dispatch = False
                # A restart that a bot asked for, a new Hermes, or a workspace recovery keeps the pause that it also needs.
                if (self.restart_deadline is None and self.hermes_restart_deadline is None
                        and not getattr(self, '_workspace_recovery_hold', False)):
                    self.restart_pending = False
                with self.changed:
                    self.changed.notify_all()
            for path in (request, drained):
                path.unlink(missing_ok=True)
            return
        # While the drain holds, the deferral of a restart that a bot asked for must not release the pause (see
        # take_service_restart). Otherwise both would switch the pause off and on, and admit turns between.
        self._draining = True
        if not self.restart_pending:
            self._drain_holds_dispatch = True
            self.restart_pending = True
        with self.lock:
            active = self._service_busy()
        if not active and not drained.exists():
            drained.write_text('1')

    def _service_busy(self):
        # Caller holds self.lock. The same work that keeps a bot's restart from taking effect: turns, claims, uncertain
        # dispatch and worker cleanup.
        return (any(t.is_alive() for t in [*self.running.values(), *self.context_threads.values(), *self.gateway_cron_threads.values()])
                or bool(self._kanban_claiming) or bool(self._kanban_dispatch_uncertain) or bool(self._kanban_trimming))

    def poll_hermes_change(self):
        """After a change of Hermes, start no new turn until the restart. See hermes_changed and take_service_restart."""
        if self.hermes_restart_deadline is None:
            if not self.hermes_changed():
                return
            self.hermes_restart_deadline = time.monotonic() + HERMES_DRAIN_SECONDS
            print('Hermes changed. Coconut restarts when the running turns finish.', flush=True)
        # The end of another pause releases dispatch (see poll_service_drain). This pause holds until the restart.
        self.restart_pending = True

    def clear_service_drain(self):
        for name in (DRAIN_REQUEST_FILE, DRAINED_FILE):
            (self.root / name).unlink(missing_ok=True)

    def request_service_restart(self, task_id, payload):
        from .service import ServiceError
        if payload != {}:
            raise ServiceError(400, 'The restart tool takes no arguments.')
        with self.transaction():
            task = self._get('tasks', task_id)
            previous = task.get('_service_restart')
            if previous and previous['turn_id'] == task.get('_native_turn_id'):
                return self._service_restart_result() if previous['generation'] == self.generation else {'status': 'restarted'}
            if not self.restart_enabled:
                return {'status': 'unavailable', 'error': 'Install the Coconut login service before requesting a restart.'}
            if task['status'] != 'working' or self.stopping.is_set():
                raise ServiceError(409, 'This task is not running.')
            task['_service_restart'] = {'generation': self.generation, 'turn_id': task.get('_native_turn_id')}
            self._put('tasks', task)
            # Repeated requests must not extend the pause or pause dispatch again after deferral.
            if self.restart_deadline is None:
                self.restart_deadline = time.monotonic() + RESTART_DRAIN_SECONDS
                self.restart_pending = True
            return self._service_restart_result()

    def _service_restart_result(self):
        if self.restart_pending:
            return {'status': 'queued', 'detail': 'Finish your reply now. Dispatch pauses for at most 60 seconds while active turns finish.'}
        return {'status': 'deferred', 'detail': 'Work continues. The service will restart when active turns finish. Finish your reply now.'}

    def take_service_restart(self):
        # An update is about to stop the service. A restart now would start a process that removes the drain request,
        # and the updater would wait for an idle mark that never comes. The update supersedes the restart.
        if getattr(self, '_draining', False):
            return False
        # The HTTP accept loop must remain available while another request owns the queue.
        if not self.dispatch_gate.acquire(blocking=False):
            return False
        if not self.lock.acquire(blocking=False):
            self.dispatch_gate.release()
            return False
        try:
            recovery = self._pending_workspace_recovery()
            # A workspace recovery restarts the service too, so a new Hermes waits for it.
            hermes = self.hermes_restart_deadline is not None and not recovery
            if ((self.restart_deadline is None and not recovery and not hermes) or self.stopping.is_set()
                    or getattr(self, '_workspace_recovery_hold', False)):
                return False
            active_turns = any(t.is_alive() for t in [*self.running.values(), *self.context_threads.values(), *self.gateway_cron_threads.values()])
            if hermes:
                # As an update does: wait for the running work, but not longer than HERMES_DRAIN_SECONDS.
                if self._service_busy() and time.monotonic() < self.hermes_restart_deadline:
                    return False
            elif active_turns or self._kanban_trimming:
                if (not recovery and self.restart_pending and time.monotonic() >= self.restart_deadline
                        and not getattr(self, '_draining', False)):
                    with self.transaction():
                        self._event('service.restart_deferred', {'reason': 'active_turns' if active_turns else 'worker_cleanup', 'dispatch_resumed': True})
                    self.restart_pending = False
                    self.changed.notify_all()
                return False
            if recovery:
                try:
                    self.kanban.call({'op': 'snapshot'})
                    self._reconcile_kanban()
                    # No tick/reclaim here: read the native ledger before anything
                    # can discard evidence of surviving or ambiguous writers.
                    self._check_recovery_writers()
                except Exception as error:
                    self._refuse_workspace_recovery(recovery, error)
                    return False
            self.restart_pending = False
            self.restart_deadline = None
            self.stopping.set()
            return True
        finally:
            self.lock.release()
            self.dispatch_gate.release()
