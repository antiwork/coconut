#!/usr/bin/env python3
"""Build, install, and inspect Coconut's tunnel sidecar, which connects this Mac to the relay.

The sidecar (scripts/tunnel-sidecar.swift) runs as the LaunchAgent com.gumroad.coconut.tunnel. It forwards each
iPhone request from the relay to the backend's loopback listener on 127.0.0.1:8961. See docs/relay-transport.md.
"""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys

from host import DEFAULT_DATA, ROOT, TUNNEL_PORT, private_directory, write_agent
from push import compile_swift

LABEL = 'com.gumroad.coconut.tunnel'
RELAY_URL = 'https://coconut-push.gumroad.workers.dev'
SOURCES = [ROOT / 'scripts/tunnel-sidecar.swift', ROOT / 'ios/Tunnel/Noise.swift', ROOT / 'ios/Tunnel/TunnelFrames.swift']


def tunnel_directory(data_dir: Path) -> Path:
    directory = data_dir.resolve() / 'tunnel'
    if directory.is_symlink():
        raise SystemExit('The tunnel directory must not be a symbolic link.')
    private_directory(directory)
    return directory


def compile_sidecar(data_dir: Path) -> bool:
    """Compile the sidecar into <data>/tunnel when its sources changed. Return True when it compiled."""
    directory = tunnel_directory(data_dir)
    return compile_swift(SOURCES, directory / 'tunnel-sidecar', directory / 'sidecar.sha256')


def build(data_dir: Path) -> Path:
    compile_sidecar(data_dir)
    return data_dir.resolve() / 'tunnel/tunnel-sidecar'


def setup(data_dir: Path, relay: str) -> dict:
    """Create this Mac's identity key and register its route with the relay, once for each relay."""
    result = subprocess.run([str(build(data_dir)), 'setup', '--data-dir', str(data_dir.resolve()), '--relay', relay],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def agent_path() -> Path:
    return Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'


def launchctl(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(['launchctl', *arguments], check=False, capture_output=True, text=True)


def install(data_dir: Path, relay: str) -> None:
    data_dir = data_dir.resolve()
    route = setup(data_dir, relay)
    logs = data_dir / 'logs'
    private_directory(logs)
    path = agent_path()
    if path.is_symlink():
        raise SystemExit('The LaunchAgent file must not be a symbolic link.')
    payload = {
        'Label': LABEL,
        'ProgramArguments': [str(data_dir / 'tunnel/tunnel-sidecar'), 'run', '--data-dir', str(data_dir),
                             '--backend', f'127.0.0.1:{TUNNEL_PORT}'],
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 5, 'ProcessType': 'Interactive',
        'StandardOutPath': str(logs / 'tunnel.log'), 'StandardErrorPath': str(logs / 'tunnel-error.log'),
    }
    # Replace an earlier copy, so a rebuilt binary or a new relay takes effect.
    launchctl('bootout', f'gui/{os.getuid()}/{LABEL}')
    path.parent.mkdir(parents=True, exist_ok=True)
    write_agent(path, payload)
    if launchctl('bootstrap', f'gui/{os.getuid()}', str(path)).returncode:
        raise SystemExit(f'The service file exists at {path}, but launchctl did not start it.')
    print(f'Installed {LABEL}. This Mac uses route {route["route"][:8]}… on {route["relay"]}.')


def loaded() -> bool:
    return launchctl('print', f'gui/{os.getuid()}/{LABEL}').returncode == 0


def rebuild(data_dir: Path) -> None:
    """The deploy step: rebuild an installed sidecar when its sources changed, and restart it.

    A Mac without a route or without the loaded LaunchAgent stays without the sidecar. The pairing flow or setup
    installs it.
    """
    if not (data_dir.resolve() / 'tunnel/route.json').is_file() or not loaded():
        print('The tunnel sidecar is not installed. Nothing to rebuild.')
        return
    if not compile_sidecar(data_dir):
        print('The tunnel sidecar is up to date.')
        return
    if launchctl('kickstart', '-k', f'gui/{os.getuid()}/{LABEL}').returncode:
        raise SystemExit('The tunnel sidecar was rebuilt, but launchctl did not restart it.')
    print('Rebuilt and restarted the tunnel sidecar.')


def uninstall() -> None:
    launchctl('bootout', f'gui/{os.getuid()}/{LABEL}')
    agent_path().unlink(missing_ok=True)
    print(f'Removed {LABEL}. The identity key and route stay in the tunnel directory.')


def status(data_dir: Path) -> dict:
    """Return the sidecar's last status, or {'state': 'not_installed'} without a route."""
    directory = data_dir.resolve() / 'tunnel'
    if not (directory / 'route.json').is_file():
        return {'state': 'not_installed'}
    try:
        return json.loads((directory / 'status.json').read_text())
    except (OSError, ValueError):
        return {'state': 'stopped'}


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build', 'rebuild', 'install', 'uninstall', 'status'])
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--relay', default=RELAY_URL)
    args = parser.parse_args()
    try:
        if args.command == 'build':
            print(build(args.data_dir))
        elif args.command == 'rebuild':
            rebuild(args.data_dir)
        elif args.command == 'install':
            install(args.data_dir, args.relay)
        elif args.command == 'uninstall':
            uninstall()
        else:
            print(json.dumps(status(args.data_dir), indent=2))
    except subprocess.CalledProcessError as error:
        sys.exit(error.stderr.strip() if isinstance(error.stderr, str) and error.stderr else
                 'The tunnel sidecar did not build or start. Check the Xcode Command Line Tools.')


if __name__ == '__main__':
    main()
