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
import stat
import subprocess
import tempfile
from uuid import UUID, uuid4

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


def check_writers(root, allowed=()):
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
    listing = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True,
                             timeout=10, check=True).stdout
    script = str(Path(__file__).with_name('hermes_worker.py'))
    for line in listing.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or 'hermes_worker.py' not in fields[1]:
            continue
        pid = int(fields[0])
        if pid in allowed:
            continue
        # Also recognize workers from an earlier installed source location. Read
        # environment only into memory; never log it or read profile credentials.
        result = subprocess.run(['ps', 'eww', '-p', str(pid), '-o', 'command='],
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            if alive(pid):
                raise ValueError('An orphan worker cannot be inspected.')
            continue
        # A bot's HERMES_HOME is a Hermes profile that the person's own processes share. COCONUT_DATA_DIR marks a worker.
        if (f'COCONUT_WORKSPACE_ROOT={root}/workspaces ' in result.stdout + ' '
                or f'COCONUT_PROCESS_HOME={root}/runtimes/' in result.stdout
                or f'COCONUT_DATA_DIR={root} ' in result.stdout + ' '
                or f'HERMES_HOME={root}/bots/' in result.stdout):
            raise ValueError('An unaccounted Coconut worker requires inspection.')
        if script in fields[1] and not any(marker in result.stdout for marker in ('COCONUT_WORKSPACE_ROOT=', 'COCONUT_DATA_DIR=', 'HERMES_HOME=')):
            raise ValueError('An unscoped Coconut worker requires inspection.')


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


def checkout_git(path, *arguments, input=None, env=None):
    result = subprocess.run(['git', '-C', str(path), *arguments], input=input, capture_output=True,
                            text=True, timeout=60, env=env)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or 'Git refused checkout recovery.').strip())
    return result.stdout


def checkout_state(request, *, initial, read_only=False):
    path = Path(request['path'])
    repository = request['repository']
    def read(location, *arguments):
        if read_only:
            return checkout_git(location, *arguments, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0')).strip()
        return git(location, *arguments)
    for branch, head in ((request['saved_branch'], request['saved_head']),
                         (request['attached_branch'], request['attached_head'])):
        if read(repository, 'rev-parse', '--verify', 'refs/heads/' + branch) != head:
            raise ValueError('An original branch moved after authorization.')
    records = read(repository, 'worktree', 'list', '--porcelain').split('\n\n')
    branch = request['attached_branch'] if initial else request['recovery_branch']
    if not any(('worktree ' + str(path)) in record.splitlines()
               and ('branch refs/heads/' + branch) in record.splitlines() for record in records):
        raise ValueError('Git does not register the expected worktree and branch.')
    if read(path, 'rev-parse', 'HEAD') != (request['attached_head'] if initial else request['merge_commit']):
        raise ValueError('The checked-out commit changed after authorization.')


def dirty_state(path, *, read_only=False):
    def read(*arguments):
        environment = dict(os.environ, GIT_OPTIONAL_LOCKS='0') if read_only else None
        return checkout_git(path, *arguments, env=environment)
    if read('config', '--bool', 'core.filemode').strip() != 'true':
        raise ValueError('Checkout recovery requires Git file-mode tracking.')
    if read('diff', '--cached', '--name-only'):
        raise ValueError('Staged edits need separate inspection before checkout recovery.')
    if read('ls-files', '-u'):
        raise ValueError('An unmerged index needs separate inspection.')
    for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG',
                   'rebase-merge', 'rebase-apply', 'index.lock'):
        if Path(read('rev-parse', '--git-path', marker).strip()).exists():
            raise ValueError('A Git operation is still in progress.')
    for entry in filter(None, read('ls-files', '-v', '-z').split('\0')):
        if entry[0].islower() or entry[0] == 'S':
            raise ValueError('A tracked file has assume-unchanged or skip-worktree set.')
    names = read('ls-files', '-m', '-z').split('\0')
    dirty = {}
    for name in filter(None, names):
        target = path / name
        if not target.exists() and not target.is_symlink():
            dirty[name] = None  # An unstaged tracked deletion is a retained edit.
            continue
        mode = target.lstat().st_mode
        if not stat.S_ISREG(mode) or not target.resolve().is_relative_to(path.resolve()):
            raise ValueError('A changed tracked file is not a regular file inside the worktree.')
        dirty[name] = [hashlib.sha256(target.read_bytes()).hexdigest(), stat.S_IMODE(mode)]
    return dirty


