"""The idle exit of a gateway that a Hermes client started, with and without the `coconut` platform.

A Hermes chat or TUI client that finds no gateway starts `hermes gateway run --idle-exit`
(hermes_cli/gateway_runtime_start.py). Hermes stops that gateway when it has nothing to do for
`gateway.unmanaged_idle_exit_seconds` (gateway/run_idle_exit.py). The test sets a short time and starts the gateway as
such a client does.

Without the platform, the gateway exits. With the platform and an idle Coconut service, the gateway stays up: Hermes
holds an unmanaged gateway while a messaging platform is enabled (`_messaging_busy`), and a platform cannot leave that
check. The long poll of the adapter is not the cause. docs/architecture.md ("A gateway that a Hermes client started")
has the evidence and the upstream ask. When Hermes adds a signal for it, the second check fails: then set the signal in
the plugin and make the check expect the exit.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from hermes_env import needs_hermes, PYTHON, SOURCE

ROOT = Path(__file__).resolve().parents[2]
IDLE_SECONDS = 2
# The start of a gateway and its first idle readings.
EXIT_SECONDS = 120
# The gateway with the platform must stay up this much longer than the idle time.
HOLD_SECONDS = 10 * IDLE_SECONDS


@needs_hermes
class NativeGatewayIdleExitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='coconut-idle-exit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def home(self, name, platforms=None):
        home = self.root / name
        home.mkdir()
        # No model call happens. The address only completes the config.
        (home / 'config.yaml').write_text(json.dumps({
            'model': {'default': 'stub-model', 'provider': 'custom', 'api_key': 'stub-key', 'context_length': 131072,
                      'base_url': 'http://127.0.0.1:9/v1'},
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

    def test_a_client_started_gateway_exits_when_idle_without_the_coconut_platform(self):
        home = self.home('plain')
        process = self.start(home)
        try:
            process.wait(EXIT_SECONDS)
        except subprocess.TimeoutExpired:
            self.fail('The gateway did not reach its idle exit.\n' + self.diagnostics(home))
        state = json.loads((home / 'gateway_state.json').read_text())
        self.assertTrue(str(state.get('exit_reason')).startswith('auto-started gateway idle'), self.diagnostics(home))

    def test_the_coconut_platform_keeps_a_client_started_gateway_up_while_coconut_is_idle(self):
        from coconut.providers import FixtureProvider
        from coconut.server import APIServer
        from coconut.service import Service
        data = self.root / 'data'
        # The adapter reads the bridge file of the service from the platform's settings. Setup's edit keeps them.
        home = self.home('coconut', {'coconut': {'extra': {'bridge': str(data / 'gateway/bridge.json')}}})
        # Setup's edit, with the config writer of this Hermes.
        edit = subprocess.run([PYTHON, '-I', '-B', str(ROOT / 'backend/coconut/skill_dirs.py'), 'add', SOURCE,
                               str(ROOT / 'skills'), str(home)], env=self.environment(home), capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(edit.returncode, 0, edit.stdout + edit.stderr)
        self.assertIn('coconut gateway platform added', edit.stdout)

        class Provider(FixtureProvider):
            python, hermes_root = PYTHON, SOURCE

        # The default chat runtime, so the service starts no gateway of its own and offers no turn.
        service = Service(data, Provider(data), start_workers=False, hermes_home=home)
        server = APIServer(('127.0.0.1', 0), service)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
        try:
            service.gateway_bridge.publish(f'http://127.0.0.1:{server.server_address[1]}')
            self.assertEqual(service.gateway_bridge.address_file.resolve(), (data / 'gateway/bridge.json').resolve())
            process = self.start(home)
            deadline = time.monotonic() + EXIT_SECONDS
            while not service.gateway_bridge.connected(home) and time.monotonic() < deadline:
                self.assertIsNone(process.poll(), self.diagnostics(home))
                time.sleep(.5)
            self.assertTrue(service.gateway_bridge.connected(home), self.diagnostics(home))
            time.sleep(HOLD_SECONDS)
            self.assertIsNone(process.poll(), 'Hermes now stops a client-started gateway that has the coconut '
                              'platform. See the docstring of this test.\n' + self.diagnostics(home))
            self.assertEqual(service.gateway_bridge.turns, {})
        finally:
            service.close()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
