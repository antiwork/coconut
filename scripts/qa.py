#!/usr/bin/env python3
"""The QA loop for the iPhone app: one command to a screen with demo data, in its own simulator.

    python3 scripts/qa.py [place]        build what changed, install, and open the app on a place (default: home)
    python3 scripts/qa.py open <place>   open the installed app on another place, without a build
    python3 scripts/qa.py shot [file]    save a screenshot of the simulator (default: .build/qa/shot.png)

A place is a name of the app's --open launch argument (CoconutHome.showPreviewPlace in ios/Coconut/CoconutApp.swift),
for example home, chats, cards, chat, agent, agent-routines, business, team. The app runs with its demo data
(--demo), so it needs no Mac and no pairing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".build/qa"
DEVICE = "Coconut QA"
BUNDLE = "com.gumroad.coconut"


def run(arguments, **kwargs):
    return subprocess.run(arguments, check=True, text=True, **kwargs)


def device() -> str:
    """The UDID of the QA simulator, booted. It is created on the first run, apart from the simulators of tests."""
    listed = json.loads(run(["xcrun", "simctl", "list", "devices", "available", "--json"], capture_output=True).stdout)
    found = [d for devices in listed["devices"].values() for d in devices if d["name"] == DEVICE]
    if found:
        udid, state = found[0]["udid"], found[0]["state"]
    else:
        runtimes = json.loads(run(["xcrun", "simctl", "list", "runtimes", "available", "--json"],
                                  capture_output=True).stdout)["runtimes"]
        ios = [r for r in runtimes if r["identifier"].startswith("com.apple.CoreSimulator.SimRuntime.iOS")]
        if not ios:
            sys.exit("Install an iOS simulator runtime in Xcode first.")
        models = [t["identifier"] for t in ios[-1].get("supportedDeviceTypes", []) if t["name"].startswith("iPhone")]
        model = next((m for m in models if "Pro" not in m and "Plus" not in m and "Air" not in m), models[-1])
        udid = run(["xcrun", "simctl", "create", DEVICE, model, ios[-1]["identifier"]], capture_output=True).stdout.strip()
        state = "Shutdown"
    if state != "Booted":
        booted = subprocess.run(["xcrun", "simctl", "boot", udid], text=True, capture_output=True)
        if booted.returncode:
            running = sum(d["state"] == "Booted" for devices in listed["devices"].values() for d in devices)
            sys.exit(f"The {DEVICE} simulator did not start ({running} other simulators run). "
                     "Shut down simulators you no longer need (xcrun simctl shutdown <udid>) and run this again.")
    run(["xcrun", "simctl", "bootstatus", udid, "-b"], stdout=subprocess.DEVNULL)
    run(["open", "-a", "Simulator", "--args", "-CurrentDeviceUDID", udid])
    return udid


def build() -> Path:
    """Builds for any simulator, so the build does not wait for a simulator to boot."""
    project = ROOT / "ios/Coconut.xcodeproj"
    spec = ROOT / "ios/project.yml"
    if shutil.which("xcodegen") and (not project.exists()
                                     or spec.stat().st_mtime > (project / "project.pbxproj").stat().st_mtime):
        run(["xcodegen", "generate", "--spec", str(spec)], stdout=subprocess.DEVNULL)
    BUILD.mkdir(parents=True, exist_ok=True)
    log = BUILD / "build.log"
    print("Building (the first build is slow; later builds compile only what changed)…", flush=True)
    with log.open("w") as out:
        result = subprocess.run(["xcodebuild", "-project", str(project), "-scheme", "Coconut", "-configuration", "Debug",
                                 "-destination", "generic/platform=iOS Simulator", "-derivedDataPath", str(BUILD / "dd"), "build"],
                                stdout=out, stderr=subprocess.STDOUT)
    if result.returncode:
        errors = [line for line in log.read_text().splitlines() if "error:" in line]
        sys.exit("\n".join(errors[:20]) or f"The build failed. See {log}.")
    return BUILD / "dd/Build/Products/Debug-iphonesimulator/Coconut.app"


def launch(udid: str, place: str, app: Path | None = None):
    if app:
        run(["xcrun", "simctl", "install", udid, str(app)])
    subprocess.run(["xcrun", "simctl", "terminate", udid, BUNDLE], capture_output=True)
    run(["xcrun", "simctl", "launch", udid, BUNDLE, "--demo", "--open", place], stdout=subprocess.DEVNULL)
    print(f"Coconut shows {place}.")


def main():
    arguments = sys.argv[1:]
    if arguments[:1] == ["open"] and len(arguments) == 2:
        launch(device(), arguments[1])
    elif arguments[:1] == ["shot"]:
        target = Path(arguments[1]) if len(arguments) > 1 else BUILD / "shot.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        run(["xcrun", "simctl", "io", device(), "screenshot", str(target)], capture_output=True)
        print(target)
    elif len(arguments) <= 1:
        app = build()
        launch(device(), arguments[0] if arguments else "home", app)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
