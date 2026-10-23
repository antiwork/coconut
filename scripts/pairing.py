#!/usr/bin/env python3
"""Open a private, one-time setup QR for the iPhone."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import host


ROOT = Path(__file__).resolve().parents[1]


def validate(payload: dict) -> dict:
    fields = {"version", "server_url", "certificate_sha256", "pairing_code"}
    if not isinstance(payload, dict) or set(payload) != fields or type(payload["version"]) is not int or payload["version"] != 1:
        raise ValueError("The setup data uses an unsupported format.")
    address = host.address_parts(payload["server_url"])[0]
    pin, code = payload["certificate_sha256"], payload["pairing_code"]
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", pin):
        raise ValueError("The certificate fingerprint is invalid.")
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{10}", code):
        raise ValueError("The pairing code is invalid.")
    return dict(version=1, server_url=address, certificate_sha256=pin.lower(), pairing_code=code)


def local_payload() -> dict:
    data = ROOT / ".runtime/host"
    connection = data / "connection.json"
    if not connection.is_file():
        raise SystemExit("Configure the Mac address with host.py setup --address before pairing.")
    address = host.address_parts(json.loads(connection.read_text())["address"])[0]
    hostname = host.address_parts(address)[1]
    host.verify_certificate(data / "server.crt", data / "server.key", hostname)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/host.py"), "pair"],
                            check=True, capture_output=True, text=True)
    return validate(dict(version=1, server_url=address,
                         certificate_sha256=host.fingerprint(data / "server.crt"),
                         pairing_code=result.stdout.strip()))


def link(payload: dict) -> str:
    """A setup link opens Coconut on the iPhone. The QR code holds the same link."""
    data = json.dumps(validate(payload), separators=(",", ":")).encode()
    return "coconut://pair?setup=" + base64.urlsafe_b64encode(data).decode().rstrip("=")


def render(payload: dict, *, show: bool = True, share: bool = False) -> Path:
    payload = validate(payload)
    # A shared code opens Coconut from a tapped link or from the Camera app. The Mac's own QR keeps
    # the JSON format, which earlier app builds also read.
    setup_link = link(payload) if share else None
    directory = ROOT / ".runtime/pairing"
    for path in (ROOT / ".runtime", directory):
        if path.is_symlink():
            raise SystemExit("The pairing folder must not be a symbolic link.")
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)
    output = directory / "iphone-setup.png"
    if output.is_symlink():
        raise SystemExit("The setup image must not be a symbolic link.")
    command = ["swift", str(ROOT / "scripts/pairing-qr.swift"), str(output)]
    subprocess.run(command, input=(setup_link or json.dumps(payload)).encode(), check=True, stdout=subprocess.DEVNULL)
    output.chmod(0o600)
    if show:
        subprocess.run(["open", str(output)], check=True)
    if share:
        # A Hermes agent sends these to the person. The link and the image contain a one-time code.
        print("Setup link: " + setup_link)
        print("QR image: " + str(output))
        print("On the iPhone, tap the link, or scan the QR image in Coconut.")
    else:
        print("Open Coconut on the iPhone, then tap Scan.")
    print("This setup code expires after five minutes. Generate another code if it expires.")
    return output


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdin", action="store_true", help="Read setup JSON privately from standard input.")
    parser.add_argument("--no-open", action="store_true", help="Create the QR without opening Preview.")
    parser.add_argument("--share", action="store_true",
                        help="Print the setup link and the QR image path, so a Hermes agent can send them.")
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin) if args.stdin else local_payload()
        render(payload, show=not args.no_open and not args.share, share=args.share)
    except (ValueError, OSError, subprocess.CalledProcessError):
        raise SystemExit("Coconut could not create the setup QR. Check the host and Xcode Command Line Tools.") from None


if __name__ == "__main__":
    main()
