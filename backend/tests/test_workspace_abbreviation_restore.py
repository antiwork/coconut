"""The restore of a retired worktree after Git began to abbreviate object names with more characters.

Git shortens the blob names in the index lines of a text patch by the number of objects in the repository. A fetch
after retirement can lengthen them, so the restored checkout gives other patch bytes than the archive for the same
changes, and the last comparison of unpack failed ("The restored files do not match the archive."). Each test retires
a real worktree with Coconut, grows the repository with git fast-import, and restores, through the gateway's
coconut_workspace (Service.gateway_tool and the plugin's _workspace) where the route matters.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
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


def legacy_comparison(path, patches, expected):
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
        (self.repo / '.gitignore').write_text('.knapsack_pro/\nsub/.gitattributes\n')
        # Tracked attributes give .dat files a text diff. A nested untracked or ignored file can override them.
        (self.repo / '.gitattributes').write_text('*.dat diff\n')
        for name in ('index.tsx', 'product_edit.ts', 'file_embeds_spec.rb', 'notes.txt', 'bin/data.dat', 'sub/notes.dat'):
            (self.repo / name).parent.mkdir(exist_ok=True)
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

    def shrink(self):
        """Repack as git gc does. The added objects are unreachable, so Git abbreviates object names as before."""
        git(self.repo, 'repack', '-a', '-d', '-q')

    def retire(self, earlier=None, shape='qa'):
        """Retire a worktree with edits to tracked files, untracked evidence, and ignored files.

        `earlier(binding)` runs before the retirement, for example to add the record of an earlier recovery. With
        shape 'gitignore', the only change is an unstaged line `.worktrees/` in .gitignore, as in the second owner's
        archive."""
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
        if shape == 'gitignore':
            with (self.path / '.gitignore').open('a') as stream:
                stream.write('.worktrees/\n')
        elif shape in ('attributes', 'attributes-link'):
            self.attribute_changes(link=shape == 'attributes-link')
        else:
            self.qa_changes()
        self.head = git(self.path, 'rev-parse', 'HEAD')
        self.before_retirement(self.path)
        self.service._next_worktree_retirement = 0
        self.assertEqual(self.service.retire_finished_worktrees(), [self.identity])
        self.assertFalse(self.path.exists())
        self.archive = Path(self.registry()[self.identity]['retired']['archive'])
        self.archive_bytes = self.archive.read_bytes()
        with tarfile.open(self.archive) as tar:
            self.staged = tar.extractfile('staged.patch').read()
            self.unstaged = tar.extractfile('unstaged.patch').read()
        self.assertEqual(len(index_names(self.staged)), 1 if shape == 'qa' else 0)

    def qa_changes(self):
        for name in ('index.tsx', 'product_edit.ts', 'file_embeds_spec.rb'):
            with (self.path / name).open('a') as stream:
                stream.write(f'QA change of {name}\n')
        # One edit is staged, so the archive holds a staged text patch too.
        git(self.path, 'add', 'file_embeds_spec.rb')
        (self.path / EVIDENCE).mkdir()
        (self.path / EVIDENCE / 'ledger.json').write_text('{"run": 1}\n')
        (self.path / EVIDENCE / 'report.md').write_text('# Run 1\n')
        (self.path / '.knapsack_pro').mkdir()
        (self.path / '.knapsack_pro' / 'report.json').write_text('{}\n')

    def attribute_changes(self, link=False):
        """Archived attributes files that mark two edited files binary, beside a text edit with short names.

        bin/.gitattributes is untracked, and sub/.gitattributes is ignored. With `link`, bin/.gitattributes is a link,
        which Git does not read, so bin/data.dat keeps a text patch."""
        if link:
            (self.path / 'attributes.txt').write_text('*.dat binary\n')
            (self.path / 'bin' / '.gitattributes').symlink_to('../attributes.txt')
        else:
            (self.path / 'bin' / '.gitattributes').write_text('*.dat binary\n')
        (self.path / 'sub' / '.gitattributes').write_text('*.dat binary\n')
        for name in ('bin/data.dat', 'sub/notes.dat', 'index.tsx'):
            with (self.path / name).open('a') as stream:
                stream.write(f'QA change of {name}\n')

    def before_retirement(self, path):
        """A test can read the worktree here, just before retirement removes it."""

    def rewrite_archive(self, staged=None, unstaged=None):
        """Replace the archived patches, and save the digest that they give, as a consistent but other archive."""
        with tarfile.open(fileobj=io.BytesIO(self.archive_bytes)) as source:
            members = [(member, source.extractfile(member).read() if member.isfile() else None)
                       for member in source.getmembers()]
        names, patches = [], {'staged.patch': staged, 'unstaged.patch': unstaged}
        with tarfile.open(self.archive, 'w:gz') as target:
            for member, data in members:
                if patches.get(member.name) is not None:
                    data = patches[member.name]
                    member.size = len(data)
                if member.name.startswith('untracked/'):
                    names.append(member.name[len('untracked/'):])
                target.addfile(member, io.BytesIO(data) if data is not None else None)
        self.archive_bytes = self.archive.read_bytes()
        saved = self.registry()
        saved[self.identity]['retired']['changes'] = changes_digest(
            self.staged if staged is None else staged, self.unstaged if unstaged is None else unstaged, names)
        saved[self.identity]['retired']['restore_failed'] = dict(at=OLD, error=DIGEST_FAILURE)
        save_bindings(self.manifest, saved)
        self.before = saved[self.identity]

    def git_state(self):
        """The refs, index, config, and object files of the repository, to show that a check wrote nothing."""
        common = Path(git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        files = {}
        for item in sorted(common.rglob('*')):
            relative = item.relative_to(common).as_posix()
            if item.is_file() and not relative.startswith(('logs/', 'worktrees/')):
                files[relative] = hashlib.sha256(item.read_bytes()).hexdigest()
        return files

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
        self.assertEqual(git(self.path, 'diff', '--cached', '--name-only'), 'file_embeds_spec.rb')
        self.assertEqual(git(self.path, 'diff', '--name-only').split(), ['index.tsx', 'product_edit.ts'])
        self.assertEqual((self.path / EVIDENCE / 'ledger.json').read_text(), '{"run": 1}\n')
        self.assertEqual((self.path / '.knapsack_pro' / 'report.json').read_text(), '{}\n')

    def assert_fenced(self, result, binding=None):
        self.assertIn('Ask the operator', result.get('error', ''))
        self.assertEqual(self.registry()[self.identity], self.before if binding is None else binding)
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertNotIn(str(self.path) + '\n', git(self.repo, 'worktree', 'list', '--porcelain') + '\n')

    def test_the_fixture_shows_the_longer_object_names(self):
        self.retire()
        archived = {len(name) for patch in (self.staged, self.unstaged) for pair in index_names(patch) for name in pair}
        self.grow()
        # The old side names blobs of the commit. Git now abbreviates each with one more character.
        current = {len(git(self.repo, 'rev-parse', '--short', git(self.repo, 'rev-parse', old.decode())))
                   for patch in (self.staged, self.unstaged) for old, _ in index_names(patch)}
        self.assertEqual((archived, current), ({7}, {8}))

    def test_the_fixture_shows_the_names_shrink_after_a_repack(self):
        self.retire()
        self.grow()
        self.shrink()
        current = {len(git(self.repo, 'rev-parse', '--short', git(self.repo, 'rev-parse', old.decode())))
                   for patch in (self.staged, self.unstaged) for old, _ in index_names(patch)}
        self.assertEqual(current, {7})

    # The repair.

    def test_a_restore_after_the_repository_grew_gets_the_archived_changes(self):
        self.retire()
        self.grow()
        result = self.gateway_acquire()
        self.assertNotIn('error', result)
        self.assert_restored()
        self.assertNotIn('retired', self.registry()[self.identity])
        # The patches of the checkout differ from the archive only in their longer object names, on both sides.
        for patch, arguments in ((self.staged, ('--cached', 'HEAD')), (self.unstaged, ())):
            restored = work_workspace.git_output(self.path, 'diff', *work_workspace.DIFF, *arguments)
            self.assertNotEqual(restored, patch)
            self.assertEqual({len(n) for pair in index_names(restored) for n in pair}, {8})

    def test_a_checkout_left_by_a_stopped_restore_is_adopted_after_the_repository_grew(self):
        self.retire()
        # A restore that stopped before the registry changed left a complete checkout at the path.
        work_workspace.unpack(self.registry()[self.identity], {})
        self.grow()
        result = self.gateway_acquire()
        self.assertNotIn('error', result)
        self.assert_restored()
        self.assertNotIn('retired', self.registry()[self.identity])

    def test_an_unchanged_leftover_checkout_is_adopted_without_reading_the_patches(self):
        self.retire()
        work_workspace.unpack(self.registry()[self.identity], {})
        real, reads = tarfile.open, []

        def counting(*arguments, **options):
            reads.append(arguments[0] if arguments else options.get('name'))
            return real(*arguments, **options)
        # compare_checkouts reads the archived files before the lock. Under the lock, a matching digest reads none.
        checked = work_workspace.compare_checkouts([self.registry()[self.identity]])
        with patch.object(work_workspace.tarfile, 'open', side_effect=counting):
            binding = self.registry()[self.identity]
            work_workspace.unpack(binding, checked)
        self.assertEqual(reads, [])

    def opened_under_lock(self):
        """Patch tarfile.open to record, for each open of an archive, whether the registry lock is held."""
        real, opens = tarfile.open, []

        def probe(*arguments, **options):
            with (self.workspaces / '.workspace.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    opens.append(False)
                except BlockingIOError:
                    opens.append(True)
            return real(*arguments, **options)
        return patch.object(work_workspace.tarfile, 'open', side_effect=probe), opens

    def leftover_after_growth(self):
        """A restore that stopped before the registry changed left a complete checkout. Then the repository grew."""
        self.retire()
        work_workspace.unpack(self.registry()[self.identity], {})
        self.grow()
        self.assertNotEqual(changes_digest(*changes(self.path)), self.registry()[self.identity]['retired']['changes'])

    def test_an_adoption_after_growth_reads_the_archive_only_before_the_lock(self):
        self.leftover_after_growth()
        probe, opens = self.opened_under_lock()
        with probe:
            binding = acquire(self.workspaces, self.outcome, str(self.repo))
        self.assertEqual(binding['path'], str(self.path))
        self.assert_restored()
        # compare_checkouts read the archive once without the lock. Under the lock, unpack used that read.
        self.assertEqual(opens, [False])

    def test_an_adoption_without_an_unlocked_comparison_compares_under_the_lock(self):
        self.leftover_after_growth()
        probe, opens = self.opened_under_lock()
        # The binding changed after the unlocked comparison, so the comparison has no entry for it.
        real = work_workspace.compare_checkouts
        calls = []

        def missing_first(bindings):
            bindings = list(bindings)
            calls.append(bindings)
            return {} if len(calls) == 1 else real(bindings)
        with probe, patch.object(work_workspace, 'compare_checkouts', side_effect=missing_first):
            self.assertEqual(acquire(self.workspaces, self.outcome, str(self.repo))['path'], str(self.path))
        self.assertEqual(len(calls), 2)
        self.assertEqual(opens, [True])
        self.assert_restored()

    def test_a_replaced_archive_is_compared_again_and_refused(self):
        self.leftover_after_growth()
        real = work_workspace.compare_checkouts
        calls = []

        def replace_after(bindings):
            result = real(bindings)
            if not calls:
                # Another file takes the archive's name after the unlocked read. Its patches are not the saved ones.
                other = self.archive.with_name('.other')
                with tarfile.open(fileobj=io.BytesIO(self.archive_bytes)) as source, tarfile.open(other, 'w:gz') as target:
                    for member in source.getmembers():
                        data = source.extractfile(member).read() if member.isfile() else None
                        if member.name == 'unstaged.patch':
                            data = data.replace(b'QA change of index.tsx', b'QA change of other.tsx')
                            member.size = len(data)
                        target.addfile(member, io.BytesIO(data) if data is not None else None)
                os.replace(other, self.archive)
            calls.append(result)
            return result
        with patch.object(work_workspace, 'compare_checkouts', side_effect=replace_after):
            with self.assertRaisesRegex(ValueError, 'is not empty and does not match the archive'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        self.assertEqual(len(calls), 2)
        # The checkout and the archive stay, and the restore is fenced.
        self.assertTrue((self.path / '.git').is_file())
        self.assertTrue(self.archive.exists())
        self.assertIn('restore_failed', self.registry()[self.identity]['retired'])

    def test_files_changed_after_the_unlocked_comparison_are_refused(self):
        self.leftover_after_growth()
        real = work_workspace.compare_checkouts

        def edit_after(bindings):
            result = real(bindings)
            (self.path / EVIDENCE / 'ledger.json').write_text('{"run": 2}\n')
            return result
        with patch.object(work_workspace, 'compare_checkouts', side_effect=edit_after):
            with self.assertRaisesRegex(ValueError, 'is not empty and does not match the archive'):
                acquire(self.workspaces, self.outcome, str(self.repo))
        self.assertEqual((self.path / EVIDENCE / 'ledger.json').read_text(), '{"run": 2}\n')
        self.assertTrue(self.archive.exists())

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

    # A saved digest failure after the names grew and shrank again, as for both Gumroad owners.

    def test_the_owner_restores_after_growth_and_a_repack_through_the_gateway(self):
        self.retire()
        self.grow()
        self.fail_first_restore()
        # A later git gc repacks: Git abbreviates the archived names as at retirement again.
        self.shrink()
        result = self.gateway_acquire(existing_worktree=str(self.path))
        self.assertNotIn('error', result)
        self.assert_restored()
        recovery = self.registry()[self.identity]['digest_recovery']
        self.assertEqual((recovery['archive'], recovery['failure']), (str(self.archive), self.before['retired']['restore_failed']))
        self.assertEqual(recovery['abbreviation'], [[7, 7]])
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual(list(self.workspaces.glob('.proof-*')), [])

    def test_a_gitignore_only_archive_restores_after_growth_and_a_repack(self):
        self.retire(shape='gitignore')
        self.assertIn(b'+.worktrees/', self.unstaged)
        self.grow()
        self.fail_first_restore()
        self.shrink()
        result = self.gateway_acquire()
        self.assertNotIn('error', result)
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD'), self.head)
        self.assertTrue((self.path / '.gitignore').read_text().endswith('.worktrees/\n'))
        self.assertEqual(git(self.path, 'diff', '--name-only'), '.gitignore')
        self.assertEqual(self.registry()[self.identity]['digest_recovery']['abbreviation'], [[7, 7]])
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)

    def test_archived_untracked_and_ignored_attributes_take_part_in_the_proof(self):
        self.retire(shape='attributes')
        # The archive holds binary patches for both .dat files, from the nested attributes, and a text patch.
        self.assertEqual(self.unstaged.count(b'GIT binary patch'), 2)
        self.assertEqual({len(n) for pair in index_names(self.unstaged) for n in pair}, {7, 40})
        self.grow()
        self.fail_first_restore()
        self.shrink()
        result = self.gateway_acquire()
        self.assertNotIn('error', result)
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD'), self.head)
        # The attributes files come back with their bytes, as untracked and ignored files, and stay out of Git.
        self.assertEqual((self.path / 'bin' / '.gitattributes').read_text(), '*.dat binary\n')
        self.assertEqual((self.path / 'sub' / '.gitattributes').read_text(), '*.dat binary\n')
        self.assertEqual(git(self.path, 'ls-files', '--others', '--exclude-standard'), 'bin/.gitattributes')
        self.assertEqual(git(self.path, 'diff', '--name-only').split(), ['bin/data.dat', 'index.tsx', 'sub/notes.dat'])
        self.assertTrue((self.path / 'sub' / 'notes.dat').read_text().endswith('QA change of sub/notes.dat\n'))
        # Binary patches always carry full names. The text patch has the names of retirement again.
        self.assertEqual(self.registry()[self.identity]['digest_recovery']['abbreviation'], [[7, 7], [40, 40]])
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual(list(self.workspaces.glob('.proof-*')), [])

    def test_an_archived_link_named_gitattributes_is_not_read_as_attributes(self):
        # Git does not read a .gitattributes that is a link, so the archive holds a text patch for bin/data.dat.
        self.retire(shape='attributes-link')
        self.assertEqual(self.unstaged.count(b'GIT binary patch'), 1)
        self.grow()
        self.fail_first_restore()
        self.shrink()
        result = self.gateway_acquire()
        self.assertNotIn('error', result)
        self.assertEqual(os.readlink(self.path / 'bin' / '.gitattributes'), '../attributes.txt')
        self.assertIn('digest_recovery', self.registry()[self.identity])

    def test_the_proof_runs_no_fsmonitor_and_writes_no_split_index_in_the_repository(self):
        self.retire(shape='attributes')
        marker = self.root / 'fsmonitor-ran'
        hook = self.root / 'fsmonitor.sh'
        hook.write_text(f'#!/bin/sh\ntouch {marker}\necho 1\nprintf "\\0"\n')
        hook.chmod(0o755)
        # The repository's own config: an fsmonitor hook, a split index, and an untracked cache.
        for key, value in (('core.fsmonitor', str(hook)), ('core.splitIndex', 'true'), ('core.untrackedCache', 'true')):
            git(self.repo, 'config', key, value)
        state = self.git_state()
        attributes = [('bin/.gitattributes', b'*.dat binary\n'), ('sub/.gitattributes', b'*.dat binary\n')]
        with tempfile.TemporaryDirectory(dir=self.workspaces, prefix='.proof-') as scratch:
            self.assertEqual(work_workspace.round_trip(self.repo, self.head, self.staged, self.unstaged, scratch,
                                                       attributes), [(7, 7), (40, 40)])
        self.assertFalse(marker.exists())
        self.assertEqual(self.git_state(), state)

    def test_an_archived_attributes_file_cannot_take_the_name_of_a_tracked_file(self):
        self.retire()
        with tempfile.TemporaryDirectory(dir=self.workspaces, prefix='.proof-') as scratch:
            with self.assertRaisesRegex(ValueError, 'name of a tracked file'):
                work_workspace.round_trip(self.repo, self.head, self.staged, self.unstaged, scratch,
                                          [('.gitattributes', b'* binary\n')])

    def test_an_archive_whose_attributes_do_not_explain_its_binary_patch_stays_fenced(self):
        # The archived attributes file says nothing about .dat files, so Git writes text patches: refuse, no effect.
        self.retire(shape='attributes')
        with tarfile.open(fileobj=io.BytesIO(self.archive_bytes)) as source:
            members = [(member, source.extractfile(member).read()) for member in source.getmembers()]
        with tarfile.open(self.archive, 'w:gz') as target:
            for member, data in members:
                if member.name.endswith('/.gitattributes'):
                    data = b'*.txt binary\n'
                    member.size = len(data)
                target.addfile(member, io.BytesIO(data))
        self.archive_bytes = self.archive.read_bytes()
        saved = self.registry()
        saved[self.identity]['retired']['restore_failed'] = dict(at=OLD, error=DIGEST_FAILURE)
        save_bindings(self.manifest, saved)
        self.before = saved[self.identity]
        state = self.git_state()
        result = self.gateway_acquire()
        self.assertIn('do not give the archived changes', result['error'])
        self.assert_refused_before_any_effect(result, state)

    def test_a_saved_digest_failure_without_a_current_difference_restores_once(self):
        self.retire()
        self.fail_first_restore_without_growth()
        result = self.gateway_acquire()
        self.assertNotIn('error', result)
        self.assert_restored()
        self.assertIn('digest_recovery', self.registry()[self.identity])

    # Archives that hold the saved digest but not what Git writes for the saved commit: no live effect.

    def assert_refused_before_any_effect(self, result, state):
        self.assert_fenced(result)
        self.assertFalse(self.path.exists())
        self.assertNotIn('residue', self.registry()[self.identity]['retired'])
        self.assertEqual(self.git_state(), state)
        self.assertEqual(list(self.workspaces.glob('.proof-*')), [])

    def test_a_consistent_archive_whose_patch_does_not_apply_stays_fenced(self):
        self.retire()
        self.rewrite_archive(unstaged=self.unstaged.replace(b' line 39 of index.tsx', b' line 39 of other.tsx'))
        state = self.git_state()
        result = self.gateway_acquire()
        self.assertIn('Ask the operator', result['error'])
        self.assert_refused_before_any_effect(result, state)

    def test_an_archive_of_the_earlier_writer_stays_fenced(self):
        # The earlier archive writer could write an entry twice. Its restores failed for other reasons.
        self.retire()
        with tarfile.open(fileobj=io.BytesIO(self.archive_bytes)) as source:
            members = [(member, source.extractfile(member).read()) for member in source.getmembers()]
        with tarfile.open(self.archive, 'w:gz') as target:
            for member, data in members + [next(row for row in members if row[0].name.startswith('ignored/'))]:
                target.addfile(member, io.BytesIO(data))
        self.archive_bytes = self.archive.read_bytes()
        saved = self.registry()
        saved[self.identity]['retired']['restore_failed'] = dict(at=OLD, error=DIGEST_FAILURE)
        save_bindings(self.manifest, saved)
        self.before = saved[self.identity]
        state = self.git_state()
        result = self.gateway_acquire()
        self.assertIn('earlier archive writer', result['error'])
        self.assert_refused_before_any_effect(result, state)

    def test_a_consistent_archive_that_git_does_not_write_stays_fenced(self):
        written = {}
        self.before_retirement = lambda path: written.setdefault(
            'patch', work_workspace.git_output(path, 'diff', *work_workspace.DIFF, '-U1'))
        self.retire()
        # The patch applies, but Git writes three lines of context, not one.
        self.rewrite_archive(unstaged=written['patch'])
        state = self.git_state()
        result = self.gateway_acquire()
        self.assertIn('Ask the operator', result['error'])
        self.assert_refused_before_any_effect(result, state)

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
        # A host core.hooksPath makes Git ignore this repository's hooks, and the restore would then run none.
        git(self.repo, 'config', 'core.hooksPath', str(hooks))

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
