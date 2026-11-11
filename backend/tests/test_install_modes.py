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
import sys
import unittest
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('coconut_install_modes_deploy', SCRIPTS / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)

from test_deploy_tools import FakeOperations, archive, package_files  # noqa: E402

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
        import tempfile
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def test_a_full_setup_and_update_run_the_steps_of_the_fixture(self):
        self.assertEqual(full_plan(self.home), json.loads(FIXTURE.read_text()))


if __name__ == '__main__':
    unittest.main()
