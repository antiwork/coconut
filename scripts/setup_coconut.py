#!/usr/bin/env python3
"""Set up Coconut on this Mac for a Hermes agent, then print the iPhone setup link.

Coconut.app runs the service of this folder with its login items. This command installs the app from the update feed
when it is missing, and moves a service of the earlier LaunchAgents to the app. The iPhone reaches this Mac through the
end-to-end encrypted relay, so no VPN or open port is necessary. Run it again at any time. Each step skips the work that
is already done, and each run prints a new setup link.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
import tempfile

import deploy
import host
import mac_feed


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".runtime/host"


def step(message: str) -> None:
    print(f"==> {message}", flush=True)


def run(arguments: list[str], label: str, *, quiet: bool = True) -> subprocess.CompletedProcess:
    """Run one project command. A quiet command shows its output only when it fails."""
    result = subprocess.run(arguments, cwd=ROOT, text=True, capture_output=quiet)
    if result.returncode:
        if quiet:
            sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"{label} failed with exit code {result.returncode}. Read the output above, then run this command again.")
    return result


def script(name: str, *arguments: str) -> list[str]:
    # Any Python runs the project scripts. The service itself starts again under the Python of the Hermes that it finds.
    return [sys.executable, str(ROOT / "scripts" / name), *arguments]


def prerequisites() -> None:
    if sys.platform != "darwin":
        raise SystemExit("Coconut runs on macOS. Run this command on the Mac that runs Hermes.")
    if sys.version_info < (3, 9):
        raise SystemExit("Run this command with Python 3.9 or newer.")
    tools = subprocess.run(["/usr/bin/xcode-select", "-p"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if tools.returncode:
        raise SystemExit("Install Xcode Command Line Tools with xcode-select --install. "
                         "Finish the installer, then run this command again.")


def use_hermes() -> None:
    """Find and check the Hermes that the person already uses. Coconut does not install its own copy.

    The service finds the same Hermes again at each start: hermes_runtime looks at COCONUT_HERMES_PYTHON and
    COCONUT_HERMES_ROOT, then at `hermes` on PATH, then at ~/.hermes/hermes-agent.
    """
    status = host.hermes_runtime().status(DATA, save=False)
    if status["status"] != "ok":
        raise SystemExit(f"{status['reason']} Then run this command again.")
    print(f"Coconut uses Hermes {status['version'] or 'of unknown version'} in {status['root']}.")
    warning = host.hermes_runtime().untested_warning(status["version"])
    if warning:
        print(warning)


def add_skill() -> None:
    """Let the person's Hermes load the `coconut` skill of this checkout. See hermes_skills.py. Coconut changes the
    Hermes config only during setup. A failure does not stop the setup: Coconut works without the skill."""
    result = subprocess.run(script("hermes_skills.py", "add"), cwd=ROOT, text=True, capture_output=True)
    sys.stdout.write(result.stdout)
    if result.returncode:
        sys.stderr.write(result.stderr)
        print("Hermes does not know the coconut skill yet. Run this command again to retry.")
    else:
        print(f"Hermes loads the coconut skill from {ROOT / 'skills'}.")


def prepare_notifications() -> None:
    # The service sends notifications only when the encryption helper and its sender key exist.
    run(script("push.py", "--rebuild"), "The notification setup")
    print("Notifications are ready.")


APP_NAME = "Coconut.app"
BUNDLE_ID = "com.gumroad.coconut.mac"
SERVICE_LABEL = "com.gumroad.coconut.service"
RELAY_LABEL = "com.gumroad.coconut.relay"
# The settings that choose another Hermes. See hermes_runtime.candidates.
HERMES_SETTINGS = ("COCONUT_HERMES_PYTHON", "COCONUT_HERMES_ROOT", "HERMES_HOME")


def app_folders() -> list[Path]:
    return [Path("/Applications"), Path.home() / "Applications"]


def find_app() -> Path | None:
    """Return an installed Coconut.app, or None."""
    for folder in app_folders():
        app = folder / APP_NAME
        try:
            with (app / "Contents/Info.plist").open("rb") as handle:
                if plistlib.load(handle).get("CFBundleIdentifier") == BUNDLE_ID:
                    return app
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
    return None


def version_key(value: str) -> tuple:
    return tuple(int(part) for part in value.split(".") if part.isdigit())


def choose_item(feed: bytes | None, system: str | None = None) -> mac_feed.Item:
    """The newest archive that runs on this macOS."""
    system = system or platform.mac_ver()[0]
    items = mac_feed.parse(feed or b"")
    if items and not any(version_key(item.minimum_system) <= version_key(system) for item in items):
        lowest = min((item.minimum_system for item in items), key=version_key)
        raise SystemExit(f"Coconut.app needs macOS {lowest} or later. This Mac has macOS {system}.")
    items = [item for item in items if version_key(item.minimum_system) <= version_key(system)]
    if items:
        return max(items, key=lambda item: item.build)
    raise SystemExit(f"The update feed {mac_feed.FEED_URL} has no Coconut.app yet. Try again later.")


def verify_app(app: Path) -> None:
    """Accept only an app with a valid signature that Gatekeeper accepts, which means that Apple notarized it."""
    for command, label in ((["codesign", "--verify", "--deep", "--strict", str(app)], "The signature check of Coconut.app"),
                           (["spctl", "--assess", "--type", "execute", str(app)], "The Gatekeeper check of Coconut.app")):
        run(command, label)


def install_app() -> Path:
    """Download Coconut.app from the update feed, check it, and put it in Applications."""
    item = choose_item(mac_feed.fetch(mac_feed.FEED_URL))
    print(f"Downloading Coconut {item.version} ({item.build}).", flush=True)
    data = mac_feed.fetch(f"{mac_feed.BASE_URL}/{item.file}")
    if data is None or len(data) != item.length or not mac_feed.matches_name(item.file, data):
        raise SystemExit(f"The download of {item.file} is incomplete. Run this command again.")
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / item.file
        archive.write_bytes(data)
        unpacked = Path(temporary) / "app"
        run(["ditto", "-x", "-k", str(archive), str(unpacked)], "Unpacking Coconut.app")
        verify_app(unpacked / APP_NAME)
        system, personal = app_folders()
        folder = system if os.access(system, os.W_OK) else personal
        folder.mkdir(exist_ok=True)
        run(["ditto", str(unpacked / APP_NAME), str(folder / APP_NAME)], "Copying Coconut.app")
    return folder / APP_NAME


def app_folder() -> Path:
    """The folder that the app runs now: its preference, else ~/Coconut."""
    result = subprocess.run(["defaults", "read", BUNDLE_ID, "InstallDirectory"], capture_output=True, text=True)
    value = result.stdout.strip() if result.returncode == 0 else ""
    return Path(value).expanduser() if value else Path.home() / "Coconut"


def use_this_folder() -> Path | None:
    """Make the app and its login items run this checkout. Sparkle then updates the app, and git pull updates the source.

    Return the earlier folder of the app when it was another one.
    """
    previous = app_folder()
    run(["defaults", "write", BUNDLE_ID, "InstallDirectory", str(ROOT)], "Choosing the Coconut folder")
    print(f"Coconut.app runs Coconut from {ROOT}.")
    return None if previous.resolve() == ROOT.resolve() else previous


def saved_hermes_settings() -> dict:
    """The Hermes settings in the app's preference ServiceEnvironment."""
    result = subprocess.run(["defaults", "export", BUNDLE_ID, "-"], capture_output=True)
    try:
        saved = plistlib.loads(result.stdout).get("ServiceEnvironment") if result.returncode == 0 else None
    except (ValueError, plistlib.InvalidFileException):
        saved = None
    if not isinstance(saved, dict):
        return {}
    return {name: value for name, value in saved.items() if name in HERMES_SETTINGS and isinstance(value, str) and value}


