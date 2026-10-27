"""Coconut finds the person's own Hermes, checks it, and refuses cleanly without it."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from coconut import hermes_runtime, server
from coconut.hermes_runtime import HermesUnavailable
from coconut.hermes_unavailable import UnavailableService
from coconut.providers import FixtureProvider
from coconut.service import Service

MODULES = ('run_agent', 'hermes_cli', 'hermes_state', 'cron', 'tools', 'agent', 'gateway', 'dotenv', 'ruamel.yaml',
           'mcp', 'PIL')
REPOSITORY_VERSIONS = hermes_runtime.VERSIONS_FILE


def make_hermes(root: Path, *, missing=(), venv: Path | None = None) -> Path:
    """Create a Hermes source folder with a virtual environment, as the Hermes installer lays it out.

    The environment holds empty modules with the names that Coconut imports. Return its Python. With `venv`, the
    environment sits there, as the official installer's dependency environment, and its editable install points at an
    empty build copy, so only the source folder first on sys.path has run_agent.
    """
    venv = venv or root / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / 'run_agent.py').write_text('')
    packages = Path(subprocess.run([str(venv / 'bin/python'), '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
                                   check=True, capture_output=True, text=True).stdout.strip())
    editable = root if venv == root / 'venv' else venv.parent / 'workspace'
    editable.mkdir(parents=True, exist_ok=True)
    (packages / 'hermes_agent.pth').write_text(str(editable) + '\n')
    for name in MODULES:
        if name in missing or name == 'run_agent':
            continue
        folder = packages.joinpath(*name.split('.'))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / '__init__.py').write_text('')
    return venv / 'bin/python'


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / 'home'
        self.bin = self.home / '.local/bin'
        self.bin.mkdir(parents=True)
        self.environ = {'PATH': str(self.bin), 'HOME': str(self.home)}

    def command(self, text):
        path = self.bin / 'hermes'
        path.write_text(text)
        path.chmod(0o755)
        return path

    def test_the_search_order_is_the_settings_then_the_command_then_the_default_folder(self):
        self.command('#!/opt/hermes/venv/bin/python\nfrom hermes_cli.main import main\n')
        environ = dict(self.environ, COCONUT_HERMES_PYTHON='/custom/python', COCONUT_HERMES_ROOT='/custom')
        found = [(str(python), root and str(root), explicit) for python, root, explicit in hermes_runtime.candidates(environ, self.home)]
        default = self.home / '.hermes/hermes-agent'
        self.assertEqual(found, [('/custom/python', '/custom', True), ('/opt/hermes/venv/bin/python', None, False)])
        # The default folder counts once it has a Python.
        (default / '.venv/bin').mkdir(parents=True)
        (default / '.venv/bin/python').write_text('')
        found = [str(python) for python, _, _ in hermes_runtime.candidates(environ, self.home)]
        self.assertEqual(found[-1], str(default / '.venv/bin/python'))

    def official_install(self, home):
        """Lay out a Hermes as its official installer does, with HERMES_HOME `home`. Return the source folder and Python.

        The installer links ~/.local/bin/hermes to a launcher in the source folder. That launcher runs a store Python with
        the source folder first on sys.path. The dependency environment sits in <home>/installs/<key>, named by facts.json.
        """
        source = self.home / '.hermes/hermes-agent'
        environment = home / 'installs/key/environments/1e6af2de/venv'
        python = make_hermes(source, missing=(), venv=environment)
        key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
        facts = home / 'installs' / key / 'facts.json'
        facts.parent.mkdir(parents=True)
        facts.write_text(json.dumps({'packages': {'venv': {'environment': str(environment)}}, 'schema': 1}))
        launcher = source / '.hermes/bin/hermes'
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexec /store/python3 -I -c 'import os, sys\n"
                            f"sys.path.insert(0, '\"'\"'{source}'\"'\"')\nimport hermes_bootstrap' \"$@\"\n")
        launcher.chmod(0o755)
        self.command(f'#!/bin/sh\nexec {launcher} "$@"\n')
        return source, python

    def test_the_official_installer_layout_is_found_through_its_launchers(self):
        home = Path(self.temporary.name).resolve() / 'person-home'
        source, python = self.official_install(home)
        runtime = hermes_runtime.find(dict(self.environ, HERMES_HOME=str(home)), self.home)
        self.assertEqual((runtime.python, runtime.root, runtime.home), (python, source.resolve(), home))

    def test_the_hermes_home_of_the_login_shell_is_used(self):
        # A LaunchAgent does not get HERMES_HOME. Coconut asks the person's login shell, which reads ~/.zprofile.
        home = Path(self.temporary.name).resolve() / 'person-home'
        source, python = self.official_install(home)
        (self.home / '.zprofile').write_text(f'export HERMES_HOME={home}\n')
        (self.home / '.profile').write_text(f'export HERMES_HOME={home}\n')
        environ = dict(self.environ, SHELL='/bin/zsh' if Path('/bin/zsh').exists() else '/bin/sh')
        self.assertEqual(hermes_runtime.hermes_home(environ, self.home), home)
        self.assertEqual(hermes_runtime.find(environ, self.home).python, python)

    def test_a_hermes_home_inside_coconuts_runtime_folder_is_the_persons(self):
        # On the Mac mini, HERMES_HOME is .runtime/hermes/terminal/hermes, the old shared home of the bots.
        home = hermes_runtime.PROJECT_ROOT / '.runtime/hermes/terminal/hermes'
        python = home / 'installs/key/environments/1e6af2de/venv/bin/python'
        with patch.object(hermes_runtime, 'source_pythons', return_value=[python]):
            found = [candidate for candidate, _, _ in hermes_runtime.candidates(dict(self.environ, HERMES_HOME=str(home)), self.home)]
        self.assertIn(python, found)
        old = hermes_runtime.PROJECT_ROOT / '.runtime/hermes/venv/bin/python'
        self.command(f'#!{old}\n')
        self.assertNotIn(old, [candidate for candidate, _, _ in hermes_runtime.candidates(self.environ, self.home)])

    def test_coconuts_own_launcher_is_not_the_persons_hermes(self):
        self.command('#!/bin/sh\n# Coconut owns this Hermes launcher.\nexec /usr/bin/python3 terminal.py launch -- "$@"\n')
        self.assertNotIn('/usr/bin/python3', [str(python) for python, _, _ in hermes_runtime.candidates(self.environ, self.home)])

    def test_the_persons_hermes_after_coconuts_old_launcher_on_path_is_found(self):
        # On the Mac mini, ~/.local/bin/hermes is Coconut's old launcher, and the real command comes later on PATH.
        self.command('#!/bin/sh\n# Coconut owns this Hermes launcher.\nexec /usr/bin/python3 terminal.py launch -- "$@"\n')
        later = self.home / 'tools/bin'
        later.mkdir(parents=True)
        (later / 'hermes').write_text('#!/opt/hermes/venv/bin/python\nfrom hermes_cli.main import main\n')
        (later / 'hermes').chmod(0o755)
        environ = dict(self.environ, PATH=f'{self.bin}:{later}')
        found = [python for python, _, _ in hermes_runtime.candidates(environ, self.home)]
        self.assertEqual(found[0], Path('/opt/hermes/venv/bin/python'))
        self.assertNotIn(Path('/usr/bin/python3'), found)

    def test_a_wrapper_with_a_quoted_path_reaches_the_launcher(self):
        folder = self.home / 'Hermes Agent' / "it's here"
        folder.mkdir(parents=True)
        (folder / 'hermes').write_text('#!/opt/hermes/venv/bin/python\n')
        quoted = folder.as_posix().replace("'", "'\\''")
        escaped = folder.as_posix().replace(' ', '\\ ').replace("'", "\\'")
        for line in (f'exec "{folder}/hermes" "$@"', f"exec '{quoted}/hermes' \"$@\"", f'exec {escaped}/hermes "$@"'):
            with self.subTest(line=line):
                command = self.command(f'#!/bin/sh\n{line}\n')
                self.assertEqual(hermes_runtime.launcher_target(command), (Path('/opt/hermes/venv/bin/python'), None))

    def test_a_long_interpreter_path_in_a_shell_wrapper_is_read(self):
        command = self.command("#!/bin/sh\n'''exec' \"/very/long/hermes/venv/bin/python\" \"$0\" \"$@\"\n' '''\nimport sys\n")
        self.assertEqual(hermes_runtime.launcher_target(command), (Path('/very/long/hermes/venv/bin/python'), None))

    def test_the_private_runtime_of_older_versions_is_skipped(self):
        private = hermes_runtime.PROJECT_ROOT / '.runtime/hermes/venv/bin/python'
        self.command(f'#!{private}\n')
        self.assertNotIn(private, [python for python, _, _ in hermes_runtime.candidates(self.environ, self.home)])

    def test_the_installed_hermes_is_found_through_its_command(self):
        root = self.home / '.hermes/hermes-agent'
        python = make_hermes(root)
        (self.bin / 'hermes').symlink_to(python.parent / 'hermes')
        (python.parent / 'hermes').write_text(f'#!{python}\nfrom hermes_cli.main import main\n')
        runtime = hermes_runtime.find(self.environ, self.home)
        self.assertEqual(runtime.python, python)
        self.assertEqual(runtime.root, root.resolve())
        self.assertEqual(runtime.home, self.home / '.hermes')

    def test_a_wrong_explicit_setting_never_falls_back_to_another_hermes(self):
        make_hermes(self.home / '.hermes/hermes-agent')
        environ = dict(self.environ, COCONUT_HERMES_ROOT=str(self.home / 'elsewhere'))
        with self.assertRaises(HermesUnavailable) as raised:
            hermes_runtime.find(environ, self.home)
        self.assertIn('COCONUT_HERMES_ROOT names no Hermes Python', raised.exception.reason)

    def test_a_source_folder_with_keys_is_refused(self):
        root = self.home / '.hermes/hermes-agent'
        make_hermes(root)
        (root / '.env').write_text('ANTHROPIC_API_KEY=secret\n')
        with self.assertRaises(HermesUnavailable) as raised:
            hermes_runtime.find(self.environ, self.home)
        self.assertEqual(raised.exception.status, 'incompatible')
        self.assertIn('.env', raised.exception.reason)
        self.assertNotIn('secret', raised.exception.reason)

    def test_no_hermes_is_missing(self):
        with self.assertRaises(HermesUnavailable) as raised:
            hermes_runtime.find(self.environ, self.home)
        self.assertEqual(raised.exception.status, 'missing')
        self.assertIn('Install Hermes', raised.exception.reason)

    def test_a_hermes_without_a_module_that_coconut_uses_is_incompatible(self):
        make_hermes(self.home / '.hermes/hermes-agent', missing=('mcp',))
        with self.assertRaises(HermesUnavailable) as raised:
            hermes_runtime.find(self.environ, self.home)
        self.assertEqual(raised.exception.status, 'incompatible')
        self.assertIn("No module named 'mcp'", raised.exception.reason)

    def test_a_hermes_that_fails_the_contract_is_incompatible(self):
        make_hermes(self.home / '.hermes/hermes-agent')
        runtime = hermes_runtime.find(self.environ, self.home)
        # The empty modules lack every interface that the contract check reads.
        with self.assertRaises(HermesUnavailable) as raised:
            hermes_runtime.check(runtime, self.environ)
        self.assertEqual(raised.exception.status, 'incompatible')
        self.assertIn('does not work with the installed Hermes', raised.exception.reason)

    def test_the_status_is_saved_and_reused_for_the_same_hermes_and_code(self):
        make_hermes(self.home / '.hermes/hermes-agent')
        data = Path(self.temporary.name) / 'data'
        with patch.object(hermes_runtime, 'check', return_value={}) as check:
            first = hermes_runtime.status(data, self.environ, self.home)
            # A restart with the same Hermes and code starts no program.
            with patch.object(hermes_runtime, 'probe', side_effect=AssertionError('The probe ran again.')):
                second = hermes_runtime.status(data, self.environ, self.home)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(hermes_runtime.read_status(data), first)
        self.assertEqual(hermes_runtime.public(first), {'status': 'ok', 'version': None, 'reason': None, 'tested': False})
        # An update checks new code without replacing the status of the running version.
        with patch.object(hermes_runtime, 'check', side_effect=HermesUnavailable('incompatible', 'Changed.')), \
                patch.object(hermes_runtime, 'fingerprint', return_value='new code'):
            self.assertEqual(hermes_runtime.status(data, self.environ, self.home, save=False)['status'], 'incompatible')
        self.assertEqual(hermes_runtime.read_status(data), first)

    def test_a_failure_is_checked_again_at_the_next_start(self):
        # A person can repair Hermes without a change that the fingerprint sees. A restart then works at once.
        make_hermes(self.home / '.hermes/hermes-agent')
        data = Path(self.temporary.name) / 'data'
        failure = HermesUnavailable('incompatible', 'Broken.')
        with patch.object(hermes_runtime, 'check', side_effect=[failure, {}]):
            self.assertEqual(hermes_runtime.status(data, self.environ, self.home)['status'], 'incompatible')
            self.assertEqual(hermes_runtime.status(data, self.environ, self.home)['status'], 'ok')

    def test_only_coconuts_own_launcher_is_removed(self):
        launcher = self.command('#!/bin/sh\n# Coconut owns this Hermes launcher.\nexec true\n')
        self.assertTrue(hermes_runtime.remove_coconut_launcher(self.home))
        self.assertFalse(launcher.exists())
        own = self.command('#!/opt/hermes/venv/bin/python\n')
        self.assertFalse(hermes_runtime.remove_coconut_launcher(self.home))
        self.assertTrue(own.exists())


class RefuseModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / 'data'
        self.status = {'status': 'missing', 'version': None, 'reason': 'Coconut found no Hermes on this Mac.'}
        self.start(UnavailableService(self.data, self.status))

    def start(self, service):
        self.service = service
        self.server = server.APIServer(('127.0.0.1', 0), service)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.close()

    def request(self, method, path, body=None, token=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        content = None
        if body is not None:
            content = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=content, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def pair(self):
        code = server.create_pairing_code(self.data)
        status, body = self.request('POST', '/v1/pair', {'code': code, 'device_name': 'Phone'})
        self.assertEqual(status, 200)
        return body['token']

    def test_the_phone_can_pair_and_read_why_coconut_cannot_run(self):
        self.addCleanup(self.stop)
        self.assertEqual(self.request('GET', '/health'), (200, {'ok': True, 'api_version': 1}))
        token = self.pair()
        status, setup = self.request('GET', '/v1/setup', token=token)
        self.assertEqual(status, 200)
        self.assertEqual(setup['hermes'], dict(self.status, tested=None))
        self.assertEqual(set(setup), {'hermes'})

    def test_agent_routes_answer_hermes_unavailable(self):
        self.addCleanup(self.stop)
        self.assertEqual(self.request('GET', '/v1/state')[0], 401)
        token = self.pair()
        for method, path in (('GET', '/v1/state'), ('GET', '/v1/updates?after=0'), ('POST', '/v1/bots')):
            with self.subTest(path=path):
                status, body = self.request(method, path, {} if method == 'POST' else None, token=token)
                self.assertEqual(status, 503)
                self.assertEqual(body['code'], 'hermes_unavailable')
                self.assertEqual(body['hermes'], dict(self.status, tested=None))

    def test_an_update_stops_refuse_mode_without_waiting_for_turns(self):
        self.addCleanup(self.stop)
        server.write_process_record(self.service)
        self.assertFalse(json.loads((self.data / 'process.json').read_text())['drain'])

    def test_a_phone_paired_in_refuse_mode_stays_paired_with_hermes(self):
        token = self.pair()
        self.stop()
        service = Service(self.data, FixtureProvider(self.data), start_workers=False)
        self.addCleanup(service.close)
        self.assertTrue(service.authenticate(token))


class ServiceInterpreterTests(unittest.TestCase):
    def test_a_checked_status_from_host_is_used_once(self):
        status = {'status': 'missing', 'version': None, 'reason': 'None here.'}
        with patch.dict(os.environ, {'COCONUT_HERMES_STATUS': json.dumps(status)}), \
                patch.object(hermes_runtime, 'status') as find:
            self.assertEqual(server.hermes_status('/data', []), status)
            self.assertNotIn('COCONUT_HERMES_STATUS', os.environ)
        find.assert_not_called()

    def test_the_restarted_service_never_starts_again(self):
        # A launcher can report another path in sys.executable. The checked status from the first process is final.
        status = {'status': 'ok', 'version': '1.0', 'reason': None, 'python': '/hermes/venv/bin/python', 'root': '/hermes'}
        with patch.dict(os.environ, {'COCONUT_HERMES_STATUS': json.dumps(status), 'COCONUT_HERMES_RESTARTED': '1'}), \
                patch.object(server.os, 'execv') as execv:
            self.assertEqual(server.hermes_status('/data', []), status)
            self.assertNotIn('COCONUT_HERMES_RESTARTED', os.environ)
        execv.assert_not_called()

    def test_a_status_from_host_start_still_moves_to_the_hermes_python(self):
        # host.py start checks Hermes before the service starts, under the Python that runs host.py.
        status = {'status': 'ok', 'version': '1.0', 'reason': None, 'python': '/hermes/venv/bin/python', 'root': '/hermes'}
        with patch.dict(os.environ, {'COCONUT_HERMES_STATUS': json.dumps(status)}), \
                patch.object(hermes_runtime, 'status') as find, patch.object(server.os, 'execv') as execv:
            server.hermes_status('/data', [])
            self.assertEqual(os.environ['COCONUT_HERMES_RESTARTED'], '1')
        find.assert_not_called()
        execv.assert_called_once()

    def test_a_python_that_cannot_start_means_refuse_mode(self):
        status = {'status': 'ok', 'version': '1.0', 'reason': None, 'python': '/hermes/venv/bin/python', 'root': '/hermes'}
        with patch.dict(os.environ, {}), patch.object(hermes_runtime, 'status', return_value=status), \
                patch.object(server.os, 'execv', side_effect=FileNotFoundError(2, 'No such file or directory')):
            result = server.hermes_status('/data', [])
            self.assertNotIn('COCONUT_HERMES_STATUS', os.environ)
        self.assertEqual(result['status'], 'incompatible')

    def test_the_service_starts_again_under_the_hermes_python(self):
        status = {'status': 'ok', 'version': '1.0', 'reason': None, 'python': '/hermes/venv/bin/python', 'root': '/hermes'}
        with patch.dict(os.environ, {}), patch.object(hermes_runtime, 'status', return_value=status), \
                patch.object(server.os, 'execv') as execv:
            server.hermes_status('/data', ['--data-dir', '/data'])
            self.assertEqual(json.loads(os.environ['COCONUT_HERMES_STATUS']), status)
        execv.assert_called_once_with('/hermes/venv/bin/python', ['/hermes/venv/bin/python', '-m', 'coconut', '--data-dir', '/data'])


class SupportedVersionTests(unittest.TestCase):
    """Coconut reports a version outside the tested range as untested, and still runs on it."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'hermes_versions.json'
        path.write_text(json.dumps({'minimum': '0.22.4', 'tested': ['0.22.4', '0.22.5']}))
        patcher = patch.object(hermes_runtime, 'VERSIONS_FILE', path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_repository_file_names_a_minimum_and_tested_versions(self):
        with patch.object(hermes_runtime, 'VERSIONS_FILE', REPOSITORY_VERSIONS):
            versions = hermes_runtime.supported_versions()
        self.assertIsNotNone(hermes_runtime.version_key(versions['minimum']))
        self.assertIn(versions['minimum'], versions['tested'])
        self.assertTrue(all(hermes_runtime.version_key(item) for item in versions['tested']))

    def test_versions_in_the_range_are_tested(self):
        for version in ('0.22.4', 'v0.22.5'):
            self.assertTrue(hermes_runtime.is_tested(version), version)
            self.assertIsNone(hermes_runtime.untested_warning(version))

    def test_other_versions_are_untested(self):
        # 2026.9.24 is a calendar version from before 0.21.6, so it is older than the minimum.
        for version in ('0.22.6', '0.23.0', '0.22.3', '2026.9.24', 'commit 4d04303818b6', None):
            self.assertFalse(hermes_runtime.is_tested(version), version)
        self.assertEqual(hermes_runtime.untested_warning('0.23.0'),
                         'Coconut has not been tested with Hermes 0.23.0. The contract check passed. '
                         'The newest tested version is 0.22.5.')

    def test_a_missing_or_invalid_version_file_makes_every_version_untested(self):
        for text in (None, 'not json', '{"tested": ["0.22.5"]}', '{"minimum": "new", "tested": []}'):
            with self.subTest(text=text):
                if text is None:
                    hermes_runtime.VERSIONS_FILE.unlink(missing_ok=True)
                else:
                    hermes_runtime.VERSIONS_FILE.write_text(text)
                self.assertFalse(hermes_runtime.is_tested('0.22.5'))
                self.assertIn('The newest tested version is unknown.', hermes_runtime.untested_warning('0.22.5'))
                self.assertIs(hermes_runtime.public({'status': 'ok', 'version': '0.22.5'})['tested'], False)

    def test_the_phone_gets_the_flag_only_for_a_usable_hermes(self):
        self.assertEqual(hermes_runtime.public({'status': 'ok', 'version': '0.23.0'})['tested'], False)
        self.assertEqual(hermes_runtime.public({'status': 'ok', 'version': '0.22.5'})['tested'], True)
        self.assertIsNone(hermes_runtime.public({'status': 'incompatible', 'version': '0.23.0', 'reason': 'No.'})['tested'])
        self.assertIsNone(hermes_runtime.public(None)['tested'])

    def test_the_setup_route_reports_an_untested_version(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory, FixtureProvider(directory), start_workers=False)
            self.addCleanup(service.close)
            service.hermes_status = hermes_runtime.public({'status': 'ok', 'version': '0.23.0'})
            self.assertEqual(service.hermes_setup(),
                             {'hermes': {'status': 'ok', 'version': '0.23.0', 'reason': None, 'tested': False}})


if __name__ == '__main__':
    unittest.main()
