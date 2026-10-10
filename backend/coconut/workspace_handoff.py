"""Prove from native Hermes records that a claimed turn continues the work of a retired worktree.

The worker calls this from coconut_workspace. It reads the Kanban database read-only and changes nothing.
"""
import contextlib
from pathlib import Path
import sqlite3


def owner_keys(outcome):
    # Coconut creates the native task of request R with one of these idempotency keys (see kanban_runtime.sync).
    return ('coconut:' + outcome, 'coconut:' + outcome + ':', 'coconut-followup:' + outcome + ':')


def names_owner(key, outcome):
    exact, *prefixes = owner_keys(outcome)
    return isinstance(key, str) and (key == exact or any(key.startswith(p) and len(key) > len(p) for p in prefixes))


def native_successor(database, claim, task_id, native_id, tenant):
    """Return a check for acquire. It returns the native proof, or raises ValueError.

    The check proves that the claimed native task is a direct child of a native task that Coconut created for the
    retired binding's outcome, in the same tenant, at the same path, and that no native run of that outcome is active.
    """
    def check(binding):
        owner, path = binding['outcome'], binding['path']
        location = Path(database)
        if location.is_symlink() or not location.is_file():
            raise ValueError('The native task database is missing or unsafe.')
        with contextlib.closing(sqlite3.connect(location.as_uri() + '?mode=ro', uri=True)) as db:
            columns = 'id,status,tenant,idempotency_key,created_by,workspace_path,current_run_id,worker_pid,claim_lock'
            current = db.execute(f'SELECT {columns} FROM tasks WHERE id=?', (claim.get('native_id'),)).fetchone()
            external = db.execute('SELECT external_id FROM coconut_bindings WHERE native_id=?', (claim.get('native_id'),)).fetchone()
            # The claim lock and the run prove that this worker holds the current native claim.
            if (not current or current[0] != native_id or current[1] != 'running' or current[2] != tenant
                    or not claim.get('claim_lock') or current[8] != claim['claim_lock'] or current[6] != claim.get('run_id')
                    or not external or external[0] != task_id or claim.get('external_id') != task_id):
                raise ValueError('This turn does not hold the native claim that it names.')
            parents = db.execute(f'SELECT {", ".join("t." + c for c in columns.split(","))} FROM task_links l '
                                 'JOIN tasks t ON t.id=l.parent_id WHERE l.child_id=?', (native_id,)).fetchall()
            bound_owners = {row[0] for row in db.execute(
                'SELECT native_id FROM coconut_bindings WHERE external_id=?', (owner,)).fetchall()}
            matches = [p for p in parents if p[2] == tenant and p[4] == 'coconut'
                       and (names_owner(p[3], owner) or p[0] in bound_owners)]
            # Another native parent at this path, or two parents of the owner, leave the ancestry ambiguous.
            others = [p for p in parents if p not in matches and p[5] == path]
            if not matches:
                raise ValueError('No native parent of this task belongs to the outcome that owns the worktree.')
            if len(matches) > 1 or others:
                raise ValueError('The native ancestry of this worktree is ambiguous.')
            parent = matches[0]
            if parent[5] != path:
                raise ValueError('The native parent names another workspace path.')
            exact, prefix, followup = owner_keys(owner)
            active = db.execute(
                'SELECT 1 FROM tasks WHERE tenant=? AND (idempotency_key=? OR substr(idempotency_key,1,?)=? '
                'OR substr(idempotency_key,1,?)=? OR id=?) AND (status NOT IN (\'done\',\'archived\') '
                'OR current_run_id IS NOT NULL OR worker_pid IS NOT NULL '
                'OR EXISTS (SELECT 1 FROM task_runs r WHERE r.task_id=tasks.id AND r.ended_at IS NULL)) LIMIT 1',
                (tenant, exact, len(prefix), prefix, len(followup), followup, parent[0])).fetchone()
            if active or parent[1] not in ('done', 'archived'):
                raise ValueError('The outcome that owns the worktree still has native work in progress.')
        return dict(native_id=native_id, parent_native_id=parent[0])

    def prove(binding):
        # A locked, corrupt, or unexpected database proves nothing. The tool refuses as for any failed proof.
        try:
            return check(binding)
        except sqlite3.Error:
            raise ValueError('The native task database cannot be read, so Coconut cannot prove this handoff.') from None
    return prove
