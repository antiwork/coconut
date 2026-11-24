#!/usr/bin/env python3
"""Steps of the TestFlight release of the iPhone app.

The workflow archives Coconut without a signature. This script signs the archive ad hoc with the entitlements of
the project. Then xcodebuild exports the archive with automatic signing: Xcode makes the App Store profiles and signs
with a cloud-managed distribution certificate through the App Store Connect API key. No certificate is in a secret.

  testflight.py next-build --key AuthKey.p8 --key-id KEY --issuer ISSUER
  testflight.py sign --archive Coconut.xcarchive --build 42
  testflight.py export-options --output ExportOptions.plist
  testflight.py wait --key AuthKey.p8 --key-id KEY --issuer ISSUER --build 42
  testflight.py notes --key AuthKey.p8 --key-id KEY --issuer ISSUER --build 42 --apply

The script uses only the standard library and the openssl command.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import base64
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TEAM = '66LJACJ2U7'
BUNDLE_ID = 'com.gumroad.coconut'
# The App Store Connect app "Coconut Work".
APP_ID = '6810507745'
API = 'https://api.appstoreconnect.apple.com/v1'
# The entitlements of each signed bundle in the archive, relative to the app. Each comes from ios/project.yml.
ENTITLEMENTS = {
    'PlugIns/CoconutNotificationService.appex': ROOT / 'ios/CoconutNotificationService/CoconutNotificationService.entitlements',
    'PlugIns/CoconutShare.appex': ROOT / 'ios/CoconutShare/CoconutShare.entitlements',
    '.': ROOT / 'ios/Coconut/Coconut.entitlements',
}
# The build settings that the entitlements use, for a Release build of the Gumroad team.
SETTINGS = {'APS_ENVIRONMENT': 'production', 'AppIdentifierPrefix': TEAM + '.'}
# The Info.plist key that names the keychain group of the app and the notification extension, and its value in
# ios/project.yml. An archive without a signature has no team, so Xcode writes the value without the team prefix.
# Each bundle then asks the keychain for a group that its entitlements do not have, and iOS refuses the request.
KEYCHAIN_GROUP_KEY = 'CoconutPushKeychainGroup'
KEYCHAIN_GROUP = '$(AppIdentifierPrefix)com.gumroad.coconut.push'
# TestFlight shows the "whatsNew" text of a beta build localization as "What to Test". Apple cuts the field off
# without complaint far past this size, but a person reads it on a phone, so keep it short.
NOTES_LOCALE = 'en-US'
NOTES_LIMIT = 1800
# The newest changes whose own line a tester should act on first.
TRY_FIRST = 5
# A commit that only touches these never changes what a tester sees.
NOT_USER_FACING = ('docs/', '.github/', 'scripts/test_', '.gitignore', 'qa-media/', 'ios/CoconutTests/',
                   'ios/CoconutUITests/', 'CONTRIBUTING.md', 'README.md', 'AGENTS.md', 'CLAUDE.md', 'app-store/',
                   'scripts/appstore.py')
NOT_USER_FACING_SUFFIXES = ('.md',)


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
    """A JSON Web Token for the App Store Connect API. It expires after 19 minutes. Apple accepts at most 20, and refuses
    a token signed for exactly 20 when the clock of the runner is a second ahead of Apple's."""
    now = int(time.time()) if now is None else now
    header = base64url(json.dumps({'alg': 'ES256', 'kid': key_id, 'typ': 'JWT'}).encode())
    claims = base64url(json.dumps({'iss': issuer, 'iat': now, 'exp': now + 19 * 60, 'aud': 'appstoreconnect-v1'}).encode())
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


def expand_text(text: str) -> str:
    return re.sub(r'\$\((\w+)\)', lambda match: SETTINGS[match.group(1)], text)


def expand(entitlements: Path) -> bytes:
    expanded = expand_text(entitlements.read_text())
    plistlib.loads(expanded.encode())
    return expanded.encode()


