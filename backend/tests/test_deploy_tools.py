import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4


SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
spec = importlib.util.spec_from_file_location('coconut_deploy_tools', SCRIPTS / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)


def package_files(**changes):
    files = {
        'backend/coconut/__init__.py': b'"""Coconut service."""\n',
        'backend/coconut/__main__.py': b'print("service entry")\n',
        'scripts/host.py': b'print("host entry")\n',
        'README.md': b'# Coconut\n',
    }
    files.update(changes)
    return files


def hashes(files):
    return {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}


def archive(files=None, *, manifest=None, include_manifest=True, extra=()):
    files = package_files() if files is None else files
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode='w:gz') as tar:
        entries = list(files.items())
        if include_manifest:
            manifest = {'version': 1, 'files': hashes(files)} if manifest is None else manifest
            content = manifest if isinstance(manifest, bytes) else json.dumps(manifest).encode()
            entries.append(('deployment.json', content))
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
        for info, content in extra:
            tar.addfile(info, io.BytesIO(content))
    result.seek(0)
    return result


class FakeOperations:
    def __init__(self, *, mode='stopped', fail=None, fail_stop_after=None, hermes='ok'):
        self.calls = []
        self.state = {'mode': mode}
        self.hermes_status = hermes
        self.fail = fail
        self.fail_stop_after = fail_stop_after
        self.stop_count = 0

    def record(self, name, root, *args):
        self.calls.append((name, Path(root), *args))
        if self.fail == name:
            raise deploy.DeployError('The injected operation failed.')

    def preflight(self, root, address, package):
        self.record('preflight', root, address, package)

    def inspect(self, root):
        self.record('inspect', root)
        return self.state.copy()

    def assert_stopped(self, root):
        self.record('assert_stopped', root)

    def stop(self, root):
        self.stop_count += 1
        self.record('stop', root)
        if self.fail_stop_after is not None and self.stop_count > self.fail_stop_after:
            raise deploy.DeployError('The injected stop failed.')
        return self.state.copy()

    def stop_recovery(self, root, source):
        self.record('stop_recovery', root, source)
        return self.state.copy()

    def prepare(self, root, address, package):
        self.record('prepare', root, address, package)

    def restore(self, root, state):
        self.record('restore', root, state)

    def hermes(self, root):
        self.record('hermes', root)
        return {'status': self.hermes_status, 'version': '0.9.0',
                'reason': None if self.hermes_status == 'ok' else 'Coconut found no Hermes on this Mac.'}

    def install(self, root, address):
        self.record('install', root, address)

    def health(self, root, address):
        self.record('health', root, address)
        return {'status': 'ok', 'provider': 'hermes', 'ready': True}

    def probe(self, root, address):
        # A probe is a short health check. An injected health failure fails it too.
        self.record('probe', root, address)
        if self.fail == 'health':
            raise deploy.DeployError('The injected operation failed.')
        return {'status': 'ok', 'provider': 'hermes', 'ready': True}


