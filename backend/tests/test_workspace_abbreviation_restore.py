"""The restore of a retired worktree after Git began to abbreviate object names with more characters.

Git shortens the blob names in the index lines of a text patch by the number of objects in the repository. A fetch
after retirement can lengthen them, so the restored checkout gives other patch bytes than the archive for the same
changes, and the last comparison of unpack failed ("The restored files do not match the archive."). Each test retires
a real worktree with Coconut, grows the repository with git fast-import, and restores, through the gateway's
coconut_workspace (Service.gateway_tool and the plugin's _workspace) where the route matters.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from coconut import work_workspace
from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
from coconut.work_workspace import acquire, changes, changes_digest, git, preferred, save_bindings, validate_binding

OLD = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace('+00:00', 'Z')
PLUGIN_TOOLS = Path(__file__).resolve().parents[2] / 'hermes_plugin' / 'coconut' / 'tools.py'
IDENTITY = ('-c', 'user.name=Coconut Test', '-c', 'user.email=coconut-test@example.invalid')
EVIDENCE = '.gp8998-evidence'
DIGEST_FAILURE = 'The restored files do not match the archive.'


def plugin_tools():
    spec = importlib.util.spec_from_file_location('coconut_plugin_tools_abbreviation', PLUGIN_TOOLS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def index_names(patch):
    return re.findall(rb'^index ([0-9a-f]+)\.\.([0-9a-f]+)', patch, re.M)


def legacy_comparison(path, staged, unstaged, expected):
    # The last comparison of unpack before this change: the digest of the checkout's own patches.
    return changes_digest(*changes(path)) == expected


class AbbreviationRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        (self.repo / '.gitignore').write_text('.knapsack_pro/\n')
        for name in ('index.tsx', 'product_edit.ts', 'file_embeds_spec.rb', 'notes.txt'):
            (self.repo / name).write_text(''.join(f'line {number} of {name}\n' for number in range(40)))
        git(self.repo, 'add', '.')
        self.commit(self.repo, 'Initial')
        self.service = Service(self.root / 'coconut', FixtureProvider(self.root / 'coconut'), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Coder', 'role': ''}, str(uuid4()))
        self.workspaces = self.service.root / 'workspaces'
        self.workspaces.mkdir(mode=0o700, exist_ok=True)
        self.manifest = self.workspaces / 'worktrees.json'
        patcher = patch.multiple(work_workspace,
                                 holders=MagicMock(side_effect=lambda path, detail=False: {} if detail else set()),
                                 holders_by_path=MagicMock(side_effect=lambda paths, detail=False: {
                                     str(p): {} if detail else set() for p in paths}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def commit(self, path, message):
        git(path, *IDENTITY, 'commit', '-q', '-m', message)

    def registry(self):
        return json.loads(self.manifest.read_text())

    def grow(self, count=17000):
        """Add objects, as a fetch does, until Git abbreviates object names with one more character."""
        stream = b''.join(b'blob\ndata %d\n%s\n' % (len(data), data)
                          for data in (b'object %d\n' % number for number in range(count)))
        subprocess.run(['git', '-C', str(self.repo), 'fast-import', '--quiet'], input=stream, check=True, timeout=120)

    def retire(self, earlier=None):
        """Retire a worktree with edits to tracked files, untracked evidence, and ignored files.

        `earlier(binding)` runs before the retirement, for example to add the record of an earlier recovery."""
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
        if earlier:
            earlier(binding)
        save_bindings(self.manifest, saved)
        for name in ('index.tsx', 'product_edit.ts', 'file_embeds_spec.rb'):
            with (self.path / name).open('a') as stream:
                stream.write(f'QA change of {name}\n')
        (self.path / EVIDENCE).mkdir()
        (self.path / EVIDENCE / 'ledger.json').write_text('{"run": 1}\n')
        (self.path / EVIDENCE / 'report.md').write_text('# Run 1\n')
        (self.path / '.knapsack_pro').mkdir()
        (self.path / '.knapsack_pro' / 'report.json').write_text('{}\n')
        self.head = git(self.path, 'rev-parse', 'HEAD')
        self.service._next_worktree_retirement = 0
        self.assertEqual(self.service.retire_finished_worktrees(), [self.identity])
        self.assertFalse(self.path.exists())
        self.archive = Path(self.registry()[self.identity]['retired']['archive'])
        self.archive_bytes = self.archive.read_bytes()
        with tarfile.open(self.archive) as tar:
            self.unstaged = tar.extractfile('unstaged.patch').read()

    def fail_first_restore(self):
        """Fail the first restore with the last comparison of the earlier version, as it failed for the owner.

        The earlier version itself has no matches_archive, and its first restore fails by itself."""
        legacy = (patch.object(work_workspace, 'matches_archive', legacy_comparison)
                  if hasattr(work_workspace, 'matches_archive') else contextlib.nullcontext())
        with legacy:
            with self.assertRaisesRegex(ValueError, 'do not match the archive'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        failure = self.registry()[self.identity]['retired']['restore_failed']
        self.assertEqual(set(failure), {'at', 'error'})
        self.assertEqual(failure['error'], DIGEST_FAILURE)
        self.assertFalse(self.path.exists())
        self.before = self.registry()[self.identity]

    def gateway_acquire(self, task=None, **arguments):
        """coconut_workspace of a working gateway turn of the task, as the plugin runs it."""
        task = task or self.task
        with self.service.transaction():
            self.service._put('tasks', dict(self.service._get('tasks', task['id']), status='working'))
        turn = self.service.gateway_bridge.open_turn(task['id'], self.root, self.bot['id'], cron=False)
        tools = plugin_tools()

        def service_tool(name):
            def handler(values, **_):
                try:
                    return json.dumps(self.service.gateway_tool({'turn_id': turn, 'tool': name,
                                                                 'arguments': dict(values or {})}))
                except ServiceError as error:
                    return json.dumps({'error': str(error)})
            return handler
        with patch.object(tools, '_service_tool', service_tool):
            return json.loads(tools._workspace(dict(repository=str(self.repo), **arguments)))

    def assert_restored(self):
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD'), self.head)
        self.assertEqual(git(self.path, 'branch', '--show-current'), self.branch)
        for name in ('index.tsx', 'product_edit.ts', 'file_embeds_spec.rb'):
            self.assertTrue((self.path / name).read_text().endswith(f'QA change of {name}\n'))
        self.assertEqual((self.path / EVIDENCE / 'ledger.json').read_text(), '{"run": 1}\n')
        self.assertEqual((self.path / '.knapsack_pro' / 'report.json').read_text(), '{}\n')

    def assert_fenced(self, result, binding=None):
        self.assertIn('Ask the operator', result.get('error', ''))
        self.assertEqual(self.registry()[self.identity], self.before if binding is None else binding)
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertNotIn(str(self.path) + '\n', git(self.repo, 'worktree', 'list', '--porcelain') + '\n')

    def test_the_fixture_shows_the_longer_object_names(self):
        self.retire()
        archived = {len(name) for pair in index_names(self.unstaged) for name in pair}
        self.grow()
        # The old side names blobs of the commit. Git now abbreviates each with one more character.
        current = {len(git(self.repo, 'rev-parse', '--short', git(self.repo, 'rev-parse', old.decode())))
                   for old, _ in index_names(self.unstaged)}
        self.assertEqual((archived, current), ({7}, {8}))

    def test_the_drift_proof_looks_up_few_names_under_the_lock(self):
        self.retire()
        real, calls = work_workspace.git, []

        def counting(where, *arguments, **options):
            calls.extend(arguments[:2] == ('rev-parse', '--short') and [1] or [])
            return real(where, *arguments, **options)
        with patch.object(work_workspace, 'git', side_effect=counting), patch.object(work_workspace, 'DRIFT_SAMPLES', 2):
            with self.assertRaisesRegex(ValueError, 'as before'):
                work_workspace.abbreviation_drift(self.repo, b'', self.unstaged)
            self.assertEqual(len(calls), 2)
            self.grow()
            calls.clear()
            self.assertEqual(work_workspace.abbreviation_drift(self.repo, b'', self.unstaged), [(7, 8)])
            self.assertEqual(len(calls), 1)

    # The repair.

    def test_a_restore_after_the_repository_grew_gets_the_archived_changes(self):
        self.retire()
        self.grow()
        result = self.gateway_acquire()
        self.assertNotIn('error', result)
        self.assert_restored()
        self.assertNotIn('retired', self.registry()[self.identity])
        # The patches of the checkout differ from the archive only in their longer object names.
        self.assertNotEqual(git(self.path, 'diff', *work_workspace.DIFF).encode() + b'\n', self.unstaged)

    def test_a_checkout_left_by_a_stopped_restore_is_adopted_after_the_repository_grew(self):
        self.retire()
        # A restore that stopped before the registry changed left a complete checkout at the path.
        work_workspace.unpack(self.registry()[self.identity], {})
        self.grow()
        result = self.gateway_acquire()
        self.assertNotIn('error', result)
        self.assert_restored()
        self.assertNotIn('retired', self.registry()[self.identity])

    def test_the_owner_restores_after_the_saved_digest_failure_and_keeps_every_record(self):
        records = {}

        def earlier_recovery(binding):
            # An earlier recovery of this binding moved newer evidence aside and kept its archive, under the name
            # that a first retirement gives. The retirement below then writes an archive with a time in its name.
            identity = binding['outcome'] + '-' + hashlib.sha256(str(self.repo).encode()).hexdigest()[:16]
            records['archive'] = self.workspaces / 'archive' / (identity + '.tar.gz')
            records['archive'].parent.mkdir(mode=0o700, exist_ok=True)
            records['archive'].write_bytes(b'earlier archive')
            records['residue'] = self.workspaces / 'archive' / 'residue' / (identity + '-1')
            records['residue'].mkdir(parents=True)
            (records['residue'] / 'ledger.json').write_text('{"run": 0}\n')
            binding['residue_recovery'] = dict(archive=str(records['archive']), residue=str(records['residue']),
                                               at='then', failure=dict(at='then', error='occupied'))
        self.retire(earlier_recovery)
        earlier, residue = records['archive'], records['residue']
        self.assertNotEqual(self.archive, earlier)
        self.grow()
        self.fail_first_restore()
        saved = self.registry()
        result = self.gateway_acquire()
        self.assertNotIn('error', result)
        self.assert_restored()
        binding = self.registry()[self.identity]
        self.assertNotIn('retired', binding)
        self.assertEqual(binding['residue_recovery'], saved[self.identity]['residue_recovery'])
        recovery = binding['digest_recovery']
        self.assertEqual((recovery['archive'], recovery['failure']),
                         (str(self.archive), self.before['retired']['restore_failed']))
        self.assertEqual(recovery['abbreviation'], [[7, 8]])
        # Both archives and the earlier residue stay, and no sweep deletes the archives.
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        os.utime(self.archive, (0, 0))
        os.utime(earlier, (0, 0))
        work_workspace.sweep_temporary(self.workspaces)
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual(earlier.read_bytes(), b'earlier archive')
        self.assertEqual((residue / 'ledger.json').read_text(), '{"run": 0}\n')
        # The next turn gets the live worktree.
        self.assertEqual(self.gateway_acquire()['path'], str(self.path))

    def test_the_owner_restores_into_the_empty_folder_that_a_native_claim_made(self):
        # The owner's saved path is an empty folder: a native claim made it after the failed restore.
        self.retire()
        self.grow()
        self.fail_first_restore()
        self.path.mkdir(parents=True)
        result = self.gateway_acquire()
        self.assertNotIn('error', result)
        self.assert_restored()
        self.assertIn('digest_recovery', self.registry()[self.identity])

    def test_a_stop_during_the_recovery_is_never_retried(self):
        self.retire()
        self.grow()
        self.fail_first_restore()
        on_disk = []

        def killed(*_, **__):
            # The process stops here. What the registry holds on disk now is what the next acquire finds.
            on_disk.append(self.manifest.read_text())
            raise KeyboardInterrupt
        with patch.object(work_workspace, 'unpack', side_effect=killed):
            with self.assertRaises(KeyboardInterrupt):
                acquire(self.workspaces, self.outcome, str(self.repo))
        self.manifest.write_text(on_disk[0])
        saved = self.registry()[self.identity]
        # The registry names the place of a partial checkout before the restore, and keeps the first failure.
        self.assertIn('checkout', saved['retired']['residue'])
        self.assertEqual(saved['retired']['restore_failed'], self.before['retired']['restore_failed'])
        self.assert_fenced(self.gateway_acquire(), saved)

    # Refusals: each keeps the saved failure, the archive, and the empty path.

    def test_a_saved_digest_failure_without_longer_names_stays_fenced(self):
        self.retire()
        self.fail_first_restore_without_growth()
        self.assert_fenced(self.gateway_acquire())
        self.assertIn('abbreviates the archived object names as before', self.gateway_acquire()['error'])

    def fail_first_restore_without_growth(self):
        with patch.object(work_workspace, 'matches_archive', lambda *_: False):
            with self.assertRaisesRegex(ValueError, 'do not match the archive'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        self.before = self.registry()[self.identity]
        self.assertEqual(set(self.before['retired']['restore_failed']), {'at', 'error'})

    def test_a_path_hint_and_another_outcome_never_restore_again(self):
        self.retire()
        self.grow()
        self.fail_first_restore()
        self.assertEqual(preferred(self.workspaces, self.outcome), self.workspaces / self.outcome)
        self.assertEqual(self.registry()[self.identity], self.before)
        other = dict(self.task, id=str(uuid4()), _execution_session_id=str(uuid4()))
        with self.service.transaction():
            self.service._put('tasks', other)
        self.assertIn('Only a linked worktree can be adopted',
                      self.gateway_acquire(other, existing_worktree=str(self.path))['error'])
        self.assertEqual(self.registry()[self.identity], self.before)
        self.assertFalse(self.path.exists())

    def test_files_at_the_path_or_an_interrupted_checkout_stay_fenced(self):
        self.retire()
        self.grow()
        self.fail_first_restore()
        self.path.mkdir(parents=True)
        (self.path / 'newer.txt').write_text('newer\n')
        self.assert_fenced(self.gateway_acquire())
        self.assertEqual((self.path / 'newer.txt').read_text(), 'newer\n')
        (self.path / 'newer.txt').unlink()
        self.path.rmdir()
        # A stop during an earlier attempt left a registered partial checkout.
        git(self.repo, 'worktree', 'add', '-q', '--detach', str(self.path))
        result = self.gateway_acquire()
        self.assertIn('Ask the operator', result['error'])
        self.assertEqual(self.registry()[self.identity], self.before)
        self.assertIn(str(self.path), git(self.repo, 'worktree', 'list', '--porcelain'))

    def test_a_moved_branch_or_another_archive_stays_fenced(self):
        self.retire()
        self.grow()
        self.fail_first_restore()
        for change in ('patch', 'metadata'):
            with tarfile.open(fileobj=io.BytesIO(self.archive_bytes)) as source, tarfile.open(self.archive, 'w:gz') as target:
                for member in source.getmembers():
                    data = source.extractfile(member).read() if member.isfile() else None
                    if change == 'patch' and member.name == 'unstaged.patch':
                        data = data.replace(b'QA change of index.tsx', b'QA change of other.tsx')
                    if change == 'metadata' and member.name == 'retirement.json':
                        data = json.dumps(dict(json.loads(data), head='0' * 40)).encode()
                    member.size = len(data) if data is not None else member.size
                    target.addfile(member, io.BytesIO(data) if data is not None else None)
            result = self.gateway_acquire()
            self.assertIn('Ask the operator', result['error'])
            self.assertIn('does not hold the saved changes' if change == 'patch' else 'another owner', result['error'])
            self.assertEqual(self.registry()[self.identity], self.before)
        self.archive.write_bytes(self.archive_bytes)
        commit = git(self.repo, *IDENTITY, 'commit-tree', git(self.repo, 'rev-parse', self.head + '^{tree}'),
                     '-p', self.head, '-m', 'Moved')
        git(self.repo, 'update-ref', 'refs/heads/' + self.branch, commit)
        result = self.gateway_acquire()
        self.assertIn('branch moved', result['error'])
        self.assertEqual(self.registry()[self.identity], self.before)
        self.assertFalse(self.path.exists())

    def test_other_saved_failures_and_a_second_recovery_stay_fenced(self):
        self.retire()
        self.grow()
        self.fail_first_restore()
        failure = self.before['retired']['restore_failed']
        for other in (dict(failure, digest_retry_failed=True), dict(failure, duplicate_retry_failed=True),
                      dict(failure, error='The restored files do not match.')):
            saved = self.registry()
            saved[self.identity]['retired']['restore_failed'] = other
            save_bindings(self.manifest, saved)
            self.assert_fenced(self.gateway_acquire(), saved[self.identity])
        saved = self.registry()
        saved[self.identity]['retired']['restore_failed'] = failure
        saved[self.identity]['digest_recovery'] = dict(archive='earlier', failure=failure, abbreviation=[], at='then')
        save_bindings(self.manifest, saved)
        self.assert_fenced(self.gateway_acquire(), saved[self.identity])

    def hook(self, script):
        hooks = Path(git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')) / 'hooks'
        hooks.mkdir(exist_ok=True)
        (hooks / 'post-checkout').write_text('#!/bin/sh\n' + script)
        (hooks / 'post-checkout').chmod(0o755)

    def test_a_real_difference_still_fails_and_keeps_the_partial_checkout(self):
        for name, script in (('untracked', f'echo late > {EVIDENCE}-late.txt\n'),
                             ('tracked', 'echo late >> notes.txt\n')):
            with self.subTest(name):
                self.setUp()
                self.retire()
                self.grow()
                self.fail_first_restore()
                # A hook of the repository writes into the new checkout, so it no longer holds the archived changes.
                self.hook(script)
                result = self.gateway_acquire()
                self.assertIn(DIGEST_FAILURE, result['error'])
                binding = self.registry()[self.identity]
                failure = binding['retired']['restore_failed']
                self.assertTrue(failure['digest_retry_failed'] and failure['duplicate_retry_failed'])
                checkout = Path(binding['retired']['residue']['checkout'])
                self.assertTrue((checkout / '.git-detached').is_file())
                self.assertFalse(self.path.exists())
                self.assertNotIn('branch refs/heads/' + self.branch, git(self.repo, 'worktree', 'list', '--porcelain'))
                self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
                # The next turn of the owner changes nothing.
                self.assertIn('Ask the operator', self.gateway_acquire()['error'])
                self.assertEqual(self.registry()[self.identity], binding)


class SamePatchTests(unittest.TestCase):
    ARCHIVED = (b'diff --git a/f b/f\nindex 5626abf..814f4a4 100644\n--- a/f\n+++ b/f\n@@ -1 +1,2 @@\n one\n+two\n')

    def full(self, line=b'index 5626abf0c4a1b2..814f4a42d9e3f7 100644'):
        return self.ARCHIVED.replace(b'index 5626abf..814f4a4 100644', line)

    def test_longer_names_of_the_same_change_match(self):
        self.assertTrue(work_workspace.same_patch(self.ARCHIVED, self.full()))
        self.assertTrue(work_workspace.same_patch(self.ARCHIVED, self.ARCHIVED))
        self.assertTrue(work_workspace.same_patch(b'', b''))

    def test_other_names_modes_or_lines_do_not_match(self):
        for line in (b'index 5626abe0c4a1b2..814f4a42d9e3f7 100644', b'index 5626abf0c4a1b2..814f4a42d9e3f7 100755',
                     b'index 5626abf0c4a1b2..814f4a42d9e3f7'):
            self.assertFalse(work_workspace.same_patch(self.ARCHIVED, self.full(line)))
        self.assertFalse(work_workspace.same_patch(self.ARCHIVED, self.full().replace(b'+two', b'+three')))
        self.assertFalse(work_workspace.same_patch(self.ARCHIVED, self.full() + b'extra\n'))
        short = self.ARCHIVED.replace(b'index 5626abf..814f4a4', b'index 562..814')
        self.assertFalse(work_workspace.same_patch(short, self.full().replace(b'5626abf0c4a1b2', b'562')))

    def test_only_the_untouched_digest_failure_counts(self):
        failure = dict(at='now', error=DIGEST_FAILURE)
        self.assertTrue(work_workspace.digest_failure(failure))
        for other in (dict(failure, digest_retry_failed=True), dict(failure, duplicate_retry_failed=True),
                      dict(failure, error='other'), None):
            self.assertFalse(work_workspace.digest_failure(other))


if __name__ == '__main__':
    unittest.main()
