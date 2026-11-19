#!/usr/bin/env python3
"""Open a private, one-time setup QR for the iPhone.

The setup is for the relay transport of the tunnel sidecar (scripts/tunnel.py): a version 2 setup in a version 3 link.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from urllib.parse import urlsplit

import host


ROOT = Path(__file__).resolve().parents[1]
V2_FIELDS = {"version", "relay", "route", "mac_key", "secret", "expires", "name"}
LINK_VERSION = 3


def unpadded(value: str, size: int) -> bytes | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return None
    data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    return data if len(data) == size else None


def validate(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != V2_FIELDS or type(payload["version"]) is not int or payload["version"] != 2:
        raise ValueError("The setup data uses an unsupported format.")
    relay, route, name, expires = payload["relay"], payload["route"], payload["name"], payload["expires"]
    # Plain HTTP is only for a relay on this Mac.
    if not isinstance(relay, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?|http://127\.0\.0\.1:[0-9]+", relay):
        raise ValueError("The relay address is invalid.")
    if not isinstance(route, str) or not re.fullmatch(r"[0-9a-f]{64}", route):
        raise ValueError("The route is invalid.")
    if unpadded(payload["mac_key"], 32) is None or unpadded(payload["secret"], 32) is None:
        raise ValueError("The Mac key or the pairing secret is invalid.")
    if type(expires) is not int or not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("The expiry or the Mac name is invalid.")
    return dict(payload, name=name.strip())


def local_payload() -> dict:
    data = host.DEFAULT_DATA
    if not (data / "tunnel/route.json").is_file():
        raise SystemExit("Install the relay tunnel with scripts/setup_coconut.py before pairing.")
    return tunnel_payload(data)


def hkdf(secret: bytes, label: str) -> bytes:
    """HKDF-SHA256 with an empty salt and one 32-byte output block. The sidecar and the iPhone use the same labels."""
    key = hmac.new(bytes(32), secret, hashlib.sha256).digest()
    return hmac.new(key, label.encode() + b"\x01", hashlib.sha256).digest()


def tunnel_ticket(secret: str) -> tuple[str, str]:
    """Return the relay ticket hash and the Noise pre-shared key of a pairing secret.

    The iPhone connects to the relay with `Authorization: Ticket <ticket>`, and the relay stores only the hash.
    """
    data = unpadded(secret, 32)
    ticket = hkdf(data, "coconut pairing ticket").hex()
    return hashlib.sha256(ticket.encode()).hexdigest(), hkdf(data, "coconut pairing psk").hex()


def mac_name() -> str:
    result = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, check=False)
    return (result.stdout.strip() or socket.gethostname().removesuffix(".local"))[:80]


def tunnel_payload(data: Path) -> dict:
    """Create a version 2 setup: a 32-byte secret for the backend, and its ticket for the sidecar.

    The backend stores only the hash of the secret. The sidecar gets the ticket hash and the pre-shared key in
    tunnel/tickets, and sends the ticket hash to the relay.
    """
    route = json.loads((data / "tunnel/route.json").read_text())
    result = subprocess.run([sys.executable, "-m", "coconut", "--data-dir", str(data), "--pair", "--tunnel"],
                            cwd=ROOT / "backend", check=True, capture_output=True, text=True)
    secret, expires = result.stdout.split()
    expires = int(expires)
    ticket_hash, psk = tunnel_ticket(secret)
    tickets = data / "tunnel/tickets"
    if tickets.is_symlink():
        raise SystemExit("The tunnel folder must not be a symbolic link.")
    host.private_directory(tickets)
    path = tickets / f"{ticket_hash}.json"
    temporary = tickets / f".{ticket_hash}.tmp"
    temporary.write_text(json.dumps({"psk": psk, "expires": expires}))
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return validate(dict(version=2, relay=route["relay"], route=route["route"], mac_key=route["mac_key"],
                            secret=secret, expires=expires, name=mac_name()))


def link(payload: dict) -> str:
    """A setup link opens Coconut on the iPhone. The QR code holds the same link.

    A version 2 setup goes in a version 3 link: coconut://pair/<token>. The token is the packed setup in lowercase
    base32. Hermes masks text that looks like a secret before its agent sends the link,
    and no Hermes rule matches lowercase letters and the digits 2 to 7 after a slash.
    """
    payload = validate(payload)
    token = base64.b32encode(pack(payload)).decode().rstrip("=").lower()
    return f"coconut://pair/{token}"


def pack(payload: dict) -> bytes:
    """Link version, route, Mac key, one-time code, expiry, relay length (2 bytes), relay, and the Mac name last.

    Any relay that validate_v2 accepts fits: a host name has at most 253 characters.
    """
    relay, name = payload["relay"].encode(), payload["name"].encode()
    if not 0 <= payload["expires"] < 2**32 or len(relay) >= 2**16:
        raise ValueError("The expiry or the relay address is out of range.")
    return (bytes([LINK_VERSION]) + bytes.fromhex(payload["route"]) + unpadded(payload["mac_key"], 32)
            + unpadded(payload["secret"], 32) + payload["expires"].to_bytes(4, "big") + len(relay).to_bytes(2, "big") + relay + name)


def read_link(text: str) -> dict:
    """Return the setup of a link from link()."""
    parts = urlsplit(text.strip())
    if parts.scheme != "coconut" or parts.netloc != "pair":
        raise ValueError("This is not a Coconut setup link.")
    token = parts.path.removeprefix("/")
    if not re.fullmatch(r"[a-z2-7]+", token):
        raise ValueError("The setup link is damaged.")
    data = base64.b32decode(token.upper() + "=" * (-len(token) % 8))
    size = int.from_bytes(data[101:103], "big")
    if len(data) < 103 or data[0] != LINK_VERSION or len(data) < 103 + size:
        raise ValueError("The setup link is damaged.")
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return validate(dict(version=2, route=data[1:33].hex(), mac_key=encode(data[33:65]), secret=encode(data[65:97]),
                            expires=int.from_bytes(data[97:101], "big"), relay=data[103:103 + size].decode(),
                            name=data[103 + size:].decode()))


def render(payload: dict, *, show: bool = True, share: bool = False) -> Path:
    # The code opens Coconut from a tapped link or from the Camera app.
    setup_link = link(payload)
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
    subprocess.run(command, input=setup_link.encode(), check=True, stdout=subprocess.DEVNULL)
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
    print("This setup code expires after ten minutes. Generate another code if it expires.")
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
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError):
        raise SystemExit("Coconut could not create the setup QR. Check the host and Xcode Command Line Tools.") from None


if __name__ == "__main__":
    main()
