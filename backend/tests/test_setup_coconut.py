import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import setup_coconut


ADDRESS = "https://mac.tail1234.ts.net:8960"


def completed(arguments, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)


def tailscale_status(state="Running", name="mac.tail1234.ts.net.", addresses=("100.64.0.5", "fd7a::5")):
    return json.dumps({"BackendState": state, "Self": {"DNSName": name, "TailscaleIPs": list(addresses)}})


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

    def save(self, address):
        (self.data / "connection.json").write_text(json.dumps({"address": address}))
        (self.data / "server.crt").write_text("certificate")

    def prerequisites(self, *, platform="darwin", tools=0, status=None, which="/usr/local/bin/tailscale"):
        def fake_run(arguments, **kwargs):
            if arguments[0] == "/usr/bin/xcode-select":
                return completed(arguments, tools)
            return completed(arguments, stdout=status if status is not None else tailscale_status())
        with patch.object(setup_coconut.sys, "platform", platform), \
                patch.object(setup_coconut.subprocess, "run", fake_run), \
                patch.object(setup_coconut.shutil, "which", return_value=which), \
                patch.object(setup_coconut, "TAILSCALE_APP", Path(self.directory.name) / "missing"):
            return setup_coconut.prerequisites()

    def test_prerequisites_use_the_tailscale_name_and_default_port(self):
        self.assertEqual(self.prerequisites(), ADDRESS)

    def test_prerequisites_keep_a_saved_port_for_the_same_name(self):
        self.save("https://mac.tail1234.ts.net:9443")
        self.assertEqual(self.prerequisites(), "https://mac.tail1234.ts.net:9443")
        self.save("https://old-name.tail1234.ts.net:9443")
        self.assertEqual(self.prerequisites(), ADDRESS)

    def test_prerequisites_use_the_ipv4_address_without_magicdns(self):
        self.assertEqual(self.prerequisites(status=tailscale_status(name="")), "https://100.64.0.5:8960")

    def test_each_missing_prerequisite_has_an_actionable_message(self):
        cases = [
            (dict(platform="linux"), "runs on macOS"),
            (dict(tools=2), "xcode-select --install"),
            (dict(which=None), "tailscale.com/download"),
            (dict(status=tailscale_status(state="NeedsLogin")), "Open Tailscale on this Mac, sign in"),
            (dict(status="not json"), "Open Tailscale on this Mac, sign in"),
            (dict(status=tailscale_status(name="", addresses=("fd7a::5",))), "Reconnect Tailscale"),
        ]
        for options, message in cases:
            with self.subTest(options=options), self.assertRaises(SystemExit) as raised:
                self.prerequisites(**options)
            self.assertIn(message, str(raised.exception))

    def test_installs_hermes_only_when_missing(self):
        with patch.object(setup_coconut, "hermes_installed", return_value=True):
            setup_coconut.install_hermes()
        self.assertEqual(self.commands, [])
        with patch.object(setup_coconut, "hermes_installed", return_value=False):
            setup_coconut.install_hermes()
        self.assertEqual(self.commands, [["hermes.py", "install"]])

    def test_configure_skips_a_saved_address(self):
        self.save(ADDRESS)
        with patch.object(setup_coconut, "service_running", side_effect=AssertionError):
            setup_coconut.configure(ADDRESS)
        self.assertEqual(self.commands, [])

    def test_configure_saves_a_new_address(self):
        with patch.object(setup_coconut, "service_running", return_value=False):
            setup_coconut.configure(ADDRESS)
        self.assertEqual(self.commands, [["host.py", "setup", "--address", ADDRESS]])

    def test_configure_stops_a_running_service_before_it_changes_the_address(self):
        self.save("https://old-name.tail1234.ts.net:8960")
        with patch.object(setup_coconut, "service_running", return_value=True):
            setup_coconut.configure(ADDRESS)
        self.assertEqual(self.commands, [["host.py", "stop"], ["host.py", "setup", "--address", ADDRESS]])

    def service(self, *, plist_exists, loaded, process=None, health=None):
        plist = Path(self.directory.name) / "local.coconut.agent.plist"
        if plist_exists:
            plist.write_text("plist")
        checks = []

        def check(operations, root, address):
            checks.append(address)
            if health:
                raise health

        with patch.object(setup_coconut.host, "owned_agent", return_value=(plist, loaded)), \
                patch.object(setup_coconut.host, "owned_process", return_value=process), \
                patch.object(setup_coconut.deploy.Operations, "health", check):
            setup_coconut.start_service(ADDRESS)
        self.assertEqual(checks, [ADDRESS])
        return plist

    def test_a_running_login_service_only_gets_a_health_check(self):
        self.service(plist_exists=True, loaded=True)
        self.assertEqual(self.commands, [])

    def test_installs_the_login_service_when_it_is_missing(self):
        self.service(plist_exists=False, loaded=False)
        self.assertEqual(self.commands, [["host.py", "install-service", "--address", ADDRESS]])

    def test_replaces_a_manual_process_with_the_login_service(self):
        self.service(plist_exists=False, loaded=False, process=(42, {}))
        self.assertEqual(self.commands, [["host.py", "stop"], ["host.py", "install-service", "--address", ADDRESS]])

    def test_loads_an_existing_login_service(self):
        plist = self.service(plist_exists=True, loaded=False)
        self.assertEqual(self.commands, [["launchctl", "bootstrap", f"gui/{setup_coconut.os.getuid()}", str(plist)]])

    def test_a_failed_health_check_names_the_log(self):
        with self.assertRaises(SystemExit) as raised:
            self.service(plist_exists=True, loaded=True, health=setup_coconut.deploy.DeployError("No answer."))
        self.assertIn("No answer.", str(raised.exception))
        self.assertIn("logs/error.log", str(raised.exception))

    def test_share_prints_the_link_and_the_image_last(self):
        self.output = ("Setup link: coconut://pair?setup=abc\nQR image: /tmp/iphone-setup.png\n"
                       "On the iPhone, tap the link.\nThis setup code expires after five minutes.\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            setup_coconut.share()
        lines = output.getvalue().splitlines()
        self.assertEqual(self.commands, [["pairing.py", "--share"]])
        self.assertIn("same tailnet", output.getvalue())
        self.assertEqual(lines[-2:], ["Setup link: coconut://pair?setup=abc", "QR image: /tmp/iphone-setup.png"])

    def test_share_fails_without_a_link(self):
        self.output = "Open Coconut on the iPhone, then tap Scan.\n"
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            setup_coconut.share()

    def test_main_runs_the_steps_in_order(self):
        calls = []
        names = ["prerequisites", "install_hermes", "configure", "start_service", "share"]
        with contextlib.ExitStack() as stack:
            for name in names:
                stack.enter_context(patch.object(setup_coconut, name,
                                                 lambda *args, name=name: calls.append(name) or ADDRESS))
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
