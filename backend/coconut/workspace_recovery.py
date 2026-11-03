"""Device-authorized, pre-worker repair of one retained workspace binding.

The request is a durable compare-and-swap, not a worker tool. No Git operation
here changes a branch, checkout, file, or approval. Startup owns service.lock.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from uuid import UUID

from .work_capacity import execution_key
from .work_workspace import git, validate_binding


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def alive(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError('A writer record has no valid host PID.')
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        raise ValueError('A recorded writer cannot be inspected.') from None
    return True


def lifecycle_homes(root):
    # Inspect only native lifecycle locations, not symlinked skills or user
    # repositories inside a bot's workspace.
    def children(directory):
        if directory.is_symlink():
            raise ValueError('A lifecycle directory is unsafe.')
        if not directory.exists():
            return []
        result = list(directory.iterdir())
        if any(p.is_symlink() or not p.is_dir() for p in result):
            raise ValueError('A lifecycle directory is ambiguous.')
        return result
    for owner in children(root / 'runtimes'):
        yield from children(owner)
    # Each bot keeps its state.db and processes.json in its Hermes profile.
    from .profile_registry import homes
    for home in map(Path, homes(root)):
        if home.is_symlink():
            raise ValueError('A native profile lifecycle directory is unsafe.')
        if home.is_dir():
            yield home


def check_writers(root):
    """Read before native orphan recovery, which may otherwise erase evidence."""
    database = root / 'kanban' / 'kanban.db'
    if database.is_symlink() or not database.is_file():
        raise ValueError('The native writer ledger is missing or unsafe.')
    with contextlib.closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        tasks = db.execute('SELECT id,status,worker_pid,current_run_id FROM tasks '
                           "WHERE status='running' OR worker_pid IS NOT NULL OR current_run_id IS NOT NULL").fetchall()
        runs = db.execute('SELECT id,task_id,worker_pid FROM task_runs '
                          "WHERE status='running' OR ended_at IS NULL").fetchall()
        by_run = {r[0]: r for r in runs}
        for identity, status, pid, run in tasks:
            if run not in by_run or by_run[run][1:] != (identity, pid):
                raise ValueError('An ambiguous native claim requires inspection.')
            if alive(pid):
                raise ValueError('A native writer is still active or its PID was reused.')
        if any(r not in {(t[3], t[0], t[2]) for t in tasks} for r in runs):
            raise ValueError('An orphan native run requires inspection.')
    # Never recover/kill native checkpoints to establish quiescence.
    for home in lifecycle_homes(root):
        ledger = home / 'state.db'
        if ledger.is_symlink():
            raise ValueError('A delegation ledger is unsafe.')
        if ledger.exists():
            with contextlib.closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='async_delegations'").fetchone():
                    if db.execute("SELECT 1 FROM async_delegations WHERE state IN ('running','stalling','finalizing') LIMIT 1").fetchone():
                        raise ValueError('A native delegation is active or its outcome is ambiguous.')
        checkpoint = home / 'processes.json'
        if checkpoint.is_symlink():
            raise ValueError('A process checkpoint is unsafe.')
        if not checkpoint.exists():
            continue
        entries = json.loads(checkpoint.read_text())
        if not isinstance(entries, list):
            raise ValueError('A process checkpoint is ambiguous.')
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('pid_scope', 'host') != 'host':
                raise ValueError('A background writer has ambiguous scope.')
            if alive(entry.get('pid')):
                raise ValueError('A background writer is active or its PID was reused.')
            # Hermes starts host commands in a new session. A dead wrapper
            # (even with a saved host_start_time) does not prove its children
            # exited: its process group can outlive it. Probe without signaling.
            try:
                os.killpg(entry['pid'], 0)
            except ProcessLookupError:
                pass
            except (OSError, AttributeError):
                raise ValueError('A background process group cannot be inspected.') from None
            else:
                raise ValueError('A background process group is still active or was reused.')
            # A systemd scope can contain descendants outside that group. Do
            # not invoke native checkpoint recovery (which stops the scope),
            # or treat an absent wrapper/group as evidence that it is empty.
            if entry.get('systemd_unit', '') != '':
                raise ValueError('A background systemd scope requires quiescence inspection.')


@contextlib.contextmanager
def workspace_lock(root):
    directory = root / 'workspaces'
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('The workspace registry directory is unsafe.')
    fd = os.open(directory / '.workspace.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A workspace writer is active.') from None
        yield


def atomic_manifest(path, data):
    fd, name = tempfile.mkstemp(prefix='.workspace-recovery-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class WorkspaceRecovery:
    @staticmethod
    def _workspace_recovery_receipt(receipt):
        return {k: receipt[k] for k in ('id', 'state', 'request', 'created_at', 'error', 'resume_path') if k in receipt}

    def _recovery_task_scope(self, request):
        from .service import ServiceError
        task = self._get('tasks', request['task_id'])
        if (execution_key(task) != request['expected_owner']
                or task.get('_native_outcome_id') != request['native_task_id']):
            raise ServiceError(409, 'The intended task no longer owns this execution and native outcome.')
        database = self.root / 'kanban' / 'kanban.db'
        if database.is_symlink():
            raise ServiceError(409, 'The native ownership ledger is unsafe.')
        with contextlib.closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            binding = db.execute('SELECT b.native_id,t.tenant FROM coconut_bindings b JOIN tasks t ON t.id=b.native_id '
                                 'WHERE b.external_id=?', (task['id'],)).fetchone()
            if binding != (request['native_task_id'], task['bot_id']):
                raise ServiceError(409, 'The native task binding changed before authorization.')
        return {key: task.get(key) for key in ('id', 'bot_id', 'session_id', '_conversation_epoch',
            '_execution_session_id', '_native_outcome_id', '_native_turn_id', '_work_context_id')}

    def request_workspace_recovery(self, data, key, device):
        from .service import ServiceError, now
        fields = {'task_id', 'native_task_id', 'expected_owner', 'repository', 'path', 'old_branch', 'new_branch'}
        if (not isinstance(data, dict) or set(data) != fields
                or any(not isinstance(v, str) or not v or len(v) > 4096 for v in data.values())):
            raise ServiceError(400, 'Supply exactly the task, native task, owner, repository, path, and old/new branches.')
        try:
            UUID(key)
        except (ValueError, TypeError, AttributeError):
            raise ServiceError(400, 'Supply a UUID in Idempotency-Key.') from None
        with self.dispatch_gate, self.lock:
            if not self.db.execute('SELECT 1 FROM devices WHERE id=?', (device,)).fetchone():
                raise ServiceError(403, 'Workspace recovery requires an authenticated paired device.')
            previous = self.db.execute('SELECT data FROM workspace_recoveries WHERE id=?', (key,)).fetchone()
            if previous:
                receipt = json.loads(previous[0])
                if receipt['request'] != data or receipt['device_id'] != device:
                    raise ServiceError(409, 'This recovery key belongs to another authorization.')
                return self._workspace_recovery_receipt(receipt)
            if not self.restart_enabled or not self.kanban or self.stopping.is_set():
                raise ServiceError(409, 'Recovery requires the managed native service.')
            if self.restart_pending and not getattr(self, '_workspace_recovery_hold', False):
                raise ServiceError(409, 'A service drain is already pending.')
            try:
                scope = self._recovery_task_scope(data)
                for field in ('repository', 'path'):
                    path = Path(data[field])
                    if not path.is_absolute() or str(path.resolve()) != str(path):
                        raise ValueError('Use exact canonical absolute paths.')
                if data['old_branch'] == data['new_branch']:
                    raise ValueError('The requested branch transition is empty.')
                git(data['repository'], 'check-ref-format', '--branch', data['new_branch'])
                with workspace_lock(self.root):
                    manifest = self.root / 'workspaces' / 'worktrees.json'
                    if manifest.is_symlink():
                        raise ValueError('The workspace manifest is unsafe.')
                    before = manifest.read_text()
                    bindings = json.loads(before)
                    if not isinstance(bindings, dict) or any(not isinstance(v, dict)
                            or any(not isinstance(v.get(k), str) or not v[k] for k in ('outcome', 'repository', 'path', 'branch'))
                            for v in bindings.values()):
                        raise ValueError('The workspace registry is ambiguous.')
                    identity = data['expected_owner'] + '-' + hashlib.sha256(data['repository'].encode()).hexdigest()[:16]
                    expected = dict(outcome=data['expected_owner'], repository=data['repository'],
                                    path=data['path'], branch=data['old_branch'])
                    binding = bindings.get(identity, {})
                    if any(binding.get(k) != v for k, v in expected.items()):
                        raise ValueError('The saved owner, repository, path, or old branch changed.')
                    if any(k != identity and Path(v['path']).resolve() == Path(data['path']) for k, v in bindings.items()):
                        raise ValueError('Another binding also names this worktree.')
                    replacement = dict(binding, branch=data['new_branch'], _recovery_request_id=key)
                    validate_binding(replacement)
                    records = git(data['repository'], 'worktree', 'list', '--porcelain').split('\n\n')
                    if not any(('worktree ' + data['path']) in record.splitlines()
                               and ('branch refs/heads/' + data['new_branch']) in record.splitlines() for record in records):
                        raise ValueError('Git does not register this exact worktree and branch.')
                    bindings[identity] = replacement
                    receipt = dict(id=key, device_id=device, request=data, task_scope=scope,
                        before=before, after=encoded(bindings), state='draining', created_at=now())
                with self.transaction():
                    for old in self._all('workspace_recoveries'):
                        if old['state'] == 'held':
                            old['state'] = 'superseded'
                            self._put('workspace_recoveries', old)
                    self._put('workspace_recoveries', receipt)
                self._workspace_recovery_hold = False
                self.restart_pending = True
                return self._workspace_recovery_receipt(receipt)
            except (ValueError, OSError, sqlite3.Error) as error:
                raise ServiceError(409, str(error)) from error

    def _pending_workspace_recovery(self):
        return next((r for r in self._all('workspace_recoveries') if r['state'] in ('draining', 'held', 'admitting')), None)

    def _check_recovery_writers(self):
        if any(self._background_pending(task['id']) for task in self._all('tasks')):
            raise ValueError('A background completion is pending; inspect its writer before recovery.')
        check_writers(self.root)

    def _refuse_workspace_recovery(self, receipt, error):
        with self.transaction():
            receipt.update(state='held', error=str(error))
            self._put('workspace_recoveries', receipt)
        self._workspace_recovery_hold = True
        self.restart_pending = True

    def _recover_workspace_binding(self):
        """Called with the lifetime lock, before any native recovery or dispatch."""
        from .service import ServiceError
        # Coconut no longer recovers stopped checkouts. An open checkout receipt of an earlier version ends unchanged.
        for old in self._all('workspace_recoveries'):
            if 'merge_commit' in old and old['state'] in ('draining', 'held', 'admitting'):
                with self.transaction():
                    old.update(state='superseded', error='Checkout recovery was removed.')
                    self._put('workspace_recoveries', old)
        receipt = self._pending_workspace_recovery()
        if not receipt:
            self._workspace_recovery_hold = any(r['state'] == 'held' for r in self._all('workspace_recoveries'))
            self.restart_pending = self._workspace_recovery_hold
            return
        try:
            self._check_recovery_writers()
            if self._recovery_task_scope(receipt['request']) != receipt['task_scope']:
                raise ValueError('The authorized task input changed before repair.')
            with workspace_lock(self.root):
                manifest = self.root / 'workspaces' / 'worktrees.json'
                if manifest.is_symlink():
                    raise ValueError('The workspace manifest is unsafe.')
                data = receipt['request']
                # Compare only the authorized binding. Worktree retirement can change other bindings at any time.
                identity = data['expected_owner'] + '-' + hashlib.sha256(data['repository'].encode()).hexdigest()[:16]
                current = json.loads(manifest.read_text())
                old, new = (json.loads(receipt[k]).get(identity) for k in ('before', 'after'))
                if not isinstance(current, dict) or current.get(identity) not in (old, new):
                    raise ValueError('The workspace binding changed after authorization.')
                # Other bindings can change after authorization. None of them may name this worktree.
                if any(k != identity and (not isinstance(v, dict) or not isinstance(v.get('path'), str)
                                          or Path(v['path']).resolve() == Path(data['path'])) for k, v in current.items()):
                    raise ValueError('Another binding also names this worktree.')
                validate_binding(dict(path=data['path'], repository=data['repository'], branch=data['new_branch']))
                if current[identity] == old:
                    atomic_manifest(manifest, encoded(dict(current, **{identity: new})))
                else:
                    # The previous process may have stopped before fsync of the
                    # rename. Establish durability before committing its receipt.
                    for path in (manifest, manifest.parent):
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                with self.transaction():
                    receipt.pop('error', None)
                    receipt.update(state='applied', resume_path='/v1/tasks/' + data['task_id'] + '/replies')
                    self._put('workspace_recoveries', receipt)
                self._workspace_recovery_hold = False
                self.restart_pending = False
        except (ValueError, OSError, sqlite3.Error, ServiceError) as error:
            with self.transaction():
                receipt.update(state='held', error=str(error))
                self._put('workspace_recoveries', receipt)
            self._workspace_recovery_hold = True
            self.restart_pending = True
