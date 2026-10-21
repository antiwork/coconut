"""Keep the parsed task table between service operations. Read again only the rows that changed.

The task table holds every task the service ever ran (17,000 rows and 83 MB on the Mac mini). A full read and parse
took about 0.37 s, and the card checks did it on each pass while they held the service lock. SQLite triggers write
the ID of each changed row to `task_changes` with a rising number, so a later read applies only those rows.
The triggers commit and roll back with the write, and a rollback also drops this cache. They use an upsert, because
the conflict rule of the outer statement overrides INSERT OR REPLACE inside a trigger.

Set COCONUT_VERIFY_TASK_CACHE=1 to compare the kept tasks with the table on every read. CI runs the backend tests so.
"""
import json
import os

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


class TaskCache:
    def _cached_tasks(self):
        """Return {id: task} in row order, as `SELECT ... FROM tasks ORDER BY rowid` gives it.

        The task objects stay in memory for later operations, so only shared readers may use them (see _shared_reads).
        Each caller gets its own dict.
        """
        with self.lock:
            state = getattr(self, '_task_rows', None)
            if state is not None:
                state = self._apply_task_changes(*state)
            if state is None:
                state = self._read_all_tasks()
            self._task_rows = state
            if os.environ.get('COCONUT_VERIFY_TASK_CACHE') == '1':
                fresh = [(row[0], json.loads(row[1])) for row in self.db.execute('SELECT id, data FROM tasks ORDER BY rowid')]
                if fresh != list(state[1].items()):
                    raise AssertionError('The kept tasks differ from the task table. A reader changed a shared task.')
            return dict(state[1])

    def _apply_task_changes(self, seq, tasks, rowids, last):
        """Apply the rows changed since `seq`. Return None when only a full read keeps the row order."""
        latest = self.db.execute('SELECT coalesce(max(seq), 0) FROM task_changes').fetchone()[0]
        if latest < seq:
            # The numbers went back, so a rollback removed changes that this cache read.
            return None
        changed = self.db.execute('SELECT c.id, t.rowid, t.data FROM task_changes c LEFT JOIN tasks t ON t.id = c.id '
                                  'WHERE c.seq > ?', (seq,)).fetchall()
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
        seq = self.db.execute('SELECT coalesce(max(seq), 0) FROM task_changes').fetchone()[0]
        rows = self.db.execute('SELECT rowid, id, data FROM tasks ORDER BY rowid').fetchall()
        return (seq, {identity: json.loads(data) for _, identity, data in rows},
                {identity: rowid for rowid, identity, _ in rows}, rows[-1][0] if rows else 0)

    def _forget_cached_tasks(self):
        self._task_rows = None
