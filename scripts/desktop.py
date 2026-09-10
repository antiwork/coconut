#!/usr/bin/env python3
"""Open the local Coconut service or iPhone Simulator from Finder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".runtime/host"
BUILD = ROOT / ".build/simulator"
BUNDLE_ID = "com.gumroad.coconut"


def simulator_address() -> str:
    connection = DATA / "connection.json"
    if not connection.exists():
        return "https://localhost:8960"
    address = json.loads(connection.read_text())["address"]
    return f"https://localhost:{urlsplit(address).port or 443}"


def run(arguments: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(arguments, cwd=ROOT, check=True, **kwargs)


def start() -> None:
    if not (DATA / "credentials.json").is_file():
        if any(os.environ.get(key) for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")):
            run([sys.executable, "scripts/host.py", "save-model-access"])
        else:
            print("No saved model key. Configure a local endpoint or save model access before sending work.")
    run([sys.executable, "scripts/host.py", "start"])


def simulator() -> None:
    if not shutil.which("xcodebuild") or not shutil.which("xcrun"):
        raise SystemExit("Install Xcode before opening the iPhone Simulator.")
    source = ROOT / ".runtime/hermes/source/run_agent.py"
    if not source.is_file():
        raise SystemExit("Install Hermes first: python3 scripts/hermes.py install")
    result = run(["xcrun", "simctl", "list", "devices", "available", "--json"],
                 capture_output=True, text=True)
    runtimes = json.loads(result.stdout)["devices"]
    supported = [key for key in runtimes if (match := re.search(r"\.iOS-(\d+)", key))
                 and int(match.group(1)) >= 18]
    supported.sort(key=lambda key: tuple(int(part) for part in key.split(".iOS-")[1].split("-")), reverse=True)
    devices = [device for key in supported for device in runtimes[key]
               if device.get("isAvailable") and device["name"].startswith("iPhone")]
    devices.sort(key=lambda device: device.get("state") != "Booted")
    if not devices:
        raise SystemExit("Add an iPhone Simulator in Xcode Settings before continuing.")
    device = devices[0]
    BUILD.mkdir(parents=True, exist_ok=True)
    log = ROOT / ".build/simulator-build.log"
    print("Building Coconut for the iPhone Simulator.", flush=True)
    with log.open("w") as output:
        command = ["xcodebuild", "-project", "ios/Coconut.xcodeproj", "-scheme", "Coconut",
                   "-configuration", "Debug", "-sdk", "iphonesimulator",
                   "-destination", f"id={device['udid']}", "-derivedDataPath", str(BUILD),
                   "CODE_SIGNING_ALLOWED=YES", "CODE_SIGN_IDENTITY=-", "build"]
        result = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise SystemExit(f"The build failed. Read {log}.")
    start()
    if device.get("state") != "Booted":
        run(["xcrun", "simctl", "boot", device["udid"]], stdout=subprocess.DEVNULL)
    run(["xcrun", "simctl", "bootstatus", device["udid"], "-b"], stdout=subprocess.DEVNULL)
    app = BUILD / "Build/Products/Debug-iphonesimulator/Coconut.app"
    run(["xcrun", "simctl", "install", device["udid"], str(app)])
    result = run([sys.executable, "scripts/host.py", "pair"], capture_output=True, text=True)
    code = result.stdout.strip()
    if not code or "\n" in code:
        raise SystemExit("The Mac did not return one pairing code. Open Coconut Settings to pair manually.")
    cert = DATA / "server.crt"
    pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
    child_env = dict(os.environ)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        child_env.pop(key, None)
    child_env.update(SIMCTL_CHILD_COCONUT_SERVER_URL=simulator_address(),
                     SIMCTL_CHILD_COCONUT_CERT_SHA256=pin,
                     SIMCTL_CHILD_COCONUT_PAIR_CODE=code)
    launched_at = time.time()
    run(["xcrun", "simctl", "launch", "--terminate-running-process", device["udid"], BUNDLE_ID],
        env=child_env, stdout=subprocess.DEVNULL)
    run(["open", "-a", "Simulator", "--args", "-CurrentDeviceUDID", device["udid"]])
    result = run(["xcrun", "simctl", "get_app_container", device["udid"], BUNDLE_ID, "data"],
                 capture_output=True, text=True)
    cache = Path(result.stdout.strip()) / "Library/Application Support/Coconut"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            pairing = json.loads((cache / "pairing.json").read_text())
            snapshot = cache / "snapshot.json"
            if pairing["fingerprint"].lower() == pin.lower() and snapshot.stat().st_mtime >= launched_at:
                json.loads(snapshot.read_text())["snapshot"]
                print("Coconut is paired. The app displays a fresh snapshot from this Mac.")
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.2)
    raise SystemExit("Coconut opened, but synchronization did not finish. Inspect its connection message in Settings.")


def uninstall() -> None:
    run([sys.executable, "scripts/cleanup.py"])
    print("This removes Hermes, conversations, files, and model keys stored in the local runtime.")
    if input("Type DELETE to continue: ").strip() != "DELETE":
        print("The local runtime remains installed.")
        return
    run([sys.executable, "scripts/cleanup.py", "--yes"])
    print("Delete the Coconut folder to remove its source and build files.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "simulator", "uninstall"])
    args = parser.parse_args()
    try:
        {"start": start, "simulator": simulator, "uninstall": uninstall}[args.command]()
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"The Coconut command stopped with exit code {error.returncode}.") from None


if __name__ == "__main__":
    main()
