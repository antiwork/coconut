"""Drain active turns before one launchd-managed service restart."""
RESTART_SCHEMA = {
    'name': 'coconut_restart_service',
    'description': 'Request one Coconut service restart after active turns finish. Use this to reload host configuration. Finish your reply after requesting it. This preserves chats, queued work, and Hermes sessions. It does not restart the Mac. Never create launchctl reload jobs.',
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
                return {'status': 'queued' if previous['generation'] == self.generation else 'restarted'}
            if not self.restart_enabled:
                return {'status': 'unavailable', 'error': 'Install the Coconut login service before requesting a restart.'}
            if task['status'] != 'working' or self.stopping.is_set():
                raise ServiceError(409, 'This task is not running.')
            task['_service_restart'] = {'generation': self.generation, 'turn_id': task.get('_native_turn_id')}
            self._put('tasks', task)
            self.restart_pending = True
            return {'status': 'queued', 'detail': 'The service restarts once after active turns finish. Finish your reply now.'}

    def take_service_restart(self):
        # The HTTP accept loop must remain available while another request owns the queue.
        if not self.dispatch_gate.acquire(blocking=False):
            return False
        if not self.lock.acquire(blocking=False):
            self.dispatch_gate.release()
            return False
        try:
            if not self.restart_pending or self.stopping.is_set() or getattr(self, '_workspace_recovery_hold', False):
                return False
            if any(t.is_alive() for t in [*self.running.values(), *self.context_threads.values()]):
                return False
            recovery = self._pending_workspace_recovery()
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
            self.stopping.set()
            return True
        finally:
            self.lock.release()
            self.dispatch_gate.release()
