"""The idle exit of a gateway that a Hermes client started, with and without the `coconut` platform.

A Hermes chat or TUI client that finds no gateway starts `hermes gateway run --idle-exit`
(hermes_cli/gateway_runtime_start.py). Hermes stops that gateway when it has nothing to do for
`gateway.unmanaged_idle_exit_seconds` (gateway/run_idle_exit.py). The test sets a short time and starts the gateway as
such a client does.

Without the platform, the gateway exits. The coconut adapter sets `keeps_gateway_up = False`
(NousResearch/hermes-agent#146209). On a Hermes with that attribute, a gateway with the platform and an idle Coconut
service also exits, and a running Coconut turn holds it up until the turn ends. On a Hermes without the attribute, the
gateway with the platform stays up: Hermes holds an unmanaged gateway while a messaging platform is enabled
(`_messaging_busy`). docs/architecture.md ("A gateway that a Hermes client started") has the evidence.

The Hermes home has no cron jobs. A runnable cron job keeps such a gateway up by design.
"""
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from hermes_env import needs_hermes, PYTHON, SOURCE
from test_native_gateway_chat import Model

ROOT = Path(__file__).resolve().parents[2]
IDLE_SECONDS = 2
# The start of a gateway and its first idle readings.
EXIT_SECONDS = 120
# The gateway with the platform must stay up this much longer than the idle time.
HOLD_SECONDS = 10 * IDLE_SECONDS


class HeldModel(Model):
    """Answer as Model does, but only after the test releases the call."""
    asked = threading.Event()
    release = threading.Event()

    def do_POST(self):
        HeldModel.asked.set()
        HeldModel.release.wait(EXIT_SECONDS)
        super().do_POST()


def opts_out() -> bool:
    """Whether the installed Hermes reads `keeps_gateway_up` (NousResearch/hermes-agent#146209)."""
    check = subprocess.run([PYTHON, '-c', 'from gateway.platforms.base import BasePlatformAdapter as B; '
                            'print(hasattr(B, "keeps_gateway_up"))'], cwd=SOURCE, capture_output=True, text=True,
                           timeout=120, env={key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ})
    if check.returncode:
        raise RuntimeError(check.stderr[-4000:])
    return check.stdout.strip() == 'True'


