import argparse
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import sys
import subprocess
import tempfile
import threading
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
        # This Mac can run the login item of Coconut.app, which every lifecycle check would then prefer.
        login_item = patch.object(host, 'login_item_loaded', return_value=False)
        login_item.start()
        self.addCleanup(login_item.stop)
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
        command = f'Ss   /usr/bin/python3 -m coconut --data-dir {self.data} --port 9000 --provider fixture --tunnel-port 8961'
        with patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, command, '')):
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
        command = f'S    /usr/bin/python3 -m coconut --data-dir {self.data}-other --port 9000 --provider fixture --tunnel-port 8961'
        with patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, command, '')), \
             patch.object(host.os, 'kill') as kill:
            with self.assertRaisesRegex(SystemExit, 'does not match'):
                host.stop_owned_process()
            kill.assert_not_called()

    def test_a_service_that_exits_after_the_signal_is_stopped_and_not_foreign(self):
        # A service of the login item: launchd is its parent. ps shows it as "(Python)" while it exits, and as
        # "<defunct>" until launchd collects it. Then the PID is gone.
        self.write_process()
        command = f'Ss   /Users/me/.hermes/hermes-agent/venv/bin/python -m coconut --data-dir {self.data} --port 8960 --provider hermes --tunnel-port 8961'
        outputs = iter([command, command, '?Es  (Python)', '?Es  (Python)', 'Z    <defunct>', 'Z    <defunct>'])
        calls = []
        def run(arguments, **kwargs):
            calls.append(arguments)
            output = next(outputs)
            return subprocess.CompletedProcess(arguments, 1 if output is None else 0, output or '', '')
        with patch.object(host.subprocess, 'run', side_effect=run), patch.object(host.os, 'kill') as kill, \
             patch.object(host.time, 'sleep'), patch.object(host, 'process_table', return_value={}):
            host.stop_owned_process()
        kill.assert_called_once_with(4242, host.signal.SIGTERM)
        self.assertEqual(calls[0], ['ps', '-ww', '-p', '4242', '-o', 'stat=,command='])
        # The stop waits for the zombie, which no longer holds service.lock. It does not need the PID to vanish.
        self.assertEqual(len(calls), 6)

    LINUX_START = 'Fri Oct 30 03:42:42 2026'

    def linux_stop(self, outputs, start):
        """Stop a service whose ps outputs follow `outputs`. lstart prints `start` for its process ID."""
        command = f'Sl   /usr/bin/python3 -m coconut --data-dir {self.data} --port 8960 --provider hermes --tunnel-port 8961'
        outputs = iter([command, *outputs])
        calls = []
        def run(arguments, **kwargs):
            calls.append(arguments)
            if arguments[-1] == 'lstart=':
                return subprocess.CompletedProcess(arguments, 0, f'{start}\n', '')
            output = next(outputs)
            return subprocess.CompletedProcess(arguments, 1 if output is None else 0, output or '', '')
        table = {4242: (1, 4242, 'Sl', self.LINUX_START, command.split(None, 1)[1])}
        return calls, run, table

    def test_a_linux_service_that_exits_after_the_signal_is_stopped_and_not_foreign(self):
        # On Linux, a process that exits releases its memory before it becomes a zombie. In between, ps shows a live
        # state and the name in brackets, not the launch command. The CI runner saw this under load.
        self.write_process()
        calls, run, table = self.linux_stop(['R    [python3]', 'D    [python3]', 'Z    [python3] <defunct>',
                                             'Z    [python3] <defunct>'], self.LINUX_START)
        with patch.object(host.subprocess, 'run', side_effect=run), patch.object(host.os, 'kill') as kill, \
             patch.object(host.time, 'sleep'), patch.object(host, 'process_table', return_value=table):
            host.stop_owned_process()
        kill.assert_called_once_with(4242, host.signal.SIGTERM)
        self.assertEqual(sum(call[-1] == 'stat=,command=' for call in calls), 5)

    def test_a_reused_pid_with_a_bracketed_name_is_not_killed(self):
        # The service exits, and another process gets its PID. Its name in brackets, such as a kernel worker, is not proof
        # that it is the service that exits: its start time differs. The stop refuses it and sends no SIGKILL.
        self.write_process()
        calls, run, table = self.linux_stop(['S    [kworker/0:1]'], 'Fri Oct 30 03:50:00 2026')
        with patch.object(host.subprocess, 'run', side_effect=run), patch.object(host.os, 'kill') as kill, \
             patch.object(host.time, 'sleep'), patch.object(host, 'process_table', return_value=table):
            with self.assertRaisesRegex(SystemExit, 'does not match'):
                host.stop_owned_process()
        kill.assert_called_once_with(4242, host.signal.SIGTERM)

    def test_a_service_that_is_gone_before_its_start_time_is_read_has_ended(self):
        # ps reads the bracketed name, then the service ends before ps reads its start time.
        self.write_process()
        calls, run, table = self.linux_stop(['R    [python3]', None, None], '')
        def gone(arguments, **kwargs):
            if arguments[-1] == 'lstart=':
                return subprocess.CompletedProcess(arguments, 1, '', '')
            return run(arguments, **kwargs)
        with patch.object(host.subprocess, 'run', side_effect=gone), patch.object(host.os, 'kill') as kill, \
             patch.object(host.time, 'sleep'), patch.object(host, 'process_table', return_value=table):
            host.stop_owned_process()
        kill.assert_called_once_with(4242, host.signal.SIGTERM)

    def test_a_bracketed_name_needs_a_checked_service(self):
        self.write_process()
        lstart = subprocess.CompletedProcess([], 0, self.LINUX_START + '\n', '')
        for output in ('R    [python3]', 'S    /usr/bin/python3 [python3]'):
            listed = subprocess.CompletedProcess([], 0, output, '')
            with self.subTest(output=output), patch.object(host.subprocess, 'run',
                                                           side_effect=lambda a, **k: lstart if a[-1] == 'lstart=' else listed):
                # Without a checked service, a bracketed name is a foreign process.
                with self.assertRaisesRegex(SystemExit, 'does not match'):
                    host.owned_process()
                # Another PID or another start time is a foreign process too.
                for verified in ((4343, self.LINUX_START), (4242, 'Fri Oct 30 03:50:00 2026')):
                    with self.assertRaisesRegex(SystemExit, 'does not match'):
                        host.owned_process(verified)
        with patch.object(host.subprocess, 'run',
                          side_effect=lambda a, **k: lstart if a[-1] == 'lstart=' else subprocess.CompletedProcess([], 0, 'R    [python3]', '')):
            self.assertIsNone(host.owned_process((4242, self.LINUX_START)))

    def test_an_exiting_service_is_not_running_and_the_stop_waits_for_its_end(self):
        self.write_process()
        outputs = iter(['?Es  (Python)', '?Es  (Python)', '?Es  (Python)', None, None])
        def run(arguments, **kwargs):
            output = next(outputs)
            return subprocess.CompletedProcess(arguments, 1 if output is None else 0, output or '', '')
        with patch.object(host.subprocess, 'run', side_effect=run), patch.object(host.os, 'kill') as kill, \
             patch.object(host.time, 'sleep') as sleep, patch.object(host, 'process_table', return_value={}):
            host.stop_owned_process()
        kill.assert_not_called()
        sleep.assert_called_once()

    def test_a_live_process_with_another_command_still_does_not_match(self):
        self.write_process()
        result = subprocess.CompletedProcess([], 0, 'Ss   /usr/bin/python3 -c pass', '')
        with patch.object(host.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(SystemExit, 'does not match'):
                host.owned_process()

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

    def app_service(self):
        """Patch launchd so that the login item of Coconut.app runs the service, without a host.py LaunchAgent."""
        return patch.object(host, 'login_item_loaded', return_value=True), \
            patch.object(host, 'owned_agent', return_value=(self.path, False))

    def test_the_app_mode_needs_the_login_item_and_no_launch_agent(self):
        with patch.object(host, 'login_item_loaded', return_value=True), \
             patch.object(host, 'owned_agent', return_value=(self.path, True)):
            self.assertFalse(host.app_runs_service())
        with patch.object(host, 'login_item_loaded', return_value=True), \
             patch.object(host, 'owned_agent', side_effect=SystemExit('Another installation owns the job')):
            self.assertFalse(host.app_runs_service())
        with patch.object(host, 'login_item_loaded', return_value=False), \
             patch.object(host, 'owned_agent', return_value=(self.path, False)):
            self.assertFalse(host.app_runs_service())
        loaded, agent = self.app_service()
        with loaded, agent:
            self.assertTrue(host.app_runs_service())
            self.assertEqual(host.inspect_for_update(), self.state('app'))

    def test_the_update_drains_a_service_of_the_app_login_item(self):
        self.write_process(drain=True)
        loaded, agent = self.app_service()
        with loaded, agent, patch.object(host, 'owned_process', return_value=(4242, self.options)), \
             patch.object(host.time, 'monotonic', side_effect=[0, 1, 700]), patch.object(host.time, 'sleep'):
            host.drain_for_update(timeout=600)
        self.assertTrue((self.data / 'drain-request').exists())

    def test_an_app_update_holds_the_login_item_while_it_stops_and_releases_it_at_restore(self):
        events = []
        def stop():
            events.append(('stop', (self.data / host.UPDATE_HOLD).exists()))
        loaded, agent = self.app_service()
        with loaded, agent, patch.object(host, 'stop_owned_process', side_effect=stop), \
             patch.object(host.time, 'time', return_value=1000.0):
            self.assertEqual(host.stop_for_update(), self.state('app'))
        # The hold exists before the stop, so the tool that launchd starts again waits for the new source.
        self.assertEqual(events, [('stop', True)])
        self.assertEqual(float((self.data / host.UPDATE_HOLD).read_text()), 1000.0 + host.UPDATE_HOLD_SECONDS)
        host.restore_after_update(self.state('app'))
        self.assertFalse((self.data / host.UPDATE_HOLD).exists())

    def test_a_failed_app_stop_releases_the_hold(self):
        loaded, agent = self.app_service()
        with loaded, agent, patch.object(host, 'stop_owned_process', side_effect=SystemExit('Coconut did not stop.')):
            with self.assertRaisesRegex(SystemExit, 'did not stop'):
                host.stop_for_update()
        self.assertFalse((self.data / host.UPDATE_HOLD).exists())

    def test_the_move_to_the_app_stops_the_service_and_keeps_the_earlier_launch_agents(self):
        self.write_agent()
        tunnel = self.path.with_name('com.gumroad.coconut.tunnel.plist')
        tunnel.write_bytes(plistlib.dumps({'Label': 'com.gumroad.coconut.tunnel'}))
        events = []
        with patch.object(host, 'drain_for_update', side_effect=lambda: events.append('drain')), \
             patch.object(host, 'stop_for_update', side_effect=lambda: events.append('stop')), \
             patch.object(host.subprocess, 'run', side_effect=lambda command, **_: events.append(command[1])):
            backup = host.migrate_to_app()
        self.assertEqual(events, ['drain', 'stop', 'bootout'])
        self.assertEqual(backup, self.root / '.runtime/legacy-agents')
        self.assertFalse(self.path.exists())
        self.assertFalse(tunnel.exists())
        self.assertEqual(sorted(path.name for path in backup.iterdir()),
                         ['com.gumroad.coconut.tunnel.plist', 'local.coconut.agent.plist'])

    def test_a_failed_stop_keeps_the_earlier_launch_agents(self):
        self.write_agent()
        with patch.object(host, 'drain_for_update'), \
             patch.object(host, 'stop_for_update', side_effect=SystemExit('launchctl could not unload Coconut.')):
            with self.assertRaises(SystemExit):
                host.migrate_to_app()
        self.assertTrue(self.path.exists())

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
        with patch.object(host, 'owned_agent', return_value=(self.path, False)), \
                patch.object(host, 'owned_process', return_value=None), patch.object(host, 'start') as start:
            host.restore_after_update(dict(self.state('manual'), options=self.options))
        self.assertEqual(vars(start.call_args.args[0]), dict(self.options, data_dir=self.data))
        with patch.object(host, 'owned_agent', return_value=(self.path, False)), \
                patch.object(host, 'owned_process', return_value=None), self.assertRaisesRegex(SystemExit, 'invalid'):
            host.restore_after_update(dict(self.state('manual'), options=dict(self.options, host='0.0.0.0')))

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

    def test_restore_rejects_state_from_another_project(self):
        state = self.state('managed')
        state['project'] = '/another/project'
        with patch.object(host, 'owned_agent') as agent:
            with self.assertRaisesRegex(SystemExit, 'does not belong'):
                host.restore_after_update(state)
            agent.assert_not_called()

    def test_pid_change_during_stop_does_not_signal_the_new_process(self):
        with patch.object(host, 'owned_process', side_effect=[(4242, self.options), (4343, self.options)]), \
             patch.object(host, 'process_table', return_value={}), patch.object(host.os, 'kill') as kill:
            with self.assertRaisesRegex(SystemExit, r'Another Coconut process \(4343\) started while process 4242 stopped'):
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




# A service that matches host.owned_process. On SIGTERM it stops its gateway, a child in its own session, as
# Service.close does. The environment sets how slowly each one stops: a number of seconds, or "ignore" for never.
FAKE_SERVICE = '''
import fcntl, json, os, signal, subprocess, sys, time
data = sys.argv[sys.argv.index('--data-dir') + 1]
lock = open(os.path.join(data, 'service.lock'), 'a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
gateway_stop = os.environ['FAKE_GATEWAY_STOP']
handler = 'signal.SIG_IGN' if gateway_stop == 'ignore' else f'lambda *a: (time.sleep({gateway_stop}), sys.exit(0))'
gateway = subprocess.Popen([sys.executable, '-c', f'import signal, sys, time\\nsignal.signal(signal.SIGTERM, {handler})\\n'
                            'while True: time.sleep(1)'], start_new_session=True)
# A worker that starts a background command of a turn in a session of its own. Both ignore SIGTERM, as a stuck worker
# and a long command can. The SIGKILL of the worker does not reach the command.
worker = subprocess.Popen([sys.executable, '-c', 'import signal, subprocess, sys, time\\n'
                           'signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n'
                           'command = subprocess.Popen([sys.executable, "-c", "import signal, time\\\\n'
                           'signal.signal(signal.SIGTERM, signal.SIG_IGN)\\\\nwhile True: time.sleep(1)"], start_new_session=True)\\n'
                           'print(command.pid, flush=True)\\nwhile True: time.sleep(1)'],
                          start_new_session=True, stdout=subprocess.PIPE, text=True)
background = int(worker.stdout.readline())
with open(os.path.join(data, 'events'), 'a') as events:
    events.write(json.dumps({'start': os.getpid(), 'gateway': gateway.pid, 'worker': worker.pid, 'background': background,
                             'source': open(os.path.join(os.environ['FAKE_PROJECT'], 'source')).read()}) + '\\n')
with open(os.path.join(data, 'process.json'), 'w') as record:
    json.dump({'pid': os.getpid(), 'project': os.environ['FAKE_PROJECT'], 'data_dir': data}, record)
def stop(*arguments):
    if os.environ['FAKE_SERVICE_STOP'] == 'ignore':
        return
    if os.environ['FAKE_SERVICE_STOP'] != 'leave-gateway':
        os.killpg(gateway.pid, signal.SIGTERM)
        gateway.wait()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    time.sleep(1)
'''

# The `coconut service` tool of the login item: it waits while the update hold holds, then becomes the service.
FAKE_TOOL = '''
import os, sys, time
hold = os.path.join(os.environ['FAKE_DATA'], 'update-hold')
def held():
    try:
        return float(open(hold).read()) > time.time()
    except (OSError, ValueError):
        return False
while held():
    if os.path.exists(os.path.join(os.environ['FAKE_DATA'], 'quit')):
        sys.exit(0)
    time.sleep(.1)
os.chdir(os.environ['FAKE_PACKAGE'])
os.execv(sys.executable, [sys.executable, '-m', 'coconut', '--data-dir', os.environ['FAKE_DATA'], '--port', '9000',
                          '--provider', 'fixture', '--tunnel-port', str(os.environ['FAKE_TUNNEL_PORT'])])
'''


class AppServiceStopTests(unittest.TestCase):
    """Real processes: a login item with KeepAlive, its service, and the gateway child of the service.

    On the Mac mini, the gateway of the service needed more time to stop than the stop waited (2026-10-29). The stop
    then failed, released the hold, and launchd started the old source again.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='coconut app stop ')
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.root = base / 'Coconut'
        self.data = self.root / '.runtime/host'
        self.data.mkdir(parents=True)
        package = base / 'fake'
        (package / 'coconut').mkdir(parents=True)
        (package / 'coconut/__init__.py').write_text('')
        (package / 'coconut/__main__.py').write_text(FAKE_SERVICE)
        self.tool = base / 'tool.py'
        self.tool.write_text(FAKE_TOOL)
        # The installed source. An update replaces it while the hold keeps the service stopped.
        (self.root / 'source').write_text('old')
        self.environment = dict(host.os.environ, FAKE_DATA=str(self.data), FAKE_PACKAGE=str(package),
                                FAKE_PROJECT=str(self.root), FAKE_TUNNEL_PORT=str(host.TUNNEL_PORT),
                                FAKE_SERVICE_STOP='gateway', FAKE_GATEWAY_STOP='0')
        for name, value in (('ROOT', self.root), ('DEFAULT_DATA', self.data)):
            patcher = patch.object(host, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.quit = False
        self.addCleanup(self.stop_everything)

    def start_login_item(self, **settings):
        """Run the tool again after each exit, as launchd does for a KeepAlive login item."""
        self.environment.update(settings)
        def keep_alive():
            while not self.quit:
                subprocess.run([sys.executable, str(self.tool)], env=self.environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(.2)
        self.launchd = threading.Thread(target=keep_alive, daemon=True)
        self.launchd.start()
        return self.wait_for_start(1)

    def events(self):
        path = self.data / 'events'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for_start(self, count, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            events = self.events()
            if len(events) >= count and (self.data / 'process.json').exists() \
                    and json.loads((self.data / 'process.json').read_text())['pid'] == events[count - 1]['start']:
                return events[count - 1]
            time.sleep(.05)
        self.fail(f'The service did not start {count} times.')

    def alive(self, pid):
        state = host.process_state(pid)
        return state is not None and not any(letter in state[0] for letter in host.EXITED)

    def stop_everything(self):
        self.quit = True
        (self.data / 'quit').touch()
        for attempt in range(2):
            for event in self.events():
                for pid in (event['start'], event['gateway'], event['worker'], event['background']):
                    try:
                        host.os.kill(pid, host.signal.SIGKILL)
                    except OSError:
                        pass
            if hasattr(self, 'launchd') and not attempt:
                self.launchd.join(timeout=5)

    def test_the_stop_waits_for_a_slow_gateway_and_the_hold_keeps_the_service_down_until_release(self):
        first = self.start_login_item(FAKE_GATEWAY_STOP='2')
        started = time.monotonic()
        with patch.object(host, 'CHILD_STOP_SECONDS', .5):
            self.assertEqual(host.stop_for_app_update()['mode'], 'app')
        self.assertGreaterEqual(time.monotonic() - started, 2)
        self.assertFalse(self.alive(first['background']))
        self.assertFalse(self.alive(first['start']))
        self.assertFalse(self.alive(first['gateway']))
        # launchd starts the tool again at once, but the tool waits for the hold.
        time.sleep(1)
        self.assertEqual(len(self.events()), 1)
        (self.root / 'source').write_text('new')
        host.release_login_item()
        self.assertEqual(self.wait_for_start(2)['source'], 'new')

    def test_a_service_that_does_not_stop_gets_sigkill_and_its_gateway_stops_too(self):
        first = self.start_login_item(FAKE_SERVICE_STOP='ignore', FAKE_GATEWAY_STOP='ignore')
        with patch.object(host, 'STOP_GRACE_SECONDS', .5), patch.object(host, 'CHILD_STOP_SECONDS', .5):
            self.assertEqual(host.stop_for_app_update()['mode'], 'app')
        self.assertFalse(self.alive(first['start']))
        # The gateway, the worker, and its background command ran in sessions of their own, so the SIGKILL of the service
        # did not reach them. The stop found them by their parents.
        self.assertFalse(self.alive(first['gateway']))
        self.assertFalse(self.alive(first['worker']))
        self.assertFalse(self.alive(first['background']))
        self.assertTrue((self.data / host.UPDATE_HOLD).exists())
        time.sleep(.5)
        self.assertEqual(len(self.events()), 1)

    def test_a_gateway_that_outlives_its_service_is_stopped(self):
        first = self.start_login_item(FAKE_SERVICE_STOP='leave-gateway', FAKE_GATEWAY_STOP='0')
        with patch.object(host, 'CHILD_STOP_SECONDS', .5):
            self.assertEqual(host.stop_for_app_update()['mode'], 'app')
        for name in ('start', 'gateway', 'worker', 'background'):
            self.assertFalse(self.alive(first[name]), name)

    def test_a_gateway_that_the_person_started_keeps_running(self):
        # Not a process of the service: no parent links it to the service, and it leads its own group.
        person = subprocess.Popen([sys.executable, '-c', 'import time\nwhile True: time.sleep(1)'], start_new_session=True)
        self.addCleanup(person.wait)
        self.addCleanup(person.kill)
        self.start_login_item(FAKE_SERVICE_STOP='leave-gateway', FAKE_GATEWAY_STOP='0')
        with patch.object(host, 'CHILD_STOP_SECONDS', .5):
            host.stop_for_app_update()
        self.assertIsNone(person.poll())


class StopDescendantsTests(unittest.TestCase):
    SERVICE = ('Thu Oct 29 16:08:05 2026', 'python -m coconut --data-dir /data')
    LISTING = (' 1 0 1 Ss Mon Oct 26 09:05:20 2026 /sbin/launchd\n'
               ' 4242 1 4242 Ss Thu Oct 29 16:08:05 2026 python -m coconut --data-dir /data\n'
               ' 20 4242 20 Ss Thu Oct 29 16:08:06 2026 python -c gateway run\n'
               ' 21 4242 21 Ss Thu Oct 29 16:08:07 2026 python hermes_worker.py\n'
               ' 30 21 30 Ss Thu Oct 29 16:09:00 2026 /bin/zsh -c npm run dev\n'
               ' 31 1 30 S Thu Oct 29 16:09:01 2026 node server.js\n'
               ' 40 1 40 Ss Thu Oct 29 16:00:00 2026 python -m hermes_cli.main gateway run\n'
               ' 41 40 40 S Thu Oct 29 16:00:01 2026 python gateway helper\n')

    def table(self, listing):
        with patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, listing, '')) as run:
            table = host.process_table()
        self.assertEqual(run.call_args.args[0], ['ps', '-A', '-ww', '-o', 'pid=,ppid=,pgid=,stat=,lstart=,command='])
        return table

    def stop(self, listing, known):
        """Run stop_descendants against a fixed process list. Return the signalled processes and the error output."""
        errors = io.StringIO()
        with patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, listing, '')), \
             patch.object(host.os, 'kill') as kill, patch.object(host, 'CHILD_STOP_SECONDS', 0), \
             patch.object(host, 'KILL_WAIT_SECONDS', 0), patch.object(sys, 'stderr', errors):
            try:
                host.stop_descendants(4242, known)
            except SystemExit:
                pass
        return {call.args[0] for call in kill.call_args_list}, errors.getvalue()

    def test_the_processes_of_the_service_are_its_descendants_and_the_groups_they_lead(self):
        known = {4242: self.SERVICE}
        host.record_descendants(known, self.table(self.LISTING))
        # 31 lost its parent, but it is in the group that the background command 30 leads, and 30 still runs.
        # 40 and 41 are a gateway that a person started, and its helper.
        self.assertEqual(set(known), {4242, 20, 21, 30, 31})
        self.assertEqual(known[21], ('Thu Oct 29 16:08:07 2026', 'python hermes_worker.py'))

    def test_a_reused_process_id_proves_nothing_about_its_children(self):
        # Worker 21 ended. Another program got its process ID and started a child. Neither gets a signal.
        reused = (' 21 1 21 Ss Thu Oct 29 16:30:00 2026 /usr/bin/ssh-agent\n'
                  ' 50 21 21 S Thu Oct 29 16:30:01 2026 /usr/bin/ssh-agent helper\n')
        known = {4242: self.SERVICE, 21: ('Thu Oct 29 16:08:07 2026', 'python hermes_worker.py')}
        host.record_descendants(known, self.table(reused))
        self.assertEqual(set(known), {4242, 21})
        signalled, _ = self.stop(reused, known)
        self.assertEqual(signalled, set())

    def test_a_process_in_the_group_of_an_ended_process_is_named_but_not_signalled(self):
        # The background command 30 ended. 31 is still in its group, but no living process proves that 31 is Coconut's.
        listing = ' 31 1 30 S Thu Oct 29 16:09:01 2026 node server.js\n'
        signalled, errors = self.stop(listing, {30: ('Thu Oct 29 16:09:00 2026', '/bin/zsh -c npm run dev')})
        self.assertEqual(signalled, set())
        self.assertIn('Coconut did not stop process 31 (node server.js)', errors)

    def test_only_the_processes_of_the_service_get_signals(self):
        known = {4242: self.SERVICE}
        host.record_descendants(known, self.table(self.LISTING))
        # The service itself already ended. The person's gateway 40 and its helper 41 get nothing.
        signalled, _ = self.stop(self.LISTING.replace(' 4242 1 4242 Ss', ' 4242 1 4242 Z'), known)
        self.assertEqual(signalled, {20, 21, 30, 31})

    def test_a_failed_process_list_signals_nothing(self):
        with patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'error')), \
             patch.object(host.os, 'kill') as kill:
            host.stop_descendants(4242, {20: ('Thu Oct 29 16:08:06 2026', 'python -c gateway run')})
        kill.assert_not_called()


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
