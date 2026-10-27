"""The login service restarts by itself for a new Hermes, after the running turns finish."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from coconut import hermes_runtime
from coconut.hermes_setup import RESTARTING
from coconut.hermes_unavailable import UnavailableService
from coconut.providers import FixtureProvider
from coconut.server import APIServer
from coconut.service import Service
from coconut.service_restart import DRAIN_REQUEST_FILE


class IdentityTests(unittest.TestCase):
    """The identity reads files only. It changes with each part that `hermes update` changes."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        hermes_home = self.home / '.hermes'
        self.environ = {'PATH': '', 'HOME': str(self.home), 'HERMES_HOME': str(hermes_home)}
        self.source = hermes_home / 'hermes-agent'
        python = self.source / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('')
        python.chmod(0o755)
        (self.source / '.git/refs/heads').mkdir(parents=True)
        (self.source / '.git/HEAD').write_text('ref: refs/heads/main\n')
        (self.source / '.git/refs/heads/main').write_text('a' * 40 + '\n')
        key = hashlib.sha256(str(self.source).encode()).hexdigest()[:16]
        self.facts = hermes_home / 'installs' / key / 'facts.json'
        self.facts.parent.mkdir(parents=True)
        self.facts.write_text(json.dumps({'packages': {'venv': {'environment': 'one'}}}))
        self.packages = self.home / 'site-packages'
        self.packages.mkdir()
        self.status = {'status': 'ok', 'packages': str(self.packages)}

    def identity(self):
        return hermes_runtime.identity(self.status, self.environ, self.home)

    def test_the_identity_stays_the_same_while_nothing_changes(self):
        self.assertEqual(self.identity(), self.identity())

    def test_each_part_of_an_update_changes_the_identity(self):
        def new_environment():
            self.facts.write_text(json.dumps({'packages': {'venv': {'environment': 'two, a new one'}}}))

        def new_commit():
            (self.source / '.git/refs/heads/main').write_text('b' * 40 + '\n')

        def new_package():
            (self.packages / 'hermes_agent-0.23.0.dist-info').mkdir()

        def moved_source():
            self.source.rename(self.source.with_name('moved'))

        for change in (new_environment, new_commit, new_package, moved_source):
            with self.subTest(change=change.__name__):
                before = self.identity()
                change()
                self.assertNotEqual(self.identity(), before)

    def test_an_update_of_the_running_hermes_counts_when_the_search_finds_another_first(self):
        # The first Hermes on PATH failed the check, so the service runs the one in ~/.hermes/hermes-agent.
        other = self.home / 'other/venv/bin/python'
        other.parent.mkdir(parents=True)
        other.write_text('')
        other.chmod(0o755)
        command = self.home / 'bin/hermes'
        command.parent.mkdir()
        command.write_text(f'#!{other}\n')
        command.chmod(0o755)
        self.environ['PATH'] = str(command.parent)
        self.status.update(python=str(self.source / '.venv/bin/python'), root=str(self.source))
        self.assertEqual(hermes_runtime.locate(self.environ, self.home)[0], other)
        before = self.identity()
        (self.source / '.git/refs/heads/main').write_text('b' * 40 + '\n')
        self.assertNotEqual(self.identity(), before)

    def test_the_check_starts_no_program(self):
        with patch('subprocess.run') as run, patch('subprocess.Popen') as popen:
            self.identity()
        run.assert_not_called()
        popen.assert_not_called()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = Service(self.temporary.name, FixtureProvider(self.temporary.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.service.restart_enabled = True
        self.identity = 'first'
        self.calls = 0
        def identity(status, environ=None, home=None):
            self.calls += 1
            return self.identity
        patcher = patch.object(hermes_runtime, 'identity', identity)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service.watch_hermes({'status': 'ok'})
        self.worker = Mock()
        self.worker.is_alive.return_value = True
        self.addCleanup(self.service.running.clear)

    def check_now(self):
        self.service._hermes_watch[2] = 0

    def change(self):
        self.identity = 'second'
        self.check_now()
        self.service.poll_hermes_change()

    def test_no_restart_while_hermes_stays_the_same(self):
        self.check_now()
        self.service.poll_hermes_change()
        self.assertFalse(self.service.restart_pending)
        self.assertIsNone(self.service.hermes_restart_deadline)
        self.assertFalse(self.service.take_service_restart())
        self.assertNotIn('restarting', self.service.hermes_setup()['hermes'])

    def test_the_check_runs_at_most_once_a_minute(self):
        calls = self.calls
        self.identity = 'second'
        self.service.poll_hermes_change()
        self.assertEqual(self.calls, calls)
        self.assertFalse(self.service.restart_pending)

    def test_a_new_hermes_drains_then_exits_once(self):
        self.service.running['turn'] = self.worker
        self.change()
        # No new turn starts, and the running turn finishes first.
        self.assertTrue(self.service.restart_pending)
        self.assertFalse(self.service.take_service_restart())
        self.assertEqual(self.service.hermes_setup()['hermes']['reason'], RESTARTING)
        self.assertIs(self.service.hermes_setup()['hermes']['restarting'], True)
        self.worker.is_alive.return_value = False
        with self.assertRaises(KeyboardInterrupt):
            APIServer.service_actions(Mock(service=self.service))
        self.assertTrue(self.service.stopping.is_set())
        self.assertFalse(self.service.take_service_restart())

    def test_the_drain_has_the_bound_of_an_update(self):
        self.service.running['turn'] = self.worker
        self.change()
        self.assertFalse(self.service.take_service_restart())
        self.service.hermes_restart_deadline = time.monotonic() - 1
        self.assertTrue(self.service.take_service_restart())

    def test_the_end_of_an_update_drain_keeps_the_pause(self):
        self.service.running['turn'] = self.worker
        self.change()
        (self.service.root / DRAIN_REQUEST_FILE).write_text(str(time.time() + 60))
        self.service.poll_service_drain()
        # An update drains the service: it stops the service, so no restart of its own.
        self.worker.is_alive.return_value = False
        self.assertFalse(self.service.take_service_restart())
        (self.service.root / DRAIN_REQUEST_FILE).write_text(str(time.time() - 1))
        self.service.poll_service_drain()
        self.service.poll_hermes_change()
        self.assertTrue(self.service.restart_pending)
        self.assertTrue(self.service.take_service_restart())

    def test_the_dispatcher_checks_for_a_new_hermes(self):
        with patch.object(self.service, 'poll_hermes_change') as poll:
            self.service._dispatch_pending()
        poll.assert_called_once_with()

    def test_a_service_that_launchd_does_not_manage_does_not_watch(self):
        service = Service(Path(self.temporary.name) / 'other', FixtureProvider(self.temporary.name), start_workers=False)
        self.addCleanup(service.close)
        self.identity = 'second'
        self.assertFalse(service.hermes_changed())


class RefuseModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.status = {'status': 'incompatible', 'version': '0.23.0',
                       'reason': 'This Coconut version does not work with the installed Hermes: ImportError.'}
        self.service = UnavailableService(Path(self.temporary.name) / 'data', self.status)
        self.addCleanup(self.service.close)

    def test_a_new_hermes_that_fails_the_check_causes_no_second_restart(self):
        home = Path(self.temporary.name)
        environ = {'PATH': '', 'HOME': str(home), 'HERMES_HOME': str(home / '.hermes')}
        with patch.dict(os.environ, environ, clear=True), patch.object(Path, 'home', return_value=home):
            # The process that started after the change watches from the Hermes that failed.
            self.service.watch_hermes(self.status)
            self.service._hermes_watch[2] = 0
            self.assertFalse(self.service.take_service_restart())
            self.assertEqual(self.service.hermes_setup()['hermes'], dict(self.status, tested=None))
            # A repaired Hermes takes effect at once: nothing runs in refuse mode.
            python = home / '.hermes/hermes-agent/.venv/bin/python'
            python.parent.mkdir(parents=True)
            python.write_text('')
            python.chmod(0o755)
            self.service._hermes_watch[2] = 0
            self.assertTrue(self.service.take_service_restart())


if __name__ == '__main__':
    unittest.main()
