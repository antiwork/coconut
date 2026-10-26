#!/usr/bin/env python3
"""Build Coconut.app: the menu bar app, the `coconut` tool, the relay sidecar and the two login items.

The bundle:
  Contents/MacOS/Coconut                      the menu bar app (mac/Sources/CoconutApp)
  Contents/Helpers/coconut                    the tool that the login items run (mac/Sources/CoconutTool)
  Contents/Helpers/tunnel-sidecar             the relay sidecar (scripts/tunnel-sidecar.swift)
  Contents/Library/LaunchAgents/*.plist       the login items (mac/LaunchAgents)

Each binary is universal (arm64 and x86_64). Without --identity, the bundle has an ad-hoc signature.

A release signs the bundle with the Developer ID Application certificate, notarizes it with `notarytool` and an App
Store Connect API key, staples the ticket, and makes the zip archive that Sparkle downloads:

  mac_app.py build --identity <SHA-1 of the Developer ID identity>
  mac_app.py notarize --key AuthKey.p8 --key-id <id> --issuer <issuer>
  mac_app.py archive
"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import plistlib
import shutil
import subprocess
import tempfile

from tunnel import SOURCES as SIDECAR_SOURCES

ROOT = Path(__file__).resolve().parents[1]
MAC = ROOT / 'mac'
BUNDLE_ID = 'com.gumroad.coconut.mac'
MINIMUM_SYSTEM = '14.0'
ARCHITECTURES = ('arm64', 'x86_64')


def run(arguments: list, **options) -> subprocess.CompletedProcess:
    return subprocess.run([str(argument) for argument in arguments], check=True, **options)


def build_number() -> str:
    """The number of commits on HEAD. It grows with each merge to main, as Sparkle needs. CI needs the full history."""
    return run(['git', '-C', ROOT, 'rev-list', '--count', 'HEAD'], capture_output=True, text=True).stdout.strip()


def info_plist(version: str, build: str) -> dict:
    return {
        'CFBundleDevelopmentRegion': 'en',
        'CFBundleDisplayName': 'Coconut',
        'CFBundleExecutable': 'Coconut',
        'CFBundleIdentifier': BUNDLE_ID,
        'CFBundleInfoDictionaryVersion': '6.0',
        'CFBundleName': 'Coconut',
        'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': version,
        'CFBundleVersion': build,
        'LSApplicationCategoryType': 'public.app-category.productivity',
        'LSMinimumSystemVersion': MINIMUM_SYSTEM,
        # A menu bar app: no Dock icon and no main menu.
        'LSUIElement': True,
        'NSHighResolutionCapable': True,
    }


def swift_products(scratch: Path) -> Path:
    """Build the app and the tool for both architectures. Return the folder of the products."""
    command = ['swift', 'build', '-c', 'release', '--package-path', MAC, '--scratch-path', scratch]
    for architecture in ARCHITECTURES:
        command += ['--arch', architecture]
    for product in ('CoconutApp', 'CoconutTool'):
        run(command + ['--product', product])
    return Path(run(command + ['--show-bin-path'], capture_output=True, text=True).stdout.strip())


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
        products = swift_products(MAC / '.build')
        shutil.copy2(products / 'CoconutApp', contents / 'MacOS/Coconut')
        shutil.copy2(products / 'CoconutTool', contents / 'Helpers/coconut')
        compile_sidecar(contents / 'Helpers/tunnel-sidecar', scratch)
    for agent in sorted((MAC / 'LaunchAgents').glob('*.plist')):
        shutil.copy2(agent, contents / 'Library/LaunchAgents' / agent.name)
    with (contents / 'Info.plist').open('wb') as handle:
        plistlib.dump(info_plist(version, build), handle)
    (contents / 'PkgInfo').write_text('APPL????')
    return app


def sign(app: Path, identity: str) -> None:
    """Sign the inner binaries first, then the app, with the hardened runtime that notarization requires."""
    options = ['--force', '--options', 'runtime', '--sign', identity]
    options += ['--timestamp=none'] if identity == '-' else ['--timestamp']
    for binary in sorted((app / 'Contents/Helpers').iterdir()):
        run(['codesign', *options, binary])
    run(['codesign', *options, app])
    run(['codesign', '--verify', '--deep', '--strict', app])


def archive(app: Path, destination: Path) -> Path:
    """Zip the app with ditto, which keeps the signature, the stapled ticket and the symbolic links."""
    destination.unlink(missing_ok=True)
    run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, destination])
    return destination


def archive_name(app: Path) -> str:
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
    parser.add_argument('--version', default='0.1.0', help='build: CFBundleShortVersionString.')
    parser.add_argument('--build', help='build: CFBundleVersion. Defaults to the commit count of HEAD.')
    parser.add_argument('--identity', default='-', help='build: a codesign identity. Defaults to an ad-hoc signature.')
    parser.add_argument('--key', type=Path, help='notarize: the App Store Connect API key file (.p8).')
    parser.add_argument('--key-id', help='notarize: the ID of the API key.')
    parser.add_argument('--issuer', help='notarize: the issuer ID of the API key.')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    if args.command == 'build':
        app = assemble(output, args.version, args.build or build_number())
        sign(app, args.identity)
        print(app)
    elif args.command == 'notarize':
        if not (args.key and args.key_id and args.issuer):
            parser.error('notarize needs --key, --key-id and --issuer.')
        notarize(output / 'Coconut.app', args.key, args.key_id, args.issuer)
        print('Notarized and stapled ' + str(output / 'Coconut.app'))
    else:
        app = output / 'Coconut.app'
        print(archive(app, output / archive_name(app)))


if __name__ == '__main__':
    main()
