#!/usr/bin/env python3
"""Steps of the TestFlight release of the iPhone app (.github/workflows/testflight.yml).

The workflow archives Coconut without a signature. This script signs the archive ad hoc with the entitlements of
the project. Then xcodebuild exports the archive with automatic signing: Xcode makes the App Store profiles and signs
with a cloud-managed distribution certificate through the App Store Connect API key. No certificate is in a secret.

  testflight.py next-build --key AuthKey.p8 --key-id KEY --issuer ISSUER
  testflight.py sign --archive Coconut.xcarchive --build 42
  testflight.py export-options --output ExportOptions.plist
  testflight.py wait --key AuthKey.p8 --key-id KEY --issuer ISSUER --build 42

The script uses only the standard library and the openssl command.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import base64
import json
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TEAM = '66LJACJ2U7'
BUNDLE_ID = 'com.gumroad.coconut'
# The App Store Connect app "Coconut for Hermes".
APP_ID = '6810507745'
API = 'https://api.appstoreconnect.apple.com/v1'
# The entitlements of each signed bundle in the archive, relative to the app. Each comes from ios/project.yml.
ENTITLEMENTS = {
    'PlugIns/CoconutNotificationService.appex': ROOT / 'ios/CoconutNotificationService/CoconutNotificationService.entitlements',
    '.': ROOT / 'ios/Coconut/Coconut.entitlements',
}
# The build settings that the entitlements use, for a Release build of the Gumroad team.
SETTINGS = {'APS_ENVIRONMENT': 'production', 'AppIdentifierPrefix': TEAM + '.'}


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def der_to_raw(signature: bytes) -> bytes:
    """Change an ECDSA signature from DER (openssl) to r || s (JWT ES256)."""
    def integer(data: bytes, offset: int) -> tuple[bytes, int]:
        if data[offset] != 0x02:
            raise ValueError('The signature is not DER.')
        length = data[offset + 1]
        value = data[offset + 2:offset + 2 + length]
        return value.lstrip(b'\0').rjust(32, b'\0'), offset + 2 + length

    if signature[0] != 0x30:
        raise ValueError('The signature is not DER.')
    offset = 3 if signature[1] & 0x80 else 2
    r, offset = integer(signature, offset)
    s, _ = integer(signature, offset)
    if len(r) != 32 or len(s) != 32:
        raise ValueError('The signature is not a P-256 signature.')
    return r + s


def token(key: Path, key_id: str, issuer: str, now: int | None = None) -> str:
    """A JSON Web Token for the App Store Connect API. It expires after 20 minutes, the longest time Apple accepts."""
    now = int(time.time()) if now is None else now
    header = base64url(json.dumps({'alg': 'ES256', 'kid': key_id, 'typ': 'JWT'}).encode())
    claims = base64url(json.dumps({'iss': issuer, 'iat': now, 'exp': now + 20 * 60, 'aud': 'appstoreconnect-v1'}).encode())
    message = f'{header}.{claims}'
    signature = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(key)], input=message.encode(),
                               capture_output=True, check=True).stdout
    return f'{message}.{base64url(der_to_raw(signature))}'


def get(path: str, bearer: str) -> dict:
    request = urllib.request.Request(API + path, headers={'Authorization': 'Bearer ' + bearer})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def build_numbers(bearer: str) -> list[int]:
    """The build numbers of every build of the app in App Store Connect, also of the expired builds."""
    query = urllib.parse.urlencode({'filter[app]': APP_ID, 'fields[builds]': 'version', 'limit': 200})
    path = '/builds?' + query
    numbers = []
    while path:
        document = get(path, bearer)
        numbers.extend(int(build['attributes']['version']) for build in document.get('data', [])
                       if str(build['attributes'].get('version', '')).isdigit())
        following = document.get('links', {}).get('next')
        path = following.removeprefix(API) if following else ''
    return numbers


def next_build(bearer: str) -> int:
    return max(build_numbers(bearer), default=0) + 1


def expand(entitlements: Path) -> bytes:
    text = entitlements.read_text()
    expanded = re.sub(r'\$\((\w+)\)', lambda match: SETTINGS[match.group(1)], text)
    plistlib.loads(expanded.encode())
    return expanded.encode()


def sign(archive: Path, build: int) -> None:
    app = archive / 'Products/Applications/Coconut.app'
    info = plistlib.loads((app / 'Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != BUNDLE_ID:
        raise SystemExit(f'The archive must hold {BUNDLE_ID}.')
    if info.get('CoconutPushEnvironment') != 'production':
        raise SystemExit('TestFlight requires a Release archive with production notifications.')
    if info.get('ITSAppUsesNonExemptEncryption') is not False:
        raise SystemExit("The archive is missing Coconut's encryption declaration.")
    bundles = sorted(path.relative_to(app).as_posix() for path in app.glob('PlugIns/*.appex'))
    if bundles != [name for name in ENTITLEMENTS if name != '.']:
        raise SystemExit(f'The archive has unexpected extensions: {bundles}. Add their entitlements to testflight.py.')
    for name, entitlements in ENTITLEMENTS.items():
        bundle = app / name
        bundle_info = plistlib.loads((bundle / 'Info.plist').read_bytes())
        if bundle_info.get('CFBundleVersion') != str(build):
            raise SystemExit(f'{name} has build {bundle_info.get("CFBundleVersion")}, not {build}.')
        # The extension comes first: the signature of the app seals the signature of each extension.
        with tempfile.NamedTemporaryFile(suffix='.entitlements') as file:
            file.write(expand(entitlements))
            file.flush()
            subprocess.run(['codesign', '--force', '--sign', '-', '--generate-entitlement-der',
                            '--entitlements', file.name, str(bundle)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    print(f'Signed Coconut {info["CFBundleShortVersionString"]} ({build}) ad hoc with its entitlements.')


def export_options() -> bytes:
    return plistlib.dumps({
        'method': 'app-store-connect', 'destination': 'upload', 'teamID': TEAM, 'signingStyle': 'automatic',
        'manageAppVersionAndBuildNumber': False, 'testFlightInternalTestingOnly': True, 'uploadSymbols': True,
    })


def wait(key: Path, key_id: str, issuer: str, build: int, minutes: int) -> None:
    """Wait until App Store Connect lists the build, so that the next release does not use the same number."""
    deadline = time.time() + minutes * 60
    while True:
        if build in build_numbers(token(key, key_id, issuer)):
            print(f'App Store Connect lists build {build}.')
            return
        if time.time() > deadline:
            raise SystemExit(f'App Store Connect does not list build {build} after {minutes} minutes.')
        time.sleep(30)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('next-build', 'wait'):
        command = commands.add_parser(name)
        command.add_argument('--key', type=Path, required=True)
        command.add_argument('--key-id', required=True)
        command.add_argument('--issuer', required=True)
        if name == 'wait':
            command.add_argument('--build', type=int, required=True)
            command.add_argument('--minutes', type=int, default=30)
    signing = commands.add_parser('sign')
    signing.add_argument('--archive', type=Path, required=True)
    signing.add_argument('--build', type=int, required=True)
    options = commands.add_parser('export-options')
    options.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'next-build':
        print(next_build(token(args.key, args.key_id, args.issuer)))
    elif args.command == 'wait':
        wait(args.key, args.key_id, args.issuer, args.build, args.minutes)
    elif args.command == 'sign':
        sign(args.archive, args.build)
    else:
        args.output.write_bytes(export_options())


if __name__ == '__main__':
    sys.exit(main())
