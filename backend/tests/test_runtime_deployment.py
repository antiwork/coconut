"""Keep runtime updates and rollback inside the managed deployment transaction."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from test_deploy_tools import deploy, archive, package_files, FakeOperations

OLD = '0123456789abcdef0123456789abcdef01234567'
NEW = 'a' * 40


class RuntimeOperations(FakeOperations):
    def prepare(self, root, address, package):
        super().prepare(root, address, package)
        runtime = root / '.runtime/hermes'
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / 'install.json').write_text(json.dumps({'commit': package.revision}))
        (runtime / 'environment').write_text(str(runtime))


class RuntimeDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / 'Coconut'
        self.address = 'https://mini.example:8960'
        self.receive(package_files())
        runtime = self.root / '.runtime/hermes'
        (runtime / 'terminal').mkdir()
        (runtime / 'terminal/session.json').write_text('Native session')
        (runtime / 'terminal.lock').touch()
        self.lock_inode = (runtime / 'terminal.lock').stat().st_ino
        (self.root / '.runtime/host').mkdir()
        (self.root / '.runtime/host/coconut.sqlite3').write_bytes(b'Unchanged chat data')
        self.files = package_files(**{'scripts/hermes.py': f"REVISION = '{NEW}'\n".encode()})

    def receive(self, files, operations=None, **kwargs):
        return deploy.receive('Coconut', self.address, archive(files), home=self.home,
                              operations=operations or RuntimeOperations(mode='managed'), **kwargs)

    def assert_old_runtime(self):
        runtime = self.root / '.runtime/hermes'
        self.assertEqual(json.loads((runtime / 'install.json').read_text())['commit'], OLD)
        self.assertEqual((runtime / 'terminal/session.json').read_text(), 'Native session')
        self.assertEqual((runtime / 'terminal.lock').stat().st_ino, self.lock_inode)
        self.assertEqual((self.root / '.runtime/host/coconut.sqlite3').read_bytes(), b'Unchanged chat data')
        self.assertEqual((self.root / 'scripts/hermes.py').read_bytes(), package_files()['scripts/hermes.py'])

    def test_source_update_does_not_change_the_runtime_without_explicit_option(self):
        with self.assertRaisesRegex(deploy.DeployError, '--update-runtime'):
            self.receive(self.files)
        self.assert_old_runtime()

    def test_upgrade_installs_at_final_paths_and_retains_native_state_and_rollback(self):
        result = self.receive(self.files, update_runtime=True)
        runtime = self.root / '.runtime/hermes'
        self.assertTrue(result['updated'])
        self.assertEqual(json.loads((runtime / 'install.json').read_text())['commit'], NEW)
        self.assertEqual((runtime / 'environment').read_text(), str(runtime))
        self.assertEqual((runtime / 'terminal/session.json').read_text(), 'Native session')
        self.assertEqual((runtime / 'terminal.lock').stat().st_ino, self.lock_inode)
        saved = list((self.root / '.runtime').glob('hermes-rollback-*'))
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads((saved[0] / 'install.json').read_text())['commit'], OLD)
        self.assertFalse((saved[0] / 'terminal').exists())
        self.assertFalse((self.root / '.runtime/deploy/update.json').exists())
        self.assertEqual((self.root / '.runtime/host/coconut.sqlite3').read_bytes(), b'Unchanged chat data')

    def test_an_upgrade_removes_the_rollback_copies_of_earlier_updates(self):
        earlier = self.root / '.runtime/hermes-rollback-earlier-update'
        earlier.mkdir()
        (earlier / 'install.json').write_text(json.dumps({'commit': 'b' * 40}))
        manual = self.root / '.runtime/hermes-before-manual-change'
        manual.mkdir()
        self.receive(self.files, update_runtime=True)
        saved = list((self.root / '.runtime').glob('hermes-rollback-*'))
        # Only the copy of this update can serve a rollback. A manual copy stays.
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads((saved[0] / 'install.json').read_text())['commit'], OLD)
        self.assertFalse(earlier.exists())
        self.assertTrue(manual.exists())

    def test_install_failure_restores_source_runtime_and_native_state(self):
        with self.assertRaises(deploy.DeployError):
            self.receive(self.files, RuntimeOperations(mode='managed', fail='prepare'), update_runtime=True)
        self.assert_old_runtime()
        self.assertFalse((self.root / '.runtime/deploy/update.json').exists())

    def test_health_failure_restores_the_previous_runtime(self):
        class FailsNewHealth(RuntimeOperations):
            def health(self, root, address):
                if json.loads((root / '.runtime/hermes/install.json').read_text())['commit'] == NEW:
                    raise deploy.DeployError('Candidate health failed')
                return super().health(root, address)
        with self.assertRaises(deploy.DeployError):
            self.receive(self.files, FailsNewHealth(mode='managed'), update_runtime=True)
        self.assert_old_runtime()
        self.assertFalse((self.root / '.runtime/deploy/update.json').exists())

    def test_interruption_after_saving_runtime_recovers_from_the_journal(self):
        original = deploy.move_terminal_state
        def interrupt(source, target, root):
            if source.name.startswith('hermes-rollback-'):
                raise KeyboardInterrupt()
            return original(source, target, root)
        with patch.object(deploy, 'move_terminal_state', side_effect=interrupt), self.assertRaises(deploy.DeployError):
            self.receive(self.files, update_runtime=True)
        self.assert_old_runtime()

    def test_failed_rollback_can_resume_without_losing_terminal_state(self):
        operations = RuntimeOperations(mode='managed', fail='prepare', fail_stop_after=1)
        with self.assertRaises(deploy.DeployError):
            self.receive(self.files, operations, update_runtime=True)
        self.assertTrue((self.root / '.runtime/deploy/update.json').is_file())
        deploy.reconcile(self.root, RuntimeOperations(mode='managed'))
        self.assert_old_runtime()
        self.assertFalse((self.root / '.runtime/deploy/update.json').exists())

    def test_recover_replays_a_pending_journal_and_installs_no_package(self):
        self.assertFalse(deploy.recover('Coconut', home=self.home, operations=RuntimeOperations(mode='managed')))
        with self.assertRaises(deploy.DeployError):
            self.receive(self.files, RuntimeOperations(mode='managed', fail='prepare', fail_stop_after=1), update_runtime=True)
        operations = RuntimeOperations(mode='managed')
        self.assertTrue(deploy.recover('Coconut', home=self.home, operations=operations))
        # The automatic updater runs this before the Hermes candidate checks. The new pin stays uninstalled.
        self.assert_old_runtime()
        self.assertFalse((self.root / '.runtime/deploy/update.json').exists())
        self.assertNotIn('prepare', [call[0] for call in operations.calls])

    def test_runtime_backup_symlink_does_not_redirect_rollback(self):
        with self.assertRaises(deploy.DeployError):
            self.receive(self.files, RuntimeOperations(mode='managed', fail='prepare', fail_stop_after=1), update_runtime=True)
        journal = json.loads((self.root / '.runtime/deploy/update.json').read_text())
        runtime, backup, _ = deploy.runtime_paths(self.root, journal)
        saved = backup.with_suffix('.saved')
        backup.rename(saved)
        backup.symlink_to(saved, target_is_directory=True)
        with self.assertRaises(deploy.DeployError):
            deploy.reconcile(self.root, RuntimeOperations(mode='managed'))
        self.assertTrue(saved.exists())
        self.assertTrue(runtime.exists())

    def test_installed_runtime_python_is_rejected_before_stopping(self):
        operations = RuntimeOperations(mode='managed')
        with patch.object(deploy.sys, 'executable', str(self.root / '.runtime/hermes/venv/bin/python')):
            with self.assertRaisesRegex(deploy.DeployError, 'Python outside'):
                self.receive(self.files, operations, update_runtime=True)
        self.assertNotIn('stop', [call[0] for call in operations.calls])
        self.assert_old_runtime()

    def test_legacy_terminal_lease_blocks_upgrade_before_stopping(self):
        import fcntl
        operations = RuntimeOperations(mode='managed')
        with (self.root / '.runtime/hermes/terminal.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self.assertRaisesRegex(deploy.DeployError, 'Terminal session'):
                self.receive(self.files, operations, update_runtime=True)
        self.assertNotIn('stop', [call[0] for call in operations.calls])
        self.assert_old_runtime()

    def test_terminal_cannot_create_a_second_lock_during_runtime_replacement(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
        import terminal
        runtime = self.root / '.runtime/hermes'
        saved = self.root / '.runtime/retained-hermes'
        with patch.object(terminal, 'ROOT', self.root), deploy.runtime_update_lease(self.root):
            runtime.rename(saved)
            runtime.mkdir()
            with self.assertRaises(SystemExit):
                with terminal.runtime_lease():
                    self.fail('A Terminal launch must wait for the runtime transaction.')
            self.assertFalse((runtime / 'terminal.lock').exists())
            runtime.rmdir()
            saved.rename(runtime)
        self.assert_old_runtime()

    def test_next_receive_recovers_after_terminal_state_moved(self):
        with self.assertRaises(deploy.DeployError):
            self.receive(self.files, RuntimeOperations(mode='managed', fail='prepare', fail_stop_after=1), update_runtime=True)
        self.receive(package_files())
        self.assert_old_runtime()
        self.assertFalse((self.root / '.runtime/deploy/update.json').exists())

    def test_next_receive_recovers_before_terminal_state_moved(self):
        with patch.object(deploy, 'move_terminal_state', side_effect=KeyboardInterrupt()), self.assertRaises(deploy.DeployError):
            self.receive(self.files, RuntimeOperations(mode='managed', fail_stop_after=1), update_runtime=True)
        self.receive(package_files())
        self.assert_old_runtime()
        self.assertFalse((self.root / '.runtime/deploy/update.json').exists())


if __name__ == '__main__':
    unittest.main()
