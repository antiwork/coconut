import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4
import test_work_self_recovery as recovery_fixtures

from unittest.mock import patch

from coconut.work_workspace import acquire, git, preferred, retire
import legacy_work_sync
from hermes_env import needs_hermes, SOURCE

setUpModule, tearDownModule = legacy_work_sync.scope()


@needs_hermes
class WorkWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'Initial')
        self.saved = self.root / 'coconut'
        self.old_path = list(sys.path)
        sys.path.insert(0, SOURCE)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), self.old_path))

    def test_native_worktrees_are_distinct_and_dirty_changes_survive_resume(self):
        first, second = str(uuid4()), str(uuid4())
        one = acquire(self.saved, first, str(self.repo))
        two = acquire(self.saved, second, str(self.repo))
        self.assertNotEqual(one['path'], two['path'])
        self.assertNotEqual(one['branch'], two['branch'])
        note = Path(one['path']) / 'changes.txt'
        note.write_text('Uncommitted work')
        self.assertEqual(acquire(self.saved, first, str(self.repo)), one)
        self.assertEqual(note.read_text(), 'Uncommitted work')
        self.assertFalse((Path(two['path']) / note.name).exists())
        self.assertEqual(preferred(self.saved, first), Path(one['path']))
        git(one['path'], 'switch', '-c', 'unexpected')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            acquire(self.saved, first, str(self.repo))
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            preferred(self.saved, first)
        git(one['path'], 'switch', one['branch'])
        git(self.repo, 'worktree', 'move', one['path'], str(self.root / 'moved-worktree'))
        with self.assertRaisesRegex(ValueError, 'missing'):
            preferred(self.saved, first)

    def test_a_retired_native_worktree_returns_with_its_changes(self):
        outcome = str(uuid4())
        original = acquire(self.saved, outcome, str(self.repo))
        note = Path(original['path']) / 'changes.txt'
        note.write_text('Uncommitted work')
        identity = next(iter(json.loads((self.saved / 'worktrees.json').read_text())))
        with patch('coconut.work_workspace.holders', return_value=set()):
            archive = retire(self.saved, identity, lambda wait: True)
        self.assertTrue(archive.is_file())
        self.assertFalse(Path(original['path']).exists())
        resumed = acquire(self.saved, outcome, str(self.repo))
        self.assertEqual((resumed['path'], resumed['branch']), (original['path'], original['branch']))
        self.assertEqual(note.read_text(), 'Uncommitted work')

    def test_verified_branch_rename_keeps_the_same_owner_and_dirty_files(self):
        outcome = str(uuid4())
        original = acquire(self.saved, outcome, str(self.repo))
        note = Path(original['path']) / 'pending.txt'
        note.write_text('Accepted unfinished changes')
        git(original['path'], 'branch', '-m', 'reviewed-change')
        self.assertEqual(preferred(self.saved, outcome), Path(original['path']))
        resumed = acquire(self.saved, outcome, str(self.repo))
        self.assertEqual({k: resumed[k] for k in original if k != '_head_log'},
                         {k: v for k, v in dict(original, branch='reviewed-change').items() if k != '_head_log'})
        self.assertEqual(note.read_text(), 'Accepted unfinished changes')
        manifest = json.loads((self.saved / 'worktrees.json').read_text())
        self.assertEqual(list(manifest.values()), [resumed])
        with self.assertRaisesRegex(ValueError, 'Another outcome'):
            acquire(self.saved, str(uuid4()), str(self.repo), original['path'])
        git(original['path'], 'branch', '-m', 'second-reviewed-name')
        self.assertEqual(preferred(self.saved, outcome), Path(original['path']))
        self.assertEqual(list(json.loads((self.saved / 'worktrees.json').read_text()).values())[0]['branch'], 'second-reviewed-name')
        git(original['path'], 'switch', '--detach')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            preferred(self.saved, outcome)

    def test_an_old_branch_rename_does_not_authorize_a_later_switch(self):
        path = self.root / 'existing'
        git(self.repo, 'worktree', 'add', '-b', 'original', str(path))
        git(path, 'branch', '-m', 'historical-name')
        git(path, 'switch', '-c', 'original')
        outcome = str(uuid4())
        acquire(self.saved, outcome, str(self.repo), str(path))
        git(path, 'switch', 'historical-name')
        git(path, 'branch', '-D', 'original')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            preferred(self.saved, outcome)

    def test_accepted_branch_switch_sets_a_new_rename_checkpoint(self):
        from coconut.work_workspace import validate_binding
        outcome = str(uuid4())
        binding = acquire(self.saved, outcome, str(self.repo))
        git(binding['path'], 'switch', '-c', 'approved-recovery')
        replacement = dict(binding, branch='approved-recovery')
        validate_binding(replacement)
        git(binding['path'], 'branch', '-m', 'approved-published-name')
        validate_binding(replacement, allow_rename=True)
        self.assertEqual(replacement['branch'], 'approved-published-name')

    def test_rename_during_inspection_retries_the_git_snapshot(self):
        from coconut import work_workspace
        from unittest.mock import patch
        outcome = str(uuid4())
        binding = acquire(self.saved, outcome, str(self.repo))
        original = work_workspace.head_log
        calls = 0
        def rename_after_first_read(path):
            nonlocal calls
            result = original(path)
            calls += 1
            if calls == 1:
                git(path, 'branch', '-m', 'concurrent-rename')
            return result
        with patch.object(work_workspace, 'head_log', side_effect=rename_after_first_read):
            self.assertEqual(preferred(self.saved, outcome), Path(binding['path']))
        self.assertEqual(acquire(self.saved, outcome, str(self.repo))['branch'], 'concurrent-rename')

    def test_existing_worktree_is_adopted_once_without_moving_its_branch(self):
        path = self.root / 'existing'
        git(self.repo, 'worktree', 'add', '-b', 'existing-pr', str(path))
        (path / 'pending.txt').write_text('Earlier work')
        outcome = str(uuid4())
        result = acquire(self.saved, outcome, str(self.repo), str(path))
        self.assertEqual(result['path'], str(path.resolve()))
        self.assertEqual(result['branch'], 'existing-pr')
        self.assertEqual((path / 'pending.txt').read_text(), 'Earlier work')
        with self.assertRaisesRegex(ValueError, 'Another outcome'):
            acquire(self.saved, str(uuid4()), str(self.repo), str(path))
        with self.assertRaisesRegex(ValueError, 'linked worktree'):
            acquire(self.saved, str(uuid4()), str(self.repo), str(self.repo))

    def test_multiple_repositories_validate_every_saved_branch(self):
        other = self.root / 'other'
        other.mkdir()
        git(other, 'init')
        git(other, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'Initial')
        outcome = str(uuid4())
        one = acquire(self.saved, outcome, str(self.repo))
        acquire(self.saved, outcome, str(other))
        self.assertEqual(preferred(self.saved, outcome), self.saved / outcome)
        git(one['path'], 'switch', '-c', 'unexpected')
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            preferred(self.saved, outcome)

    def test_retry_cannot_take_a_worktree_adopted_after_an_interrupted_save(self):
        first, second = str(uuid4()), str(uuid4())
        one = acquire(self.saved, first, str(self.repo))
        # Reproduce interruption after Git creation but before the registry commit.
        manifest = self.saved / 'worktrees.json'
        manifest.write_text('{}')
        adopted = acquire(self.saved, second, str(self.repo), one['path'])
        with self.assertRaisesRegex(ValueError, 'Another outcome'):
            acquire(self.saved, first, str(self.repo))
        self.assertEqual(list(json.loads(manifest.read_text()).values()), [adopted])


