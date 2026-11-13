#!/usr/bin/env python3
"""Build and update the native Coconut app on one paired iPhone."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "com.gumroad.coconut"
CONFIG = ROOT / ".runtime/iphone.json"
BUILD = ROOT / ".build/iphone"
APP = BUILD / "Build/Products/Debug-iphoneos/Coconut.app"
ARCHIVE = BUILD / "Coconut.xcarchive"
EXPORT_OPTIONS = BUILD / "ExportOptions.plist"
TESTFLIGHT_OPTIONS = BUILD / "TestFlightOptions.plist"


def run(arguments: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(arguments, cwd=ROOT, check=True, **kwargs)


def load_config() -> dict:
    if not CONFIG.exists():
        raise SystemExit("Configure the phone first: python3 scripts/iphone.py configure --team TEAM --device DEVICE")
    return json.loads(CONFIG.read_text())


def save_config(config: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="iphone-", suffix=".json", dir=CONFIG.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(config, output, indent=2)
            output.write("\n")
        os.replace(name, CONFIG)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def update_lock():
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with (CONFIG.parent / "iphone.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another iPhone update is running.") from None
        yield


def device_command(arguments: list[str], timeout: int = 30) -> dict:
    with tempfile.TemporaryDirectory(prefix="coconut-device-") as folder:
        output = Path(folder) / "result.json"
        command = ["xcrun", "devicectl", *arguments, "--timeout", str(timeout), "--json-output", str(output)]
        environment = {key: value for key, value in os.environ.items() if not key.startswith("DEVICECTL_CHILD_")}
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout + 10, env=environment)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            if "1011" in detail:
                raise SystemExit("The paired iPhone is not reachable. Connect it by cable and unlock it. Then retry with install --skip-build.")
            raise SystemExit(detail or "The iPhone command failed.")
        document = json.loads(output.read_text())
        if document.get("info", {}).get("outcome") == "failed":
            raise SystemExit("The iPhone command failed. Connect the phone by cable and unlock it.")
        return document.get("result", {})


def device_details(identifier: str) -> dict:
    result = device_command(["device", "info", "details", "--device", identifier])
    return result.get("device", result)


def signing_identities() -> list[dict]:
    result = run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True)
    valid = set(re.findall(r"\b([A-F0-9]{40})\b", result.stdout))
    certificates = run(["security", "find-certificate", "-a", "-c", "Apple Development", "-p"], capture_output=True)
    identities = []
    for part in certificates.stdout.split(b"-----END CERTIFICATE-----"):
        if b"-----BEGIN CERTIFICATE-----" not in part:
            continue
        certificate = part + b"-----END CERTIFICATE-----\n"
        details = run(["openssl", "x509", "-noout", "-subject", "-dates", "-fingerprint", "-sha1"],
                      input=certificate, capture_output=True).stdout.decode()
        fingerprint = re.search(r"Fingerprint=([A-Fa-f0-9:]+)", details)
        team = re.search(r"\bOU\s*=\s*([A-Z0-9]{10})\b", details)
        name = re.search(r"\bO\s*=\s*([^,\n]+)", details)
        expiry = re.search(r"notAfter=(.+)", details)
        if fingerprint and team and fingerprint.group(1).replace(":", "").upper() in valid:
            identities.append({"team": team.group(1), "name": name.group(1) if name else team.group(1),
                               "expires": expiry.group(1) if expiry else "unknown"})
    return identities


def doctor() -> None:
    print("Valid Apple Development identities:")
    identities = signing_identities()
    for identity in identities:
        print(f"  {identity['name']} · {identity['team']} · certificate expires {identity['expires']}")
    if not identities:
        print("  None. Add your Apple Account in Xcode Settings.")
    print("Physical devices:")
    for device in device_command(["list", "devices"]).get("devices", []):
        hardware = device.get("hardwareProperties", {})
        if hardware.get("platform") != "iOS":
            continue
        properties = device.get("deviceProperties", {})
        connection = device.get("connectionProperties", {})
        print(f"  {properties.get('name', 'iPhone')} · {device['identifier']} · {connection.get('pairingState', 'unknown')}")
    if CONFIG.exists():
        config = load_config()
        print(f"Saved team: {config['team']} · bundle: {config['bundle_id']}")
        details = device_details(config["device"])
        properties = details.get("deviceProperties", {})
        connection = details.get("connectionProperties", {})
        print(f"Developer Mode: {properties.get('developerModeStatus', 'unknown')} · tunnel: {connection.get('tunnelState', 'unknown')}")
        if config.get("profile_expires"):
            print(f"Last built app profile expires: {config['profile_expires']}")


def configure(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[A-Z0-9]{10}", args.team):
        raise SystemExit("The team must be a ten-character Apple team ID.")
    if args.bundle_id != BUNDLE_ID:
        raise SystemExit(f"This project builds {BUNDLE_ID}. Use the default bundle ID.")
    if args.version != "date" and not re.fullmatch(r"\d+(?:\.\d+){0,2}", args.version):
        raise SystemExit("Use date for a UTC calendar version, or numbers such as 1.0.0.")
    if not any(identity["team"] == args.team for identity in signing_identities()):
        raise SystemExit("This Mac has no valid Apple Development identity for that team.")
    details = device_details(args.device)
    if details.get("hardwareProperties", {}).get("platform") != "iOS":
        raise SystemExit("Select a physical iPhone or iPad.")
    config = load_config() if CONFIG.exists() else {}
    if config and (config["team"] != args.team or config["bundle_id"] != args.bundle_id):
        raise SystemExit("The saved signing team and bundle ID must stay unchanged to preserve this installation.")
    config.update(team=args.team, device=details["identifier"], bundle_id=args.bundle_id,
                  version=args.version, signing_identity="Apple Development")
    config["device_udid"] = details.get("hardwareProperties", {}).get("udid")
    save_config(config)
    print(f"Saved {args.bundle_id} for team {args.team}. Future updates reuse this app identity.")


def next_build_number(config: dict) -> int:
    previous = int(config.get("last_build_number", 0))
    for app in [APP, ARCHIVE / "Products/Applications/Coconut.app"]:
        if (app / "Info.plist").exists():
            value = plistlib.loads((app / "Info.plist").read_bytes()).get("CFBundleVersion", "0")
            if str(value).isdigit():
                previous = max(previous, int(value))
    return previous + 1


def xcode(arguments: list[str], log_name: str) -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    log = BUILD / log_name
    print(f"Building Coconut. Log: {log}", flush=True)
    with log.open("w") as output:
        result = subprocess.run(["xcodebuild", *arguments], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        if log_name == "testflight.log":
            raise SystemExit(f"TestFlight upload did not finish. Check App Store Connect before retrying. Log: {log}")
        raise SystemExit(f"Xcode failed. Read {log}. The installed app remains unchanged.")


def build(config: dict, archive: bool = False) -> Path:
    if not shutil.which("xcodegen"):
        raise SystemExit("Install XcodeGen before updating Coconut.")
    run(["xcodegen", "generate", "--spec", "ios/project.yml"], stdout=subprocess.DEVNULL)
    version = config["version"]
    if version == "date":
        today = datetime.now(timezone.utc)
        version = f"{today.year}.{today.month}.{today.day}"
    number = next_build_number(config)
    config["last_build_number"] = number
    save_config(config)
    command = ["-project", "ios/Coconut.xcodeproj", "-scheme", "Coconut", "-configuration", "Release" if archive else "Debug",
               "-sdk", "iphoneos", "-destination", "generic/platform=iOS", "-derivedDataPath", str(BUILD),
               "-allowProvisioningUpdates", "-allowProvisioningDeviceRegistration", f"DEVELOPMENT_TEAM={config['team']}",
               "CODE_SIGN_STYLE=Automatic", "CODE_SIGN_IDENTITY=Apple Development",
               f"MARKETING_VERSION={version}", f"CURRENT_PROJECT_VERSION={number}"]
    if archive:
        command.extend(["-archivePath", str(ARCHIVE), "archive"])
    else:
        command.append("build")
    xcode(command, "archive.log" if archive else "build.log")
    app = ARCHIVE / "Products/Applications/Coconut.app" if archive else APP
    verify_app(config, app)
    return app


def verify_app(config: dict, app: Path) -> None:
    if not app.is_dir():
        raise SystemExit("No signed device build exists. Run update first.")
    run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True)
    signature = run(["codesign", "-d", "--verbose=4", str(app)], capture_output=True, text=True).stderr
    if f"TeamIdentifier={config['team']}" not in signature.splitlines():
        raise SystemExit("The built app does not match the saved signing team.")
    info = plistlib.loads((app / "Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != config["bundle_id"]:
        raise SystemExit("The built app does not match the saved bundle ID.")
    for extension in (app / "PlugIns").glob("*.appex"):
        extension_info = plistlib.loads((extension / "Info.plist").read_bytes())
        if not extension_info.get("CFBundleDisplayName"):
            raise SystemExit("The notification extension needs a display name before upload.")
        if extension_info.get("CFBundleVersion") != info["CFBundleVersion"]:
            raise SystemExit("The app and notification extension must use the same build number.")
    profile_data = run(["security", "cms", "-D", "-i", str(app / "embedded.mobileprovision")], capture_output=True).stdout
    profile = plistlib.loads(profile_data)
    expiry = profile["ExpirationDate"].replace(tzinfo=timezone.utc)
    if expiry <= datetime.now(timezone.utc):
        raise SystemExit("The built app's provisioning profile has expired.")
    if config["team"] not in profile.get("TeamIdentifier", []):
        raise SystemExit("The embedded profile does not match the saved team.")
    if config.get("device_udid") and config["device_udid"] not in profile.get("ProvisionedDevices", []):
        raise SystemExit("The profile does not include the selected iPhone.")
    config["profile_expires"] = expiry.isoformat()
    config["built_version"] = str(info["CFBundleShortVersionString"])
    config["built_number"] = str(info["CFBundleVersion"])
    save_config(config)
    print(f"Verified Coconut {config['built_version']} ({config['built_number']}). Profile expires {expiry.date()}.")


def install(config: dict, skip_build: bool) -> None:
    app = APP if skip_build else build(config)
    if skip_build:
        verify_app(config, app)
    print("Installing on the paired iPhone. Keep it connected and unlocked.", flush=True)
    device_command(["device", "install", "app", "--device", config["device"], str(app)], timeout=120)
    config["installed_build_number"] = config["built_number"]
    config["installed_at"] = datetime.now(timezone.utc).isoformat()
    save_config(config)
    print("Coconut installed. Opening the app.", flush=True)
    device_command(["device", "process", "launch", "--device", config["device"],
                    "--terminate-existing", config["bundle_id"]])
    print("Coconut launched on the iPhone. Its Mac pairing remains in the app.")


def archive(config: dict) -> None:
    build(config, archive=True)
    EXPORT_OPTIONS.write_bytes(plistlib.dumps({"method": "app-store-connect", "destination": "export",
                                               "teamID": config["team"], "signingStyle": "automatic",
                                               "manageAppVersionAndBuildNumber": False}))
    print(f"Local archive: {ARCHIVE}. No build was uploaded.")


def testflight_notes(config: dict, build: int, previous: str | None, apply: bool) -> str:
    """Preview the notes a tester reads for a build, and write them when an App Store Connect key is at hand.

    The App Store Connect API reports when the build before this one arrived, so with a key the note covers
    exactly the commits a tester has not seen. Without one, fall back to the time of the last upload this
    machine recorded.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import testflight as flight
    key, key_id, issuer = (os.environ.get(name) for name in ("ASC_KEY_PATH", "ASC_KEY_ID", "ASC_ISSUER_ID"))
    if key and key_id and issuer:
        bearer = flight.token(Path(key), key_id, issuer)
        text, entries = flight.notes_for(bearer, build, None)
    else:
        bearer = None
        entries = flight.user_facing_since(previous) if previous else []
        text = flight.compose(entries)
    reason = "no commit since the last upload changes something a tester can see" if not entries else f"{len(entries)} change(s)"
    print(f"\nWhat TestFlight shows for build {build} ({reason}):\n\n{text}\n", flush=True)
    if apply and bearer:
        flight.set_notes(bearer, build, text)
        print(f"App Store Connect keeps these notes for build {build}.")
    elif apply:
        print("Set ASC_KEY_PATH, ASC_KEY_ID and ASC_ISSUER_ID to write these notes to App Store Connect.")
    return text


