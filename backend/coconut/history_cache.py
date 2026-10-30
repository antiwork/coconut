"""Keep the visible history of each bot between turns. Read again only the messages that changed.

Each turn reads the whole visible history of its bot. On the Mac mini copy, Desk had 26,000 visible messages (26 MB of
JSON), and the read took about 0.4 s on a laptop. SQLite triggers write the ID of each changed message to
`message_changes` with a rising number, as `task_changes` does for tasks (see task_cache.py). A later read applies only
those messages. The audience of a message changes only with a write of the message, so its ID is in the changes too.

The states hold only committed rows: a read view sees only committed rows, and the writer uses the states only outside
a transaction. Nobody changes a state: a newer state copies the lists and replaces the changed messages. A VACUUM can
renumber rowids, so it clears the states and starts a new generation.
"""
import bisect
import json
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_changes (id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS message_changes_seq ON message_changes (seq);
CREATE TRIGGER IF NOT EXISTS message_changes_insert AFTER INSERT ON messages BEGIN
    INSERT INTO message_changes VALUES (NEW.id, (SELECT coalesce(max(seq), 0) + 1 FROM message_changes))
        ON CONFLICT(id) DO UPDATE SET seq = excluded.seq;
END;
CREATE TRIGGER IF NOT EXISTS message_changes_update AFTER UPDATE ON messages BEGIN
    INSERT INTO message_changes VALUES (NEW.id, (SELECT coalesce(max(seq), 0) + 1 FROM message_changes))
        ON CONFLICT(id) DO UPDATE SET seq = excluded.seq;
END;
CREATE TRIGGER IF NOT EXISTS message_changes_delete AFTER DELETE ON messages BEGIN
    INSERT INTO message_changes VALUES (OLD.id, (SELECT coalesce(max(seq), 0) + 1 FROM message_changes))
        ON CONFLICT(id) DO UPDATE SET seq = excluded.seq;
END;
"""

# The visibility rule of _history: the bot is in the audience, and the message is neither hidden nor deleted.
VISIBLE = ("EXISTS (SELECT 1 FROM message_audience a WHERE a.bot_id=? AND a.message_id=m.id) "
           "AND COALESCE(json_extract(m.data, '$._hidden'),0)=0 AND COALESCE(json_extract(m.data, '$._deleted'),0)=0")


# More changed messages than this since the kept state: read the whole history again.
CATCH_UP_LIMIT = 2000


class HistoryStates:
    """The latest committed history state of each bot: (change number, [rowid], [public message])."""

    def __init__(self):
        self.guard = threading.Lock()
        self.states = {}
        self.generation = 0

    def base(self, bot_id, seq, generation):
        with self.guard:
            state = self.states.get(bot_id) if generation == self.generation else None
            return state if state is not None and state[0] <= seq else None

    def add(self, bot_id, state, generation):
        with self.guard:
            current = self.states.get(bot_id)
            if generation == self.generation and (current is None or current[0] < state[0]):
                self.states[bot_id] = state

    def clear(self):
        with self.guard:
            self.states = {}
            self.generation += 1


class HistoryCache:
    def _kept_history(self, bot_id):
        """The visible history of the bot as _history returns it, or None when the caller has open writes."""
        shared = getattr(self, '_history_states', None)
        view = getattr(self, '_history_generation', None) is not None
        if shared is None or (not view and self.db.in_transaction):
            return None
        generation = self._history_generation if view else shared.generation
        with self.lock:
            seq = self.db.execute('SELECT coalesce(max(seq), 0) FROM message_changes').fetchone()[0]
            base = shared.base(bot_id, seq, generation)
            state = self._apply_message_changes(bot_id, base, seq) if base is not None else self._read_history(bot_id, seq)
        shared.add(bot_id, state, generation)
        # Each caller gets its own list and message objects, as a read of the table gives them.
        return [dict(message) for message in state[2]]

    def _read_history(self, bot_id, seq):
        # The audience index finds the bot's messages, as _history reads them.
        rows = self.db.execute(
            "SELECT m.rowid, m.data FROM message_audience a JOIN messages m ON m.id=a.message_id WHERE a.bot_id=? "
            "AND COALESCE(json_extract(m.data, '$._hidden'),0)=0 AND COALESCE(json_extract(m.data, '$._deleted'),0)=0 "
            "ORDER BY m.rowid", (bot_id,)).fetchall()
        return seq, [row[0] for row in rows], [self._public(json.loads(row[1])) for row in rows]

    def _apply_message_changes(self, bot_id, base, seq):
        previous, rowids, messages = base
        if seq == previous:
            return base
        count = self.db.execute('SELECT count(*) FROM message_changes WHERE seq > ?', (previous,)).fetchone()[0]
        if count > CATCH_UP_LIMIT:
            # A long gap reads the whole history again. Each insert into a long list moves its tail.
            return self._read_history(bot_id, seq)
        changed = self.db.execute(
            'SELECT c.id, m.rowid, m.data, CASE WHEN m.id IS NOT NULL AND ' + VISIBLE + ' THEN 1 ELSE 0 END '
            'FROM message_changes c LEFT JOIN messages m ON m.id = c.id WHERE c.seq > ?', (bot_id, previous)).fetchall()
        rowids, messages = list(rowids), list(messages)
        positions = {message['id']: index for index, message in enumerate(messages)}
        removed = {identity for identity, *_ in changed if identity in positions}
        if removed:
            kept = [index for index, message in enumerate(messages) if message['id'] not in removed]
            rowids, messages = [rowids[index] for index in kept], [messages[index] for index in kept]
        for identity, rowid, data, visible in sorted(changed, key=lambda row: row[1] or 0):
            if visible:
                index = bisect.bisect_left(rowids, rowid)
                rowids.insert(index, rowid)
                messages.insert(index, self._public(json.loads(data)))
        return seq, rowids, messages

    def _forget_kept_history(self):
        if getattr(self, '_history_states', None) is not None:
            self._history_states.clear()