class RecoveryWorkspaceTests(unittest.TestCase):
    setUp = recovery_fixtures.WorkSelfRecoveryTests.setUp
    tearDown = recovery_fixtures.WorkSelfRecoveryTests.tearDown
    start = recovery_fixtures.WorkSelfRecoveryTests.start
    sync = recovery_fixtures.WorkSelfRecoveryTests.sync
    write = recovery_fixtures.WorkSelfRecoveryTests.write
    stranded = recovery_fixtures.WorkSelfRecoveryTests.stranded
    recover = recovery_fixtures.WorkSelfRecoveryTests.recover

    def test_recovery_keeps_execution_owner_before_replacing_task(self):
        original = self.service._get('tasks', self.task['id'])
        self.service._ensure_execution_session(original)
        item = self.stranded()
        recovered = self.recover()
        self.assertNotEqual(recovered['id'], original['id'])
        self.assertEqual(self.service._get('artifacts', item['id'])['task_id'], recovered['id'])
        self.service._ensure_execution_session(recovered)
        self.assertEqual(recovered['_execution_session_id'], original['_execution_session_id'])

    def test_cancel_and_resume_keeps_execution_owner(self):
        self.start()
        item = self.sync()[0]
        original = self.service._get('tasks', self.task['id'])
        self.service._ensure_execution_session(original)
        self.write('POST', '/v1/work/items/' + item['id'] + '/cancel', {})
        self.service.running.clear()
        self.write('POST', '/v1/work/items/' + item['id'] + '/resume', {})
        resumed = self.service._all('tasks')[-1]
        self.assertNotEqual(resumed['id'], original['id'])
        self.assertEqual(resumed['_execution_session_id'], original['_execution_session_id'])

    def test_chat_reply_to_failed_work_keeps_execution_owner(self):
        self.stranded()
        original = self.service._get('tasks', self.task['id'])
        self.service._ensure_execution_session(original)
        reply = self.write('POST', '/v1/tasks/' + original['id'] + '/replies', {'text': 'Continue the existing change'})
        continued = self.service._get('tasks', reply['id'])
        self.service._ensure_execution_session(continued)
        self.assertNotEqual(continued['id'], original['id'])
        self.assertEqual(continued['_execution_session_id'], original['_execution_session_id'])


class WorkspaceToolTextTests(unittest.TestCase):
    # This guard needs no Git repository or Hermes runtime. The contracts job runs it too.
    def test_the_workspace_tool_tells_bots_to_rename_the_branch(self):
        from coconut.work_workspace import SCHEMA
        # A branch switch stops the work item, and a rename keeps it. The tool must say so to every bot.
        text = SCHEMA['description']
        self.assertIn('rename the worktree branch with git branch -m <name>', text)
        self.assertIn('Do not create or switch branches in the worktree with git checkout -b, git checkout <branch>, '
                      'or git switch, and do not check out a commit without a branch', text)
        self.assertIn('inside this worktree', text)

if __name__ == '__main__':
    unittest.main()
