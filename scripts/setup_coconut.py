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


def prepare_notifications() -> None:
    # The service sends notifications only when the encryption helper and its sender key exist.
    run(script("push.py", "--rebuild"), "The notification setup")
    print("Notifications are ready.")


APP_NAME = "Coconut.app"
BUNDLE_ID = "com.gumroad.coconut.mac"
SERVICE_LABEL = "com.gumroad.coconut.service"


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


def choose_item(feed: bytes | None, channel: str) -> tuple[mac_feed.Item, str]:
    """The newest archive of the channel. Before the first stable release, the stable channel takes the nightly one."""
    items = mac_feed.parse(feed or b"")
    for name in [channel, *(["nightly"] if channel == "stable" else [])]:
        found = [item for item in items if item.channel == name]
        if found:
            return max(found, key=lambda item: item.build), name
    raise SystemExit(f"The update feed {mac_feed.FEED_URL} has no Coconut.app yet. Try again later.")


def verify_app(app: Path) -> None:
    """Accept only an app with a valid signature that Gatekeeper accepts, which means that Apple notarized it."""
    for command, label in ((["codesign", "--verify", "--deep", "--strict", str(app)], "The signature check of Coconut.app"),
                           (["spctl", "--assess", "--type", "execute", str(app)], "The Gatekeeper check of Coconut.app")):
        run(command, label)


def install_app(channel: str) -> Path:
    """Download Coconut.app from the update feed, check it, and put it in Applications."""
    item, used = choose_item(mac_feed.fetch(mac_feed.FEED_URL), channel)
    print(f"Downloading Coconut {item.version} ({item.build}) from the {used} channel.", flush=True)
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
    if used == "nightly":
        # The app then keeps the channel of this build.
        run(["defaults", "write", BUNDLE_ID, "updateChannel", "nightly"], "Choosing the nightly channel")
    return folder / APP_NAME


def use_this_folder() -> None:
    """Make the app and its login items run this checkout. Sparkle then updates the app, and git pull updates the source."""
    run(["defaults", "write", BUNDLE_ID, "InstallDirectory", str(ROOT)], "Choosing the Coconut folder")
    print(f"Coconut.app runs Coconut from {ROOT}.")


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
    parser.add_argument("--channel", choices=["stable", "nightly"], default="stable",
                        help="The update channel of a new Coconut.app.")
    args = parser.parse_args()
    os.umask(0o077)
    step("Checking this Mac")
    prerequisites()
    step("Finding Hermes")
    use_hermes()
    step("Preparing notifications")
    prepare_notifications()
    step("Installing Coconut.app")
    app = find_app()
    if app is None:
        app = install_app(args.channel)
    print(f"Coconut.app is in {app.parent}. It updates itself.")
    use_this_folder()
    step("Starting the Coconut service")
    leave_launch_agents()
    start_app(app)
    step("Starting the relay tunnel")
    start_tunnel(app)
    step("Creating the iPhone setup link")
    share()


if __name__ == "__main__":
    main()
