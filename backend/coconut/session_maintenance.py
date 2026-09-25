"""Let Hermes prune the native sessions of idle outcomes. Each bot keeps its one open chat session."""
import logging
import time

from .work_capacity import execution_key

# Hermes deletes an ended session after it has been inactive this long. Coconut's own messages and the native
# card keep the record, and a resumed outcome gets its visible conversation again (see NativeConversation).
SESSION_RETENTION_DAYS = 7
SESSION_MAINTENANCE_SECONDS = 60 * 60
# Before it reads task state, maintenance waits this long for the service lock.
SESSION_LOCK_WAIT_SECONDS = 5


class SessionMaintenance:
    def maintain_native_sessions(self, current=None):
        """Run at most once an hour. End the sessions of idle outcomes, then run Hermes maintenance per bot.

        An outcome is idle under the same rules as worktree retirement. Hermes throttles its own prune and VACUUM.
        """
        if not hasattr(self.provider, 'maintain_sessions'):
            return {}
        current = time.time() if current is None else current
        if current < getattr(self, '_next_session_maintenance', 0):
            return {}
        self._next_session_maintenance = current + SESSION_MAINTENANCE_SECONDS
        idle = self._finished_outcomes(current, timeout=SESSION_LOCK_WAIT_SECONDS)
        if idle is None:
            return {}
        if not self.lock.acquire(timeout=SESSION_LOCK_WAIT_SECONDS):
            return {}
        try:
            tasks = self._all('tasks')
            bots = [b for b in self._all('bots') if not b.get('archived')]
        finally:
            self.lock.release()
        outcomes = {}
        for task in tasks:
            # Only work has its own session. A chat task uses the bot's conversation, which stays open.
            if task.get('_execution_session_id') and execution_key(task) in idle:
                item = dict(execution_session_id=task['_execution_session_id'], epoch=task.get('_conversation_epoch'))
                outcomes.setdefault(task['bot_id'], {})[(item['execution_session_id'], item['epoch'])] = item
        results = {}
        for bot in bots:
            if self.stopping.is_set():
                break
            if not (self.root / 'bots' / bot['id'] / 'home' / 'state.db').exists():
                continue
            try:
                results[bot['id']] = self.provider.maintain_sessions(
                    dict(id=bot['id'], session_id=bot['session_id']),
                    list(outcomes.get(bot['id'], {}).values()), SESSION_RETENTION_DAYS)
            except Exception:
                logging.getLogger(__name__).exception('Could not maintain the native sessions of bot %s.', bot['id'])
        return results
