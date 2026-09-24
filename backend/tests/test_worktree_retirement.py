from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut import work_workspace
from coconut.providers import FixtureProvider
from coconut.service import Service
from coconut.work_workspace import acquire, git, preferred, save_bindings, validate_binding

OLD = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace('+00:00', 'Z')
RECENT = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class WorktreeRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init')
        (self.repo / '.gitignore').write_text('build/\n')
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
        # Most tests do not depend on lsof. One test below checks a real process.
        self.in_use = patch('coconut.work_workspace.in_use', return_value=False)
        self.in_use.start()
        self.addCleanup(self.in_use.stop)

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

        self.assertEqual(self.retire(), [identity])

        self.assertFalse(path.exists())
        self.assertEqual(git(self.repo, 'rev-parse', '--verify', 'refs/heads/wt/' + identity), head)
        binding = self.registry()[identity]
        archive = self.workspaces / 'archive' / (identity + '.tar.gz')
        self.assertEqual(binding['retired']['archive'], str(archive))
        self.assertEqual(binding['retired']['head'], head)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive) as tar:
            self.assertEqual(sorted(tar.getnames()), ['changes.patch', 'retirement.json', 'untracked/notes/draft.md'])
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
        self.assertFalse((path / 'build').exists())
        self.assertTrue(archive.exists())
        self.assertEqual(preferred(self.workspaces, outcome), path)

    def test_preferred_restores_into_an_empty_directory_from_a_native_claim(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'draft.txt').write_text('keep\n')
        self.assertEqual(self.retire(), [identity])
        path.mkdir()
        self.assertEqual(preferred(self.workspaces, outcome), path)
        self.assertEqual((path / 'draft.txt').read_text(), 'keep\n')
        self.assertNotIn('retired', self.registry()[identity])

    def test_unfinished_pending_recent_running_locked_and_native_outcomes_keep_their_worktrees(self):
        kept = []
        kept.append(self.outcome(status='working'))
        kept.append(self.outcome(status='failed'))
        kept.append(self.outcome(updated_at=RECENT))
        pending = self.outcome()
        self.service._put('decisions', dict(id=str(uuid4()), task_id=pending[3]['id'], status='pending'))
        kept.append(pending)
        running = self.outcome()
        self.service.running[running[3]['id']] = SimpleNamespace()
        self.addCleanup(self.service.running.pop, running[3]['id'])
        kept.append(running)
        locked = self.outcome()
        git(self.repo, 'worktree', 'lock', str(locked[2]))
        kept.append(locked)
        native = self.outcome()
        kept.append(native)
        for _, _, path, _ in kept:
            (path / 'draft.txt').write_text('in progress\n')
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={native[3]['id']: dict(id='n1', status='blocked')})):
            self.assertEqual(self.retire(), [])
        for _, identity, path, _ in kept:
            self.assertEqual((path / 'draft.txt').read_text(), 'in progress\n')
            self.assertNotIn('retired', self.registry()[identity])
        self.assertFalse((self.workspaces / 'archive' / (locked[1] + '.tar.gz')).exists())

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

    def test_failed_restore_keeps_the_archive_and_raises(self):
        outcome, identity, path, _ = self.outcome()
        (path / 'tracked.txt').write_text('changed\n')
        self.assertEqual(self.retire(), [identity])
        archive = Path(self.registry()[identity]['retired']['archive'])
        with tarfile.open(archive) as tar:
            metadata = tar.extractfile('retirement.json').read()
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data in (('retirement.json', metadata), ('changes.patch', b'not a patch\n')):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with self.assertRaisesRegex(ValueError, 'could not restore the retired worktree'):
            acquire(self.workspaces, outcome, str(self.repo))
        self.assertTrue(archive.exists())
        self.assertFalse(path.exists())
        self.assertIn('retired', self.registry()[identity])
        self.assertNotIn(str(path), git(self.repo, 'worktree', 'list', '--porcelain'))

        # A moved branch also stops the restore before Git creates a checkout.
        self.commit_on_branch('wt/' + identity)
        with self.assertRaisesRegex(ValueError, 'branch moved'):
            preferred(self.workspaces, outcome)
        self.assertTrue(archive.exists())
        self.assertIn('retired', self.registry()[identity])

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
        self.in_use.stop()
        self.assertTrue(work_workspace.in_use(path))
        self.assertEqual(self.retire(), [])
        self.assertTrue(path.is_dir())
        self.assertNotIn('retired', self.registry()[identity])


if __name__ == '__main__':
    unittest.main()
