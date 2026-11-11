"""The two modes of an install (docs/install-and-update.md, "A Mac without a person").

A full install sets up and updates as before: fixtures/full_install_plan.json holds its steps as origin/main gave them
before the team-only mode existed. A team-only install runs no step that finds, checks or changes Hermes.
"""
import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('coconut_install_modes_deploy', SCRIPTS / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)
sys.path.insert(0, str(SCRIPTS))
import desktop  # noqa: E402
import host  # noqa: E402
import remote  # noqa: E402
import setup_coconut  # noqa: E402

from coconut.service_base import TEAM_MODE, WRONG_FOLDER, ServiceError  # noqa: E402
from test_deploy_tools import FakeOperations, archive, package_files  # noqa: E402
from test_team_only import make_hermes_home, snapshot  # noqa: E402

FIXTURE = TESTS / 'fixtures/full_install_plan.json'
ROOT = Path('/Coconut')


class DrainingOperations(FakeOperations):
    def drain(self, root, source=None):
        self.record('drain', root)


def step_names(operations):
    return [call[0] for call in operations.calls]


@contextlib.contextmanager
def recorded_commands(output=b''):
    """Record each command of the real Operations in place of running it."""
    commands = []

    def command(self, arguments, root, phase, **options):
        commands.append([phase, [str(argument).replace(sys.executable, 'python') for argument in arguments]])
        return output
    with patch.object(deploy.Operations, 'command', command):
        yield commands


def health_port(operations):
    """The port that the health check of the real Operations asks."""
    ports = []

    class Connection:
        def __init__(self, host, port, timeout):
            ports.append(port)

        def request(self, method, path):
            pass

        def getresponse(self):
            class Response:
                status = 200

                def read(self, limit):
                    return b'{"ok": true, "api_version": 1}'
            return Response()

        def close(self):
            pass
    with patch.object(deploy.http.client, 'HTTPConnection', Connection):
        operations.health(SCRIPTS.parent)
    return ports


def full_plan(home):
    """The steps of a full setup and of a full update under Coconut.app, and the commands of each step."""
    target = home / 'Coconut'
    operations = FakeOperations()
    operations.install_mode = 'app'
    deploy.receive('Coconut', archive(), home=home, operations=operations)
    setup = step_names(operations)
    operations = DrainingOperations(mode='app')
    deploy.receive('Coconut', archive(package_files(**{'README.md': b'New code.\n'})), home=home, operations=operations)
    update = step_names(operations)
    assert target.is_dir()
    real = deploy.Operations()
    commands = {}
    with patch.dict(os.environ, {deploy.APP_MANAGED: '1'}):
        for name, call in (('preflight', lambda: real.preflight(ROOT, None)),
                           ('prepare', lambda: real.prepare(ROOT, None)),
                           ('install', lambda: real.install(ROOT)),
                           ('tunnel', lambda: real.tunnel(ROOT)),
                           ('skills', lambda: real.skills(ROOT))):
            with recorded_commands() as recorded:
                call()
            commands[name] = [[phase, [part.replace(str(os.getuid()), '<uid>') for part in arguments]]
                              for phase, arguments in recorded]
        with recorded_commands(b'COCONUT_HERMES_STATUS={"status": "ok"}\n') as recorded:
            real.hermes(ROOT)
        commands['hermes'] = recorded
    return {'setup': setup, 'update': update, 'commands': commands, 'health_port': health_port(real)}


class FullInstallPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def test_a_full_setup_and_update_run_the_steps_of_the_fixture(self):
        environment = {key: value for key, value in os.environ.items() if key != deploy.MODE_ENVIRONMENT}
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(full_plan(self.home), json.loads(FIXTURE.read_text()))


class TeamOnlyPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def test_the_app_names_the_mode_in_the_environment(self):
        self.assertTrue(deploy.team_only_mode({'COCONUT_MODE': 'team'}))
        self.assertFalse(deploy.team_only_mode({}))
        self.assertFalse(deploy.team_only_mode({'COCONUT_MODE': 'full'}))
        with patch.dict(os.environ, {'COCONUT_MODE': 'team'}):
            self.assertTrue(deploy.Operations(team_only=deploy.team_only_mode()).team_only)

    def test_a_team_only_setup_and_update_check_no_hermes_and_start_no_sidecar(self):
        operations = FakeOperations(hermes='incompatible')
        operations.install_mode = 'app'
        result = deploy.receive('Coconut', archive(), home=self.home, operations=operations, team_only=True)
        self.assertEqual(step_names(operations), ['preflight', 'prepare', 'install', 'health'])
        self.assertEqual(result['mode'], 'app')
        self.assertTrue(result['team_only'])
        self.assertNotIn('tunnel', result)
        self.assertNotIn('skills', result)
        # The service has no drain (team_only.TeamOnlyService.drains), so host.drain_for_update returns at once.
        operations = DrainingOperations(mode='app', hermes='incompatible')
        deploy.receive('Coconut', archive(package_files(**{'README.md': b'New code.\n'})), home=self.home,
                       operations=operations, team_only=True)
        self.assertEqual(step_names(operations),
                         ['preflight', 'inspect', 'drain', 'stop', 'prepare', 'restore', 'health'])
        self.assertIn(('restore', self.home / 'Coconut', {'mode': 'app'}), operations.calls)
        # The update wrote no Hermes status for the service to take.
        self.assertFalse((self.home / 'Coconut/.runtime/host/hermes.json').exists())

    def test_the_team_only_steps_run_no_hermes_or_notification_command(self):
        real = deploy.Operations(team_only=True)
        with recorded_commands() as commands:
            real.preflight(ROOT, None)
            real.prepare(ROOT, None)
        self.assertEqual(commands, [['Team helper build', ['python', '/Coconut/scripts/team.py', 'build']]])
        # A team-only service has no tunnel listener. Its health check asks the main listener.
        self.assertEqual(health_port(real), [8960])

    def test_a_team_only_install_and_update_leave_a_hermes_home_as_it_was(self):
        """The real steps of deploy.py with the real source package, next to a Hermes home full of files. Only the steps
        that stop and start the service are stand-ins, because they ask launchd."""
        hermes = self.home / '.hermes'
        make_hermes_home(hermes)
        # A `hermes` command on the PATH that would leave a mark if anything ran it.
        launcher = self.home / 'bin/hermes'
        launcher.parent.mkdir()
        launcher.write_text(f'#!/bin/sh\ntouch {self.home}/hermes-ran\n')
        launcher.chmod(0o755)
        before = snapshot(hermes)

        class ServiceStandIns(deploy.Operations):
            calls = []

            def command(self, arguments, root, phase, **options):
                self.calls.append(phase)
                return super().command(arguments, root, phase, **options)

            def inspect(self, root, source=None):
                return {'mode': 'app', 'project': str(root), 'data_dir': str(root / '.runtime/host')}

            def drain(self, root, source=None):
                return {'drained': False}

            def stop(self, root, source=None):
                return self.inspect(root)

            def restore(self, root, state):
                pass

            def install(self, root):
                (root / '.runtime/host').mkdir(parents=True, exist_ok=True)
                return 'app'

            def health(self, root):
                return {'ok': True}

        files = remote.source_files()
        environment = {'HOME': str(self.home), 'HERMES_HOME': str(hermes), 'PATH': f'{launcher.parent}:/usr/bin:/bin',
                       'COCONUT_MODE': 'team', 'COCONUT_APP_MANAGED': '1',
                       'COCONUT_TEAM_HELPER': str(self.home / 'App/Contents/Helpers/team-helper')}
        with patch.dict(os.environ, environment), patch.object(deploy.platform, 'system', return_value='Darwin'):
            first = deploy.receive('Coconut', archive(files), home=self.home, operations=ServiceStandIns(team_only=True))
            second = deploy.receive('Coconut', archive(dict(files, **{'README.md': b'New code.\n'})), home=self.home,
                                    operations=ServiceStandIns(team_only=True))
        self.assertTrue(first['team_only'] and second['updated'])
        self.assertEqual(ServiceStandIns.calls, ['Team helper build', 'Team helper build'])
        self.assertEqual(snapshot(hermes), before)
        self.assertFalse((self.home / 'hermes-ran').exists())