@needs_hermes
class NativeGatewayIdleExitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.opts_out = opts_out()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='coconut-idle-exit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def home(self, name, platforms=None, model='http://127.0.0.1:9/v1'):
        home = self.root / name
        home.mkdir()
        # Without a model stand-in, no model call happens. The address only completes the config.
        (home / 'config.yaml').write_text(json.dumps({
            'model': {'default': 'stub-model', 'provider': 'custom', 'api_key': 'stub-key', 'context_length': 131072,
                      'base_url': model},
            'gateway': {'unmanaged_idle_exit_seconds': IDLE_SECONDS, **({'platforms': platforms} if platforms else {})}}))
        return home

    def environment(self, home):
        # Only these variables. INVOCATION_ID or a launchd label would make Hermes see a supervisor, and a supervised
        # gateway never arms its idle exit (gateway/control_socket.py, _detect_supervisor).
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LANG') if key in os.environ}
        environment.update(HERMES_HOME=str(home), HERMES_GATEWAY_DETACHED='1', HERMES_DISABLE_LAZY_INSTALLS='1',
                           HERMES_DISABLE_TELEMETRY='1', PYTHONIOENCODING='utf-8')
        return environment

    def start(self, home):
        """Start the gateway with the command of spawn_unmanaged_gateway."""
        output = (home / 'gateway-stdio.log').open('ab')
        self.addCleanup(output.close)
        process = subprocess.Popen([PYTHON, '-m', 'hermes_cli.main', 'gateway', 'run', '--quiet', '--idle-exit'],
                                   cwd=SOURCE, env=self.environment(home), stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=output)

        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(10)
        self.addCleanup(stop)
        return process

    def diagnostics(self, home):
        parts = []
        for path in [home / 'gateway-stdio.log', home / 'gateway_state.json', *sorted((home / 'logs').glob('*.log'))]:
            if path.exists():
                parts += [path.name + ':', path.read_text(errors='replace')[-6000:]]
        return '\n'.join(parts)

    def assert_idle_exit(self, process, home):
        try:
            process.wait(EXIT_SECONDS)
        except subprocess.TimeoutExpired:
            self.fail('The gateway did not reach its idle exit.\n' + self.diagnostics(home))
        state = json.loads((home / 'gateway_state.json').read_text())
        self.assertTrue(str(state.get('exit_reason')).startswith('auto-started gateway idle'), self.diagnostics(home))

    def coconut(self, model='http://127.0.0.1:9/v1'):
        """A Hermes home with the coconut platform, and a Coconut service for it. Return the home and the service."""
        from coconut.providers import FixtureProvider
        from coconut.server import APIServer
        from coconut.service import Service
        data = self.root / 'data'
        # The adapter reads the bridge file of the service from the platform's settings. Setup's edit keeps them.
        home = self.home('coconut', {'coconut': {'extra': {'bridge': str(data / 'gateway/bridge.json')}}}, model)
        # Setup's edit, with the config writer of this Hermes.
        edit = subprocess.run([PYTHON, '-I', '-B', str(ROOT / 'backend/coconut/skill_dirs.py'), 'add', SOURCE,
                               str(ROOT / 'skills'), str(home)], env=self.environment(home), capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(edit.returncode, 0, edit.stdout + edit.stderr)
        self.assertIn('coconut gateway platform added', edit.stdout)

        class Provider(FixtureProvider):
            python, hermes_root = PYTHON, SOURCE

        # The default chat runtime, so the service starts no gateway of its own and offers no turn by itself.
        service = Service(data, Provider(data), start_workers=False, hermes_home=home)
        server = APIServer(('127.0.0.1', 0), service)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()

        def close():
            service.close()
            server.shutdown()
            server.server_close()
        self.addCleanup(close)
        service.gateway_bridge.publish(f'http://127.0.0.1:{server.server_address[1]}')
        self.assertEqual(service.gateway_bridge.address_file.resolve(), (data / 'gateway/bridge.json').resolve())
        # A runnable cron job keeps an auto-started gateway up by design.
        jobs = home / 'cron/jobs.json'
        self.assertFalse(jobs.exists() and json.loads(jobs.read_text()).get('jobs'), jobs)
        return home, service

    def wait_connected(self, process, home, service):
        deadline = time.monotonic() + EXIT_SECONDS
        while not service.gateway_bridge.connected(home) and time.monotonic() < deadline:
            self.assertIsNone(process.poll(), self.diagnostics(home))
            time.sleep(.1)
        self.assertTrue(service.gateway_bridge.connected(home), self.diagnostics(home))

    def test_a_client_started_gateway_exits_when_idle_without_the_coconut_platform(self):
        home = self.home('plain')
        self.assert_idle_exit(self.start(home), home)

    def test_an_idle_coconut_platform_holds_a_client_started_gateway_only_on_a_hermes_without_the_opt_out(self):
        home, service = self.coconut()
        process = self.start(home)
        self.wait_connected(process, home, service)
        if self.opts_out:
            self.assert_idle_exit(process, home)
        else:
            time.sleep(HOLD_SECONDS)
            self.assertIsNone(process.poll(), 'This Hermes stops a client-started gateway that has the coconut '
                              'platform, but it has no keeps_gateway_up. See the docstring of this test.\n'
                              + self.diagnostics(home))
        self.assertEqual(service.gateway_bridge.turns, {})

    def test_a_running_coconut_turn_holds_a_client_started_gateway(self):
        # CI requires a plain OK for this module, so the check runs on each Hermes. Without keeps_gateway_up, the
        # platform also holds the gateway, and only the exit after the turn differs.
        model = ThreadingHTTPServer(('127.0.0.1', 0), HeldModel)
        threading.Thread(target=model.serve_forever, daemon=True).start()
        self.addCleanup(model.server_close)
        self.addCleanup(model.shutdown)
        HeldModel.asked.clear()
        HeldModel.release.clear()
        self.addCleanup(HeldModel.release.set)
        home, service = self.coconut(f'http://127.0.0.1:{model.server_port}/v1')
        events = []
        # The turn waits in the bridge, so the first poll of the adapter takes it.
        turn = threading.Thread(target=service.gateway_bridge.run_turn,
                                args=('idle-exit-task', home, 'idle-exit-chat', 'Say hello.',
                                      lambda kind, data: events.append(kind)), daemon=True)
        turn.start()
        process = self.start(home)
        self.assertTrue(HeldModel.asked.wait(EXIT_SECONDS), self.diagnostics(home))
        time.sleep(HOLD_SECONDS)
        self.assertIsNone(process.poll(), 'The gateway stopped during a Coconut turn.\n' + self.diagnostics(home))
        HeldModel.release.set()
        turn.join(EXIT_SECONDS)
        self.assertIn('completed', events, self.diagnostics(home))
        if self.opts_out:
            self.assert_idle_exit(process, home)


if __name__ == '__main__':
    unittest.main()
