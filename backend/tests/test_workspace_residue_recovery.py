"""The owner's restore of a retired worktree whose path holds newer evidence and no checkout (recover_residue).

Each test retires a real worktree with Coconut, writes newer evidence at its path, and lets a first restore fail as
it does in production. The owner then asks again through the gateway's coconut_workspace (Service.gateway_tool and
the plugin's _workspace), or through acquire directly for the refusals that need a patch of this process.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from coconut import work_workspace
from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
from coconut.work_workspace import acquire, git, preferred, save_bindings, validate_binding

OLD = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace('+00:00', 'Z')
PLUGIN_TOOLS = Path(__file__).resolve().parents[2] / 'hermes_plugin' / 'coconut' / 'tools.py'
EVIDENCE = '.gp8998-evidence'
ARCHIVED = {'ledger.json': b'{"run": 1}\n', 'report.md': b'# Run 1\n', 'shots/one.png': b'\x89PNG one',
            'shots/two.png': b'\x89PNG two'}
NEWER = {'ledger.json': b'{"run": 2}\n', 'report.md': b'# Run 2\nNewer.\n', 'run-2.log': b'second run\n'}


def plugin_tools():
    spec = importlib.util.spec_from_file_location('coconut_plugin_tools_residue', PLUGIN_TOOLS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree(folder):
    """Every file and link under folder, with its bytes or target."""
    result = {}
    for directory, folders, files in os.walk(folder):
        for name in folders + files:
            item = Path(directory) / name
            relative = item.relative_to(folder).as_posix()
            if item.is_symlink():
                result[relative] = ('link', os.readlink(item))
            elif item.is_file():
                result[relative] = item.read_bytes()
    return result


def layout(folder):
    """Every folder, file and link under folder. tree() does not show an empty folder."""
    result = tree(folder)
    for directory, folders, _ in os.walk(folder):
        for name in folders:
            if not (Path(directory) / name).is_symlink():
                result[(Path(directory) / name).relative_to(folder).as_posix()] = 'folder'
    return result


class ResidueRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        (self.repo / '.gitignore').write_text(EVIDENCE + '/\n')
        (self.repo / 'tracked.txt').write_text('one\n')
        git(self.repo, 'add', '.')
        self.commit(self.repo, 'Initial')
        self.service = Service(self.root / 'coconut', FixtureProvider(self.root / 'coconut'), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Coder', 'role': ''}, str(uuid4()))
        self.workspaces = self.service.root / 'workspaces'
        self.workspaces.mkdir(mode=0o700, exist_ok=True)
        self.manifest = self.workspaces / 'worktrees.json'
        # Retirement and the direct acquire calls run in this process. The plugin loads its own copy of the module
        # and uses the real lsof.
        self.holders = MagicMock(side_effect=lambda path, detail=False: {} if detail else set())
        patcher = patch.multiple(work_workspace, holders=self.holders, holders_by_path=MagicMock(
            side_effect=lambda paths, detail=False: {str(p): {} if detail else set() for p in paths}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def commit(self, path, message):
        git(path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-q', '-m', message)

    def registry(self):
        return json.loads(self.manifest.read_text())

    def write(self, folder, files):
        for name, data in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)

    def incident(self, held=False):
        """Retire a worktree with evidence, write newer evidence at its path, and fail the first restore.

        With `held`, the evidence folder is too large for the archive, and retirement holds it (held_folders)."""
        outcome = str(uuid4())
        self.task = dict(id=str(uuid4()), bot_id=self.bot['id'], _execution_session_id=outcome, title='QA',
                         status='completed', summary='', created_at=OLD, updated_at=OLD)
        with self.service.transaction():
            self.service._put('tasks', self.task)
        self.outcome = outcome
        self.identity = outcome + '-' + hashlib.sha256(str(self.repo).encode()).hexdigest()[:16]
        self.path = self.repo / '.worktrees' / self.identity
        self.branch = 'wt/' + self.identity
        git(self.repo, 'worktree', 'add', '-q', '-b', self.branch, str(self.path))
        binding = dict(path=str(self.path), branch=self.branch, repository=str(self.repo), outcome=outcome)
        validate_binding(binding)
        saved = self.registry() if self.manifest.exists() else {}
        saved[self.identity] = binding
        save_bindings(self.manifest, saved)
        (self.path / 'tracked.txt').write_text('one\ncommitted\n')
        git(self.path, 'add', 'tracked.txt')
        self.commit(self.path, 'Work')
        self.head = git(self.path, 'rev-parse', 'HEAD')
        (self.path / 'tracked.txt').write_text('one\ncommitted\nunstaged\n')
        (self.path / 'notes.md').write_text('untracked\n')
        self.write(self.path / EVIDENCE, ARCHIVED)
        self.service._next_worktree_retirement = 0
        with patch.object(work_workspace, 'IGNORED_LIMIT_BYTES', 10 if held else work_workspace.IGNORED_LIMIT_BYTES):
            self.assertEqual(self.service.retire_finished_worktrees(), [self.identity])
        self.assertEqual(self.registry()[self.identity]['retired'].get('held', {}).get('names'),
                         [EVIDENCE] if held else None)
        self.assertFalse(self.path.exists())
        self.archive = Path(self.registry()[self.identity]['retired']['archive'])
        self.archive_bytes = self.archive.read_bytes()
        # A process of the outcome wrote its evidence again after the removal. No checkout is there.
        self.write(self.path / EVIDENCE, NEWER)
        self.residue = tree(self.path)
        with self.assertRaisesRegex(ValueError, 'is not empty and does not match the archive'):
            acquire(self.workspaces, outcome, str(self.repo))
        failure = self.registry()[self.identity]['retired']['restore_failed']
        self.assertEqual(failure['error'], f'The path {self.path} is not empty and does not match the archive.')
        self.assertEqual(tree(self.path), self.residue)
        self.before = self.registry()[self.identity]
        # Another worktree of the same repository, detached, as a candidate pull request.
        self.followup = self.repo / '.worktrees' / 'followup'
        git(self.repo, 'worktree', 'add', '-q', '--detach', str(self.followup))
        (self.followup / 'candidate.txt').write_text('candidate\n')
        self.followup_head = git(self.followup, 'rev-parse', 'HEAD')

    def gateway_acquire(self, task=None, **arguments):
        """coconut_workspace of a working gateway turn of the task, as the plugin runs it."""
        task = task or self.task
        with self.service.transaction():
            self.service._put('tasks', dict(self.service._get('tasks', task['id']), status='working'))
        turn = self.service.gateway_bridge.open_turn(task['id'], self.root, self.bot['id'], cron=False)
        tools = plugin_tools()

        def service_tool(name):
            # The plugin posts this payload to /v1/gateway/tools. A refusal comes back as its error.
            def handler(values, **_):
                try:
                    return json.dumps(self.service.gateway_tool({'turn_id': turn, 'tool': name,
                                                                 'arguments': dict(values or {})}))
                except ServiceError as error:
                    return json.dumps({'error': str(error)})
            return handler
        with patch.object(tools, '_service_tool', service_tool):
            return json.loads(tools._workspace(dict(repository=str(self.repo), **arguments)))

    def assert_fenced(self, result=None, *, residue=None, binding=None):
        if result is not None:
            self.assertIn('Ask the operator', result.get('error', ''))
        self.assertEqual(tree(self.path), self.residue if residue is None else residue)
        self.assertEqual(self.registry()[self.identity], self.before if binding is None else binding)
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertNotIn(str(self.path) + '\n', git(self.repo, 'worktree', 'list', '--porcelain') + '\n')
        moved = self.workspaces / 'archive' / work_workspace.RESIDUE
        self.assertEqual(list(moved.iterdir()) if moved.exists() else [], [])

    # The repair.

    def test_the_owner_restores_through_the_gateway_and_keeps_the_archive_and_the_newer_evidence(self):
        self.incident()
        result = self.gateway_acquire(existing_worktree=str(self.followup))
        self.assertNotIn('error', result)
        self.assertEqual(result['path'], str(self.path))
        self.assertNotIn('retired', result)
        # The checkout holds the commit and every archived change.
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD'), self.head)
        self.assertEqual(git(self.path, 'branch', '--show-current'), self.branch)
        self.assertEqual((self.path / 'tracked.txt').read_text(), 'one\ncommitted\nunstaged\n')
        self.assertEqual((self.path / 'notes.md').read_text(), 'untracked\n')
        self.assertEqual(tree(self.path / EVIDENCE), ARCHIVED)
        # The archive stays, named by the binding, so no sweep deletes it.
        recovery = result['residue_recovery']
        self.assertEqual(recovery['archive'], str(self.archive))
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual(recovery['failure'], self.before['retired']['restore_failed'])
        self.assertIn(self.archive.resolve(), work_workspace.named_retirement_archives(self.workspaces))
        # Every newer file stays, byte for byte, in the folder that the binding names.
        residue = Path(recovery['residue'])
        self.assertEqual(residue.parent, self.workspaces / 'archive' / work_workspace.RESIDUE)
        self.assertEqual(tree(residue), self.residue)
        os.utime(self.archive, (time.time() - 90 * 86400,) * 2)
        work_workspace.sweep_temporary(self.workspaces)
        self.assertTrue(self.archive.exists())
        self.assertEqual(tree(residue), self.residue)
        saved = self.registry()[self.identity]
        self.assertEqual(saved['residue_recovery'], recovery)
        self.assertNotIn('retired', saved)
        # The candidate worktree that the turn named stays as it was.
        self.assertEqual(git(self.followup, 'rev-parse', 'HEAD'), self.followup_head)
        self.assertEqual(git(self.followup, 'branch', '--show-current'), '')
        self.assertEqual((self.followup / 'candidate.txt').read_text(), 'candidate\n')
        # The next turn gets the live worktree.
        self.assertEqual(self.gateway_acquire()['path'], str(self.path))
        self.assertEqual(preferred(self.workspaces, self.outcome), self.path)

    def test_a_direct_acquire_of_the_owner_also_restores(self):
        self.incident()
        binding = acquire(self.workspaces, self.outcome, str(self.repo))
        self.assertEqual(binding['path'], str(self.path))
        self.assertEqual(tree(Path(binding['residue_recovery']['residue'])), self.residue)
        self.assertTrue(self.archive.exists())

    def test_evidence_in_a_held_folder_restores_with_the_held_folder(self):
        self.incident(held=True)
        result = self.gateway_acquire()
        self.assertNotIn('error', result)
        # The held folder is back in the checkout, and the newer evidence is in the moved folder.
        self.assertEqual(tree(self.path / EVIDENCE), ARCHIVED)
        self.assertEqual(tree(Path(result['residue_recovery']['residue'])), self.residue)

    def test_a_writer_of_a_held_folder_during_the_restore_keeps_both_copies(self):
        self.incident(held=True)
        held = Path(self.before['retired']['held']['folder'])
        real = work_workspace.return_held
        written = b'{"run": 3}\n'

        def writer_first(record, path):
            # The writer of the outcome makes the evidence folder in the new checkout before the held one returns.
            self.write(Path(path) / EVIDENCE, {'ledger.json': written})
            return real(record, path)
        with patch.object(work_workspace, 'return_held', writer_first):
            with self.assertRaisesRegex(ValueError, 'held folder appeared'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        saved = self.registry()[self.identity]
        self.assertTrue(saved['retired']['restore_failed']['residue_retry_failed'])
        # Nothing is lost: the held copy, the writer's file in the kept checkout, and the first files at the path.
        self.assertEqual(saved['retired']['held'], self.before['retired']['held'])
        self.assertEqual(tree(held / EVIDENCE), ARCHIVED)
        checkout = Path(saved['retired']['residue']['checkout'])
        self.assertEqual((checkout / EVIDENCE / 'ledger.json').read_bytes(), written)
        self.assertEqual(tree(self.path), self.residue)
        self.assertIn('Ask the operator', self.gateway_acquire()['error'])

    def test_a_second_recovery_of_the_same_binding_stays_fenced(self):
        self.incident()
        saved = self.registry()
        saved[self.identity]['residue_recovery'] = dict(archive=str(self.root / 'first.tar.gz'), residue='first',
                                                        failure={}, at='then')
        save_bindings(self.manifest, saved)
        self.assertIn('Ask the operator', self.gateway_acquire()['error'])
        self.assert_fenced(binding=saved[self.identity])

    # The owner and the route.

    def test_a_turn_of_another_outcome_gets_no_access(self):
        self.incident()
        other = dict(id=str(uuid4()), bot_id=self.bot['id'], _execution_session_id=str(uuid4()), title='Other',
                     status='working', summary='', created_at=OLD, updated_at=OLD)
        with self.service.transaction():
            self.service._put('tasks', other)
        result = self.gateway_acquire(other, existing_worktree=str(self.path))
        self.assertIn('Only a linked worktree can be adopted', result['error'])
        self.assert_fenced()

    def test_a_binding_of_another_owner_is_refused(self):
        self.incident()
        saved = self.registry()
        saved[self.identity]['outcome'] = str(uuid4())
        save_bindings(self.manifest, saved)
        with self.assertRaisesRegex(ValueError, 'another owner'):
            acquire(self.workspaces, self.outcome, str(self.repo))
        self.assert_fenced(binding=saved[self.identity])

    def test_a_turn_that_does_not_run_gets_no_worktree(self):
        self.incident()
        turn = self.service.gateway_bridge.open_turn(self.task['id'], self.root, self.bot['id'], cron=False)
        with self.assertRaisesRegex(ServiceError, 'only in a turn of a work session'):
            self.service.gateway_tool({'turn_id': turn, 'tool': 'workspace', 'arguments': {}})
        self.assert_fenced()

    def test_a_path_hint_never_moves_the_evidence(self):
        self.incident()
        self.assertEqual(preferred(self.workspaces, self.outcome), self.workspaces / self.outcome)
        self.assert_fenced()

    # The archive and the branch.

    def test_a_moved_branch_is_refused(self):
        self.incident()
        git(self.repo, 'update-ref', 'refs/heads/' + self.branch, git(self.repo, 'rev-parse', 'HEAD'))
        self.assertIn('branch moved', self.gateway_acquire()['error'])
        self.assert_fenced()

    def test_a_branch_checked_out_elsewhere_is_refused(self):
        self.incident()
        git(self.repo, 'worktree', 'add', '-q', str(self.root / 'elsewhere'), self.branch)
        self.assertIn('Git registers a worktree', self.gateway_acquire()['error'])
        self.assert_fenced()

    def test_an_archive_of_another_path_or_head_is_refused(self):
        self.incident()
        for change in (dict(path=str(self.root / 'other')), dict(head='0' * 40), dict(outcome=str(uuid4()))):
            with tarfile.open(fileobj=io.BytesIO(self.archive_bytes)) as source, \
                    tarfile.open(self.archive, 'w:gz') as target:
                for member in source.getmembers():
                    data = source.extractfile(member).read() if member.isfile() else None
                    if member.name == 'retirement.json':
                        data = json.dumps(dict(json.loads(data), **change)).encode()
                        member.size = len(data)
                    target.addfile(member, io.BytesIO(data) if data is not None else None)
            self.assertIn('another owner, path, branch, or commit', self.gateway_acquire()['error'])
            self.assertEqual(tree(self.path), self.residue)
            self.assertEqual(self.registry()[self.identity], self.before)
        self.archive.write_bytes(self.archive_bytes)
        self.assert_fenced()

    def test_a_missing_or_linked_archive_is_refused(self):
        self.incident()
        moved = self.root / 'moved.tar.gz'
        self.archive.rename(moved)
        self.assertIn('is missing', self.gateway_acquire()['error'])
        self.archive.symlink_to(moved)
        self.assertIn('is missing', self.gateway_acquire()['error'])
        self.archive.unlink()
        moved.rename(self.archive)
        self.assert_fenced()

    # The files at the path.

    def test_unrelated_files_are_refused(self):
        self.incident()
        for extra in ('stray.txt', 'other/file.txt'):
            self.write(self.path, {extra: b'not evidence\n'})
            residue = tree(self.path)
            self.assertIn('does not name', self.gateway_acquire()['error'])
            self.assert_fenced(residue=residue)
            (self.path / extra).unlink()

    def test_links_special_files_and_git_records_are_refused(self):
        self.incident()
        link = self.path / EVIDENCE / 'link'
        link.symlink_to('/etc/hosts')
        self.assertIn('a link or a special file', self.gateway_acquire()['error'])
        self.assert_fenced(residue=tree(self.path))
        link.unlink()
        os.mkfifo(self.path / EVIDENCE / 'pipe')
        self.assertIn('a link or a special file', self.gateway_acquire()['error'])
        (self.path / EVIDENCE / 'pipe').unlink()
        for record in (self.path / '.git', self.path / EVIDENCE / '.git'):
            record.write_text('gitdir: /elsewhere\n')
            self.assertIn('Ask the operator', self.gateway_acquire()['error'])
            self.assert_fenced(residue=tree(self.path))
            record.unlink()
        self.assert_fenced()

    def test_a_linked_or_redirected_path_is_refused(self):
        self.incident()
        real = self.root / 'real-residue'
        self.path.rename(real)
        self.path.symlink_to(real)
        self.assertIn('not a plain folder', self.gateway_acquire()['error'])
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(tree(real), self.residue)
        self.path.unlink()
        # A link in a parent folder redirects the path, too.
        parent = self.root / 'redirected'
        (parent / self.identity).parent.mkdir()
        real.rename(parent / self.identity)
        shutil.move(str(self.repo / '.worktrees' / 'followup'), str(parent / 'followup'))
        (self.repo / '.worktrees').rmdir()
        (self.repo / '.worktrees').symlink_to(parent)
        self.assertIn('not a plain folder', self.gateway_acquire()['error'])
        self.assertEqual(tree(parent / self.identity), self.residue)
        self.assertEqual(self.registry()[self.identity], self.before)

    def redirect_on_scan(self, scan):
        """Make the parent of the path a link on lsof scan number `scan`, as a process could during the recovery."""
        parent, calls = self.root / 'redirected', []

        def scanning(path, detail=False):
            calls.append(path)
            if len(calls) == scan:
                shutil.move(str(self.repo / '.worktrees'), str(parent))
                (self.repo / '.worktrees').symlink_to(parent)
            return {} if detail else set()
        self.holders.side_effect = scanning
        return parent

    def test_a_path_redirected_during_the_writer_check_is_not_moved(self):
        self.incident()
        parent = self.redirect_on_scan(1)
        with self.assertRaisesRegex(ValueError, 'redirected'):
            acquire(self.workspaces, self.outcome, str(self.repo))
        self.assertEqual(tree(parent / self.identity), self.residue)
        self.assertEqual(self.registry()[self.identity], self.before)
        self.assertEqual(list((self.workspaces / 'archive' / work_workspace.RESIDUE).iterdir()), [])

    def test_a_path_redirected_after_the_move_gets_no_checkout_and_no_files(self):
        self.incident()
        parent = self.redirect_on_scan(2)
        with self.assertRaisesRegex(ValueError, 'redirected'):
            acquire(self.workspaces, self.outcome, str(self.repo))
        saved = self.registry()[self.identity]
        self.assertTrue(saved['retired']['restore_failed']['residue_retry_failed'])
        # The files stay in the moved folder that the binding names. Nothing goes through the link.
        self.assertEqual(tree(Path(saved['retired']['residue']['folder'])), self.residue)
        self.assertFalse(os.path.lexists(parent / self.identity))
        self.assertNotIn('branch refs/heads/' + self.branch, git(self.repo, 'worktree', 'list', '--porcelain'))

    def test_a_live_writer_is_refused(self):
        self.incident()
        self.holders.side_effect = lambda path, detail=False: {4242: False} if detail else {4242}
        with self.assertRaisesRegex(ValueError, 'A process uses the saved path'):
            acquire(self.workspaces, self.outcome, str(self.repo))
        self.assert_fenced()

    @unittest.skipUnless(shutil.which('lsof'), 'lsof is missing')
    def test_a_live_writer_that_lsof_sees_is_refused_through_the_gateway(self):
        self.incident()
        ready = self.path / EVIDENCE / 'writer.log'
        writer = subprocess.Popen([sys.executable, '-c', 'import sys, time\nf = open(sys.argv[1], "a")\n'
                                   'f.write("x"); f.flush()\ntime.sleep(60)', str(ready)])
        self.addCleanup(writer.wait)
        self.addCleanup(writer.kill)
        deadline = time.monotonic() + 10
        while not (ready.exists() and ready.stat().st_size) and time.monotonic() < deadline:
            time.sleep(0.05)
        residue = tree(self.path)
        self.assertIn('A process uses the saved path', self.gateway_acquire()['error'])
        self.assert_fenced(residue=residue)

    # Races, stops and earlier failures.

    def test_a_writer_during_the_recovery_gets_its_files_back_at_the_path(self):
        self.incident()
        real = work_workspace.save_bindings
        calls = []

        def racing(manifest, saved):
            real(manifest, saved)
            if not calls:
                calls.append(1)
                (self.path / EVIDENCE / 'late').symlink_to('/etc/hosts')
        with patch.object(work_workspace, 'save_bindings', racing):
            with self.assertRaisesRegex(ValueError, 'changed during the recovery'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        self.assert_fenced(residue=dict(self.residue, **{EVIDENCE + '/late': ('link', '/etc/hosts')}))

    def test_a_stop_after_the_record_is_never_retried(self):
        self.incident()
        with patch.object(work_workspace.os, 'rename', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                acquire(self.workspaces, self.outcome, str(self.repo))
        saved = self.registry()[self.identity]
        self.assertIn('residue', saved['retired'])
        self.assertEqual(saved['retired']['restore_failed'], self.before['retired']['restore_failed'])
        self.assertIn('Ask the operator', self.gateway_acquire()['error'])
        self.assert_fenced(binding=saved)

    def test_a_failed_checkout_puts_the_evidence_back_and_is_never_retried(self):
        self.incident()
        real = work_workspace.git

        def refuse(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'add'):
                raise ValueError('Git refused the checkout.')
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=refuse):
            with self.assertRaisesRegex(ValueError, 'Git refused the checkout'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        saved = self.registry()[self.identity]
        failure = saved['retired']['restore_failed']
        self.assertTrue(failure['residue_retry_failed'] and failure['duplicate_retry_failed'])
        self.assertNotIn('residue', saved['retired'])
        self.assert_fenced(binding=saved)
        # The next turn of the owner leaves everything as it is.
        self.assertIn('Git refused the checkout', self.gateway_acquire()['error'])
        self.assert_fenced(binding=saved)

    def test_a_writer_during_the_checkout_keeps_its_file_and_the_partial_checkout(self):
        self.incident()
        real = work_workspace.apply_unstaged
        written = b'{"run": 3}\n'

        def writer_returns(path, unstaged):
            real(path, unstaged)
            # The writer of the outcome writes its ledger again, into the new checkout, before the extraction.
            self.write(Path(path) / EVIDENCE, {'ledger.json': written})
        with patch.object(work_workspace, 'apply_unstaged', writer_returns):
            with self.assertRaisesRegex(ValueError, 'already contains'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        saved = self.registry()[self.identity]
        self.assertTrue(saved['retired']['restore_failed']['residue_retry_failed'])
        # The first files went back to the path, so the record names only the partial checkout.
        self.assertEqual(set(saved['retired']['residue']), {'checkout'})
        checkout = Path(saved['retired']['residue']['checkout'])
        self.assertIn(str(checkout), saved['retired']['restore_failed']['error'])
        # Nothing is deleted: the newer file is in the partial checkout, and the first files are back at the path.
        self.assertEqual((checkout / EVIDENCE / 'ledger.json').read_bytes(), written)
        # The kept copy is plain files. A later checkout of the path cannot share its Git record.
        self.assertFalse(os.path.lexists(checkout / '.git'))
        self.assertTrue((checkout / '.git-detached').is_file())
        self.assertEqual(tree(self.path), self.residue)
        self.assertNotIn(str(checkout), git(self.repo, 'worktree', 'list', '--porcelain'))
        self.assertNotIn('branch refs/heads/' + self.branch, git(self.repo, 'worktree', 'list', '--porcelain'))
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        # The next turn of the owner changes nothing.
        self.assertIn('Ask the operator', self.gateway_acquire()['error'])
        self.assertEqual(tree(self.path), self.residue)
        self.assertEqual(self.registry()[self.identity], saved)

    def test_a_failed_last_check_keeps_the_restored_checkout_and_names_its_error(self):
        self.incident()
        with patch.object(work_workspace, 'validate_binding', side_effect=ValueError('Git is busy.')):
            with self.assertRaisesRegex(ValueError, 'Git is busy'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        saved = self.registry()[self.identity]
        self.assertNotIn('retired', saved)
        self.assertEqual(tree(Path(saved['residue_recovery']['residue'])), self.residue)
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD'), self.head)
        # The next turn gets the restored worktree.
        self.assertEqual(self.gateway_acquire()['path'], str(self.path))

    def test_other_saved_failures_stay_fenced(self):
        self.incident()
        failure = self.before['retired']['restore_failed']
        for other in (dict(failure, error='The saved branch moved after retirement.'),
                      dict(failure, residue_retry_failed=True), dict(failure, duplicate_retry_failed=True),
                      dict(failure, error=failure['error'].replace(str(self.path), str(self.root / 'other')))):
            saved = self.registry()
            saved[self.identity]['retired']['restore_failed'] = other
            save_bindings(self.manifest, saved)
            self.assertIn('Ask the operator', self.gateway_acquire()['error'])
            self.assert_fenced(binding=saved[self.identity])

    def test_a_restore_without_a_saved_failure_keeps_refusing_newer_files(self):
        self.incident()
        saved = self.registry()
        saved[self.identity]['retired'].pop('restore_failed')
        save_bindings(self.manifest, saved)
        self.assertIn('is not empty and does not match the archive', self.gateway_acquire()['error'])
        # That restore saves its own failure again. Its next acquire is the recovery of an owner.
        failure = self.registry()[self.identity]['retired']['restore_failed']
        self.assertEqual(failure['error'], self.before['retired']['restore_failed']['error'])
        self.assert_fenced(binding=dict(self.before, retired=dict(self.before['retired'], restore_failed=failure)))

    # A subagent's nested worktree, recreated after retirement and removed by Hermes, leaves only an empty folder.

    def nested_incident(self):
        """Retire a worktree with a nested subagent worktree. The subagent then recreates its worktree after the
        retirement, and Hermes archives it to a ref and removes it when the subagent ends. Only the empty .worktrees/
        stays at the saved path, and the first restore fails. The archive of the parent holds only its patches."""
        outcome = str(uuid4())
        self.task = dict(id=str(uuid4()), bot_id=self.bot['id'], _execution_session_id=outcome, title='Dev',
                         status='completed', summary='', created_at=OLD, updated_at=OLD)
        with self.service.transaction():
            self.service._put('tasks', self.task)
        self.outcome = outcome
        self.identity = outcome + '-' + hashlib.sha256(str(self.repo).encode()).hexdigest()[:16]
        self.path = self.repo / '.worktrees' / self.identity
        self.branch = 'wt/' + self.identity
        git(self.repo, 'worktree', 'add', '-q', '-b', self.branch, str(self.path))
        binding = dict(path=str(self.path), branch=self.branch, repository=str(self.repo), outcome=outcome)
        validate_binding(binding)
        save_bindings(self.manifest, {self.identity: binding})
        self.head = git(self.path, 'rev-parse', 'HEAD')
        # Hermes adds .worktrees/ to the .gitignore of the parent, so the archive holds only an unstaged patch.
        (self.path / '.gitignore').write_text(EVIDENCE + '/\n.worktrees/\n')
        self.child = self.path / '.worktrees' / 'subagent-sa-0'
        self.child_branch = 'hermes-subagent/subagent-sa-0'
        git(self.repo, 'worktree', 'add', '-q', '-b', self.child_branch, str(self.child), self.head)
        (self.child / 'tracked.txt').write_text('one\ncandidate\n')
        git(self.child, 'add', 'tracked.txt')
        self.commit(self.child, 'Candidate')
        (self.child / 'tracked.txt').write_text('one\ncandidate\nuncommitted\n')
        self.service._next_worktree_retirement = 0
        self.assertEqual(self.service.retire_finished_worktrees(), [self.identity])
        self.assertFalse(self.path.exists())
        nested_archives = list((self.workspaces / 'archive' / 'nested').iterdir())
        self.assertEqual(len(nested_archives), 1)
        self.nested_archive, self.nested_bytes = nested_archives[0], nested_archives[0].read_bytes()
        self.archive = Path(self.registry()[self.identity]['retired']['archive'])
        self.archive_bytes = self.archive.read_bytes()
        with tarfile.open(self.archive) as tar:
            self.assertEqual(sorted(tar.getnames()), ['retirement.json', 'staged.patch', 'unstaged.patch'])
        # The subagent still runs. It creates the parent folders and checks its branch out again.
        self.child.parent.mkdir(parents=True)
        git(self.repo, 'worktree', 'add', '-q', str(self.child), self.child_branch)
        (self.child / 'tracked.txt').write_text('one\ncandidate\nuncommitted again\n')
        # The subagent ends. Hermes keeps its edits in a ref and removes the worktree (finalize_subagent_worktree).
        git(self.child, 'add', 'tracked.txt')
        snapshot = git(self.child, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit-tree',
                       git(self.child, 'write-tree'), '-p', 'HEAD', '-m', 'hermes worktree archive: subagent-sa-0')
        git(self.repo, 'update-ref', 'refs/hermes/archive/subagent-sa-0', snapshot)
        git(self.repo, 'worktree', 'remove', '--force', str(self.child))
        self.assertEqual(layout(self.path), {'.worktrees': 'folder'})
        self.refs = git(self.repo, 'for-each-ref', '--format=%(refname) %(objectname)')
        with self.assertRaisesRegex(ValueError, 'is not empty and does not match the archive'):
            acquire(self.workspaces, outcome, str(self.repo))
        self.residue = layout(self.path)
        self.before = self.registry()[self.identity]

    def assert_nested_fenced(self, result=None, *, residue=None, binding=None):
        if result is not None:
            self.assertIn('Ask the operator', result.get('error', ''))
        self.assertEqual(layout(self.path), self.residue if residue is None else residue)
        self.assertEqual(self.registry()[self.identity], self.before if binding is None else binding)
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual(self.nested_archive.read_bytes(), self.nested_bytes)
        self.assertNotIn('branch refs/heads/' + self.branch, git(self.repo, 'worktree', 'list', '--porcelain'))
        moved = self.workspaces / 'archive' / work_workspace.RESIDUE
        self.assertEqual(list(moved.iterdir()) if moved.exists() else [], [])

    def test_an_empty_nested_worktree_folder_restores_through_the_gateway(self):
        self.nested_incident()
        result = self.gateway_acquire(existing_worktree=str(self.child))
        self.assertNotIn('error', result)
        # The saved binding of the outcome decides. The checkout is the parent, with its branch and its changes.
        self.assertEqual(result['path'], str(self.path))
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD'), self.head)
        self.assertEqual(git(self.path, 'branch', '--show-current'), self.branch)
        self.assertEqual((self.path / '.gitignore').read_text(), EVIDENCE + '/\n.worktrees/\n')
        # The archive stays, and the empty folder is in the moved folder that the binding names.
        recovery = result['residue_recovery']
        self.assertEqual(recovery['failure'], self.before['retired']['restore_failed'])
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual(layout(Path(recovery['residue'])), self.residue)
        self.assertEqual(self.nested_archive.read_bytes(), self.nested_bytes)
        # Coconut does not restore the nested worktree. Its branch and the Hermes archive ref stay as they were,
        # so the owner can check it out again inside the restored parent.
        self.assertEqual(git(self.repo, 'for-each-ref', '--format=%(refname) %(objectname)'), self.refs)
        self.assertFalse(self.child.exists())
        git(self.repo, 'worktree', 'add', '-q', str(self.child), self.child_branch)
        self.assertEqual((self.child / 'tracked.txt').read_text(), 'one\ncandidate\n')
        self.assertEqual(git(self.path, 'status', '--porcelain'), 'M .gitignore')
        self.assertEqual(self.gateway_acquire()['path'], str(self.path))

    def test_files_in_the_empty_nested_folder_are_refused(self):
        self.nested_incident()
        for extra in ('.worktrees/subagent-sa-0/tracked.txt', '.worktrees/stray.txt', 'stray.txt'):
            self.write(self.path, {extra: b'not evidence\n'})
            residue = layout(self.path)
            self.assertIn('does not name', self.gateway_acquire()['error'])
            self.assert_nested_fenced(residue=residue)
            (self.path / extra).unlink()
            if (self.path / extra).parent == self.child:
                self.child.rmdir()
        self.assert_nested_fenced()

    def test_a_nested_checkout_or_link_in_the_folder_is_refused(self):
        self.nested_incident()
        record = self.child / '.git'
        record.parent.mkdir()
        record.write_text('gitdir: /elsewhere\n')
        self.assertIn('Git record', self.gateway_acquire()['error'])
        self.assert_nested_fenced(residue=layout(self.path))
        record.unlink()
        record.parent.rmdir()
        link = self.path / '.worktrees' / 'subagent-sa-0'
        link.symlink_to(self.root)
        self.assertIn('a link or a special file', self.gateway_acquire()['error'])
        self.assert_nested_fenced(residue=layout(self.path))
        link.unlink()
        self.assert_nested_fenced()

    def test_a_linked_nested_path_is_refused(self):
        self.nested_incident()
        real = self.root / 'real-residue'
        self.path.rename(real)
        self.path.symlink_to(real)
        self.assertIn('not a plain folder', self.gateway_acquire()['error'])
        self.assertEqual(layout(real), self.residue)
        self.assertEqual(self.registry()[self.identity], self.before)

    def test_a_writer_moved_branch_or_other_owner_keeps_the_empty_folder(self):
        self.nested_incident()
        self.holders.side_effect = lambda path, detail=False: {4242: False} if detail else {4242}
        with self.assertRaisesRegex(ValueError, 'A process uses the saved path'):
            acquire(self.workspaces, self.outcome, str(self.repo))
        self.assert_nested_fenced()
        self.holders.side_effect = lambda path, detail=False: {} if detail else set()
        git(self.repo, 'update-ref', 'refs/heads/' + self.branch, git(self.repo, 'rev-parse', self.child_branch))
        self.assertIn('branch moved', self.gateway_acquire()['error'])
        self.assert_nested_fenced()
        git(self.repo, 'update-ref', 'refs/heads/' + self.branch, self.head)
        saved = self.registry()
        saved[self.identity]['outcome'] = str(uuid4())
        save_bindings(self.manifest, saved)
        with self.assertRaisesRegex(ValueError, 'another owner'):
            acquire(self.workspaces, self.outcome, str(self.repo))
        self.assert_nested_fenced(binding=saved[self.identity])

    def test_a_subagent_checkout_during_the_recovery_puts_the_folder_back(self):
        self.nested_incident()
        real = work_workspace.save_bindings
        calls = []

        def racing(manifest, saved):
            real(manifest, saved)
            if not calls:
                calls.append(1)
                # The subagent checks its branch out again after the recovery recorded the move.
                git(self.repo, 'worktree', 'add', '-q', str(self.child), self.child_branch)
        with patch.object(work_workspace, 'save_bindings', racing):
            with self.assertRaisesRegex(ValueError, 'changed during the recovery'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        # The subagent's checkout is back at its path, whole, and nothing else changed.
        self.assertEqual(git(self.child, 'branch', '--show-current'), self.child_branch)
        self.assertEqual(self.registry()[self.identity], self.before)
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual(list((self.workspaces / 'archive' / work_workspace.RESIDUE).iterdir()), [])

    def test_an_interrupted_nested_recovery_is_never_retried(self):
        self.nested_incident()
        with patch.object(work_workspace.os, 'rename', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                acquire(self.workspaces, self.outcome, str(self.repo))
        saved = self.registry()[self.identity]
        self.assertIn('residue', saved['retired'])
        self.assertIn('Ask the operator', self.gateway_acquire()['error'])
        self.assert_nested_fenced(binding=saved)

    def test_a_failed_nested_checkout_puts_the_empty_folder_back_and_is_never_retried(self):
        self.nested_incident()
        real = work_workspace.git

        def refuse(where, *arguments, **options):
            if arguments[:2] == ('worktree', 'add'):
                raise ValueError('Git refused the checkout.')
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=refuse):
            with self.assertRaisesRegex(ValueError, 'Git refused the checkout'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        saved = self.registry()[self.identity]
        self.assertTrue(saved['retired']['restore_failed']['residue_retry_failed'])
        self.assertNotIn('residue', saved['retired'])
        self.assert_nested_fenced(binding=saved)
        self.assertIn('Git refused the checkout', self.gateway_acquire()['error'])
        self.assert_nested_fenced(binding=saved)

    def test_a_partial_nested_checkout_with_a_late_writer_is_kept_whole(self):
        self.nested_incident()
        real = work_workspace.apply_unstaged

        def writer_returns(path, unstaged):
            real(path, unstaged)
            # A writer adds a file to the new checkout, and the final digest check then fails.
            (Path(path) / 'late.txt').write_text('late\n')
        with patch.object(work_workspace, 'apply_unstaged', writer_returns):
            with self.assertRaisesRegex(ValueError, 'do not match the archive'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        saved = self.registry()[self.identity]
        checkout = Path(saved['retired']['residue']['checkout'])
        self.assertEqual((checkout / 'late.txt').read_text(), 'late\n')
        self.assertTrue((checkout / '.git-detached').is_file())
        self.assertEqual(layout(self.path), self.residue)
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertNotIn('branch refs/heads/' + self.branch, git(self.repo, 'worktree', 'list', '--porcelain'))
        self.assertIn('Ask the operator', self.gateway_acquire()['error'])
        self.assertEqual(self.registry()[self.identity], saved)


class ResidueEntryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.members = [(None, EVIDENCE + '/ledger.json'), (None, 'notes.md')]

    def test_only_folders_that_the_archive_names_count(self):
        (self.folder / EVIDENCE / 'deep').mkdir(parents=True)
        (self.folder / EVIDENCE / 'deep' / 'new.txt').write_text('x')
        names = [entry[0] for entry in work_workspace.residue_entries(self.folder, self.members)]
        self.assertEqual(names, [EVIDENCE, EVIDENCE + '/deep', EVIDENCE + '/deep/new.txt'])
        # A file at the top, even one that the archive names, is not in an evidence folder.
        (self.folder / 'notes.md').write_text('x')
        with self.assertRaisesRegex(ValueError, 'does not name'):
            work_workspace.residue_entries(self.folder, self.members)

    def test_empty_folders_count_anywhere_but_their_files_do_not(self):
        (self.folder / '.worktrees' / 'subagent').mkdir(parents=True)
        names = [entry[0] for entry in work_workspace.residue_entries(self.folder, self.members)]
        self.assertEqual(names, ['.worktrees', '.worktrees/subagent'])
        (self.folder / '.worktrees' / 'subagent' / 'file.txt').write_text('x')
        with self.assertRaisesRegex(ValueError, 'does not name'):
            work_workspace.residue_entries(self.folder, self.members)

    def test_an_empty_folder_is_not_evidence(self):
        with self.assertRaisesRegex(ValueError, 'holds no files'):
            work_workspace.residue_entries(self.folder, self.members)

    def test_only_the_untouched_occupied_failure_counts(self):
        path = Path('/x/y')
        failure = dict(at='now', error='The path /x/y is not empty and does not match the archive.')
        self.assertTrue(work_workspace.occupied_failure(failure, path))
        self.assertFalse(work_workspace.occupied_failure(dict(failure, residue_retry_failed=True), path))
        self.assertFalse(work_workspace.occupied_failure(failure, Path('/x/z')))
        self.assertFalse(work_workspace.occupied_failure(None, path))


if __name__ == '__main__':
    unittest.main()
