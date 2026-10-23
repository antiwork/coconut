#!/usr/bin/env python3
"""Configure Coconut's private APNs key and native macOS sender."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def compile_swift(sources, binary, receipt):
    """Compile the Swift sources into binary, unless the receipt holds the fingerprint of the same sources."""
    fingerprint = hashlib.sha256(b''.join(source.read_bytes() for source in sources)).hexdigest()
    if binary.is_file() and receipt.is_file() and receipt.read_text() == fingerprint:
        return
    with tempfile.TemporaryDirectory(dir=binary.parent) as temp:
        output = Path(temp) / binary.name
        subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-O', *map(str, sources), '-o', str(output)], check=True)
        output.chmod(0o700)
        os.replace(output, binary)
    receipt.write_text(fingerprint)
    receipt.chmod(0o600)


def build_encryption(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise SystemExit('The push directory must not be a symbolic link.')
    compile_swift([ROOT / 'scripts/push-envelope.swift', ROOT / 'scripts/push-encrypt.swift'],
                  directory / 'push-encrypt', directory / 'encryption.sha256')
    subprocess.run([str(directory / 'push-encrypt'), str(directory), 'setup'], check=True, stdout=subprocess.DEVNULL)


def build_sender(directory):
    compile_swift([ROOT / 'scripts/apns-provider.swift'], directory / 'apns-provider', directory / 'provider.sha256')


def configure(args):
    if not re.fullmatch(r'[A-Z0-9]{10}', args.team) or not re.fullmatch(r'[A-Z0-9]{10}', args.key_id):
        raise SystemExit('Use the ten-character Apple team ID and key ID.')
    if not re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', args.topic):
        raise SystemExit('Use the bundle identifier of the signed iPhone app.')
    sender_directory = args.data_dir.resolve() / 'push'
    directory = sender_directory / 'production' if getattr(args, 'environment', 'development') == 'production' else sender_directory
    if sender_directory.is_symlink() or directory.is_symlink():
        raise SystemExit('The push directory must not be a symbolic link.')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    key = sys.stdin.buffer.read(8192) if args.key == '-' else Path(args.key).read_bytes()
    if b'-----BEGIN PRIVATE KEY-----' not in key or len(key) > 8192:
        raise SystemExit('Supply the APNs private key downloaded from Apple.')
    sender_directory.chmod(0o700)
    build_sender(sender_directory)
    for name, content in [('AuthKey.p8', key), ('config.json', json.dumps(dict(team_id=args.team, key_id=args.key_id, topic=args.topic)).encode())]:
        fd, temp = tempfile.mkstemp(dir=directory)
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
        os.replace(temp, directory / name)
    (directory / 'token.json').unlink(missing_ok=True)
    print('Coconut push credentials are configured. The service detects them automatically.')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / '.runtime/host')
    parser.add_argument('--rebuild', action='store_true')
    parser.add_argument('--environment', choices=['development', 'production'], default='development')
    parser.add_argument('--team')
    parser.add_argument('--key-id')
    parser.add_argument('--topic')
    parser.add_argument('--key', help='Path to the .p8 key, or - for standard input')
    args = parser.parse_args()
    if args.rebuild:
        directory = args.data_dir.resolve() / 'push'
        build_encryption(directory)
        if (directory / 'config.json').is_file() or (directory / 'production/config.json').is_file():
            build_sender(directory)
    elif all((args.team, args.key_id, args.topic, args.key)):
        configure(args)
    else:
        parser.error('Supply --team, --key-id, --topic, and --key, or use --rebuild.')


if __name__ == '__main__':
    main()