def load_hermes_settings(reset: bool) -> bool:
    """Use the saved Hermes settings where this shell has none, so use_hermes checks the Hermes that the service uses.

    With `reset`, forget them: the service then finds Hermes as this shell does. Return True when it forgot settings.
    """
    if reset:
        forgot = bool(saved_hermes_settings())
        subprocess.run(["defaults", "delete", BUNDLE_ID, "ServiceEnvironment"], capture_output=True)
        print("Coconut.app forgot its saved Hermes settings.")
        return forgot
    for name, value in saved_hermes_settings().items():
        os.environ.setdefault(name, value)
    return False


def save_hermes_settings() -> bool:
    """Keep the Hermes settings that use_hermes checked. The login items start without the environment of this shell,
    and the earlier LaunchAgent that kept them goes away. The `coconut` tool reads them from the app's preferences.

    The settings come from this shell, which load_hermes_settings filled from the saved ones, else from the earlier
    LaunchAgent. Without any, the saved preference stays as it is. Return True when the saved settings changed.
    """
    service = host.service_environment()
    settings = {name: os.environ.get(name) or service.get(name) for name in HERMES_SETTINGS}
    settings = {name: value for name, value in settings.items() if value}
    if not settings or settings == saved_hermes_settings():
        return False
    arguments = ["defaults", "write", BUNDLE_ID, "ServiceEnvironment", "-dict"]
    for name, value in sorted(settings.items()):
        arguments += [name, value]
    run(arguments, "Saving the Hermes settings")
    print("Coconut.app keeps these Hermes settings: " + ", ".join(sorted(settings)) + ".")
    return True


def login_item_loaded(label: str) -> bool:
    return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True).returncode == 0


