#!/usr/bin/env python3
"""Set up Coconut on this Mac for a Hermes agent, then print the iPhone setup link.

The iPhone reaches this Mac through the end-to-end encrypted relay, so no VPN or open port is necessary. Run it again at
any time. Each step skips the work that is already done, and each run prints a new setup link.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

import deploy
import host


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


def start_service() -> None:
    plist, loaded = host.owned_agent()
    if loaded:
        print("The Coconut login service is already running.")
    else:
        # A process from host.py start would hold the port that the login service needs.
        if host.owned_process() is not None:
            run(script("host.py", "stop"), "Stopping Coconut")
        if plist.exists():
            run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], "Starting the Coconut login service")
        else:
            run(script("host.py", "install-service"), "The login service installation")
        print("Coconut starts again after a restart, when this Mac account logs in.")
    print("Waiting for Coconut to answer its health check.", flush=True)
    try:
        deploy.Operations().health(ROOT)
    except deploy.DeployError as error:
        if not loaded:
            raise SystemExit(f"{error} Read {DATA / 'logs/error.log'}, then run this command again.") from None
        # The health check uses the tunnel listener. A service from before that listener must restart.
        ensure_tunnel_listener()
    print("Coconut is healthy.")


def tunnel_listener_ready() -> bool:
    """True when the backend answers on the loopback listener that the tunnel sidecar forwards to."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{host.TUNNEL_PORT}/health", timeout=2) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def ensure_tunnel_listener(seconds: float = 180) -> None:
    if tunnel_listener_ready():
        return
    # A service that started before the tunnel listener existed has no --tunnel-port. A restart reads the
    # current launch settings. Without the listener, the phone cannot pair through a version 2 link.
    print("Coconut has no tunnel listener yet. Restarting it with the current launch settings.", flush=True)
    # A stop interrupts the running bot turns, and they lose their tool results. Let them finish first, as the
    # deploy does. A service that cannot drain stops at once, as before.
    print("Waiting for running bot turns to finish. This can take up to 10 minutes.", flush=True)
    host.drain_for_update()
    run(script("host.py", "restart"), "Restarting Coconut")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if tunnel_listener_ready():
            return
        time.sleep(1)
    raise SystemExit(f"Coconut does not answer on 127.0.0.1:{host.TUNNEL_PORT}. "
                     f"Read {DATA / 'logs/error.log'}, then run this command again.")


def start_tunnel() -> None:
    # The app reaches this Mac through the relay tunnel. start_service made sure that the backend's tunnel listener answers.
    run(script("tunnel.py", "install", "--data-dir", str(DATA)), "The relay tunnel installation")
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
    os.umask(0o077)
    step("Checking this Mac")
    prerequisites()
    step("Finding Hermes")
    use_hermes()
    step("Preparing notifications")
    prepare_notifications()
    step("Starting the Coconut service")
    start_service()
    step("Starting the relay tunnel")
    start_tunnel()
    step("Creating the iPhone setup link")
    share()


if __name__ == "__main__":
    main()
