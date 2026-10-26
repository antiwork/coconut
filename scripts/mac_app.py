#!/usr/bin/env python3
"""Build Coconut.app: the menu bar app, the `coconut` tool, the relay sidecar and the two login items.

The bundle:
  Contents/MacOS/Coconut                      the menu bar app (mac/Sources/CoconutApp)
  Contents/Helpers/coconut                    the tool that the login items run (mac/Sources/CoconutTool)
  Contents/Helpers/tunnel-sidecar             the relay sidecar (scripts/tunnel-sidecar.swift)
  Contents/Library/LaunchAgents/*.plist       the login items (mac/LaunchAgents)

Each binary is universal (arm64 and x86_64). Without --identity, the bundle has an ad-hoc signature.
"""
from __future__ import annotations

from pathlib import Path
import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['build'])
    parser.add_argument('--output', type=Path, default=ROOT / 'build/mac')
    parser.add_argument('--version', default='0.1.0', help='CFBundleShortVersionString.')
    parser.add_argument('--build', help='CFBundleVersion. Defaults to the commit count of HEAD.')
    parser.add_argument('--identity', default='-', help='A codesign identity. Defaults to an ad-hoc signature.')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = assemble(args.output.resolve(), args.version, args.build or build_number())
    sign(app, args.identity)
    print(app)


if __name__ == '__main__':
    main()