def unchanged_held_checkout(receipt, manifest_bytes, *, read_only=False):
    request = receipt['request']
    if hashlib.sha256(manifest_bytes).hexdigest() != receipt['manifest_sha256']:
        raise ValueError('The original manifest changed after authorization.')
    checkout_state(request, initial=True, read_only=read_only)
    if dirty_state(Path(request['path']), read_only=read_only) != receipt['dirty']:
        raise ValueError('The original dirty edits changed after authorization.')
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS='0') if read_only else None
    if subprocess.run(['git', '-C', request['repository'], 'show-ref', '--verify', '--quiet',
                       'refs/heads/' + request['recovery_branch']], timeout=10, env=environment).returncode != 1:
        raise ValueError('The held checkout may have partially created its recovery branch.')


def reject_custom_git_commands(path, commits):
    result = subprocess.run(['git', '-C', str(path), 'config', '--get-regexp',
                             r'^merge\..*\.driver$'], capture_output=True, text=True, timeout=10)
    if result.returncode != 1:
        raise ValueError('Custom Git merge drivers require separate checkout inspection.')
    worktree_names = set(filter(None, checkout_git(path, 'ls-files', '-z').split('\0')))
    for commit in commits:
        names = checkout_git(path, 'ls-tree', '-r', '--name-only', '-z', commit)
        worktree_names.update(filter(None, names.split('\0')))
        if not names:
            continue
        attributes = checkout_git(path, 'check-attr', '--source', commit, '-z', '--stdin', 'filter',
                                  input=names).split('\0')
        if any(value not in ('unspecified', 'unset') for value in attributes[2::3]):
            raise ValueError('A Git checkout filter requires separate inspection.')
    # Untracked or edited attributes can apply to paths introduced by the target tree.
    if worktree_names:
        attributes = checkout_git(path, 'check-attr', '-z', '--stdin', 'filter',
                                  input='\0'.join(sorted(worktree_names)) + '\0').split('\0')
        if any(value not in ('unspecified', 'unset') for value in attributes[2::3]):
            raise ValueError('A Git checkout filter requires separate inspection.')


def checkout_obstructions(request, tree, dirty):
    path = Path(request['path'])
    reject_custom_git_commands(path, (request['attached_head'], tree))
    changed = set(filter(None, checkout_git(path, 'diff', '--no-renames', '--name-only', '-z',
                                            request['attached_head'], tree).split('\0')))
    if changed.intersection(dirty):
        raise ValueError('The merge changes a dirty tracked file; manual reconciliation is required.')
    for commit in (request['attached_head'], tree):
        if changed:
            attributes = checkout_git(path, 'check-attr', '--source', commit, '-z', '--stdin', 'filter',
                                      input='\0'.join(changed) + '\0').split('\0')
            if any(value not in ('unspecified', 'unset') for value in attributes[2::3]):
                raise ValueError('A checkout filter needs separate inspection before recovery.')
    trees = {}
    for commit in (request['attached_head'], tree):
        trees[commit] = {name: details.split()[0] for entry in
                         filter(None, checkout_git(path, 'ls-tree', '-r', '-z', commit).split('\0'))
                         for details, name in (entry.split('\t', 1),)}
    for name in changed:
        if any(trees[commit].get(name) == '160000' for commit in trees):
            raise ValueError('A changed submodule needs separate checkout inspection.')
        target = path / name
        if any(parent.is_symlink() for parent in target.parents if parent != path and parent.is_relative_to(path)):
            raise ValueError('The merge touches a path under a symlink.')
        if (target.exists() or target.is_symlink()) and name not in trees[request['attached_head']]:
            raise ValueError('The merge would overwrite an untracked or ignored file.')


