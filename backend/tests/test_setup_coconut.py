import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import setup_coconut


def completed(arguments, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)


class SetupCoconutTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name) / "host"
        self.data.mkdir()
        self.use(patch.object(setup_coconut, "DATA", self.data))
        self.commands = []
        self.use(patch.object(setup_coconut, "run", self.record))
        self.use(contextlib.redirect_stdout(io.StringIO()))

    def use(self, manager):
        result = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return result

    def record(self, arguments, label, *, quiet=True):
        self.commands.append([Path(part).name if part.endswith(".py") else part for part in arguments[1:]]
                             if arguments[0] == sys.executable else arguments)
        return completed(arguments, stdout=self.output if "--share" in arguments else "")

    def prerequisites(self, *, platform="darwin", tools=0):
        ran = []

        def fake_run(arguments, **kwargs):
            ran.append(arguments[0])
            return completed(arguments, tools)
        with patch.object(setup_coconut.sys, "platform", platform), patch.object(setup_coconut.subprocess, "run", fake_run):
            setup_coconut.prerequisites()
        # The phone reaches the Mac through the relay, so setup needs no VPN.
        self.assertEqual(ran, ["/usr/bin/xcode-select"])

    def test_prerequisites_need_only_macos_and_the_command_line_tools(self):
        self.prerequisites()

    def test_each_missing_prerequisite_has_an_actionable_message(self):
        for options, message in [(dict(platform="linux"), "runs on macOS"), (dict(tools=2), "xcode-select --install")]:
            with self.subTest(options=options), self.assertRaises(SystemExit) as raised:
                self.prerequisites(**options)
            self.assertIn(message, str(raised.exception))

    def use_hermes(self, status, warning=None):
        runtime = SimpleNamespace(status=Mock(return_value=status), untested_warning=Mock(return_value=warning))
        with patch.object(setup_coconut.host, "hermes_runtime", return_value=runtime), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            setup_coconut.use_hermes()
        runtime.status.assert_called_once_with(setup_coconut.DATA, save=False)
        return output.getvalue()

    def test_use_hermes_names_the_hermes_that_the_service_finds(self):
        # The service finds the same Hermes at each start, so setup passes nothing to it. See hermes_runtime.
        status = {"status": "ok", "version": "0.9.0", "reason": None, "root": "/hermes", "python": "/hermes/venv/bin/python"}
        self.assertIn("Coconut uses Hermes 0.9.0 in /hermes.", self.use_hermes(status))
        self.assertEqual(self.commands, [])

    def test_use_hermes_warns_about_an_untested_version(self):
        status = {"status": "ok", "version": "0.99.0", "reason": None, "root": "/hermes", "python": "/hermes/venv/bin/python"}
        output = self.use_hermes(status, warning="Coconut has not been tested with Hermes 0.99.0.")
        self.assertIn("Coconut uses Hermes 0.99.0 in /hermes.\nCoconut has not been tested with Hermes 0.99.0.\n", output)

    def test_use_hermes_stops_with_the_reason(self):
        status = {"status": "missing", "version": None, "reason": "Coconut found no Hermes on this Mac."}
        with self.assertRaises(SystemExit) as raised:
            self.use_hermes(status)
        self.assertIn("Coconut found no Hermes on this Mac. Then run this command again.", str(raised.exception))

    def test_prepares_notifications(self):
        setup_coconut.prepare_notifications()
        self.assertEqual(self.commands, [["push.py", "--rebuild"]])

    def service(self, *, plist_exists, loaded, process=None, health=None):
        plist = Path(self.directory.name) / "local.coconut.agent.plist"
        if plist_exists:
            plist.write_text("plist")
        checks = []

        def check(operations, root):
            checks.append(root)
            if health:
                raise health

        with patch.object(setup_coconut.host, "owned_agent", return_value=(plist, loaded)), \
                patch.object(setup_coconut.host, "owned_process", return_value=process), \
                patch.object(setup_coconut.deploy.Operations, "health", check):
            setup_coconut.start_service()
        self.assertEqual(checks, [setup_coconut.ROOT])
        return plist

    def test_a_running_login_service_only_gets_a_health_check(self):
        self.service(plist_exists=True, loaded=True)
        self.assertEqual(self.commands, [])

    def test_installs_the_login_service_when_it_is_missing(self):
        self.service(plist_exists=False, loaded=False)
        self.assertEqual(self.commands, [["host.py", "install-service"]])

    def test_replaces_a_manual_process_with_the_login_service(self):
        self.service(plist_exists=False, loaded=False, process=(42, {}))
        self.assertEqual(self.commands, [["host.py", "stop"], ["host.py", "install-service"]])

    def test_loads_an_existing_login_service(self):
        plist = self.service(plist_exists=True, loaded=False)
        self.assertEqual(self.commands, [["launchctl", "bootstrap", f"gui/{setup_coconut.os.getuid()}", str(plist)]])

    def test_a_failed_health_check_names_the_log(self):
        with self.assertRaises(SystemExit) as raised:
            self.service(plist_exists=False, loaded=False, health=setup_coconut.deploy.DeployError("No answer."))
        self.assertIn("No answer.", str(raised.exception))
        self.assertIn("logs/error.log", str(raised.exception))

    def test_a_running_service_without_the_tunnel_listener_restarts(self):
        # The health check uses the tunnel listener, which a service from before that listener does not have.
        with patch.object(setup_coconut, "ensure_tunnel_listener") as ensure:
            self.service(plist_exists=True, loaded=True, health=setup_coconut.deploy.DeployError("No answer."))
        ensure.assert_called_once_with()

    def test_share_prints_the_link_and_the_image_last(self):
        self.output = ("Setup link: coconut://pair?setup=abc\nQR image: /tmp/iphone-setup.png\n"
                       "On the iPhone, tap the link.\nThis setup code expires after five minutes.\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            setup_coconut.share()
        lines = output.getvalue().splitlines()
        self.assertEqual(self.commands, [["pairing.py", "--share"]])
        self.assertNotIn("Tailscale", output.getvalue())
        self.assertIn("ten minutes", output.getvalue())
        self.assertEqual(lines[-2:], ["Setup link: coconut://pair?setup=abc", "QR image: /tmp/iphone-setup.png"])

    def test_start_tunnel_installs_the_sidecar_in_the_data_folder(self):
        setup_coconut.start_tunnel()
        self.assertEqual(self.commands, [["tunnel.py", "install", "--data-dir", str(self.data)]])

    def test_a_service_without_the_tunnel_listener_drains_and_restarts(self):
        answers = iter([False, False, True])
        drain = Mock(side_effect=lambda: self.commands.append(["drain"]) or True)
        with patch.object(setup_coconut, "tunnel_listener_ready", lambda: next(answers)), \
                patch.object(setup_coconut.host, "drain_for_update", drain), \
                patch.object(setup_coconut.time, "sleep"):
            setup_coconut.ensure_tunnel_listener()
        self.assertEqual(self.commands, [["drain"], ["host.py", "restart"]])

    def test_a_running_listener_needs_no_drain_or_restart(self):
        drain = Mock()
        with patch.object(setup_coconut, "tunnel_listener_ready", return_value=True), \
                patch.object(setup_coconut.host, "drain_for_update", drain):
            setup_coconut.ensure_tunnel_listener()
        drain.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_setup_stops_when_the_listener_stays_away(self):
        clock = iter(range(0, 1000, 60))
        with patch.object(setup_coconut, "tunnel_listener_ready", return_value=False), \
                patch.object(setup_coconut.host, "drain_for_update", return_value=False), \
                patch.object(setup_coconut.time, "sleep"), \
                patch.object(setup_coconut.time, "monotonic", lambda: next(clock)), \
                self.assertRaises(SystemExit) as raised:
            setup_coconut.ensure_tunnel_listener()
        self.assertIn("127.0.0.1:8961", str(raised.exception))
        self.assertEqual(self.commands, [["host.py", "restart"]])

    def test_tunnel_listener_check_reads_the_health_route(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(setup_coconut.urllib.request, "urlopen", return_value=response) as opened:
            self.assertTrue(setup_coconut.tunnel_listener_ready())
        self.assertEqual(opened.call_args.args[0], "http://127.0.0.1:8961/health")
        with patch.object(setup_coconut.urllib.request, "urlopen", side_effect=ConnectionRefusedError()):
            self.assertFalse(setup_coconut.tunnel_listener_ready())

    def test_share_fails_without_a_link(self):
        self.output = "Open Coconut on the iPhone, then tap Scan.\n"
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            setup_coconut.share()

    def test_main_runs_the_steps_in_order(self):
        calls = []
        names = ["prerequisites", "use_hermes", "prepare_notifications", "start_service", "start_tunnel", "share"]
        with contextlib.ExitStack() as stack:
            for name in names:
                stack.enter_context(patch.object(setup_coconut, name,
                                                 lambda *args, name=name: calls.append(name)))
            setup_coconut.main()
        self.assertEqual(calls, names)


class RunTests(unittest.TestCase):
    def test_a_failed_quiet_command_shows_its_output(self):
        with patch.object(setup_coconut.subprocess, "run", return_value=completed([], 3, "details\n", "error\n")), \
                contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as raised:
            setup_coconut.run(["host.py"], "The host setup")
        self.assertEqual(errors.getvalue(), "details\nerror\n")
        self.assertIn("The host setup failed with exit code 3", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
