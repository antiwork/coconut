"""Keep one native Hermes worktree per outcome and repository."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace


def git(path, *arguments):
    result = subprocess.run(['git', '-C', str(path), *arguments], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError((result.stderr or 'Git could not inspect the workspace.').strip())
    return result.stdout.strip()


def validate_binding(binding):
    path = Path(binding['path'])
    repository = Path(binding['repository']).resolve()
    if not path.is_dir():
        raise ValueError('The saved worktree is missing. Inspect its Git record before creating another checkout.')
    if path.resolve() == repository or not (path / '.git').is_file():
        raise ValueError('The saved workspace is no longer a linked worktree.')
    if git(path, 'branch', '--show-current') != binding['branch']:
        raise ValueError('The saved worktree changed branches. Inspect it before continuing.')
    if Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != repository / '.git':
        raise ValueError('The saved worktree belongs to another repository.')
    return path


def acquire(root, outcome, repository, existing_worktree=None):
    root = Path(root)
    repository = Path(repository).expanduser()
    if not repository.is_absolute():
        raise ValueError('Use an absolute repository path.')
    common = Path(git(repository, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    if common.name != '.git':
        raise ValueError('Use a checkout of a non-bare Git repository.')
    repository = common.parent
    key = hashlib.sha256(str(repository).encode()).hexdigest()[:16]
    identity = outcome + '-' + key
    if '/' in outcome or outcome in ('', '.', '..'):
        raise ValueError('Invalid work identity.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.workspace.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = root / 'worktrees.json'
        saved = json.loads(manifest.read_text()) if manifest.exists() else {}
        binding = saved.get(identity)
        if binding:
            validate_binding(binding)
            return binding
        branch = 'wt/' + identity
        target = repository
        if existing_worktree:
            target = Path(existing_worktree).expanduser().resolve()
            if target == repository or not (target / '.git').is_file():
                raise ValueError('Only a linked worktree can be adopted.')
            if Path(git(target, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != common:
                raise ValueError('The worktree belongs to another repository.')
            branch = git(target, 'branch', '--show-current')
            if not branch:
                raise ValueError('The worktree must have a branch.')
        if any(Path(item['path']).resolve() == target.resolve() for item in saved.values()):
            raise ValueError('Another outcome owns this worktree. Use its existing work item to continue it.')
        from hermes_cli.kanban_db_workspace import resolve_workspace
        task = SimpleNamespace(id=identity, workspace_kind='worktree', workspace_path=str(target), branch_name=branch)
        path = resolve_workspace(task).resolve()
        if path == repository or git(path, 'branch', '--show-current') != branch:
            raise ValueError('Hermes did not resolve the expected isolated branch.')
        if any(Path(item['path']).resolve() == path for item in saved.values()):
            raise ValueError('Another outcome owns this worktree. Use its existing work item to continue it.')
        binding = dict(path=str(path), branch=branch, repository=str(repository), outcome=outcome)
        saved[identity] = binding
        temporary = manifest.with_suffix('.tmp')
        with temporary.open('w') as output:
            temporary.chmod(0o600)
            output.write(json.dumps(saved, sort_keys=True))
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(manifest)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return binding


SCHEMA = {'name': 'coconut_workspace', 'description':
    'Get this work item\'s persistent Git worktree before editing code or running repository tests. '
    'Hermes creates it and reuses it for replies and recovery. Supply the local repository path. '
    'When continuing earlier changes, pass their existing_worktree to retain their branch and files. '
    'Use the returned path as cwd for all commands. Use OrbStack for isolated test services when needed. '
    'This routine setup does not require a decision card. It does not push, merge, or deploy.',
    'parameters': {'type': 'object', 'properties': {
        'repository': {'type': 'string'}, 'existing_worktree': {'type': 'string'}}, 'required': ['repository']}}


def preferred(root, outcome, *, verify=True):
    root = Path(root)
    manifest = root / 'worktrees.json'
    if manifest.exists():
        bindings = [v for v in json.loads(manifest.read_text()).values() if v['outcome'] == outcome]
        if verify:
            for binding in bindings:
                validate_binding(binding)
        if len(bindings) == 1:
            if Path(bindings[0]['path']).is_dir():
                return Path(bindings[0]['path'])
    return root / outcome