class DeploymentPackageTests(unittest.TestCase):
    def test_valid_package_keeps_exact_file_bytes(self):
        files = package_files()
        files.update({'backend/coconut/nested/module.py': b'value = 1\n', 'backend/pyproject.toml': b'[project]\nname="coconut"\n',
                      'backend/coconut/hermes_versions.json': b'{"minimum": "0.22.4", "tested": ["0.22.4"]}\n',
                      'scripts/desktop.swift': b'import AppKit\n', 'docs/setup/mac.md': b'# Mac setup\n',
                      'Start Coconut.command': b'#!/bin/zsh\n', 'Uninstall Coconut.command': b'#!/bin/zsh\n'})
        result = deploy.validate_package(archive(files))
        self.assertEqual(result.files, files)
        self.assertEqual(result.hashes, hashes(files))
        self.assertRegex(result.package_id, r'^[0-9a-f]{64}$')
        self.assertEqual(result.package_id, deploy.validate_package(archive(files)).package_id)

    def test_rejects_paths_outside_the_source_allowlist(self):
        invalid = ['../escape.py', '/tmp/escape.py', 'backend/coconut/../escape.py', 'backend//coconut/module.py',
                   './README.md', 'backend\\coconut\\module.py', '.env', '.runtime/coconut.sqlite3',
                   'backend/coconut/settings.json', 'scripts/private/key.txt', 'scripts/nested/run.py', 'docs/secret.key']
        for name in invalid:
            with self.subTest(name=name), self.assertRaises(deploy.DeployError):
                deploy.validate_package(archive(package_files(**{name: b'unsafe'})))

    def test_rejects_symbolic_links_hard_links_directories_and_devices(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            info = tarfile.TarInfo('backend/coconut/extra.py')
            info.type = kind
            info.linkname = '../../outside'
            with self.subTest(kind=kind), self.assertRaises(deploy.DeployError):
                deploy.validate_package(archive(extra=[(info, b'')]))

    def test_rejects_duplicate_archive_names(self):
        info = tarfile.TarInfo('README.md')
        info.size = 9
        with self.assertRaises(deploy.DeployError):
            deploy.validate_package(archive(extra=[(info, b'duplicate')]))

    def test_requires_the_manifest_and_exact_manifest_shape(self):
        bad_manifests = [b'not json', [], {'version': 2, 'files': hashes(package_files())},
                         {'version': True, 'files': hashes(package_files())}, {'version': 1},
                         {'version': 1, 'files': hashes(package_files()), 'extra': True}, {'version': 1, 'files': []}]
        for manifest in bad_manifests:
            with self.subTest(manifest=manifest), self.assertRaises(deploy.DeployError):
                deploy.validate_package(archive(manifest=manifest))
        with self.assertRaises(deploy.DeployError):
            deploy.validate_package(archive(include_manifest=False))

    def test_manifest_must_match_all_files_and_their_hashes(self):
        original = hashes(package_files())
        cases = [original | {'README.md': '0' * 64}, original | {'README.md': original['README.md'].upper()},
                 original | {'README.md': 17}, original | {'README.md': 'not-a-hash'},
                 {name: value for name, value in original.items() if name != 'README.md'},
                 original | {'scripts/absent.py': '0' * 64}]
        for manifest_files in cases:
            with self.subTest(files=manifest_files), self.assertRaises(deploy.DeployError):
                deploy.validate_package(archive(manifest={'version': 1, 'files': manifest_files}))

    def test_requires_service_entries(self):
        required = ['backend/coconut/__init__.py', 'backend/coconut/__main__.py', 'scripts/host.py']
        for name in required:
            files = package_files()
            del files[name]
            with self.subTest(name=name), self.assertRaises(deploy.DeployError):
                deploy.validate_package(archive(files))

    def test_rejects_non_gzip_and_truncated_archives(self):
        complete = archive().getvalue()
        for content in (b'not a tar file', complete[:40], complete[:len(complete) // 2]):
            with self.subTest(size=len(content)), self.assertRaises(deploy.DeployError):
                deploy.validate_package(io.BytesIO(content))

    def test_enforces_compressed_uncompressed_file_and_count_limits(self):
        files = package_files()
        compressed = archive(files).getvalue()
        cases = [('MAX_COMPRESSED_BYTES', len(compressed) - 1), ('MAX_UNCOMPRESSED_BYTES', 20),
                 ('MAX_FILE_BYTES', 20), ('MAX_FILES', len(files) - 1)]
        for name, limit in cases:
            with self.subTest(limit=name), patch.object(deploy, name, limit), self.assertRaises(deploy.DeployError):
                deploy.validate_package(io.BytesIO(compressed))


class DeploymentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name).resolve()
        self.home = self.parent / 'home'
        self.home.mkdir()
        self.target = self.home / 'Coconut'
        self.address = 'https://mini.example.ts.net:8960'

    def tearDown(self):
        self.temporary.cleanup()

    def receive(self, *, files=None, operations=None):
        return deploy.receive('Coconut', self.address, archive(files), home=self.home, operations=operations or FakeOperations())

    def test_fresh_install_writes_only_package_files_and_records_ownership(self):
        operations = FakeOperations()
        files = package_files()
        result = self.receive(files=files, operations=operations)
        for name, content in files.items():
            self.assertEqual((self.target / name).read_bytes(), content)
        marker = json.loads((self.target / '.coconut-install.json').read_text())
        self.assertEqual(marker['files'], hashes(files))
        self.assertEqual(marker['state'], 'installed')
        self.assertEqual(marker['package_id'], result['package_id'])
        self.assertEqual(marker['installation_id'], result['installation_id'])
        self.assertEqual(result['mode'], 'managed')
        self.assertFalse(result['updated'])
        self.assertIn('install', [call[0] for call in operations.calls])
        self.assertEqual(result['health']['status'], 'ok')

    def test_existing_unowned_directory_is_rejected_before_any_operation(self):
        self.target.mkdir()
        private = self.target / 'private.txt'
        private.write_text('Keep this file.')
        operations = FakeOperations()
        with self.assertRaises(deploy.DeployError):
            self.receive(operations=operations)
        self.assertEqual(private.read_text(), 'Keep this file.')
        self.assertEqual(operations.calls, [])

    def test_symbolic_target_cannot_redirect_installation(self):
        outside = self.parent / 'outside'
        outside.mkdir()
        self.target.symlink_to(outside, target_is_directory=True)
        operations = FakeOperations()
        with self.assertRaises(deploy.DeployError):
            self.receive(operations=operations)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(operations.calls, [])

    def test_invalid_package_cannot_stop_the_existing_service(self):
        operations = FakeOperations()
        with self.assertRaises(deploy.DeployError):
            deploy.receive('Coconut', self.address, io.BytesIO(b'bad archive'), home=self.home, operations=operations)
        self.assertEqual(operations.calls, [])
        self.assertFalse(self.target.exists())

    def test_update_preserves_host_data_and_service_mode(self):
        first = self.receive()
        data = self.target / '.runtime/data/coconut.sqlite3'
        data.parent.mkdir(parents=True)
        data.write_bytes(b'Durable host history.')
        files = package_files(**{'README.md': b'# Updated Coconut\n'})
        operations = FakeOperations(mode='manual')
        result = self.receive(files=files, operations=operations)
        self.assertTrue(result['updated'])
        self.assertEqual(result['mode'], 'manual')
        self.assertEqual(result['installation_id'], first['installation_id'])
        self.assertEqual((self.target / 'README.md').read_bytes(), files['README.md'])
        self.assertEqual(data.read_bytes(), b'Durable host history.')
        self.assertNotIn('install', [call[0] for call in operations.calls])

    def test_update_removes_only_files_owned_by_the_previous_package(self):
        self.receive(files=package_files(**{'docs/old.md': b'Old package documentation.\n'}))
        personal = self.target / 'docs/personal.md'
        personal.write_bytes(b'Keep this local note.\n')
        self.receive(files=package_files(), operations=FakeOperations(mode='manual'))
        self.assertFalse((self.target / 'docs/old.md').exists())
        self.assertEqual(personal.read_bytes(), b'Keep this local note.\n')

    def test_an_update_lets_the_running_turns_finish_before_it_stops_the_service(self):
        class DrainingOperations(FakeOperations):
            def drain(self, root):
                self.record('drain', root)
                return {'drained': True}
        self.receive()
        operations = DrainingOperations(mode='managed')
        self.receive(files=package_files(**{'README.md': b'New code.\n'}), operations=operations)
        names = [call[0] for call in operations.calls]
        # The drain comes first: a stop that interrupts a turn loses its tool results, and its task fails at the next start.
        self.assertLess(names.index('drain'), names.index('stop'))
        self.assertEqual(names.count('drain'), 1)

    def save_hermes_status(self, status):
        """Write the status that the running service saved at its start. See hermes_runtime.status."""
        path = self.target / '.runtime/host/hermes.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'status': status, 'version': '0.9.0', 'reason': None}))

    def test_an_update_that_breaks_a_working_hermes_restores_the_earlier_code(self):
        self.receive()
        self.save_hermes_status('ok')
        operations = FakeOperations(mode='managed', hermes='incompatible')
        with self.assertRaisesRegex(deploy.DeployError, 'does not work with the installed Hermes'):
            self.receive(files=package_files(**{'README.md': b'New code.\n'}), operations=operations)
        self.assertEqual((self.target / 'README.md').read_bytes(), package_files()['README.md'])
        self.assertIn(('restore', self.target, operations.state), operations.calls)

    def test_an_update_from_the_private_runtime_needs_a_working_hermes(self):
        # Versions before the Hermes check ran on their own private Hermes, which worked. They saved no status.
        self.receive()
        operations = FakeOperations(mode='managed', hermes='missing')
        with self.assertRaisesRegex(deploy.DeployError, 'no Hermes'):
            self.receive(files=package_files(**{'README.md': b'New code.\n'}), operations=operations)
        self.assertEqual((self.target / 'README.md').read_bytes(), package_files()['README.md'])

    def test_an_update_installs_when_hermes_was_already_unavailable(self):
        self.receive()
        self.save_hermes_status('missing')
        files = package_files(**{'README.md': b'New code.\n'})
        self.receive(files=files, operations=FakeOperations(mode='managed', hermes='missing'))
        self.assertEqual((self.target / 'README.md').read_bytes(), files['README.md'])

    def test_the_new_code_starts_with_the_result_of_its_check(self):
        self.receive()
        self.save_hermes_status('ok')
        self.receive(files=package_files(**{'README.md': b'New code.\n'}), operations=FakeOperations(mode='managed'))
        saved = json.loads((self.target / '.runtime/host/hermes.json').read_text())
        self.assertEqual(saved['status'], 'ok')

    def test_a_fresh_install_starts_without_hermes(self):
        operations = FakeOperations(hermes='missing')
        self.receive(operations=operations)
        self.assertIn('hermes', [call[0] for call in operations.calls])
        self.assertIn('install', [call[0] for call in operations.calls])

    def test_recovery_restores_the_runtime_and_settings_of_an_older_installers_update(self):
        # An older installer moved .runtime/hermes to hermes-rollback-<id> and stopped before it moved the shared settings.
        self.receive()
        runtime = self.target / '.runtime/hermes'
        (runtime / 'terminal/hermes').mkdir(parents=True)
        (runtime / 'terminal/hermes/auth.json').write_text('Login.')
        (runtime / 'install.json').write_text(json.dumps({'commit': 'a' * 40}))
        with self.assertRaises(deploy.DeployError):
            self.receive(files=package_files(**{'README.md': b'New code.\n'}),
                         operations=FakeOperations(mode='managed', fail='prepare', fail_stop_after=1))
        journal = self.target / '.runtime/deploy/update.json'
        record = json.loads(journal.read_text())
        record['runtime_change'] = {'previous': 'a' * 40, 'candidate': 'b' * 40}
        journal.write_text(json.dumps(record))
        saved = self.target / f".runtime/hermes-rollback-{record['deployment_id']}"
        runtime.rename(saved)
        runtime.mkdir()
        (runtime / 'install.json').write_text(json.dumps({'commit': 'b' * 40}))
        files = package_files(**{'README.md': b'Newer code.\n'})
        self.receive(files=files, operations=FakeOperations(mode='managed'))
        self.assertEqual((runtime / 'terminal/hermes/auth.json').read_text(), 'Login.')
        self.assertEqual(json.loads((runtime / 'install.json').read_text())['commit'], 'a' * 40)
        self.assertFalse(saved.exists())
        self.assertEqual((self.target / 'README.md').read_bytes(), files['README.md'])

    def test_a_runtime_copy_with_the_shared_settings_stays(self):
        self.receive()
        copy = self.target / f'.runtime/hermes-rollback-{uuid4()}'
        (copy / 'terminal/hermes').mkdir(parents=True)
        self.receive(files=package_files(**{'README.md': b'New code.\n'}), operations=FakeOperations(mode='managed'))
        self.assertTrue((copy / 'terminal/hermes').is_dir())

    def test_a_finished_update_removes_old_runtime_copies(self):
        self.receive()
        runtime = self.target / '.runtime'
        copies = [runtime / f'hermes-rollback-{uuid4()}', runtime / f'hermes-failed-{uuid4()}']
        manual = runtime / 'hermes-upgrade-2026-9-11'
        for path in (*copies, manual):
            (path / 'venv').mkdir(parents=True)
        self.receive(files=package_files(**{'README.md': b'New code.\n'}), operations=FakeOperations(mode='managed'))
        self.assertFalse(any(path.exists() for path in copies))
        self.assertTrue(manual.is_dir())

    def test_an_update_without_a_drain_step_still_stops_the_service(self):
        operations = FakeOperations(mode='managed')
        self.assertFalse(hasattr(operations, 'drain'))
        self.receive()
        self.receive(files=package_files(**{'README.md': b'New code.\n'}), operations=operations)
        self.assertIn('stop', [call[0] for call in operations.calls])

    def test_failed_start_restores_owned_code_and_keeps_host_data(self):
        self.receive()
        before = {name: (self.target / name).read_bytes() for name in package_files()}
        marker = (self.target / '.coconut-install.json').read_bytes()
        private = self.target / '.runtime/private.txt'
        private.write_bytes(b'Keep durable data.')
        operations = FakeOperations(mode='managed', fail='health')
        with self.assertRaises(deploy.DeployError):
            self.receive(files=package_files(**{'README.md': b'New code that did not start.\n'}), operations=operations)
        self.assertEqual({name: (self.target / name).read_bytes() for name in before}, before)
        self.assertEqual(private.read_bytes(), b'Keep durable data.')
        self.assertEqual((self.target / '.coconut-install.json').read_bytes(), marker)
        self.assertGreaterEqual(operations.stop_count, 2)
        self.assertIn(('restore', self.target, operations.state), operations.calls)

    def test_recovery_accepts_restored_service_that_started_after_deadline(self):
        self.receive()
        with self.assertRaises(deploy.DeployError):
            self.receive(files=package_files(**{'README.md': b'New code.\n'}),
                         operations=FakeOperations(mode='managed', fail='health'))
        journal = self.target / '.runtime/deploy/update.json'
        self.assertEqual(json.loads(journal.read_text())['phase'], 'code_restored')
        operations = FakeOperations(mode='managed')
        deploy.reconcile(self.target, operations)
        # The recovery check uses the short probe, not the full startup wait.
        self.assertEqual([call[0] for call in operations.calls], ['inspect', 'probe'])
        self.assertFalse(journal.exists())

    def test_failed_stop_does_not_replace_code(self):
        self.receive()
        before = (self.target / 'README.md').read_bytes()
        operations = FakeOperations(mode='managed', fail='stop')
        with self.assertRaises(deploy.DeployError):
            self.receive(files=package_files(**{'README.md': b'Replacement\n'}), operations=operations)
        self.assertEqual((self.target / 'README.md').read_bytes(), before)
        self.assertNotIn('prepare', [call[0] for call in operations.calls])

    def test_failed_cleanup_stop_keeps_current_code_for_the_running_process(self):
        self.receive()
        replacement = b'New code remains while stop is unconfirmed.\n'
        operations = FakeOperations(mode='managed', fail='health', fail_stop_after=1)
        with self.assertRaises(deploy.DeployError):
            self.receive(files=package_files(**{'README.md': replacement}), operations=operations)
        self.assertEqual((self.target / 'README.md').read_bytes(), replacement)
        self.assertEqual(operations.stop_count, 2)

    def test_partial_source_copy_restores_prior_code_before_restarting(self):
        self.receive()
        before = {name: (self.target / name).read_bytes() for name in package_files()}
        files = package_files(**{'backend/coconut/__init__.py': b'"""Replacement package."""\n',
                                 'backend/coconut/__main__.py': b'print("replacement entry")\n'})
        operations = FakeOperations(mode='managed')
        replace = deploy.os.replace
        injected = []
        def fail_one_copy(source, target):
            if not injected and any(part.startswith('stage-') for part in Path(source).parts) and Path(target).name == '__main__.py':
                injected.append(True)
                raise OSError('The injected source copy failed.')
            return replace(source, target)
        with patch.object(deploy.os, 'replace', side_effect=fail_one_copy), self.assertRaises(deploy.DeployError):
            self.receive(files=files, operations=operations)
        self.assertEqual(injected, [True])
        self.assertEqual({name: (self.target / name).read_bytes() for name in before}, before)
        self.assertIn(('restore', self.target, operations.state), operations.calls)

    def test_redeploy_after_uninstall_keeps_source_ownership_and_starts_managed_service(self):
        first = self.receive()
        shutil.rmtree(self.target / '.runtime')
        self.assertTrue((self.target / '.coconut-install.json').is_file())
        operations = FakeOperations(mode='stopped')
        result = self.receive(operations=operations)
        self.assertEqual(result['installation_id'], first['installation_id'])
        self.assertEqual(result['mode'], 'managed')
        self.assertTrue(result['updated'])
        self.assertIn('install', [call[0] for call in operations.calls])
        self.assertEqual(result['health']['status'], 'ok')

    def test_failed_reinstall_still_starts_managed_service_on_retry(self):
        first = self.receive()
        shutil.rmtree(self.target / '.runtime')
        with self.assertRaises(deploy.DeployError):
            self.receive(operations=FakeOperations(mode='stopped', fail='prepare'))
        retry = FakeOperations(mode='stopped')
        result = self.receive(operations=retry)
        self.assertEqual(result['installation_id'], first['installation_id'])
        self.assertEqual(result['mode'], 'managed')
        self.assertIn('install', [call[0] for call in retry.calls])

    def test_fresh_rollback_interruption_after_host_removal_recovers_on_retry(self):
        operations = FakeOperations(fail='health')
        prepare = operations.prepare
        history = self.target / '.runtime/host/coconut.sqlite3'
        def prepare_with_history(root, address, package):
            prepare(root, address, package)
            history.parent.mkdir(parents=True, exist_ok=True)
            history.write_bytes(b'Durable history from the first service start.')
        operations.prepare = prepare_with_history
        unlink = Path.unlink
        interrupted = []
        def interrupt_after_host_removal(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            if path == self.target / 'scripts/host.py' and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt()
            return result
        with patch.object(Path, 'unlink', new=interrupt_after_host_removal), self.assertRaises(KeyboardInterrupt):
            self.receive(operations=operations)
        self.assertEqual(interrupted, [True])
        self.assertFalse((self.target / 'scripts/host.py').exists())
        journal = self.target / '.runtime/deploy/update.json'
        record = json.loads(journal.read_text())
        self.assertEqual(record['phase'], 'rollback_stopped')
        recovery_source = journal.parent / record['backup'] / 'scripts/host.py'
        self.assertEqual(recovery_source.read_bytes(), package_files()['scripts/host.py'])
        retry = FakeOperations(mode='stopped')
        result = self.receive(operations=retry)
        self.assertEqual(result['mode'], 'managed')
        self.assertIn('install', [call[0] for call in retry.calls])
        self.assertEqual(history.read_bytes(), b'Durable history from the first service start.')
        self.assertEqual({name: (self.target / name).read_bytes() for name in package_files()}, package_files())
        self.assertFalse((self.target / '.runtime/deploy/update.json').exists())
        self.assertIn(('stop_recovery', self.target, recovery_source), retry.calls)
        names = [call[0] for call in retry.calls]
        self.assertLess(names.index('stop_recovery'), names.index('install'))

    def test_interrupted_reinstall_directory_creation_does_not_count_as_installed_runtime(self):
        first = self.receive()
        shutil.rmtree(self.target / '.runtime')
        (self.target / '.runtime/deploy').mkdir(parents=True)
        (self.target / '.runtime/deploy/deploy.lock').touch()
        retry = FakeOperations(mode='stopped')
        result = self.receive(operations=retry)
        self.assertEqual(result['installation_id'], first['installation_id'])
        self.assertEqual(result['mode'], 'managed')
        self.assertIn('install', [call[0] for call in retry.calls])
        self.assertEqual(json.loads((self.target / '.runtime/deploy/runtime.json').read_text()),
                         {'version': 1, 'installation_id': first['installation_id']})

    def test_managed_local_edit_is_rejected_before_service_stop(self):
        self.receive()
        note = self.target / 'README.md'
        note.write_bytes(b'Keep this local source edit.\n')
        operations = FakeOperations(mode='managed')
        with self.assertRaisesRegex(deploy.DeployError, 'local edit'):
            self.receive(operations=operations)
        self.assertEqual(note.read_bytes(), b'Keep this local source edit.\n')
        self.assertNotIn('stop', [call[0] for call in operations.calls])
        self.assertNotIn('restore', [call[0] for call in operations.calls])

    def interrupt_receiver(self, files, crash_point):
        package = self.parent / 'interrupted-package.tar.gz'
        package.write_bytes(archive(files).getvalue())
        program = textwrap.dedent('''
            import importlib.util
            import os
            from pathlib import Path
            import sys
            spec = importlib.util.spec_from_file_location('deployment_crash_tests', sys.argv[1])
            tests = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = tests
            spec.loader.exec_module(tests)
            root = Path(sys.argv[2]) / 'Coconut'
            point = sys.argv[4]
            def crash():
                (root / '.runtime/host/coconut.sqlite3').write_bytes(b'History advanced during the interrupted update.')
                (root / '.runtime/host/decisions.json').write_bytes(b'An approval completed during the interrupted update.')
                os._exit(77)
            replace = tests.deploy.os.replace
            def interrupted_replace(source, target):
                result = replace(source, target)
                staged = any(part.startswith('stage-') for part in Path(source).parts)
                if staged and ((point == 'first_copy' and Path(target) == root / 'backend/coconut/__init__.py')
                               or (point == 'new_file' and Path(target) == root / 'docs/new.md')):
                    crash()
                return result
            unlink = Path.unlink
            def interrupted_unlink(path, *args, **kwargs):
                result = unlink(path, *args, **kwargs)
                if point == 'obsolete_unlink' and path == root / 'docs/obsolete.md':
                    crash()
                return result
            class CrashOperations(tests.FakeOperations):
                def restore(self, root, state):
                    if point == 'restore_mode':
                        crash()
                    super().restore(root, state)
            operations = CrashOperations(mode='manual')
            operations.state['options'] = {'fixture': 'original service options'}
            tests.deploy.os.replace = interrupted_replace
            Path.unlink = interrupted_unlink
            with open(sys.argv[3], 'rb') as stream:
                tests.deploy.receive('Coconut', sys.argv[5], stream, home=Path(sys.argv[2]), operations=operations)
            raise AssertionError('The injected interruption did not happen.')
        ''')
        result = subprocess.run([sys.executable, '-c', program, str(Path(__file__).resolve()), str(self.home),
                                 str(package), crash_point, self.address], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 77, result.stderr)

    def assert_subprocess_recovery(self, crash_point, *, same_package=False, interrupt_recovery=False):
        original_files = package_files(**{'docs/obsolete.md': b'Original documentation.\n'})
        initial = self.receive(files=original_files)
        original_marker = json.loads((self.target / '.coconut-install.json').read_text())
        runtime = self.target / '.runtime/host'
        runtime.mkdir(exist_ok=True)
        (runtime / 'coconut.sqlite3').write_bytes(b'Initial history.')
        (runtime / 'decisions.json').write_bytes(b'Initial pending decision.')
        (runtime / 'connection.json').write_bytes(b'Original paired connection.')
        replacement = original_files if same_package else package_files(**{
            'backend/coconut/__init__.py': b'"""Updated service package."""\n',
            'README.md': b'# Updated Coconut\n', 'docs/new.md': b'New documentation.\n',
        })
        self.interrupt_receiver(replacement, crash_point)
        journal = self.target / '.runtime/deploy/update.json'
        record = json.loads(journal.read_text())
        self.assertEqual(record['phase'], 'applying')
        self.assertNotEqual(record['deployment_id'], original_marker['deployment_id'])
        self.assertEqual(json.loads((self.target / '.coconut-install.json').read_text())['files'], hashes(original_files))
        if crash_point == 'obsolete_unlink':
            self.assertFalse((self.target / 'docs/obsolete.md').exists())
        if crash_point == 'new_file':
            self.assertTrue((self.target / 'docs/new.md').exists())
        if interrupt_recovery:
            self.interrupt_receiver(replacement, 'restore_mode')
            record = json.loads(journal.read_text())
            self.assertEqual(record['phase'], 'code_restored')
            self.assertTrue((journal.parent / record['backup']).is_dir())
        restored = []
        class RecoveryOperations(FakeOperations):
            def restore(operation, root, state):
                restored.append({name: (root / name).read_bytes() for name in original_files if (root / name).is_file()})
                super().restore(root, state)
                operation.state = state.copy()
        operations = RecoveryOperations(mode='stopped')
        result = self.receive(files=replacement, operations=operations)
        self.assertEqual(restored[0], original_files)
        self.assertEqual(result['mode'], 'manual')
        self.assertEqual(result['installation_id'], initial['installation_id'])
        self.assertEqual({name: (self.target / name).read_bytes() for name in replacement}, replacement)
        self.assertEqual((runtime / 'coconut.sqlite3').read_bytes(), b'History advanced during the interrupted update.')
        self.assertEqual((runtime / 'decisions.json').read_bytes(), b'An approval completed during the interrupted update.')
        self.assertEqual((runtime / 'connection.json').read_bytes(), b'Original paired connection.')
        self.assertFalse(journal.exists())
        self.assertEqual(list(journal.parent.glob('rollback-*')), [])
        self.assertEqual(list(journal.parent.glob('stage-*')), [])
        names = [call[0] for call in operations.calls]
        self.assertLess(names.index('stop'), names.index('restore'))
        self.assertLess(names.index('restore'), names.index('prepare'))
        first_restore = next(call for call in operations.calls if call[0] == 'restore')
        self.assertEqual(first_restore[2], {'mode': 'manual', 'options': {'fixture': 'original service options'}})

    def test_process_crash_after_first_source_copy_recovers_before_retry(self):
        self.assert_subprocess_recovery('first_copy')

    def test_process_crash_after_new_file_copy_recovers_before_retry(self):
        self.assert_subprocess_recovery('new_file')

    def test_process_crash_after_obsolete_file_removal_recovers_before_retry(self):
        self.assert_subprocess_recovery('obsolete_unlink')

    def test_same_package_interruption_still_restores_original_mode(self):
        self.assert_subprocess_recovery('first_copy', same_package=True)

    def test_recovery_interruption_keeps_backup_until_original_mode_returns(self):
        self.assert_subprocess_recovery('new_file', interrupt_recovery=True)


class DeploymentCommandTests(unittest.TestCase):
    def test_a_group_that_the_system_calls_not_permitted_is_treated_as_gone(self):
        # On macOS, killpg answers "not permitted" for a group whose last process has just exited and is not yet reaped.
        # That surfaced as an error of a deployment command that had already finished, about once in seventy loaded runs.
        process = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        process.wait()
        calls = []
        def killpg(group, signal_number):
            calls.append(signal_number)
            raise PermissionError(1, 'Operation not permitted')
        with patch.object(deploy.os, 'killpg', side_effect=killpg):
            deploy.Operations.terminate_group(process)
        self.assertEqual(calls, [signal.SIGTERM, signal.SIGKILL])
    def assert_descendants_stop(self, *, timeout, parent_exit):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'escaped-descendant.txt'
            started = root / 'descendant-started.txt'
            child = 'import sys,time; from pathlib import Path; time.sleep(.4); Path(sys.argv[1]).write_text("escaped")'
            parent = textwrap.dedent('''
                from pathlib import Path
                import subprocess,sys,time
                process = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                Path(sys.argv[3]).write_text(str(process.pid))
                if sys.argv[4] == 'failure':
                    raise SystemExit(7)
                time.sleep(5)
            ''')
            with self.assertRaises(deploy.DeployError):
                deploy.Operations().command([sys.executable, '-c', parent, child, str(marker), str(started), parent_exit],
                                            root, 'Fixture command', timeout=timeout)
            self.assertTrue(started.is_file(), 'The helper must spawn its descendant before the failure.')
            time.sleep(.6)
            self.assertFalse(marker.exists(), 'The helper descendant survived command cleanup.')

    def test_command_timeout_stops_descendants(self):
        self.assert_descendants_stop(timeout=.1, parent_exit='wait')

    def test_failed_command_stops_descendants(self):
        self.assert_descendants_stop(timeout=2, parent_exit='failure')


class DeploymentHealthTests(unittest.TestCase):
    def assert_health_uses_installed_trust(self, peer_certificate, *, delayed=False):
        saved_certificate = b'The saved certificate DER bytes.'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert = root / '.runtime/host/server.crt'
            cert.parent.mkdir(parents=True)
            cert.write_text(deploy.ssl.DER_cert_to_PEM_cert(saved_certificate))
            context = MagicMock()
            secured = context.wrap_socket.return_value
            secured.getpeercert.return_value = peer_certificate
            installed_host = Mock()
            installed_host.trusted_certificate_context.return_value = context
            raw = Mock()
            connection = MagicMock()
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"ok":true,"api_version":1}'
            with patch.object(deploy, 'load_host', return_value=installed_host) as load_host, \
                 patch.object(deploy.socket, 'create_connection', return_value=raw) as connect, \
                 patch.object(deploy.http.client, 'HTTPConnection', return_value=connection) as http, \
                 patch.object(deploy.time, 'monotonic', side_effect=[0, 21] if delayed else [0, 0]):
                if peer_certificate != saved_certificate:
                    with self.assertRaisesRegex(deploy.DeployError, 'certificate does not match'):
                        deploy.Operations().health(root, 'https://mini.example.ts.net:9000')
                    connection.request.assert_not_called()
                else:
                    result = deploy.Operations().health(root, 'https://mini.example.ts.net:9000')
                    self.assertEqual(result['certificate_sha256'], hashlib.sha256(saved_certificate).hexdigest().upper())
                    self.assertTrue(result['ok'])
                    connection.request.assert_called_once_with('GET', '/health')
                load_host.assert_called_once_with(root)
                installed_host.trusted_certificate_context.assert_called_once_with(cafile=str(cert))
                connect.assert_called_once_with(('127.0.0.1', 9000), timeout=2)
                http.assert_called_once_with('mini.example.ts.net', 9000, timeout=2)
                context.wrap_socket.assert_called_once_with(raw, server_hostname='mini.example.ts.net')
                secured.getpeercert.assert_called_once_with(binary_form=True)
                connection.close.assert_called_once()

    def test_health_uses_installed_certificate_policy_with_exact_sni_and_pin(self):
        self.assert_health_uses_installed_trust(b'The saved certificate DER bytes.')

    def test_health_rejects_a_different_certificate_before_the_http_request(self):
        self.assert_health_uses_installed_trust(b'A different certificate.')

    def test_the_recovery_probe_waits_less_than_a_real_start(self):
        waits = []
        with patch.object(deploy.Operations, '_health', side_effect=lambda root, address, seconds: waits.append(seconds)):
            deploy.Operations().probe(Path('/tmp'), 'https://localhost:8443')
            deploy.Operations().health(Path('/tmp'), 'https://localhost:8443')
        self.assertEqual(waits, [deploy.PROBE_WAIT_SECONDS, deploy.HEALTH_WAIT_SECONDS])
        self.assertLess(deploy.PROBE_WAIT_SECONDS, deploy.HEALTH_WAIT_SECONDS)

    def test_health_accepts_startup_after_twenty_seconds(self):
        self.assert_health_uses_installed_trust(b'The saved certificate DER bytes.', delayed=True)
