#!/usr/bin/env python3
"""Pair a physical iPhone through the real Settings UI and verify its saved cache."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import time

import iphone
import pairing
import remote


ROOT = Path(__file__).resolve().parents[1]
TEST = "PhysicalPairingFlowTests/testPairPhysicalPhoneThroughSettings"
STAGES = {"launched", "settings", "details", "address", "fingerprint", "code", "submitted", "saved"}


def payload_from_host(local: bool) -> dict:
    if local:
        return pairing.local_payload()
    config = remote.configuration()
    remote.verify_remote_health(config)
    code = remote.ROOT_CODE + """
sys.path.insert(0, str(root / 'scripts'))
import pairing
print(json.dumps(pairing.local_payload()))
"""
    result = remote.ssh(config, ["python3", "-c", code, config["directory"]])
    return pairing.validate(json.loads(result.stdout))


def prepare_run(source: Path, destination: Path, payload: dict) -> None:
    document = plistlib.loads(source.read_bytes())

    def paths(value):
        if isinstance(value, str):
            return value.replace("__TESTROOT__", str(source.parent))
        if isinstance(value, list):
            return [paths(item) for item in value]
        if isinstance(value, dict):
            return {key: paths(item) for key, item in value.items()}
        return value

    document = paths(document)
    configs = document.get("TestConfigurations", [])
    if configs:
        targets = [target for config in configs for target in config.get("TestTargets", [])]
    else:
        targets = [target for key, target in document.items() if not key.startswith("__") and isinstance(target, dict)]
    selected = []
    for target in targets:
        name = target.get("BlueprintName") or target.get("ProductModuleName")
        if name != "CoconutUITests":
            target["IsEnabled"] = False
            continue
        target["IsEnabled"] = True
        target["OnlyTestIdentifiers"] = [TEST]
        target["SkipTestIdentifiers"] = []
        target["ParallelizationEnabled"] = False
        target["SystemAttachmentLifetime"] = "keepNever"
        target["UserAttachmentLifetime"] = "keepNever"
        target["CommandLineArguments"] = []
        target["UITargetAppCommandLineArguments"] = []
        target["UITargetAppEnvironmentVariables"] = {}
        environment = target.setdefault("EnvironmentVariables", {})
        environment["COCONUT_PHYSICAL_PAIRING"] = json.dumps({"serverURL": payload["server_url"],
                                                              "fingerprint": payload["certificate_sha256"],
                                                              "code": payload["pairing_code"]})
        selected.append(target)
    if not selected:
        raise SystemExit("The xctestrun file must include CoconutUITests.")
    destination.write_bytes(plistlib.dumps(document))
    destination.chmod(0o600)


def copy_cache(config: dict, directory: Path, name: str) -> dict | None:
    destination = directory / name
    command = ["xcrun", "devicectl", "device", "copy", "from", "--device", config["device"],
               "--domain-type", "appDataContainer", "--domain-identifier", config["bundle_id"],
               "--source", "Library/Application Support/Coconut/" + name,
               "--destination", str(destination), "--timeout", "20"]
    result = subprocess.run(command, capture_output=True, timeout=30)
    if result.returncode or not destination.exists():
        return None
    return json.loads(destination.read_text())


def validate_cache(saved: dict | None, snapshot: dict | None, previous: dict | None, payload: dict, started: float) -> dict:
    if saved is None or snapshot is None:
        raise ValueError("The phone did not return both protected cache files.")
    if saved.get("serverURL", "").rstrip("/") != payload["server_url"].rstrip("/"):
        raise ValueError("The phone did not save the requested host.")
    if saved.get("fingerprint", "").lower() != payload["certificate_sha256"].lower():
        raise ValueError("The phone did not save the requested certificate pin.")
    if not saved.get("deviceId") or (previous and saved["deviceId"] == previous.get("deviceId")):
        raise ValueError("The phone did not save a new pairing receipt.")
    synced = snapshot.get("syncedAt")
    if not isinstance(synced, (int, float)) or synced + 978307200 < started:
        raise ValueError("The phone snapshot predates this pairing attempt.")
    state = snapshot.get("snapshot", {})
    if state.get("provider") in {None, "fixture", "preview", "demo"}:
        raise ValueError("The snapshot does not identify a live execution provider.")
    return {"paired": True, "fresh_snapshot": True, "provider": state["provider"], "cursor": state.get("cursor"),
            "bots": len(state.get("bots", [])), "tasks": len(state.get("tasks", [])),
            "decisions": len(state.get("decisions", [])), "artifacts": len(state.get("artifacts", []))}


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xctestrun", required=True, type=Path)
    parser.add_argument("--local", action="store_true", help="Pair with this Mac instead of the saved remote Mac.")
    args = parser.parse_args()
    config = iphone.load_config()
    source = args.xctestrun.resolve()
    if not source.is_file() or "iphoneos" not in str(source):
        raise SystemExit("Provide the physical-device xctestrun from build-for-testing.")
    root = ROOT / ".runtime"
    remote.private_directory(root)
    report_path = root / "physical-pairing-report.json"
    report = {"paired": False, "fresh_snapshot": False, "stage": "prepare"}
    try:
        with tempfile.TemporaryDirectory(prefix="physical-pair-", dir=root) as temporary:
            folder = Path(temporary)
            before = folder / "before"
            before.mkdir()
            previous = copy_cache(config, before, "pairing.json")
            payload = payload_from_host(args.local)
            runfile = folder / "Pairing.xctestrun"
            prepare_run(source, runfile, payload)
            result_bundle = folder / "Pairing.xcresult"
            log_path = folder / "xcodebuild.log"
            started = time.time()
            command = ["xcodebuild", "test-without-building", "-xctestrun", str(runfile),
                       "-destination", "platform=iOS,id=" + (config.get("device_udid") or config["device"]),
                       "-only-testing:CoconutUITests/" + TEST, "-parallel-testing-enabled", "NO",
                       "-resultBundlePath", str(result_bundle)]
            print("Pairing the iPhone through Coconut Settings. Test details remain private.", flush=True)
            with log_path.open("w") as log:
                result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=240)
            stages = re.findall(r"COCONUT_PAIR_STAGE: ([a-z]+)", log_path.read_text(errors="replace"))
            report["stage"] = next((stage for stage in reversed(stages) if stage in STAGES), "test-start")
            if result.returncode:
                raise ValueError("The physical Settings test did not pass.")
            if report["stage"] != "saved":
                raise ValueError("The physical Settings test did not complete the pairing form.")
            after = folder / "after"
            after.mkdir()
            saved = copy_cache(config, after, "pairing.json")
            snapshot = copy_cache(config, after, "snapshot.json")
            report.update(validate_cache(saved, snapshot, previous, payload, started))
            report["stage"] = "verified"
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        report["error"] = str(error) if isinstance(error, ValueError) else "The physical pairing command failed or timed out."
    finally:
        report["checked_at"] = datetime.now(timezone.utc).isoformat()
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        report_path.chmod(0o600)
    print(json.dumps(report))
    if not report["paired"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
