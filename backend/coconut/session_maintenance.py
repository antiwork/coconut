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
# Other ended Coconut sessions, such as the compressed parts of a chat, stay searchable this long.
SESSION_HISTORY_DAYS = 30
SESSION_MAINTENANCE_SECONDS = 60 * 60
# Before it reads task state, maintenance waits this long for the service lock.
SESSION_LOCK_WAIT_SECONDS = 5
# After a bot's first full pass, Coconut records the newest session of conversations updated this recently.
TIP_WINDOW_SECONDS = 30 * 86400
TIPS_SETTING = 'sessions.tips-recorded.'
# A VACUUM rewrites the store of one bot and takes minutes. The service stops new turns of that bot and waits this long for
# its running turns to end. If one is still running, the bot goes on and maintenance tries again after the retry time.
VACUUM_DRAIN_SECONDS = 120
VACUUM_RETRY_SECONDS = 15 * 60
# After a pass that ends without a VACUUM, the bot goes on hold again only after this time.
VACUUM_DEFERRED_SECONDS = 6 * 60 * 60
# A bot that has a running turn is not held at first. Maintenance tries again for a quiet moment, and only after this
# many busy tries does the bot go on hold, so a bot with long turns loses two minutes once in many hours.
VACUUM_IDLE_TRIES = 3


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
                arguments = (dict(id=bot['id'], session_id=bot['session_id']),
                             list(conversations.get(bot['id'], {}).values()), pending, SESSION_RETENTION_DAYS)
                results[bot['id']] = self.provider.maintain_sessions(*arguments, allow_vacuum=False)
                if results[bot['id']].get('vacuum_pending'):
                    results[bot['id']] = self._vacuum_native_store(bot['id'], arguments, results[bot['id']], current)
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

    def _vacuum_native_store(self, bot_id, arguments, first, current):
        """Run the VACUUM that the first pass found due, while the bot starts no new turn.

        A busy bot never has a quiet moment, and its store can grow for weeks. So the bot goes on hold: it keeps its
        queue, and its running turns finish, but no new one starts. Then the store is rewritten and the hold ends.
        Only a bot with a due VACUUM goes on hold, and at most VACUUM_DRAIN_SECONDS plus the rewrite. A bot with a running
        turn first gets VACUUM_IDLE_TRIES tries without a hold. A pass that does not end in a VACUUM, for example because
        a turn outlasts the drain or another process holds the store, waits VACUUM_DEFERRED_SECONDS before the bot goes
        on hold again.
        """
        retries = self.__dict__.setdefault('_vacuum_retry_at', {})
        busy_tries = self.__dict__.setdefault('_vacuum_busy_tries', {})
        if current < retries.get(bot_id, 0):
            return first
        if busy_tries.get(bot_id, 0) < VACUUM_IDLE_TRIES and self._bot_has_live_turn(bot_id):
            # No hold yet. A quiet moment can come, and the hold costs the bot its new turns.
            busy_tries[bot_id] = busy_tries.get(bot_id, 0) + 1
            retries[bot_id] = self._next_session_maintenance = current + VACUUM_RETRY_SECONDS
            return first
        busy_tries[bot_id] = 0
        retries[bot_id] = current + VACUUM_DEFERRED_SECONDS
        try:
            with self.lock:
                self._store_hold.add(bot_id)
            if not self._wait_for_turns(bot_id):
                return first
            second = self.provider.maintain_sessions(*arguments, allow_vacuum=True)
        finally:
            with self.changed:
                self._store_hold.discard(bot_id)
                self.changed.notify_all()
        # The second pass prunes nothing more. Keep the counts of the first and add what the VACUUM did.
        return dict(first, vacuum_pending=False, **{key: second[key] for key in ('vacuumed', 'vacuum_error', 'vacuum_skipped') if key in second})

    def _wait_for_turns(self, bot_id):
        """Wait for the running turns of a bot on hold. Return False if one does not end in time."""
        deadline = time.monotonic() + VACUUM_DRAIN_SECONDS
        while self._bot_has_live_turn(bot_id):
            if time.monotonic() >= deadline or self.stopping.wait(1):
                return False
        return True

    def _bot_has_live_turn(self, bot_id):
        if not self.lock.acquire(timeout=SESSION_LOCK_WAIT_SECONDS):
            return True
        try:
            if bot_id in self._kanban_claim_bots:
                return True
            live = [self._get('tasks', i)['bot_id'] for i, worker in self.running.items() if worker.is_alive()]
            live += [self._get('contexts', i)['bot_id'] for i, worker in self.context_threads.items() if worker.is_alive()]
            return bot_id in live
        finally:
            self.lock.release()


def recent(task, current):
    try:
        updated = datetime.fromisoformat((task.get('updated_at') or task['created_at']).replace('Z', '+00:00'))
    except (KeyError, TypeError, ValueError, AttributeError):
        return True
    return current - updated.timestamp() < TIP_WINDOW_SECONDS
