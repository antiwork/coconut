#!/usr/bin/env python3
"""Build Coconut.app: the menu bar app, the `coconut` tool, the relay sidecar and the two login items.

The bundle:
  Contents/MacOS/Coconut                      the menu bar app (mac/Sources/CoconutApp)
  Contents/Helpers/coconut                    the tool that the login items run (mac/Sources/CoconutTool)
  Contents/Helpers/tunnel-sidecar             the relay sidecar (scripts/tunnel-sidecar.swift)
  Contents/Frameworks/Sparkle.framework       the updater, which reads FEED_URL
  Contents/Library/LaunchAgents/*.plist       the login items (mac/LaunchAgents)
  Contents/Resources/coconut-source.tar.gz    the source package of scripts/remote.py, which `coconut update` installs
  Contents/Resources/package.json             its package ID
  Contents/Resources/deploy.py                the scripts/deploy.py that installs it
  Contents/Resources/Coconut.icns             the app icon, made from the iPhone icon (scripts/mac-icon.swift)

Each binary is universal (arm64 and x86_64). Without --identity, the bundle has an ad-hoc signature.

A release signs the bundle with the Developer ID Application certificate, notarizes it with `notarytool` and an App
Store Connect API key, staples the ticket, and makes the zip archive that Sparkle downloads:

  mac_app.py build --identity <SHA-1 of the Developer ID identity>
  mac_app.py notarize --key AuthKey.p8 --key-id <id> --issuer <issuer>
  mac_app.py archive

The version is in mac/VERSION. The build number is the commit count of HEAD.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import plistlib
import shutil
import subprocess
import tempfile

import remote
from tunnel import SOURCES as SIDECAR_SOURCES

ROOT = Path(__file__).resolve().parents[1]
MAC = ROOT / 'mac'
BUNDLE_ID = 'com.gumroad.coconut.mac'
MINIMUM_SYSTEM = '15.0'
ARCHITECTURES = ('arm64', 'x86_64')
# The update feed serves each archive as a Workers Static Asset, and Cloudflare accepts at most 25 MiB per file.
ARCHIVE_LIMIT = 25 * 1024 * 1024
# The Sparkle feed: the Worker coconut-updates (updates/) serves the appcast and the archives next to it. This is the only
# place that names it.
FEED_URL = 'https://coconut-updates.gumroad.workers.dev/mac/appcast.xml'
# The public half of the EdDSA key in the secret SPARKLE_ED_PRIVATE_KEY. Sparkle installs only archives that it signed.
ED_PUBLIC_KEY = 'bsZLk/GXxQHmjyBDgQ/RY31DmKYvGBLPmJhzOFCkAWE='
# The iPhone icon is the one source of the app icon.
ICON_SOURCE = ROOT / 'ios/Coconut/Assets.xcassets/CoconutAppIcon.appiconset/CoconutAppIcon.png'
# The app target of ios/project.yml. It compiles the views of the iPhone app with the menu bar code.
PROJECT = ROOT / 'ios/Coconut.xcodeproj'
APP_SCHEME = 'CoconutMac'
# Xcode keeps its build and the downloaded packages here. The release takes sign_update from the Sparkle package.
XCODE_BUILD = MAC / '.build/xcode'


def run(arguments: list, **options) -> subprocess.CompletedProcess:
    return subprocess.run([str(argument) for argument in arguments], check=True, **options)


def version() -> str:
    return (MAC / 'VERSION').read_text().strip()


def build_number() -> str:
    """The number of commits on HEAD. It grows with each merge to main, as Sparkle needs. CI needs the full history."""
    return run(['git', '-C', ROOT, 'rev-list', '--count', 'HEAD'], capture_output=True, text=True).stdout.strip()


def info_plist(version: str, build: str) -> dict:
    return {
        'CFBundleDevelopmentRegion': 'en',
        'CFBundleDisplayName': 'Coconut',
        'CFBundleExecutable': 'Coconut',
        # Finder, System Settings and the login items list show this icon.
        'CFBundleIconFile': 'Coconut',
        'CFBundleIdentifier': BUNDLE_ID,
        'CFBundleInfoDictionaryVersion': '6.0',
        'CFBundleName': 'Coconut',
        'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': version,
        'CFBundleVersion': build,
        'LSApplicationCategoryType': 'public.app-category.productivity',
        'LSMinimumSystemVersion': MINIMUM_SYSTEM,
        # The chat window has a Dock icon and a main menu. The menu bar item shows the service of this Mac.
        'LSUIElement': False,
        # A pairing link from Hermes opens the pairing page of the chat window.
        'CFBundleURLTypes': [{'CFBundleURLName': 'Pairing', 'CFBundleURLSchemes': ['coconut']}],
        'NSHighResolutionCapable': True,
        'SUFeedURL': FEED_URL,
        'SUPublicEDKey': ED_PUBLIC_KEY,
        # Check every six hours, and install an update without a question. The app updates its source after the relaunch.
        'SUEnableAutomaticChecks': True,
        'SUAutomaticallyUpdate': True,
        # Without this key, Sparkle allows automatic installs only while automatic checks are on. Then `coconut update`
        # with automatic updates off made Sparkle ask in a window that nobody saw. See Updater.checkForRequest.
        'SUAllowsAutomaticUpdates': True,
        'SUScheduledCheckInterval': 21600,
        # The relay that keeps a pairing code until the Mac answers it (PairingCode.swift).
        'CoconutPushRelayURL': 'https://coconut-push.gumroad.workers.dev',
    }


def swift_tool(scratch: Path) -> Path:
    """Build the `coconut` tool for both architectures. Return its path."""
    command = ['swift', 'build', '-c', 'release', '--package-path', MAC, '--scratch-path', scratch]
    for architecture in ARCHITECTURES:
        command += ['--arch', architecture]
    run(command + ['--product', 'CoconutTool'])
    return Path(run(command + ['--show-bin-path'], capture_output=True, text=True).stdout.strip()) / 'CoconutTool'


def xcode_app() -> Path:
    """Build the app target for both architectures, without a signature. Return the app that Xcode made.

    sign() signs the finished bundle, so Xcode does not sign here.
    """
    run(['xcodebuild', '-project', PROJECT, '-scheme', APP_SCHEME, '-configuration', 'Release',
         '-destination', 'generic/platform=macOS', '-derivedDataPath', XCODE_BUILD,
         '-clonedSourcePackagesDirPath', XCODE_BUILD / 'packages', f'ARCHS={" ".join(ARCHITECTURES)}',
         'ONLY_ACTIVE_ARCH=NO', 'CODE_SIGNING_ALLOWED=NO', 'build'], stdout=subprocess.DEVNULL)
    return XCODE_BUILD / 'Build/Products/Release/Coconut.app'


def compile_sidecar(output: Path, scratch: Path) -> None:
    slices = []
    for architecture in ARCHITECTURES:
        binary = scratch / f'tunnel-sidecar-{architecture}'
        run(['xcrun', 'swiftc', '-parse-as-library', '-O', '-target', f'{architecture}-apple-macos{MINIMUM_SYSTEM}',
             *SIDECAR_SOURCES, '-o', binary])
        slices.append(binary)
    run(['lipo', '-create', *slices, '-output', output])


def assemble(output: Path, version: str, build: str) -> Path:
    app = output / 'Coconut.app'
    if app.exists():
        shutil.rmtree(app)
    contents = app / 'Contents'
    for folder in ('MacOS', 'Helpers', 'Resources', 'Library/LaunchAgents'):
        (contents / folder).mkdir(parents=True)
    with tempfile.TemporaryDirectory() as temporary:
        scratch = Path(temporary)
        built = xcode_app()
        shutil.copy2(built / 'Contents/MacOS/Coconut', contents / 'MacOS/Coconut')
        # The resources of the app target, such as the text of the Hermes pairing message.
        for resource in sorted((built / 'Contents/Resources').iterdir()):
            copy = shutil.copytree if resource.is_dir() else shutil.copy2
            copy(resource, contents / 'Resources' / resource.name)
        shutil.copy2(swift_tool(MAC / '.build'), contents / 'Helpers/coconut')
        add_sparkle(built / 'Contents/Frameworks/Sparkle.framework', contents / 'Frameworks/Sparkle.framework')
        compile_sidecar(contents / 'Helpers/tunnel-sidecar', scratch)
        make_icon(contents / 'Resources/Coconut.icns', scratch)
    for agent in sorted((MAC / 'LaunchAgents').glob('*.plist')):
        shutil.copy2(agent, contents / 'Library/LaunchAgents' / agent.name)
    bundle_source(contents / 'Resources')
    with (contents / 'Info.plist').open('wb') as handle:
        plistlib.dump(info_plist(version, build), handle)
    (contents / 'PkgInfo').write_text('APPL????')
    return app


def make_icon(output: Path, scratch: Path) -> None:
    iconset = scratch / 'Coconut.iconset'
    run(['xcrun', 'swift', ROOT / 'scripts/mac-icon.swift', ICON_SOURCE, iconset])
    run(['iconutil', '-c', 'icns', iconset, '-o', output])


def add_sparkle(framework: Path, destination: Path) -> None:
    """Copy Sparkle.framework with its symbolic links, without the XPC services.

    Only a sandboxed app needs the XPC services of Sparkle. Coconut.app is not sandboxed.
    """
    shutil.copytree(framework, destination, symlinks=True)
    shutil.rmtree(destination / 'Versions/B/XPCServices', ignore_errors=True)
    (destination / 'XPCServices').unlink(missing_ok=True)


def bundle_source(resources: Path) -> str:
    """Add the source package, its ID and its installer. Return the package ID."""
    metadata = remote.package(resources / 'coconut-source.tar.gz')
    (resources / 'package.json').write_text(json.dumps({'package_id': metadata['package_id']}) + '\n')
    shutil.copy2(ROOT / 'scripts/deploy.py', resources / 'deploy.py')
    return metadata['package_id']


def sign(app: Path, identity: str) -> None:
    """Sign the inner code first, then the app, with the hardened runtime that notarization requires.

    An ad-hoc signature has no Team ID. The library validation of the hardened runtime then refuses to load
    Sparkle.framework, so an ad-hoc build for tests has no hardened runtime.
    """
    if identity == '-':
        options = ['--force', '--sign', identity, '--timestamp=none']
    else:
        options = ['--force', '--options', 'runtime', '--sign', identity, '--timestamp']
    sparkle = app / 'Contents/Frameworks/Sparkle.framework'
    for code in [sparkle / 'Versions/B/Autoupdate', sparkle / 'Versions/B/Updater.app', sparkle,
                 *sorted((app / 'Contents/Helpers').iterdir())]:
        run(['codesign', *options, code])
    run(['codesign', *options, app])
    run(['codesign', '--verify', '--deep', '--strict', app])


def archive(app: Path, destination: Path, limit: int | None = None) -> Path:
    """Zip the app with ditto, which keeps the signature, the stapled ticket and the symbolic links.

    With a limit, a larger archive stops the release.
    """
    destination.unlink(missing_ok=True)
    run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, destination])
    if limit is not None and (size := destination.stat().st_size) > limit:
        raise SystemExit(f'{destination.name} has {size / 1048576:.1f} MiB. The update feed accepts at most '
                         f'{limit / 1048576:.0f} MiB for each file.')
    return destination


def archive_name(app: Path) -> str:
    """Coconut-<version>-<build>.zip."""
    with (app / 'Contents/Info.plist').open('rb') as handle:
        info = plistlib.load(handle)
    return f"Coconut-{info['CFBundleShortVersionString']}-{info['CFBundleVersion']}.zip"


def notarize(app: Path, key: Path, key_id: str, issuer: str) -> None:
    """Send the app to the notary service, wait for the result, and staple the ticket to the app.

    The notary service needs an archive. This one is temporary: the stapled app gets a new archive afterwards.
    """
    with tempfile.TemporaryDirectory() as temporary:
        upload = archive(app, Path(temporary) / 'Coconut.zip')
        credentials = ['--key', key, '--key-id', key_id, '--issuer', issuer]
        result = subprocess.run(['xcrun', 'notarytool', 'submit', str(upload), *map(str, credentials), '--wait',
                                 '--timeout', '30m', '--output-format', 'json'], capture_output=True, text=True)
        try:
            submission = json.loads(result.stdout)
        except ValueError:
            raise SystemExit('notarytool did not submit Coconut.app: ' + (result.stderr or result.stdout).strip()) from None
        if submission.get('status') != 'Accepted':
            # The log names each file that the notary service refused, and why.
            log = subprocess.run(['xcrun', 'notarytool', 'log', submission.get('id', ''), *map(str, credentials)],
                                 capture_output=True, text=True)
            raise SystemExit(f"The notary service did not accept Coconut.app: {submission.get('status')} "
                             f"{submission.get('message', '')}\n{log.stdout}{log.stderr}")
    run(['xcrun', 'stapler', 'staple', app])
    run(['xcrun', 'stapler', 'validate', app])
    run(['spctl', '--assess', '--type', 'execute', '--verbose=2', app])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['build', 'notarize', 'archive'])
    parser.add_argument('--output', type=Path, default=ROOT / 'build/mac', help='The folder of Coconut.app.')
    parser.add_argument('--version', help='build: CFBundleShortVersionString. Defaults to mac/VERSION.')
    parser.add_argument('--build', help='build: CFBundleVersion. Defaults to the commit count of HEAD.')
    parser.add_argument('--identity', default='-', help='build: a codesign identity. Defaults to an ad-hoc signature.')
    parser.add_argument('--key', type=Path, help='notarize: the App Store Connect API key file (.p8).')
    parser.add_argument('--key-id', help='notarize: the ID of the API key.')
    parser.add_argument('--issuer', help='notarize: the issuer ID of the API key.')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    if args.command == 'build':
        app = assemble(output, args.version or version(), args.build or build_number())
        sign(app, args.identity)
        print(app)
    elif args.command == 'notarize':
        if not (args.key and args.key_id and args.issuer):
            parser.error('notarize needs --key, --key-id and --issuer.')
        notarize(output / 'Coconut.app', args.key, args.key_id, args.issuer)
        print('Notarized and stapled ' + str(output / 'Coconut.app'))
    else:
        app = output / 'Coconut.app'
        print(archive(app, output / archive_name(app), ARCHIVE_LIMIT))


if __name__ == '__main__':
    main()
