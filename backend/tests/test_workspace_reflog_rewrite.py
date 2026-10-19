"""A saved worktree stays continuous when Git gc rewrites its reflog. These checks use real Git and no Hermes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut import work_workspace
from coconut.work_workspace import git, head_log, preferred, validate_binding
import test_finished_routine_workspace as provider_fixtures

IDENTITY = {'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}


def use_test_git(test):
    # Keep the user's Git settings out, and give commits a fixed identity.
    environment = dict(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', **IDENTITY)
    saved = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    test.addCleanup(lambda: [os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
                             for key, value in saved.items()])


def retain_worktree(test, root, saved, outcome):
    """Add a Coconut worktree as Hermes does, and save its binding and checkpoint in `saved`."""
    test.upstream = root / 'upstream'
    test.upstream.mkdir()
    git(test.upstream, 'init', '-b', 'main')
    git(test.upstream, 'commit', '--allow-empty', '-m', 'Base')
    test.repo = root / 'repo'
    git(root, 'clone', '-q', str(test.upstream), str(test.repo))
    # Gc in a test runs in the foreground, so it finishes before the next check.
    git(test.repo, 'config', 'gc.autoDetach', 'false')
    test.saved, test.outcome = saved, outcome
    test.path = test.repo / '.worktrees' / outcome
    test.branch = 'wt/' + outcome
    # Coconut saves the checkpoint right after Hermes adds the worktree, as the retained worktree shows.
    git(test.repo, 'worktree', 'add', '-q', '-b', test.branch, str(test.path))
    test.binding = dict(path=str(test.path), branch=test.branch, repository=str(test.repo), outcome=outcome)
    validate_binding(test.binding)
    test.manifest = saved / 'worktrees.json'
    test.manifest.write_text(json.dumps({outcome + '-key': test.binding}))
    test.checkpoint = dict(test.binding['_head_log'])


class ReflogRewriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_test_git(self)
        (self.root / 'coconut').mkdir()
        retain_worktree(self, self.root, self.root / 'coconut', 'c7f4a250-0000-4000-8000-000000000000')

    def upstream_commit(self, message):
        git(self.upstream, 'commit', '--allow-empty', '-m', message)

    def gc(self):
        # The same rewrite that gc runs for the whole repository, from the main checkout.
        before = head_log(self.path)
        git(self.repo, 'reflog', 'expire', '--all')
        after = head_log(self.path)
        size = self.checkpoint['size']
        # Precondition: the rewrite changed the saved bytes, so the check below does not pass on a raw match.
        self.assertNotEqual(hashlib.sha256(after[:size]).hexdigest(), self.checkpoint['digest'])
        self.assertEqual(after.replace(b'\t\n', b'\n'), before.replace(b'\t\n', b'\n'))

    def retained_history(self):
        # The entries of the retained worktree: a fast-forward, two commits, a rename, an alignment reset, a commit.
        self.upstream_commit('Pull request head')
        git(self.path, 'fetch', '-q', 'origin')
        git(self.path, 'merge', '-q', '--ff-only', 'origin/main')
        self.gc()
        git(self.path, 'commit', '--allow-empty', '-m', 'First scoped change')
        git(self.path, 'commit', '--allow-empty', '-m', 'Second scoped change')
        git(self.path, 'branch', '-m', 'reviewed-name')
        self.upstream_commit('Main moved')
        git(self.path, 'fetch', '-q', 'origin')
        git(self.path, 'reset', '-q', '--hard', 'origin/main')
        git(self.path, 'commit', '--allow-empty', '-m', 'Scoped change on the new base')

    def test_saved_retained_shape_resumes_after_gc_rewrote_the_reflog(self):
        self.retained_history()
        self.assertEqual(preferred(self.saved, self.outcome), self.path)
        saved = json.loads(self.manifest.read_text())[self.outcome + '-key']
        self.assertEqual({k: v for k, v in saved.items() if k != '_head_log'},
                         {k: v for k, v in dict(self.binding, branch='reviewed-name').items() if k != '_head_log'})
        log = head_log(self.path)
        self.assertEqual(saved['_head_log'], dict(size=len(log), digest=hashlib.sha256(log).hexdigest()))
        # The new checkpoint is a raw match, and a later switch still stops the work.
        git(self.path, 'switch', '-q', '-c', 'unexpected')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            preferred(self.saved, self.outcome)

    def test_a_switch_after_the_rewrite_still_stops_the_work(self):
        self.gc()
        git(self.path, 'switch', '-q', '-c', 'unexpected')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            validate_binding(dict(self.binding), allow_rename=True)

    def test_a_rename_needs_allow_rename_after_the_rewrite_too(self):
        self.gc()
        git(self.path, 'branch', '-m', 'reviewed-name')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            validate_binding(dict(self.binding))
        checked = dict(self.binding)
        validate_binding(checked, allow_rename=True)
        self.assertEqual(checked['branch'], 'reviewed-name')

    def test_a_removed_later_entry_stops_the_work(self):
        self.gc()
        git(self.path, 'commit', '--allow-empty', '-m', 'Later change')
        git(self.path, 'branch', '-m', 'reviewed-name')
        git(self.path, 'commit', '--allow-empty', '-m', 'Another change')
        # Git removes one later entry. The saved entries remain, but the history after them has a gap.
        git(self.path, 'reflog', 'delete', 'HEAD@{3}')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            validate_binding(dict(self.binding), allow_rename=True)

    def test_an_unlogged_branch_move_stops_the_work(self):
        self.gc()
        git(self.path, 'branch', '-m', 'reviewed-name')
        git(self.path, 'commit', '--allow-empty', '-m', 'Later change')
        # The main checkout moves the branch. The worktree reflog does not record it, so HEAD differs from its end.
        git(self.repo, 'update-ref', 'refs/heads/reviewed-name', 'reviewed-name~1')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            validate_binding(dict(self.binding), allow_rename=True)

    def test_custom_reflog_expiry_stops_the_work(self):
        self.gc()
        git(self.path, 'branch', '-m', 'reviewed-name')
        # With another expiry setting, a younger entry can be gone.
        git(self.repo, 'config', 'gc.reflogExpireUnreachable', 'now')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            validate_binding(dict(self.binding), allow_rename=True)
        git(self.repo, 'config', '--unset', 'gc.reflogExpireUnreachable')
        git(self.repo, 'config', 'gc.refs/heads/*.reflogExpire', '1.day')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            validate_binding(dict(self.binding), allow_rename=True)

    def test_a_later_entry_older_than_the_expiry_window_stops_the_work(self):
        self.gc()
        old = int(time.time()) - work_workspace.REFLOG_KEEP_SECONDS - 3600
        environment = dict(os.environ, GIT_COMMITTER_DATE=f'{old} +0000')
        subprocess.run(['git', '-C', str(self.path), 'commit', '-q', '--allow-empty', '-m', 'Old change'],
                       env=environment, check=True)
        git(self.path, 'branch', '-m', 'reviewed-name')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            validate_binding(dict(self.binding), allow_rename=True)

    def test_other_changed_saved_bytes_stop_the_work(self):
        self.gc()
        git(self.path, 'branch', '-m', 'reviewed-name')
        log = head_log(self.path)
        # A saved version that differs in more than the tab that Git adds. Its history is unknown.
        first = log.splitlines(keepends=True)[0]
        other = first.replace(b'test@example.invalid', b'other@example.invalid').replace(b'\t\n', b'\n')
        unknown = dict(size=len(other), digest=hashlib.sha256(other).hexdigest())
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            validate_binding(dict(self.binding, _head_log=unknown), allow_rename=True)
        # A checkpoint of another worktree is unknown history too. It starts at another commit, so its entries differ
        # even when both worktrees were added in the same second.
        git(self.repo, 'commit', '--allow-empty', '-m', 'Other base')
        other_path = self.root / 'other'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'other', str(other_path))
        foreign = dict(path=str(other_path), branch='other', repository=str(self.repo), outcome='other')
        validate_binding(foreign)
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            validate_binding(dict(self.binding, _head_log=foreign['_head_log']), allow_rename=True)
        # A missing checkpoint stays closed.
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            validate_binding({k: v for k, v in self.binding.items() if k != '_head_log'}, allow_rename=True)

    def test_a_damaged_reflog_line_stops_the_work(self):
        self.gc()
        git(self.path, 'branch', '-m', 'reviewed-name')
        reflog = Path(git(self.path, 'rev-parse', '--path-format=absolute', '--git-path', 'logs/HEAD'))
        reflog.write_bytes(reflog.read_bytes() + b'not a reflog entry\n')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            validate_binding(dict(self.binding), allow_rename=True)

    def test_a_fetch_that_starts_gc_keeps_the_worktree_continuous(self):
        # A fetch runs `git maintenance run --auto`, and each Git version's default maintenance rewrites the reflogs once
        # its threshold is met. Before Git 2.54, the default runs gc when the repository has too many packs. From Git
        # 2.54, the default ("geometric") has no gc, and its reflog-expire task runs `git reflog expire --all` once HEAD
        # has maintenance.reflog-expire.auto expirable entries, 100 by default. Lower both thresholds.
        git(self.repo, 'config', 'gc.autoPackLimit', '1')
        git(self.repo, 'config', 'maintenance.reflog-expire.auto', '-1')
        git(self.repo, 'config', 'fetch.unpackLimit', '1')
        git(self.repo, 'repack', '-q', '-d')
        self.upstream_commit('Fetched change')
        before = head_log(self.path)
        git(self.repo, 'fetch', '-q', 'origin')
        self.assertNotEqual(head_log(self.path), before, 'The fetch must start gc for this check.')
        git(self.path, 'branch', '-m', 'reviewed-name')
        checked = dict(self.binding)
        validate_binding(checked, allow_rename=True)
        self.assertEqual(checked['branch'], 'reviewed-name')


class RetainedWorktreeProviderTests(unittest.TestCase):
    """The provider checks the saved worktree before a turn, in a real worker process with the shared native stub."""
    upstream_commit = ReflogRewriteTests.upstream_commit
    gc = ReflogRewriteTests.gc
    retained_history = ReflogRewriteTests.retained_history

    def setUp(self):
        provider_fixtures.FinishedRoutineWorkspaceTests.setUp(self)
        use_test_git(self)
        retain_worktree(self, self.root, self.data / 'workspaces', self.execution)

    def run_turn(self):
        task = dict(id=str(uuid4()), title='Continue the accepted change', bot_id=self.bot['id'],
                    _native_turn_id='turn', _native_outcome_id='t_native', _execution_session_id=self.execution)
        task.update(_kanban_external_id=task['id'], _kanban_workspace=str(self.workspace))
        events = []
        self.provider.prepare_kanban_worker(task['id'], self.bot, str(self.workspace), self.execution)
        with patch.object(self.provider, 'observe_bot', return_value=self.bot), patch.object(
                self.provider, 'resolve_task_model', return_value=dict(
                    model='gpt-6-astra', provider='openai-codex', base_url=self.native['base_url'])):
            try:
                self.provider._run_turn('start', task, self.bot, [], lambda kind, data: events.append((kind, data)),
                                        isolated=True)
            finally:
                scopes = Path(str(self.capture) + '.cwd')
                self.rows = [json.loads(line) for line in scopes.read_text().splitlines()] if scopes.exists() else []
        return task, events

    def test_the_turn_starts_in_the_retained_worktree_after_gc_rewrote_its_reflog(self):
        self.retained_history()
        task, events = self.run_turn()
        self.assertIn(('completed', {'summary': 'Complete.'}), events)
        # The worker gives the native session and terminal the retained worktree as cwd.
        self.assertEqual([row[1]['cwd'] for row in self.rows if row[0] == 'session'], [str(self.path)])
        self.assertIn(['override', task['id'], {'cwd': str(self.path)}], self.rows)
        saved = json.loads(self.manifest.read_text())
        self.assertEqual(list(saved), [self.execution + '-key'])
        binding = saved[self.execution + '-key']
        self.assertEqual({k: v for k, v in binding.items() if k != '_head_log'},
                         {k: v for k, v in dict(self.binding, branch='reviewed-name').items() if k != '_head_log'})
        log = head_log(self.path)
        self.assertEqual(binding['_head_log'], dict(size=len(log), digest=hashlib.sha256(log).hexdigest()))

    def test_unknown_history_stops_the_turn_before_the_worker_runs(self):
        self.retained_history()
        # The saved checkpoint names other entries. Nothing proves that this checkout continues them.
        saved = json.loads(self.manifest.read_text())
        other = b'0' * 40 + b' ' + b'1' * 40 + b' Test <test@example.invalid> 1792428026 -0400\n'
        saved[self.execution + '-key']['_head_log'] = dict(size=len(other), digest=hashlib.sha256(other).hexdigest())
        self.manifest.write_text(json.dumps(saved))
        before = self.manifest.read_text()
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            self.run_turn()
        self.assertEqual(self.manifest.read_text(), before)
        self.assertEqual(self.rows, [])
        self.assertFalse(Path(str(self.capture) + '.prompts').exists())


if __name__ == '__main__':
    unittest.main()
