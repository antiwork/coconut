import contextlib
from datetime import datetime, timedelta, timezone
import copy
import errno
import fcntl
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from coconut import work_workspace
from coconut import worktree_retirement as retirement
from coconut.providers import FixtureProvider
from coconut.service import Service
from coconut.work_workspace import acquire, git, preferred, save_bindings, validate_binding

OLD = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace('+00:00', 'Z')
RECENT = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def environments_visible():
    """Return True when this system shows another process's environment.

    macOS hides the environment of its platform binaries, such as /bin/sleep, so the probe runs this Python.
    """
    if not shutil.which('lsof') or os.uname().sysname != 'Darwin':
        return False
    # Coconut never stops a shared program, so an orphan of such an interpreter cannot show the rules.
    if work_workspace.shared(sys.executable):
        return False
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'], env=dict(os.environ, COCONUT_PROBE='1'))
    try:
        return work_workspace.environment_names(process.pid, 'COCONUT_PROBE=1')
    finally:
        process.kill()
        process.wait()


ENVIRONMENTS_VISIBLE = environments_visible()


class WorktreeRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init')
        # A merged pull request counts only for the repository that its URL names.
        git(self.repo, 'remote', 'add', 'origin', 'git@github.com:antiwork/coconut.git')
        (self.repo / '.gitignore').write_text('build/\n.env\nnode_modules/\n/tmp\n')
        (self.repo / 'tracked.txt').write_text('one\ntwo\n')
        (self.repo / 'logo.bin').write_bytes(bytes(range(256)))
        (self.repo / 'obsolete.txt').write_text('remove me\n')
        git(self.repo, 'add', '.')
        self.commit(self.repo, 'Initial')
        self.service = Service(self.root / 'coconut', FixtureProvider(self.root / 'coconut'), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Coder', 'role': ''}, str(uuid4()))
        self.workspaces = self.service.root / 'workspaces'
        self.workspaces.mkdir(mode=0o700, exist_ok=True)
        # Most tests do not depend on lsof or send signals. The process tests below use real processes.
        self.holders = patch.multiple('coconut.work_workspace', holders=MagicMock(side_effect=lambda path, detail=False: {} if detail else set()),
                                      holders_by_path=MagicMock(side_effect=lambda paths, detail=False: {
                                          str(p): {} if detail else set() for p in paths}))
        self.holders.start()
        self.addCleanup(self.holders.stop)
        # Most tests check retirement itself. The discard of long idle or low-disk worktrees has its own tests.
        rules = patch.multiple(retirement, DISCARD_IDLE_SECONDS=10 ** 9, LOW_DISK_BYTES=0,
                               BOT_WORKSPACE_STALE_SECONDS=10 ** 9, BOT_WORKSPACE_LOW_DISK_STALE_SECONDS=10 ** 9)
        rules.start()
        self.addCleanup(rules.stop)

    def commit(self, path, message):
        git(path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-q', '-m', message)

    def outcome(self, status='completed', updated_at=OLD):
        outcome = str(uuid4())
        task = dict(id=str(uuid4()), bot_id=self.bot['id'], _execution_session_id=outcome, title='Change',
                    status=status, summary='', created_at=OLD, updated_at=updated_at)
        with self.service.transaction():
            self.service._put('tasks', task)
        identity = outcome + '-' + hashlib.sha256(str(self.repo).encode()).hexdigest()[:16]
        path = self.repo / '.worktrees' / identity
        git(self.repo, 'worktree', 'add', '-q', '-b', 'wt/' + identity, str(path))
        binding = dict(path=str(path), branch='wt/' + identity, repository=str(self.repo), outcome=outcome)
        validate_binding(binding)
        manifest = self.workspaces / 'worktrees.json'
        saved = json.loads(manifest.read_text()) if manifest.exists() else {}
        saved[identity] = binding
        save_bindings(manifest, saved)
        return outcome, identity, path, task

    def registry(self):
        return json.loads((self.workspaces / 'worktrees.json').read_text())

    def retire(self):
        self.service._next_worktree_retirement = 0
        return self.service.retire_finished_worktrees()

    def pull_request(self, task, state):
        with self.service.transaction():
            self.service._put('artifacts', dict(id=str(uuid4()), kind='pr', bot_id=self.bot['id'], task_id=task['id'],
                                                url='https://github.com/antiwork/coconut/pull/' + str(len(self.service._all('artifacts')) + 1),
                                                pr_state=state, created_at=OLD))

    def test_a_merged_outcome_loses_its_worktree_without_the_resumable_work_checks(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        # Each of these would keep the worktree of resumable work.
        git(path, 'checkout', '-q', '-b', 'published-branch')
        (path / 'committed.txt').write_text('in the pull request\n')
        git(path, 'add', 'committed.txt')
        self.commit(path, 'Work')
        (path / 'tracked.txt').write_text('one\nstaged\n')
        git(path, 'add', 'tracked.txt')
        (path / 'logo.bin').write_bytes(b'unstaged')
        (path / 'notes.txt').write_text('untracked\n')
        (path / 'build').mkdir()
        (path / 'build' / 'output.log').write_bytes(b'x' * 64)
        inner = path / '.worktrees' / 'subagent'
        git(path, 'worktree', 'add', '-q', '-b', 'subagent-branch', str(inner))
        (inner / 'tracked.txt').write_text('nested change\n')
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 10):
            self.assertEqual(self.retire(), [identity])
        self.assertFalse(path.exists())
        self.assertFalse(inner.exists())
        self.assertNotIn(str(inner), git(self.repo, 'worktree', 'list', '--porcelain'))
        binding = self.registry()[identity]
        self.assertEqual((binding['branch'], binding['retired']['done']), ('published-branch', True))
        # The branches keep their commits. The archive keeps the patches and the small untracked files.
        self.assertEqual(git(self.repo, 'show', 'published-branch:committed.txt'), 'in the pull request')
        with tarfile.open(binding['retired']['archive']) as tar:
            self.assertEqual(sorted(tar.getnames()), ['retirement.json', 'staged.patch', 'unstaged.patch', 'untracked/notes.txt'])
            self.assertIn(b'+staged', tar.extractfile('staged.patch').read())
        # A later turn of the outcome gets a new checkout of the saved branch.
        restored = acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(git(restored['path'], 'branch', '--show-current'), 'published-branch')
        self.assertEqual((Path(restored['path']) / 'committed.txt').read_text(), 'in the pull request\n')
        self.assertNotIn('retired', self.registry()[identity])

    def test_a_long_idle_worktree_that_retirement_refuses_is_discarded(self):
        outcome, identity, path, _ = self.outcome()
        git(path, 'checkout', '-q', '-b', 'switched-branch')
        (path / 'draft.txt').write_text('keep\n')
        (path / 'tmp').mkdir()
        (path / 'tmp' / 'cache.bin').write_bytes(b'x' * 64)
        # The outcome is idle for 3 days. Retirement refuses the branch switch, and the discard follows.
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60), \
                self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            self.assertEqual(self.retire(), [identity])
        self.assertFalse(path.exists())
        self.assertTrue(any('refused worktree' in line and 'changed branches' in line for line in logs.output))
        binding = self.registry()[identity]
        self.assertEqual((binding['branch'], binding['retired']['done']), ('switched-branch', True))
        with tarfile.open(binding['retired']['archive']) as tar:
            self.assertIn('untracked/draft.txt', tar.getnames())
        restored = acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(git(restored['path'], 'branch', '--show-current'), 'switched-branch')

    def test_a_discard_keeps_the_commits_of_a_detached_head(self):
        _, identity, path, _ = self.outcome()
        git(path, 'checkout', '-q', '--detach')
        (path / 'tracked.txt').write_text('detached work\n')
        git(path, 'add', 'tracked.txt')
        self.commit(path, 'Detached commit')
        head = git(path, 'rev-parse', 'HEAD')
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60):
            self.assertEqual(self.retire(), [identity])
        self.assertFalse(path.exists())
        refs = git(self.repo, 'for-each-ref', '--format=%(refname) %(objectname)', f'refs/coconut/discarded/{identity}/')
        self.assertTrue(refs.endswith(f'-{head} {head}'))

    def test_a_merged_pull_request_of_another_repository_keeps_the_worktree(self):
        _, identity, path, task = self.outcome()
        with self.service.transaction():
            self.service._put('artifacts', dict(id=str(uuid4()), kind='pr', bot_id=self.bot['id'], task_id=task['id'],
                                                url='https://github.com/antiwork/gumroad/pull/1', pr_state='merged', created_at=OLD))
        git(path, 'checkout', '-q', '-b', 'switched-branch')
        self.assertEqual(self.retire(), [])
        self.assertTrue(path.exists())

    def test_a_refused_removal_keeps_no_discard_archive(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / 'draft.txt').write_text('keep\n')
        real = work_workspace.git

        def refuse(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'remove'):
                raise ValueError('Git refused the removal.')
            return real(where, *arguments, **options)
        for _ in range(2):
            with patch.object(work_workspace, 'git', side_effect=refuse):
                self.assertEqual(self.retire(), [])
        # The checkout still holds the work, so no archive stays, and a repeated failure adds none.
        self.assertEqual(list((self.workspaces / 'archive' / 'done').glob('*.tar.gz')), [])
        self.assertTrue((path / 'draft.txt').exists())

    def test_a_refused_removal_of_a_clean_checkout_is_logged(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        real = work_workspace.git

        def refuse(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'remove'):
                raise ValueError('Git refused the removal.')
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=refuse), \
                self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            self.assertEqual(self.retire(), [])
        self.assertTrue(any('Git refused the removal' in line for line in logs.output))

    def test_a_partial_removal_names_the_archive_and_fences_the_path(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / 'draft.txt').write_text('keep\n')
        real = work_workspace.git

        def partial(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'remove'):
                (path / '.git').unlink()
                raise subprocess.TimeoutExpired('git', 600)
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=partial):
            self.assertEqual(self.retire(), [])
        retired = self.registry()[identity]['retired']
        self.assertTrue(Path(retired['archive']).exists())
        self.assertIn('Inspect the path', retired['restore_failed']['error'])
        self.assertEqual(work_workspace.preferred(self.workspaces, outcome), self.workspaces / outcome)

    def test_discarded_refs_go_30_days_after_the_discard(self):
        head = git(self.repo, 'rev-parse', 'HEAD')
        old, new = int(time.time()) - 31 * 86400, int(time.time())
        for moment in (old, new):
            git(self.repo, 'update-ref', f'refs/coconut/discarded/x/{moment}-{head}', head)
        work_workspace.prune_discarded_refs(self.repo)
        # The commit date does not count. The discard time in the name does.
        self.assertEqual(git(self.repo, 'for-each-ref', '--format=%(refname)', 'refs/coconut/discarded/'),
                         f'refs/coconut/discarded/x/{new}-{head}')

    def test_an_open_pull_request_keeps_a_long_idle_refused_worktree(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'open')
        git(path, 'checkout', '-q', '-b', 'switched-branch')
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60):
            self.assertEqual(self.retire(), [])
        self.assertTrue(path.exists())

    def test_a_resumed_discard_gets_its_changes_back(self):
        outcome, identity, path, _ = self.outcome()
        git(path, 'checkout', '-q', '-b', 'switched-branch')
        (path / 'tracked.txt').write_text('one\nstaged\n')
        git(path, 'add', 'tracked.txt')
        (path / 'logo.bin').write_bytes(b'unstaged')
        (path / 'draft.txt').write_text('untracked\n')
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60):
            self.assertEqual(self.retire(), [identity])
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertEqual(git(restored, 'diff', '--cached', '--name-only'), 'tracked.txt')
        self.assertEqual((restored / 'logo.bin').read_bytes(), b'unstaged')
        self.assertEqual((restored / 'draft.txt').read_text(), 'untracked\n')

    def test_the_origin_must_be_on_github(self):
        cache = {}
        git(self.repo, 'remote', 'set-url', 'origin', 'git@notgithub.com:antiwork/coconut.git')
        self.assertIsNone(retirement.repository_name(str(self.repo), cache))
        for url in ('https://github.com/Antiwork/Coconut.git', 'git@github.com:antiwork/coconut', 'ssh://git@github.com/antiwork/coconut.git'):
            git(self.repo, 'remote', 'set-url', 'origin', url)
            self.assertEqual(retirement.repository_name(str(self.repo), {}), 'antiwork/coconut')

    def test_a_failed_restore_of_a_discarded_branch_sends_turns_to_the_scratch_workspace(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        branch = self.registry()[identity]['branch']
        self.assertEqual(self.retire(), [identity])
        git(self.repo, 'branch', '-D', branch)
        self.assertEqual(work_workspace.preferred(self.workspaces, outcome), self.workspaces / outcome)
        self.assertIn('restore_failed', self.registry()[identity]['retired'])

    def test_a_busy_service_lock_skips_the_long_idle_discard(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'open')
        git(path, 'checkout', '-q', '-b', 'switched-branch')
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60), \
                patch.object(self.service, '_done_outcomes', return_value=None):
            self.assertEqual(self.retire(), [])
        self.assertTrue(path.exists())

    def test_an_unmarked_stop_needs_the_path_on_the_command_line_and_keeps_session_programs(self):
        worktree = '/work/tree'
        table = {10: (1, 10 ** 6, '/usr/bin/tmux'), 11: (1, 10 ** 6, '/usr/local/bin/node'), 12: (1, 10 ** 6, '/opt/homebrew/bin/watchman'),
                 13: (1, 10 ** 6, '/System/Library/mdworker_shared'), 14: (1, 10 ** 6, '/usr/local/bin/node')}
        lines = {10: f'tmux new -c {worktree}', 11: f'node {worktree}/node_modules/.bin/vite', 12: f'watchman --path {worktree}',
                 13: 'mdworker_shared -s mdworker', 14: 'node /elsewhere/server.js'}
        with patch.object(work_workspace, 'process_table', return_value=table), \
                patch.object(work_workspace, 'shared', return_value=False), \
                patch.object(work_workspace, 'arguments', side_effect=lambda pid: lines[pid]), \
                patch.object(work_workspace, 'working_folder_inside', side_effect=lambda pid, folder: pid == 15), \
                patch.object(work_workspace, 'stop', side_effect=lambda pids, sig, seconds: set()) as stop:
            table[15] = (1, 10 ** 6, '/usr/local/bin/node')
            lines[15] = 'node server.js'
            work_workspace.stop_orphans({10, 11, 12, 13, 14, 15}, 'outcome', 1, marked=False, within=worktree)
        stopped = set().union(*(call.args[0] for call in stop.call_args_list))
        # Only the dev server that names the worktree goes. A session program, a daemon that names the worktree, a
        # system daemon that only holds a file, and a program that names another path stay.
        self.assertEqual(stopped, {11, 15})

    def test_the_working_folder_of_a_process_is_read_from_lsof(self):
        outside = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd='/')
        self.addCleanup(outside.kill)
        _, identity, path, _ = self.outcome()
        inside = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=path)
        self.addCleanup(inside.kill)
        if not shutil.which('lsof'):
            self.skipTest('The test needs lsof.')
        self.assertTrue(work_workspace.working_folder_inside(inside.pid, path))
        self.assertFalse(work_workspace.working_folder_inside(outside.pid, path))

    def test_a_resumed_discard_gets_its_links_back(self):
        outcome, identity, path, _ = self.outcome()
        git(path, 'checkout', '-q', '-b', 'switched-branch')
        (path / 'target.txt').write_text('target\n')
        git(path, 'add', 'target.txt')
        self.commit(path, 'Target')
        (path / 'local.link').symlink_to('target.txt')
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60):
            self.assertEqual(self.retire(), [identity])
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertEqual(os.readlink(restored / 'local.link'), 'target.txt')

    def test_a_refused_discard_removal_waits_a_day(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / 'draft.txt').write_text('keep\n')
        real = work_workspace.git
        attempts = []

        def refuse(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'remove'):
                attempts.append(1)
                raise ValueError('Git refused the removal.')
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=refuse):
            self.retire()
            self.retire()
        self.assertEqual(len(attempts), 1)
        self.assertIn('retire_failed', self.registry()[identity])

    def test_the_scratch_trash_is_emptied_by_the_sweep(self):
        left = self.workspaces / work_workspace.SCRATCH_TRASH / 'stopped-1'
        left.mkdir(parents=True)
        (left / 'big.bin').write_bytes(b'x')
        work_workspace.sweep_temporary(self.workspaces)
        self.assertFalse(left.exists())

    def test_a_gone_checkout_keeps_the_archive_of_an_earlier_failed_retirement(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        kept = self.workspaces / 'archive' / (identity + '.tar.gz')
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_bytes(b'only copy')
        saved = self.registry()
        saved[identity]['retire_failed'] = dict(at=OLD, error='Git refused', archive=str(kept))
        save_bindings(self.workspaces / 'worktrees.json', saved)
        head = git(path, 'rev-parse', 'HEAD')
        with tarfile.open(kept, 'w:gz') as tar:
            data = json.dumps(dict(head=head)).encode()
            info = tarfile.TarInfo('retirement.json')
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        git(self.repo, 'worktree', 'remove', '--force', str(path))
        self.assertEqual(self.retire(), [identity])
        self.assertTrue(kept.exists())
        retired = self.registry()[identity]['retired']
        self.assertEqual((retired['archive'], retired['head']), (str(kept), head))

    def test_a_new_branch_never_moves_an_existing_branch(self):
        outcome, identity, path, _ = self.outcome()
        original = git(path, 'branch', '--show-current')
        (path / 'tracked.txt').write_text('one\ntwo\nthree\n')
        git(path, 'add', 'tracked.txt')
        self.commit(path, 'Work on the wt branch')
        tip = git(path, 'rev-parse', 'HEAD')
        main = git(self.repo, 'branch', '--show-current')
        git(path, 'checkout', '-q', '--detach')
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60):
            self.assertEqual(self.retire(), [identity])
        saved = self.registry()
        # The saved branch is held by the main checkout, so the resume needs another name.
        saved[identity]['branch'] = main
        save_bindings(self.workspaces / 'worktrees.json', saved)
        acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(git(self.repo, 'rev-parse', original), tip)
        self.assertNotEqual(self.registry()[identity]['branch'], original)

    def test_a_worktree_that_holds_the_tracked_worktree_of_another_outcome_is_not_discarded(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        # The other outcome is active, so its own retirement does not run.
        other, other_identity, other_path, _ = self.outcome(status='working', updated_at=RECENT)
        nested = path / '.worktrees' / 'adopted'
        nested.parent.mkdir(exist_ok=True)
        git(self.repo, 'worktree', 'move', str(other_path), str(nested))
        saved = self.registry()
        saved[other_identity]['path'] = str(nested)
        save_bindings(self.workspaces / 'worktrees.json', saved)
        (nested / 'draft.txt').write_text('other outcome work\n')
        self.retire()
        self.assertTrue((nested / 'draft.txt').exists())
        self.assertNotIn('retired', self.registry()[identity])

    def test_a_failed_branch_creation_keeps_the_saved_branch_name(self):
        outcome, identity, path, _ = self.outcome()
        saved_branch = self.registry()[identity]['branch']
        main = git(self.repo, 'branch', '--show-current')
        git(path, 'checkout', '-q', '--detach')
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60):
            self.assertEqual(self.retire(), [identity])
        saved = self.registry()
        saved[identity]['branch'] = main
        save_bindings(self.workspaces / 'worktrees.json', saved)
        real = work_workspace.git

        def refuse(where, *arguments, **options):
            if arguments[:1] == ('branch',) and len(arguments) == 3:
                raise ValueError('Git could not create the branch.')
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=refuse), self.assertRaises(ValueError):
            acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(self.registry()[identity]['branch'], main)
        self.assertIn('restore_failed', self.registry()[identity]['retired'])

    def test_a_failed_pull_request_read_does_not_stop_the_pass(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        with patch.object(self.service, '_done_outcomes', side_effect=sqlite3.OperationalError('closed')):
            # The pass goes on. The merged outcome gets the ordinary retirement, with the full archive.
            self.assertEqual(self.retire(), [identity])
        self.assertFalse(path.exists())
        self.assertFalse(self.registry()[identity]['retired'].get('done'))

    def test_a_scratch_folder_that_holds_a_tracked_worktree_stays(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        scratch = self.workspaces / outcome
        scratch.mkdir(exist_ok=True)
        (scratch / 'notes.txt').write_text('keep\n')
        other, other_identity, other_path, _ = self.outcome(status='working', updated_at=RECENT)
        inner = scratch / 'adopted'
        git(self.repo, 'worktree', 'move', str(other_path), str(inner))
        saved = self.registry()
        saved[other_identity]['path'] = str(inner)
        save_bindings(self.workspaces / 'worktrees.json', saved)
        self.retire()
        self.assertTrue((scratch / 'notes.txt').exists())

    def test_the_open_file_scan_runs_before_the_registry_lock(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / 'draft.txt').write_text('keep\n')
        seen = []

        def scan(where):
            # A second open of the lock file conflicts with the lock of this process only while it is held.
            with (self.workspaces / '.workspace.lock').open('a') as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen.append('free')
                    fcntl.flock(probe, fcntl.LOCK_UN)
                except BlockingIOError:
                    seen.append('held')
            return False
        with patch.object(work_workspace, 'in_use', side_effect=scan):
            self.assertEqual(self.retire(), [identity])
        self.assertTrue(seen and set(seen) == {'free'}, seen)

    def test_the_binding_names_the_archive_before_the_removal(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / 'draft.txt').write_text('keep\n')
        real = work_workspace.git
        seen = []

        def watch(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'remove'):
                retired = self.registry()[identity].get('retired')
                seen.append(retired and Path(retired['archive']).exists())
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=watch):
            self.assertEqual(self.retire(), [identity])
        # A stop during the removal leaves a binding that names an archive that exists.
        self.assertEqual(seen, [True])

    def test_patches_over_the_limit_are_dropped_with_a_warning(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / 'tracked.txt').write_text('one\ntwo\n' + 'x' * 4096)
        (path / 'draft.txt').write_text('keep\n')
        with patch.object(work_workspace, 'PATCH_LIMIT_BYTES', 100), self.assertLogs('coconut.work_workspace', 'WARNING') as logs:
            self.assertEqual(self.retire(), [identity])
        self.assertTrue(any('staged and unstaged changes' in line for line in logs.output))
        with tarfile.open(self.registry()[identity]['retired']['archive']) as tar:
            self.assertEqual(tar.extractfile('unstaged.patch').read(), b'')
            self.assertIn('untracked/draft.txt', tar.getnames())

    def test_a_stop_after_the_record_leaves_a_checkout_that_the_resume_adopts(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / 'draft.txt').write_text('keep\n')
        real = work_workspace.git

        def stopped(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'remove'):
                raise KeyboardInterrupt()
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=stopped), self.assertRaises(KeyboardInterrupt):
            self.retire()
        self.assertTrue(self.registry()[identity].get('retired'))
        self.assertTrue((path / 'draft.txt').exists())
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertEqual((restored / 'draft.txt').read_text(), 'keep\n')
        self.assertNotIn('retired', self.registry()[identity])

    def test_scratch_files_stay_when_a_merged_pull_request_is_in_another_repository(self):
        outcome, identity, path, task = self.outcome()
        with self.service.transaction():
            self.service._put('artifacts', dict(id=str(uuid4()), kind='pr', bot_id=self.bot['id'], task_id=task['id'],
                                                url='https://github.com/antiwork/gumroad/pull/1', pr_state='merged', created_at=OLD))
        scratch = self.workspaces / outcome
        (scratch / 'clone').mkdir(parents=True)
        (scratch / 'notes.txt').write_text('a clone of the other repository, with work\n')
        self.retire()
        self.assertTrue((scratch / 'notes.txt').exists())

    def test_a_failed_reapply_tells_the_bot_and_leaves_a_clean_checkout(self):
        outcome, identity, path, _ = self.outcome()
        git(path, 'checkout', '-q', '-b', 'switched-branch')
        (path / 'tracked.txt').write_text('one\nstaged\n')
        git(path, 'add', 'tracked.txt')
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60):
            self.assertEqual(self.retire(), [identity])
        # The branch moved, so the patches no longer fit.
        (self.repo / 'tracked.txt').write_text('one\ntwo\nthree\n')
        git(self.repo, 'add', 'tracked.txt')
        self.commit(self.repo, 'Move on')
        git(self.repo, 'branch', '-f', 'switched-branch', git(self.repo, 'rev-parse', 'HEAD'))
        with self.assertRaisesRegex(ValueError, 'archive .* keeps them'):
            acquire(self.workspaces, outcome, str(self.repo))
        self.assertIn('restore_failed', self.registry()[identity]['retired'])
        self.assertEqual(git(path, 'status', '--porcelain'), '')
        # The clean checkout is adopted at the next restore, and the outcome goes on.
        again = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertEqual(again, path)
        self.assertNotIn('retired', self.registry()[identity])

    def test_a_low_disk_shortens_the_retry_delay_of_a_refused_removal_to_an_hour(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')

        def failed(hours):
            moment = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace('+00:00', 'Z')
            saved = self.registry()
            saved[identity]['retire_failed'] = dict(at=moment, error='Git refused', archive=None)
            save_bindings(self.workspaces / 'worktrees.json', saved)
        failed(0.1)
        with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            # A failure of six minutes ago waits, also on a low disk.
            self.assertEqual(self.retire(), [])
        failed(2)
        # Two hours ago waits for a normal disk, and not for a low one.
        self.assertEqual(self.retire(), [])
        with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            self.assertEqual(self.retire(), [identity])

    def test_a_missing_nested_checkout_does_not_block_the_discard(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        other, other_identity, other_path, _ = self.outcome(status='working', updated_at=RECENT)
        git(self.repo, 'worktree', 'remove', '--force', str(other_path))
        saved = self.registry()
        saved[other_identity]['path'] = str(path / '.worktrees' / 'gone')
        save_bindings(self.workspaces / 'worktrees.json', saved)
        self.assertEqual(self.retire(), [identity])

    def test_the_named_discard_archives_stay_within_their_limit(self):
        first, second = [], []
        for bucket in (first, second):
            _, identity, path, task = self.outcome()
            self.pull_request(task, 'merged')
            (path / 'draft.txt').write_text('x' * 2000)
            self.assertEqual(self.retire(), [identity])
            bucket.append(Path(self.registry()[identity]['retired']['archive']))
        os.utime(first[0], (time.time() - 3600,) * 2)
        with patch.object(work_workspace, 'DISCARD_ARCHIVE_LIMIT_BYTES', second[0].stat().st_size + 10):
            work_workspace.sweep_temporary(self.workspaces)
        self.assertFalse(first[0].exists())
        self.assertTrue(second[0].exists())

    def test_a_discarded_detached_head_gets_a_branch_and_an_interrupted_removal_is_adopted(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        git(path, 'checkout', '-q', '--detach')
        (path / 'tracked.txt').write_text('detached work\n')
        git(path, 'add', 'tracked.txt')
        self.commit(path, 'Detached commit')
        head = git(path, 'rev-parse', 'HEAD')
        real = work_workspace.git

        def stopped(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'remove'):
                raise KeyboardInterrupt()
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=stopped), self.assertRaises(KeyboardInterrupt):
            self.retire()
        branch = self.registry()[identity]['branch']
        self.assertTrue(branch.endswith('-detached'))
        self.assertEqual(git(self.repo, 'rev-parse', branch), head)
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertEqual(git(restored, 'branch', '--show-current'), branch)
        self.assertEqual(git(restored, 'rev-parse', 'HEAD'), head)

    def test_a_refused_detached_discard_leaves_no_branch_behind(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        git(path, 'checkout', '-q', '--detach')
        real = work_workspace.git

        def refuse(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'remove'):
                raise ValueError('Git refused the removal.')
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=refuse):
            self.retire()
        self.assertEqual(git(self.repo, 'branch', '--list', 'wt/*-detached*'), '')

    def test_a_checkout_with_one_deleted_file_is_adopted(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / 'obsolete.txt').unlink()
        real = work_workspace.git

        def stopped(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'remove'):
                raise KeyboardInterrupt()
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=stopped), self.assertRaises(KeyboardInterrupt):
            self.retire()
        # The bot deleted one file before the stop. The checkout is whole, and the resume adopts it.
        acquire(self.workspaces, outcome, str(self.repo))
        self.assertNotIn('retired', self.registry()[identity])

    def test_a_failed_archive_write_leaves_no_branch_and_no_temporary_file(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        git(path, 'checkout', '-q', '--detach')
        (path / 'draft.txt').write_text('keep\n')
        real = work_workspace.write_archive

        def full_disk(temporary, *arguments, **options):
            Path(temporary).write_bytes(b'half an archive')
            raise OSError(28, 'No space left on device')
        with patch.object(work_workspace, 'write_archive', side_effect=full_disk):
            self.retire()
        self.assertEqual(git(self.repo, 'branch', '--list', 'wt/*-detached*'), '')
        self.assertEqual(list((self.workspaces / 'archive' / 'done').glob('.*.tmp')), [])

    def test_a_discard_during_a_rebase_keeps_the_saved_branch(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        saved_branch = self.registry()[identity]['branch']
        # Git names a rebase in progress with a folder, and HEAD is detached while it runs.
        git(path, 'checkout', '-q', '--detach')
        marker = Path(git(path, 'rev-parse', '--path-format=absolute', '--git-path', 'rebase-merge'))
        marker.mkdir(parents=True)
        self.assertEqual(self.retire(), [identity])
        self.assertEqual(self.registry()[identity]['branch'], saved_branch)
        self.assertEqual(git(self.repo, 'branch', '--list', 'wt/*-detached*'), '')

    def test_a_linked_scratch_trash_moves_nothing(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        scratch = self.workspaces / outcome
        scratch.mkdir()
        (scratch / 'notes.txt').write_text('scratch\n')
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        (self.workspaces / work_workspace.SCRATCH_TRASH).symlink_to(elsewhere, target_is_directory=True)
        self.retire()
        self.assertTrue((scratch / 'notes.txt').exists())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_checkout_that_changed_after_the_record_is_not_removed_and_a_partial_one_needs_a_person(self):
        def commit(path):
            git(path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'Later')

        def remove_files(path):
            for name in ('tracked.txt', 'logo.bin'):
                (path / name).unlink()
        damages = (('new file', lambda path: (path / 'later.txt').write_text('an edit after the record\n')),
                   ('edit of an untracked file', lambda path: (path / 'draft.txt').write_text('changed after the record\n')),
                   ('edit of an ignored file', lambda path: (path / '.env').write_text('CHANGED=1\n')),
                   ('new commit', commit), ('partial removal', remove_files))
        for name, damage in damages:
            with self.subTest(name):
                outcome, identity, path, task = self.outcome()
                self.pull_request(task, 'merged')
                (path / 'draft.txt').write_text('keep\n')
                (path / '.env').write_text('SECRET=value\n')
                real = work_workspace.git

                def stopped(where, *arguments, **options):
                    if arguments[:2] == ('worktree', 'remove'):
                        damage(path)
                        raise KeyboardInterrupt()
                    return real(where, *arguments, **options)
                with patch.object(work_workspace, 'git', side_effect=stopped), self.assertRaises(KeyboardInterrupt):
                    self.retire()
                # The checkout is not the one that the archive holds. The resume leaves it alone, and asks for a look.
                with self.assertRaisesRegex(ValueError, 'not empty'):
                    acquire(self.workspaces, outcome, str(self.repo))
                self.assertIn('restore_failed', self.registry()[identity]['retired'])

    def test_scratch_files_of_an_outcome_without_a_binding_wait_for_the_long_idle_rule(self):
        outcome = str(uuid4())
        task = dict(id=str(uuid4()), bot_id=self.bot['id'], _execution_session_id=outcome, title='Scratch', status='completed',
                    summary='', created_at=OLD, updated_at=(datetime.now(timezone.utc) - timedelta(hours=7)).isoformat().replace('+00:00', 'Z'))
        with self.service.transaction():
            self.service._put('tasks', task)
            self.service._put('artifacts', dict(id=str(uuid4()), kind='pr', bot_id=self.bot['id'], task_id=task['id'],
                                                url='https://github.com/antiwork/coconut/pull/9', pr_state='merged', created_at=OLD))
        scratch = self.workspaces / outcome
        scratch.mkdir()
        (scratch / 'notes.txt').write_text('unfinished work in a repository that no binding names\n')
        self.retire()
        self.assertTrue((scratch / 'notes.txt').exists())

    def test_a_discard_keeps_a_small_ignored_file_and_puts_it_back(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / '.env').write_text('SECRET=value\n')
        (path / 'node_modules').mkdir()
        (path / 'node_modules' / 'pkg.js').write_text('build output')
        self.assertEqual(self.retire(), [identity])
        with tarfile.open(self.registry()[identity]['retired']['archive']) as tar:
            names = tar.getnames()
        self.assertIn('ignored/.env', names)
        self.assertFalse(any('node_modules' in name for name in names))
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertEqual((restored / '.env').read_text(), 'SECRET=value\n')

    def test_a_missing_archive_tells_the_bot(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / 'draft.txt').write_text('keep\n')
        self.assertEqual(self.retire(), [identity])
        Path(self.registry()[identity]['retired']['archive']).unlink()
        with self.assertRaisesRegex(ValueError, 'is gone'):
            acquire(self.workspaces, outcome, str(self.repo))

    def test_the_snapshot_digest_cannot_be_forged_by_rearranging_files(self):
        _, identity, path, task = self.outcome()
        (path / 'a').write_bytes(b'untracked/b\0R')
        (path / 'b').write_bytes(b'')
        head = git(path, 'rev-parse', 'HEAD')
        one = work_workspace.snapshot_digest(path, head, b'', b'', [('untracked/a', 'a')])
        (path / 'a').write_bytes(b'')
        (path / 'b').write_bytes(b'R')
        two = work_workspace.snapshot_digest(path, head, b'', b'', [('untracked/a', 'a'), ('untracked/b', 'b')])
        self.assertNotEqual(one, two)

    def test_a_dropped_nested_repository_is_logged(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        clone = path / 'other-clone'
        clone.mkdir()
        git(clone, 'init', '-q')
        with self.assertLogs('coconut.work_workspace', 'WARNING') as logs:
            self.assertEqual(self.retire(), [identity])
        self.assertTrue(any('discarded the nested repositories' in line and 'other-clone' in line for line in logs.output))

    def test_a_refused_discard_does_not_log_a_dropped_repository(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        clone = path / 'other-clone'
        clone.mkdir()
        git(clone, 'init', '-q')
        real = work_workspace.git

        def refuse(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'remove'):
                raise ValueError('Git refused the removal.')
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=refuse), self.assertLogs('coconut.work_workspace', 'WARNING') as logs:
            logging.getLogger('coconut.work_workspace').warning('start')
            self.retire()
        self.assertFalse(any('other-clone' in line for line in logs.output))

    def test_the_scratch_recheck_confirms_the_idle_time(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (self.workspaces / outcome).mkdir()
        ((self.workspaces / outcome) / 'notes.txt').write_text('scratch\n')
        real = self.service._finished_outcomes
        idles = []

        def watched(current, *, timeout=-1, only=None, idle=None, **options):
            if only == outcome:
                idles.append((idle, options.get('urgent', False)))
            return real(current, timeout=timeout, only=only, idle=idle, **options)
        with patch.object(self.service, '_finished_outcomes', side_effect=watched):
            self.retire()
        # Every re-check of this outcome, also the one before the scratch move, uses the real idle time.
        self.assertTrue(idles and all(idle in (None, retirement.WORKTREE_RETIRE_SECONDS) and not urgent for idle, urgent in idles), idles)

    def test_a_second_discard_keeps_the_first_archive(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / 'first.txt').write_text('first\n')
        self.assertEqual(self.retire(), [identity])
        first = self.registry()[identity]['retired']['archive']
        acquire(self.workspaces, outcome, str(self.repo))
        (path / 'second.txt').write_text('second\n')
        self.assertEqual(self.retire(), [identity])
        second = self.registry()[identity]['retired']['archive']
        self.assertNotEqual(first, second)
        self.assertTrue(Path(first).exists() and Path(second).exists())

    def test_a_discard_drops_the_archive_of_an_earlier_failed_removal(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        stale = self.workspaces / 'archive' / (identity + '.tar.gz')
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b'old retire archive')
        saved = self.registry()
        saved[identity]['retire_failed'] = dict(at=OLD, error='Git refused', archive=str(stale))
        save_bindings(self.workspaces / 'worktrees.json', saved)
        self.assertEqual(self.retire(), [identity])
        self.assertFalse(stale.exists())

    def test_a_failed_checkout_of_a_discarded_branch_leaves_no_partial_checkout(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        self.assertEqual(self.retire(), [identity])
        real = work_workspace.git

        def interrupted(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'add'):
                Path(arguments[-2]).mkdir(parents=True)
                (Path(arguments[-2]) / 'partial.txt').write_text('partial')
                raise subprocess.TimeoutExpired('git', 600)
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=interrupted), self.assertRaises(ValueError):
            acquire(self.workspaces, outcome, str(self.repo))
        self.assertFalse(path.exists())
        # The next turn checks out the branch again.
        restored = acquire(self.workspaces, outcome, str(self.repo))
        self.assertTrue(Path(restored['path']).is_dir())

    def test_a_refused_worktree_of_a_recently_idle_outcome_stays(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat().replace('+00:00', 'Z')
        _, identity, path, _ = self.outcome(updated_at=recent)
        git(path, 'checkout', '-q', '-b', 'switched-branch')
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 2 * 24 * 60 * 60):
            self.assertEqual(self.retire(), [])
        self.assertTrue(path.exists())
        self.assertNotIn('retired', self.registry()[identity])

    def test_a_low_disk_discards_a_refused_idle_worktree_at_once(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat().replace('+00:00', 'Z')
        _, identity, path, _ = self.outcome(updated_at=recent)
        git(path, 'checkout', '-q', '-b', 'switched-branch')
        with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            self.assertEqual(self.retire(), [identity])
        self.assertFalse(path.exists())
        # A low disk also runs the pass more often.
        self.assertLessEqual(self.service._next_worktree_retirement - time.time(), retirement.LOW_DISK_INTERVAL_SECONDS)

    def ago(self, **delta):
        return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat().replace('+00:00', 'Z')

    def test_a_low_disk_retires_the_worktree_of_an_outcome_idle_for_minutes(self):
        _, identity, path, _ = self.outcome(updated_at=self.ago(minutes=20))
        # With enough disk space, the outcome must idle for hours.
        self.assertEqual(self.retire(), [])
        self.assertTrue(path.exists())
        with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            self.assertEqual(self.retire(), [identity])
        self.assertFalse(path.exists())

    def test_a_low_disk_keeps_the_worktree_of_an_outcome_that_changed_a_minute_ago(self):
        _, identity, path, _ = self.outcome(updated_at=self.ago(minutes=1))
        with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            self.assertEqual(self.retire(), [])
        self.assertTrue(path.exists())

    def test_a_low_disk_clears_the_scratch_files_of_an_outcome_only_after_two_idle_hours(self):
        young, _, _, young_task = self.outcome(updated_at=self.ago(minutes=30))
        old, _, _, old_task = self.outcome(updated_at=self.ago(hours=3))
        for outcome, task in ((young, young_task), (old, old_task)):
            self.pull_request(task, 'merged')
            (self.workspaces / outcome).mkdir()
            (self.workspaces / outcome / 'notes.txt').write_text('scratch\n')
        with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            self.retire()
        # Scratch files have no archive, so they wait longer than a worktree.
        self.assertTrue((self.workspaces / young / 'notes.txt').exists())
        self.assertFalse((self.workspaces / old / 'notes.txt').exists())

    def scratch_evidence(self, outcome):
        files = {'STATE.md': b'# Retained acceptance state\nReuse repair-evidence and repair-review.\n',
                 'repair-evidence/REPORT.md': b'# Exact-address repair\n6 regressions fail before and pass after.\n',
                 'repair-evidence/repair-review/result.json': b'{"findings": [], "exit": 0}\n'}
        for name, data in files.items():
            (self.workspaces / outcome / name).parent.mkdir(parents=True, exist_ok=True)
            (self.workspaces / outcome / name).write_bytes(data)
        return files

    def native_board(self, cards, fresh=None):
        """A native board whose snapshot call records whether the service lock is held, as the adapter would see it."""
        calls = []
        def call(request):
            calls.append((request, self.service.lock._is_owned()))
            if isinstance(fresh, BaseException):
                raise fresh
            board.tasks = dict(fresh if fresh is not None else cards)
            return {'tasks': list(board.tasks.values())}
        board = SimpleNamespace(tasks=dict(cards), call=call)
        return board, calls

    def test_a_scheduled_native_owner_keeps_its_scratch_evidence_after_a_merge_on_a_low_disk(self):
        # The observed ea5 shape: PR merged, Coconut task completed, native card scheduled with a pending capacity
        # checkpoint, free space below LOW_DISK_BYTES, and no update for over two hours.
        outcome, identity, path, task = self.outcome(updated_at=self.ago(hours=3))
        self.pull_request(task, 'merged')
        files = self.scratch_evidence(outcome)
        (path / 'tmp' / 'repair-kit').mkdir(parents=True)
        (path / 'tmp' / 'repair-kit' / 'run.sh').write_text('rebuildable\n')
        board, calls = self.native_board({task['id']: dict(id='t_81201bc4', external_id=task['id'], status='scheduled')})
        with patch.object(self.service, 'kanban', board), patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            # Source retirement is unchanged: the merged worktree goes, and its archive keeps the branch and patches.
            self.assertEqual(self.retire(), [identity])
        self.assertFalse(path.exists())
        for name, data in files.items():
            self.assertEqual((self.workspaces / outcome / name).read_bytes(), data)
        # The board was read again outside the service lock, before the scratch check.
        self.assertEqual(calls, [({'op': 'snapshot'}, False)])

    def test_unfinished_native_cards_keep_scratch_files_but_finished_ones_release_them(self):
        for status, kept in (('scheduled', True), ('blocked', True), ('review', True), ('triage', True), ('todo', True),
                             ('done', False), ('archived', False)):
            with self.subTest(status=status):
                outcome, _, _, task = self.outcome()
                self.pull_request(task, 'merged')
                files = self.scratch_evidence(outcome)
                board, _ = self.native_board({task['id']: dict(id='t_' + status, external_id=task['id'], status=status)})
                with patch.object(self.service, 'kanban', board):
                    self.retire()
                if kept:
                    for name, data in files.items():
                        self.assertEqual((self.workspaces / outcome / name).read_bytes(), data)
                else:
                    # A finished outcome keeps an empty folder for a later turn.
                    self.assertEqual(list((self.workspaces / outcome).iterdir()), [])

    def test_a_fresh_native_snapshot_decides_and_a_failed_read_keeps_the_scratch_files(self):
        outcome, _, _, task = self.outcome()
        self.pull_request(task, 'merged')
        files = self.scratch_evidence(outcome)
        done = {task['id']: dict(id='t_owner', external_id=task['id'], status='done')}
        reopened = {task['id']: dict(id='t_owner', external_id=task['id'], status='scheduled')}
        for label, fresh in (('reopened since the last snapshot', reopened),
                             ('board unreadable', RuntimeError('The Hermes Kanban adapter did not respond.'))):
            with self.subTest(label):
                board, calls = self.native_board(done, fresh=fresh)
                with patch.object(self.service, 'kanban', board):
                    self.retire()
                self.assertEqual(calls, [({'op': 'snapshot'}, False)])
                for name, data in files.items():
                    self.assertEqual((self.workspaces / outcome / name).read_bytes(), data)
        board, _ = self.native_board(done)
        with patch.object(self.service, 'kanban', board):
            self.retire()
        self.assertEqual(list((self.workspaces / outcome).iterdir()), [])

    def verification_evidence(self, path):
        """Write ignored verification evidence and a nested review checkout into the worktree, as an install check does."""
        with (self.repo / '.git' / 'info' / 'exclude').open('a') as exclude:
            exclude.write('.runtime/\n')
        files = {'.runtime/verifiers/accept.py': b'print("accept")\n',
                 '.runtime/preinstall/raw-before.json': b'{"runtime": "before"}\n'}
        for name, data in files.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_bytes(data)
        review = path / '.runtime' / 'review'
        git(path, 'worktree', 'add', '-q', '-b', 'review-' + path.name[:8], str(review))
        (review / 'tracked.txt').write_text('review note\n')
        return files, review

    def assert_evidence_kept(self, path, identity, files, review):
        self.assertTrue(path.exists())
        self.assertNotIn('retired', self.registry()[identity])
        for name, data in files.items():
            self.assertEqual((path / name).read_bytes(), data)
        self.assertEqual((review / 'tracked.txt').read_text(), 'review note\n')

    def test_a_merge_keeps_the_ignored_evidence_of_a_scheduled_native_owner_on_a_low_disk(self):
        # The observed 0b3 shape: PR merged, native card scheduled to accept the install, free space below LOW_DISK_BYTES,
        # and ignored evidence that retirement cannot archive exactly. Three idle hours also make the outcome stale.
        outcome, identity, path, task = self.outcome(updated_at=self.ago(hours=3))
        self.pull_request(task, 'merged')
        files, review = self.verification_evidence(path)
        (path / 'node_modules').mkdir()
        (path / 'node_modules' / 'package.js').write_bytes(b'x' * 64)
        board, calls = self.native_board({task['id']: dict(id='t_4ae01508', external_id=task['id'], status='scheduled')})
        with patch.object(self.service, 'kanban', board), patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18), \
                patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 10):
            self.assertEqual(self.retire(), [])
        self.assert_evidence_kept(path, identity, files, review)
        self.assertFalse((self.workspaces / 'archive' / 'done' / (identity + '.tar.gz')).exists())
        # The build folders still go.
        self.assertFalse((path / 'node_modules').exists())
        self.assertEqual(calls, [({'op': 'snapshot'}, False)])

    def test_a_merge_retires_the_worktree_of_a_scheduled_native_owner_with_its_ignored_evidence(self):
        outcome, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        files = {'.env': b'SECRET=1\n', 'build/raw-before.json': b'{"runtime": "before"}\n'}
        for name, data in files.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_bytes(data)
        board, _ = self.native_board({task['id']: dict(id='t_owner', external_id=task['id'], status='scheduled')})
        with patch.object(self.service, 'kanban', board):
            self.assertEqual(self.retire(), [identity])
        # Retirement archived the ignored files exactly. The resume puts them back.
        self.assertNotIn('done', self.registry()[identity]['retired'])
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        for name, data in files.items():
            self.assertEqual((restored / name).read_bytes(), data)

    def test_an_unfinished_native_owner_keeps_a_refused_worktree_after_long_idle_or_on_a_low_disk(self):
        for status in ('scheduled', 'blocked', 'review', 'triage', 'todo'):
            for label, rules in (('long idle', dict(DISCARD_IDLE_SECONDS=24 * 60 * 60)),
                                 ('low disk', dict(LOW_DISK_BYTES=10 ** 18))):
                with self.subTest(status=status, rules=label):
                    _, identity, path, task = self.outcome()
                    git(path, 'checkout', '-q', '-b', 'switched-' + path.name[:8])
                    files, review = self.verification_evidence(path)
                    board, _ = self.native_board({task['id']: dict(id='t_' + status, external_id=task['id'], status=status)})
                    with patch.object(self.service, 'kanban', board), patch.multiple(retirement, **rules):
                        # The board holds only this card. The outcomes of earlier subtests count as done now.
                        self.assertNotIn(identity, self.retire())
                    self.assert_evidence_kept(path, identity, files, review)

    def test_a_done_or_archived_native_owner_still_loses_its_merged_or_refused_worktree(self):
        for status in ('done', 'archived'):
            for label in ('merged', 'long idle'):
                with self.subTest(status=status, path=label):
                    _, identity, path, task = self.outcome()
                    if label == 'merged':
                        self.pull_request(task, 'merged')
                    else:
                        git(path, 'checkout', '-q', '-b', 'switched-' + path.name[:8])
                    _, review = self.verification_evidence(path)
                    board, _ = self.native_board({task['id']: dict(id='t_' + status, external_id=task['id'], status=status)})
                    with patch.object(self.service, 'kanban', board), \
                            patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60), \
                            patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 10):
                        self.assertIn(identity, self.retire())
                    self.assertFalse(path.exists())
                    self.assertFalse(review.exists())
                    self.assertTrue(self.registry()[identity]['retired']['done'])

    def test_live_unknown_or_unreadable_native_states_keep_a_merged_worktree(self):
        done = lambda task: {task['id']: dict(id='t_owner', external_id=task['id'], status='done')}
        cases = [(status, lambda task, status=status: (
                     {task['id']: dict(id='t_owner', external_id=task['id'], status=status)}, None))
                 for status in ('running', 'ready', 'claiming', 'unknown-state')]
        cases.append(('reopened since the last snapshot', lambda task: (
            done(task), {task['id']: dict(id='t_owner', external_id=task['id'], status='scheduled')})))
        cases.append(('board unreadable', lambda task: (done(task), RuntimeError('The Hermes Kanban adapter did not respond.'))))
        for label, cards in cases:
            with self.subTest(label):
                _, identity, path, task = self.outcome()
                self.pull_request(task, 'merged')
                files, review = self.verification_evidence(path)
                board, _ = self.native_board(*cards(task))
                with patch.object(self.service, 'kanban', board), patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18), \
                        patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 10):
                    # The board holds only this card. The outcomes of earlier subtests count as done now.
                    self.assertNotIn(identity, self.retire())
                self.assert_evidence_kept(path, identity, files, review)

    def test_a_native_card_that_reopens_during_the_discard_keeps_the_worktree(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        files, review = self.verification_evidence(path)
        board, _ = self.native_board({task['id']: dict(id='t_owner', external_id=task['id'], status='done')})

        def reopen(path):
            # The worktree checks are done. The card reopens before the registry lock.
            board.tasks = {task['id']: dict(id='t_owner', external_id=task['id'], status='scheduled')}
            return False
        with patch.object(self.service, 'kanban', board), patch.object(work_workspace, 'in_use', side_effect=reopen), \
                patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 10), \
                self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            self.assertEqual(self.retire(), [])
        self.assertTrue(any('resumed' in line for line in logs.output))
        self.assert_evidence_kept(path, identity, files, review)

    def test_a_low_disk_keeps_a_stale_queued_worktree_only_while_its_own_claim_is_in_progress(self):
        claimed, claimed_identity, claimed_path, _ = self.outcome(status='queued', updated_at=self.ago(hours=13))
        other, other_identity, other_path, _ = self.outcome(status='queued', updated_at=self.ago(hours=13))
        self.service._kanban_claiming = 1
        self.service._kanban_claim_outcomes = {claimed}
        with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            # A claim for another outcome does not delay the space recovery.
            self.assertEqual(self.retire(), [other_identity])
        self.assertTrue(claimed_path.exists())
        self.service._kanban_claiming = 0
        self.service._kanban_claim_outcomes = set()
        with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            self.assertEqual(self.retire(), [claimed_identity])

    def test_a_low_disk_discards_a_refused_worktree_only_after_two_idle_hours(self):
        _, young, young_path, _ = self.outcome(updated_at=self.ago(hours=1))
        _, old, old_path, _ = self.outcome(updated_at=self.ago(hours=3))
        for path in (young_path, old_path):
            git(path, 'checkout', '-q', '-b', 'switched-' + path.name[:8])
        with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            self.assertEqual(self.retire(), [old])
        self.assertTrue(young_path.exists())
        self.assertFalse(old_path.exists())

    def test_a_low_disk_ignores_a_task_that_waited_long_in_the_queue_but_not_a_recent_or_running_one(self):
        stale, stale_identity, stale_path, _ = self.outcome(status='queued', updated_at=self.ago(hours=13))
        recent, _, recent_path, _ = self.outcome(status='queued', updated_at=self.ago(hours=1))
        running, _, running_path, _ = self.outcome(status='working', updated_at=self.ago(hours=13))
        # A queued task of a normal disk keeps its worktree, however long it waits.
        self.assertEqual(self.retire(), [])
        with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            self.assertEqual(self.retire(), [stale_identity])
        self.assertFalse(stale_path.exists())
        self.assertTrue(recent_path.exists() and running_path.exists())

    def test_a_locked_worktree_stays_even_when_long_idle(self):
        _, identity, path, _ = self.outcome()
        git(path, 'checkout', '-q', '-b', 'switched-branch')
        git(self.repo, 'worktree', 'lock', str(path))
        with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
            self.assertEqual(self.retire(), [])
        self.assertTrue(path.exists())

    def test_idle_scratch_workspaces_lose_their_files_and_unknown_old_ones_go(self):
        idle, _, _, task = self.outcome()
        self.pull_request(task, 'merged')
        active, _, _, _ = self.outcome(status='working', updated_at=RECENT)
        folders = {name: self.workspaces / name for name in (idle, active, self.bot['id'])}
        for folder in folders.values():
            (folder / 'clone' / '.git').mkdir(parents=True)
            (folder / 'report.md').write_text('evidence')
        unknown_old, unknown_new = self.workspaces / str(uuid4()), self.workspaces / str(uuid4())
        for folder in (unknown_old, unknown_new):
            (folder / 'build').mkdir(parents=True)
        os.utime(unknown_old, (time.time() - 3 * 24 * 60 * 60,) * 2)
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60):
            self.retire()
        # The done outcome keeps an empty folder for a later turn. The active outcome and the bot keep their files.
        self.assertEqual(list(folders[idle].iterdir()), [])
        self.assertTrue((folders[active] / 'report.md').exists())
        self.assertTrue((folders[self.bot['id']] / 'report.md').exists())
        self.assertFalse(unknown_old.exists())
        self.assertTrue(unknown_new.exists())

    def test_a_named_discard_archive_survives_the_size_limit_but_not_the_age_limit(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        (path / 'draft.txt').write_text('keep\n')
        self.assertEqual(self.retire(), [identity])
        archive = Path(self.registry()[identity]['retired']['archive'])
        with patch.object(work_workspace, 'NESTED_ARCHIVE_LIMIT_BYTES', 0):
            os.utime(archive, (time.time() - 3 * 86400,) * 2)
            work_workspace.sweep_temporary(self.workspaces)
        self.assertTrue(archive.exists())
        os.utime(archive, (time.time() - (work_workspace.NESTED_ARCHIVE_DAYS + 1) * 86400,) * 2)
        work_workspace.sweep_temporary(self.workspaces)
        self.assertFalse(archive.exists())

    def test_a_discarded_branch_that_another_worktree_holds_gets_a_new_branch_on_resume(self):
        outcome, identity, path, _ = self.outcome()
        # The bot leaves the worktree on main, which the main checkout holds too.
        main = git(self.repo, 'branch', '--show-current')
        git(path, 'checkout', '-q', '--detach')
        git(path, 'checkout', '-q', '-B', 'temp-holder')
        with patch.object(retirement, 'DISCARD_IDLE_SECONDS', 24 * 60 * 60):
            git(path, 'checkout', '-q', '-b', 'other-branch')
            self.assertEqual(self.retire(), [identity])
        saved = self.registry()
        saved[identity]['branch'] = main
        save_bindings(self.workspaces / 'worktrees.json', saved)
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertTrue(restored.is_dir())
        self.assertNotEqual(git(restored, 'branch', '--show-current'), main)

    def test_the_sweep_trims_old_discard_archives(self):
        done = self.workspaces / 'archive' / 'done'
        done.mkdir(parents=True)
        old, new = done / 'old.tar.gz', done / 'new.tar.gz'
        for archive in (old, new):
            archive.write_bytes(b'x')
        os.utime(old, (time.time() - (work_workspace.NESTED_ARCHIVE_DAYS + 1) * 86400,) * 2)
        work_workspace.sweep_temporary(self.workspaces)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_an_open_pull_request_keeps_the_worktree_of_its_outcome(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        self.pull_request(task, 'open')
        git(path, 'checkout', '-q', '-b', 'other-branch')
        self.assertEqual(self.retire(), [])
        self.assertTrue(path.is_dir())
        self.assertNotIn('retired', self.registry()[identity])

    def test_a_missing_checkout_closes_its_binding_instead_of_failing_every_hour(self):
        outcome, identity, path, _ = self.outcome()
        shutil.rmtree(path)
        with self.assertNoLogs('coconut.worktree_retirement', 'WARNING'):
            self.assertEqual(self.retire(), [identity])
        self.assertTrue(self.registry()[identity]['retired']['done'])
        self.assertEqual(self.retire(), [])
        restored = acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(git(restored['path'], 'branch', '--show-current'), 'wt/' + identity)

    def test_an_active_outcome_with_a_merged_pull_request_keeps_its_worktree(self):
        _, identity, path, task = self.outcome(status='working')
        self.pull_request(task, 'merged')
        self.assertEqual(self.retire(), [])
        self.assertTrue(path.is_dir())
        self.assertNotIn('retired', self.registry()[identity])

    def test_a_branch_renamed_in_the_last_turn_still_retires(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'draft.txt').write_text('keep\n')
        # coconut_workspace asks bots to rename the branch, and a bot often does that just before it pushes.
        git(path, 'branch', '-m', 'gianfranco/published-name')
        self.assertEqual(self.retire(), [identity])
        binding = self.registry()[identity]
        self.assertEqual(binding['branch'], 'gianfranco/published-name')
        self.assertIn('retired', binding)
        restored = acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(git(restored['path'], 'branch', '--show-current'), 'gianfranco/published-name')
        self.assertEqual((Path(restored['path']) / 'draft.txt').read_text(), 'keep\n')

    def test_a_branch_switch_or_detached_checkout_is_not_retired(self):
        # Only a rename keeps the work item. Retirement must refuse a switch before it archives or removes files.
        for name, change in (('branch switch', lambda path: git(path, 'checkout', '-q', '-b', 'other-branch')),
                             ('detached checkout', lambda path: git(path, 'checkout', '-q', '--detach'))):
            with self.subTest(name):
                _, identity, path, _ = self.outcome()
                (path / 'draft.txt').write_text('keep\n')
                change(path)
                self.assertEqual(self.retire(), [])
                self.assertNotIn('retired', self.registry()[identity])
                self.assertEqual((path / 'draft.txt').read_text(), 'keep\n')

    def test_finished_idle_worktree_is_archived_removed_and_restored_with_its_changes(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'committed.txt').write_text('in a commit\n')
        git(path, 'add', 'committed.txt')
        self.commit(path, 'Work')
        head = git(path, 'rev-parse', 'HEAD')
        (path / 'tracked.txt').write_text('one\nstaged\n')
        git(path, 'add', 'tracked.txt')
        (path / 'tracked.txt').write_text('one\nstaged\nunstaged\n')
        (path / 'logo.bin').write_bytes(bytes(reversed(range(256))))
        (path / 'obsolete.txt').unlink()
        (path / 'notes').mkdir()
        (path / 'notes' / 'draft.md').write_text('untracked draft\n')
        (path / 'build').mkdir()
        (path / 'build' / 'output.bin').write_bytes(b'x' * 4096)
        (path / '.env').write_text('SECRET=local\n')
        for rebuildable in ('node_modules/pkg/index.js', 'tmp/cache/file.bin', 'build/node_modules/x.js'):
            (path / rebuildable).parent.mkdir(parents=True, exist_ok=True)
            (path / rebuildable).write_text('rebuildable\n')

        self.assertEqual(self.retire(), [identity])

        self.assertFalse(path.exists())
        self.assertEqual(git(self.repo, 'rev-parse', '--verify', 'refs/heads/wt/' + identity), head)
        binding = self.registry()[identity]
        archive = self.workspaces / 'archive' / (identity + '.tar.gz')
        self.assertEqual(binding['retired']['archive'], str(archive))
        self.assertEqual(binding['retired']['head'], head)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive) as tar:
            self.assertEqual(sorted(tar.getnames()), ['ignored/.env', 'ignored/build/output.bin', 'retirement.json',
                                                      'staged.patch', 'unstaged.patch', 'untracked/notes/draft.md'])
            metadata = json.loads(tar.extractfile('retirement.json').read())
        self.assertEqual((metadata['branch'], metadata['head'], metadata['outcome']), ('wt/' + identity, head, outcome))

        # A path hint names the saved path. It does not restore the worktree.
        self.assertEqual(preferred(self.workspaces, outcome, verify=False), path)
        self.assertFalse(path.exists())

        restored = acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(restored['path'], str(path))
        self.assertNotIn('retired', restored)
        self.assertNotIn('retired', self.registry()[identity])
        self.assertEqual(git(path, 'rev-parse', 'HEAD'), head)
        self.assertEqual((path / 'tracked.txt').read_text(), 'one\nstaged\nunstaged\n')
        self.assertEqual((path / 'logo.bin').read_bytes(), bytes(reversed(range(256))))
        self.assertFalse((path / 'obsolete.txt').exists())
        self.assertEqual((path / 'notes' / 'draft.md').read_text(), 'untracked draft\n')
        self.assertEqual((path / 'build' / 'output.bin').read_bytes(), b'x' * 4096)
        self.assertEqual((path / '.env').read_text(), 'SECRET=local\n')
        for rebuildable in ('node_modules', 'tmp', 'build/node_modules'):
            self.assertFalse((path / rebuildable).exists())
        self.assertEqual(git(path, 'diff', '--cached', '--name-only'), 'tracked.txt')
        # The worktree holds every archived change again, so Coconut deletes the archive.
        self.assertFalse(archive.exists())
        self.assertEqual(preferred(self.workspaces, outcome), path)

    def test_preferred_restores_into_an_empty_directory_from_a_native_claim(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'draft.txt').write_text('keep\n')
        self.assertEqual(self.retire(), [identity])
        path.mkdir()
        self.assertEqual(preferred(self.workspaces, outcome), path)
        self.assertEqual((path / 'draft.txt').read_text(), 'keep\n')
        self.assertNotIn('retired', self.registry()[identity])

    def test_active_recent_running_locked_and_claimed_native_outcomes_keep_their_worktrees(self):
        kept = [self.outcome(status=status) for status in retirement.ACTIVE_STATUSES]
        kept.append(self.outcome(updated_at=RECENT))
        running = self.outcome()
        self.service.running[running[3]['id']] = SimpleNamespace()
        self.addCleanup(self.service.running.pop, running[3]['id'])
        kept.append(running)
        locked = self.outcome()
        git(self.repo, 'worktree', 'lock', str(locked[2]))
        kept.append(locked)
        # An unknown native state keeps the worktree too.
        claimed = [self.outcome(), self.outcome(), self.outcome()]
        kept.extend(claimed)
        for _, _, path, _ in kept:
            (path / 'draft.txt').write_text('in progress\n')
        native = {claimed[0][3]['id']: dict(id='n1', status='running'), claimed[1][3]['id']: dict(id='n2', status='ready'),
                  claimed[2][3]['id']: dict(id='n3', status='claiming')}
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks=native)):
            self.assertEqual(self.retire(), [])
        for _, identity, path, _ in kept:
            self.assertEqual((path / 'draft.txt').read_text(), 'in progress\n')
            self.assertNotIn('retired', self.registry()[identity])
        self.assertFalse((self.workspaces / 'archive' / (locked[1] + '.tar.gz')).exists())

    def test_active_statuses_are_the_service_statuses_that_do_not_wait_for_a_person(self):
        # A new active status must keep its worktree. This test fails until retirement lists it.
        self.assertEqual(set(retirement.ACTIVE_STATUSES), Service.ACTIVE - {'needs_you', 'paused'})

    def test_idle_outcomes_that_wait_or_failed_retire_and_return_with_their_changes(self):
        idle = [self.outcome(status=status) for status in ('failed', 'paused', 'needs_you')]
        self.service._put('decisions', dict(id=str(uuid4()), task_id=idle[2][3]['id'], status='pending'))
        blocked = [self.outcome(), self.outcome()]
        idle.extend(blocked)
        for _, _, path, _ in idle:
            (path / 'draft.txt').write_text('in progress\n')
        native = {blocked[0][3]['id']: dict(id='n1', status='blocked'), blocked[1][3]['id']: dict(id='n2', status='triage')}
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks=native)):
            self.assertEqual(sorted(self.retire()), sorted(identity for _, identity, _, _ in idle))
        for outcome, identity, path, _ in idle:
            self.assertFalse(path.exists())
            self.assertEqual(preferred(self.workspaces, outcome), path)
            self.assertEqual((path / 'draft.txt').read_text(), 'in progress\n')
            self.assertNotIn('retired', self.registry()[identity])

    def test_the_job_runs_at_most_once_an_hour(self):
        _, identity, path, _ = self.outcome()
        self.service._next_worktree_retirement = 0
        now = datetime.now(timezone.utc).timestamp()
        with patch('coconut.work_workspace.retire', return_value=None) as retire:
            self.service.retire_finished_worktrees(now)
            self.service.retire_finished_worktrees(now + 60)
            self.assertEqual(retire.call_count, 1)
            self.service.retire_finished_worktrees(now + 3601)
            self.assertEqual(retire.call_count, 2)
        self.assertTrue(path.exists())

    def test_an_outcome_that_resumes_during_retirement_keeps_its_worktree(self):
        _, identity, path, task = self.outcome()
        original = work_workspace.write_archive
        def resume(*arguments):
            original(*arguments)
            with self.service.transaction():
                self.service._put('tasks', dict(task, status='queued', updated_at=RECENT))
        with patch('coconut.work_workspace.write_archive', side_effect=resume):
            self.assertEqual(self.retire(), [])
        self.assertTrue(path.is_dir())
        self.assertNotIn('retired', self.registry()[identity])
        self.assertEqual([p for p in (self.workspaces / 'archive').iterdir()], [])

    def test_failed_restore_keeps_the_archive_reports_it_and_does_not_retry(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'tracked.txt').write_text('changed\n')
        self.assertEqual(self.retire(), [identity])
        archive = Path(self.registry()[identity]['retired']['archive'])
        original = archive.read_bytes()
        with tarfile.open(archive) as tar:
            metadata = tar.extractfile('retirement.json').read()
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data in (('retirement.json', metadata), ('staged.patch', b''), ('unstaged.patch', b'not a patch\n')):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with self.assertRaisesRegex(ValueError, 'could not restore it') as raised:
            acquire(self.workspaces, outcome, str(self.repo))
        self.assertIn(str(archive), str(raised.exception))
        self.assertTrue(archive.exists())
        self.assertFalse(path.exists())
        self.assertIn('restore_failed', self.registry()[identity]['retired'])
        self.assertNotIn(str(path), git(self.repo, 'worktree', 'list', '--porcelain'))

        # Later work continues without the worktree. Coconut does not try the restore again.
        archive.write_bytes(original)
        self.assertEqual(preferred(self.workspaces, outcome), self.workspaces / outcome)
        self.assertEqual(preferred(self.workspaces, outcome, verify=False), self.workspaces / outcome)
        with self.assertRaisesRegex(ValueError, 'Ask the operator'):
            acquire(self.workspaces, outcome, str(self.repo))
        self.assertFalse(path.exists())

        # An operator removes the mark to try again.
        saved = self.registry()
        saved[identity]['retired'].pop('restore_failed')
        save_bindings(self.workspaces / 'worktrees.json', saved)
        self.assertEqual(acquire(self.workspaces, outcome, str(self.repo))['path'], str(path))
        self.assertEqual((path / 'tracked.txt').read_text(), 'changed\n')

    def test_a_moved_branch_marks_the_restore_failed_without_stopping_work(self):
        outcome, identity, path, _ = self.outcome()
        self.assertEqual(self.retire(), [identity])
        self.commit_on_branch('wt/' + identity)
        self.assertEqual(preferred(self.workspaces, outcome), self.workspaces / outcome)
        self.assertIn('branch moved', self.registry()[identity]['retired']['restore_failed']['error'])
        self.assertFalse(path.exists())

    def test_staged_new_file_survives_retire_and_restore(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'added.txt').write_text('staged new file\n')
        git(path, 'add', 'added.txt')
        (path / 'added.txt').write_text('staged new file\nand an unstaged line\n')
        self.assertEqual(self.retire(), [identity])
        acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(git(path, 'diff', '--cached', '--name-status'), 'A\tadded.txt')
        self.assertEqual(git(path, 'show', ':added.txt'), 'staged new file')
        self.assertEqual((path / 'added.txt').read_text(), 'staged new file\nand an unstaged line\n')

    def test_untracked_links_that_point_outside_return_as_links(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'absolute').symlink_to('/etc/hosts')
        (path / 'links').mkdir()
        (path / 'links' / 'escaping').symlink_to('../../../outside')
        (path / 'links' / 'inside').symlink_to('../tracked.txt')
        self.assertEqual(self.retire(), [identity])
        acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(os.readlink(path / 'absolute'), '/etc/hosts')
        self.assertEqual(os.readlink(path / 'links' / 'escaping'), '../../../outside')
        self.assertEqual(os.readlink(path / 'links' / 'inside'), '../tracked.txt')

    def refused_with_build_folders(self):
        """Return a worktree that retirement refuses, with build folders and files that are not rebuildable."""
        _, identity, path, _ = self.outcome()
        (path / 'draft.txt').write_text('keep\n')
        for name in ('tmp/cache/big.bin', 'node_modules/pkg/index.js', 'build/fixture.db'):
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_bytes(b'x' * 1024)
        git(path, 'checkout', '-q', '-b', 'other-branch')
        return identity, path

    def test_a_refused_idle_worktree_loses_only_its_build_folders(self):
        identity, path = self.refused_with_build_folders()
        self.assertEqual(self.retire(), [])
        self.assertNotIn('retired', self.registry()[identity])
        # A build creates tmp and node_modules again. Other ignored and untracked files stay.
        self.assertFalse((path / 'tmp').exists())
        self.assertFalse((path / 'node_modules').exists())
        self.assertEqual((path / 'build/fixture.db').stat().st_size, 1024)
        self.assertEqual((path / 'draft.txt').read_text(), 'keep\n')
        self.assertEqual(git(path, 'branch', '--show-current'), 'other-branch')

    def test_a_folder_that_git_lists_with_its_children_is_deleted_once(self):
        identity, path = self.refused_with_build_folders()
        # Git lists .cache/ and .cache/pkg/ when only the folder's own .gitignore ignores its files.
        (path / '.cache/pkg').mkdir(parents=True)
        (path / '.cache/pkg/data.bin').write_bytes(b'x')
        (path / '.cache/.gitignore').write_text('*\n')
        listing = work_workspace.git_output(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z')
        self.assertIn(b'.cache/pkg/\0', listing)
        self.assertEqual(self.retire(), [])
        self.assertFalse((path / '.cache').exists())
        self.assertFalse((path / 'tmp').exists())

    def test_a_folder_that_cannot_move_does_not_keep_the_others(self):
        identity, path = self.refused_with_build_folders()
        rename = os.rename

        def refuse_node_modules(source, target):
            if Path(source).name == 'node_modules':
                raise PermissionError('Operation not permitted')
            return rename(source, target)
        with patch('coconut.work_workspace.os.rename', side_effect=refuse_node_modules):
            self.assertEqual(self.retire(), [])
        self.assertTrue((path / 'node_modules/pkg/index.js').exists())
        self.assertFalse((path / 'tmp').exists())
        # The moved folder is deleted after the lock, and nothing stays in the trash.
        self.assertEqual(list((self.workspaces / work_workspace.TRASH).iterdir()), [])

    def test_a_folder_on_another_disk_stays_and_is_logged(self):
        identity, path = self.refused_with_build_folders()

        def other_disk(source, target):
            raise OSError(errno.EXDEV, 'Cross-device link')
        with patch('coconut.work_workspace.os.rename', side_effect=other_disk), \
                self.assertLogs('coconut.work_workspace', 'WARNING') as logs:
            self.assertEqual(self.retire(), [])
        # A deletion in place would hold the registry lock for minutes.
        self.assertTrue((path / 'tmp/cache/big.bin').exists())
        self.assertTrue(any('another disk' in line for line in logs.output))

    def test_an_unreadable_build_folder_stays(self):
        identity, path = self.refused_with_build_folders()
        hidden = path / 'tmp/probe'
        (hidden / 'clone/.git').mkdir(parents=True)
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o700)
        self.assertEqual(self.retire(), [])
        hidden.chmod(0o700)
        self.assertTrue((hidden / 'clone/.git').is_dir())
        self.assertFalse((path / 'node_modules').exists())

    def test_a_link_named_like_a_build_folder_is_not_followed(self):
        identity, path = self.refused_with_build_folders()
        outside = self.root / 'outside-cache'
        outside.mkdir()
        (outside / 'keep.bin').write_bytes(b'x')
        shutil.rmtree(path / 'node_modules')
        (path / 'node_modules').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.retire(), [])
        self.assertTrue((outside / 'keep.bin').exists())
        self.assertTrue((path / 'node_modules').is_symlink())
        self.assertFalse((path / 'tmp').exists())

    def test_the_sweep_empties_a_trash_that_a_stop_left(self):
        left = self.workspaces / work_workspace.TRASH / 'stopped'
        left.mkdir(parents=True)
        (left / 'big.bin').write_bytes(b'x')
        work_workspace.sweep_temporary(self.workspaces)
        self.assertFalse(left.exists())

    def test_a_build_folder_that_holds_a_worktree_stays(self):
        identity, path = self.refused_with_build_folders()
        # coconut_workspace tells a bot to test another commit in a worktree under tmp.
        git(path, 'worktree', 'add', '-q', '-b', 'probe', str(path / 'tmp/probe'))
        (path / 'tmp/probe/work.txt').write_text('uncommitted\n')
        self.assertEqual(self.retire(), [])
        self.assertEqual((path / 'tmp/probe/work.txt').read_text(), 'uncommitted\n')
        self.assertTrue((path / 'tmp/cache/big.bin').exists())
        self.assertFalse((path / 'node_modules').exists())

    def test_a_worktree_of_another_repository_keeps_its_build_folders(self):
        identity, path = self.refused_with_build_folders()
        other = self.root / 'other'
        other.mkdir()
        git(other, 'init', '-q')
        (other / 'readme.txt').write_text('other\n')
        git(other, 'add', '.')
        self.commit(other, 'Other')
        # The saved path now holds a worktree of another repository.
        git(self.repo, 'worktree', 'remove', '--force', str(path))
        git(other, 'worktree', 'add', '-q', '-b', 'moved', str(path))
        (path / '.gitignore').write_text('/tmp\n')
        (path / 'tmp').mkdir()
        (path / 'tmp/data.bin').write_bytes(b'x')
        self.assertEqual(self.retire(), [])
        self.assertTrue((path / 'tmp/data.bin').exists())

    def test_a_locked_worktree_keeps_its_build_folders(self):
        identity, path = self.refused_with_build_folders()
        git(self.repo, 'worktree', 'lock', str(path))
        with self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            self.assertEqual(self.retire(), [])
        # retire() already reports the lock. The build-folder step stays silent.
        self.assertFalse(any('build folders' in line for line in logs.output))
        self.assertTrue((path / 'tmp/cache/big.bin').exists())
        self.assertTrue((path / 'node_modules/pkg/index.js').exists())

    def test_a_stop_during_retirement_skips_the_build_folders(self):
        identity, path = self.refused_with_build_folders()

        def refuse(*args, **kwargs):
            self.service.restart_pending = True
            raise ValueError('The saved worktree changed branches.')
        with patch('coconut.work_workspace.retire', side_effect=refuse):
            self.retire()
        self.service.restart_pending = False
        self.assertTrue((path / 'tmp/cache/big.bin').exists())

    def test_a_busy_service_skips_the_build_folders(self):
        identity, path = self.refused_with_build_folders()
        with patch('coconut.work_workspace.retire', side_effect=retirement.ServiceBusy(retirement.BUSY_MESSAGE)), \
                patch.object(retirement, 'BUSY_RETRY_SECONDS', 0), \
                patch('coconut.work_workspace.free_rebuildable') as free:
            self.retire()
        free.assert_not_called()
        self.assertTrue((path / 'tmp/cache/big.bin').exists())

    def test_a_build_folder_that_holds_another_repository_stays(self):
        identity, path = self.refused_with_build_folders()
        clone = path / 'tmp/clone'
        clone.mkdir()
        git(clone, 'init', '-q')
        (clone / 'work.txt').write_text('uncommitted\n')
        # A bare clone has no .git entry.
        (path / '.cache').mkdir()
        git(path / '.cache', 'init', '-q', '--bare', 'mirror')
        self.assertEqual(self.retire(), [])
        self.assertEqual((clone / 'work.txt').read_text(), 'uncommitted\n')
        self.assertTrue((path / '.cache/mirror/HEAD').is_file())
        self.assertFalse((path / 'node_modules').exists())

    def test_a_linked_trash_moves_nothing(self):
        identity, path = self.refused_with_build_folders()
        outside = self.root / 'outside-trash'
        outside.mkdir()
        (self.workspaces / work_workspace.TRASH).symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.retire(), [])
        self.assertTrue((path / 'tmp/cache/big.bin').exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_failed_deletion_is_logged_and_not_counted(self):
        identity, path = self.refused_with_build_folders()
        rmtree = shutil.rmtree

        def fail_in_trash(target, *args, **kwargs):
            # The folders move out of the worktree, and then their deletion in the trash fails.
            if work_workspace.TRASH in str(target):
                raise PermissionError('Operation not permitted')
            return rmtree(target, *args, **kwargs)
        with patch('coconut.work_workspace.shutil.rmtree', side_effect=fail_in_trash), \
                self.assertLogs('coconut.work_workspace', 'WARNING') as logs:
            freed = work_workspace.free_rebuildable(self.workspaces, identity, lambda wait: True)
        self.assertEqual(len(freed), 0)
        self.assertTrue(any('could not delete build folder' in line for line in logs.output))

    def test_a_used_or_resumed_worktree_keeps_its_build_folders(self):
        identity, path = self.refused_with_build_folders()
        with patch('coconut.work_workspace.in_use', return_value=True):
            self.assertEqual(self.retire(), [])
        self.assertTrue((path / 'tmp/cache/big.bin').exists())
        answers = iter([True, True, False])
        with patch('coconut.work_workspace.retire', return_value=None), \
                patch.object(self.service, '_finished_outcomes', side_effect=lambda *a, **k: (
                    {self.registry()[identity]['outcome']} if next(answers, False) else set())):
            self.retire()
        self.assertTrue((path / 'node_modules/pkg/index.js').exists())

    def test_large_ignored_files_keep_the_worktree(self):
        _, identity, path, _ = self.outcome()
        (path / 'build').mkdir()
        (path / 'build' / 'fixture.db').write_bytes(b'x' * 4096)
        (path / 'node_modules').mkdir()
        (path / 'node_modules' / 'big.js').write_bytes(b'x' * 8192)
        with patch('coconut.work_workspace.IGNORED_LIMIT_BYTES', 4096):
            self.assertEqual(self.retire(), [identity])
        # A large ignored file outside a folder cannot move aside (see held_folders).
        _, identity, path, _ = self.outcome()
        (path / '.env').write_bytes(b'x' * 4097)
        with patch('coconut.work_workspace.IGNORED_LIMIT_BYTES', 4096):
            self.assertEqual(self.retire(), [])
        self.assertEqual((path / '.env').stat().st_size, 4097)
        self.assertNotIn('retired', self.registry()[identity])

    # Bots keep QA evidence in ignored folders. It does not fit in the archive, so retirement moves it aside.
    def evidence_worktree(self):
        (self.repo / '.git' / 'info' / 'exclude').write_text('.private-evidence/\n')
        outcome, identity, path, task = self.outcome()
        media = path / '.private-evidence' / 'media'
        media.mkdir(parents=True)
        (media / 'capture.png').write_bytes(b'x' * 8192)
        (path / '.private-evidence' / 'RESULT.md').write_text('result\n')
        (path / '.env').write_text('SECRET=1\n')
        (path / 'tracked.txt').write_text('one\nchanged\n')
        return outcome, identity, path, task

    def test_large_ignored_evidence_moves_aside_and_comes_back(self):
        outcome, identity, path, _ = self.evidence_worktree()
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 4096):
            self.assertEqual(self.retire(), [identity])
        self.assertFalse(path.exists())
        held = self.registry()[identity]['retired']['held']
        self.assertEqual(held['names'], ['.private-evidence'])
        self.assertEqual((Path(held['folder']) / '.private-evidence' / 'media' / 'capture.png').stat().st_size, 8192)
        with tarfile.open(self.registry()[identity]['retired']['archive']) as tar:
            names = tar.getnames()
        self.assertIn('ignored/.env', names)
        self.assertFalse(any('.private-evidence' in name for name in names))
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertEqual((restored / '.private-evidence' / 'media' / 'capture.png').stat().st_size, 8192)
        self.assertEqual((restored / '.private-evidence' / 'RESULT.md').read_text(), 'result\n')
        self.assertEqual((restored / '.env').read_text(), 'SECRET=1\n')
        self.assertEqual((restored / 'tracked.txt').read_text(), 'one\nchanged\n')
        self.assertFalse(Path(held['folder']).exists())

    def test_a_failed_removal_puts_the_held_evidence_back(self):
        _, identity, path, _ = self.evidence_worktree()
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 4096), \
                patch('coconut.work_workspace.remove_worktree', side_effect=ValueError('refused')), \
                self.assertLogs('coconut.worktree_retirement', 'WARNING'):
            self.assertEqual(self.retire(), [])
        self.assertEqual((path / '.private-evidence' / 'media' / 'capture.png').stat().st_size, 8192)
        self.assertNotIn('retired', self.registry()[identity])
        self.assertFalse((self.workspaces / 'archive' / work_workspace.HELD).exists()
                         and any((self.workspaces / 'archive' / work_workspace.HELD).iterdir()))

    def test_a_stop_before_the_held_move_leaves_the_evidence_for_the_restore(self):
        outcome, identity, path, _ = self.evidence_worktree()
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 4096), \
                patch('coconut.work_workspace.hold', side_effect=SystemExit), self.assertRaises(SystemExit):
            self.retire()
        self.assertIn('held', self.registry()[identity]['retired'])
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertEqual(restored, path)
        self.assertEqual((restored / '.private-evidence' / 'RESULT.md').read_text(), 'result\n')

    def test_a_failed_return_of_held_evidence_keeps_every_folder(self):
        (self.repo / '.git' / 'info' / 'exclude').write_text('.private-evidence/\n.qa-evidence/\n')
        outcome, identity, path, _ = self.outcome()
        for name in ('.private-evidence', '.qa-evidence'):
            (path / name).mkdir()
            (path / name / 'capture.png').write_bytes(name.encode() * 1000)
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 4096):
            self.assertEqual(self.retire(), [identity])
        held = self.registry()[identity]['retired']['held']
        self.assertEqual(sorted(held['names']), ['.private-evidence', '.qa-evidence'])
        rename, calls = os.rename, []

        def second_fails(source, target):
            # The first folder moves back into the new checkout. The second rename fails.
            if str(source).startswith(held['folder']):
                calls.append(source)
                if len(calls) == 2:
                    raise PermissionError('Operation not permitted')
            return rename(source, target)
        with patch('coconut.work_workspace.os.rename', side_effect=second_fails), \
                contextlib.suppress(Exception), self.assertLogs(level='WARNING'):
            acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(len(calls), 2)
        # The failed restore deleted its checkout, but every evidence folder is back in its held place.
        self.assertFalse(path.exists())
        for name in ('.private-evidence', '.qa-evidence'):
            self.assertEqual((Path(held['folder']) / name / 'capture.png').read_bytes(), name.encode() * 1000)
        self.assertEqual(self.registry()[identity]['retired']['held'], held)

    def test_a_held_folder_that_cannot_go_back_keeps_the_checkout(self):
        _, identity, path, _ = self.evidence_worktree()
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 4096):
            self.assertEqual(self.retire(), [identity])
        retired = self.registry()[identity]['retired']
        failure = work_workspace.HeldMoveFailed('Operation not permitted', ['.private-evidence'])
        with patch('coconut.work_workspace.return_held', side_effect=failure), \
                patch('coconut.work_workspace.discard_checkout') as discard, self.assertRaises(work_workspace.HeldMoveFailed):
            work_workspace.unpack(dict(self.registry()[identity]), {})
        discard.assert_not_called()
        self.assertTrue((path / '.git').is_file())
        self.assertEqual(self.registry()[identity]['retired'], retired)

    def test_an_ignored_folder_with_a_repository_is_not_held(self):
        _, identity, path, _ = self.evidence_worktree()
        git(path / '.private-evidence', 'init', '-q')
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 4096), \
                self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            self.assertEqual(self.retire(), [])
        self.assertTrue(any('larger than the archive limit' in line for line in logs.output))
        self.assertTrue((path / '.private-evidence' / '.git').is_dir())

    def test_held_folders_go_after_the_archive_age(self):
        _, identity, path, _ = self.evidence_worktree()
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 4096):
            self.assertEqual(self.retire(), [identity])
        folder = Path(self.registry()[identity]['retired']['held']['folder'])
        work_workspace.sweep_temporary(self.workspaces)
        self.assertTrue(folder.is_dir())
        self.age(folder, work_workspace.NESTED_ARCHIVE_DAYS + 1)
        with self.assertLogs('coconut.work_workspace', 'WARNING'):
            work_workspace.sweep_temporary(self.workspaces)
        self.assertFalse(folder.exists())

    def test_an_unreadable_ignored_folder_keeps_the_worktree(self):
        _, identity, path, _ = self.outcome()
        secret = path / 'build' / 'secret'
        secret.mkdir(parents=True)
        (secret / 'key.pem').write_text('private')
        secret.chmod(0)
        self.addCleanup(secret.chmod, 0o700)
        self.assertEqual(self.retire(), [])
        secret.chmod(0o700)
        self.assertEqual((secret / 'key.pem').read_text(), 'private')
        self.assertNotIn('retired', self.registry()[identity])

    def test_large_changes_keep_the_worktree(self):
        _, identity, path, _ = self.outcome()
        (path / 'tracked.txt').write_text('changed\n' * 100)
        with patch('coconut.work_workspace.PATCH_LIMIT_BYTES', 64):
            self.assertEqual(self.retire(), [])
        self.assertTrue(path.exists())
        self.assertNotIn('retired', self.registry()[identity])

    def test_a_truncated_archive_marks_the_restore_failed(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'tracked.txt').write_text('changed\n')
        self.assertEqual(self.retire(), [identity])
        archive = Path(self.registry()[identity]['retired']['archive'])
        data = archive.read_bytes()
        archive.write_bytes(data[:len(data) // 2])
        with self.assertRaisesRegex(ValueError, 'could not restore it'):
            acquire(self.workspaces, outcome, str(self.repo))
        self.assertIn('restore_failed', self.registry()[identity]['retired'])
        self.assertEqual(preferred(self.workspaces, outcome), self.workspaces / outcome)

    def test_large_untracked_files_keep_the_worktree(self):
        _, identity, path, _ = self.outcome()
        (path / 'fixture.dump').write_bytes(b'x' * 1024)
        with patch('coconut.work_workspace.UNTRACKED_LIMIT_BYTES', 512):
            self.assertEqual(self.retire(), [])
        self.assertTrue((path / 'fixture.dump').exists())
        self.assertNotIn('retired', self.registry()[identity])

    def test_repeated_refused_removals_keep_one_archive(self):
        _, identity, path, _ = self.outcome()
        (path / '.env').write_text('SECRET=local\n')
        original = work_workspace.git
        def refuse(*arguments, **options):
            if 'remove' in arguments:
                raise ValueError('working trees containing submodules cannot be removed')
            return original(*arguments, **options)
        with patch('coconut.work_workspace.git', side_effect=refuse):
            for _ in range(3):
                self.assertEqual(self.retire(), [])
                saved = self.registry()
                saved[identity]['retire_failed']['at'] = OLD
                save_bindings(self.workspaces / 'worktrees.json', saved)
        archives = list((self.workspaces / 'archive').iterdir())
        self.assertEqual([str(p) for p in archives], [self.registry()[identity]['retire_failed']['archive']])

    def test_a_file_change_during_archiving_keeps_the_worktree(self):
        _, identity, path, _ = self.outcome()
        (path / '.env').write_text('SECRET=old\n')
        original = work_workspace.write_archive
        def edit_during(*arguments, **options):
            result = original(*arguments, **options)
            (path / '.env').write_text('SECRET=newer value\n')
            return result
        with patch('coconut.work_workspace.write_archive', side_effect=edit_during):
            self.assertEqual(self.retire(), [])
        self.assertEqual((path / '.env').read_text(), 'SECRET=newer value\n')
        self.assertNotIn('retired', self.registry()[identity])

    def test_a_same_size_edit_with_a_reset_time_keeps_the_worktree(self):
        _, identity, path, _ = self.outcome()
        (path / '.env').write_text('SECRET=aaa\n')
        original = work_workspace.write_archive
        def edit_during(*arguments, **options):
            result = original(*arguments, **options)
            before = os.stat(path / '.env')
            (path / '.env').write_text('SECRET=bbb\n')
            os.utime(path / '.env', ns=(before.st_atime_ns, before.st_mtime_ns))
            return result
        with patch('coconut.work_workspace.write_archive', side_effect=edit_during):
            self.assertEqual(self.retire(), [])
        self.assertEqual((path / '.env').read_text(), 'SECRET=bbb\n')

    def test_a_same_size_file_with_other_contents_is_not_accepted(self):
        outcome, identity, path, _ = self.outcome()
        (path / '.env').write_text('SECRET=aaa\n')
        self.assertEqual(self.retire(), [identity])
        archive = Path(self.registry()[identity]['retired']['archive'])
        git(self.repo, 'worktree', 'add', '-q', str(path), 'wt/' + identity)
        (path / '.env').write_text('SECRET=bbb\n')
        with self.assertRaisesRegex(ValueError, 'could not restore it'):
            acquire(self.workspaces, outcome, str(self.repo))
        self.assertTrue(archive.exists())

    def test_an_ignored_special_file_keeps_the_worktree(self):
        _, identity, path, _ = self.outcome()
        (path / 'build').mkdir()
        os.mkfifo(path / 'build' / 'events.fifo')
        self.assertEqual(self.retire(), [])
        self.assertTrue((path / 'build' / 'events.fifo').exists())
        self.assertNotIn('retired', self.registry()[identity])

    def test_a_checkout_missing_ignored_files_is_not_accepted(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'notes.md').write_text('untracked\n')
        (path / '.env').write_text('SECRET=local\n')
        self.assertEqual(self.retire(), [identity])
        archive = Path(self.registry()[identity]['retired']['archive'])
        # A restore stopped after the checkout and the untracked file, before the ignored files.
        git(self.repo, 'worktree', 'add', '-q', str(path), 'wt/' + identity)
        (path / 'notes.md').write_text('untracked\n')
        with self.assertRaisesRegex(ValueError, 'could not restore it'):
            acquire(self.workspaces, outcome, str(self.repo))
        self.assertTrue(archive.exists())
        self.assertIn('restore_failed', self.registry()[identity]['retired'])

    def test_a_path_hint_does_not_wait_for_the_registry_lock(self):
        outcome, _, path, _ = self.outcome()
        results = []
        with (self.workspaces / '.workspace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            reader = threading.Thread(target=lambda: results.append(preferred(self.workspaces, outcome, verify=False)),
                                      daemon=True)
            reader.start()
            reader.join(2)
            self.assertEqual(results, [path])
        reader.join(5)
        (self.workspaces / 'worktrees.json').write_text('{"partial')
        self.assertEqual(preferred(self.workspaces, outcome, verify=False), self.workspaces / outcome)
        (self.workspaces / 'worktrees.json').unlink()
        self.assertEqual(preferred(self.workspaces, outcome, verify=False), self.workspaces / outcome)

    def test_a_briefly_busy_service_does_not_stop_retirement(self):
        _, identity, path, _ = self.outcome()
        entered, held = threading.Event(), threading.Event()
        finished = self.service._finished_outcomes
        def watched(current, *, timeout=-1, only=None, **options):
            # The final check under the registry lock runs with a short timeout.
            if only is not None and timeout != retirement.SERVICE_WAIT_SECONDS:
                entered.set()
            return finished(current, timeout=timeout, only=only, **options)
        original = work_workspace.check_worktree
        def check(*arguments, **options):
            result = original(*arguments, **options)
            if options.get('processes') is False:
                def hold():
                    with self.service.lock:
                        held.set()
                        # Release the lock only after the final check starts to wait for it.
                        entered.wait(5)
                        time.sleep(.05)
                threading.Thread(target=hold, daemon=True).start()
                # The test covers the wait only when the holder owns the lock.
                self.assertTrue(held.wait(5))
            return result
        with patch.object(self.service, '_finished_outcomes', side_effect=watched), \
                patch('coconut.work_workspace.check_worktree', side_effect=check):
            self.assertEqual(self.retire(), [identity])
        self.assertTrue(entered.is_set())
        self.assertFalse(path.exists())

    def test_a_busy_service_is_not_awaited_under_the_registry_lock(self):
        _, identity, path, _ = self.outcome()
        original = work_workspace.write_archive
        acquired, release, blocked = threading.Event(), threading.Event(), []
        def hold_service_lock():
            with self.service.lock:
                acquired.set()
                release.wait(10)
        def watch_registry_lock():
            while not release.is_set():
                with (self.workspaces / '.workspace.lock').open('a') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        blocked.append(True)
                release.wait(.02)
        def archive(*arguments):
            original(*arguments)
            # A dispatch holds the service lock from now until retirement ends.
            threading.Thread(target=hold_service_lock, daemon=True).start()
            acquired.wait(5)
            threading.Thread(target=watch_registry_lock, daemon=True).start()
        with patch('coconut.work_workspace.write_archive', side_effect=archive), \
                patch('coconut.worktree_retirement.SERVICE_WAIT_SECONDS', .5), \
                patch('coconut.worktree_retirement.BUSY_RETRIES', 0), \
                self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            try:
                self.assertEqual(self.retire(), [])
            finally:
                release.set()
        self.assertIn('service was busy', '\n'.join(logs.output))
        self.assertEqual(blocked, [])
        self.assertTrue(path.is_dir())
        self.assertNotIn('retired', self.registry()[identity])

    def test_a_worktree_that_meets_a_busy_service_is_tried_again_in_the_same_pass(self):
        outcome, identity, path, _ = self.outcome()
        real = work_workspace.retire
        calls = []

        def busy_once(root, name, finished, **options):
            calls.append(name)
            if len(calls) == 1:
                raise retirement.ServiceBusy(retirement.BUSY_MESSAGE)
            return real(root, name, finished, **options)
        with patch('coconut.work_workspace.retire', side_effect=busy_once), \
                patch('coconut.worktree_retirement.BUSY_RETRY_SECONDS', 0):
            self.assertEqual(self.retire(), [identity])
        self.assertEqual(calls, [identity, identity])
        self.assertIn('retired', self.registry()[identity])

    def test_a_real_busy_check_is_retried(self):
        outcome, identity, path, _ = self.outcome()
        finished, answers = self.service._finished_outcomes, []

        def busy_first(current, *, timeout=-1, only=None, **options):
            # The first check for this outcome finds the service busy.
            if only is not None and not answers:
                answers.append(None)
                return None
            return finished(current, timeout=timeout, only=only, **options)
        with patch.object(self.service, '_finished_outcomes', side_effect=busy_first), \
                patch('coconut.worktree_retirement.BUSY_RETRY_SECONDS', 0):
            self.assertEqual(self.retire(), [identity])

    def test_a_stop_during_the_busy_pause_ends_the_pass_and_names_the_worktree(self):
        outcome, identity, path, _ = self.outcome()
        stopping = self.service.stopping

        def busy_then_stop(*arguments, **options):
            # The service starts to stop while retirement waits to try again.
            threading.Timer(0.2, stopping.set).start()
            raise retirement.ServiceBusy(retirement.BUSY_MESSAGE)
        started = time.monotonic()
        with patch('coconut.work_workspace.retire', side_effect=busy_then_stop) as attempt, \
                patch('coconut.worktree_retirement.BUSY_RETRY_SECONDS', 30), \
                self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            try:
                self.assertEqual(self.retire(), [])
            finally:
                stopping.clear()
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(attempt.call_count, 1)
        self.assertTrue(any(identity in line and 'Coconut is stopping or restarting' in line for line in logs.output))

    def test_a_busy_check_under_the_registry_lock_is_retried_with_one_archive(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'draft.txt').write_text('keep\n')
        finished, answers = self.service._finished_outcomes, []

        def busy_under_lock(current, *, timeout=-1, only=None, **options):
            # The check under the registry lock, after the archive write, finds the service busy once.
            if only is not None and timeout == retirement.LOCKED_WAIT_SECONDS and not answers:
                answers.append(None)
                return None
            return finished(current, timeout=timeout, only=only, **options)
        with patch.object(self.service, '_finished_outcomes', side_effect=busy_under_lock), \
                patch('coconut.worktree_retirement.BUSY_RETRY_SECONDS', 0):
            self.assertEqual(self.retire(), [identity])
        self.assertEqual(answers, [None])
        archive = self.workspaces / 'archive'
        self.assertEqual(sorted(p.name for p in archive.iterdir() if p.is_file()), [identity + '.tar.gz'])

    def test_a_pending_restart_skips_the_busy_pause(self):
        outcome, identity, path, _ = self.outcome()

        def busy_then_restart(*arguments, **options):
            self.service.restart_pending = True
            raise retirement.ServiceBusy(retirement.BUSY_MESSAGE)
        started = time.monotonic()
        try:
            with patch('coconut.work_workspace.retire', side_effect=busy_then_restart), \
                    patch('coconut.worktree_retirement.BUSY_RETRY_SECONDS', 30):
                self.assertEqual(self.retire(), [])
        finally:
            self.service.restart_pending = False
        self.assertLess(time.monotonic() - started, 10)

    def test_a_restart_during_the_busy_pause_ends_it(self):
        threading.Timer(0.3, lambda: setattr(self.service, 'restart_pending', True)).start()
        started = time.monotonic()
        try:
            with patch('coconut.worktree_retirement.BUSY_RETRY_SECONDS', 30):
                self.assertTrue(self.service._busy_pause())
        finally:
            self.service.restart_pending = False
        self.assertLess(time.monotonic() - started, 5)
        with patch('coconut.worktree_retirement.BUSY_RETRY_SECONDS', 0.2):
            self.assertFalse(self.service._busy_pause())

    def test_a_busy_first_check_runs_the_pass_again_soon(self):
        self.outcome()
        with patch.object(self.service, '_finished_outcomes', return_value=None):
            self.service._next_worktree_retirement = 0
            self.assertEqual(self.service.retire_finished_worktrees(1000), [])
        self.assertEqual(self.service._next_worktree_retirement, 1000 + retirement.BUSY_RETRY_SECONDS)

    def test_a_service_that_stays_busy_is_logged_after_the_last_retry(self):
        outcome, identity, path, _ = self.outcome()
        with patch('coconut.work_workspace.retire', side_effect=retirement.ServiceBusy(retirement.BUSY_MESSAGE)) as attempt, \
                patch('coconut.worktree_retirement.BUSY_RETRY_SECONDS', 0), \
                self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            self.assertEqual(self.retire(), [])
        self.assertEqual(attempt.call_count, retirement.BUSY_RETRIES + 1)
        self.assertEqual(sum('service was busy' in line for line in logs.output), 1)

    def test_a_refused_removal_keeps_its_archive_and_waits_a_day(self):
        outcome, identity, path, _ = self.outcome()
        (path / '.env').write_text('SECRET=local\n')
        original = work_workspace.git
        def refuse(*arguments, **options):
            if 'remove' in arguments:
                raise ValueError('working trees containing submodules cannot be removed')
            return original(*arguments, **options)
        with patch('coconut.work_workspace.git', side_effect=refuse):
            self.assertEqual(self.retire(), [])
        binding = self.registry()[identity]
        self.assertNotIn('retired', binding)
        self.assertIn('submodules', binding['retire_failed']['error'])
        # Git can delete ignored files before it refuses, so the archive stays.
        self.assertEqual([str(p) for p in (self.workspaces / 'archive').iterdir()], [binding['retire_failed']['archive']])
        self.assertEqual((path / '.env').read_text(), 'SECRET=local\n')

        with patch('coconut.work_workspace.write_archive') as archive:
            self.assertEqual(self.retire(), [])
            archive.assert_not_called()
        saved = self.registry()
        saved[identity]['retire_failed']['at'] = OLD
        save_bindings(self.workspaces / 'worktrees.json', saved)
        self.assertEqual(self.retire(), [identity])
        self.assertNotIn('retire_failed', self.registry()[identity])
        self.assertEqual(len(list((self.workspaces / 'archive').iterdir())), 1)

    def interrupted_removal(self, before_timeout):
        original, timeouts = work_workspace.git, []
        def remove(*arguments, **options):
            if 'remove' in arguments and not timeouts:
                timeouts.append(options.get('timeout'))
                before_timeout()
                raise subprocess.TimeoutExpired(['git', 'worktree', 'remove'], options.get('timeout'))
            return original(*arguments, **options)
        return patch('coconut.work_workspace.git', side_effect=remove), timeouts

    def test_a_removal_that_times_out_after_git_deleted_the_checkout_completes(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'draft.txt').write_text('keep\n')
        slow, timeouts = self.interrupted_removal(lambda: git(self.repo, 'worktree', 'remove', '--force', str(path)))
        with slow:
            self.assertEqual(self.retire(), [identity])
        self.assertEqual(timeouts, [work_workspace.CHECKOUT_SECONDS])
        self.assertFalse(path.exists())
        self.assertNotIn('restore_failed', self.registry()[identity]['retired'])
        acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual((path / 'draft.txt').read_text(), 'keep\n')

    def test_a_removal_that_times_out_before_git_deleted_files_rolls_back(self):
        _, identity, path, _ = self.outcome()
        (path / 'draft.txt').write_text('keep\n')
        slow, _ = self.interrupted_removal(lambda: None)
        with slow:
            self.assertEqual(self.retire(), [])
        binding = self.registry()[identity]
        self.assertNotIn('retired', binding)
        self.assertIn('retire_failed', binding)
        self.assertEqual(len(list((self.workspaces / 'archive').iterdir())), 1)
        self.assertEqual((path / 'draft.txt').read_text(), 'keep\n')

    def test_a_removal_that_times_out_part_way_keeps_the_archive_for_the_operator(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'draft.txt').write_text('keep\n')
        slow, _ = self.interrupted_removal(lambda: (path / 'tracked.txt').unlink())
        with slow, self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            self.assertEqual(self.retire(), [])
        self.assertIn('part of the checkout remains', '\n'.join(logs.output))
        retired = self.registry()[identity]['retired']
        self.assertIn('part of the checkout remains', retired['restore_failed']['error'])
        self.assertTrue(Path(retired['archive']).is_file())
        self.assertEqual(preferred(self.workspaces, outcome), self.workspaces / outcome)
        self.assertEqual((path / 'draft.txt').read_text(), 'keep\n')

    def test_an_interrupted_restore_leaves_no_registered_partial_checkout(self):
        for broken in (False, True):
            with self.subTest(broken=broken):
                self.interrupted_restore(broken)

    def interrupted_restore(self, broken):
        outcome, identity, path, _ = self.outcome()
        (path / 'tracked.txt').write_text('changed\n')
        self.service._next_worktree_retirement = 0
        self.assertIn(identity, self.service.retire_finished_worktrees())
        original, timeouts = work_workspace.git, []
        def add(*arguments, **options):
            if 'add' in arguments and not timeouts:
                timeouts.append(options.get('timeout'))
                original(*arguments, **options)
                if broken:
                    # Git stopped before it wrote the link to the repository. The record stays in Git.
                    (path / '.git').unlink()
                raise subprocess.TimeoutExpired(['git', 'worktree', 'add'], options.get('timeout'))
            return original(*arguments, **options)
        with patch('coconut.work_workspace.git', side_effect=add):
            with self.assertRaisesRegex(ValueError, 'could not restore it'):
                acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(timeouts, [work_workspace.CHECKOUT_SECONDS])
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(self.repo, 'worktree', 'list', '--porcelain'))
        saved = self.registry()
        saved[identity]['retired'].pop('restore_failed')
        save_bindings(self.workspaces / 'worktrees.json', saved)
        acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual((path / 'tracked.txt').read_text(), 'changed\n')

    def test_a_new_retirement_keeps_one_archive_and_sweeps_unfinished_files(self):
        _, first, _, _ = self.outcome()
        self.assertEqual(self.retire(), [first])
        kept = Path(self.registry()[first]['retired']['archive'])
        _, second, _, _ = self.outcome()
        directory = self.workspaces / 'archive'
        for name in (second + '.tar.gz', second + '-20260101T000000000000Z.tar.gz', '.' + second + '.tar.gz.tmp'):
            (directory / name).write_bytes(b'old')
        self.assertEqual(self.retire(), [second])
        current = Path(self.registry()[second]['retired']['archive'])
        self.assertEqual(sorted(directory.iterdir()), sorted([kept, current]))
        with tarfile.open(current) as tar:
            self.assertIn('retirement.json', tar.getnames())

    def test_hard_linked_untracked_files_retire_and_return(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'first.txt').write_text('shared content\n')
        os.link(path / 'first.txt', path / 'second.txt')
        self.assertEqual(self.retire(), [identity])
        acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual((path / 'first.txt').read_text(), 'shared content\n')
        self.assertEqual((path / 'second.txt').read_text(), 'shared content\n')

    def test_archive_work_does_not_hold_the_registry_lock(self):
        _, identity, _, _ = self.outcome()
        original = work_workspace.write_archive
        shared = []
        def archive(*arguments):
            # A path hint takes a shared lock. It must not wait while Coconut writes the archive.
            with (self.workspaces / '.workspace.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                shared.append(True)
            original(*arguments)
        with patch('coconut.work_workspace.write_archive', side_effect=archive):
            self.assertEqual(self.retire(), [identity])
        self.assertEqual(shared, [True])

    def write_checkout_files(self, path):
        for name, text in (('tracked.txt', 'changed\n'), ('notes.md', 'untracked\n'), ('.env', 'SECRET=local\n')):
            (path / name).write_text(text)

    def remaining_checkout(self):
        """Retire a worktree, then check it out again with the archived files, as an interrupted restore can."""
        outcome, identity, path, _ = self.outcome()
        self.write_checkout_files(path)
        # The job also retires the worktree of an earlier subtest again.
        self.assertIn(identity, self.retire())
        git(self.repo, 'worktree', 'add', '-q', str(path), 'wt/' + identity)
        self.write_checkout_files(path)
        return outcome, identity, path, Path(self.registry()[identity]['retired']['archive'])

    def test_the_comparison_with_the_archive_does_not_hold_the_registry_lock(self):
        for entry in ('acquire', 'preferred'):
            with self.subTest(entry=entry):
                outcome, identity, path, archive = self.remaining_checkout()
                original, free = work_workspace.path_digest, []
                def digest(target):
                    # A worktree acquisition takes this lock. It must not wait while Coconut reads the files.
                    with (self.workspaces / '.workspace.lock').open('a') as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                            free.append(True)
                        except BlockingIOError:
                            free.append(False)
                    return original(target)
                with patch('coconut.work_workspace.path_digest', side_effect=digest):
                    if entry == 'acquire':
                        self.assertEqual(acquire(self.workspaces, outcome, str(self.repo))['path'], str(path))
                    else:
                        self.assertEqual(preferred(self.workspaces, outcome), path)
                self.assertEqual(free, [True, True])
                self.assertNotIn('retired', self.registry()[identity])
                self.assertFalse(archive.exists())

    def test_a_file_change_after_the_comparison_refuses_the_checkout(self):
        outcome, identity, path, archive = self.remaining_checkout()
        original = work_workspace.path_digest
        def edit_after(target):
            result = original(target)
            if target.name == '.env':
                # The same size and modification time. Only the change time shows the write.
                before = os.stat(target)
                target.write_text('SECRET=other\n')
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            return result
        with patch('coconut.work_workspace.path_digest', side_effect=edit_after):
            self.assertEqual(preferred(self.workspaces, outcome), self.workspaces / outcome)
        self.assertIn('does not match the archive', self.registry()[identity]['retired']['restore_failed']['error'])
        self.assertTrue(archive.exists())
        self.assertEqual((path / '.env').read_text(), 'SECRET=other\n')

    def test_a_checkout_that_appears_after_the_comparison_is_compared_under_the_lock(self):
        outcome, identity, path, archive = self.remaining_checkout()
        # Before the unlocked comparison, the path is empty.
        git(self.repo, 'worktree', 'remove', '--force', str(path))
        original, calls = work_workspace.compare_checkouts, []
        def appear(bindings):
            result = original(bindings)
            if not calls:
                # Another restore finished the checkout and stopped before it saved the registry.
                git(self.repo, 'worktree', 'add', '-q', str(path), 'wt/' + identity)
                self.write_checkout_files(path)
            calls.append(result)
            return result
        with patch('coconut.work_workspace.compare_checkouts', side_effect=appear):
            self.assertEqual(acquire(self.workspaces, outcome, str(self.repo))['path'], str(path))
        self.assertEqual(len(calls), 2)
        self.assertNotIn('retired', self.registry()[identity])
        self.assertFalse(archive.exists())
        self.assertEqual((path / '.env').read_text(), 'SECRET=local\n')

    def test_an_incomplete_checkout_that_finishes_after_comparison_is_rechecked(self):
        outcome, identity, path, archive = self.remaining_checkout()
        (path / '.env').unlink()
        original, calls = work_workspace.compare_checkouts, []
        def finish(bindings):
            result = original(bindings)
            if not calls:
                record = result[json.dumps(self.registry()[identity], sort_keys=True)]
                self.assertIsNone(record[0] if isinstance(record, tuple) else record)
                (path / '.env').write_text('SECRET=local\n')
            calls.append(result)
            return result
        with patch('coconut.work_workspace.compare_checkouts', side_effect=finish):
            self.assertEqual(acquire(self.workspaces, outcome, str(self.repo))['path'], str(path))
        self.assertEqual(len(calls), 2)
        self.assertNotIn('retired', self.registry()[identity])
        self.assertFalse(archive.exists())

    def test_a_stable_mismatch_does_not_recompare_under_the_lock(self):
        outcome, identity, path, archive = self.remaining_checkout()
        (path / '.env').write_text('SECRET=other\n')
        original, held = work_workspace.path_digest, []
        def digest(target):
            with (self.workspaces / '.workspace.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    held.append(False)
                except BlockingIOError:
                    held.append(True)
            return original(target)
        with patch('coconut.work_workspace.path_digest', side_effect=digest):
            with self.assertRaisesRegex(ValueError, 'could not restore'):
                acquire(self.workspaces, outcome, str(self.repo))
        self.assertTrue(held)
        self.assertNotIn(True, held)
        self.assertTrue(archive.exists())
        self.assertIn('restore_failed', self.registry()[identity]['retired'])

    def test_missing_archived_file_during_comparison_never_deletes_archive(self):
        outcome, identity, path, archive = self.remaining_checkout()
        (path / '.env').unlink()
        original, appeared = work_workspace.checkout_marks, []
        compare = work_workspace.compare_checkouts
        def appear_after_snapshot(root, names):
            marks = original(root, names)
            if not appeared:
                self.assertIn(('.env', None), marks)
                (path / '.env').write_text('SECRET=local\n')
                appeared.append(True)
            return marks
        def disappear_after_comparison(bindings):
            result = compare(bindings)
            (path / '.env').unlink(missing_ok=True)
            return result
        with patch('coconut.work_workspace.checkout_marks', side_effect=appear_after_snapshot), \
             patch('coconut.work_workspace.compare_checkouts', side_effect=disappear_after_comparison):
            with self.assertRaisesRegex(ValueError, 'could not restore'):
                acquire(self.workspaces, outcome, str(self.repo))
        self.assertEqual(appeared, [True])
        self.assertTrue(archive.exists())
        self.assertIn('restore_failed', self.registry()[identity]['retired'])

    def commit_on_branch(self, branch):
        tree = git(self.repo, 'rev-parse', branch + '^{tree}')
        commit = git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                     'commit-tree', tree, '-p', branch, '-m', 'Moved')
        git(self.repo, 'update-ref', 'refs/heads/' + branch, commit)

    @unittest.skipUnless(shutil.which('lsof'), 'lsof is not installed.')
    def test_a_process_with_its_cwd_in_the_worktree_keeps_it(self):
        _, identity, path, _ = self.outcome()
        process = subprocess.Popen(['sleep', '60'], cwd=path)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.holders.stop()
        self.assertTrue(work_workspace.in_use(path))
        self.assertEqual(self.retire(), [])
        self.assertTrue(path.is_dir())
        self.assertNotIn('retired', self.registry()[identity])


    def orphan(self, path, outcome=None, *, argument=None):
        """Start a process in the path whose parent ends at once, so that launchd adopts it.

        `argument` goes on its command line, as the path of a dev server does.
        """
        env = dict(os.environ, COCONUT_WORKSPACE_OUTCOME=outcome) if outcome else None
        command = (f'{shlex.quote(sys.executable)} -c "import time; time.sleep(60)" {shlex.quote(argument or "")} '
                   '>/dev/null 2>&1 & echo $!')
        started = subprocess.run(['sh', '-c', command], cwd=path, env=env, capture_output=True, text=True, check=True)
        pid = int(started.stdout)
        self.addCleanup(lambda: subprocess.run(['kill', str(pid)], capture_output=True))
        return pid

    def orphan_with_child(self, path, outcome):
        """Start an orphan of the outcome that has a /bin/sleep child. macOS hides the child's environment."""
        script = "import subprocess, time; subprocess.Popen(['sleep', '60']); time.sleep(60)"
        command = f'{shlex.quote(sys.executable)} -c {shlex.quote(script)} >/dev/null 2>&1 & echo $!'
        started = subprocess.run(['sh', '-c', command], cwd=path, env=dict(os.environ, COCONUT_WORKSPACE_OUTCOME=outcome),
                                 capture_output=True, text=True, check=True)
        pid = int(started.stdout)
        self.addCleanup(lambda: subprocess.run(['kill', str(pid)], capture_output=True))
        for _ in range(50):
            children = subprocess.run(['pgrep', '-P', str(pid)], capture_output=True, text=True).stdout.split()
            if children:
                break
            time.sleep(0.1)
        child = int(children[0])
        self.addCleanup(lambda: subprocess.run(['kill', str(child)], capture_output=True))
        return pid, child

    def alive(self, pid):
        return subprocess.run(['ps', '-p', str(pid)], capture_output=True).returncode == 0

    @unittest.skipUnless(ENVIRONMENTS_VISIBLE, 'The test needs lsof, launchd, and visible process environments.')
    def test_an_old_orphaned_process_of_the_outcome_is_stopped_and_the_worktree_retires(self):
        outcome, identity, path, _ = self.outcome()
        pid = self.orphan(path, outcome)
        self.holders.stop()
        with patch.object(retirement, 'ORPHAN_AGE_SECONDS', 0):
            self.assertEqual(self.retire(), [identity])
        self.assertFalse(self.alive(pid))
        self.assertIn('retired', self.registry()[identity])

    @unittest.skipUnless(ENVIRONMENTS_VISIBLE, 'The test needs lsof, launchd, and visible process environments.')
    def test_young_and_foreign_orphans_stay_and_a_locked_worktree_keeps_its_files(self):
        young_outcome, young, young_path, _ = self.outcome()
        young_pid = self.orphan(young_path, young_outcome)
        _, foreign, foreign_path, _ = self.outcome()
        # launchd also starts apps and agents. A process without the outcome variable is not Coconut's.
        foreign_pid = self.orphan(foreign_path, str(uuid4()))
        locked_outcome, locked, locked_path, _ = self.outcome()
        locked_pid = self.orphan(locked_path, locked_outcome)
        git(self.repo, 'worktree', 'lock', str(locked_path))
        self.holders.stop()
        self.assertEqual(self.retire(), [])
        with patch.object(retirement, 'ORPHAN_AGE_SECONDS', 0):
            self.assertEqual(self.retire(), [young])
        self.assertFalse(self.alive(young_pid))
        self.assertTrue(self.alive(foreign_pid))
        # An idle outcome loses its orphans even when its worktree cannot retire. The locked worktree stays.
        for _ in range(50):
            if not self.alive(locked_pid):
                break
            time.sleep(0.1)
        self.assertFalse(self.alive(locked_pid))
        for identity, path in ((foreign, foreign_path), (locked, locked_path)):
            self.assertTrue(path.is_dir())
            self.assertNotIn('retired', self.registry()[identity])

    @unittest.skipUnless(ENVIRONMENTS_VISIBLE, 'The test needs lsof, launchd, and visible process environments.')
    def test_an_old_orphan_without_the_outcome_variable_stops_when_the_pull_request_merged(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        # A dev server that a bot started in its own shell has no COCONUT_WORKSPACE_OUTCOME. Its command line names the path.
        pid = self.orphan(path, argument=str(path / 'node_modules/.bin/vite'))
        self.holders.stop()
        with patch.object(retirement, 'ORPHAN_AGE_SECONDS', 0):
            self.assertEqual(self.retire(), [identity])
        for _ in range(50):
            if not self.alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(self.alive(pid))
        self.assertFalse(path.exists())

    @unittest.skipUnless(ENVIRONMENTS_VISIBLE, 'The test needs lsof, launchd, and visible process environments.')
    def test_an_orphan_and_its_child_process_are_both_stopped(self):
        outcome, identity, path, _ = self.outcome()
        pid, child = self.orphan_with_child(path, outcome)
        self.holders.stop()
        with patch.object(retirement, 'ORPHAN_AGE_SECONDS', 0):
            self.assertEqual(self.retire(), [identity])
        self.assertFalse(self.alive(pid))
        self.assertFalse(self.alive(child))

    @unittest.skipUnless(ENVIRONMENTS_VISIBLE, 'The test needs lsof, launchd, and visible process environments.')
    def test_a_process_of_the_outcome_with_a_live_parent_keeps_the_worktree(self):
        outcome, identity, path, _ = self.outcome()
        # A live worker has the service as its parent. Only launchd's adopted processes are orphans.
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=path,
                                   env=dict(os.environ, COCONUT_WORKSPACE_OUTCOME=outcome))
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.holders.stop()
        with patch.object(retirement, 'ORPHAN_AGE_SECONDS', 0):
            self.assertEqual(self.retire(), [])
        self.assertIsNone(process.poll())
        self.assertTrue(path.is_dir())
        self.assertNotIn('retired', self.registry()[identity])

    def test_holders_pairs_each_open_file_with_its_process(self):
        path = self.root / 'work'
        listing = b''.join([b'p10\nfcwd\nn/elsewhere\n', b'p11\nfcwd\nn' + os.fsencode(str(path)) + b'\n',
                            b'p12\nf3\nn' + os.fsencode(str(path)) + b'/log.txt\nf4\nn/elsewhere/a\n',
                            b'p13\nf5\nn' + os.fsencode(str(path)) + b'-other/file\n'])
        self.holders.stop()
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing, returncode=0)):
            self.assertEqual(work_workspace.holders(path), {11, 12})
        # A partial list could hide a holder, so a failed lsof refuses the worktree.
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing, returncode=1)):
            with self.assertRaises(ValueError):
                work_workspace.holders(path)

    def test_the_environment_of_another_process_is_read(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   env=dict(os.environ, COCONUT_WORKSPACE_OUTCOME='one'))
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        time.sleep(0.2)
        self.assertTrue(work_workspace.environment_names(process.pid, 'COCONUT_WORKSPACE_OUTCOME=one'))
        self.assertFalse(work_workspace.environment_names(process.pid, 'COCONUT_WORKSPACE_OUTCOME=on'))
        self.assertFalse(work_workspace.environment_names(process.pid, 'COCONUT_WORKSPACE_OUTCOME=two'))

    def test_process_outcomes_trusts_the_scan_only_when_the_worker_probe_shows(self):
        started = []

        class Probe:
            def __init__(self, arguments, env, **options):
                started.append((arguments[0], env['COCONUT_WORKSPACE_OUTCOME']))

            def kill(self):
                pass

            def wait(self):
                pass

        def listing(shown):
            marker = started[-1][1]
            probe = f' COCONUT_WORKSPACE_OUTCOME={marker}' if shown else ''
            return SimpleNamespace(stdout='\n'.join([f'20 python -c sleep{probe}',
                '10 sleep 60 COCONUT_WORKSPACE_OUTCOME=one PATH=/usr/bin', '11 /bin/sh', '12 codex COCONUT_WORKSPACE_OUTCOME=two']))
        # Without /proc, Coconut reads macOS `ps -E`. It trusts the list only when the worker interpreter's marker shows.
        with patch('coconut.work_workspace.Path.exists', return_value=False), \
                patch('coconut.work_workspace.subprocess.Popen', Probe), \
                patch('coconut.work_workspace.subprocess.run', side_effect=lambda *a, **k: listing(True)):
            self.assertEqual(work_workspace.process_outcomes('/opt/hermes/python'), {'one', 'two'})
        self.assertEqual(started[-1][0], '/opt/hermes/python')
        with patch('coconut.work_workspace.Path.exists', return_value=False), \
                patch('coconut.work_workspace.subprocess.Popen', Probe), \
                patch('coconut.work_workspace.subprocess.run', side_effect=lambda *a, **k: listing(False)):
            self.assertIsNone(work_workspace.process_outcomes('/opt/hermes/python'))

    def test_process_outcomes_finds_a_real_child_process(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   env=dict(os.environ, COCONUT_WORKSPACE_OUTCOME='probe-outcome'))
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        time.sleep(0.2)
        found = work_workspace.process_outcomes()
        if found is None:
            self.skipTest('This system hides process environments.')
        self.assertIn('probe-outcome', found)

    def test_busy_paths_reports_each_held_path_from_one_scan(self):
        held, free = self.root / 'held', self.root / 'free'
        held.mkdir()
        free.mkdir()
        listing = b'p10\nfcwd\nn' + os.fsencode(str(held)) + b'/log.txt\np11\nfcwd\nn' + os.fsencode(str(free)) + b'-other\n'
        self.holders.stop()
        # busy_paths reads the process table only to exempt shared services.
        table = patch('coconut.work_workspace.process_table', return_value={})
        table.start()
        self.addCleanup(table.stop)
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing, returncode=0)) as run:
            self.assertEqual(work_workspace.busy_paths({str(held), str(free)}), {str(held)})
        self.assertEqual(run.call_count, 1)
        # A failed lsof can print part of the list, so it does not count.
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing, returncode=1)):
            with self.assertRaises(ValueError):
                work_workspace.busy_paths({str(held), str(free)})
        # Two paths with one resolved form are both busy.
        link = self.root / 'held-link'
        link.symlink_to(held)
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing, returncode=0)):
            self.assertEqual(work_workspace.busy_paths({str(held), str(link)}), {str(held), str(link)})

    def nested_worktree(self, parent, name='base', folder='tmp'):
        """Add a detached worktree inside the parent, as a bot does to test a base commit."""
        inner = parent / folder / name
        git(self.repo, 'worktree', 'add', '-q', '--detach', str(inner), 'HEAD')
        return inner

    def listed(self, path):
        return any(Path(r['path']).resolve() == path.resolve() for r in work_workspace.registered(self.repo))

    def test_a_clean_nested_worktree_is_removed_and_the_parent_retires(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        (inner / 'node_modules').mkdir()
        (inner / 'node_modules' / 'cache.js').write_text('rebuildable\n')
        self.assertEqual(self.retire(), [identity])
        self.assertFalse(inner.exists())
        self.assertFalse(self.listed(inner))
        self.assertIn('retired', self.registry()[identity])

    def test_a_nested_worktree_that_git_cannot_rebuild_keeps_the_parent(self):
        cases = {
            'locked': lambda inner: git(self.repo, 'worktree', 'lock', str(inner)),
            'unsaved commit': lambda inner: (
                (inner / 'new.txt').write_text('commit\n'), git(inner, 'add', 'new.txt'), self.commit(inner, 'Only here')),
            'git operation': lambda inner: Path(git(inner, 'rev-parse', '--path-format=absolute', '--git-path', 'MERGE_HEAD'))
                .write_text(git(inner, 'rev-parse', 'HEAD') + '\n'),
            'unmerged file': lambda inner: subprocess.run(['git', '-C', str(inner), 'update-index', '--index-info'], check=True,
                input=''.join(f"{mode} {git(inner, 'hash-object', '-w', 'tracked.txt')} {stage}\ttracked.txt\n"
                              for mode, stage in (('0', 0), ('100644', 1), ('100644', 2), ('100644', 3))), text=True),
            # An archive cannot hold a plain repository inside the nested worktree.
            'nested repository': lambda inner: (git(inner / 'vendor', 'init', '-q') if (inner / 'vendor').mkdir() is None else None),
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                _, identity, path, _ = self.outcome()
                inner = self.nested_worktree(path, name.replace(' ', '-'))
                change(inner)
                self.assertEqual(self.retire(), [])
                self.assertTrue(inner.is_dir())
                self.assertTrue(self.listed(inner))
                self.assertNotIn('retired', self.registry()[identity])

    def nested_archives(self):
        return sorted((self.workspaces / 'archive' / 'nested').glob('*.tar.gz'))

    def test_a_nested_worktree_with_changes_is_archived_and_the_parent_retires(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path, 'subagent', folder='.worktrees')
        git(self.repo, 'branch', 'subagent-branch', git(inner, 'rev-parse', 'HEAD'))
        git(inner, 'checkout', '-q', 'subagent-branch')
        (inner / 'tracked.txt').write_text('one\nstaged\n')
        git(inner, 'add', 'tracked.txt')
        (inner / 'tracked.txt').write_text('one\nstaged\nunstaged\n')
        (inner / 'draft.md').write_text('untracked draft\n')
        (inner / '.env').write_text('SECRET=1\n')
        (inner / 'node_modules').mkdir()
        (inner / 'node_modules' / 'cache.js').write_text('rebuildable\n')
        self.assertEqual(self.retire(), [identity])
        self.assertFalse(inner.exists())
        self.assertFalse(self.listed(inner))
        self.assertIn('retired', self.registry()[identity])
        [archive] = self.nested_archives()
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive) as tar:
            self.assertEqual(sorted(tar.getnames()), ['ignored/.env', 'retirement.json', 'staged.patch', 'unstaged.patch',
                                                      'untracked/draft.md'])
            metadata = json.loads(tar.extractfile('retirement.json').read())
            self.assertIn(b'+staged', tar.extractfile('staged.patch').read())
            self.assertIn(b'+unstaged', tar.extractfile('unstaged.patch').read())
        self.assertEqual((metadata['branch'], metadata['parent'], metadata['path']),
                         ('refs/heads/subagent-branch', str(path.resolve()), str(inner.resolve())))
        # The branch keeps the nested worktree's commits.
        self.assertTrue(git(self.repo, 'rev-parse', '--verify', 'refs/heads/subagent-branch'))

    def test_a_refused_nested_removal_keeps_one_archive(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        (inner / 'draft.txt').write_text('keep\n')
        real = work_workspace.git

        def refuse(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'remove') and Path(arguments[-1]) == inner.resolve():
                raise ValueError('working trees containing submodules cannot be removed')
            return real(where, *arguments, **options)
        with patch('coconut.work_workspace.git', side_effect=refuse):
            self.assertEqual(self.retire(), [])
            self.assertEqual(self.retire(), [])
            with self.assertRaisesRegex(ValueError, 'Files remain in .*Its changes are in'):
                work_workspace.remove_nested(self.workspaces, path.resolve(), self.repo)
        self.assertEqual(len(self.nested_archives()), 1)
        self.assertTrue((inner / 'draft.txt').is_file())

    def test_a_worktree_inside_a_nested_worktree_is_archived_only_once(self):
        _, identity, path, _ = self.outcome()
        outer = self.nested_worktree(path, 'outer', folder='.worktrees')
        (outer / '.gitignore').write_text('build/\n')
        inner = self.nested_worktree(outer, 'inner', folder='build')
        (inner / 'draft.txt').write_text('keep\n')
        self.assertEqual(self.retire(), [identity])
        names = {}
        for archive in self.nested_archives():
            with tarfile.open(archive) as tar:
                names[json.loads(tar.extractfile('retirement.json').read())['path']] = sorted(tar.getnames())
        self.assertEqual(names[str(inner.resolve())], ['retirement.json', 'staged.patch', 'unstaged.patch', 'untracked/draft.txt'])
        # The outer archive holds its own .gitignore change, but not the files of the worktree inside it.
        self.assertEqual(names[str(outer.resolve())], ['retirement.json', 'staged.patch', 'unstaged.patch'])

    def test_a_nested_worktree_with_a_submodule_keeps_the_parent(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        library = self.root / 'library'
        library.mkdir()
        git(library, 'init', '-q')
        (library / 'lib.txt').write_text('library\n')
        git(library, 'add', '.')
        self.commit(library, 'Library')
        subprocess.run(['git', '-C', str(inner), '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q',
                        str(library), 'vendor/lib'], check=True, capture_output=True)
        (inner / 'vendor' / 'lib' / 'lib.txt').write_text('uncommitted edit\n')
        self.assertEqual(self.retire(), [])
        self.assertEqual((inner / 'vendor' / 'lib' / 'lib.txt').read_text(), 'uncommitted edit\n')
        self.assertEqual(self.nested_archives(), [])
        # A gitlink folder with files but without its .git also stays.
        (inner / 'vendor' / 'lib' / '.git').unlink()
        self.assertEqual(self.retire(), [])
        self.assertEqual((inner / 'vendor' / 'lib' / 'lib.txt').read_text(), 'uncommitted edit\n')

    def test_a_branch_switch_during_the_archive_keeps_the_nested_worktree(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        (inner / 'draft.txt').write_text('keep\n')
        real = work_workspace.write_archive

        def switch(*arguments, **options):
            result = real(*arguments, **options)
            git(inner, 'checkout', '-q', '-b', 'switched')
            return result
        with patch('coconut.work_workspace.write_archive', side_effect=switch):
            with self.assertRaisesRegex(ValueError, 'changed during retirement'):
                work_workspace.remove_nested(self.workspaces, path.resolve(), self.repo)
        self.assertTrue((inner / 'draft.txt').is_file())
        self.assertTrue(self.listed(inner))

    def test_a_parent_that_grows_during_the_archive_keeps_its_nested_worktree(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        (inner / 'draft.txt').write_text('keep\n')
        real = work_workspace.write_archive

        def grow(*arguments, **options):
            result = real(*arguments, **options)
            (path / 'large.bin').write_bytes(b'x' * 2048)
            return result
        with patch('coconut.work_workspace.write_archive', side_effect=grow), \
                patch('coconut.work_workspace.UNTRACKED_LIMIT_BYTES', 1024):
            with self.assertRaisesRegex(ValueError, 'larger than the archive limit'):
                work_workspace.remove_nested(self.workspaces, path.resolve(), self.repo)
        self.assertTrue((inner / 'draft.txt').is_file())
        self.assertTrue(self.listed(inner))

    def test_a_link_to_a_sibling_nested_worktree_stays_in_the_archive(self):
        _, identity, path, _ = self.outcome()
        first = self.nested_worktree(path, 'first', folder='.worktrees')
        second = self.nested_worktree(path, 'second', folder='.worktrees')
        (first / 'draft.txt').write_text('keep\n')
        # Git lists an ignored link at the top level as one entry without a slash.
        (first / 'peer').symlink_to(second)
        (first / '.gitignore').write_text('peer\n')
        self.assertEqual(self.retire(), [identity])
        names = {}
        for archive in self.nested_archives():
            with tarfile.open(archive) as tar:
                names[json.loads(tar.extractfile('retirement.json').read())['path']] = sorted(tar.getnames())
        self.assertIn('ignored/peer', names[str(first.resolve())])

    def test_a_parent_that_cannot_retire_keeps_its_nested_worktree(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        (inner / 'draft.txt').write_text('keep\n')
        # The parent's own untracked files are over the archive limit.
        (path / 'large.bin').write_bytes(b'x' * 2048)
        with patch('coconut.work_workspace.UNTRACKED_LIMIT_BYTES', 1024):
            self.assertEqual(self.retire(), [])
        self.assertTrue((inner / 'draft.txt').is_file())
        self.assertTrue(self.listed(inner))
        self.assertEqual(self.nested_archives(), [])

    def test_old_nested_archives_are_deleted_and_the_folders_are_private(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        (inner / 'draft.txt').write_text('keep\n')
        self.assertEqual(self.retire(), [identity])
        [archive] = self.nested_archives()
        self.assertEqual((self.workspaces / 'archive').stat().st_mode & 0o777, 0o700)
        self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
        work_workspace.sweep_temporary(self.workspaces)
        self.assertTrue(archive.exists())
        old = time.time() - (work_workspace.NESTED_ARCHIVE_DAYS + 1) * 86400
        os.utime(archive, (old, old))
        with self.assertLogs('coconut.work_workspace', 'WARNING') as logs:
            work_workspace.sweep_temporary(self.workspaces)
        self.assertFalse(archive.exists())
        self.assertIn(archive.name, '\n'.join(logs.output))
        # Over the size limit, the oldest archives go first.
        folder = self.workspaces / 'archive' / 'nested'
        for index, age in enumerate((300, 200, 100)):
            item = folder / f'item-{index}.tar.gz'
            item.write_bytes(b'x' * 1000)
            os.utime(item, (time.time() - age, time.time() - age))
        # An archive younger than a day stays, even over the limit.
        with patch('coconut.work_workspace.NESTED_ARCHIVE_LIMIT_BYTES', 2000):
            work_workspace.sweep_temporary(self.workspaces)
        self.assertEqual(len(list(folder.glob('*.tar.gz'))), 3)
        with patch('coconut.work_workspace.NESTED_ARCHIVE_LIMIT_BYTES', 2000), \
                patch('coconut.work_workspace.NESTED_ARCHIVE_KEEP_SECONDS', 0):
            work_workspace.sweep_temporary(self.workspaces)
        self.assertEqual(sorted(p.name for p in folder.glob('*.tar.gz')), ['item-1.tar.gz', 'item-2.tar.gz'])
        # An archive that a pass depends on stays, even when it is old or the folder is over the limit.
        needed = folder / 'item-1.tar.gz'
        os.utime(needed, (old, old))
        with patch('coconut.work_workspace.NESTED_ARCHIVE_LIMIT_BYTES', 500), \
                patch('coconut.work_workspace.NESTED_ARCHIVE_KEEP_SECONDS', 0):
            work_workspace.trim_nested_archives(self.workspaces, keep={needed})
        self.assertEqual(sorted(p.name for p in folder.glob('*.tar.gz')), ['item-1.tar.gz'])

    def test_old_retirement_archives_go_unless_a_binding_names_them(self):
        folder = self.workspaces / 'archive'
        (folder / 'nested').mkdir(parents=True)
        old = time.time() - (work_workspace.NESTED_ARCHIVE_DAYS + 1) * 86400
        names = ('unnamed', 'retired', 'failed', 'handoff', 'nested/inner')
        for name in names:
            (folder / f'{name}.tar.gz').write_bytes(b'x' * 1000)
            os.utime(folder / f'{name}.tar.gz', (old, old))
        fresh = folder / 'fresh.tar.gz'
        fresh.write_bytes(b'x' * 1000)
        manifest = self.workspaces / 'worktrees.json'
        manifest.write_text('{')
        # Without the registry, no retirement archive goes.
        work_workspace.sweep_temporary(self.workspaces)
        self.assertTrue((folder / 'unnamed.tar.gz').exists())
        manifest.write_text(json.dumps({
            'one': dict(path='/x/one', retired=dict(archive=str(folder / 'retired.tar.gz'))),
            'two': dict(path='/x/two', retire_failed=dict(archive=str(folder / 'failed.tar.gz'))),
            'three': dict(path='/x/three', handoff=dict(archive=str(folder.resolve() / 'handoff.tar.gz')))}))
        with self.assertLogs('coconut.work_workspace', 'WARNING') as logs:
            work_workspace.sweep_temporary(self.workspaces)
        self.assertIn('unnamed.tar.gz', '\n'.join(logs.output))
        self.assertEqual(sorted(p.name for p in folder.glob('*.tar.gz')),
                         ['failed.tar.gz', 'fresh.tar.gz', 'handoff.tar.gz', 'retired.tar.gz'])
        # The nested archive follows its own rule.
        self.assertFalse((folder / 'nested' / 'inner.tar.gz').exists())
        # Over the size limit, the oldest unnamed archive goes first. A named archive stays.
        for index, age in enumerate((300, 200)):
            item = folder / f'item-{index}.tar.gz'
            item.write_bytes(b'x' * 1000)
            os.utime(item, (time.time() - age, time.time() - age))
        with patch('coconut.work_workspace.NESTED_ARCHIVE_LIMIT_BYTES', 5500), \
                patch('coconut.work_workspace.NESTED_ARCHIVE_KEEP_SECONDS', 0):
            work_workspace.sweep_temporary(self.workspaces)
        self.assertEqual(sorted(p.name for p in folder.glob('*.tar.gz')),
                         ['failed.tar.gz', 'fresh.tar.gz', 'handoff.tar.gz', 'item-1.tar.gz', 'retired.tar.gz'])

    def test_an_outcome_that_resumes_during_the_nested_archive_keeps_the_nested_worktree(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        (inner / 'draft.txt').write_text('keep\n')
        answers = iter([True, False])
        with self.assertRaisesRegex(ValueError, 'resumed'):
            work_workspace.remove_nested(self.workspaces, path.resolve(), self.repo, finished=lambda wait: next(answers))
        self.assertTrue((inner / 'draft.txt').is_file())
        self.assertTrue(self.listed(inner))
        self.assertEqual(self.nested_archives(), [])
        self.assertEqual(list((self.workspaces / 'archive' / 'nested').glob('.*')), [])

    def test_a_nested_worktree_at_a_path_that_git_does_not_ignore_is_removed(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path, folder='checks')
        self.assertEqual(self.retire(), [identity])
        self.assertFalse(inner.exists())

    def test_one_nested_worktree_that_cannot_go_keeps_every_nested_worktree(self):
        _, identity, path, _ = self.outcome()
        first = self.nested_worktree(path, 'first')
        second = self.nested_worktree(path, 'second')
        (first / 'draft.txt').write_text('keep\n')
        git(self.repo, 'worktree', 'lock', str(second))
        self.assertEqual(self.retire(), [])
        # Coconut checks all nested worktrees before it archives or removes any.
        self.assertTrue((first / 'draft.txt').is_file())
        self.assertTrue(self.listed(first))
        self.assertTrue(second.is_dir())
        self.assertEqual(self.nested_archives(), [])

    def test_an_unreadable_registry_keeps_the_nested_worktree(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        with patch('coconut.work_workspace.json.loads', side_effect=[self.registry(), ValueError('broken')]):
            with self.assertRaisesRegex(ValueError, 'cannot read the worktree registry'):
                work_workspace.remove_nested(self.workspaces, path.resolve(), self.repo)
        self.assertTrue(inner.is_dir())

    def test_an_ignored_file_written_before_removal_keeps_the_nested_worktree(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        original = work_workspace.ignored_files
        calls = []
        def late(where, inside=()):
            calls.append(where)
            # The file appears after the first check and before the removal.
            if where == inner.resolve() and len([c for c in calls if c == inner.resolve()]) == 2:
                (inner / '.env').write_text('SECRET=1\n')
            return original(where, inside)
        with patch('coconut.work_workspace.ignored_files', side_effect=late):
            with self.assertRaisesRegex(ValueError, 'changed during retirement'):
                work_workspace.remove_nested(self.workspaces, path.resolve(), self.repo)
        self.assertEqual((inner / '.env').read_text(), 'SECRET=1\n')
        self.assertTrue(self.listed(inner))

    def test_a_doubly_nested_worktree_is_removed_deepest_first(self):
        _, identity, path, _ = self.outcome()
        outer = self.nested_worktree(path, 'outer', folder='checks')
        inner = self.nested_worktree(outer, 'inner', folder='checks')
        self.assertEqual(self.retire(), [identity])
        self.assertFalse(outer.exists())
        self.assertFalse(self.listed(inner))
        self.assertFalse(self.listed(outer))

    def test_a_nested_record_without_its_folder_does_not_keep_the_parent(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        shutil.rmtree(inner)
        self.assertEqual(self.retire(), [identity])

    def test_a_nested_worktree_of_another_outcome_keeps_the_parent(self):
        _, identity, path, _ = self.outcome()
        inner = self.nested_worktree(path)
        saved = self.registry()
        saved['inner-binding'] = dict(path=str(inner), branch='none', repository=str(self.repo), outcome='other')
        save_bindings(self.workspaces / 'worktrees.json', saved)
        self.assertEqual(self.retire(), [])
        self.assertTrue(inner.is_dir())

    @unittest.skipUnless(ENVIRONMENTS_VISIBLE, 'The test needs lsof, launchd, and visible process environments.')
    def test_an_idle_outcome_loses_its_orphans_even_when_its_worktree_cannot_retire(self):
        outcome, identity, path, _ = self.outcome()
        # A locked worktree cannot retire. Its orphan still goes.
        git(self.repo, 'worktree', 'lock', str(path))
        scratch = self.workspaces / outcome
        scratch.mkdir()
        in_worktree, in_scratch = self.orphan(path, outcome), self.orphan(scratch, outcome)
        foreign = self.orphan(scratch, str(uuid4()))
        self.holders.stop()
        with patch.object(retirement, 'ORPHAN_AGE_SECONDS', 0):
            self.assertEqual(self.retire(), [])
        for _ in range(50):
            if not (self.alive(in_worktree) or self.alive(in_scratch)):
                break
            time.sleep(0.1)
        self.assertFalse(self.alive(in_worktree))
        self.assertFalse(self.alive(in_scratch))
        self.assertTrue(self.alive(foreign))
        self.assertNotIn('retired', self.registry()[identity])

    @unittest.skipUnless(ENVIRONMENTS_VISIBLE, 'The test needs lsof, launchd, and visible process environments.')
    def test_an_active_outcome_keeps_its_orphans(self):
        outcome, identity, path, task = self.outcome(status='working')
        pid = self.orphan(path, outcome)
        self.holders.stop()
        with patch.object(retirement, 'ORPHAN_AGE_SECONDS', 0):
            self.assertEqual(self.retire(), [])
        self.assertTrue(self.alive(pid))

    @unittest.skipUnless(ENVIRONMENTS_VISIBLE, 'The test needs lsof, launchd, and visible process environments.')
    def test_an_idle_outcome_with_only_a_scratch_workspace_loses_its_orphans(self):
        outcome = str(uuid4())
        with self.service.transaction():
            self.service._put('tasks', dict(id=str(uuid4()), bot_id=self.bot['id'], _execution_session_id=outcome,
                                            title='Change', status='completed', summary='', created_at=OLD, updated_at=OLD))
        scratch = self.workspaces / outcome
        scratch.mkdir()
        pid = self.orphan(scratch, outcome)
        self.assertFalse((self.workspaces / 'worktrees.json').exists())
        self.holders.stop()
        with patch.object(retirement, 'ORPHAN_AGE_SECONDS', 0):
            self.assertEqual(self.retire(), [])
        for _ in range(50):
            if not self.alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(self.alive(pid))

    @unittest.skipUnless(ENVIRONMENTS_VISIBLE, 'The test needs lsof, launchd, and visible process environments.')
    def test_an_outcome_that_resumes_during_the_scan_keeps_its_orphans(self):
        outcome, identity, path, task = self.outcome()
        git(self.repo, 'worktree', 'lock', str(path))
        pid = self.orphan(path, outcome)
        self.holders.stop()
        scan = work_workspace.holders_by_path

        def resume_then_scan(paths):
            with self.service.transaction():
                self.service._put('tasks', dict(task, status='working', updated_at=RECENT))
            return scan(paths)
        with patch.object(retirement, 'ORPHAN_AGE_SECONDS', 0), \
                patch('coconut.work_workspace.holders_by_path', side_effect=resume_then_scan):
            self.assertEqual(self.retire(), [])
        self.assertTrue(self.alive(pid))

    def test_an_outcome_named_like_a_folder_link_scans_no_scratch_workspace(self):
        scanned = []
        with patch('coconut.work_workspace.holders_by_path', side_effect=lambda paths: scanned.append(set(paths)) or {}):
            self.service._stop_idle_orphans({'.', '..', ''}, {})
        self.assertEqual(scanned, [])

    def test_a_pending_restart_skips_the_orphan_pass(self):
        outcome, identity, path, _ = self.outcome()
        self.service.restart_pending = True
        try:
            with patch('coconut.work_workspace.holders_by_path', return_value={str(path): {12345}}) as scan, \
                    patch('coconut.work_workspace.stop_orphans') as stop:
                self.service._stop_idle_orphans({outcome}, self.registry())
        finally:
            self.service.restart_pending = False
        scan.assert_not_called()
        stop.assert_not_called()

    def test_stop_orphans_reads_each_adb_command_line_once(self):
        table = {300: (1, 99999, '/usr/bin/ruby'), 301: (300, 99999, '/usr/bin/sh'), 302: (301, 99999, '/usr/bin/tee'),
                 400: (1, 99999, '/opt/sdk/platform-tools/adb')}
        with patch('coconut.work_workspace.process_table', return_value=table), \
                patch('coconut.work_workspace.environment_names', return_value=True), \
                patch('coconut.work_workspace.stop', return_value=set()), \
                patch('coconut.work_workspace.arguments', return_value='adb fork-server server') as read:
            self.assertEqual(work_workspace.stop_orphans({300, 400}, 'outcome', 0), {300, 301, 302})
        self.assertEqual(read.call_count, 1)

    def test_a_failed_orphan_check_does_not_stop_retirement(self):
        outcome, identity, path, _ = self.outcome()
        with patch('coconut.work_workspace.holders_by_path', return_value={str(path): {12345}}), \
                patch('coconut.work_workspace.stop_orphans', side_effect=ValueError('Coconut cannot list processes without ps.')):
            self.assertEqual(self.retire(), [identity])
        self.assertIn('retired', self.registry()[identity])

    def test_a_registry_entry_without_a_path_does_not_stop_retirement(self):
        outcome, identity, path, _ = self.outcome()
        saved = self.registry()
        saved['broken'] = dict(outcome=outcome, branch='wt/broken', repository=str(self.repo))
        (self.workspaces / 'worktrees.json').write_text(json.dumps(saved))
        self.assertEqual(self.retire(), [identity])

    def test_only_named_shared_services_are_exempt(self):
        for executable in ('/Applications/OrbStack.app/Contents/MacOS/OrbStack',
                           '/Applications/OrbStack.app/Contents/Frameworks/OrbStack Helper.app/Contents/MacOS/OrbStack Helper',
                           '/Applications/OrbStack.app/Contents/MacOS/vmgr',
                           '/Applications/Docker.app/Contents/MacOS/com.docker.backend',
                           '/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Resources/bin/simdiskimaged',
                           'launchd_sim',
                           '/Library/Developer/CoreSimulator/Volumes/iOS_23A/Library/Developer/CoreSimulator/Profiles/Runtimes/iOS.simruntime/Contents/Resources/RuntimeRoot/usr/libexec/installd'):
            self.assertTrue(work_workspace.shared(executable), executable)
        # The developer toolchain and system programs live in these folders too. Their orphans must go.
        for executable in ('/Library/Java/JavaVirtualMachines/zulu-17.jdk/Contents/Home/bin/java',
                           '/Library/Developer/CommandLineTools/usr/bin/git',
                           '/Library/Frameworks/Python.framework/Versions/3.12/bin/python3',
                           '/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild',
                           '/System/Library/Frameworks/WebKit.framework/XPCServices/WebContent',
                           # A client of a shared service belongs to its turn.
                           '/Applications/OrbStack.app/Contents/MacOS/xbin/docker',
                           '/Applications/OrbStack.app/Contents/MacOS/bin/orbctl',
                           '/Applications/Docker.app/Contents/Resources/bin/docker',
                           '/Applications/Docker.app/Contents/Resources/cli-plugins/docker-compose',
                           '/Users/me/Library/Android/sdk/emulator/qemu/darwin-aarch64/qemu-system-aarch64'):
            self.assertFalse(work_workspace.shared(executable), executable)
        # Only the adb server is shared. An adb client belongs to its turn.
        adb = '/Users/me/Library/Android/sdk/platform-tools/adb'
        with patch('coconut.work_workspace.arguments', return_value='adb -L tcp:5037 fork-server server --reply-fd 4\n'):
            self.assertTrue(work_workspace.shared(adb, 10))
        with patch('coconut.work_workspace.arguments', return_value='adb logcat\n'):
            self.assertFalse(work_workspace.shared(adb, 10))
        with patch('coconut.work_workspace.arguments', return_value='adb nodaemon server\n'):
            self.assertTrue(work_workspace.shared(adb, 10))
        # When ps cannot read the command line, a stop keeps the process, and the process still keeps its path.
        with patch('coconut.work_workspace.arguments', return_value=None):
            self.assertTrue(work_workspace.shared(adb, 10, unknown=True))
            self.assertFalse(work_workspace.shared(adb, 10))
            self.assertEqual(work_workspace.blocking({10: True}, self.root, {10: (1, 50, adb)}), {10})
            with patch('coconut.work_workspace.environment_names', return_value=True), \
                    patch('coconut.work_workspace.process_table', return_value={10: (1, 50, adb)}), \
                    patch('coconut.work_workspace.stop', return_value=set()):
                self.assertEqual(work_workspace.stop_orphans({10}, 'outcome', 0), set())
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout='', returncode=1)):
            self.assertTrue(work_workspace.shared(adb, 10, unknown=True))
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout='adb fork-server server', returncode=0)) as run:
            work_workspace.shared(adb, 10)
        self.assertIn('-ww', run.call_args.args[0])
        self.assertFalse(work_workspace.shared(adb))

    def test_holders_by_path_maps_each_path_from_one_scan(self):
        held, other = self.root / 'held', self.root / 'other'
        held.mkdir()
        other.mkdir()
        listing = b'p10\nfcwd\nn' + os.fsencode(str(held)) + b'/log\np11\nfcwd\nn' + os.fsencode(str(other)) + b'\np12\nfcwd\nn/elsewhere\n'
        self.holders.stop()
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing, returncode=0)) as run:
            self.assertEqual(work_workspace.holders_by_path({str(held), str(other)}), {str(held): {10}, str(other): {11}})
        self.assertEqual(run.call_count, 1)

    def test_a_shared_app_daemon_with_the_outcome_environment_is_never_stopped(self):
        # A turn started OrbStack, so its helper carries the outcome variable and has launchd as its parent.
        table = {100: (1, 99999, '/Applications/OrbStack.app/Contents/Frameworks/OrbStack Helper.app/Contents/MacOS/OrbStack Helper'),
                 101: (100, 99999, '/Applications/OrbStack.app/Contents/MacOS/vmgr'),
                 200: (1, 99999, '/bin/bash'), 201: (200, 99999, '/usr/bin/tee')}
        sent = []
        with patch('coconut.work_workspace.process_table', return_value=table), \
                patch('coconut.work_workspace.environment_names', return_value=True), \
                patch('coconut.work_workspace.stop', side_effect=lambda pids, sig, seconds: sent.append(set(pids)) or set()):
            stopped = work_workspace.stop_orphans({100, 200}, 'outcome', 0)
        self.assertEqual(stopped, {200, 201})
        self.assertTrue(all(not ({100, 101} & group) for group in sent))

    def test_a_shared_service_that_holds_the_worktree_does_not_keep_it(self):
        _, identity, path, _ = self.outcome()
        self.holders.stop()
        server = patch('coconut.work_workspace.arguments', return_value='adb fork-server server')
        server.start()
        self.addCleanup(server.stop)
        def listing(pid, descriptor, target):
            return lambda paths, detail=False: (
                {str(p): ({pid: descriptor == 'cwd'} if detail else {pid}) if str(p) == str(target) else ({} if detail else set())
                 for p in paths})
        # The adb server keeps the cwd of the turn that started it.
        with patch('coconut.work_workspace.holders_by_path', side_effect=listing(500, 'cwd', path.resolve())), \
                patch('coconut.work_workspace.process_table', return_value={500: (1, 99999, '/opt/sdk/platform-tools/adb')}), \
                patch('coconut.work_workspace.stop') as stop:
            self.assertEqual(self.retire(), [identity])
        stop.assert_not_called()
        self.assertIn('retired', self.registry()[identity])
        # An open file of a shared service, and any holder that is not shared, still keep the worktree.
        for pid, descriptor, executable in ((501, '12', '/opt/sdk/platform-tools/adb'), (502, 'cwd', '/usr/bin/ruby')):
            _, other, other_path, _ = self.outcome()
            with patch('coconut.work_workspace.holders_by_path', side_effect=listing(pid, descriptor, other_path.resolve())), \
                    patch('coconut.work_workspace.process_table', return_value={pid: (77, 99999, executable)}):
                self.assertNotIn(other, self.retire())
            self.assertNotIn('retired', self.registry()[other])

    def test_a_busy_service_still_sweeps_unfinished_archives(self):
        self.outcome()
        (self.workspaces / 'archive').mkdir()
        leftover = self.workspaces / 'archive' / '.old.tar.gz.tmp'
        leftover.write_bytes(b'partial')
        with patch.object(self.service, '_finished_outcomes', return_value=None):
            self.assertEqual(self.retire(), [])
        self.assertFalse(leftover.exists())

    def test_an_empty_registry_still_sweeps_unfinished_archives(self):
        (self.workspaces / 'worktrees.json').write_text('{}')
        (self.workspaces / 'archive').mkdir()
        leftover = self.workspaces / 'archive' / '.old.tar.gz.tmp'
        leftover.write_bytes(b'partial')
        self.assertEqual(self.retire(), [])
        self.assertFalse(leftover.exists())

    def test_holders_by_path_matches_only_whole_folders(self):
        held = self.root / 'held'
        held.mkdir()
        listing = b''.join([b'p10\nfcwd\nn' + os.fsencode(str(held)) + b'/deep/file\n',
                            b'p11\nfcwd\nn' + os.fsencode(str(held)) + b'-other/file\n',
                            b'p12\nfcwd\nn' + os.fsencode(str(held)) + b'\n', b'p13\nfcwd\nn/\n'])
        self.holders.stop()
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing, returncode=0)):
            self.assertEqual(work_workspace.holders_by_path({str(held)}), {str(held): {10, 12}})
        # One process with its cwd and an open file in the path holds more than its cwd, in either order.
        for records in ((b'fcwd\nn' + os.fsencode(str(held)) + b'\n', b'f12\nn' + os.fsencode(str(held)) + b'/log\n'),
                        (b'f12\nn' + os.fsencode(str(held)) + b'/log\n', b'fcwd\nn' + os.fsencode(str(held)) + b'\n')):
            both = b'p10\n' + b''.join(records) + b'p11\nfcwd\nn' + os.fsencode(str(held)) + b'\n'
            with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=both, returncode=0)):
                self.assertEqual(work_workspace.holders_by_path({str(held)}, detail=True), {str(held): {10: False, 11: True}})

    def test_a_shared_service_does_not_make_a_path_busy(self):
        server = patch('coconut.work_workspace.arguments', return_value='adb fork-server server')
        server.start()
        self.addCleanup(server.stop)
        held = self.root / 'held'
        held.mkdir()
        listing = b'p10\nfcwd\nn' + os.fsencode(str(held)) + b'\n'
        self.holders.stop()
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing, returncode=0)):
            with patch('coconut.work_workspace.process_table', return_value={10: (1, 50, '/opt/sdk/platform-tools/adb')}):
                self.assertEqual(work_workspace.busy_paths({str(held)}), set())
        # An open file of a shared service keeps the path busy.
        opened = b'p10\nf12\nn' + os.fsencode(str(held)) + b'/data\n'
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=opened, returncode=0)):
            with patch('coconut.work_workspace.process_table', return_value={10: (1, 50, '/opt/sdk/platform-tools/adb')}):
                self.assertEqual(work_workspace.busy_paths({str(held)}), {str(held)})
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing, returncode=0)):
            with patch('coconut.work_workspace.process_table', return_value={10: (1, 50, '/usr/bin/ruby')}):
                self.assertEqual(work_workspace.busy_paths({str(held)}), {str(held)})

    def test_a_system_child_of_an_orphan_stops_and_a_shared_child_stays(self):
        table = {200: (1, 99999, '/usr/bin/safaridriver'), 201: (200, 99999, '/System/Library/Frameworks/WebKit.framework/XPCServices/WebContent'),
                 202: (201, 99999, '/usr/bin/tee')}
        with patch('coconut.work_workspace.process_table', return_value=table), \
                patch('coconut.work_workspace.environment_names', return_value=True), \
                patch('coconut.work_workspace.stop', return_value=set()):
            self.assertEqual(work_workspace.stop_orphans({200}, 'outcome', 0), {200, 201, 202})
        # A shared service stays, even as the child of an orphan.
        table = {300: (1, 99999, '/usr/bin/ruby'), 301: (300, 99999, '/opt/sdk/platform-tools/adb'), 302: (300, 99999, '/usr/bin/tee')}
        with patch('coconut.work_workspace.process_table', return_value=table), \
                patch('coconut.work_workspace.environment_names', return_value=True), \
                patch('coconut.work_workspace.stop', return_value=set()), \
                patch('coconut.work_workspace.arguments', return_value='adb fork-server server'):
            self.assertEqual(work_workspace.stop_orphans({300}, 'outcome', 0), {300, 302})

    def test_process_table_reads_executables_with_spaces(self):
        listing = '  100     1 06-16:25:58 /Applications/OrbStack.app/Contents/MacOS/OrbStack Helper\n  200   100    02:03 /bin/bash\n'
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing)):
            table = work_workspace.process_table()
        self.assertEqual(table[100], (1, 6 * 86400 + 16 * 3600 + 25 * 60 + 58, '/Applications/OrbStack.app/Contents/MacOS/OrbStack Helper'))
        self.assertEqual(table[200], (100, 123, '/bin/bash'))

    def test_elapsed_times_from_ps_become_seconds(self):
        self.assertEqual(work_workspace.elapsed_seconds('05'), 5)
        self.assertEqual(work_workspace.elapsed_seconds('02:03'), 123)
        self.assertEqual(work_workspace.elapsed_seconds('1:02:03'), 3723)
        self.assertEqual(work_workspace.elapsed_seconds('06-16:25:58'), 6 * 86400 + 16 * 3600 + 25 * 60 + 58)

    # A bot keeps clones, review checkouts and evidence in bots/<id>/workspace. Stale entries go.
    def bot_workspace(self):
        folder = self.service.root / 'bots' / self.bot['id'] / 'workspace'
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def age(path, days):
        moment = time.time() - days * 86400
        for place in [path, *path.rglob('*')] if path.is_dir() else [path]:
            os.utime(place, (moment, moment), follow_symlinks=False)

    def clone(self, where, name='clone'):
        origin = where.parent / (name + '-origin.git')
        if not origin.exists():
            git(where.parent, 'init', '-q', '--bare', '-b', 'main', str(origin))
            git(self.repo, 'push', '-q', str(origin), 'HEAD:refs/heads/main')
        git(where.parent, 'clone', '-q', str(origin), str(where))
        return where

    def stale_rule(self, days=14, low=False):
        # os.utime sets the mtime but moves the ctime to now. These tests age entries by mtime, so the age test reads mtime.
        return patch.multiple(retirement, BOT_WORKSPACE_STALE_SECONDS=days * 86400,
                              BOT_WORKSPACE_LOW_DISK_STALE_SECONDS=days * 86400,
                              changed_at=lambda info: info.st_mtime,
                              **({'LOW_DISK_BYTES': 10 ** 18} if low else {}))

    def test_a_bot_workspace_loses_a_stale_file_and_folder_but_keeps_fresh_and_hidden_ones(self):
        workspace = self.bot_workspace()
        (workspace / 'old.log').write_text('old\n')
        (workspace / 'old-evidence').mkdir()
        (workspace / 'old-evidence' / 'notes.md').write_text('old\n')
        (workspace / 'fresh-evidence').mkdir()
        (workspace / 'fresh-evidence' / 'notes.md').write_text('fresh\n')
        (workspace / '.hidden').mkdir()
        (workspace / '.hidden' / 'config').write_text('keep\n')
        for name in ('old.log', 'old-evidence', '.hidden'):
            self.age(workspace / name, 30)
        with self.stale_rule():
            self.retire()
        self.assertFalse((workspace / 'old.log').exists())
        self.assertFalse((workspace / 'old-evidence').exists())
        self.assertTrue((workspace / 'fresh-evidence' / 'notes.md').exists())
        self.assertTrue((workspace / '.hidden' / 'config').exists())
        self.assertFalse((workspace / work_workspace.SCRATCH_TRASH).exists() and any((workspace / work_workspace.SCRATCH_TRASH).iterdir()))

    def test_configured_source_reader_and_bindings_survive_stale_cleanup(self):
        workspace = self.bot_workspace()
        for name in ('reader', 'coverage', 'unrelated'):
            (workspace / name).mkdir()
            (workspace / name / 'old.txt').write_text('old')
            self.age(workspace / name, 30)
        (workspace / 'reader' / 'helper.py').write_text('old')
        self.age(workspace / 'reader', 30)
        reader = workspace / 'reader' / 'old.txt'
        sources = [dict(bot_id=self.bot['id'], command=['python3', str(reader)],
                        bindings='coverage/not-written-yet.json')]
        (self.service.root / 'work-sources.json').write_text(json.dumps(sources))
        with self.stale_rule(low=True):
            self.retire()
        self.assertTrue(reader.exists())
        # A reader keeps the files next to it. A bindings file keeps its folder, but not the old files next to it.
        self.assertTrue((workspace / 'reader' / 'helper.py').exists())
        self.assertTrue((workspace / 'coverage').is_dir())
        self.assertFalse((workspace / 'coverage' / 'old.txt').exists())
        self.assertFalse((workspace / 'unrelated').exists())
        # Relative reader paths are also rooted in the bot workspace.
        sources[0]['command'][1] = 'reader/old.txt'
        (self.service.root / 'work-sources.json').write_text(json.dumps(sources))
        with self.stale_rule():
            self.retire()
        self.assertTrue(reader.exists())
        # Removing a source releases its files for ordinary cleanup.
        (self.service.root / 'work-sources.json').write_text('[]')
        # The deletion of old.txt changed the folder. Age it again.
        self.age(workspace / 'coverage', 30)
        with self.stale_rule():
            self.retire()
        self.assertFalse(reader.exists())
        self.assertFalse((workspace / 'coverage').exists())

    def test_an_empty_scratch_workspace_goes_after_a_day_unless_it_is_in_use(self):
        old = time.time() - 2 * 86400
        def folder(name, age=old):
            place = self.workspaces / name
            place.mkdir()
            os.utime(place, (age, age))
            return place
        idle = folder(str(uuid4()))
        fresh = folder(str(uuid4()), time.time())
        full = folder(str(uuid4()))
        (full / 'notes.txt').write_text('keep')
        os.utime(full, (old, old))
        own = folder(self.bot['id'])
        archive = folder('archive')
        named = folder(str(uuid4()))
        active, _, _, _ = self.outcome(status='queued')
        busy = folder(active)
        saved = {'one': dict(path=str(named / 'worktree'))}
        with patch.object(retirement, 'changed_at', lambda info: info.st_mtime):
            self.service._remove_empty_scratch(self.workspaces, saved, time.time())
        self.assertFalse(idle.exists())
        for kept in (fresh, full, own, archive, named, busy):
            self.assertTrue(kept.exists(), kept)
        # The retirement pass runs the rule.
        idle = folder(str(uuid4()))
        with patch.object(retirement, 'changed_at', lambda info: info.st_mtime):
            self.retire()
        self.assertFalse(idle.exists())
        self.assertTrue(busy.exists())

    def test_a_bot_tmp_entry_goes_after_three_days_while_the_bot_runs_no_turn(self):
        tmp = self.service.root / 'bots' / self.bot['id'] / 'tmp'
        workspace = self.bot_workspace()
        def entries():
            for place in (tmp / 'old-build', workspace / 'old-clone'):
                place.mkdir(parents=True, exist_ok=True)
                (place / 'file.bin').write_bytes(b'x')
                self.age(place, 4)
        entries()
        # A running turn of the bot keeps its tmp entries until the workspace wait.
        _, _, _, task = self.outcome(status='working')
        self.service.running[task['id']] = SimpleNamespace()
        with self.stale_rule():
            self.service._clear_stale_bot_workspaces(time.time(), {}, False)
        self.assertTrue((tmp / 'old-build').exists())
        # A turn that starts during the pass keeps them too.
        self.service.running.pop(task['id'])
        with self.stale_rule(), patch.object(self.service, '_bots_with_turns', side_effect=[set(), {self.bot['id']}]):
            self.service._clear_stale_bot_workspaces(time.time(), {}, False)
        self.assertTrue((tmp / 'old-build').exists())
        # A failed check keeps them.
        with self.stale_rule(), patch.object(self.service, '_bots_with_turns', return_value=None):
            self.service._clear_stale_bot_workspaces(time.time(), {}, False)
        self.assertTrue((tmp / 'old-build').exists())
        # Without a turn, the tmp entry goes. The workspace entry waits the full time.
        with self.stale_rule():
            self.service._clear_stale_bot_workspaces(time.time(), {}, False)
        self.assertFalse((tmp / 'old-build').exists())
        self.assertTrue((workspace / 'old-clone').exists())
        # A fresh tmp entry stays.
        (tmp / 'new').write_text('x')
        with self.stale_rule():
            self.service._clear_stale_bot_workspaces(time.time(), {}, False)
        self.assertTrue((tmp / 'new').exists())

    def test_bare_source_executable_does_not_protect_unrelated_workspace_directory(self):
        workspace = self.bot_workspace()
        (workspace / 'python3').mkdir()
        (workspace / 'python3' / 'old.txt').write_text('old')
        (workspace / 'reader.py').write_text('reader')
        self.age(workspace, 30)
        (self.service.root / 'work-sources.json').write_text(json.dumps([
            dict(bot_id=self.bot['id'], command=['python3', 'reader.py'], bindings='coverage.json')]))
        with self.stale_rule():
            self.retire()
        self.assertFalse((workspace / 'python3').exists())
        self.assertTrue((workspace / 'reader.py').exists())

    def test_invalid_work_sources_keep_bot_files_without_interrupting_worktree_retirement(self):
        workspace = self.bot_workspace()
        (workspace / 'reader.py').write_text('reader')
        self.age(workspace, 30)
        for content in ('{', '{}', '[{}]', '[null]', '[{"bot_id": 1}]'):
            with self.subTest(content=content):
                (self.service.root / 'work-sources.json').write_text(content)
                with self.stale_rule(), self.assertLogs('coconut.worktree_retirement', level='WARNING'):
                    self.service._clear_stale_bot_workspaces(time.time(), {}, False)
                self.assertTrue((workspace / 'reader.py').exists())
        with self.stale_rule(), patch.object(Path, 'read_text', side_effect=FileNotFoundError), \
                self.assertLogs('coconut.worktree_retirement', level='WARNING'):
            self.service._clear_stale_bot_workspaces(time.time(), {}, False)
        self.assertTrue((workspace / 'reader.py').exists())

    def test_a_bot_workspace_judges_the_entries_inside_a_container_that_has_a_fresh_file(self):
        workspace = self.bot_workspace()
        container = workspace / 'reviews'
        for name in ('pr-1', 'pr-2'):
            (container / name).mkdir(parents=True)
            (container / name / 'notes.md').write_text(name)
        self.age(container / 'pr-1', 30)
        with self.stale_rule():
            self.retire()
        self.assertFalse((container / 'pr-1').exists())
        self.assertTrue((container / 'pr-2' / 'notes.md').exists())
        # Only one level inside the container: a stale folder deeper in a fresh one stays.
        (container / 'pr-2' / 'deep').mkdir()
        (container / 'pr-2' / 'deep' / 'old.txt').write_text('old')
        self.age(container / 'pr-2' / 'deep', 30)
        with self.stale_rule():
            self.retire()
        self.assertTrue((container / 'pr-2' / 'deep' / 'old.txt').exists())

    # git worktree remove stops at a read-only folder, but it still deletes the Git record. The folder stays orphaned.
    def read_only_evidence(self, path):
        folder = path / 'evidence' / 'frozen'
        folder.mkdir(parents=True)
        (folder / 'result.md').write_text('result\n')
        os.chmod(folder, 0o555)
        self.addCleanup(ReadOnlyTrashTests.writable, self.root)
        return folder

    def drop_git_record(self, identity):
        shutil.rmtree(self.repo / '.git' / 'worktrees' / identity)

    def test_orphan_record_names_only_a_missing_or_reused_git_record(self):
        _, identity, path, _ = self.outcome()
        binding = self.registry()[identity]
        self.assertIsNone(work_workspace.orphan_record(binding))
        record = self.repo / '.git' / 'worktrees' / identity
        self.drop_git_record(identity)
        self.assertEqual(work_workspace.orphan_record(binding), record)
        # Git gave the name to another folder.
        record.mkdir()
        (record / 'gitdir').write_text(str(self.root / 'elsewhere' / '.git') + '\n')
        self.assertEqual(work_workspace.orphan_record(binding), record)
        (record / 'gitdir').write_text(str(path / '.git') + '\n')
        self.assertIsNone(work_workspace.orphan_record(binding))
        # A missing folder is gone, not orphaned. A main checkout is not a linked worktree.
        self.assertIsNone(work_workspace.orphan_record(dict(binding, path=str(self.root / 'missing'))))
        self.assertIsNone(work_workspace.orphan_record(dict(binding, path=str(self.repo))))

    def test_a_read_only_folder_does_not_leave_an_orphaned_half_checkout(self):
        _, identity, path, task = self.outcome()
        self.pull_request(task, 'merged')
        self.read_only_evidence(path)
        self.assertEqual(self.retire(), [identity])
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(self.repo, 'worktree', 'list'))
        self.assertTrue(self.registry()[identity]['retired']['done'])

    def test_an_orphaned_worktree_is_archived_removed_and_restored(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'tracked.txt').write_text('one\nchanged\n')
        # A removal that stopped part way deleted this file. The restore does not repeat the deletion.
        (path / 'obsolete.txt').unlink()
        (path / 'notes.txt').write_text('untracked\n')
        (path / '.env').write_text('SECRET=1\n')
        (path / 'node_modules' / 'pkg').mkdir(parents=True)
        (path / 'node_modules' / 'pkg' / 'index.js').write_bytes(b'x' * 8192)
        self.read_only_evidence(path)
        inner = path / 'tmp' / 'baseline'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'baseline-branch', str(inner))
        (inner / 'tracked.txt').write_text('nested change\n')
        self.drop_git_record(identity)
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 4096):
            self.assertEqual(self.retire(), [identity])
        self.assertFalse(path.exists())
        listed = git(self.repo, 'worktree', 'list')
        self.assertNotIn(str(path), listed)
        retired = self.registry()[identity]['retired']
        self.assertTrue(retired['done'] and retired['orphaned'])
        self.assertEqual(len(list((self.workspaces / 'archive' / 'nested').glob('baseline-*.tar.gz'))), 1)
        self.assertFalse(any((self.workspaces / work_workspace.TRASH).iterdir()))
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertEqual((restored / 'tracked.txt').read_text(), 'one\nchanged\n')
        self.assertEqual((restored / 'obsolete.txt').read_text(), 'remove me\n')
        self.assertEqual((restored / 'notes.txt').read_text(), 'untracked\n')
        self.assertEqual((restored / 'evidence' / 'frozen' / 'result.md').read_text(), 'result\n')
        self.assertEqual((restored / '.env').read_text(), 'SECRET=1\n')
        self.assertFalse((restored / 'node_modules').exists())

    def test_an_orphaned_worktree_keeps_its_files_under_the_retirement_checks(self):
        outcome, identity, path, task = self.outcome()
        (path / 'notes.txt').write_text('x' * 64)
        self.drop_git_record(identity)
        # A process uses it.
        with patch('coconut.work_workspace.in_use', return_value=True), \
                self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            self.assertEqual(self.retire(), [])
        self.assertTrue(any('A process uses the worktree' in line for line in logs.output))
        # Real untracked work keeps its size limit.
        with patch.object(work_workspace, 'UNTRACKED_LIMIT_BYTES', 10), \
                self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            self.assertEqual(self.retire(), [])
        self.assertTrue(any('untracked files in the worktree are larger' in line for line in logs.output))
        # A nested clone holds work that no archive keeps.
        clone = path / 'tmp' / 'clone'
        clone.mkdir(parents=True)
        git(clone, 'init', '-q')
        (path / 'nested').mkdir()
        git(path / 'nested', 'init', '-q')
        self.assertEqual(self.retire(), [])
        shutil.rmtree(path / 'nested')
        # The clone in the ignored tmp folder still keeps it.
        with self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            self.assertEqual(self.retire(), [])
        self.assertTrue(any('build folder of the worktree holds a Git repository' in line for line in logs.output))
        shutil.rmtree(clone)
        # The outcome works again.
        with self.service.transaction():
            self.service._put('tasks', dict(task, status='working', updated_at=RECENT))
        self.assertEqual(self.retire(), [])
        self.assertEqual((path / 'notes.txt').read_text(), 'x' * 64)
        self.assertNotIn('retired', self.registry()[identity])

    def test_an_orphaned_worktree_with_a_populated_submodule_stays(self):
        _, identity, path, _ = self.outcome()
        sub = path / 'sub'
        sub.mkdir()
        git(sub, 'init', '-q')
        (sub / 'file.txt').write_text('one\n')
        git(sub, 'add', '.')
        self.commit(sub, 'Sub')
        git(path, '-c', 'advice.addEmbeddedRepo=false', 'add', 'sub')
        self.commit(path, 'Add a submodule')
        # The patch of the parent cannot hold this edit.
        (sub / 'file.txt').write_text('local edit\n')
        self.drop_git_record(identity)
        with self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            self.assertEqual(self.retire(), [])
        self.assertTrue(any('contains a submodule' in line for line in logs.output))
        self.assertEqual((sub / 'file.txt').read_text(), 'local edit\n')
        self.assertNotIn('retired', self.registry()[identity])

    def orphan_with_notes(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'notes.txt').write_text('untracked\n')
        self.drop_git_record(identity)
        return outcome, identity, path

    def test_a_stop_before_the_orphan_move_is_finished_by_the_next_pass(self):
        outcome, identity, path = self.orphan_with_notes()
        with patch('coconut.work_workspace.os.rename', side_effect=SystemExit), self.assertRaises(SystemExit):
            self.retire()
        binding = self.registry()[identity]
        self.assertNotIn('retired', binding)
        self.assertIn(work_workspace.ORPHAN_REMOVAL, binding)
        first = Path(binding[work_workspace.ORPHAN_REMOVAL]['archive'])
        self.assertTrue(path.is_dir() and first.is_file())
        self.assertEqual(self.retire(), [identity])
        binding = self.registry()[identity]
        self.assertNotIn(work_workspace.ORPHAN_REMOVAL, binding)
        self.assertTrue(binding['retired']['done'])
        self.assertFalse(path.exists())
        # Only the archive that the binding names stays.
        self.assertEqual(list((self.workspaces / 'archive' / 'done').glob(identity + '*.tar.gz')),
                         [Path(binding['retired']['archive'])])
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertEqual((restored / 'notes.txt').read_text(), 'untracked\n')

    def test_a_stop_after_the_orphan_move_is_finished_by_the_next_pass(self):
        outcome, identity, path = self.orphan_with_notes()
        rename = os.rename

        def move_then_stop(source, target):
            rename(source, target)
            if Path(source) == path:
                raise SystemExit
        with patch('coconut.work_workspace.os.rename', side_effect=move_then_stop), self.assertRaises(SystemExit):
            self.retire()
        self.assertFalse(path.exists())
        self.assertNotIn('retired', self.registry()[identity])
        # The archive is the only copy of the changes now. The size limit of the sweep keeps it.
        pending = Path(self.registry()[identity][work_workspace.ORPHAN_REMOVAL]['archive'])
        with patch.multiple(work_workspace, NESTED_ARCHIVE_LIMIT_BYTES=0, NESTED_ARCHIVE_KEEP_SECONDS=0):
            work_workspace.sweep_temporary(self.workspaces)
        self.assertTrue(pending.is_file())
        self.assertEqual(self.retire(), [identity])
        binding = self.registry()[identity]
        self.assertNotIn(work_workspace.ORPHAN_REMOVAL, binding)
        self.assertTrue(binding['retired']['done'])
        # The pass emptied the trash that the stop left.
        trash = self.workspaces / work_workspace.TRASH
        self.assertFalse(trash.exists() and any(trash.iterdir()))
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertEqual((restored / 'notes.txt').read_text(), 'untracked\n')

    def test_an_orphaned_worktree_whose_branch_is_gone_stays(self):
        _, identity, path, _ = self.outcome()
        self.drop_git_record(identity)
        git(self.repo, 'branch', '-D', self.registry()[identity]['branch'])
        with self.assertLogs('coconut.worktree_retirement', 'WARNING'):
            self.assertEqual(self.retire(), [])
        self.assertTrue(path.exists())

    def test_ignored_caches_do_not_count_toward_the_archive_limit(self):
        (self.repo / '.git' / 'info' / 'exclude').write_text('.next/\n__pycache__/\n/public/vite-test/\n')
        outcome, identity, path, _ = self.outcome()
        for folder in ('.next/cache', 'app/__pycache__', 'public/vite-test'):
            (path / folder).mkdir(parents=True)
            (path / folder / 'big.bin').write_bytes(b'x' * 8192)
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 4096):
            self.assertEqual(self.retire(), [identity])
        restored = Path(acquire(self.workspaces, outcome, str(self.repo))['path'])
        self.assertFalse((restored / '.next').exists())
        self.assertFalse((restored / 'public' / 'vite-test').exists())

    def test_a_protected_path_does_not_keep_the_old_entries_next_to_it(self):
        workspace = self.bot_workspace()
        triage = workspace / 'triage'
        (triage / 'snapshots' / '20261010T000000Z').mkdir(parents=True)
        (triage / 'snapshots' / '20261010T000000Z' / 'mail.json').write_text('{}')
        (triage / 'runs' / 'old').mkdir(parents=True)
        (triage / 'runs' / 'old' / 'run.json').write_text('{}')
        (triage / 'runs' / 'new').mkdir()
        (triage / 'runs' / 'new' / 'run.json').write_text('{}')
        (triage / 'coverage.json').write_text('{}')
        for name in ('snapshots', 'runs/old', 'coverage.json'):
            self.age(triage / name, 30)
        reviews = workspace / 'reviews'
        repository = reviews / 'repo'
        repository.mkdir(parents=True)
        git(repository, 'init', '-q')
        (reviews / 'old-evidence').mkdir()
        (reviews / 'old-evidence' / 'video.mp4').write_bytes(b'x')
        self.age(reviews / 'old-evidence', 30)
        (self.service.root / 'work-sources.json').write_text(json.dumps([
            dict(bot_id=self.bot['id'], command=['python3', 'reader.py'], bindings='triage/coverage.json')]))
        saved = {'binding': dict(path=str(self.root / 'missing'), repository=str(repository), outcome='x', branch='b')}
        with self.stale_rule():
            self.service._clear_stale_bot_workspaces(time.time(), saved, False)
        self.assertFalse((triage / 'snapshots').exists())
        self.assertFalse((triage / 'runs' / 'old').exists())
        self.assertTrue((triage / 'runs' / 'new' / 'run.json').exists())
        self.assertTrue((triage / 'coverage.json').exists())
        self.assertFalse((reviews / 'old-evidence').exists())
        self.assertTrue((repository / '.git').is_dir())

    def test_a_bot_tmp_folder_loses_its_stale_entries(self):
        tmp = self.service.root / 'bots' / self.bot['id'] / 'tmp'
        (tmp / 'TemporaryDirectory.abc').mkdir(parents=True)
        (tmp / 'TemporaryDirectory.abc' / 'data').write_text('old')
        (tmp / 'old.log').write_text('old')
        (tmp / 'fresh.log').write_text('fresh')
        (tmp / '.hidden').write_text('keep')
        for name in ('TemporaryDirectory.abc', 'old.log', '.hidden'):
            self.age(tmp / name, 30)
        with self.stale_rule():
            self.retire()
        self.assertFalse((tmp / 'TemporaryDirectory.abc').exists())
        self.assertFalse((tmp / 'old.log').exists())
        self.assertTrue((tmp / 'fresh.log').exists())
        self.assertTrue((tmp / '.hidden').exists())

    def test_a_low_disk_uses_the_shorter_stale_time_for_bot_workspaces(self):
        workspace = self.bot_workspace()
        (workspace / 'four-days').mkdir()
        (workspace / 'four-days' / 'notes.md').write_text('notes')
        self.age(workspace / 'four-days', 4)
        with patch.multiple(retirement, BOT_WORKSPACE_STALE_SECONDS=14 * 86400, BOT_WORKSPACE_LOW_DISK_STALE_SECONDS=3 * 86400,
                            changed_at=lambda info: info.st_mtime):
            self.retire()
            self.assertTrue((workspace / 'four-days').exists())
            with patch.object(retirement, 'LOW_DISK_BYTES', 10 ** 18):
                self.retire()
        self.assertFalse((workspace / 'four-days').exists())

    def test_a_stale_checkout_goes_only_when_a_remote_has_all_its_work(self):
        workspace = self.bot_workspace()
        clean = self.clone(workspace / 'clean')
        dirty = self.clone(workspace / 'dirty', 'dirty')
        (dirty / 'tracked.txt').write_text('changed\n')
        unpushed = self.clone(workspace / 'unpushed', 'unpushed')
        (unpushed / 'new.txt').write_text('new\n')
        git(unpushed, 'add', 'new.txt')
        self.commit(unpushed, 'Unpushed')
        for name in ('clean', 'dirty', 'unpushed'):
            self.age(workspace / name, 30)
        with self.stale_rule():
            self.retire()
        self.assertFalse(clean.exists())
        self.assertEqual((dirty / 'tracked.txt').read_text(), 'changed\n')
        self.assertTrue((unpushed / 'new.txt').exists())

    def test_a_checkout_inside_a_stale_folder_keeps_the_folder_when_it_has_unpushed_work(self):
        workspace = self.bot_workspace()
        folder = workspace / 'evidence'
        folder.mkdir()
        (folder / 'report.md').write_text('report')
        inner = self.clone(folder / 'clone')
        git(inner, 'checkout', '-q', '-b', 'local-only')
        (inner / 'work.txt').write_text('work\n')
        git(inner, 'add', 'work.txt')
        self.commit(inner, 'Local only')
        self.age(folder, 30)
        with self.stale_rule():
            self.retire()
        self.assertTrue((inner / 'work.txt').exists())
        self.assertTrue((folder / 'report.md').exists())

    def test_a_stale_main_checkout_stays_while_a_worktree_of_it_stays_and_both_go_together(self):
        workspace = self.bot_workspace()
        main = self.clone(workspace / 'repo')
        linked = workspace / 'pr-1'
        git(main, 'worktree', 'add', '-q', '--detach', str(linked))
        self.age(main, 30)
        with self.stale_rule():
            self.retire()
        # The worktree is fresh, so the main checkout stays for it.
        self.assertTrue(main.exists() and linked.exists())
        self.age(linked, 30)
        with self.stale_rule():
            self.retire()
        self.assertFalse(linked.exists())
        self.assertFalse(main.exists())

    def test_a_stale_worktree_goes_and_the_main_checkout_forgets_it(self):
        workspace = self.bot_workspace()
        main = self.clone(workspace / 'repo')
        linked = workspace / 'pr-1'
        git(main, 'worktree', 'add', '-q', '--detach', str(linked))
        self.age(linked, 30)
        with self.stale_rule():
            self.retire()
        self.assertFalse(linked.exists())
        self.assertTrue(main.exists())
        self.assertNotIn('pr-1', git(main, 'worktree', 'list'))

    def test_a_main_checkout_stays_when_git_cannot_list_its_worktrees(self):
        workspace = self.bot_workspace()
        main = self.clone(workspace / 'repo')
        self.age(main, 30)
        with self.stale_rule(), patch.object(retirement, 'linked_worktrees', side_effect=ValueError('git failed')):
            self.retire()
        self.assertTrue((main / 'tracked.txt').exists())

    def test_an_entry_that_changes_after_the_walk_stays(self):
        workspace = self.bot_workspace()
        entry = workspace / 'evidence'
        entry.mkdir()
        (entry / 'report.md').write_text('report')
        self.age(entry, 30)
        walk = retirement.stale_tree

        def walk_then_write(path, stale_before):
            result = walk(path, stale_before)
            (path / 'new.md').write_text('a turn wrote this just now')
            return result
        with self.stale_rule(), patch.object(retirement, 'stale_tree', walk_then_write):
            self.retire()
        self.assertTrue((entry / 'report.md').exists() and (entry / 'new.md').exists())

    def test_trash_that_a_stop_left_in_a_bot_workspace_is_emptied(self):
        workspace = self.bot_workspace()
        (workspace / 'reviews').mkdir()
        for place in (workspace, workspace / 'reviews'):
            (place / work_workspace.SCRATCH_TRASH / 'old-clone-1759000000000000000').mkdir(parents=True)
            (place / work_workspace.SCRATCH_TRASH / 'old-clone-1759000000000000000' / 'file').write_text('left by a stop')
            (place / work_workspace.SCRATCH_TRASH / 'bots-own-notes.txt').write_text('not Coconut\'s')
        self.retire()
        for place in (workspace, workspace / 'reviews'):
            self.assertEqual([item.name for item in (place / work_workspace.SCRATCH_TRASH).iterdir()], ['bots-own-notes.txt'])

    def test_keeping_one_entry_keeps_the_main_checkout_whose_worktree_lies_in_it_whatever_the_order(self):
        # a-main has a worktree in the folder z. The folder also holds a main checkout with a fresh worktree outside,
        # so Coconut keeps z, and then it must keep a-main too, although a-main comes first in the pass.
        workspace = self.bot_workspace()
        a_main = self.clone(workspace / 'a-main', 'a')
        (workspace / 'z').mkdir()
        git(a_main, 'worktree', 'add', '-q', '--detach', str(workspace / 'z' / 'w'))
        holder = self.clone(workspace / 'z' / 'n', 'n')
        git(holder, 'worktree', 'add', '-q', '--detach', str(workspace / 'fresh-wt'))
        self.age(workspace / 'a-main', 30)
        self.age(workspace / 'z', 30)
        with self.stale_rule():
            self.retire()
        self.assertTrue((workspace / 'a-main' / 'tracked.txt').exists())
        self.assertTrue((workspace / 'z' / 'w').exists() and (workspace / 'fresh-wt').exists())

    def test_a_file_that_arrives_with_an_old_mtime_is_not_stale(self):
        workspace = self.bot_workspace()
        arrived = workspace / 'unpacked'
        arrived.mkdir()
        (arrived / 'report.md').write_text('just unpacked')
        # An extract or a copy keeps the old mtime, but the change time is now.
        moment = time.time() - 30 * 86400
        os.utime(arrived / 'report.md', (moment, moment))
        os.utime(arrived, (moment, moment))
        with patch.multiple(retirement, BOT_WORKSPACE_STALE_SECONDS=14 * 86400, BOT_WORKSPACE_LOW_DISK_STALE_SECONDS=14 * 86400):
            self.retire()
        self.assertTrue((arrived / 'report.md').exists())

    def test_a_stale_bare_repository_stays(self):
        workspace = self.bot_workspace()
        bare = workspace / 'mirror.git'
        git(workspace, 'init', '-q', '--bare', str(bare))
        with_submodule = self.clone(workspace / 'with-modules')
        module = with_submodule / '.git' / 'modules' / 'x'
        (module / 'objects').mkdir(parents=True)
        (module / 'refs').mkdir()
        (module / 'HEAD').write_text('ref: refs/heads/main\n')
        for name in ('mirror.git', 'with-modules'):
            self.age(workspace / name, 30)
        with self.stale_rule():
            self.retire()
        self.assertTrue((bare / 'HEAD').exists())
        # A module store inside a checkout is part of the checkout. It does not keep a clean checkout.
        self.assertFalse(with_submodule.exists())

    def test_a_bare_repository_with_a_fresh_change_keeps_its_old_parts(self):
        workspace = self.bot_workspace()
        bare = workspace / 'mirror.git'
        git(workspace, 'init', '-q', '--bare', str(bare))
        (bare / 'FETCH_HEAD').write_text('fresh\n')
        for part in ('HEAD', 'config', 'hooks', 'objects', 'refs'):
            self.age(bare / part, 30)
        with self.stale_rule():
            self.retire()
        for part in ('HEAD', 'config', 'hooks', 'objects', 'refs'):
            self.assertTrue((bare / part).exists(), part)

    def test_the_repository_of_a_binding_stays_although_its_worktree_is_retired(self):
        workspace = self.bot_workspace()
        main = self.clone(workspace / 'repo')
        other = self.clone(workspace / 'other', 'other')
        binding = dict(path=str(self.workspaces / 'gone-worktree'), branch='wt/x', repository=str(main), outcome=str(uuid4()))
        save_bindings(self.workspaces / 'worktrees.json', {'x-binding': binding})
        for name in ('repo', 'other'):
            self.age(workspace / name, 30)
        with self.stale_rule():
            self.retire()
        self.assertTrue((main / 'tracked.txt').exists())
        self.assertFalse(other.exists())

    def test_a_file_written_deep_in_an_entry_after_the_walk_puts_the_entry_back(self):
        workspace = self.bot_workspace()
        entry = workspace / 'evidence'
        (entry / 'a' / 'b').mkdir(parents=True)
        (entry / 'a' / 'b' / 'old.md').write_text('old')
        self.age(entry, 30)
        walk = retirement.stale_tree

        def write_before_the_second_walk(path, stale_before, skip_root=False):
            if skip_root:
                # This is the walk of the moved entry. A worker that held a file writes deep in it.
                (path / 'a' / 'b' / 'new.md').write_text('a worker wrote this through an open file')
            return walk(path, stale_before, skip_root=skip_root)
        with self.stale_rule(), patch.object(retirement, 'stale_tree', write_before_the_second_walk):
            self.retire()
        self.assertEqual(sorted(path.name for path in (entry / 'a' / 'b').iterdir()), ['new.md', 'old.md'])

    def test_a_main_checkout_stays_when_the_worktree_that_needs_it_is_held_by_a_process(self):
        workspace = self.bot_workspace()
        main = self.clone(workspace / 'a-main', 'a')
        linked = workspace / 'z-wt'
        git(main, 'worktree', 'add', '-q', '--detach', str(linked))
        for name in ('a-main', 'z-wt'):
            self.age(workspace / name, 30)
        holders = MagicMock(side_effect=lambda paths, detail=False: {str(p): ({123} if str(p).endswith('z-wt') else set()) for p in paths})
        with self.stale_rule(), patch.object(work_workspace, 'holders_by_path', holders):
            self.retire()
        self.assertTrue(linked.exists() and (main / 'tracked.txt').exists())

    def test_a_stale_checkout_with_an_ignored_file_that_a_build_does_not_make_again_stays(self):
        workspace = self.bot_workspace()
        with_env = self.clone(workspace / 'with-env')
        (with_env / '.env').write_text('SECRET=1\n')
        build_only = self.clone(workspace / 'build-only', 'build-only')
        (build_only / 'node_modules' / 'pkg').mkdir(parents=True)
        (build_only / 'node_modules' / 'pkg' / 'index.js').write_text('module.exports = 1\n')
        for name in ('with-env', 'build-only'):
            self.age(workspace / name, 30)
        with self.stale_rule():
            self.retire()
        self.assertEqual((with_env / '.env').read_text(), 'SECRET=1\n')
        self.assertFalse(build_only.exists())

    def test_an_entry_that_cannot_be_put_back_stays_where_the_trash_sweep_does_not_delete_it(self):
        workspace = self.bot_workspace()
        entry = workspace / 'evidence'
        entry.mkdir()
        # Something took the old path, and a file blocks the keep folder, so both places fail.
        (workspace / '.stale-kept').write_text('a file')
        trash = workspace / work_workspace.SCRATCH_TRASH
        target = trash / ('evidence-1759000000000000000' + retirement.CHECKING)
        (target / 'deep').mkdir(parents=True)
        (target / 'deep' / 'new.md').write_text('changed after the walk')
        with self.assertRaises(OSError):
            retirement.put_back(target, entry)
        retirement.empty_moved(workspace)
        self.assertEqual((target / 'deep' / 'new.md').read_text(), 'changed after the walk')

    def test_a_stop_before_the_second_walk_leaves_an_entry_that_the_next_pass_settles(self):
        workspace = self.bot_workspace()
        (workspace / 'old').mkdir()
        (workspace / 'old' / 'notes.md').write_text('old')
        (workspace / 'changed').mkdir()
        (workspace / 'changed' / 'notes.md').write_text('old')
        self.age(workspace / 'old', 30)
        self.age(workspace / 'changed', 30)
        walk = retirement.stale_tree

        def stop_at_the_second_walk(path, stale_before, skip_root=False):
            if skip_root:
                raise KeyboardInterrupt
            return walk(path, stale_before, skip_root=skip_root)
        with self.stale_rule(), patch.object(retirement, 'stale_tree', stop_at_the_second_walk):
            with self.assertRaises(KeyboardInterrupt):
                self.retire()
        trash = workspace / work_workspace.SCRATCH_TRASH
        waiting = sorted(trash.iterdir())
        self.assertEqual(len(waiting), 1)
        self.assertTrue(waiting[0].name.endswith(retirement.CHECKING))
        # The moved entry changed while the service was down. The sweep must not delete it.
        (waiting[0] / 'notes.md').write_text('a worker wrote this')
        with self.stale_rule():
            self.retire()
        # The changed entry is back at its old place. The other stale entry went in the same pass.
        self.assertEqual((workspace / 'changed' / 'notes.md').read_text(), 'a worker wrote this')
        self.assertFalse((workspace / 'old').exists())

    def test_a_clean_entry_left_under_the_checking_name_is_deleted_by_the_next_pass(self):
        workspace = self.bot_workspace()
        trash = workspace / work_workspace.SCRATCH_TRASH
        item = trash / ('old-1759000000000000000' + retirement.CHECKING)
        item.mkdir(parents=True)
        (item / 'notes.md').write_text('old')
        self.age(item, 30)
        with self.stale_rule():
            self.retire()
            self.retire()
        self.assertEqual(list(trash.iterdir()), [])

    def test_an_unreadable_bot_workspace_does_not_stop_the_pass_for_the_others(self):
        broken = self.service.root / 'bots' / '0000-first' / 'workspace'
        broken.mkdir(parents=True)
        (broken / 'inside').mkdir()
        workspace = self.bot_workspace()
        (workspace / 'old').mkdir()
        (workspace / 'old' / 'notes.md').write_text('old')
        self.age(workspace / 'old', 30)
        broken.chmod(0)
        self.addCleanup(broken.chmod, 0o700)
        with self.stale_rule():
            self.retire()
        self.assertFalse((workspace / 'old').exists())

    def test_an_entry_in_the_keep_folder_ages_out_like_any_other(self):
        workspace = self.bot_workspace()
        kept = workspace / retirement.STALE_KEPT
        (kept / 'evidence-1759000000000000000').mkdir(parents=True)
        (kept / 'evidence-1759000000000000000' / 'notes.md').write_text('kept')
        self.age(kept, 30)
        with self.stale_rule():
            self.retire()
        self.assertFalse((kept / 'evidence-1759000000000000000').exists())

    def test_a_worktree_record_stays_while_its_release_fails_and_goes_when_a_later_pass_settles_it(self):
        workspace = self.bot_workspace()
        main = self.clone(workspace / 'repo')
        linked = workspace / 'pr-1'
        git(main, 'worktree', 'add', '-q', '--detach', str(linked))
        self.age(linked, 30)
        real = os.rename

        def fail_the_release(source, target, *args, **kwargs):
            if str(source).endswith(retirement.CHECKING) and not str(target).endswith(retirement.CHECKING):
                raise OSError('release failed')
            return real(source, target, *args, **kwargs)
        with self.stale_rule(), patch.object(retirement.os, 'rename', fail_the_release):
            self.retire()
        waiting = list((workspace / work_workspace.SCRATCH_TRASH).iterdir())
        self.assertEqual(len(waiting), 1)
        # The worktree may still come back, so its record stays.
        self.assertIn('pr-1', git(main, 'worktree', 'list'))
        with self.stale_rule():
            self.retire()
        self.assertNotIn('pr-1', git(main, 'worktree', 'list'))
        self.assertEqual(list((workspace / work_workspace.SCRATCH_TRASH).iterdir()), [])

    def test_a_worktree_that_a_later_pass_puts_back_is_still_registered(self):
        workspace = self.bot_workspace()
        main = self.clone(workspace / 'repo')
        linked = workspace / 'pr-1'
        git(main, 'worktree', 'add', '-q', '--detach', str(linked))
        self.age(linked, 30)
        real = os.rename

        def fail_the_release(source, target, *args, **kwargs):
            if str(source).endswith(retirement.CHECKING) and not str(target).endswith(retirement.CHECKING):
                raise OSError('release failed')
            return real(source, target, *args, **kwargs)
        with self.stale_rule(), patch.object(retirement.os, 'rename', fail_the_release):
            self.retire()
        (list((workspace / work_workspace.SCRATCH_TRASH).iterdir())[0] / 'new.txt').write_text('changed in between')
        with self.stale_rule():
            self.retire()
        self.assertTrue((linked / 'new.txt').exists())
        self.assertEqual(git(linked, 'rev-parse', '--show-toplevel'), str(linked.resolve()))
        self.assertIn('pr-1', git(main, 'worktree', 'list'))
        self.assertNotIn('prunable', git(main, 'worktree', 'list'))

    def test_a_bot_with_work_in_progress_is_cleaned_too(self):
        workspace = self.bot_workspace()
        (workspace / 'old').mkdir()
        (workspace / 'old' / 'notes.md').write_text('old')
        self.age(workspace / 'old', 30)
        for status in ('working', 'queued', 'scheduled'):
            with self.service.transaction():
                self.service._put('tasks', dict(id=str(uuid4()), bot_id=self.bot['id'], title='Review', status=status,
                                                summary='', created_at=OLD, updated_at=RECENT))
        with self.service.transaction():
            self.service._put('contexts', dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=str(uuid4()), status='working'))
        with self.stale_rule():
            self.retire()
        self.assertFalse((workspace / 'old').exists())

    def test_a_process_that_opens_an_entry_after_the_first_scan_puts_it_back(self):
        workspace = self.bot_workspace()
        (workspace / 'old').mkdir()
        (workspace / 'old' / 'notes.md').write_text('a turn opens this')
        self.age(workspace / 'old', 30)
        # The first scan finds no holder. The scan of the moved entry finds one, as lsof names the new path.
        holders = MagicMock(side_effect=lambda paths, detail=False: {
            str(p): ({123} if str(p).endswith(retirement.CHECKING) else set()) for p in paths})
        with self.stale_rule(), patch.object(work_workspace, 'holders_by_path', holders):
            self.retire()
        self.assertEqual((workspace / 'old' / 'notes.md').read_text(), 'a turn opens this')
        self.assertEqual(list((workspace / work_workspace.SCRATCH_TRASH).iterdir()), [])

    def test_a_main_checkout_goes_back_with_a_worktree_that_the_second_scan_puts_back(self):
        workspace = self.bot_workspace()
        main = self.clone(workspace / 'a-main')
        linked = workspace / 'z-wt'
        git(main, 'worktree', 'add', '-q', '--detach', str(linked))
        for name in ('a-main', 'z-wt'):
            self.age(workspace / name, 30)
        # The first scan finds no holder. The second scan, of the moved entries, finds one in the worktree only.
        holders = MagicMock(side_effect=lambda paths, detail=False: {
            str(p): ({123} if retirement.CHECKING in str(p) and Path(str(p)).name.startswith('z-wt-') else set())
            for p in paths})
        with self.stale_rule(), patch.object(work_workspace, 'holders_by_path', holders):
            self.retire()
        self.assertTrue((linked / '.git').exists() and (main / 'tracked.txt').exists())
        self.assertEqual(list((workspace / work_workspace.SCRATCH_TRASH).iterdir()), [])
        self.assertNotIn('prunable', git(main, 'worktree', 'list'))

    def test_a_failed_scan_does_not_stop_the_trash_of_released_entries_from_going(self):
        workspace = self.bot_workspace()
        trash = workspace / work_workspace.SCRATCH_TRASH
        released = trash / 'old-1759000000000000000'
        waiting = trash / ('other-1759000000000000001' + retirement.CHECKING)
        for folder in (released, waiting):
            folder.mkdir(parents=True)
            (folder / 'notes.md').write_text('old')
        self.age(waiting, 30)
        with self.stale_rule(), patch.object(work_workspace, 'holders_by_path', side_effect=ValueError('lsof failed')):
            self.retire()
        self.assertFalse(released.exists())
        self.assertTrue((waiting / 'notes.md').exists())

    def test_a_main_checkout_stays_when_the_release_of_its_worktree_fails(self):
        workspace = self.bot_workspace()
        main = self.clone(workspace / 'a-main')
        linked = workspace / 'z-wt'
        git(main, 'worktree', 'add', '-q', '--detach', str(linked))
        for name in ('a-main', 'z-wt'):
            self.age(workspace / name, 30)
        real = os.rename

        def fail_the_release_of_the_worktree(source, target, *args, **kwargs):
            if Path(str(source)).name.startswith('z-wt-') and not str(target).endswith(retirement.CHECKING):
                raise OSError('release failed')
            return real(source, target, *args, **kwargs)
        with self.stale_rule(), patch.object(retirement.os, 'rename', fail_the_release_of_the_worktree):
            self.retire()
        trash = workspace / work_workspace.SCRATCH_TRASH
        # The worktree waits under the checking name, and the main checkout is back at its place with its objects.
        self.assertEqual(len(list(trash.iterdir())), 1)
        self.assertTrue(list(trash.iterdir())[0].name.startswith('z-wt-'))
        self.assertTrue((main / 'tracked.txt').exists())

    def test_a_waiting_main_checkout_goes_back_with_a_held_worktree_and_both_go_when_nothing_holds_them(self):
        workspace = self.bot_workspace()
        main = self.clone(workspace / 'a-main')
        linked = workspace / 'z-wt'
        git(main, 'worktree', 'add', '-q', '--detach', str(linked))
        for name in ('a-main', 'z-wt'):
            self.age(workspace / name, 30)
        trash = workspace / work_workspace.SCRATCH_TRASH
        trash.mkdir()
        os.rename(main, trash / ('a-main-1759000000000000001' + retirement.CHECKING))
        os.rename(linked, trash / ('z-wt-1759000000000000002' + retirement.CHECKING))
        held = MagicMock(side_effect=lambda paths, detail=False: {
            str(p): ({123} if Path(str(p)).name.startswith('z-wt-') else set()) for p in paths})
        with self.stale_rule(), patch.object(work_workspace, 'holders_by_path', held):
            self.retire()
        # The held worktree is back, so the main checkout is back with it.
        self.assertTrue((linked / '.git').exists() and (main / 'tracked.txt').exists())
        self.assertEqual(list(trash.iterdir()), [])
        with self.stale_rule():
            self.retire()
            self.retire()
        self.assertFalse(linked.exists() or main.exists())

    def test_a_waiting_main_checkout_goes_back_when_a_worktree_of_it_lives(self):
        workspace = self.bot_workspace()
        main = self.clone(workspace / 'a-main')
        linked = workspace / 'z-wt'
        git(main, 'worktree', 'add', '-q', '--detach', str(linked))
        self.age(workspace / 'a-main', 30)
        trash = workspace / work_workspace.SCRATCH_TRASH
        trash.mkdir()
        os.rename(main, trash / ('a-main-1759000000000000001' + retirement.CHECKING))
        with self.stale_rule():
            self.retire()
        # The worktree is fresh and lives at its place, so the main checkout returns to its place too.
        self.assertTrue((main / 'tracked.txt').exists())
        self.assertEqual(list(trash.iterdir()), [])

    def test_an_unrelated_waiting_entry_does_not_keep_a_main_checkout(self):
        workspace = self.bot_workspace()
        trash = workspace / work_workspace.SCRATCH_TRASH
        trash.mkdir()
        (trash / ('a-main-1759000000000000001' + retirement.CHECKING)).mkdir()
        self.assertFalse(retirement.waiting_checking(workspace / 'a'))
        self.assertTrue(retirement.waiting_checking(workspace / 'a-main'))

    def test_a_failed_scan_of_the_moved_entries_leaves_them_under_the_checking_name(self):
        workspace = self.bot_workspace()
        (workspace / 'old').mkdir()
        (workspace / 'old' / 'notes.md').write_text('old')
        self.age(workspace / 'old', 30)
        calls = []

        def scan(paths, detail=False):
            calls.append(sorted(paths))
            if any(str(p).endswith(retirement.CHECKING) for p in paths):
                raise ValueError('lsof did not list all open files.')
            return {str(p): set() for p in paths}
        with self.stale_rule(), patch.object(work_workspace, 'holders_by_path', scan):
            self.retire()
        waiting = list((workspace / work_workspace.SCRATCH_TRASH).iterdir())
        self.assertEqual(len(waiting), 1)
        self.assertTrue(waiting[0].name.endswith(retirement.CHECKING))
        self.assertEqual((waiting[0] / 'notes.md').read_text(), 'old')

    def test_an_entry_waiting_under_the_checking_name_goes_back_while_a_process_holds_it(self):
        workspace = self.bot_workspace()
        item = workspace / work_workspace.SCRATCH_TRASH / ('old-1759000000000000000' + retirement.CHECKING)
        item.mkdir(parents=True)
        (item / 'notes.md').write_text('a turn uses this')
        self.age(item, 30)
        holders = MagicMock(side_effect=lambda paths, detail=False: {str(p): {123} for p in paths})
        with self.stale_rule(), patch.object(work_workspace, 'holders_by_path', holders):
            self.retire()
        self.assertEqual((workspace / 'old' / 'notes.md').read_text(), 'a turn uses this')
        self.assertFalse(item.exists())

    def test_a_bot_workspace_entry_that_a_process_holds_stays(self):
        workspace = self.bot_workspace()
        (workspace / 'busy').mkdir()
        (workspace / 'busy' / 'notes.md').write_text('notes')
        self.age(workspace / 'busy', 30)
        holders = MagicMock(side_effect=lambda paths, detail=False: {str(p): {123} for p in paths})
        with self.stale_rule(), patch.object(work_workspace, 'holders_by_path', holders):
            self.retire()
        self.assertTrue((workspace / 'busy' / 'notes.md').exists())

    def test_a_linked_bot_workspace_and_a_linked_entry_move_nothing(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('keep')
        self.age(outside, 30)
        workspace = self.bot_workspace()
        (workspace / 'link').symlink_to(outside, target_is_directory=True)
        with self.stale_rule():
            self.retire()
        self.assertTrue((outside / 'keep.txt').exists())
        self.assertTrue((workspace / 'link').is_symlink())

    def test_a_read_only_build_folder_is_deleted_from_the_trash(self):
        identity, path = self.refused_with_build_folders()
        ReadOnlyTrashTests.go_module_cache(path / 'tmp' / 'cache' / 'go')
        self.addCleanup(ReadOnlyTrashTests.writable, self.workspaces)
        freed = work_workspace.free_rebuildable(self.workspaces, identity, lambda wait: True)
        self.assertIn('tmp', freed)
        self.assertFalse((path / 'tmp').exists())
        self.assertEqual(list((self.workspaces / work_workspace.TRASH).iterdir()), [])


class ReadOnlyTrashTests(unittest.TestCase):
    """Go writes its module cache read-only: folders 0555 and files 0444. The trash sweeps must still delete it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.writable, self.root)
        self.outside = self.root / 'outside'
        self.outside.mkdir()
        (self.outside / 'keep.txt').write_text('keep\n')
        os.chmod(self.outside / 'keep.txt', 0o444)
        os.chmod(self.outside, 0o555)

    @staticmethod
    def writable(root):
        # A failed test must not leave folders that the temporary directory cannot delete.
        for folder, names, _ in os.walk(root):
            for name in names:
                if not os.path.islink(os.path.join(folder, name)):
                    os.chmod(os.path.join(folder, name), 0o700)
        if os.path.isdir(root):
            os.chmod(root, 0o700)

    @staticmethod
    def go_module_cache(top, outside=None):
        """Write a tree in the shape of a Go module cache, then make it read-only as Go does."""
        module = top / 'modules' / 'github.com' / 'mattn' / 'go-runewidth@v0.0.16'
        (module / 'testdata').mkdir(parents=True)
        (module / 'runewidth.go').write_text('package runewidth\n')
        (module / 'testdata' / 'table.txt').write_text('table\n')
        (top / 'cache' / 'download').mkdir(parents=True)
        (top / 'cache' / 'download' / 'list').write_text('v0.0.16\n')
        if outside is not None:
            # A link out of the trash. Its target must not change.
            (module / 'outside-folder').symlink_to(outside, target_is_directory=True)
            (module / 'outside-file').symlink_to(outside / 'keep.txt')
        for folder, names, files in os.walk(top, topdown=False):
            for name in files:
                if not os.path.islink(os.path.join(folder, name)):
                    os.chmod(os.path.join(folder, name), 0o444)
            os.chmod(folder, 0o555)

    def assert_outside_unchanged(self):
        self.assertEqual((self.outside / 'keep.txt').read_text(), 'keep\n')
        self.assertEqual(stat_mode(self.outside), 0o555)
        self.assertEqual(stat_mode(self.outside / 'keep.txt'), 0o444)
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ['keep.txt'])

    def test_empty_moved_deletes_a_read_only_go_module_cache(self):
        trash = self.root / 'workspace' / work_workspace.SCRATCH_TRASH
        trash.mkdir(parents=True, mode=0o700)
        entry = trash / '2630-evidence-1790797162801517000'
        self.go_module_cache(entry / 'tooling-dbf7e61a', outside=self.outside)
        # A bot's own read-only folder in the trash is not a moved entry, so it stays.
        mine = trash / 'mine'
        self.go_module_cache(mine)
        retirement.empty_moved(self.root / 'workspace')
        self.assertFalse(os.path.lexists(entry))
        self.assertTrue((mine / 'cache' / 'download' / 'list').is_file())
        self.assert_outside_unchanged()

    def test_the_scratch_trash_sweep_deletes_a_read_only_go_module_cache(self):
        trash = self.root / 'workspaces' / work_workspace.SCRATCH_TRASH
        trash.mkdir(parents=True, mode=0o700)
        entry = trash / 'scratch-1790797162801517000'
        self.go_module_cache(entry / 'go', outside=self.outside)
        (trash / 'top-link-1790797162801517001').symlink_to(self.outside, target_is_directory=True)
        work_workspace.empty_scratch_trash(self.root / 'workspaces')
        self.assertEqual(list(trash.iterdir()), [])
        self.assert_outside_unchanged()

    def test_a_folder_that_the_owner_cannot_read_or_enter_is_deleted(self):
        entry = self.root / 'entry'
        (entry / 'closed' / 'inner').mkdir(parents=True)
        (entry / 'closed' / 'inner' / 'file').write_text('x')
        (entry / 'blind' / 'inner').mkdir(parents=True)
        (entry / 'blind' / 'inner' / 'file').write_text('x')
        os.chmod(entry / 'closed', 0o000)
        os.chmod(entry / 'blind', 0o300)
        work_workspace.delete_tree(entry)
        self.assertFalse(os.path.lexists(entry))

    def test_a_link_to_a_read_only_folder_outside_is_deleted_without_a_change_to_its_target(self):
        link = self.root / 'link'
        link.symlink_to(self.outside, target_is_directory=True)
        work_workspace.delete_tree(link)
        self.assertFalse(os.path.lexists(link))
        self.assert_outside_unchanged()

    def test_an_entry_that_a_link_replaces_after_the_check_is_refused_and_its_target_stays(self):
        link = self.root / 'entry-1790797162801517000'
        link.symlink_to(self.outside, target_is_directory=True)
        is_symlink = Path.is_symlink
        # The check sees a folder, and then a link takes its place before the deletion starts.
        with patch.object(Path, 'is_symlink', lambda self: False if self == link else is_symlink(self)):
            with self.assertRaises(OSError):
                work_workspace.delete_tree(link)
        self.assertTrue(link.is_symlink())
        self.assert_outside_unchanged()

    def test_a_refusal_that_a_mode_change_does_not_fix_is_logged_and_the_entry_stays(self):
        trash = self.root / 'workspace' / work_workspace.SCRATCH_TRASH
        trash.mkdir(parents=True, mode=0o700)
        entry = trash / 'locked-1790797162801517000'
        entry.mkdir()
        (entry / 'file').write_text('x')
        unlink = os.unlink

        def refuse(name, *args, **kwargs):
            if os.path.basename(name) == 'file':
                raise PermissionError(errno.EPERM, 'Operation not permitted', name)
            return unlink(name, *args, **kwargs)
        with patch('os.unlink', side_effect=refuse), \
                self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            retirement.empty_moved(self.root / 'workspace')
        self.assertTrue((entry / 'file').is_file())
        self.assertTrue(any('could not delete' in line and 'Operation not permitted' in line for line in logs.output))


def stat_mode(path):
    return os.lstat(path).st_mode & 0o7777


def changed_archive_member(member, **fields):
    member = copy.deepcopy(member)
    for key, value in fields.items():
        setattr(member, key, value)
    return member


class ArchiveFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        work_workspace.git(self.repo, 'init', '-q')
        (self.repo / 'tracked').write_text('original\n')
        work_workspace.git(self.repo, 'add', '.')
        work_workspace.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                      'commit', '-qm', 'Fixture')
        self.saved = self.root / 'registry'
        self.saved.mkdir()
        self.outcome = 'original-owner'
        self.identity = self.outcome + '-' + hashlib.sha256(str(self.repo).encode()).hexdigest()[:16]
        self.path = self.repo / '.worktrees' / self.identity
        self.branch = 'wt/' + self.identity
        work_workspace.git(self.repo, 'worktree', 'add', '-qb', self.branch, str(self.path))
        self.binding = dict(path=str(self.path), repository=str(self.repo), branch=self.branch, outcome=self.outcome)
        work_workspace.validate_binding(self.binding)
        work_workspace.save_bindings(self.saved / 'worktrees.json', {self.identity: self.binding})
        self.head = work_workspace.git(self.path, 'rev-parse', 'HEAD')

    def registry(self):
        return json.loads((self.saved / 'worktrees.json').read_text())

    def files(self):
        # A nested ignore-all file makes real Git list both the directory and children.
        folder = self.path / '.knapsack_pro'
        folder.mkdir()
        (folder / '.gitignore').write_text('*\n')
        (folder / 'result.json').write_bytes(b'{"fixture": true}\n')
        (self.path / 'tracked').write_text('staged\n')
        work_workspace.git(self.path, 'add', 'tracked')
        (self.path / 'tracked').write_text('staged\nunstaged\n')
        (self.path / 'draft').write_text('untracked\n')
        self.changes = work_workspace.changes(self.path)

    def retire(self):
        with patch.object(work_workspace, 'holders', return_value=set()):
            archive = work_workspace.retire(self.saved, self.identity, lambda wait: True)
        self.assertFalse(self.path.exists())
        return archive

    def rewrite(self, archive, transform):
        with tarfile.open(archive) as tar:
            rows = [(copy.deepcopy(member), tar.extractfile(member).read() if member.isfile() else None)
                    for member in tar.getmembers()]
        rows = transform(rows)
        with tarfile.open(archive, 'w:gz') as tar:
            for member, data in rows:
                if data is not None:
                    member.size = len(data)
                tar.addfile(member, io.BytesIO(data) if data is not None else None)

    def legacy_archive(self, *, symlink=False):
        self.files()
        if symlink:
            (self.path / '.knapsack_pro/link').symlink_to('../../outside')
        archive = self.retire()
        def duplicate(rows):
            name = 'ignored/.knapsack_pro/' + ('link' if symlink else '.gitignore')
            # Reproduce old ignored_files' directory expansion followed by Git's child listing.
            first = next(row for row in rows if row[0].name == name)
            return rows + [copy.deepcopy(first)]
        self.rewrite(archive, duplicate)
        return archive

    def saved_failure(self, error='The checkout already contains .knapsack_pro/.gitignore.'):
        saved = self.registry()
        saved[self.identity]['retired']['restore_failed'] = dict(at='2026-09-26T12:00:00Z', error=error)
        work_workspace.save_bindings(self.saved / 'worktrees.json', saved)
        return saved

    def acquire(self):
        return work_workspace.acquire(self.saved, self.outcome, str(self.repo))

    def assert_restored(self, archive):
        restored = self.acquire()
        self.assertNotIn('retired', restored)
        self.assertNotIn('retired', self.registry()[self.identity])
        self.assertEqual({key: restored[key] for key in ('path', 'repository', 'branch', 'outcome')},
                         {key: self.binding[key] for key in ('path', 'repository', 'branch', 'outcome')})
        self.assertEqual(work_workspace.git(self.path, 'rev-parse', 'HEAD'), self.head)
        self.assertEqual(work_workspace.git(self.path, 'branch', '--show-current'), self.branch)
        self.assertEqual(work_workspace.changes(self.path), self.changes)
        self.assertEqual((self.path / '.knapsack_pro/.gitignore').read_bytes(), b'*\n')
        self.assertEqual((self.path / '.knapsack_pro/result.json').read_bytes(), b'{"fixture": true}\n')
        self.assertFalse(archive.exists())
        self.assertEqual(self.acquire(), restored)

    def assert_refused(self, archive, *, saved_unchanged=True):
        saved = self.registry()
        digest = work_workspace.path_digest(archive)
        branch = work_workspace.git(self.repo, 'rev-parse', 'refs/heads/' + self.branch)
        records = work_workspace.registered(self.repo)
        with patch.object(work_workspace, 'discard_checkout', wraps=work_workspace.discard_checkout) as discard:
            with self.assertRaisesRegex(ValueError, 'could not restore|another owner'):
                self.acquire()
            # All these failures must occur before Git creates a checkout.
            discard.assert_not_called()
        self.assertEqual(work_workspace.path_digest(archive), digest)
        self.assertEqual(work_workspace.git(self.repo, 'rev-parse', 'refs/heads/' + self.branch), branch)
        self.assertEqual(work_workspace.registered(self.repo), records)
        if saved_unchanged:
            self.assertEqual(self.registry(), saved)


class ArchiveDuplicateTests(ArchiveFixture):
    def test_real_git_overlap_is_counted_once_and_roundtrips(self):
        self.files()
        listing = work_workspace.git_output(self.path, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z')
        self.assertIn(b'.knapsack_pro/\0', listing)
        self.assertIn(b'.knapsack_pro/.gitignore\0', listing)
        expected = ['.knapsack_pro/.gitignore', '.knapsack_pro/result.json']
        total = sum((self.path / name).stat().st_size for name in expected)
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', total):
            self.assertEqual(sorted(work_workspace.ignored_files(self.path)), expected)
            archive = self.retire()
        with tarfile.open(archive) as tar:
            self.assertEqual(len(tar.getnames()), len(set(tar.getnames())))
        self.assert_restored(archive)

    def test_identical_legacy_duplicates_restore_once(self):
        archive = self.legacy_archive()
        self.assert_restored(archive)

    def test_saved_legacy_collision_is_retried_only_by_same_owner_acquire(self):
        archive = self.legacy_archive()
        self.saved_failure()
        before = self.registry()
        self.assertEqual(work_workspace.preferred(self.saved, self.outcome), self.saved / self.outcome)
        self.assertEqual(work_workspace.preferred(self.saved, self.outcome, verify=False), self.saved / self.outcome)
        self.assertEqual(self.registry(), before)
        self.assertFalse(self.path.exists())
        self.assert_restored(archive)

    def test_saved_legacy_collision_allows_native_empty_directory(self):
        archive = self.legacy_archive()
        self.saved_failure()
        self.path.mkdir()
        self.assert_restored(archive)

    def test_actual_legacy_extraction_failure_rolls_back_then_same_owner_recovers(self):
        archive = self.legacy_archive()
        digest = work_workspace.path_digest(archive)

        def legacy_members(tar):
            # The old extraction loop visited every member without deduplication.
            # Exercise its real collision, rollback, and durable restore_failed write.
            return [(m, m.name.split('/', 1)[1]) for m in tar.getmembers()
                    if m.name.startswith((work_workspace.UNTRACKED, work_workspace.IGNORED))], []

        with patch.object(work_workspace, 'archive_inventory', side_effect=legacy_members):
            with self.assertRaisesRegex(ValueError, 'The checkout already contains .knapsack_pro/.gitignore'):
                self.acquire()
        failure = self.registry()[self.identity]['retired']['restore_failed']
        self.assertEqual(set(failure), {'at', 'error'})
        self.assertEqual(failure['error'], 'The checkout already contains .knapsack_pro/.gitignore.')
        self.assertFalse(self.path.exists())
        self.assertFalse(any(record['path'] == self.path for record in work_workspace.registered(self.repo)))
        self.assertEqual(work_workspace.path_digest(archive), digest)
        # No registry or archive rewrite between the old failure and supported recovery.
        self.assert_restored(archive)

    def test_a_new_failure_during_duplicate_recovery_is_fenced_without_repeated_writes(self):
        archive = self.legacy_archive()
        self.saved_failure()
        digest = work_workspace.path_digest(archive)
        original = work_workspace.git_output

        def refuse_apply(path, *arguments, **options):
            if arguments[0] == 'apply':
                # Even a new failure with the same wording must not authorize another retry.
                raise ValueError('The checkout already contains .knapsack_pro/.gitignore.')
            return original(path, *arguments, **options)

        with patch.object(work_workspace, 'git_output', side_effect=refuse_apply), \
                patch.object(work_workspace, 'discard_checkout', wraps=work_workspace.discard_checkout) as discard:
            with self.assertRaisesRegex(ValueError, 'could not restore'):
                self.acquire()
            discard.assert_called_once()
        self.assertTrue(self.registry()[self.identity]['retired']['restore_failed']['duplicate_retry_failed'])
        self.assertEqual(work_workspace.path_digest(archive), digest)
        self.assertFalse(self.path.exists())
        self.assert_refused(archive)

    def test_truncated_legacy_archive_preserves_the_original_failure_and_branch(self):
        archive = self.legacy_archive()
        self.saved_failure()
        data = archive.read_bytes()
        archive.write_bytes(data[:len(data) // 2])
        self.assert_refused(archive)

    def test_duplicate_acceptance_does_not_overwrite_a_file_from_git_checkout(self):
        archive = self.legacy_archive()

        def tracked_collision(rows):
            member = tarfile.TarInfo('untracked/tracked')
            return rows + [(member, b'overwrite'), (copy.deepcopy(member), b'overwrite')]

        self.rewrite(archive, tracked_collision)
        digest = work_workspace.path_digest(archive)
        with patch.object(work_workspace, 'discard_checkout', wraps=work_workspace.discard_checkout) as discard:
            with self.assertRaisesRegex(ValueError, 'The checkout already contains tracked'):
                self.acquire()
            discard.assert_called_once()
        self.assertFalse(self.path.exists())
        self.assertEqual(work_workspace.path_digest(archive), digest)
        self.assertEqual(work_workspace.git(self.repo, 'show', self.branch + ':tracked'), 'original')

    def test_identical_symlink_duplicates_restore_links_without_following(self):
        archive = self.legacy_archive(symlink=True)
        self.assert_restored(archive)
        self.assertTrue((self.path / '.knapsack_pro/link').is_symlink())
        self.assertEqual(os.readlink(self.path / '.knapsack_pro/link'), '../../outside')
        self.assertFalse((self.path.parent / 'outside').exists())

    def test_saved_failure_on_symlink_duplicates_is_not_regular_collision_proof(self):
        archive = self.legacy_archive(symlink=True)
        self.saved_failure('The checkout already contains .knapsack_pro/link.')
        self.assert_refused(archive)

    def test_conflicting_duplicate_content_and_headers_are_refused_before_extraction(self):
        archive = self.legacy_archive()
        original = archive.read_bytes()
        variants = {
            'content': lambda m, d: (m, b'x\n'),
            'mode': lambda m, d: (m.replace(mode=m.mode ^ 0o100), d),
            'type': lambda m, d: (changed_archive_member(m, type=tarfile.SYMTYPE, linkname='target', size=0), None),
            'linkname': lambda m, d: (m.replace(linkname='different'), d),
            'mtime': lambda m, d: (changed_archive_member(m, mtime=m.mtime + 1, pax_headers={}), d),
            'uid': lambda m, d: (m.replace(uid=m.uid + 1), d),
            'pax': lambda m, d: (changed_archive_member(m, pax_headers=dict(m.pax_headers, comment='different')), d),
            'prefix': lambda m, d: (m.replace(name='untracked/.knapsack_pro/.gitignore'), d),
        }
        for label, change in variants.items():
            with self.subTest(label=label):
                archive.write_bytes(original)
                self.rewrite(archive, lambda rows: rows[:-1] + [change(*rows[-1])])
                self.saved_failure()
                self.assert_refused(archive)

    def test_control_duplicates_unsafe_names_types_and_overlap_are_refused(self):
        archive = self.legacy_archive()
        original = archive.read_bytes()
        names = ('ignored/../escape', 'ignored//absolute', 'ignored/a/../../escape',
                 'ignored/./alias', 'ignored/.git/config', 'ignored/.GIT/config',
                 'ignored/a//b', '/absolute', 'ignored/.knapsack_pro/.gitignore/child',
                 'ignored/.knapsack_pro', 'ignored/')
        for name in names:
            with self.subTest(name=name):
                archive.write_bytes(original)
                self.rewrite(archive, lambda rows: rows + [(tarfile.TarInfo(name), b'unsafe')])
                self.saved_failure()
                self.assert_refused(archive)
        for name in ('retirement.json', *work_workspace.PATCHES):
            with self.subTest(control=name):
                archive.write_bytes(original)
                self.rewrite(archive, lambda rows: rows + [copy.deepcopy(next(row for row in rows if row[0].name == name))])
                self.assert_refused(archive)
        for kind in (tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                archive.write_bytes(original)
                def special(rows):
                    member = tarfile.TarInfo('ignored/special')
                    member.type, member.linkname = kind, 'ignored/.knapsack_pro/.gitignore'
                    return rows + [(member, None)]
                self.rewrite(archive, special)
                self.assert_refused(archive)

    def test_conflicting_symlink_target_is_refused(self):
        archive = self.legacy_archive(symlink=True)
        self.rewrite(archive, lambda rows: rows[:-1] + [(rows[-1][0].replace(linkname='another'), None)])
        self.assert_refused(archive, saved_unchanged=False)

    def test_conflicting_duplicate_is_preflighted_even_without_saved_failure(self):
        archive = self.legacy_archive()
        self.rewrite(archive, lambda rows: rows[:-1] + [(rows[-1][0], b'x\n')])
        self.assert_refused(archive, saved_unchanged=False)
        self.assertIn('conflicting entries', self.registry()[self.identity]['retired']['restore_failed']['error'])

    def test_other_failure_markers_never_authorize_retry(self):
        archive = self.legacy_archive()
        for error in ('not a patch', 'The saved branch moved after retirement.',
                      'Git stopped while it removed the checkout, and part of the checkout remains.',
                      'The checkout already contains tracked.',
                      'The checkout already contains .knapsack_pro/.gitignore. extra'):
            with self.subTest(error=error):
                self.saved_failure(error)
                self.assert_refused(archive)

    def test_collision_message_without_duplicate_is_not_retry_proof(self):
        self.files()
        archive = self.retire()
        self.saved_failure()
        self.assert_refused(archive)

    def test_archive_owner_repository_path_time_branch_and_head_must_match(self):
        archive = self.legacy_archive()
        original = archive.read_bytes()
        self.saved_failure()
        for key in ('outcome', 'repository', 'path', 'at', 'branch', 'head'):
            with self.subTest(key=key):
                archive.write_bytes(original)
                def alter(rows):
                    for index, (member, data) in enumerate(rows):
                        if member.name == 'retirement.json':
                            metadata = json.loads(data)
                            metadata[key] = 'different'
                            rows[index] = (member, json.dumps(metadata).encode())
                    return rows
                self.rewrite(archive, alter)
                self.assert_refused(archive)

    def test_changed_branch_and_deleted_branch_preserve_archive(self):
        archive = self.legacy_archive()
        self.saved_failure()
        tree = work_workspace.git(self.repo, 'rev-parse', self.branch + '^{tree}')
        moved = work_workspace.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                              'commit-tree', tree, '-p', self.branch, '-m', 'Moved')
        work_workspace.git(self.repo, 'update-ref', 'refs/heads/' + self.branch, moved)
        self.assert_refused(archive)
        work_workspace.git(self.repo, 'branch', '-D', self.branch)
        digest, saved = work_workspace.path_digest(archive), self.registry()
        with self.assertRaisesRegex(ValueError, 'could not restore'):
            self.acquire()
        self.assertEqual(work_workspace.path_digest(archive), digest)
        self.assertEqual(self.registry(), saved)
        self.assertFalse(self.path.exists())

    def test_occupied_checkout_is_not_adopted_or_overwritten_on_retry(self):
        archive = self.legacy_archive()
        self.saved_failure()
        self.path.mkdir()
        sentinel = self.path / 'sentinel'
        sentinel.write_bytes(b'keep')
        self.assert_refused(archive)
        self.assertEqual(sentinel.read_bytes(), b'keep')
        sentinel.unlink()
        self.path.rmdir()
        work_workspace.git(self.repo, 'worktree', 'add', str(self.path), self.branch)
        self.assert_refused(archive)
        self.assertEqual((self.path / 'tracked').read_text(), 'original\n')

    def test_empty_and_dangling_symlink_worktree_paths_are_not_followed(self):
        archive = self.legacy_archive()
        self.saved_failure()
        target = self.root / 'empty'
        target.mkdir()
        self.path.symlink_to(target, target_is_directory=True)
        self.assert_refused(archive)
        self.assertEqual(list(target.iterdir()), [])
        self.path.unlink()
        self.path.symlink_to(self.root / 'missing', target_is_directory=True)
        self.assert_refused(archive)
        self.assertTrue(self.path.is_symlink())

    def test_symlink_ancestor_of_an_archived_file_is_refused_before_extraction(self):
        archive = self.legacy_archive()
        self.saved_failure()
        original = archive.read_bytes()
        outside = self.root / 'outside'
        outside.mkdir()
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                archive.write_bytes(original)
                def overlap(rows):
                    link = tarfile.TarInfo('ignored/redirect')
                    link.type, link.linkname = tarfile.SYMTYPE, str(outside)
                    entries = [(link, None), (tarfile.TarInfo('untracked/redirect/file'), b'unsafe')]
                    return rows + (list(reversed(entries)) if reverse else entries)
                self.rewrite(archive, overlap)
                self.assert_refused(archive)
                self.assertEqual(list(outside.iterdir()), [])

    def test_path_occupied_during_archive_validation_is_preserved(self):
        archive = self.legacy_archive()
        self.saved_failure()
        inventory = work_workspace.archive_inventory
        sentinel = self.path / 'sentinel'

        def appear(tar):
            result = inventory(tar)
            self.path.mkdir()
            sentinel.write_bytes(b'keep')
            return result

        with patch.object(work_workspace, 'archive_inventory', side_effect=appear):
            self.assert_refused(archive)
        self.assertEqual(sentinel.read_bytes(), b'keep')

    def test_redirected_parent_of_missing_checkout_is_not_used_for_retry(self):
        archive = self.legacy_archive()
        self.saved_failure()
        target = self.root / 'redirected-parent'
        target.mkdir()
        self.path.parent.rmdir()
        self.path.parent.symlink_to(target, target_is_directory=True)
        self.assert_refused(archive)
        self.assertEqual(list(target.iterdir()), [])

    def test_another_owner_cannot_adopt_the_failed_binding(self):
        archive = self.legacy_archive()
        self.saved_failure()
        before, digest = self.registry(), work_workspace.path_digest(archive)
        with self.assertRaisesRegex(ValueError, 'linked worktree'):
            work_workspace.acquire(self.saved, 'other-owner', str(self.repo), str(self.path))
        self.assertEqual(self.registry(), before)
        self.assertEqual(work_workspace.path_digest(archive), digest)
        self.assertFalse(self.path.exists())
        # Even a mismatched registry key must not grant the current caller another owner's binding.
        corrupt = self.registry()
        corrupt[self.identity]['outcome'] = 'other-owner'
        work_workspace.save_bindings(self.saved / 'worktrees.json', corrupt)
        self.assert_refused(archive)

    def test_archive_writer_and_verifier_refuse_duplicates(self):
        self.files()
        target = self.root / 'test.tar.gz'
        entries = [('ignored/.knapsack_pro/.gitignore', '.knapsack_pro/.gitignore')] * 2
        with self.assertRaisesRegex(ValueError, 'more than once'):
            work_workspace.write_archive(target, {}, self.path, dict.fromkeys(work_workspace.PATCHES, b''), entries)
        self.assertFalse(target.exists())
        archive = self.legacy_archive_for_verifier()
        with self.assertRaisesRegex(ValueError, 'expected files'):
            work_workspace.check_archive(archive, dict(zip(work_workspace.PATCHES, self.changes[:2])), self.path,
                                    [('untracked/draft', 'draft'),
                                     ('ignored/.knapsack_pro/.gitignore', '.knapsack_pro/.gitignore'),
                                     ('ignored/.knapsack_pro/result.json', '.knapsack_pro/result.json')])

    def legacy_archive_for_verifier(self):
        # Construct an archive without removing its source files, to isolate the verifier guard.
        archive = self.root / 'duplicate.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data in [('retirement.json', b'{}'), *zip(work_workspace.PATCHES, self.changes[:2])]:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            for name in ('draft', '.knapsack_pro/.gitignore', '.knapsack_pro/result.json', '.knapsack_pro/.gitignore'):
                tar.add(self.path / name, arcname=('untracked/' if name == 'draft' else 'ignored/') + name)
        return archive


class ArchiveIntentTests(ArchiveFixture):
    def intent_files(self):
        folder = self.path / '.knapsack_pro'
        folder.mkdir()
        (folder / '.gitignore').write_text('*\n')
        (folder / 'marker').write_bytes(b'')
        (self.path / 'tracked').write_text('unstaged edit\n')
        (self.path / 'addition').write_text('intent to add\n')
        work_workspace.git(self.path, 'add', '-N', 'addition')
        self.changes = work_workspace.changes(self.path)
        self.index = work_workspace.git_output(self.path, 'ls-files', '--stage', '-z')
        self.assertEqual(self.changes[0], b'')
        self.assertEqual(self.changes[2], [])

    def intent_archive(self, *, duplicate=True):
        self.intent_files()
        archive = self.retire()
        if duplicate:
            self.rewrite(archive, lambda rows: rows + [copy.deepcopy(next(
                row for row in rows if row[0].name == 'ignored/.knapsack_pro/.gitignore'))])
        return archive

    def intent_marker(self):
        saved = self.registry()
        saved[self.identity]['retired']['restore_failed'] = dict(
            at=RECENT, error='The restored files do not match the archive.', duplicate_retry_failed=True)
        save_bindings(self.saved / 'worktrees.json', saved)
        return saved

    def assert_intent_restored(self, archive):
        restored = self.acquire()
        self.assertNotIn('retired', restored)
        self.assertNotIn('retired', self.registry()[self.identity])
        self.assertEqual(work_workspace.changes(self.path), self.changes)
        self.assertEqual(work_workspace.git_output(self.path, 'ls-files', '--stage', '-z'), self.index)
        self.assertEqual(work_workspace.git(self.path, 'rev-parse', 'HEAD'), self.head)
        self.assertEqual(work_workspace.git(self.path, 'branch', '--show-current'), self.branch)
        self.assertEqual({key: restored[key] for key in ('path', 'repository', 'branch', 'outcome')},
                         {key: self.binding[key] for key in ('path', 'repository', 'branch', 'outcome')})
        self.assertEqual((self.path / 'addition').read_text(), 'intent to add\n')
        self.assertEqual((self.path / '.knapsack_pro/.gitignore').read_bytes(), b'*\n')
        self.assertEqual((self.path / '.knapsack_pro/marker').read_bytes(), b'')
        self.assertFalse(archive.exists())
        self.assertEqual(self.acquire(), restored)

    def test_new_intent_archive_restores_exact_index_and_unstaged_patch(self):
        archive = self.intent_archive(duplicate=False)
        self.assert_intent_restored(archive)

    def test_text_binary_empty_ignored_executable_link_and_literal_intent_paths_roundtrip(self):
        files = {'text': b'text\n', 'empty': b'', 'binary': bytes(range(256)), 'executable': b'#!/bin/sh\n',
                 'sub/space and ü': b'unicode\n', 'tab\tline\nname': b'newline\n',
                 ':(glob)*': b'literal pathspec\n', '-option': b'not an option\n',
                 'ignored/intent': b'ignored but tracked with intent\n'}
        (self.path / '.gitignore').write_text('ignored/\n')
        work_workspace.git(self.path, 'add', '.gitignore')
        for name, data in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (self.path / 'executable').chmod(0o755)
        (self.path / 'link').symlink_to('text')
        names = [*files, 'link']
        work_workspace.git_output(self.path, '--literal-pathspecs', 'add', '-N', '-f',
                                  '--pathspec-from-file=-', '--pathspec-file-nul',
                                  data=b'\0'.join(os.fsencode(name) for name in names) + b'\0')
        before = work_workspace.changes(self.path)
        index = work_workspace.git_output(self.path, 'ls-files', '--stage', '-z')
        archive = self.retire()
        self.acquire()
        self.assertEqual(work_workspace.changes(self.path), before)
        self.assertEqual(work_workspace.git_output(self.path, 'ls-files', '--stage', '-z'), index)
        for name, data in files.items():
            self.assertEqual((self.path / name).read_bytes(), data)
        # Git preserves executable versus non-executable mode, not umask-masked
        # group/other permissions. The exact index modes are compared above.
        self.assertEqual((self.path / 'executable').stat().st_mode & 0o100, 0o100)
        self.assertEqual(os.readlink(self.path / 'link'), 'text')
        self.assertFalse(archive.exists())

    def test_mixed_staged_unstaged_deletions_and_untracked_are_not_restaged(self):
        self.intent_files()
        (self.path / 'tracked').write_text('staged edit\n')
        work_workspace.git(self.path, 'add', 'tracked')
        (self.path / 'tracked').write_text('staged edit\nunstaged edit\n')
        (self.path / 'staged-new').write_text('staged addition\n')
        (self.path / 'staged-empty').write_bytes(b'')
        (self.path / 'staged-removed-in-worktree').write_text('staged only\n')
        work_workspace.git(self.path, 'add', 'staged-new', 'staged-empty', 'staged-removed-in-worktree')
        (self.path / 'staged-new').write_text('staged addition\nunstaged addition\n')
        (self.path / 'staged-removed-in-worktree').unlink()
        (self.path / 'untracked').write_text('must stay untracked\n')
        self.changes = work_workspace.changes(self.path)
        self.index = work_workspace.git_output(self.path, 'ls-files', '--stage', '-z')
        archive = self.retire()
        self.assert_intent_restored(archive)
        self.assertEqual(work_workspace.git(self.path, 'show', ':staged-new'), 'staged addition')
        self.assertEqual(work_workspace.git(self.path, 'show', ':tracked'), 'staged edit')
        self.assertFalse((self.path / 'staged-removed-in-worktree').exists())
        self.assertEqual((self.path / 'untracked').read_text(), 'must stay untracked\n')

    def test_post_pr92_intent_failure_restores_only_via_same_owner_acquire(self):
        archive = self.intent_archive()
        digest = work_workspace.path_digest(archive)
        self.intent_marker()
        before = self.registry()
        self.assertEqual(work_workspace.preferred(self.saved, self.outcome), self.saved / self.outcome)
        self.assertEqual(work_workspace.preferred(self.saved, self.outcome, verify=False), self.saved / self.outcome)
        self.assertEqual(self.registry(), before)
        self.assertEqual(work_workspace.path_digest(archive), digest)
        self.assertFalse(self.path.exists())
        self.assert_intent_restored(archive)

    def test_legacy_intent_recovery_accepts_only_empty_native_destination(self):
        archive = self.intent_archive()
        self.intent_marker()
        self.path.mkdir()
        self.assert_intent_restored(archive)

    def test_digest_failure_without_exact_pr92_marker_stays_fenced(self):
        archive = self.intent_archive()
        original = self.intent_marker()
        valid = original[self.identity]['retired']['restore_failed']
        variants = [dict(at=RECENT, error=valid['error']), dict(valid, duplicate_retry_failed=False),
                    dict(valid, duplicate_retry_failed=1), dict(valid, intent_retry_failed=True),
                    dict(valid, unexpected=True), dict(valid, error=valid['error'] + ' extra'),
                    dict(valid, error='The checkout already contains .knapsack_pro/.gitignore.'),
                    dict(valid, error='Git stopped while it removed the checkout, and part of the checkout remains.'),
                    dict(valid, error='not a patch'), dict(valid, error='The saved branch moved after retirement.')]
        for failure in variants:
            with self.subTest(failure=failure):
                saved = copy.deepcopy(original)
                saved[self.identity]['retired']['restore_failed'] = failure
                save_bindings(self.saved / 'worktrees.json', saved)
                self.assert_refused(archive)

    def test_intent_marker_without_identical_regular_duplicate_is_not_proof(self):
        archive = self.intent_archive(duplicate=False)
        self.intent_marker()
        self.assert_refused(archive)
        def symlinks(rows):
            member = tarfile.TarInfo('ignored/link')
            member.type, member.linkname = tarfile.SYMTYPE, 'target'
            return rows + [(member, None), (copy.deepcopy(member), None)]
        self.rewrite(archive, symlinks)
        self.assert_refused(archive)

    def test_no_unstaged_addition_staged_changes_or_untracked_members_do_not_qualify(self):
        archive = self.intent_archive()
        original, saved = archive.read_bytes(), self.intent_marker()
        # Correctly bind each altered patch set to its digest; this isolates the
        # shape gate rather than merely failing the digest check.
        for variant in ('no-addition', 'empty-unstaged', 'staged', 'untracked'):
            with self.subTest(variant=variant):
                archive.write_bytes(original)
                def alter(rows):
                    for i, (member, data) in enumerate(rows):
                        if member.name == 'unstaged.patch':
                            if variant == 'no-addition':
                                data = b'diff --git a/tracked b/tracked\nindex 4b48dee..c8d7fa7 100644\n--- a/tracked\n+++ b/tracked\n@@ -1 +1 @@\n-original\n+changed\n'
                            elif variant == 'empty-unstaged':
                                data = b''
                            rows[i] = (member, data)
                        elif member.name == 'staged.patch' and variant == 'staged':
                            rows[i] = (member, b'nonempty staged patch')
                    if variant == 'untracked':
                        rows.append((tarfile.TarInfo('untracked/draft'), b'untracked'))
                    return rows
                self.rewrite(archive, alter)
                current = copy.deepcopy(saved)
                with tarfile.open(archive) as tar:
                    parts = [tar.extractfile(name).read() for name in work_workspace.PATCHES]
                current[self.identity]['retired']['changes'] = work_workspace.changes_digest(
                    *parts, ['draft'] if variant == 'untracked' else [])
                save_bindings(self.saved / 'worktrees.json', current)
                self.assert_refused(archive)

    def test_intent_recovery_requires_original_patch_digest_and_archive_binding(self):
        archive = self.intent_archive()
        original = archive.read_bytes()
        saved = self.intent_marker()
        for key in ('outcome', 'repository', 'path', 'at', 'branch', 'head', 'patch', 'changes'):
            with self.subTest(key=key):
                archive.write_bytes(original)
                current = copy.deepcopy(saved)
                if key == 'changes':
                    current[self.identity]['retired']['changes'] = '0' * 64
                else:
                    def alter(rows):
                        for i, (member, data) in enumerate(rows):
                            if key == 'patch' and member.name == 'unstaged.patch':
                                rows[i] = (member, data.replace(b'intent to add', b'other content'))
                            elif key != 'patch' and member.name == 'retirement.json':
                                metadata = json.loads(data)
                                metadata[key] = 'different'
                                rows[i] = (member, json.dumps(metadata).encode())
                        return rows
                    self.rewrite(archive, alter)
                save_bindings(self.saved / 'worktrees.json', current)
                self.assert_refused(archive)

    def test_conflicting_or_truncated_legacy_intent_archive_retains_failure(self):
        archive = self.intent_archive()
        original = archive.read_bytes()
        self.intent_marker()
        self.rewrite(archive, lambda rows: rows[:-1] + [(rows[-1][0], b'different\n')])
        self.assert_refused(archive)
        archive.write_bytes(original[:len(original) // 2])
        self.assert_refused(archive)

    def test_occupied_redirected_moved_branch_and_wrong_owner_remain_fenced(self):
        archive = self.intent_archive()
        self.intent_marker()
        self.path.mkdir()
        (self.path / 'sentinel').write_bytes(b'keep')
        self.assert_refused(archive)
        self.assertEqual((self.path / 'sentinel').read_bytes(), b'keep')
        (self.path / 'sentinel').unlink()
        self.path.rmdir()
        target = self.root / 'redirect'
        target.mkdir()
        self.path.symlink_to(target, target_is_directory=True)
        self.assert_refused(archive)
        self.assertEqual(list(target.iterdir()), [])
        self.path.unlink()
        self.path.parent.rmdir()
        self.path.parent.symlink_to(target, target_is_directory=True)
        self.assert_refused(archive)
        self.assertEqual(list(target.iterdir()), [])
        self.path.parent.unlink()
        self.path.parent.mkdir()
        saved, digest = self.registry(), work_workspace.path_digest(archive)
        with self.assertRaisesRegex(ValueError, 'linked worktree'):
            work_workspace.acquire(self.saved, 'other-owner', str(self.repo), str(self.path))
        self.assertEqual(self.registry(), saved)
        self.assertEqual(work_workspace.path_digest(archive), digest)
        current = copy.deepcopy(saved)
        current[self.identity]['outcome'] = 'other-owner'
        save_bindings(self.saved / 'worktrees.json', current)
        self.assert_refused(archive)
        save_bindings(self.saved / 'worktrees.json', saved)
        tree = work_workspace.git(self.repo, 'rev-parse', self.branch + '^{tree}')
        moved = work_workspace.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                  'commit-tree', tree, '-p', self.branch, '-m', 'Moved')
        work_workspace.git(self.repo, 'update-ref', 'refs/heads/' + self.branch, moved)
        self.assert_refused(archive)

    def test_new_digest_failure_during_intent_recovery_rolls_back_and_never_retries(self):
        self.failed_recovery(intent=True)

    def test_new_digest_failure_during_duplicate_recovery_cannot_pose_as_pr92(self):
        self.failed_recovery(intent=False)

    def failed_recovery(self, *, intent):
        archive = self.intent_archive()
        if intent:
            self.intent_marker()
        else:
            self.saved_failure()
        digest = work_workspace.path_digest(archive)
        apply = work_workspace.apply_unstaged
        def changed(path, unstaged):
            apply(path, unstaged)
            (path / 'tracked').write_text('unexpected change\n')
        with patch.object(work_workspace, 'apply_unstaged', side_effect=changed), \
                patch.object(work_workspace, 'discard_checkout', wraps=work_workspace.discard_checkout) as discard:
            with self.assertRaisesRegex(ValueError, 'The restored files do not match the archive'):
                self.acquire()
            discard.assert_called_once()
        failure = self.registry()[self.identity]['retired']['restore_failed']
        self.assertEqual(work_workspace.path_digest(archive), digest)
        self.assertFalse(self.path.exists())
        self.assertFalse(any(record['path'] == self.path for record in work_workspace.registered(self.repo)))
        self.assert_refused(archive)
        self.assertTrue(failure['intent_retry_failed'])
        self.assertTrue(failure['duplicate_retry_failed'])

    def test_intent_recovery_does_not_overwrite_archived_path_collision(self):
        archive = self.intent_archive()
        self.intent_marker()
        self.rewrite(archive, lambda rows: rows + [(tarfile.TarInfo('ignored/addition'), b'collision')])
        digest = work_workspace.path_digest(archive)
        with self.assertRaisesRegex(ValueError, 'The checkout already contains addition'):
            self.acquire()
        self.assertEqual(work_workspace.path_digest(archive), digest)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.registry()[self.identity]['retired']['restore_failed']['intent_retry_failed'])
        self.assert_refused(archive)


if __name__ == '__main__':
    unittest.main()
