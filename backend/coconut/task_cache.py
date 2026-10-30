"""Keep the parsed task table between service operations. Read again only the rows that changed.

The task table holds every task the service ever ran (17,000 rows and 83 MB on the Mac mini). A full read and parse
took about 0.37 s, and the card checks did it on each pass while they held the service lock. SQLite triggers write
the ID of each changed row to `task_changes` with a rising number, so a later read applies only those rows.
The triggers commit and roll back with the write, and a rollback also drops this cache. They use an upsert, because
the conflict rule of the outer statement overrides INSERT OR REPLACE inside a trigger.

Read views and the writer share the committed task states (TaskStates). A state is the parsed table at one change
number. Nobody changes a state: a newer state copies the two maps and replaces the changed rows, so the states share
every unchanged task object. Before, the writer, each card pass and each read view kept or parsed a copy of their own,
about 270 MB each on a copy of the Mac mini data.

Set COCONUT_VERIFY_TASK_CACHE=1 to compare the kept tasks with the table on every read. CI runs the backend tests so.
"""
import json
import os
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_changes (id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS task_changes_seq ON task_changes (seq);
CREATE TRIGGER IF NOT EXISTS task_changes_insert AFTER INSERT ON tasks BEGIN
    INSERT INTO task_changes VALUES (NEW.id, (SELECT coalesce(max(seq), 0) + 1 FROM task_changes))
        ON CONFLICT(id) DO UPDATE SET seq = excluded.seq;
END;
CREATE TRIGGER IF NOT EXISTS task_changes_update AFTER UPDATE ON tasks BEGIN
    INSERT INTO task_changes VALUES (NEW.id, (SELECT coalesce(max(seq), 0) + 1 FROM task_changes))
        ON CONFLICT(id) DO UPDATE SET seq = excluded.seq;
END;
CREATE TRIGGER IF NOT EXISTS task_changes_delete AFTER DELETE ON tasks BEGIN
    INSERT INTO task_changes VALUES (OLD.id, (SELECT coalesce(max(seq), 0) + 1 FROM task_changes))
        ON CONFLICT(id) DO UPDATE SET seq = excluded.seq;
END;
"""


class TaskStates:
    """The latest committed task states, shared by the writer and the read views of one service.

    A state is (change number, {id: task}, {id: rowid}, last rowid). Committed rows decide the state of a change
    number, so any reader at that number or later can start from it. Only a state of committed rows goes in: a read
    view sees only committed rows, and the writer adds its state only outside a transaction. A VACUUM can renumber
    rowids, so it clears the states and starts a new generation.
    """
    KEEP = 4

    def __init__(self):
        self.guard = threading.Lock()
        self.states = []
        self.generation = 0

    def base(self, seq, generation):
        """The newest state at or before the change number `seq`, or None."""
        with self.guard:
            if generation != self.generation:
                return None
            return max((state for state in self.states if state[0] <= seq), key=lambda state: state[0], default=None)

    def add(self, state, generation):
        with self.guard:
            if generation != self.generation or any(kept[0] == state[0] for kept in self.states):
                return
            self.states = sorted([*self.states, state], key=lambda kept: kept[0])[-self.KEEP:]

    def clear(self):
        with self.guard:
            self.states = []
            self.generation += 1


class TaskCache:
    def _cached_tasks(self):
        """Return {id: task} in row order, as `SELECT ... FROM tasks ORDER BY rowid` gives it.

        The task objects stay in memory for later operations and other threads, so only shared readers may use them
        (see _shared_reads). Each caller gets its own dict.
        """
        with self.lock:
            shared = getattr(self, '_task_states', None)
            generation = getattr(self, '_task_generation', None)
            if shared is not None and generation is None:
                generation = shared.generation
            state = getattr(self, '_task_rows', None)
            if state is not None:
                state = self._apply_task_changes(*state)
            if state is None and shared is not None:
                base = shared.base(self._latest_task_change(), generation)
                if base is not None:
                    state = self._apply_task_changes(*base)
            if state is None:
                state = self._read_all_tasks()
            self._task_rows = state
            # A read view sees only committed rows. The writer's rows are committed only outside a transaction.
            if shared is not None and (getattr(self, '_task_generation', None) is not None or not self.db.in_transaction):
                shared.add(state, generation)
            if os.environ.get('COCONUT_VERIFY_TASK_CACHE') == '1':
                fresh = [(row[0], json.loads(row[1])) for row in self.db.execute('SELECT id, data FROM tasks ORDER BY rowid')]
                if fresh != list(state[1].items()):
                    raise AssertionError('The kept tasks differ from the task table. A reader changed a shared task.')
            return dict(state[1])

    def _latest_task_change(self):
        return self.db.execute('SELECT coalesce(max(seq), 0) FROM task_changes').fetchone()[0]

    def _apply_task_changes(self, seq, tasks, rowids, last):
        """Return the state with the rows changed since `seq`, or None when only a full read keeps the row order.

        The given maps are never changed: other states and threads can hold them.
        """
        latest = self._latest_task_change()
        if latest < seq:
            # The numbers went back, so a rollback removed changes that this cache read.
            return None
        changed = self.db.execute('SELECT c.id, t.rowid, t.data FROM task_changes c LEFT JOIN tasks t ON t.id = c.id '
                                  'WHERE c.seq > ?', (seq,)).fetchall()
        if not changed:
            return latest, tasks, rowids, last
        tasks, rowids = dict(tasks), dict(rowids)
        added = []
        for identity, rowid, data in changed:
            if data is not None and rowids.get(identity) == rowid:
                tasks[identity] = json.loads(data)
                continue
            # A deleted row leaves. A new row, or a row that returns with a new rowid, goes last in row order.
            tasks.pop(identity, None)
            rowids.pop(identity, None)
            if data is not None:
                added.append((rowid, identity, data))
        added.sort()
        if added and added[0][0] <= last:
            # SQLite reused a lower rowid.
            return None
        for rowid, identity, data in added:
            tasks[identity], rowids[identity], last = json.loads(data), rowid, rowid
        return latest, tasks, rowids, last

    def _read_all_tasks(self):
        seq = self._latest_task_change()
        rows = self.db.execute('SELECT rowid, id, data FROM tasks ORDER BY rowid').fetchall()
        return (seq, {identity: json.loads(data) for _, identity, data in rows},
                {identity: rowid for rowid, identity, _ in rows}, rows[-1][0] if rows else 0)

    def _forget_cached_tasks(self, *, renumbered=False):
        """Drop the writer's tasks. After a VACUUM, which can renumber rowids, drop the shared states too."""
        self._task_rows = None
        if renumbered and getattr(self, '_task_states', None) is not None:
            self._task_states.clear()
