import contextlib
import hashlib
import io
import plistlib
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

    def app(self, folder, bundle_id="com.gumroad.coconut.mac"):
        app = folder / "Coconut.app"
        (app / "Contents").mkdir(parents=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
        return app

    def folders(self):
        base = Path(self.directory.name)
        system, personal = base / "Applications", base / "home/Applications"
        system.mkdir()
        return self.use(patch.object(setup_coconut, "app_folders", return_value=[system, personal])), system, personal

    def test_the_app_is_found_in_either_applications_folder(self):
        _, system, personal = self.folders()
        self.assertIsNone(setup_coconut.find_app())
        self.app(system, bundle_id="com.example.other")
        self.assertIsNone(setup_coconut.find_app())
        personal.mkdir(parents=True)
        app = self.app(personal)
        self.assertEqual(setup_coconut.find_app(), app)

    def feed(self, *items):
        return setup_coconut.mac_feed.render([setup_coconut.mac_feed.Item(
            version="0.1.0", build=build, channel=channel, file=f"Coconut-0.1.0-{build}-{channel}-0123456789abcdef.zip",
            length=3, signature="sig", published="Mon, 26 Oct 2026 12:00:00 +0000") for build, channel in items])

    def test_the_stable_channel_takes_the_nightly_build_before_the_first_stable_release(self):
        choose = lambda feed, channel: setup_coconut.choose_item(feed, channel, "26.6")
        item, channel = choose(self.feed((5, "stable"), (7, "nightly"), (6, "nightly")), "stable")
        self.assertEqual((item.build, channel), (5, "stable"))
        item, channel = choose(self.feed((7, "nightly"), (6, "nightly")), "stable")
        self.assertEqual((item.build, channel), (7, "nightly"))
        item, channel = choose(self.feed((5, "stable"), (7, "nightly")), "nightly")
        self.assertEqual((item.build, channel), (7, "nightly"))
        with self.assertRaisesRegex(SystemExit, "has no Coconut.app yet"):
            choose(None, "stable")

    def test_an_older_macos_gets_no_app_that_cannot_open(self):
        with self.assertRaisesRegex(SystemExit, "needs macOS 14.0 or later. This Mac has macOS 13.6"):
            setup_coconut.choose_item(self.feed((7, "nightly")), "stable", "13.6")
        self.assertEqual(setup_coconut.choose_item(self.feed((7, "nightly")), "stable", "14.0")[0].build, 7)

    def test_a_downloaded_app_must_match_its_name_and_pass_gatekeeper(self):
        _, system, _ = self.folders()
        data = b"zip"
        name = f"Coconut-0.1.0-7-nightly-{hashlib.sha256(data).hexdigest()[:16]}.zip"
        item = setup_coconut.mac_feed.Item(version="0.1.0", build=7, channel="nightly", file=name, length=3,
                                           signature="sig", published="")
        downloads = {setup_coconut.mac_feed.FEED_URL: setup_coconut.mac_feed.render([item]),
                     f"{setup_coconut.mac_feed.BASE_URL}/{name}": data}
        with patch.object(setup_coconut.mac_feed, "fetch", lambda url: downloads[url]):
            app = setup_coconut.install_app("stable")
        self.assertEqual(app, system / "Coconut.app")
        names = [command[0] for command in self.commands]
        self.assertEqual(names, ["ditto", "codesign", "spctl", "ditto", "defaults"])
        self.assertEqual(self.commands[2][:4], ["spctl", "--assess", "--type", "execute"])
        self.assertEqual(self.commands[4], ["defaults", "write", "com.gumroad.coconut.mac", "updateChannel", "nightly"])
        downloads[f"{setup_coconut.mac_feed.BASE_URL}/{name}"] = b"zap"
        self.commands.clear()
        with patch.object(setup_coconut.mac_feed, "fetch", lambda url: downloads[url]), \
                self.assertRaisesRegex(SystemExit, "incomplete"):
            setup_coconut.install_app("stable")
        self.assertEqual(self.commands, [])

    def test_the_app_runs_this_checkout_and_names_its_earlier_folder(self):
        with patch.object(setup_coconut, "app_folder", return_value=Path("/Users/a/Other")):
            self.assertEqual(setup_coconut.use_this_folder(), Path("/Users/a/Other"))
        self.assertEqual(self.commands, [["defaults", "write", "com.gumroad.coconut.mac", "InstallDirectory",
                                          str(setup_coconut.ROOT)]])
        with patch.object(setup_coconut, "app_folder", return_value=setup_coconut.ROOT):
            self.assertIsNone(setup_coconut.use_this_folder())

    def test_the_hermes_settings_of_the_shell_or_the_earlier_launch_agent_stay(self):
        service = {"COCONUT_HERMES_ROOT": "/agent/root", "PATH": "/bin"}
        with patch.object(setup_coconut.host, "service_environment", return_value=service), \
                patch.dict(setup_coconut.os.environ, {"COCONUT_HERMES_PYTHON": "/shell/python"}, clear=True):
            setup_coconut.save_hermes_settings()
        self.assertEqual(self.commands, [["defaults", "write", "com.gumroad.coconut.mac", "ServiceEnvironment", "-dict",
                                          "COCONUT_HERMES_PYTHON", "/shell/python", "COCONUT_HERMES_ROOT", "/agent/root"]])

    def test_without_hermes_settings_the_saved_ones_go(self):
        with patch.object(setup_coconut.host, "service_environment", return_value={}), \
                patch.dict(setup_coconut.os.environ, {}, clear=True), \
                patch.object(setup_coconut.subprocess, "run", return_value=completed([])) as ran:
            setup_coconut.save_hermes_settings()
        self.assertEqual(ran.call_args.args[0], ["defaults", "delete", "com.gumroad.coconut.mac", "ServiceEnvironment"])

    def test_another_folder_of_the_app_drains_and_its_login_items_start_again(self):
        previous = Path(self.directory.name) / "Other"
        (previous / "scripts").mkdir(parents=True)
        (previous / "scripts/host.py").write_text("")
        with patch.object(setup_coconut.subprocess, "run", return_value=completed([])) as ran, \
                patch.object(setup_coconut, "login_item_loaded", return_value=True):
            setup_coconut.leave_other_folder(previous)
        self.assertEqual(ran.call_args_list[0].args[0][0], "osascript")
        self.assertEqual(self.commands[0][0], "-c")
        self.assertIn("drain_for_update", self.commands[0][1])
        self.assertEqual(self.commands[0][2], str(previous / "scripts"))
        self.assertEqual([command[:3] for command in self.commands[1:]], [["launchctl", "kickstart", "-k"]] * 2)

    def leave(self, *, agents=(), process=None, app_runs=False):
        home = Path(self.directory.name) / "home"
        (home / "Library/LaunchAgents").mkdir(parents=True, exist_ok=True)
        for label in agents:
            (home / f"Library/LaunchAgents/{label}.plist").write_text("plist")
        with patch.object(setup_coconut.Path, "home", return_value=home), \
                patch.object(setup_coconut.host, "owned_process", return_value=process), \
                patch.object(setup_coconut.host, "app_runs_service", return_value=app_runs):
            setup_coconut.leave_launch_agents()

    def test_a_service_of_the_earlier_launch_agents_moves_to_the_app(self):
        self.leave(agents=["local.coconut.agent"], process=(42, {}))
        self.assertEqual(self.commands, [["host.py", "migrate-to-app"]])

    def test_a_manual_process_stops_and_a_service_of_the_app_stays(self):
        self.leave(process=(42, {}))
        self.assertEqual(self.commands, [["host.py", "stop"]])
        self.commands.clear()
        self.leave(process=(42, {}), app_runs=True)
        self.assertEqual(self.commands, [])

    def test_the_app_starts_the_service_and_setup_waits_for_its_health(self):
        checks = []
        with patch.object(setup_coconut.deploy.Operations, "health", lambda operations, root: checks.append(root)), \
                patch.object(setup_coconut.host, "owned_process", return_value=(42, {})):
            setup_coconut.start_app(Path("/Applications/Coconut.app"))
        self.assertEqual(self.commands, [["open", "-g", "/Applications/Coconut.app"]])
        self.assertEqual(checks, [setup_coconut.ROOT])

    def test_a_healthy_coconut_of_another_folder_does_not_count(self):
        with patch.object(setup_coconut.deploy.Operations, "health", lambda operations, root: None), \
                patch.object(setup_coconut.host, "owned_process", return_value=None), \
                self.assertRaisesRegex(SystemExit, "Another Coconut answers"):
            setup_coconut.start_app(Path("/Applications/Coconut.app"))

    def test_a_failed_health_check_says_where_to_allow_the_login_item(self):
        def fail(operations, root):
            raise setup_coconut.deploy.DeployError("No answer.")
        with patch.object(setup_coconut.deploy.Operations, "health", fail), self.assertRaises(SystemExit) as raised:
            setup_coconut.start_app(Path("/Applications/Coconut.app"))
        self.assertIn("No answer.", str(raised.exception))
        self.assertIn("Login Items", str(raised.exception))

    def test_the_relay_uses_the_sidecar_of_the_app(self):
        with patch.object(setup_coconut.subprocess, "run", return_value=completed([])) as ran:
            setup_coconut.start_tunnel(Path("/Applications/Coconut.app"))
        self.assertEqual(ran.call_args.args[0][1:], [str(setup_coconut.ROOT / "scripts/tunnel.py"), "rebuild",
                                                     "--data-dir", str(self.data)])
        self.assertEqual(ran.call_args.kwargs["env"]["COCONUT_SIDECAR"],
                         "/Applications/Coconut.app/Contents/Helpers/tunnel-sidecar")

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

    def test_share_fails_without_a_link(self):
        self.output = "Open Coconut on the iPhone, then tap Scan.\n"
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            setup_coconut.share()

    def test_main_runs_the_steps_in_order(self):
        calls = []
        names = ["prerequisites", "use_hermes", "prepare_notifications", "save_hermes_settings", "use_this_folder",
                 "leave_launch_agents", "leave_other_folder", "start_app", "start_tunnel", "share"]
        with contextlib.ExitStack() as stack:
            for name in names:
                stack.enter_context(patch.object(setup_coconut, name, lambda *args, name=name: calls.append(name)
                                                 or (Path("/Users/a/Other") if name == "use_this_folder" else None)))
            stack.enter_context(patch.object(setup_coconut, "find_app", return_value=None))
            stack.enter_context(patch.object(setup_coconut, "install_app",
                                             lambda channel: calls.append("install_app " + channel) or Path("/A/Coconut.app")))
            stack.enter_context(patch.object(setup_coconut.sys, "argv", ["setup_coconut.py"]))
            setup_coconut.main()
        self.assertEqual(calls, names[:3] + ["install_app stable"] + names[3:])


class RunTests(unittest.TestCase):
    def test_a_failed_quiet_command_shows_its_output(self):
        with patch.object(setup_coconut.subprocess, "run", return_value=completed([], 3, "details\n", "error\n")), \
                contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as raised:
            setup_coconut.run(["host.py"], "The host setup")
        self.assertEqual(errors.getvalue(), "details\nerror\n")
        self.assertIn("The host setup failed with exit code 3", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