class StartErrorTests(unittest.TestCase):
    """A service that refuses its data folder leaves the cause next to process.json (server.START_ERROR)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name).resolve() / 'data'
        self.data.mkdir()

    def test_a_refused_folder_leaves_its_cause_and_a_later_start_removes_it(self):
        from coconut import server
        with sqlite3.connect(self.data / 'coconut.sqlite3') as database:
            database.execute('CREATE TABLE devices (id TEXT PRIMARY KEY)')
            database.execute("INSERT INTO devices VALUES ('phone')")
        database.close()
        with patch.object(sys, 'stderr'), self.assertRaises(SystemExit):
            server.main(['--data-dir', str(self.data), '--team-only'])
        cause = json.loads((self.data / server.START_ERROR).read_text())
        self.assertEqual((cause['code'], cause['mode']), ('data_folder_mode', 'team'))
        self.assertEqual(cause['error'], WRONG_FOLDER[TEAM_MODE])
        self.assertEqual((self.data / server.START_ERROR).stat().st_mode & 0o777, 0o600)

        class Started:
            root = self.data

            def clear_service_drain(self):
                pass
        server.write_process_record(Started())
        self.assertFalse((self.data / server.START_ERROR).exists())

    def test_an_error_without_a_code_leaves_no_cause(self):
        from coconut import server
        server.write_start_error(self.data, ServiceError(409, 'Another Coconut service already owns this data '
                                                              'directory.'), True)
        self.assertFalse((self.data / server.START_ERROR).exists())


class RemoveAndSetUpTests(unittest.TestCase):
    """The uninstaller and the developer setup read the mode of the install (host.install_mode)."""

    def test_the_uninstaller_of_a_team_only_mac_edits_no_hermes(self):
        commands = []
        with patch.object(host, 'install_mode', return_value='team'), \
                patch.object(host, 'login_item_loaded', return_value=False), \
                patch.object(desktop, 'run', side_effect=lambda arguments, **options: commands.append(arguments[1:])), \
                patch.object(desktop.subprocess, 'run', side_effect=lambda arguments, **options: commands.append(arguments[1:])), \
                patch.object(desktop.deploy, 'lock_skills'), patch('builtins.input', return_value='DELETE'), \
                patch('builtins.print'):
            desktop.uninstall()
        self.assertEqual(commands, [['scripts/cleanup.py'], ['scripts/cleanup.py', '--yes']])

    def test_the_uninstaller_of_a_full_mac_removes_its_hermes_entries(self):
        commands = []
        with patch.object(host, 'install_mode', return_value='full'), \
                patch.object(desktop, 'run', side_effect=lambda arguments, **options: commands.append(arguments[1:])), \
                patch.object(desktop.subprocess, 'run', side_effect=lambda arguments, **options: commands.append(arguments[1:])), \
                patch.object(desktop.deploy, 'lock_skills'), patch('builtins.input', return_value='DELETE'), \
                patch('builtins.print'):
            desktop.uninstall()
        self.assertEqual(commands, [['scripts/cleanup.py'], ['scripts/hermes_skills.py', 'remove'],
                                    ['scripts/cleanup.py', '--yes']])

    def test_coconut_remove_runs_the_uninstaller_without_the_question(self):
        """`coconut remove` (Coconut.app --remove) runs `desktop.py uninstall --yes` on a Mac with no person."""
        commands = []
        with patch.object(host, 'install_mode', return_value='team'), \
                patch.object(host, 'login_item_loaded', return_value=False), \
                patch.object(desktop, 'run', side_effect=lambda arguments, **options: commands.append(arguments[1:])), \
                patch.object(desktop.deploy, 'lock_skills'), \
                patch('builtins.input', side_effect=AssertionError('The uninstaller asked a question.')), \
                patch('builtins.print'), patch.object(sys, 'argv', ['desktop.py', 'uninstall', '--yes']):
            desktop.main()
        self.assertEqual(commands, [['scripts/cleanup.py'], ['scripts/cleanup.py', '--yes']])

    def test_the_uninstaller_waits_until_the_login_item_is_off(self):
        with patch.object(host, 'install_mode', return_value='team'), \
                patch.object(host, 'login_item_loaded', return_value=True), patch.object(desktop, 'run') as run, \
                self.assertRaises(SystemExit) as stopped:
            desktop.uninstall()
        self.assertIn('Turn off Start at Login', str(stopped.exception))
        run.assert_not_called()

    def test_the_developer_setup_refuses_a_team_only_mac(self):
        with patch.object(host, 'install_mode', return_value='team'), \
                patch.object(setup_coconut, 'prerequisites') as prerequisites, \
                patch.object(sys, 'argv', ['setup_coconut.py']), patch('builtins.print'), \
                self.assertRaises(SystemExit) as stopped:
            setup_coconut.main()
        self.assertEqual(str(stopped.exception), 'This Mac runs Coconut for a team only. Remove Coconut first, then '
                                                 'set it up again.')
        prerequisites.assert_not_called()

    def test_the_mode_is_team_only_with_the_preference(self):
        def defaults(value, code=0):
            return lambda *arguments, **options: subprocess.CompletedProcess(arguments, code, stdout=value)
        with patch.object(host.subprocess, 'run', side_effect=defaults('team\n')):
            self.assertEqual(host.install_mode(), 'team')
        with patch.object(host.subprocess, 'run', side_effect=defaults('', 1)):
            self.assertEqual(host.install_mode(), 'full')
        with patch.object(host.subprocess, 'run', side_effect=OSError()):
            self.assertEqual(host.install_mode(), 'full')


if __name__ == '__main__':
    unittest.main()
