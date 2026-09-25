"""Let Hermes prune the native sessions of idle outcomes. Each bot keeps its one open chat session."""
from datetime import datetime
import json
import logging
import time

from .work_capacity import execution_key
from .worktree_retirement import ACTIVE_STATUSES

# Hermes deletes an ended session after it has been inactive this long. Coconut's own messages and the native
# card keep the record, and a resumed outcome gets its visible conversation again (see NativeConversation).
SESSION_RETENTION_DAYS = 7
SESSION_MAINTENANCE_SECONDS = 60 * 60
# Before it reads task state, maintenance waits this long for the service lock.
SESSION_LOCK_WAIT_SECONDS = 5
# After a bot's first full pass, Coconut records the newest session of conversations updated this recently.
TIP_WINDOW_SECONDS = 30 * 86400
TIPS_SETTING = 'sessions.tips-recorded.'


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
        # The first pass for a bot records every conversation, so a prune cannot cut an old chain without a tip.
        complete = {bot['id'] for bot in bots if self._setting(TIPS_SETTING + bot['id'])}
        outcomes, conversations = {}, {}
        for bot in bots:
            # The chat root. Coconut records the newest session of every conversation before any prune.
            conversations.setdefault(bot['id'], {})[(None, None)] = dict(execution_session_id=None, epoch=None)
        for task in tasks:
            key = (task.get('_execution_session_id'), task.get('_conversation_epoch'))
            # An unfinished conversation can resume at any age, so its tip is always recorded.
            if key != (None, None) and (task['bot_id'] not in complete or recent(task, current)
                                        or task['status'] not in ('completed', 'cancelled', 'failed')):
                conversations.setdefault(task['bot_id'], {})[key] = dict(execution_session_id=key[0], epoch=key[1])
            # Only work has its own session. A chat task uses the bot's conversation, which stays open.
            # A status-only task has its own execution key but shares its owner's session, so it never decides.
            if (task.get('_execution_session_id') and not task.get('_work_status_only')
                    and execution_key(task) in idle):
                item = dict(execution_session_id=task['_execution_session_id'], epoch=task.get('_conversation_epoch'))
                outcomes.setdefault(task['bot_id'], {})[(item['execution_session_id'], item['epoch'])] = item
        # A status-only turn uses its owner's session under its own execution key. While it is live, keep the owner.
        busy = self._status_only_owners()
        for bot_id in outcomes:
            outcomes[bot_id] = {k: v for k, v in outcomes[bot_id].items() if v['execution_session_id'] not in busy}
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
                busy = self._status_only_owners()
                pending = [item for item in pending if still is not None and item['execution_session_id'] in still
                           and item['execution_session_id'] not in busy]
            try:
                results[bot['id']] = self.provider.maintain_sessions(
                    dict(id=bot['id'], session_id=bot['session_id']),
                    list(conversations.get(bot['id'], {}).values()), pending, SESSION_RETENTION_DAYS,
                    allow_vacuum=self._bot_quiet(bot['id']))
                if bot['id'] not in complete:
                    with self.transaction():
                        self.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                        (TIPS_SETTING + bot['id'], json.dumps(True)))
            except Exception:
                logging.getLogger(__name__).exception('Could not maintain the native sessions of bot %s.', bot['id'])
        return results

    def _status_only_owners(self):
        """Return the execution sessions that a queued or running status-only task uses."""
        with self.lock:
            return {t['_execution_session_id'] for t in self._all('tasks')
                    if t.get('_work_status_only') and t.get('_execution_session_id')
                    and (t['status'] in ACTIVE_STATUSES or (self.running.get(t['id']) and self.running[t['id']].is_alive()))}

    def _bot_quiet(self, bot_id):
        """Return True when the bot has no running worker and no active or queued task.

        A VACUUM rewrites the whole store, and a turn that opens it meanwhile waits and can fail. A task that waits
        for a person or a resume runs no turn, so it does not block the VACUUM, as it does not block retirement.
        """
        if not self.lock.acquire(timeout=SESSION_LOCK_WAIT_SECONDS):
            return False
        try:
            if any(t['bot_id'] == bot_id and (t['status'] in ACTIVE_STATUSES
                   or (self.running.get(t['id']) and self.running[t['id']].is_alive()))
                   for t in self._all('tasks')):
                return False
            # A decision context runs a turn while its task waits for a person.
            return not any(c.get('bot_id') == bot_id and (c['status'] in ('queued', 'working')
                           or (self.context_threads.get(c['id']) and self.context_threads[c['id']].is_alive()))
                           for c in self._all('contexts'))
        finally:
            self.lock.release()


def recent(task, current):
    try:
        updated = datetime.fromisoformat((task.get('updated_at') or task['created_at']).replace('Z', '+00:00'))
    except (KeyError, TypeError, ValueError, AttributeError):
        return True
    return current - updated.timestamp() < TIP_WINDOW_SECONDS