def checkout_plan(request):
    path = Path(request['path'])
    checkout_state(request, initial=True)
    dirty = dirty_state(path)
    reject_custom_git_commands(path, (request['attached_head'], request['saved_head']))
    common = checkout_git(path, 'merge-base', request['attached_head'], request['saved_head']).strip()
    if common in (request['attached_head'], request['saved_head']):
        raise ValueError('The original branch histories do not diverge.')
    tree = checkout_git(path, 'merge-tree', '--write-tree', request['attached_head'], request['saved_head']).splitlines()[0]
    checkout_obstructions(request, tree, dirty)
    environment = dict(os.environ, GIT_AUTHOR_NAME='Coconut local recovery',
        GIT_AUTHOR_EMAIL='local-recovery@localhost', GIT_COMMITTER_NAME='Coconut local recovery',
        GIT_COMMITTER_EMAIL='local-recovery@localhost')
    commit = checkout_git(path, 'commit-tree', tree, '-p', request['attached_head'], '-p', request['saved_head'],
                          input='Local checkout recovery (no remote operation)\n', env=environment).strip()
    return dirty, commit


class WorkspaceRecovery:
    @staticmethod
    def _workspace_recovery_receipt(receipt):
        return {k: receipt[k] for k in ('id', 'state', 'request', 'created_at', 'error', 'resume_path',
                                         'successor_task_id') if k in receipt}

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

    def _checkout_scope(self, request):
        from .service import ServiceError
        scope = self._recovery_task_scope(request)
        task = self._get('tasks', request['task_id'])
        if task['status'] not in ('completed', 'failed', 'cancelled'):
            raise ServiceError(409, 'The original owner is not terminal.')
        with contextlib.closing(sqlite3.connect((self.root / 'kanban' / 'kanban.db').as_uri() + '?mode=ro', uri=True)) as db:
            native = db.execute('SELECT status,tenant,worker_pid,current_run_id,workspace_path FROM tasks WHERE id=?',
                                (request['native_task_id'],)).fetchone()
        if (not native or native[:4] != ('archived', task['bot_id'], None, None)
                or Path(native[4]).resolve() != Path(request['path']).resolve()):
            raise ServiceError(409, 'The original native outcome does not own this idle checkout.')
        item = self._native_continuation_item(task)
        if (item['kind'] != 'work' or item['task_id'] != task['id']
                or item['bot_id'] != task['bot_id'] or item.get('_native_outcome_id') != request['native_task_id']
                or task.get('_work_context_id') not in (None, item['id'])
                or any(other['kind'] in ('work', 'pr') and other['id'] != item['id']
                       and other['task_id'] == task['id'] for other in self._all('artifacts'))):
            raise ServiceError(409, 'The original Work owner is foreign or ambiguous.')
        cancellation = self._setting('work.cancelled.work:' + item['id'])
        if (task['status'] == 'cancelled' and (self._work_identity(item) != 'work:' + item['id']
                or not cancellation)) or (task['status'] != 'cancelled' and cancellation):
            raise ServiceError(409, 'The original Work stop changed.')
        thread_tasks = {related['id'] for related in self._all('tasks')
                        if related['thread_id'] == task['thread_id']}
        if any(decision['status'] == 'pending' and decision['task_id'] in thread_tasks
               for decision in self._all('decisions')):
            raise ServiceError(409, 'A pending approval must be handled separately.')
        bot = self._get('bots', task['bot_id'])
        origin = self._get('bots', task['conversation_bot_id'])
        if (bot['archived'] or bot.get('_deleted') or origin['archived'] or origin.get('_deleted')
                or bot['session_id'] != task['session_id']
                or self._task_paused(task) or self._continuation_blocked(task) or self._work_pause(item)):
            raise ServiceError(409, 'The archived Work owner is not eligible for continuation.')
        return dict(task=task, item=item, scope=scope)

    @staticmethod
    def _checkout_resume_consent(request, scope, cancellation, dirty, device):
        return hashlib.sha256(encoded([request, scope, cancellation, dirty, device]).encode()).hexdigest()

    def checkout_recovery_preview(self, task_id, device):
        from .service import ServiceError
        with self.dispatch_gate, self.lock:
            if not self.db.execute('SELECT 1 FROM devices WHERE id=?', (device,)).fetchone():
                raise ServiceError(403, 'Pair this phone with the Mac to continue.')
            task = self._get('tasks', task_id)
            if task['status'] != 'cancelled' or not task.get('_native_outcome_id'):
                raise ServiceError(409, 'This task has no stopped checkout to recover.')
            owner = execution_key(task)
            manifest = self.root / 'workspaces' / 'worktrees.json'
            try:
                pending = self._pending_workspace_recovery()
                if pending and pending['state'] != 'held':
                    raise ValueError('Recovery is pending. Check its receipt before requesting another.')
                if manifest.is_symlink():
                    raise ValueError('The saved workspace registry is unsafe.')
                manifest_bytes = manifest.read_bytes()
                if pending:
                    if ('merge_commit' not in pending or pending['request']['task_id'] != task_id
                            or pending['device_id'] != device):
                        raise ValueError('Inspect the held recovery receipt before requesting another checkout.')
                    try:
                        unchanged_held_checkout(pending, manifest_bytes, read_only=True)
                    except ValueError as error:
                        raise ValueError('The held checkout is no longer original; inspect its held recovery receipt before retrying.') from error
                bindings = json.loads(manifest_bytes)
                if not isinstance(bindings, dict) or any(not isinstance(value, dict) for value in bindings.values()):
                    raise ValueError('The original checkout registry is ambiguous.')
                matches = [(identity, binding) for identity, binding in bindings.items()
                           if isinstance(binding, dict) and binding.get('outcome') == owner]
                if len(matches) != 1 or matches[0][1].get('retired'):
                    raise ValueError('The original checkout binding is missing or ambiguous.')
                identity, binding = matches[0]
                repository, path, saved = (binding[field] for field in ('repository', 'path', 'branch'))
                if (any(not isinstance(value, str) or not value for value in (repository, path, saved))
                        or identity != owner + '-' + hashlib.sha256(repository.encode()).hexdigest()[:16]
                        or any(not Path(value).is_absolute() or str(Path(value).resolve()) != value
                               for value in (repository, path))):
                    raise ValueError('The original checkout binding changed.')
                if any(name != identity and (not isinstance(value.get('path'), str)
                        or Path(value['path']).resolve() == Path(path)) for name, value in bindings.items()):
                    raise ValueError('Another saved binding names this checkout.')
                environment = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
                records = checkout_git(repository, 'worktree', 'list', '--porcelain', env=environment).split('\n\n')
                attached = [line.removeprefix('branch refs/heads/') for record in records
                            if 'worktree ' + path in record.splitlines()
                            for line in record.splitlines() if line.startswith('branch refs/heads/')]
                if len(attached) != 1:
                    raise ValueError('The original checkout is not attached to one branch.')
                key = str(uuid4())
                request = dict(task_id=task_id, native_task_id=task['_native_outcome_id'], expected_owner=owner,
                               repository=repository, path=path, saved_branch=saved, attached_branch=attached[0],
                               saved_head=checkout_git(repository, 'rev-parse', '--verify', 'refs/heads/' + saved,
                                                       env=environment).strip(),
                               attached_head=checkout_git(path, 'rev-parse', 'HEAD', env=environment).strip(),
                               recovery_branch='wt/recovered-' + key,
                               message='Continue the original Work in its recovered checkout. Check existing approvals before any delivery.')
                if pending and any(request[field] != pending['request'][field]
                                   for field in request if field != 'recovery_branch'):
                    raise ValueError('The held checkout incident changed; inspect its held recovery receipt before retrying.')
                scope = self._checkout_scope(request)
                validate_binding(dict(binding, branch=attached[0]))
                checkout_state(request, initial=True, read_only=True)
                dirty = dirty_state(Path(path), read_only=True)
                cancellation = self._setting('work.cancelled.work:' + scope['item']['id'])
                consent = self._checkout_resume_consent(request, (scope['scope'], scope['item']), cancellation, dirty, device)
                return dict(key=key, request=request, resume_consent=consent, dirty_count=len(dirty))
            except (ValueError, OSError, KeyError, TypeError, sqlite3.Error, subprocess.TimeoutExpired) as error:
                raise ServiceError(409, str(error)) from error

    def _checkout_cancellation(self, request, scope, device, dirty):
        from .service import ServiceError
        if scope['task']['status'] != 'cancelled':
            return None
        cancellation = self._setting('work.cancelled.work:' + scope['item']['id'])
        if (request.get('resume_confirmed') is not True or not isinstance(request.get('resume_consent'), str)
                or request['resume_consent'] != self._checkout_resume_consent(
                    {field: value for field, value in request.items() if field not in ('resume_consent', 'resume_confirmed')},
                    (scope['scope'], scope['item']), cancellation, dirty, device)):
            raise ServiceError(409, 'Confirm this stopped checkout again on the paired phone.')
        return cancellation

    def _check_checkout_consent(self, receipt, scope):
        from .service import ServiceError
        if 'cancellation' not in receipt:
            return
        cancellation = self._setting('work.cancelled.work:' + scope['item']['id'])
        if (scope['task']['status'] != 'cancelled' or cancellation != receipt['cancellation']
                or self._checkout_cancellation(receipt['request'], scope, receipt['device_id'], receipt['dirty']) != cancellation):
            raise ServiceError(409, 'The Work stop changed after confirmation.')

    def request_checkout_recovery(self, data, key, token, peer=None):
        from .service import ServiceError, now
        fields = {'task_id', 'native_task_id', 'expected_owner', 'repository', 'path', 'saved_branch',
                  'attached_branch', 'saved_head', 'attached_head', 'recovery_branch', 'message'}
        if (not isinstance(data, dict) or set(data) not in (fields, fields | {'resume_confirmed', 'resume_consent'})
                or any(not isinstance(value, str) or not value or len(value) > 4096
                       for field, value in data.items() if field not in ('message', 'resume_confirmed'))
                or ('resume_confirmed' in data and data['resume_confirmed'] is not True)
                or not isinstance(data['message'], str) or not data['message'].strip()
                or len(data['message']) > 16000):
            raise ServiceError(400, 'Supply one exact checkout, archived Work, and recovery message.')
        try:
            UUID(key)
        except (ValueError, TypeError, AttributeError):
            raise ServiceError(400, 'Supply a UUID in Idempotency-Key.') from None
        device = self.authenticate(token, peer)
        with self.dispatch_gate, self.lock:
            if not self.db.execute('SELECT 1 FROM devices WHERE id=?', (device,)).fetchone():
                raise ServiceError(403, 'Checkout recovery requires an authenticated paired device.')
            previous = self.db.execute('SELECT data FROM workspace_recoveries WHERE id=?', (key,)).fetchone()
            if previous:
                receipt = json.loads(previous[0])
                if receipt['request'] != data or receipt['device_id'] != device:
                    raise ServiceError(409, 'This recovery key belongs to another authorization.')
                return self._workspace_recovery_receipt(receipt)
            pending = self._pending_workspace_recovery()
            held_checkout = pending and pending['state'] == 'held' and 'merge_commit' in pending
            if (not self.restart_enabled or not self.kanban or self.stopping.is_set()
                    or (self.restart_pending and not held_checkout)
                    or (pending and not held_checkout)):
                raise ServiceError(409, 'Checkout recovery requires an idle managed native service.')
            try:
                if held_checkout and (pending['device_id'] != device or
                        {field: value for field, value in pending['request'].items()
                         if field not in ('recovery_branch', 'resume_consent')} !=
                        {field: value for field, value in data.items()
                         if field not in ('recovery_branch', 'resume_consent')}):
                    raise ValueError('A held checkout can only be reauthorized for the same incident and device.')
                for field in ('repository', 'path'):
                    path = Path(data[field])
                    if not path.is_absolute() or str(path.resolve()) != str(path):
                        raise ValueError('Use exact canonical absolute paths.')
                for field in ('saved_branch', 'attached_branch', 'recovery_branch'):
                    git(data['repository'], 'check-ref-format', '--branch', data[field])
                if len({data['saved_branch'], data['attached_branch'], data['recovery_branch']}) != 3:
                    raise ValueError('The three branch names must be distinct.')
                if data['recovery_branch'] != 'wt/recovered-' + key:
                    raise ValueError('The recovery branch must be unique to this authorization.')
                object_format = git(data['repository'], 'rev-parse', '--show-object-format')
                if object_format not in ('sha1', 'sha256'):
                    raise ValueError('The repository uses an unsupported Git object format.')
                oid_length = 40 if object_format == 'sha1' else 64
                if any(len(data[field]) != oid_length or any(char not in '0123456789abcdef' for char in data[field])
                       for field in ('saved_head', 'attached_head')):
                    raise ValueError('Supply exact Git commit IDs for this repository.')
                scope = self._checkout_scope(data)
                with workspace_lock(self.root):
                    manifest = self.root / 'workspaces' / 'worktrees.json'
                    if manifest.is_symlink():
                        raise ValueError('The workspace manifest is unsafe.')
                    manifest_bytes = manifest.read_bytes()
                    current = json.loads(manifest_bytes)
                    if held_checkout:
                        unchanged_held_checkout(pending, manifest_bytes)
                    identity = data['expected_owner'] + '-' + hashlib.sha256(data['repository'].encode()).hexdigest()[:16]
                    binding = current.get(identity)
                    if not isinstance(binding, dict) or any(binding.get(field) != value for field, value in (
                        ('outcome', data['expected_owner']), ('repository', data['repository']),
                        ('path', data['path']), ('branch', data['saved_branch']))):
                        raise ValueError('The saved workspace binding changed.')
                    if binding.get('retired'):
                        raise ValueError('Restore the retired workspace before checkout recovery.')
                    if any(name != identity and (not isinstance(other, dict) or not isinstance(other.get('path'), str)
                        or Path(other['path']).resolve() == Path(data['path'])) for name, other in current.items()):
                        raise ValueError('Another binding names the original checkout.')
                    validate_binding(dict(binding, branch=data['attached_branch']))
                    if subprocess.run(['git', '-C', data['repository'], 'show-ref', '--verify', '--quiet',
                                       'refs/heads/' + data['recovery_branch']], timeout=10).returncode == 0:
                        raise ValueError('The recovery branch already exists.')
                    checkout_state(data, initial=True, read_only=True)
                    consented_dirty = dirty_state(Path(data['path']), read_only=True)
                    cancellation = self._checkout_cancellation(data, scope, device, consented_dirty)
                    dirty, commit = checkout_plan(data)
                    if dirty != consented_dirty:
                        raise ValueError('The tracked edits changed after checkout confirmation. Confirm this checkout again.')
                    receipt = dict(id=key, device_id=device, request=data, task_scope=scope['scope'],
                        task_input=scope['task'],
                        work_item=scope['item'], binding=binding, binding_id=identity, dirty=dirty,
                        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                        merge_commit=commit, state='draining', created_at=now())
                    if cancellation is not None:
                        receipt['cancellation'] = cancellation
                with self.transaction():
                    if held_checkout:
                        pending['state'] = 'superseded'
                        self._put('workspace_recoveries', pending)
                    self._put('workspace_recoveries', receipt)
                self._workspace_recovery_hold = False
                self.restart_pending = True
                return self._workspace_recovery_receipt(receipt)
            except (ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as error:
                raise ServiceError(409, str(error)) from error

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
            if (pending := self._pending_workspace_recovery()) and 'merge_commit' in pending:
                raise ServiceError(409, 'An archived checkout recovery is pending inspection or replay.')
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

    def _check_recovery_writers(self, *, startup=False):
        if any(self._background_pending(task['id']) for task in self._all('tasks')):
            raise ValueError('A background completion is pending; inspect its writer before recovery.')
        allowed = ()
        if not startup:
            probe = getattr(self.provider, 'workspace_recovery_idle_pids', None)
            if probe:
                allowed = probe()
        check_writers(self.root, allowed)

    def _refuse_workspace_recovery(self, receipt, error):
        with self.transaction():
            receipt.update(state='held', error=str(error))
            self._put('workspace_recoveries', receipt)
        self._workspace_recovery_hold = True
        self.restart_pending = True

    def _recover_checkout_binding(self, receipt):
        from .service import ServiceError
        request = receipt['request']
        try:
            self._check_recovery_writers(startup=True)
            scope = self._checkout_scope(request)
            self._check_checkout_consent(receipt, scope)
            if (scope['scope'] != receipt['task_scope'] or scope['task'] != receipt['task_input']
                    or scope['item'] != receipt['work_item']):
                raise ValueError('The archived Work or its input changed before admission.')
            with workspace_lock(self.root):
                manifest = self.root / 'workspaces' / 'worktrees.json'
                if manifest.is_symlink():
                    raise ValueError('The workspace manifest is unsafe.')
                bindings = json.loads(manifest.read_text())
                identity = receipt['binding_id']
                original = receipt['binding']
                recovered = dict(original, branch=request['recovery_branch'], _recovery_request_id=receipt['id'])
                current = bindings.get(identity) if isinstance(bindings, dict) else None
                if (current != original and (not isinstance(current, dict)
                        or {key: value for key, value in current.items() if key != '_head_log'} !=
                           {key: value for key, value in recovered.items() if key != '_head_log'})):
                    raise ValueError('The saved binding changed after authorization.')
                if any(name != identity and (not isinstance(other, dict) or not isinstance(other.get('path'), str)
                    or Path(other['path']).resolve() == Path(request['path'])) for name, other in bindings.items()):
                    raise ValueError('Another binding names the original checkout.')
                ref = 'refs/heads/' + request['recovery_branch']
                result = subprocess.run(['git', '-C', request['repository'], 'rev-parse', '--verify', '--quiet', ref],
                                        capture_output=True, text=True, timeout=10)
                if result.returncode == 0 and result.stdout.strip() != receipt['merge_commit']:
                    raise ValueError('The recovery branch was changed externally.')
                if result.returncode not in (0, 1):
                    raise ValueError('The recovery branch cannot be inspected.')
                attached = git(request['path'], 'branch', '--show-current') == request['attached_branch']
                if attached:
                    checkout_state(request, initial=True)
                    if dirty_state(Path(request['path'])) != receipt['dirty']:
                        raise ValueError('Dirty tracked edits changed before recovery.')
                    checkout_obstructions(request, receipt['merge_commit'], receipt['dirty'])
                    if result.returncode:
                        checkout_git(request['repository'], '-c', 'core.hooksPath=/dev/null',
                                     'update-ref', ref, receipt['merge_commit'],
                                     '0' * len(receipt['merge_commit']))
                    checkout_git(request['path'], '-c', 'core.hooksPath=/dev/null', 'switch',
                                 '--no-overwrite-ignore', request['recovery_branch'])
                checkout_state(dict(request, merge_commit=receipt['merge_commit']), initial=False)
                if dirty_state(Path(request['path'])) != receipt['dirty']:
                    raise ValueError('Dirty tracked edits changed during recovery.')
                recovered.pop('_head_log', None)
                validate_binding(recovered)
                if bindings[identity] == original:
                    atomic_manifest(manifest, encoded(dict(bindings, **{identity: recovered})))
                elif bindings[identity] != recovered:
                    raise ValueError('The registered recovery checkpoint changed.')
                else:
                    for path in (manifest, manifest.parent):
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                with self.transaction():
                    receipt.update(state='admitting', recovered_binding=recovered)
                    receipt.pop('error', None)
                    self._put('workspace_recoveries', receipt)
            self._workspace_recovery_hold = True
            self.restart_pending = True
        except (ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired, ServiceError) as error:
            self._refuse_workspace_recovery(receipt, error)

    def _admit_checkout_recovery(self):
        from .service import ServiceError
        receipt = self._pending_workspace_recovery()
        if not receipt or 'merge_commit' not in receipt or receipt['state'] != 'admitting':
            return
        try:
            self._check_recovery_writers(startup=True)
            request = receipt['request']
            with workspace_lock(self.root):
                manifest = self.root / 'workspaces' / 'worktrees.json'
                if (manifest.is_symlink() or
                        json.loads(manifest.read_text()).get(receipt['binding_id']) != receipt['recovered_binding']):
                    raise ValueError('The recovered binding changed before admission.')
                checkout_state(dict(request, merge_commit=receipt['merge_commit']), initial=False)
                if dirty_state(Path(request['path'])) != receipt['dirty']:
                    raise ValueError('The recovered dirty edits changed before admission.')
                validate_binding(dict(receipt['recovered_binding']))
                scope = self._checkout_scope(request)
                self._check_checkout_consent(receipt, scope)
                if (scope['scope'] != receipt['task_scope'] or scope['task'] != receipt['task_input']
                        or scope['item'] != receipt['work_item']):
                    raise ValueError('The archived Work changed before admission.')
                self.kanban.call({'op': 'snapshot'})
                if self.kanban.tasks.get(request['task_id'], {}).get('status') != 'archived':
                    raise ValueError('The archived native owner changed before admission.')
                with self.transaction():
                    scope = self._checkout_scope(request)
                    self._check_checkout_consent(receipt, scope)
                    if 'cancellation' in receipt:
                        setting = 'work.cancelled.work:' + scope['item']['id']
                        removed = self.db.execute('DELETE FROM settings WHERE key=? AND value=?',
                                                  (setting, encoded(receipt['cancellation'])))
                        if removed.rowcount != 1:
                            raise ValueError('The Work stop changed before admission.')
                    self._request_device_id = receipt['device_id']
                    self._request_key = receipt['id']
                    try:
                        successor = self._reply(scope['task'], {'text': request['message']})
                    finally:
                        self._request_device_id = None
                        self._request_key = None
                    if successor['id'] == request['task_id'] or successor.get('reply_status') != 'queued_continuation':
                        raise ValueError('The native archived owner did not create a successor.')
                    self._recover()
                    receipt.update(state='applied', successor_task_id=successor['id'],
                                   resume_path='/v1/tasks/' + successor['id'] + '/replies')
                    self._put('workspace_recoveries', receipt)
            self._workspace_recovery_hold = False
            self.restart_pending = False
        except (ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired, ServiceError, RuntimeError) as error:
            self._refuse_workspace_recovery(receipt, error)

    def _recover_workspace_binding(self):
        """Called with the lifetime lock, before any native recovery or dispatch."""
        from .service import ServiceError
        receipt = self._pending_workspace_recovery()
        if not receipt:
            self._workspace_recovery_hold = any(r['state'] == 'held' for r in self._all('workspace_recoveries'))
            self.restart_pending = self._workspace_recovery_hold
            return
        if 'merge_commit' in receipt:
            self._recover_checkout_binding(receipt)
            return
        try:
            self._check_recovery_writers(startup=True)
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