def restart_login_items(previous: Path) -> None:
    """The app and its login items read the folder and the Hermes settings only when they start. Let the turns of the
    service in `previous` finish, then start the login items again, and quit the app, which `start_app` opens again.

    `previous` is the earlier folder of the app, or this checkout when only the Hermes settings changed."""
    subprocess.run(["osascript", "-e", f'quit app id "{BUNDLE_ID}"'], capture_output=True)
    if not login_item_loaded(SERVICE_LABEL):
        return
    if (previous / "scripts/host.py").is_file():
        print(f"Coconut restarts the service of {previous}. Its running bot turns finish first.", flush=True)
        drain = "import sys; sys.path.insert(0, sys.argv[1]); import host; host.drain_for_update()"
        run([sys.executable, "-c", drain, str(previous / "scripts")], "Waiting for the earlier service")
    for label in (SERVICE_LABEL, RELAY_LABEL):
        if login_item_loaded(label):
            run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], f"Restarting {label}")


def leave_launch_agents() -> None:
    """Stop a service of the earlier LaunchAgents or of host.py start. Coconut.app runs it with its login items."""
    agents = Path.home() / "Library/LaunchAgents"
    if any((agents / f"{label}.plist").exists() for label in (host.LABEL, host.TUNNEL_LABEL)):
        # The running bot turns finish first, as in an update.
        print("Moving Coconut from its earlier LaunchAgents to Coconut.app. Running bot turns finish first.", flush=True)
        run(script("host.py", "migrate-to-app"), "The move to Coconut.app")
    elif host.owned_process() is not None and not host.app_runs_service():
        run(script("host.py", "stop"), "Stopping Coconut")


def start_app(app: Path) -> None:
    """Open the app. It registers its login items, and the service login item starts Coconut."""
    run(["open", "-g", str(app)], "Opening Coconut.app")
    print("Waiting for Coconut to answer its health check.", flush=True)
    try:
        deploy.Operations().health(ROOT)
    except deploy.DeployError as error:
        raise SystemExit(f"{error} Open the Coconut menu in the menu bar. If it asks, allow Coconut in "
                         f"System Settings → General → Login Items. Then run this command again.") from None
    # The health check answers for any Coconut on this Mac. The process record says that this checkout serves.
    if host.owned_process() is None:
        raise SystemExit(f"Another Coconut answers on this Mac, not the one in {ROOT}. Open the Coconut menu, "
                         "check its folder, then run this command again.")
    print("Coconut is healthy. It starts again after a restart, when this Mac account logs in.")


def start_tunnel(app: Path) -> None:
    """Register the relay route once, with the sidecar of the app, and restart the relay login item."""
    environment = dict(os.environ, COCONUT_SIDECAR=str(app / "Contents/Helpers/tunnel-sidecar"))
    result = subprocess.run(script("tunnel.py", "rebuild", "--data-dir", str(DATA)), cwd=ROOT, env=environment,
                            text=True, capture_output=True)
    if result.returncode:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"The relay setup failed with exit code {result.returncode}. Read the output above, "
                         "then run this command again.")
    print("The relay tunnel is running.")


def share() -> None:
    output = run(script("pairing.py", "--share"), "Creating the setup link").stdout
    lines = dict(line.split(": ", 1) for line in output.splitlines() if line.startswith(("Setup link: ", "QR image: ")))
    if set(lines) != {"Setup link", "QR image"}:
        sys.stderr.write(output)
        raise SystemExit("The setup link is missing from the pairing output. Run this command again.")
    print()
    print("Send the setup link and the QR image to the person who pairs. They contain a one-time code.")
    print("On the iPhone, tap the link, or scan the QR image in Coconut. The link expires after ten minutes.")
    print("Setup link: " + lines["Setup link"])
    print("QR image: " + lines["QR image"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset-hermes-settings", action="store_true",
                        help="Forget the saved COCONUT_HERMES_PYTHON, COCONUT_HERMES_ROOT and HERMES_HOME of Coconut.app.")
    args = parser.parse_args()
    os.umask(0o077)
    step("Checking this Mac")
    prerequisites()
    settings_changed = load_hermes_settings(args.reset_hermes_settings)
    step("Finding Hermes")
    use_hermes()
    step("Adding the coconut skill to Hermes")
    add_skill()
    step("Preparing notifications")
    prepare_notifications()
    step("Installing Coconut.app")
    app = find_app()
    if app is None:
        app = install_app()
    print(f"Coconut.app is in {app.parent}. It updates itself.")
    settings_changed = save_hermes_settings() or settings_changed
    previous = use_this_folder()
    step("Starting the Coconut service")
    leave_launch_agents()
    if previous is not None or settings_changed:
        restart_login_items(previous or ROOT)
    start_app(app)
    step("Starting the relay tunnel")
    start_tunnel(app)
    step("Creating the iPhone setup link")
    share()


if __name__ == "__main__":
    main()
