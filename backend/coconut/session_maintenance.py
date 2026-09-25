"""Let Hermes prune the native sessions of idle outcomes. Each bot keeps its one open chat session."""
from datetime import datetime
import logging
import time

from .work_capacity import execution_key

# Hermes deletes an ended session after it has been inactive this long. Coconut's own messages and the native
# card keep the record, and a resumed outcome gets its visible conversation again (see NativeConversation).
SESSION_RETENTION_DAYS = 7
SESSION_MAINTENANCE_SECONDS = 60 * 60
# Before it reads task state, maintenance waits this long for the service lock.
SESSION_LOCK_WAIT_SECONDS = 5
# Coconut records the newest session of conversations updated this recently. Older ones were recorded earlier.
TIP_WINDOW_SECONDS = 30 * 86400


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
        outcomes, conversations = {}, {}
        for bot in bots:
            # The chat root. Coconut records the newest session of every conversation before any prune.
            conversations.setdefault(bot['id'], {})[(None, None)] = dict(execution_session_id=None, epoch=None)
        for task in tasks:
            key = (task.get('_execution_session_id'), task.get('_conversation_epoch'))
            if key != (None, None) and recent(task, current):
                conversations.setdefault(task['bot_id'], {})[key] = dict(execution_session_id=key[0], epoch=key[1])
            # Only work has its own session. A chat task uses the bot's conversation, which stays open.
            # A status-only task has its own execution key but shares its owner's session, so it never decides.
            if (task.get('_execution_session_id') and not task.get('_work_status_only')
                    and execution_key(task) in idle):
                item = dict(execution_session_id=task['_execution_session_id'], epoch=task.get('_conversation_epoch'))
                outcomes.setdefault(task['bot_id'], {})[(item['execution_session_id'], item['epoch'])] = item
        results = {}
        for bot in bots:
            if self.stopping.is_set() or self.restart_pending:
                break
            if not (self.root / 'bots' / bot['id'] / 'home' / 'state.db').exists():
                continue
            pending = list(outcomes.get(bot['id'], {}).values())
            if pending:
                # Earlier bots can take minutes. Check again that each outcome is still idle just before this bot.
                still = self._finished_outcomes(time.time(), timeout=SESSION_LOCK_WAIT_SECONDS)
                pending = [item for item in pending if still is not None and item['execution_session_id'] in still]
            try:
                results[bot['id']] = self.provider.maintain_sessions(
                    dict(id=bot['id'], session_id=bot['session_id']),
                    list(conversations.get(bot['id'], {}).values()), pending, SESSION_RETENTION_DAYS,
                    allow_vacuum=self._bot_quiet(bot['id']))
            except Exception:
                logging.getLogger(__name__).exception('Could not maintain the native sessions of bot %s.', bot['id'])
        return results

    def _bot_quiet(self, bot_id):
        """Return True when the bot has no running worker and no active or queued task.

        A VACUUM rewrites the whole store, and a turn that opens it meanwhile waits and can fail.
        """
        if not self.lock.acquire(timeout=SESSION_LOCK_WAIT_SECONDS):
            return False
        try:
            return not any(t['bot_id'] == bot_id and (t['status'] in self.ACTIVE
                           or (self.running.get(t['id']) and self.running[t['id']].is_alive()))
                           for t in self._all('tasks'))
        finally:
            self.lock.release()


def recent(task, current):
    try:
        updated = datetime.fromisoformat((task.get('updated_at') or task['created_at']).replace('Z', '+00:00'))
    except (KeyError, TypeError, ValueError, AttributeError):
        return True
    return current - updated.timestamp() < TIP_WINDOW_SECONDS