def set_keychain_group(bundle: Path, entitlements: bytes) -> None:
    """Write the keychain group with the team prefix into the Info.plist of the bundle, before its signature seals it."""
    group = expand_text(KEYCHAIN_GROUP)
    if group not in plistlib.loads(entitlements).get('keychain-access-groups', []):
        raise SystemExit(f'The entitlements of {bundle.name} do not have the keychain group {group}.')
    path = bundle / 'Info.plist'
    data = path.read_bytes()
    info = plistlib.loads(data)
    if info.get(KEYCHAIN_GROUP_KEY) != group:
        info[KEYCHAIN_GROUP_KEY] = group
        path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY if data.startswith(b'bplist') else plistlib.FMT_XML))


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
        expanded = expand(entitlements)
        set_keychain_group(bundle, expanded)
        # The extension comes first: the signature of the app seals the signature of each extension.
        with tempfile.NamedTemporaryFile(suffix='.entitlements') as file:
            file.write(expanded)
            file.flush()
            subprocess.run(['codesign', '--force', '--sign', '-', '--generate-entitlement-der',
                            '--entitlements', file.name, str(bundle)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    print(f'Signed Coconut {info["CFBundleShortVersionString"]} ({build}) ad hoc with its entitlements.')


def export_options(public: bool = False) -> bytes:
    """`public` lets the build go to the public TestFlight link and to App Review, not only to internal testers."""
    return plistlib.dumps({
        'method': 'app-store-connect', 'destination': 'upload', 'teamID': TEAM, 'signingStyle': 'automatic',
        'manageAppVersionAndBuildNumber': False, 'testFlightInternalTestingOnly': not public, 'uploadSymbols': True,
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


def request(method: str, path: str, bearer: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {'Authorization': 'Bearer ' + bearer, 'Content-Type': 'application/json'}
    call = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(call, timeout=60) as response:
        text = response.read()
    return json.loads(text) if text else {}


def builds(bearer: str) -> list[dict]:
    """The newest builds of the app in App Store Connect, newest first."""
    query = urllib.parse.urlencode({'filter[app]': APP_ID, 'fields[builds]': 'version,uploadedDate,processingState',
                                    'limit': 20, 'sort': '-uploadedDate'})
    return get('/builds?' + query, bearer).get('data', [])


def git(*arguments: str) -> str:
    result = subprocess.run(['git', *arguments], cwd=ROOT, capture_output=True, text=True, check=True)
    return result.stdout


def is_moment(text: str) -> bool:
    return bool(re.match(r'^\d{4}-\d{2}-\d{2}', text))


def commits(window: str | None, limit: int = 40) -> list[tuple[str, str]]:
    """The commits after a moment or a ref, newest first, as (sha, subject). Without one: the last `limit` commits."""
    if not window:
        revision = [f'-{limit}']
    elif is_moment(window):
        revision = [f'--since={window}']
    else:
        revision = [f'{window}..HEAD']
    log = git('log', '--no-merges', '--format=%h %s', 'HEAD', *revision)
    entries = []
    for line in log.splitlines():
        sha, _, subject = line.partition(' ')
        if sha and subject:
            entries.append((sha, subject.strip()))
    return entries


def changed_paths(sha: str) -> list[str]:
    return [line.strip() for line in git('show', '--name-only', '--format=', sha).splitlines() if line.strip()]


def user_facing(paths: list[str]) -> bool:
    """True when a commit touches something a tester can see; docs, CI and test files never count."""
    for path in paths:
        if path.startswith(NOT_USER_FACING) or path.endswith(NOT_USER_FACING_SUFFIXES):
            continue
        return True
    return False


def compose(entries: list[str]) -> str:
    """The text TestFlight shows as "What to Test": the newest changes to try, then the full list of what is new."""
    if not entries:
        return 'Bug fixes and improvements.'
    try_first = entries[:TRY_FIRST]
    listed, dropped = entries, 0
    while True:
        notes = ('What to test\n' + '\n'.join(f'• {subject}' for subject in try_first)
                 + "\n\nWhat's new\n" + '\n'.join(f'• {subject}' for subject in listed))
        if dropped:
            notes += f'\n• …and {dropped} more in the changelog.'
        if len(notes) <= NOTES_LIMIT or len(listed) == 1:
            return notes
        listed, dropped = listed[:-1], dropped + 1


def user_facing_since(window: str) -> list[str]:
    """The subjects of the commits a tester has not seen: those after `window`, a git moment or a ref."""
    if not is_moment(window):
        # A ref the checkout does not have would silently turn the range into the whole history.
        subprocess.run(['git', 'rev-parse', '--verify', '--quiet', window + '^{commit}'], cwd=ROOT, check=True,
                       capture_output=True)
    return [subject for sha, subject in commits(window) if user_facing(changed_paths(sha))]


def notes_for(bearer: str, build: int | None, since: str | None) -> tuple[str, list[str]]:
    """The preview text for a build, and the commits behind it.

    The baseline is the commit the previous build was made from when the caller knows it. Otherwise App Store
    Connect only says when the previous build arrived, so a run that started before the one before it leaves no
    commits in that window; walk back over the earlier builds until a window holds a change a tester can see.
    """
    if since:
        entries = user_facing_since(since)
        return compose(entries), entries
    earlier = [item['attributes'].get('uploadedDate') for item in builds(bearer)
               if not build or str(item['attributes'].get('version')) != str(build)]
    for moment in earlier[:6]:
        if not moment:
            continue
        entries = user_facing_since(moment)
        if entries:
            return compose(entries), entries
    return compose([]), []


def build_id(bearer: str, build: int) -> str | None:
    query = urllib.parse.urlencode({'filter[app]': APP_ID, 'filter[version]': str(build), 'limit': 1})
    document = get('/builds?' + query, bearer)
    for item in document.get('data', []):
        return item['id']
    return None


def set_notes(bearer: str, build: int, text: str, minutes: int = 30) -> str | None:
    """Write the notes onto the build. Apple refuses a build that is still processing, so retry until it is listed."""
    deadline = time.time() + minutes * 60
    identifier = None
    while identifier is None:
        identifier = build_id(bearer, build)
        if identifier:
            break
        if time.time() > deadline:
            raise SystemExit(f'App Store Connect does not list build {build} after {minutes} minutes, so its notes are not set.')
        time.sleep(30)
    localizations = get(f'/builds/{identifier}/betaBuildLocalizations', bearer).get('data', [])
    current = next((item for item in localizations if item['attributes'].get('locale') == NOTES_LOCALE), None)
    while True:
        try:
            if current:
                request('PATCH', f'/betaBuildLocalizations/{current["id"]}', bearer,
                        {'data': {'type': 'betaBuildLocalizations', 'id': current['id'],
                                  'attributes': {'whatsNew': text}}})
            else:
                request('POST', '/betaBuildLocalizations', bearer,
                        {'data': {'type': 'betaBuildLocalizations',
                                  'attributes': {'locale': NOTES_LOCALE, 'whatsNew': text},
                                  'relationships': {'build': {'data': {'type': 'builds', 'id': identifier}}}}})
            break
        except urllib.error.HTTPError as error:
            # 409 means the build is still processing; anything else is a real refusal.
            if error.code != 409 or time.time() > deadline:
                raise
            time.sleep(30)
    written = get(f'/builds/{identifier}/betaBuildLocalizations', bearer).get('data', [])
    saved = next((item['attributes'].get('whatsNew') for item in written
                  if item['attributes'].get('locale') == NOTES_LOCALE), None)
    if saved != text:
        raise SystemExit('App Store Connect did not keep the notes it was given, so they are not set.')
    return saved


def notes(bearer: str, build: int | None, since: str | None, apply: bool) -> None:
    text, entries = notes_for(bearer, build, since)
    if not entries:
        print('No commit since the previous build changes something a tester can see.\n')
    else:
        print(f'{len(entries)} change(s) for the notes.\n')
    print(text)
    if apply:
        if not build:
            raise SystemExit('Pass --build with --apply so the notes land on the right build.')
        set_notes(bearer, build, text)
        print(f'\nApp Store Connect keeps these notes for build {build}.')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as file:
            file.write(f'### What to Test for build {build or "the next build"}\n\n{text}\n\n')


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
    options.add_argument('--public', action='store_true', help='Allow public TestFlight testing and App Review.')
    notes_command = commands.add_parser('notes', help='Preview the release notes and what to test, or write them to a build.')
    notes_command.add_argument('--key', type=Path, required=True)
    notes_command.add_argument('--key-id', required=True)
    notes_command.add_argument('--issuer', required=True)
    notes_command.add_argument('--build', type=int, help='The build the notes belong to. Default: the next build.')
    notes_command.add_argument('--since', help='A git moment to baseline on, instead of the previous upload.')
    notes_command.add_argument('--apply', action='store_true', help='Write the notes to App Store Connect.')
    args = parser.parse_args()
    if args.command == 'next-build':
        print(next_build(token(args.key, args.key_id, args.issuer)))
    elif args.command == 'wait':
        wait(args.key, args.key_id, args.issuer, args.build, args.minutes)
    elif args.command == 'sign':
        sign(args.archive, args.build)
    elif args.command == 'notes':
        notes(token(args.key, args.key_id, args.issuer), args.build, args.since, args.apply)
    else:
        args.output.write_bytes(export_options(args.public))


if __name__ == '__main__':
    sys.exit(main())
