"""Bound the queue pause before one launchd-managed service restart."""
import time

RESTART_DRAIN_SECONDS = 60

RESTART_SCHEMA = {
    'name': 'coconut_restart_service',
    'description': 'Request one Coconut service restart after active turns finish. Dispatch pauses for at most 60 seconds, then resumes while the restart waits for idle workers. Finish your reply after requesting it; do not wait for the restart inside this turn. This preserves chats, queued work, and Hermes sessions. It does not restart the Mac. Never create launchctl reload jobs.',
    'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
}


class ServiceRestart:
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
        # The HTTP accept loop must remain available while another request owns the queue.
        if not self.dispatch_gate.acquire(blocking=False):
            return False
        if not self.lock.acquire(blocking=False):
            self.dispatch_gate.release()
            return False
        try:
            recovery = self._pending_workspace_recovery()
            if ((self.restart_deadline is None and not recovery) or self.stopping.is_set()
                    or getattr(self, '_workspace_recovery_hold', False)):
                return False
            active_turns = any(t.is_alive() for t in [*self.running.values(), *self.context_threads.values()])
            if active_turns or self._kanban_trimming:
                if not recovery and self.restart_pending and time.monotonic() >= self.restart_deadline:
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
