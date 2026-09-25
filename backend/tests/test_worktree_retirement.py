from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
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
        self.holders = patch('coconut.work_workspace.holders', return_value=set())
        self.holders.start()
        self.addCleanup(self.holders.stop)

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

    def test_large_ignored_files_keep_the_worktree(self):
        _, identity, path, _ = self.outcome()
        (path / 'build').mkdir()
        (path / 'build' / 'fixture.db').write_bytes(b'x' * 4096)
        (path / 'node_modules').mkdir()
        (path / 'node_modules' / 'big.js').write_bytes(b'x' * 8192)
        with patch('coconut.work_workspace.IGNORED_LIMIT_BYTES', 4096):
            self.assertEqual(self.retire(), [identity])
        _, identity, path, _ = self.outcome()
        (path / 'build').mkdir()
        (path / 'build' / 'fixture.db').write_bytes(b'x' * 4097)
        with patch('coconut.work_workspace.IGNORED_LIMIT_BYTES', 4096):
            self.assertEqual(self.retire(), [])
        self.assertEqual((path / 'build' / 'fixture.db').stat().st_size, 4097)
        self.assertNotIn('retired', self.registry()[identity])

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
        def watched(current, *, timeout=-1, only=None):
            # The final check under the registry lock runs with a short timeout.
            if only is not None and timeout != retirement.SERVICE_WAIT_SECONDS:
                entered.set()
            return finished(current, timeout=timeout, only=only)
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
                self.assertLogs('coconut.worktree_retirement', 'WARNING') as logs:
            try:
                self.assertEqual(self.retire(), [])
            finally:
                release.set()
        self.assertIn('service was busy', '\n'.join(logs.output))
        self.assertEqual(blocked, [])
        self.assertTrue(path.is_dir())
        self.assertNotIn('retired', self.registry()[identity])

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


    def orphan(self, path, outcome=None):
        """Start a process in the path whose parent ends at once, so that launchd adopts it."""
        env = dict(os.environ, COCONUT_WORKSPACE_OUTCOME=outcome) if outcome else None
        command = f'{shlex.quote(sys.executable)} -c "import time; time.sleep(60)" >/dev/null 2>&1 & echo $!'
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
    def test_young_foreign_and_locked_orphans_keep_their_processes_and_worktrees(self):
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
        for pid, identity, path in ((foreign_pid, foreign, foreign_path), (locked_pid, locked, locked_path)):
            self.assertTrue(self.alive(pid))
            self.assertTrue(path.is_dir())
            self.assertNotIn('retired', self.registry()[identity])

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
        with patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing)):
            self.assertEqual(work_workspace.holders(path), {11, 12})

    def test_the_environment_of_another_process_is_read(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   env=dict(os.environ, COCONUT_WORKSPACE_OUTCOME='one'))
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        time.sleep(0.2)
        self.assertTrue(work_workspace.environment_names(process.pid, 'COCONUT_WORKSPACE_OUTCOME=one'))
        self.assertFalse(work_workspace.environment_names(process.pid, 'COCONUT_WORKSPACE_OUTCOME=on'))
        self.assertFalse(work_workspace.environment_names(process.pid, 'COCONUT_WORKSPACE_OUTCOME=two'))

    def test_process_outcomes_reads_each_outcome_and_needs_a_visible_own_environment(self):
        me = str(os.getpid())
        listing = '\n'.join([f'{me} python service.py PATH=/usr/bin HOME=/x',
                              '10 sleep 60 COCONUT_WORKSPACE_OUTCOME=one PATH=/usr/bin',
                              '11 /bin/sh', '12 codex COCONUT_WORKSPACE_OUTCOME=two'])
        # Without /proc, Coconut reads macOS `ps -E`. It trusts the list only when its own environment shows.
        with patch('coconut.work_workspace.Path.exists', return_value=False), \
                patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=listing)):
            self.assertEqual(work_workspace.process_outcomes(), {'one', 'two'})
        hidden = listing.replace(' PATH=/usr/bin HOME=/x', '')
        with patch('coconut.work_workspace.Path.exists', return_value=False), \
                patch('coconut.work_workspace.subprocess.run', return_value=SimpleNamespace(stdout=hidden)):
            self.assertIsNone(work_workspace.process_outcomes())

    def test_process_outcomes_finds_a_real_child_process(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   env=dict(os.environ, COCONUT_WORKSPACE_OUTCOME='probe-outcome'))
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        time.sleep(0.2)
        found = work_workspace.process_outcomes()
        if found is not None:
            self.assertIn('probe-outcome', found)

    def test_elapsed_times_from_ps_become_seconds(self):
        self.assertEqual(work_workspace.elapsed_seconds('05'), 5)
        self.assertEqual(work_workspace.elapsed_seconds('02:03'), 123)
        self.assertEqual(work_workspace.elapsed_seconds('1:02:03'), 3723)
        self.assertEqual(work_workspace.elapsed_seconds('06-16:25:58'), 6 * 86400 + 16 * 3600 + 25 * 60 + 58)


if __name__ == '__main__':
    unittest.main()
