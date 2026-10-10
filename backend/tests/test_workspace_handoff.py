import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from coconut import work_workspace
from coconut.work_workspace import acquire, git, retire, save_bindings, validate_binding
from coconut.workspace_handoff import native_successor

BOT, OTHER_BOT = str(uuid4()), str(uuid4())
PARENT, CHILD, SIBLING = 't_0c7e54ba', 't_a3f2ccbe', 't_37161f6f'


class WorkspaceHandoffTests(unittest.TestCase):
    """A linked native successor continues a retired worktree of an earlier outcome of the same bot."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        (self.repo / 'tracked.txt').write_text('one\n')
        git(self.repo, 'add', '.')
        self.commit(self.repo, 'Initial')
        self.workspaces = self.root / 'workspaces'
        self.workspaces.mkdir(mode=0o700)
        quiet = patch.multiple(work_workspace, holders=MagicMock(side_effect=lambda path, detail=False: {} if detail else set()),
                               process_outcomes=MagicMock(return_value=set()))
        quiet.start()
        self.addCleanup(quiet.stop)
        # The old execution owns a worktree with committed, staged, and untracked work. Retirement archives it.
        self.old, self.new, self.request = str(uuid4()), str(uuid4()), str(uuid4())
        key = hashlib.sha256(str(self.repo).encode()).hexdigest()[:16]
        self.old_identity, self.new_identity = self.old + '-' + key, self.new + '-' + key
        self.path = self.repo / '.worktrees' / self.old_identity
        self.branch = 'wt/' + self.old_identity
        git(self.repo, 'worktree', 'add', '-q', '-b', self.branch, str(self.path))
        (self.path / 'review.txt').write_text('decision notes\n')
        git(self.path, 'add', 'review.txt')
        self.commit(self.path, 'Review')
        (self.path / 'tracked.txt').write_text('one\nstaged\n')
        git(self.path, 'add', 'tracked.txt')
        (self.path / 'draft.txt').write_text('untracked\n')
        self.head = git(self.path, 'rev-parse', 'HEAD')
        binding = dict(path=str(self.path), branch=self.branch, repository=str(self.repo), outcome=self.old)
        validate_binding(binding)
        save_bindings(self.workspaces / 'worktrees.json', {self.old_identity: binding})
        self.archive = retire(self.workspaces, self.old_identity, lambda wait: True)
        self.assertFalse(self.path.exists())
        self.before = self.registry()
        self.database = self.root / 'kanban.db'
        self.native()
        self.claim = dict(native_id=CHILD, run_id=7, claim_lock='claim-lock', external_id=self.request, workspace=str(self.root))

    def commit(self, path, message):
        git(path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-q', '-m', message)

    def registry(self):
        return json.loads((self.workspaces / 'worktrees.json').read_text())

    def native(self):
        # The columns that the proof reads, as Hermes stores them.
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.executescript('CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, tenant TEXT, idempotency_key TEXT, '
                             'created_by TEXT, workspace_path TEXT, current_run_id INTEGER, worker_pid INTEGER, claim_lock TEXT);'
                             'CREATE TABLE task_links (parent_id TEXT, child_id TEXT, PRIMARY KEY (parent_id, child_id));'
                             'CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, ended_at INTEGER);'
                             'CREATE TABLE coconut_bindings (external_id TEXT PRIMARY KEY, native_id TEXT, intent TEXT);')
            db.executemany('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)', [
                (PARENT, 'done', BOT, 'coconut:' + self.old, 'coconut', str(self.path), None, None, None),
                (CHILD, 'running', BOT, f'coconut-followup:{self.request}:input', 'coconut', str(self.workspaces / self.new), 7, os.getpid(), 'claim-lock'),
                (SIBLING, 'done', BOT, 'coconut:' + self.request, 'coconut', str(self.workspaces / self.new), None, None, None),
                # Another bot's child of the old task also names the path. It is not this task's parent.
                ('t_63450249', 'done', OTHER_BOT, 'coconut:' + str(uuid4()), 'default', str(self.path), None, None, None)])
            db.executemany('INSERT INTO task_links VALUES (?,?)', [(PARENT, CHILD), (SIBLING, CHILD), (PARENT, 't_63450249')])
            db.executemany('INSERT INTO task_runs VALUES (?,?,?)', [(1, PARENT, 100), (7, CHILD, None)])
            db.execute('INSERT INTO coconut_bindings VALUES (?,?,?)', (self.request, CHILD, ''))

    def sql(self, statement, *arguments):
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute(statement, arguments)

    def successor(self, claim=None):
        return native_successor(self.database, claim or self.claim, self.request, CHILD, BOT)

    def hand_off(self, path=None, successor=True):
        return acquire(self.workspaces, self.new, str(self.repo), str(path or self.path),
                       successor=self.successor() if successor is True else successor)

    def assert_refused(self, message, **options):
        with self.assertRaisesRegex(ValueError, message):
            self.hand_off(**options)
        # Nothing moved: the binding, its archive, and the empty path stay for an operator or a later proof.
        self.assertEqual(self.registry(), self.before)
        self.assertTrue(self.archive.is_file())
        self.assertFalse(self.path.exists())

    def test_the_retired_path_is_refused_without_native_proof(self):
        # The incident: the successor names the retired path, which retirement removed.
        self.assert_refused('Only a linked worktree can be adopted', successor=None)

    def test_a_linked_successor_takes_and_restores_the_retired_worktree(self):
        binding = self.hand_off()
        self.assertEqual((binding['path'], binding['branch'], binding['outcome']), (str(self.path), self.branch, self.new))
        self.assertEqual(binding['handoff']['parent_native_id'], PARENT)
        self.assertEqual(binding['handoff']['native_id'], CHILD)
        self.assertEqual(binding['handoff']['outcome'], self.old)
        self.assertEqual(binding['handoff']['archive'], str(self.archive))
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD'), self.head)
        self.assertEqual((self.path / 'draft.txt').read_text(), 'untracked\n')
        self.assertEqual((self.path / 'review.txt').read_text(), 'decision notes\n')
        self.assertIn('tracked.txt', git(self.path, 'diff', '--cached', '--name-only'))
        saved = self.registry()
        self.assertEqual(set(saved), {self.new_identity})
        self.assertNotIn('retired', saved[self.new_identity])
        # The archive stays as evidence of the handoff.
        self.assertTrue(self.archive.is_file())
        # The new owner continues it with the usual one-owner rules, and its next retirement works.
        self.assertEqual(acquire(self.workspaces, self.new, str(self.repo))['path'], str(self.path))
        self.assertTrue(retire(self.workspaces, self.new_identity, lambda wait: True).is_file())

    def test_a_promoted_parent_restores_its_retired_worktree(self):
        work = str(uuid4())
        self.sql('UPDATE tasks SET idempotency_key=?, current_run_id=? WHERE id=?',
                 'coconut-outcome:' + work, 9, PARENT)
        self.sql('INSERT INTO coconut_bindings VALUES (?,?,?)', self.old, PARENT, 'migration')
        self.assert_refused('native work in progress')
        self.sql('UPDATE tasks SET current_run_id=NULL WHERE id=?', PARENT)

        binding = self.hand_off()
        assert isinstance(binding, dict)
        assert self.archive is not None

        self.assertEqual(binding['outcome'], self.new)
        self.assertEqual(binding['handoff']['parent_native_id'], PARENT)
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD'), self.head)
        self.assertTrue(self.archive.is_file())

    def test_a_third_outcome_cannot_take_the_handed_worktree(self):
        self.hand_off()
        with self.assertRaisesRegex(ValueError, 'Another outcome owns this worktree'):
            acquire(self.workspaces, str(uuid4()), str(self.repo), str(self.path), successor=self.successor())

    def test_an_unrelated_or_cross_bot_task_is_refused(self):
        self.sql('UPDATE tasks SET tenant=? WHERE id=?', OTHER_BOT, PARENT)
        self.assert_refused('No native parent')
        self.sql('UPDATE tasks SET tenant=? WHERE id=?', BOT, PARENT)
        self.sql('UPDATE tasks SET created_by=? WHERE id=?', 'default', PARENT)
        self.assert_refused('No native parent')
        self.sql('UPDATE tasks SET created_by=? WHERE id=?', 'coconut', PARENT)
        self.sql('UPDATE tasks SET idempotency_key=? WHERE id=?', 'coconut-outcome:' + str(uuid4()), PARENT)
        self.assert_refused('No native parent')
        self.sql('UPDATE tasks SET idempotency_key=? WHERE id=?', 'coconut:' + self.old, PARENT)
        self.sql('DELETE FROM task_links WHERE parent_id=? AND child_id=?', PARENT, CHILD)
        self.assert_refused('No native parent')

    def test_a_forged_path_is_refused(self):
        self.assert_refused('Only a linked worktree', path=self.repo / '.worktrees' / 'forged')
        alias = self.root / 'alias'
        alias.symlink_to(self.repo / '.worktrees')
        self.assert_refused('path does not match', path=alias / self.old_identity)
        self.sql('UPDATE tasks SET workspace_path=? WHERE id=?', str(self.root / 'elsewhere'), PARENT)
        self.assert_refused('another workspace path')

    def test_an_active_prior_owner_is_refused(self):
        self.sql('UPDATE tasks SET status=?, current_run_id=? WHERE id=?', 'running', 9, PARENT)
        self.assert_refused('native work in progress')
        self.sql('UPDATE tasks SET status=?, current_run_id=NULL WHERE id=?', 'done', PARENT)
        self.sql('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)', 't_5c4e0f11', 'scheduled', BOT,
                 f'coconut-followup:{self.old}:later', 'coconut', str(self.path), None, None, None)
        self.assert_refused('native work in progress')
        self.sql('DELETE FROM tasks WHERE id=?', 't_5c4e0f11')
        for found in ({self.old}, None):
            with self.subTest(found=found), patch.object(work_workspace, 'process_outcomes', return_value=found):
                self.assert_refused('a process of its outcome may still run')

    def test_a_divergent_branch_or_head_is_refused(self):
        tree = git(self.repo, 'rev-parse', self.head + '^{tree}')
        moved = git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit-tree', tree, '-p', self.head, '-m', 'Elsewhere')
        git(self.repo, 'update-ref', 'refs/heads/' + self.branch, moved)
        self.assert_refused('branch moved after retirement')

    def test_a_missing_archive_is_refused(self):
        hidden = self.root / 'hidden.tar.gz'
        self.archive.rename(hidden)
        with self.assertRaisesRegex(ValueError, 'archive .* is missing'):
            self.hand_off()
        self.assertEqual(self.registry(), self.before)
        self.assertFalse(self.path.exists())

    def test_ambiguous_ancestry_is_refused(self):
        self.sql('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)', 't_9a9a9a9a', 'done', BOT,
                 f'coconut:{self.old}:second', 'coconut', str(self.path), None, None, None)
        self.sql('INSERT INTO task_links VALUES (?,?)', 't_9a9a9a9a', CHILD)
        self.assert_refused('ambiguous')
        self.sql('UPDATE tasks SET idempotency_key=? WHERE id=?', 'coconut:' + str(uuid4()), 't_9a9a9a9a')
        self.assert_refused('ambiguous')

    def test_a_turn_without_the_native_claim_is_refused(self):
        for change in (dict(claim_lock='stolen'), dict(run_id=8), dict(external_id=str(uuid4())), dict(native_id=SIBLING)):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'does not hold the native claim'):
                self.hand_off(successor=self.successor(dict(self.claim, **change)))
        self.sql('UPDATE tasks SET status=? WHERE id=?', 'done', CHILD)
        self.assert_refused('does not hold the native claim')

    def test_an_unreadable_native_database_is_refused(self):
        # A database error is a failed proof, not an error that escapes the tool.
        locked = sqlite3.OperationalError('database is locked')
        with patch('coconut.workspace_handoff.sqlite3.connect', side_effect=locked):
            self.assert_refused('native task database cannot be read')
        self.sql('DROP TABLE coconut_bindings')
        self.assert_refused('native task database cannot be read')

    def test_a_live_worktree_of_another_outcome_stays_with_its_owner(self):
        # The proof hands over only a retired binding. A live checkout keeps one owner.
        live = self.repo / '.worktrees' / 'live'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'live', str(live))
        saved = self.registry()
        saved['someone-' + self.old_identity.split('-')[-1]] = dict(path=str(live), branch='live', repository=str(self.repo), outcome=self.old)
        save_bindings(self.workspaces / 'worktrees.json', saved)
        with self.assertRaisesRegex(ValueError, 'Another outcome owns this worktree'):
            self.hand_off(path=live)

    def test_a_failed_restore_keeps_the_archive_and_the_old_binding(self):
        self.path.mkdir(parents=True)
        (self.path / 'foreign.txt').write_text('not ours\n')
        with self.assertRaisesRegex(ValueError, 'not empty and does not match'):
            self.hand_off()
        self.assertEqual(self.registry(), self.before)
        self.assertTrue(self.archive.is_file())
        self.assertEqual((self.path / 'foreign.txt').read_text(), 'not ours\n')
        shutil.rmtree(self.path)
        # A later attempt with the same proof succeeds.
        self.assertEqual(self.hand_off()['outcome'], self.new)


if __name__ == '__main__':
    unittest.main()
