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
import hermes_skills  # noqa: E402
import host  # noqa: E402
import remote  # noqa: E402
import setup_coconut  # noqa: E402

from coconut.service_base import HERMES_REFUSAL, MODE_MARK  # noqa: E402
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
    # The checks of preflight run on macOS only. The CI runner of these tests runs Linux.
    with patch.dict(os.environ, {deploy.APP_MANAGED: '1'}), patch.object(deploy.platform, 'system',
                                                                          return_value='Darwin'):
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
        with recorded_commands() as commands, patch.object(deploy.platform, 'system', return_value='Darwin'):
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


def mark_team_folder(data: Path) -> None:
    """Make the store of a team-only service, as team_only.TeamOnlyService does at its first start."""
    data.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(data / 'coconut.sqlite3') as database:
        database.executescript(MODE_MARK)
    database.close()


def listing(folder: Path) -> dict:
    return {str(path.relative_to(folder)): path.stat().st_mtime_ns for path in folder.rglob('*')}


class TeamFolderGuardTests(unittest.TestCase):
    """A team-only data folder without the preference of the app gets no Hermes step (service_base.team_folder)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.hermes = self.home / '.hermes'
        make_hermes_home(self.hermes)
        self.before = snapshot(self.hermes)

    def tearDown(self):
        self.assertEqual(snapshot(self.hermes), self.before)

    def test_the_check_reads_the_store_and_writes_nothing(self):
        data = self.home / 'data'
        self.assertIsNone(host.team_folder_refusal(data))
        mark_team_folder(data)
        files = listing(data)
        self.assertEqual(host.team_folder_refusal(data), HERMES_REFUSAL)
        self.assertEqual(listing(data), files)
        full = self.home / 'full'
        full.mkdir()
        with sqlite3.connect(full / 'coconut.sqlite3') as database:
            database.execute('CREATE TABLE devices (id TEXT PRIMARY KEY)')
            database.execute("INSERT INTO devices VALUES ('phone')")
        database.close()
        self.assertIsNone(host.team_folder_refusal(full))

    def test_a_full_install_source_or_update_stops_before_any_step(self):
        # An install of the app, then a team-only service, then a lost preference: the app runs a full update.
        files = package_files(**{'backend/coconut/service_base.py': (SCRIPTS.parent / 'backend/coconut/service_base.py')
                                 .read_bytes()})
        deploy.receive('Coconut', archive(files), home=self.home, operations=FakeOperations(), team_only=False)
        data = self.home / 'Coconut/.runtime/host'
        mark_team_folder(data)
        source = listing(self.home / 'Coconut')
        for update in (False, True):
            operations = FakeOperations(mode='app')
            changed = dict(files, **{'README.md': b'New code.\n'}) if update else files
            with self.assertRaises(deploy.DeployError) as refused:
                deploy.receive('Coconut', archive(changed), home=self.home, operations=operations, team_only=False)
            self.assertEqual(str(refused.exception), HERMES_REFUSAL)
            self.assertEqual(operations.calls, [])
        self.assertEqual(listing(self.home / 'Coconut'), source)
        # The team-only path of the same folder runs, without a Hermes step.
        operations = FakeOperations(mode='app')
        deploy.receive('Coconut', archive(dict(files, **{'README.md': b'New code.\n'})), home=self.home,
                       operations=operations, team_only=True)
        self.assertNotIn('hermes', step_names(operations))

    def test_the_hermes_skill_step_and_the_developer_setup_stop(self):
        data = self.home / 'data'
        mark_team_folder(data)
        with patch.object(host, 'DEFAULT_DATA', data), \
                patch.object(host, 'hermes_runtime', side_effect=AssertionError('Coconut searched for Hermes.')), \
                patch.object(sys, 'argv', ['hermes_skills.py', 'add']), patch('builtins.print') as printed:
            self.assertEqual(hermes_skills.main(), 1)
        printed.assert_called_once_with(HERMES_REFUSAL)
        with patch.object(host, 'DEFAULT_DATA', data), patch.object(host, 'install_mode', return_value='full'), \
                patch.object(setup_coconut, 'prerequisites') as prerequisites, \
                patch.object(sys, 'argv', ['setup_coconut.py']), patch('builtins.print'), \
                self.assertRaises(SystemExit) as stopped:
            setup_coconut.main()
        self.assertEqual(str(stopped.exception), HERMES_REFUSAL)
        prerequisites.assert_not_called()


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

    def test_the_app_runs_the_uninstaller_without_the_question(self):
        """Coconut.app --remove runs `desktop.py uninstall --yes` on a Mac with no person (AppModel.uninstall)."""
        commands = []
        with patch.object(host, 'install_mode', return_value='team'), \
                patch.object(host, 'login_item_loaded', return_value=False), \
                patch.object(desktop, 'run', side_effect=lambda arguments, **options: commands.append(arguments[1:])), \
                patch.object(desktop.deploy, 'lock_skills'), \
                patch('builtins.input', side_effect=AssertionError('The uninstaller asked a question.')), \
                patch('builtins.print'), patch.object(sys, 'argv', ['desktop.py', 'uninstall', '--yes']):
            desktop.main()
        self.assertEqual(commands, [['scripts/cleanup.py'], ['scripts/cleanup.py', '--yes']])

    def test_the_docs_give_the_removal_in_steps_and_the_skill_has_none(self):
        repository = TESTS.parents[1]
        skill = (repository / 'skills/coconut/SKILL.md').read_text()
        docs = (repository / 'docs/install-and-update.md').read_text()
        self.assertNotIn('coconut remove', skill + docs)
        section = docs[docs.index('### Remove', docs.index('## A Mac without a person')):docs.index('## Releases from CI')]
        for step in ('coconut team leave --confirm', 'pkill -x Coconut', 'open -g /Applications/Coconut.app --args --remove',
                     'Service: not installed'):
            self.assertIn(step, section)
        # An Apple event can ask for the Automation permission in a dialog that nobody sees.
        self.assertNotIn('osascript', section)

    def test_the_uninstaller_waits_until_each_team_chat_is_disconnected(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            self.assertEqual(host.connected_chats(data), [])
            with sqlite3.connect(data / 'coconut.sqlite3') as db:
                db.execute('CREATE TABLE bots (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
                db.executemany('INSERT INTO bots VALUES (?, ?)', [
                    ('a', json.dumps({'name': 'Reporter', 'chat': True})), ('b', json.dumps({'name': 'Planner'})),
                    ('c', json.dumps({'name': 'Helper', 'chat': False})),
                    ('d', json.dumps({'name': 'Writer', 'chat': False, 'plugin': True}))])
            db.close()
            # A clean-up that did not finish keeps the guard.
            self.assertEqual(host.connected_chats(data), ['Reporter', 'Writer'])
        with patch.object(host, 'install_mode', return_value='team'), \
                patch.object(host, 'connected_chats', return_value=['Reporter']), \
                patch.object(host, 'login_item_loaded', return_value=False), patch.object(desktop, 'run') as run, \
                self.assertRaises(SystemExit) as stopped:
            desktop.uninstall(confirmed=True)
        self.assertEqual(str(stopped.exception), 'The Hermes home of Reporter has the team chat of Coconut. For each '
                                                 'bot, run coconut team disconnect-chat <bot>, then remove Coconut.')
        run.assert_not_called()

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
