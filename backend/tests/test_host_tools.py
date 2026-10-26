import argparse
import fcntl
import importlib.util
import json
from pathlib import Path
import plistlib
import sys
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))  # cleanup imports host, as when it runs from scripts/.


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load('host')
cleanup = load('cleanup')


class HostToolsTests(unittest.TestCase):
    def test_restart_support_requires_the_current_launchd_process(self):
        with tempfile.TemporaryDirectory() as directory:
            plist = Path(directory) / 'agent.plist'
            plist.write_bytes(plistlib.dumps({'KeepAlive': True}))
            with patch.object(host, 'owned_agent', return_value=(plist, True)), \
                 patch.object(host.os, 'getpid', return_value=123), \
                 patch.object(host.subprocess, 'run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, '  pid = 456\n', '')
                self.assertFalse(host.managed_current_process())
                run.return_value = subprocess.CompletedProcess([], 0, '  pid = 123\n', '')
                self.assertTrue(host.managed_current_process())
                plist.write_bytes(plistlib.dumps({'KeepAlive': False}))
                self.assertFalse(host.managed_current_process())

    def test_custom_data_launch_agent_can_enable_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            custom = home / 'custom-data'
            plist = home / 'Library/LaunchAgents' / f'{host.LABEL}.plist'
            plist.parent.mkdir(parents=True)
            plist.write_bytes(plistlib.dumps({
                'Label': host.LABEL, 'KeepAlive': True, 'WorkingDirectory': str(host.ROOT / 'backend'),
                'ProgramArguments': [sys.executable, str(Path(host.__file__).resolve()), 'serve', '--data-dir', str(custom)],
            }))
            output = f'path = {plist}\nworking directory = {host.ROOT / "backend"}\npid = 123\n'
            with patch.object(host.Path, 'home', return_value=home), \
                 patch.object(host.os, 'getpid', return_value=123), \
                 patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')):
                self.assertTrue(host.managed_current_process(custom))
                self.assertFalse(host.managed_current_process(home / 'different-data'))

    def test_the_login_item_of_the_app_enables_restart_when_launchd_runs_this_process(self):
        with patch.dict(host.os.environ, {'COCONUT_LOGIN_ITEM': host.LOGIN_ITEM_LABEL}), \
             patch.object(host, 'owned_agent', side_effect=AssertionError('The login item needs no LaunchAgent file.')), \
             patch.object(host.os, 'getpid', return_value=123), \
             patch.object(host.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '  pid = 123\n', '')
            self.assertTrue(host.managed_current_process())
            self.assertEqual(run.call_args.args[0][:2], ['launchctl', 'print'])
            self.assertTrue(run.call_args.args[0][2].endswith('/com.gumroad.coconut.service'))
            # The variable alone does not count: launchd must run this process.
            run.return_value = subprocess.CompletedProcess([], 0, '  pid = 456\n', '')
            self.assertFalse(host.managed_current_process())
            run.return_value = subprocess.CompletedProcess([], 113, '', 'Could not find service')
            self.assertFalse(host.managed_current_process())

    def test_unavailable_restart_support_does_not_block_manual_start(self):
        with patch.object(host, 'owned_agent', side_effect=SystemExit('Another installation owns the job')):
            self.assertFalse(host.managed_current_process())

    def test_migrated_source_cannot_start_another_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "moved-to.json").write_text('{"ssh":"user@mini.local"}')
            with self.assertRaisesRegex(SystemExit, "Execution moved"):
                host.service_args(self.arguments(directory))

    def arguments(self, directory, **changes):
        return argparse.Namespace(**dict(dict(data_dir=Path(directory), port=8960, provider='fixture'), **changes))

    def test_the_service_listens_on_loopback_with_the_tunnel_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = host.service_args(self.arguments(directory, port=9000))
        self.assertEqual(arguments[1:], ['-m', 'coconut', '--data-dir', directory, '--port', '9000',
                                         '--provider', 'fixture', '--tunnel-port', '8961'])
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(SystemExit, 'port from 1'):
            host.service_args(self.arguments(directory, port=0))

    def serve(self, command):
        """Run host.py serve with these arguments, and return the command that it starts."""
        with patch.object(sys, 'argv', command), patch.object(host, 'load_credentials'), \
             patch.object(host, 'managed_current_process', return_value=False), \
             patch.object(host.os, 'chdir'), patch.object(host.os, 'execv') as execute:
            host.main()
        return execute.call_args.args[1]

    def test_the_launchagent_starts_the_loopback_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(Path, 'home', return_value=root), \
                 patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                host.install(self.arguments(directory, port=9000, command='install-service'))
            payload = plistlib.loads((root / 'Library/LaunchAgents/local.coconut.agent.plist').read_bytes())
            self.assertTrue(payload['KeepAlive'])
            self.assertEqual(payload['ProcessType'], 'Interactive')
            command = payload['ProgramArguments']
            self.assertEqual(command[2:], ['serve', '--data-dir', directory, '--port', '9000', '--provider', 'fixture'])
            server = self.serve(command[1:])
            self.assertEqual(server[server.index('--port') + 1], '9000')
            self.assertFalse((root / 'connection.json').exists())

    def test_an_old_launchagent_keeps_the_port_of_its_connection_file(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'connection.json').write_text('{"address": "https://mini.example.ts.net:9443"}')
            server = self.serve(['host.py', 'serve', '--data-dir', directory, '--host', '0.0.0.0', '--provider', 'fixture'])
            self.assertEqual(server[server.index('--port') + 1], '9443')
            server = self.serve(['host.py', 'serve', '--data-dir', directory, '--port', '9000', '--provider', 'fixture'])
            self.assertEqual(server[server.index('--port') + 1], '9000')

    def test_old_launchagent_flags_start_the_loopback_service(self):
        # LaunchAgents from before the loopback-only service gave a network address.
        with tempfile.TemporaryDirectory() as directory:
            for extra in (['--host', '0.0.0.0'], ['--host', '127.0.0.1', '--insecure-loopback']):
                with self.subTest(extra=extra):
                    server = self.serve(['host.py', 'serve', '--data-dir', directory, *extra, '--provider', 'fixture'])
                    self.assertEqual(server[1:], ['-m', 'coconut', '--data-dir', str(Path(directory).resolve()), '--port', '8960',
                                                  '--provider', 'fixture', '--tunnel-port', '8961'])

    def test_cleanup_cannot_follow_runtime_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            outside = base / 'unrelated'
            outside.mkdir()
            sentinel = outside / 'keep.txt'
            sentinel.write_text('Keep this file.')
            runtime = base / '.runtime'
            runtime.symlink_to(outside, target_is_directory=True)
            with patch.object(cleanup, 'RUNTIME', runtime), patch.object(sys, 'argv', ['cleanup', '--yes']):
                with self.assertRaisesRegex(SystemExit, 'symbolic link'):
                    cleanup.main()
            self.assertTrue(sentinel.exists())

    def test_cleanup_keeps_source_and_other_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / '.runtime'
            runtime.mkdir()
            (runtime / 'test-data').write_text('Delete this test fixture.')
            source = root / 'source.swift'
            source.write_text('Keep the source.')
            marker = root / '.coconut-install.json'
            marker.write_text('{"package_id":"previous-source"}')
            with patch.object(cleanup, 'ROOT', root), patch.object(cleanup, 'RUNTIME', runtime), \
                 patch.object(cleanup, 'stop_for_cleanup', return_value=root / 'absent.plist'), \
                 patch.object(sys, 'argv', ['cleanup', '--yes']), \
                 patch.object(Path, 'home', return_value=root):
                cleanup.main()
            self.assertFalse(runtime.exists())
            self.assertTrue(source.exists())
            self.assertTrue(marker.exists())

    def test_cleanup_keeps_every_file_when_managed_stop_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / '.runtime'
            runtime.mkdir()
            sentinel = runtime / 'history'
            sentinel.write_bytes(b'Keep the history.')
            plist = root / 'agent.plist'
            plist.write_bytes(b'Keep the service file.')
            with patch.object(cleanup, 'ROOT', root), patch.object(cleanup, 'RUNTIME', runtime), \
                 patch.object(cleanup, 'stop_for_cleanup', side_effect=SystemExit('launchctl could not unload Coconut.')), \
                 patch.object(sys, 'argv', ['cleanup', '--yes']):
                with self.assertRaisesRegex(SystemExit, 'could not unload'):
                    cleanup.main()
            self.assertEqual(sentinel.read_bytes(), b'Keep the history.')
            self.assertEqual(plist.read_bytes(), b'Keep the service file.')

    def test_cleanup_uses_ownership_check_before_deleting_agent(self):
        with patch.dict(sys.modules, {'host': host}), \
             patch.object(host, 'stop_for_update', side_effect=SystemExit('The LaunchAgent belongs to another installation.')), \
             patch.object(host, 'owned_agent') as agent:
            with self.assertRaisesRegex(SystemExit, 'another installation'):
                cleanup.stop_for_cleanup()
            agent.assert_not_called()

    def test_cleanup_rechecks_that_agent_is_unloaded(self):
        with patch.dict(sys.modules, {'host': host}), patch.object(host, 'stop_for_update'), \
             patch.object(host, 'owned_agent', return_value=(Path('/unused.plist'), True)):
            with self.assertRaisesRegex(SystemExit, 'restarted during cleanup'):
                cleanup.stop_for_cleanup()


class HostLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='coconut lifecycle ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.data = self.root / '.runtime/host'
        self.data.mkdir(parents=True)
        for target, name, value in ((host, 'ROOT', self.root), (host, 'DEFAULT_DATA', self.data),
                                    (Path, 'home', lambda: self.root)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.options = {'port': 9000, 'provider': 'fixture'}
        self.path = self.root / 'Library/LaunchAgents/local.coconut.agent.plist'

    def state(self, mode):
        state = {'mode': mode, 'project': str(self.root), 'data_dir': str(self.data)}
        if mode == 'manual':
            state['options'] = self.options.copy()
        return state

    def write_agent(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(plistlib.dumps({
            'Label': host.LABEL, 'WorkingDirectory': str(self.root / 'backend'),
            'ProgramArguments': [sys.executable, str(SCRIPTS / 'host.py'), 'serve', '--data-dir', str(self.data)],
        }))

    def write_process(self, **changes):
        record = {'pid': 4242, 'project': str(self.root), 'data_dir': str(self.data)}
        record.update(changes)
        (self.data / 'process.json').write_text(json.dumps(record))

    def test_process_check_handles_paths_with_spaces_and_preserves_options(self):
        self.write_process()
        command = f'/usr/bin/python3 -m coconut --data-dir {self.data}'
        tls = f' --cert {self.data}/server.crt --key {self.data}/server.key'
        # A process from before the loopback-only service has --host, and with TLS --cert and --key. A process from
        # before the tunnel listener has no --tunnel-port.
        for options in (' --port 9000 --provider fixture --tunnel-port 8961',
                        ' --host 127.0.0.1 --port 9000 --provider fixture',
                        ' --host 0.0.0.0 --port 9000 --provider fixture' + tls + ' --tunnel-port 8961'):
            with self.subTest(options=options), \
                    patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, command + options, '')):
                self.assertEqual(host.owned_process(), (4242, self.options))

    def test_foreign_process_record_fails_before_process_control(self):
        self.write_process(project=str(self.root / 'another-project'))
        with patch.object(host.subprocess, 'run') as run, patch.object(host.os, 'kill') as kill:
            with self.assertRaisesRegex(SystemExit, 'does not identify this Coconut'):
                host.stop_owned_process()
            run.assert_not_called()
            kill.assert_not_called()

    def test_reused_pid_with_different_command_cannot_be_stopped(self):
        self.write_process()
        command = f'/usr/bin/python3 -m coconut --data-dir {self.data}-other --host 127.0.0.1 --port 9000 --provider fixture'
        with patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, command, '')), \
             patch.object(host.os, 'kill') as kill:
            with self.assertRaisesRegex(SystemExit, 'does not match'):
                host.stop_owned_process()
            kill.assert_not_called()

    def test_loaded_agent_must_match_the_current_project(self):
        self.write_agent()
        printed = f'path = {self.path}\nworking directory = /another/project/backend\n'
        with patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, printed, '')):
            with self.assertRaisesRegex(SystemExit, 'loaded LaunchAgent does not identify'):
                host.owned_agent()

    def test_the_update_drains_a_service_that_asks_for_it_and_waits_for_the_idle_mark(self):
        self.write_process(drain=True)
        def sleep(seconds):
            # The service sees the request and says that no turn is left.
            self.assertTrue((self.data / 'drain-request').is_file())
            (self.data / 'drained').write_text('1')
        with patch.object(host, 'owned_process', return_value=(4242, self.options)), patch.object(host.time, 'sleep', side_effect=sleep):
            self.assertTrue(host.drain_for_update(timeout=30))
        # The request ends by itself, later than the wait, and no partial file is left behind.
        self.assertGreater(float((self.data / 'drain-request').read_text()), time.time() + 30)
        self.assertFalse((self.data / 'drain-request.tmp').exists())

    def test_the_drain_request_appears_whole_or_not_at_all(self):
        self.write_process(drain=True)
        seen = []
        real = host.os.replace
        def replace(source, target):
            # Before the swap, the request file that the service reads does not exist yet. The new content is complete.
            seen.append((Path(target).exists(), float(Path(source).read_text()) > time.time()))
            return real(source, target)
        ticks = iter(range(0, 1000))
        with patch.object(host, 'owned_process', return_value=(4242, self.options)), patch.object(host.time, 'sleep'), \
             patch.object(host.time, 'monotonic', side_effect=lambda: next(ticks)), patch.object(host.os, 'replace', side_effect=replace):
            host.drain_for_update(timeout=3)
        self.assertEqual(seen, [(False, True)])

    def test_the_update_stops_at_once_a_service_that_does_not_drain(self):
        self.write_process()
        with patch.object(host, 'owned_process', return_value=(4242, self.options)), patch.object(host.time, 'sleep') as sleep:
            self.assertFalse(host.drain_for_update(timeout=30))
        sleep.assert_not_called()
        self.assertFalse((self.data / 'drain-request').exists())

    def test_the_update_stops_a_service_that_never_reports_idle_after_the_wait(self):
        self.write_process(drain=True)
        ticks = iter(range(0, 1000))
        with patch.object(host, 'owned_process', return_value=(4242, self.options)), patch.object(host.time, 'sleep'), \
             patch.object(host.time, 'monotonic', side_effect=lambda: next(ticks)):
            self.assertFalse(host.drain_for_update(timeout=5))

    def test_the_update_does_not_drain_a_service_that_is_not_running(self):
        self.write_process(drain=True)
        with patch.object(host, 'owned_process', return_value=None):
            self.assertFalse(host.drain_for_update(timeout=30))
        self.assertFalse((self.data / 'drain-request').exists())

    def test_unloaded_agent_file_does_not_imply_managed_state(self):
        self.write_agent()
        missing = subprocess.CompletedProcess([], 113, '', f'Could not find service "{host.LABEL}"')
        with patch.object(host.subprocess, 'run', return_value=missing), \
             patch.object(host, 'owned_process', return_value=None), patch.object(host, 'stop_owned_process'):
            self.assertEqual(host.stop_for_update(), self.state('stopped'))
        self.assertTrue(self.path.exists())

    def test_unknown_launchctl_state_fails_without_stopping(self):
        result = subprocess.CompletedProcess([], 5, '', 'Permission denied')
        with patch.object(host.subprocess, 'run', return_value=result), patch.object(host, 'stop_owned_process') as stop:
            with self.assertRaisesRegex(SystemExit, 'could not read its LaunchAgent state'):
                host.stop_for_update()
            stop.assert_not_called()

    def test_managed_stop_unloads_before_stopping_the_owned_process(self):
        self.write_agent()
        events = []
        def command(arguments, **kwargs):
            events.append(arguments[1])
            return subprocess.CompletedProcess(arguments, 0, '', '')
        with patch.object(host, 'owned_agent', side_effect=[(self.path, True), (self.path, False)]), \
             patch.object(host, 'owned_process', return_value=(4242, self.options)), \
             patch.object(host.subprocess, 'run', side_effect=command), \
             patch.object(host, 'stop_owned_process', side_effect=lambda: events.append('stop')):
            self.assertEqual(host.stop_for_update(), self.state('managed'))
        self.assertEqual(events, ['bootout', 'stop'])
        self.assertTrue(self.path.exists())

    def test_managed_stop_waits_for_launchctl_to_remove_the_job(self):
        self.write_agent()
        with patch.object(host, 'owned_agent', side_effect=[(self.path, True), (self.path, True),
                                                          (self.path, True), (self.path, False)]), \
             patch.object(host, 'owned_process', return_value=(4242, self.options)), \
             patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
             patch.object(host.time, 'sleep') as sleep, patch.object(host, 'stop_owned_process') as stop:
            self.assertEqual(host.stop_for_update(), self.state('managed'))
            self.assertEqual(sleep.call_count, 2)
            stop.assert_called_once()
        self.assertTrue(self.path.exists())

    def test_managed_stop_timeout_preserves_files_and_reports_restore(self):
        self.write_agent()
        previous = self.path.read_bytes()
        with patch.object(host, 'owned_agent', return_value=(self.path, True)), \
             patch.object(host, 'owned_process', return_value=(4242, self.options)), \
             patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
             patch.object(host.time, 'monotonic', side_effect=[0, 11]), \
             patch.object(host, 'stop_owned_process') as stop:
            with self.assertRaisesRegex(SystemExit, 'did not unload within ten seconds') as raised:
                host.stop_for_update()
            stop.assert_not_called()
        self.assertIn('Restore its LaunchAgent with:', str(raised.exception))
        self.assertEqual(self.path.read_bytes(), previous)

    def test_the_update_does_not_drain_a_service_of_the_app_login_item(self):
        self.write_process(drain=True)
        with patch.object(host, 'login_item_loaded', return_value=True), \
             patch.object(host, 'owned_agent', return_value=(self.path, False)), \
             patch.object(host, 'owned_process', return_value=(4242, self.options)):
            self.assertFalse(host.drain_for_update(timeout=30))
        self.assertFalse((self.data / 'drain-request').exists())

    def test_the_update_drains_when_the_launch_agent_also_runs_the_service(self):
        # stop_for_update stops a service of the host.py LaunchAgent, so the drain must happen first.
        with patch.object(host, 'login_item_loaded', return_value=True), \
             patch.object(host, 'owned_agent', return_value=(self.path, True)):
            self.assertFalse(host.app_runs_service())
        with patch.object(host, 'login_item_loaded', return_value=True), \
             patch.object(host, 'owned_agent', side_effect=SystemExit('Another installation owns the job')):
            self.assertFalse(host.app_runs_service())

    def test_the_update_does_not_stop_a_service_of_the_app_login_item(self):
        loaded = subprocess.CompletedProcess([], 0, 'state = running\n', '')
        with patch.object(host, 'owned_agent', return_value=(self.path, False)), \
             patch.object(host, 'owned_process', return_value=(4242, self.options)), \
             patch.object(host.subprocess, 'run', return_value=loaded) as run, \
             patch.object(host, 'stop_owned_process') as stop:
            with self.assertRaisesRegex(SystemExit, 'Coconut.app runs this service'):
                host.stop_for_update()
            stop.assert_not_called()
        self.assertTrue(run.call_args.args[0][2].endswith('/com.gumroad.coconut.service'))

    def test_manual_stop_retains_launch_options(self):
        with patch.object(host, 'owned_agent', return_value=(self.path, False)), \
             patch.object(host, 'owned_process', return_value=(4242, self.options)), \
             patch.object(host, 'stop_owned_process') as stop:
            self.assertEqual(host.stop_for_update(), self.state('manual'))
            stop.assert_called_once()

    def test_failed_bootout_keeps_files_and_does_not_signal_process(self):
        self.write_agent()
        previous = self.path.read_bytes()
        with patch.object(host, 'owned_agent', return_value=(self.path, True)), \
             patch.object(host, 'owned_process', return_value=(4242, self.options)), \
             patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 5, '', 'Failed')), \
             patch.object(host, 'stop_owned_process') as stop:
            with self.assertRaisesRegex(SystemExit, 'could not unload'):
                host.stop_for_update()
            stop.assert_not_called()
        self.assertEqual(self.path.read_bytes(), previous)

    def test_partial_managed_stop_reports_quoted_restore_command(self):
        self.write_agent()
        with patch.object(host, 'owned_agent', side_effect=[(self.path, True), (self.path, False)]), \
             patch.object(host, 'owned_process', return_value=(4242, self.options)), \
             patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
             patch.object(host, 'stop_owned_process', side_effect=SystemExit('The process did not stop.')):
            with self.assertRaisesRegex(SystemExit, 'launchctl completed bootout') as raised:
                host.stop_for_update()
        command = host.shlex.join(['launchctl', 'bootstrap', f'gui/{host.os.getuid()}', str(self.path)])
        self.assertIn(command, str(raised.exception))
        self.assertTrue(self.path.exists())

    def test_restore_stopped_does_not_start_or_bootstrap(self):
        with patch.object(host, 'owned_agent') as agent, patch.object(host, 'start') as start:
            host.restore_after_update(self.state('stopped'))
            agent.assert_not_called()
            start.assert_not_called()

    def test_restore_manual_preserves_fixture_and_port_options(self):
        old = dict(self.options, host='0.0.0.0', insecure_loopback=False)
        # A state from before the loopback-only service also has host and insecure_loopback.
        for options in (self.options, old):
            with self.subTest(options=options), patch.object(host, 'owned_agent', return_value=(self.path, False)), \
                    patch.object(host, 'owned_process', return_value=None), patch.object(host, 'start') as start:
                host.restore_after_update(dict(self.state('manual'), options=options))
            self.assertEqual(vars(start.call_args.args[0]), dict(self.options, data_dir=self.data))
        with patch.object(host, 'owned_agent', return_value=(self.path, False)), \
                patch.object(host, 'owned_process', return_value=None), self.assertRaisesRegex(SystemExit, 'invalid'):
            host.restore_after_update(dict(self.state('manual'), options=dict(self.options, host='0.0.0.0')))

    def test_restore_removes_the_network_flags_of_an_old_agent(self):
        self.write_agent()
        original = plistlib.loads(self.path.read_bytes())
        command = original['ProgramArguments']
        original['ProgramArguments'] = [*command, '--host', '0.0.0.0', '--insecure-loopback', '--provider', 'hermes']
        self.path.write_bytes(plistlib.dumps(original))
        with patch.object(host, 'owned_agent', side_effect=[(self.path, False), (self.path, True)]), \
             patch.object(host, 'owned_process', return_value=None), \
             patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            host.restore_after_update(self.state('managed'))
        self.assertEqual(plistlib.loads(self.path.read_bytes())['ProgramArguments'], [*command, '--provider', 'hermes'])
        # The service of such an agent read its port from connection.json. The repair keeps that port.
        (self.data / 'connection.json').write_text('{"address": "https://mini.example.ts.net:9443"}')
        repaired = host.repaired_agent(original)['ProgramArguments']
        self.assertEqual(repaired, [*command[:3], '--port', '9443', *command[3:], '--provider', 'hermes'])

    def test_restore_managed_bootstraps_only_the_owned_agent(self):
        self.write_agent()
        with patch.object(host, 'owned_agent', side_effect=[(self.path, False), (self.path, True)]), \
             patch.object(host, 'owned_process', return_value=None), \
             patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            host.restore_after_update(self.state('managed'))
        self.assertEqual(run.call_args.args[0], ['launchctl', 'bootstrap', f'gui/{host.os.getuid()}', str(self.path)])

    def test_failed_bootstrap_keeps_agent_and_data(self):
        self.write_agent()
        sentinel = self.data / 'coconut.sqlite3'
        sentinel.write_bytes(b'Preserve host history.')
        with patch.object(host, 'owned_agent', return_value=(self.path, False)), \
             patch.object(host, 'owned_process', return_value=None), \
             patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 5, '', 'Failed')):
            with self.assertRaisesRegex(SystemExit, 'could not restore'):
                host.restore_after_update(self.state('managed'))
        self.assertTrue(self.path.exists())
        self.assertEqual(sentinel.read_bytes(), b'Preserve host history.')

    def test_restore_migrates_background_priority_without_changing_service_options(self):
        self.write_agent()
        original = plistlib.loads(self.path.read_bytes())
        original.update(ProcessType='Background', KeepAlive=True, EnvironmentVariables={'PATH': '/usr/bin'})
        self.path.write_bytes(plistlib.dumps(original))
        with patch.object(host, 'owned_agent', side_effect=[(self.path, False), (self.path, True)]), \
             patch.object(host, 'owned_process', return_value=None), \
             patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            host.restore_after_update(self.state('managed'))
        self.assertEqual(plistlib.loads(self.path.read_bytes()), dict(original, ProcessType='Interactive'))

    def test_restore_moves_the_agent_off_the_removed_hermes_runtime(self):
        self.write_agent()
        original = plistlib.loads(self.path.read_bytes())
        removed = str(self.root / '.runtime/hermes/venv/bin/python')
        original.update(ProgramArguments=[removed, *original['ProgramArguments'][1:], '--provider', 'hermes'],
                        ProcessType='Interactive', KeepAlive=True, EnvironmentVariables={'PATH': '/usr/bin', 'HERMES_HOME': '/h'})
        self.path.write_bytes(plistlib.dumps(original))
        loaded = []

        def run(command, **options):
            # launchctl loads the agent only after the repaired file is in place.
            loaded.append(plistlib.loads(self.path.read_bytes()))
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch.object(host, 'owned_agent', side_effect=[(self.path, False), (self.path, True)]), \
             patch.object(host, 'owned_process', return_value=None), \
             patch.object(host, 'service_python', return_value='/opt/homebrew/bin/python3'), \
             patch.object(host.subprocess, 'run', side_effect=run):
            host.restore_after_update(self.state('managed'))
        expected = dict(original, ProgramArguments=['/opt/homebrew/bin/python3', *original['ProgramArguments'][1:]])
        self.assertEqual(plistlib.loads(self.path.read_bytes()), expected)
        self.assertEqual(loaded, [expected])

    def test_restore_keeps_an_agent_that_has_no_usable_python(self):
        self.write_agent()
        payload = plistlib.loads(self.path.read_bytes())
        payload['ProgramArguments'][0] = str(self.root / '.runtime/hermes/venv/bin/python')
        self.path.write_bytes(plistlib.dumps(payload))
        before = self.path.read_bytes()
        with patch.object(host, 'owned_agent', return_value=(self.path, False)), \
             patch.object(host, 'owned_process', return_value=None), \
             patch.object(host, 'service_python', return_value=None), \
             patch.object(host.subprocess, 'run') as run:
            with self.assertRaisesRegex(SystemExit, 'removed private Hermes runtime'):
                host.restore_after_update(self.state('managed'))
        run.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_service_python_does_not_move_with_hermes_and_can_run_coconut(self):
        folder = self.root / 'bin'
        folder.mkdir()
        pythons = {}
        for name in ('runtime', 'broken', 'stable', 'later'):
            pythons[name] = (self.root / '.runtime/hermes/venv/bin/python') if name == 'runtime' else folder / name
            pythons[name].parent.mkdir(parents=True, exist_ok=True)
            pythons[name].write_text('#!/bin/sh\n')
            pythons[name].chmod(0o700)
        candidates = (str(folder / 'missing'), str(pythons['runtime']), str(pythons['broken']), str(pythons['stable']),
                      str(pythons['later']))

        def run(command, **options):
            self.assertEqual(command[1:], ['-c', 'import coconut.server'])
            self.assertEqual(options['cwd'], self.root / 'backend')
            return subprocess.CompletedProcess(command, 1 if command[0] == str(pythons['broken']) else 0)
        with patch.object(host, 'SERVICE_PYTHONS', candidates), patch.object(host.subprocess, 'run', side_effect=run) as checks:
            # A Python in a virtual environment, such as the one of a Hermes install, can move.
            with patch.object(host.sys, 'executable', str(pythons['later'])), patch.object(host.sys, 'prefix', '/venv'):
                self.assertEqual(host.service_python(), str(pythons['stable']))
            self.assertEqual([call.args[0][0] for call in checks.call_args_list],
                             [str(pythons['broken']), str(pythons['stable'])])
            # install-service uses the Python that runs it, so a repair prefers it too.
            with patch.object(host.sys, 'executable', str(pythons['later'])), \
                 patch.object(host.sys, 'prefix', host.sys.base_prefix):
                self.assertEqual(host.service_python(), str(pythons['later']))
            with patch.object(host.sys, 'executable', str(pythons['broken'])), \
                 patch.object(host.sys, 'prefix', host.sys.base_prefix):
                self.assertEqual(host.service_python(), str(pythons['stable']))

    def test_restore_rejects_state_from_another_project(self):
        state = self.state('managed')
        state['project'] = '/another/project'
        with patch.object(host, 'owned_agent') as agent:
            with self.assertRaisesRegex(SystemExit, 'does not belong'):
                host.restore_after_update(state)
            agent.assert_not_called()

    def test_pid_change_during_stop_does_not_signal_the_new_process(self):
        with patch.object(host, 'owned_process', side_effect=[(4242, self.options), (4343, self.options)]), \
             patch.object(host.os, 'kill') as kill:
            with self.assertRaisesRegex(SystemExit, 'Another Coconut process started'):
                host.stop_owned_process()
            kill.assert_called_once_with(4242, host.signal.SIGTERM)

    def test_restart_starts_an_initially_stopped_service(self):
        args = argparse.Namespace(data_dir=self.data, port=8960, provider='hermes')
        with patch.object(host, 'stop_for_update', return_value=self.state('stopped')), \
             patch.object(host, 'start') as start, patch.object(host, 'restore_after_update') as restore:
            host.restart(args)
            start.assert_called_once_with(args)
            restore.assert_not_called()

    def test_restart_preserves_managed_state(self):
        state = self.state('managed')
        with patch.object(host, 'stop_for_update', return_value=state), patch.object(host, 'start') as start, \
             patch.object(host, 'restore_after_update') as restore:
            host.restart(argparse.Namespace())
            restore.assert_called_once_with(state)
            start.assert_not_called()




