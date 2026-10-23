#!/usr/bin/env python3
"""Set up Coconut on this Mac for a Hermes agent, then print the iPhone setup link.

Run it again at any time. Each step skips the work that is already done, and each run prints a new setup link.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import deploy
import host


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".runtime/host"
PORT = 8960
TAILSCALE_APP = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")


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


def tailscale_command() -> str:
    found = shutil.which("tailscale")
    if found:
        return found
    if TAILSCALE_APP.is_file():
        return str(TAILSCALE_APP)
    raise SystemExit("Tailscale is not installed. Install it from https://tailscale.com/download/mac, sign in, "
                     "then run this command again.")


def tailscale_host() -> str:
    """Return this Mac's Tailscale name, or its Tailscale IPv4 address when MagicDNS is off."""
    try:
        result = subprocess.run([tailscale_command(), "status", "--json"], capture_output=True, text=True, timeout=15)
        status = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        status = {}
    if status.get("BackendState") != "Running":
        raise SystemExit("Tailscale is not connected. Open Tailscale on this Mac, sign in, then run this command again.")
    me = status.get("Self") or {}
    name = (me.get("DNSName") or "").rstrip(".")
    if name:
        return name
    addresses = [address for address in me.get("TailscaleIPs") or [] if "." in address]
    if not addresses:
        raise SystemExit("Tailscale has no address for this Mac. Reconnect Tailscale, then run this command again.")
    return addresses[0]


def prerequisites() -> None:
    if sys.platform != "darwin":
        raise SystemExit("Coconut runs on macOS. Run this command on the Mac that runs Hermes.")
    if sys.version_info < (3, 9):
        raise SystemExit("Run this command with Python 3.9 or newer.")
    tools = subprocess.run(["/usr/bin/xcode-select", "-p"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if tools.returncode:
        raise SystemExit("Install Xcode Command Line Tools with xcode-select --install. "
                         "Finish the installer, then run this command again.")


def tailscale_address() -> str:
    """Return the HTTPS address that the iPhone reaches over Tailscale."""
    name = tailscale_host()
    saved = saved_address()
    # Keep a port that an earlier setup chose for the same name.
    if saved and host.address_parts(saved)[1] == host.normalize_host(name):
        return saved
    return host.address_parts(f"https://{name}:{PORT}")[0]


def saved_address() -> str | None:
    path = DATA / "connection.json"
    if not path.is_file():
        return None
    try:
        return host.address_parts(json.loads(path.read_text())["address"])[0]
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit(f"{path} is invalid. Remove it, then run this command again.") from None


def use_hermes() -> None:
    """Find and check the Hermes that the person already uses. Coconut does not install its own copy.

    The service finds the same Hermes again at each start: hermes_runtime looks at COCONUT_HERMES_PYTHON and
    COCONUT_HERMES_ROOT, then at `hermes` on PATH, then at ~/.hermes/hermes-agent.
    """
    status = host.hermes_runtime().status(DATA, save=False)
    if status["status"] != "ok":
        raise SystemExit(f"{status['reason']} Then run this command again.")
    print(f"Coconut uses Hermes {status['version'] or 'of unknown version'} in {status['root']}.")


def prepare_notifications() -> None:
    # The service sends notifications only when the encryption helper and its sender key exist.
    run(script("push.py", "--rebuild"), "The notification setup")
    print("Notifications are ready.")


def service_running() -> bool:
    return host.owned_agent()[1] or host.owned_process() is not None


def configure(address: str) -> None:
    if saved_address() == address and (DATA / "server.crt").is_file():
        print(f"Coconut already uses {address}.")
        return
    # The host changes its address only while the service is stopped. The next step starts it again.
    if service_running():
        run(script("host.py", "stop"), "Stopping Coconut")
    run(script("host.py", "setup", "--address", address), "The host setup")
    print(f"Coconut uses {address}.")


def start_service(address: str) -> None:
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
            run(script("host.py", "install-service", "--address", address), "The login service installation")
        print("Coconut starts again after a restart, when this Mac account logs in.")
    print("Waiting for Coconut to answer its health check.", flush=True)
    try:
        deploy.Operations().health(ROOT, address)
    except deploy.DeployError as error:
        raise SystemExit(f"{error} Read {DATA / 'logs/error.log'}, then run this command again.") from None
    print("Coconut is healthy.")


def share() -> None:
    output = run(script("pairing.py", "--share"), "Creating the setup link").stdout
    lines = dict(line.split(": ", 1) for line in output.splitlines() if line.startswith(("Setup link: ", "QR image: ")))
    if set(lines) != {"Setup link", "QR image"}:
        sys.stderr.write(output)
        raise SystemExit("The setup link is missing from the pairing output. Run this command again.")
    print()
    print("Send the setup link and the QR image to the person who pairs. They contain a one-time code.")
    print("The iPhone needs Tailscale, signed in to the same tailnet as this Mac.")
    print("On the iPhone, tap the link, or scan the QR image in Coconut. The code expires after five minutes.")
    print("Setup link: " + lines["Setup link"])
    print("QR image: " + lines["QR image"])


def main() -> None:
    os.umask(0o077)
    step("Checking this Mac")
    prerequisites()
    step("Finding Hermes")
    use_hermes()
    step("Configuring the Tailscale address")
    address = tailscale_address()
    configure(address)
    step("Preparing notifications")
    prepare_notifications()
    step("Starting the Coconut service")
    start_service(address)
    step("Creating the iPhone setup link")
    share()


if __name__ == "__main__":
    main()
