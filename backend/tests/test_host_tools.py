import argparse
import fcntl
import importlib.util
import json
from pathlib import Path
import plistlib
import sys
import shutil
import ssl
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))  # cleanup imports terminal, as when it runs from scripts/.


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

    def test_unavailable_restart_support_does_not_block_manual_start(self):
        with patch.object(host, 'owned_agent', side_effect=SystemExit('Another installation owns the job')):
            self.assertFalse(host.managed_current_process())

    def test_migrated_source_cannot_start_another_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "moved-to.json").write_text('{"address":"https://mini.example.ts.net:8960"}')
            args = self.arguments(directory)
            with self.assertRaisesRegex(SystemExit, "Execution moved"):
                host.service_args(args)
            self.assertFalse((data / "server.key").exists())

    def arguments(self, directory, **changes):
        values = dict(data_dir=Path(directory), address=None, port=None, command='setup',
                      replace_certificate=False, host='0.0.0.0', provider='fixture', insecure_loopback=False)
        values.update(changes)
        args = argparse.Namespace(**values)
        host.configure_address(args)
        return args

    def assert_certificate_connects(self, cert, key, hostname):
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        client_context = ssl.create_default_context(cafile=str(cert))
        client_in, client_out, server_in, server_out = [ssl.MemoryBIO() for _ in range(4)]
        client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
        server = server_context.wrap_bio(server_in, server_out, server_side=True)
        completed = set()
        for _ in range(20):
            for name, connection in [('client', client), ('server', server)]:
                try:
                    connection.do_handshake()
                    completed.add(name)
                except ssl.SSLWantReadError:
                    pass
            if client_out.pending:
                server_in.write(client_out.read())
            if server_out.pending:
                client_in.write(server_out.read())
            if len(completed) == 2:
                break
        self.assertEqual(completed, {'client', 'server'})
        return client.getpeercert()

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL is required for the local TLS certificate.')
    def test_certificate_matches_the_local_hostname(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = host.certificate(Path(directory))
            self.assert_certificate_connects(cert, key, host.socket.gethostname())
            self.assertEqual(len(host.fingerprint(cert)), 64)
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(Path('/usr/bin/openssl').is_file(), 'The system OpenSSL is required for this certificate test.')
    def test_system_openssl_certificate_uses_named_p256_curve(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(host.shutil, 'which', return_value='/usr/bin/openssl'), \
             patch.object(host.socket, 'gethostname', return_value='test-mini'), \
             patch.object(host.socket, 'getaddrinfo', return_value=[]):
            root = Path(directory)
            cert, key = root / 'server.crt', root / 'server.key'
            host.create_certificate(root, cert, key, 'test-mini.local')
            details = subprocess.run(
                ['/usr/bin/openssl', 'x509', '-in', str(cert), '-noout', '-text'],
                check=True, capture_output=True, text=True,
            ).stdout
            self.assertIn('Public Key Algorithm: id-ecPublicKey', details)
            self.assertIn('ASN1 OID: prime256v1', details)
            self.assertNotIn('Field Type:', details)
            self.assert_certificate_connects(cert, key, 'test-mini.local')

    def test_remote_dns_certificate_keeps_local_names(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(host.socket, 'gethostname', return_value='test-mini'), \
             patch.object(host.socket, 'getaddrinfo', return_value=[]):
            args = self.arguments(directory, address='https://mini.example.ts.net:8960')
            cert, key = host.connection_certificate(args)
            details = self.assert_certificate_connects(cert, key, 'mini.example.ts.net')
            self.assertIn(('DNS', 'mini.example.ts.net'), details['subjectAltName'])
            for hostname in ('localhost', 'test-mini', 'test-mini.local', '127.0.0.1'):
                self.assert_certificate_connects(cert, key, hostname)
            with self.assertRaises(ssl.SSLCertVerificationError):
                self.assert_certificate_connects(cert, key, 'other.example.ts.net')

    def test_remote_ip_certificate_uses_ip_san(self):
        for address, hostname in (('https://100.82.1.2:8960', '100.82.1.2'), ('https://[fd7a:115c:a1e0::1]:8960', 'fd7a:115c:a1e0::1')):
            with self.subTest(address=address), tempfile.TemporaryDirectory() as directory:
                args = self.arguments(directory, address=address)
                cert, key = host.connection_certificate(args)
                details = self.assert_certificate_connects(cert, key, hostname)
                self.assertTrue(any(kind == 'IP Address' for kind, _ in details['subjectAltName']))

    def test_address_rejects_unsafe_urls_and_conflicting_ports(self):
        invalid = ['http://mini:8960', 'https://user@mini:8960', 'https://user:password@mini:8960',
                   'https://mini:8960/v1', 'https://mini:8960?', 'https://mini:8960#',
                   'https://mini:0', 'https://mini:65536', 'https://mini:-1', 'https://mini:',
                   'https://[::1]extra:8960', 'https://mini:8960\n', 'https://mini:8960 bad',
                   'https://mini,IP:127.0.0.1:8960', 'https://mini\\other:8960',
                   'https://[fe80::1%en0]:8960', 'https://%6dini:8960', 'https://-mini:8960']
        for address in invalid:
            with self.subTest(address=address), self.assertRaises(SystemExit):
                host.address_parts(address)
        with self.assertRaisesRegex(SystemExit, 'must match'):
            host.address_parts('https://mini:9000', 8960)

    def test_local_default_address_uses_port_8960(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(host.socket, 'gethostname', return_value='Test-Mini.local'):
            args = self.arguments(directory)
            self.assertEqual(args.port, 8960)
            self.assertEqual(args.address, 'https://test-mini.local:8960')
            self.assertFalse((Path(directory) / 'connection.json').exists())

    def test_saved_address_and_nondefault_port_survive_launchers(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.arguments(directory, address='https://mini.example.ts.net:9000/')
            host.connection_certificate(args)
            path = Path(directory) / 'connection.json'
            self.assertEqual(json.loads(path.read_text()), {'address': 'https://mini.example.ts.net:9000'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for command in host.ADDRESS_COMMANDS:
                restored = self.arguments(directory, command=command)
                self.assertEqual((restored.address, restored.port), ('https://mini.example.ts.net:9000', 9000))
                arguments = host.service_args(restored)
                self.assertEqual(arguments[arguments.index('--port') + 1], '9000')
            with self.assertRaisesRegex(SystemExit, 'must match'):
                self.arguments(directory, port=8960)

    def test_existing_certificate_cannot_change_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = host.certificate(Path(directory))
            previous = (cert.read_bytes(), key.read_bytes())
            args = self.arguments(directory, address='https://mini.example.ts.net:8960')
            with self.assertRaisesRegex(SystemExit, '--replace-certificate'):
                host.connection_certificate(args)
            self.assertEqual((cert.read_bytes(), key.read_bytes()), previous)
            self.assertFalse((Path(directory) / 'connection.json').exists())

    def test_certificate_replacement_keeps_backup_and_host_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = host.certificate(root)
            previous = (cert.read_bytes(), key.read_bytes())
            history = root / 'coconut.sqlite3'
            history.write_bytes(b'Host data must remain unchanged.')
            args = self.arguments(directory, address='https://mini.example.ts.net:8960', replace_certificate=True)
            host.connection_certificate(args)
            backups = list(root.glob('certificate-backup-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(((backups[0] / 'server.crt').read_bytes(), (backups[0] / 'server.key').read_bytes()), previous)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o700)
            self.assertNotEqual(cert.read_bytes(), previous[0])
            self.assert_certificate_connects(cert, key, 'mini.example.ts.net')
            self.assertEqual(history.read_bytes(), b'Host data must remain unchanged.')

    def test_certificate_replacement_requires_stopped_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = host.certificate(root)
            previous = (cert.read_bytes(), key.read_bytes())
            args = self.arguments(directory, address='https://mini.example.ts.net:8960', replace_certificate=True)
            with (root / 'service.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(SystemExit, 'Stop Coconut'):
                    host.connection_certificate(args)
            self.assertEqual((cert.read_bytes(), key.read_bytes()), previous)
            self.assertFalse((root / 'connection.json').exists())
            self.assertEqual(list(root.glob('certificate-backup-*')), [])

    def test_address_change_requires_stopped_service_but_same_address_can_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = self.arguments(directory, address='https://mini.example.ts.net:8960')
            cert, key = host.connection_certificate(original)
            previous = (cert.read_bytes(), key.read_bytes(), (root / 'connection.json').read_bytes())
            changed = self.arguments(directory, address='https://mini.example.ts.net:9000')
            with (root / 'service.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(SystemExit, 'before changing its address'):
                    host.connection_certificate(changed)
                host.connection_certificate(original)
            self.assertEqual((cert.read_bytes(), key.read_bytes(), (root / 'connection.json').read_bytes()), previous)

    def test_failed_generation_keeps_certificate_and_saved_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = self.arguments(directory, address='https://first.example.ts.net:8960')
            cert, key = host.connection_certificate(original)
            previous = (cert.read_bytes(), key.read_bytes(), (root / 'connection.json').read_bytes())
            replacement = self.arguments(directory, address='https://second.example.ts.net:8960', replace_certificate=True)
            with patch.object(host.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'openssl')):
                with self.assertRaises(subprocess.CalledProcessError):
                    host.connection_certificate(replacement)
            self.assertEqual((cert.read_bytes(), key.read_bytes(), (root / 'connection.json').read_bytes()), previous)

    def test_launchagent_reuses_saved_address_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.arguments(directory, address='https://mini.example.ts.net:9000', command='install-service')
            host.connection_certificate(args)
            with patch.object(Path, 'home', return_value=root), \
                 patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                host.install(args)
            payload = plistlib.loads((root / 'Library/LaunchAgents/local.coconut.agent.plist').read_bytes())
            command = payload['ProgramArguments']
            self.assertNotIn('--port', command)
            self.assertTrue(payload['KeepAlive'])
            self.assertEqual(payload['ProcessType'], 'Interactive')
            self.assertEqual(json.loads((root / 'connection.json').read_text())['address'], args.address)
            changed = self.arguments(directory, address='https://mini.example.ts.net:9001')
            host.connection_certificate(changed)
            with patch.object(sys, 'argv', command[1:]), patch.object(host, 'load_credentials'), \
                 patch.object(host.os, 'chdir'), patch.object(host.os, 'execv') as execute:
                host.main()
            server_args = execute.call_args.args[1]
            self.assertEqual(server_args[server_args.index('--port') + 1], '9001')

    def test_launchagent_persists_default_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.arguments(directory, command='install-service')
            host.connection_certificate(args)
            self.assertFalse((root / 'connection.json').exists())
            with patch.object(Path, 'home', return_value=root), \
                 patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                host.install(args)
            self.assertEqual(json.loads((root / 'connection.json').read_text())['address'], args.address)

    def test_connection_config_cannot_follow_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / 'keep.json'
            sentinel.write_text('{"address":"https://mini.example.ts.net:8960"}')
            (root / 'connection.json').symlink_to(sentinel)
            with self.assertRaisesRegex(SystemExit, 'symbolic link'):
                self.arguments(directory)
            self.assertTrue(sentinel.exists())

    def test_plain_http_cannot_listen_on_lan(self):
        args = argparse.Namespace(data_dir=Path('/unused'), host='0.0.0.0', port=8960,
                                  provider='hermes', insecure_loopback=True)
        with self.assertRaisesRegex(SystemExit, 'only on loopback'):
            host.service_args(args)

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
        self.options = {'host': '127.0.0.1', 'port': 9000, 'provider': 'fixture', 'insecure_loopback': True}
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
        command = f'/usr/bin/python3 -m coconut --data-dir {self.data} --host 127.0.0.1 --port 9000 --provider fixture'
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

    def test_restore_manual_preserves_fixture_and_bind_options(self):
        with patch.object(host, 'owned_agent', return_value=(self.path, False)), \
             patch.object(host, 'owned_process', return_value=None), patch.object(host, 'start') as start:
            host.restore_after_update(self.state('manual'))
        arguments = start.call_args.args[0]
        for name, value in self.options.items():
            self.assertEqual(getattr(arguments, name), value)

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
        args = argparse.Namespace(data_dir=self.data, address=None, port=None, host='0.0.0.0',
                                  provider='hermes', insecure_loopback=False, replace_certificate=False)
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


if __name__ == '__main__':
    unittest.main()