class ServiceEnvironmentTests(unittest.TestCase):
    def test_an_update_searches_for_hermes_as_the_login_service_does(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            agents = home / 'Library/LaunchAgents'
            agents.mkdir(parents=True)
            (agents / f'{host.LABEL}.plist').write_bytes(plistlib.dumps({'EnvironmentVariables': {
                'PATH': '/service/bin:/usr/bin', 'COCONUT_HERMES_ROOT': '/hermes'}}))
            updater = {'PATH': '/updater/bin', 'COCONUT_HERMES_PYTHON': '/other/python', 'HOME': str(home)}
            with patch.object(host.Path, 'home', return_value=home), patch.dict(host.os.environ, updater, clear=True):
                environment = host.service_environment()
                expected = host.service_path('/service/bin:/usr/bin')
        # host.py serve puts the package folders first, as for the service. See terminal_setup.command_path.
        self.assertEqual(environment['PATH'], expected)
        self.assertIn('/service/bin', environment['PATH'].split(':'))
        self.assertEqual(environment['COCONUT_HERMES_ROOT'], '/hermes')
        self.assertNotIn('COCONUT_HERMES_PYTHON', environment)
        self.assertEqual(environment['HOME'], str(home))

    def test_without_a_login_service_the_update_uses_its_own_environment(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(host.Path, 'home', return_value=Path(directory)), \
                patch.dict(host.os.environ, {'PATH': '/updater/bin'}, clear=True):
            self.assertEqual(host.service_environment(), {'PATH': host.service_path('/updater/bin')})


class ManualStartTests(unittest.TestCase):
    def test_start_checks_hermes_before_it_waits_for_the_service(self):
        status = {'status': 'missing', 'version': None, 'reason': 'Coconut found no Hermes on this Mac.'}
        runtime = unittest.mock.Mock()
        runtime.status.return_value = status
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            args = argparse.Namespace(data_dir=data, port=8960, provider='hermes')
            process = unittest.mock.Mock(pid=4321)
            process.poll.return_value = None

            def popen(command, **kwargs):
                (data / 'process.json').write_text(json.dumps({'pid': 4321}))
                return process
            with patch.object(host, 'hermes_runtime', return_value=runtime), \
                    patch.object(host.subprocess, 'Popen', side_effect=popen) as started, \
                    patch('sys.stdout'):
                host.start(args)
        # The search uses the PATH that host.py serve gives the service, for example with /opt/homebrew/bin.
        environment = runtime.status.call_args.kwargs['environ']
        runtime.status.assert_called_once_with(data, environ=environment)
        self.assertEqual(environment['PATH'], host.service_path(host.os.environ.get('PATH', '')))
        self.assertEqual(json.loads(started.call_args.kwargs['env']['COCONUT_HERMES_STATUS']), status)


if __name__ == '__main__':
    unittest.main()