def testflight(config: dict, skip_build: bool = False) -> None:
    """Upload a build restricted to internal TestFlight testing."""
    if config["team"] != "66LJACJ2U7" or config["bundle_id"] != BUNDLE_ID:
        raise SystemExit("TestFlight requires Coconut's Gumroad team and bundle ID.")
    app = ARCHIVE / "Products/Applications/Coconut.app"
    if skip_build:
        verify_app(config, app)
    else:
        build(config, archive=True)
    info = plistlib.loads((app / "Info.plist").read_bytes())
    if info.get("CoconutPushEnvironment") != "production":
        raise SystemExit("TestFlight requires a Release archive with production notifications.")
    if info.get("ITSAppUsesNonExemptEncryption") is not False:
        raise SystemExit("The archive is missing Coconut's encryption declaration. Rebuild it first.")
    TESTFLIGHT_OPTIONS.write_bytes(plistlib.dumps({
        "method": "app-store-connect", "destination": "upload", "teamID": config["team"],
        "signingStyle": "automatic", "manageAppVersionAndBuildNumber": False,
        "testFlightInternalTestingOnly": True, "uploadSymbols": True,
    }))
    previous = config.get("testflight_uploaded_at")
    build_number = int(info["CFBundleVersion"])
    # Show the notes before the upload so a person can stop it, then write them once Apple lists the build.
    testflight_notes(config, build_number, previous, apply=False)
    print("Uploading to internal TestFlight. Apple still controls processing and tester access.", flush=True)
    xcode(["-exportArchive", "-archivePath", str(ARCHIVE), "-exportOptionsPlist", str(TESTFLIGHT_OPTIONS),
           "-exportPath", str(BUILD / "testflight"), "-allowProvisioningUpdates"], "testflight.log")
    config["testflight_uploaded_build"] = str(info["CFBundleVersion"])
    config["testflight_uploaded_at"] = datetime.now(timezone.utc).isoformat()
    save_config(config)
    testflight_notes(config, build_number, previous, apply=True)
    print(f"Uploaded build {info['CFBundleVersion']}. Check processing and the one-person group in App Store Connect.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    setup = commands.add_parser("configure")
    setup.add_argument("--team", required=True)
    setup.add_argument("--device", required=True)
    setup.add_argument("--bundle-id", default=BUNDLE_ID)
    setup.add_argument("--version", default="date", help="Use date (UTC) or an explicit version such as 1.0.0.")
    for name in ["install", "update"]:
        command = commands.add_parser(name)
        command.add_argument("--skip-build", action="store_true", help="Retry installation of the current signed build.")
    commands.add_parser("archive")
    commands.add_parser("export")
    beta = commands.add_parser("testflight", help="Upload a Release build for internal TestFlight testing only.")
    beta.add_argument("--skip-build", action="store_true", help="Upload the existing Release archive.")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            doctor()
            return
        with update_lock():
            if args.command == "configure":
                configure(args)
            elif args.command in ["install", "update"]:
                install(load_config(), args.skip_build)
            elif args.command == "archive":
                archive(load_config())
            elif args.command == "testflight":
                testflight(load_config(), args.skip_build)
            elif args.command == "export":
                if not ARCHIVE.exists() or not EXPORT_OPTIONS.exists():
                    raise SystemExit("Create the local archive first: python3 scripts/iphone.py archive")
                xcode(["-exportArchive", "-archivePath", str(ARCHIVE), "-exportOptionsPlist", str(EXPORT_OPTIONS),
                       "-exportPath", str(BUILD / "export"), "-allowProvisioningUpdates"], "export.log")
                print("The IPA is ready under .build/iphone/export. No build was uploaded.")
    except subprocess.TimeoutExpired:
        raise SystemExit("The iPhone did not respond. Connect it by cable and unlock it before retrying.") from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"The iPhone command failed with exit code {error.returncode}.") from None


if __name__ == "__main__":
    main()
