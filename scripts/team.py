#!/usr/bin/env python3
"""Build the helper of the team log, make, join, leave and list the team rooms of this Mac, and share its agents.

A team is a set of Macs that share one sealed log on the relay (docs/team-log.md). The service does the work. This
script calls its team routes on the main listener, with the token of <data>/gateway/bridge.json.

    python3 scripts/team.py build                          # compile the helper into <data>/team
    python3 scripts/team.py create --name "Example Team"   # make a room. This Mac is its admin.
    python3 scripts/team.py invite <room ID>               # print an invite link
    python3 scripts/team.py join                           # join with the invite link on standard input
    python3 scripts/team.py rooms                          # the rooms with their members and their sync state
    python3 scripts/team.py sync                           # read each room from the relay now
    python3 scripts/team.py leave <room ID>
    python3 scripts/team.py share <room ID> <agent ID>     # share an agent and its team pages with the room
    python3 scripts/team.py unshare <room ID> <agent ID>

The invite link lets a Mac into the room, so `join` reads it from standard input and not from an argument.

On a person's Mac the token reaches only `rooms`, `sync`, `join` and `leave` (docs/team-log.md, "Routes"). There the
person makes a room and an invite in the Coconut app, and `create` and `invite` print the refusal of the service.
`share` and `unshare` work only on a Mac for a team only.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import getpass
import http.client
import json
import os
import subprocess
import sys
from urllib.parse import quote, urlsplit

from host import DEFAULT_DATA, ROOT, private_directory
from push import compile_swift

# The helper compiles the format code of the app, as the tunnel sidecar compiles ios/Tunnel/Noise.swift.
SOURCES = [ROOT / 'scripts/team-helper.swift', ROOT / 'ios/Tunnel/TeamLog.swift']
# Coconut.app carries its own signed helper. Its login item and its update name that binary here.
HELPER_ENVIRONMENT = 'COCONUT_TEAM_HELPER'


def team_directory(data_dir: Path) -> Path:
    directory = data_dir.resolve() / 'team'
    if directory.is_symlink():
        raise SystemExit('The team directory must not be a symbolic link.')
    private_directory(directory)
    return directory


def app_helper() -> Path | None:
    value = os.environ.get(HELPER_ENVIRONMENT)
    return Path(value) if value else None


def build(data_dir: Path) -> Path:
    """Compile the helper into <data>/team when its sources changed. Return the helper that the service uses.

    Under Coconut.app nothing compiles: the service uses the helper of the app.
    """
    directory = team_directory(data_dir)
    if app_helper() is not None:
        return app_helper()
    compile_swift(SOURCES, directory / 'team-helper', directory / 'helper.sha256')
    return directory / 'team-helper'


def call(data_dir: Path, method: str, path: str, body: dict | None = None) -> dict:
    """Send one request to the team routes of the service. Return the JSON object of the answer."""
    try:
        bridge = json.loads((data_dir.resolve() / 'gateway/bridge.json').read_text())
        url, token = urlsplit(bridge['url']), bridge['token']
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit('The Coconut service did not run yet for this data folder. Start it, then try again.') from None
    headers = {'Authorization': f'Bearer {token}'}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=180)
    try:
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        answer = json.loads(response.read())
    except (OSError, ValueError, http.client.HTTPException):
        raise SystemExit('The Coconut service did not answer. Check that it runs, then try again.') from None
    finally:
        connection.close()
    if response.status != 200:
        raise SystemExit(answer.get('error') or f'The service answered {response.status}.')
    return answer


def read_link() -> str:
    """Read the invite link from standard input. A terminal does not show it."""
    link = getpass.getpass('Invite link: ') if sys.stdin.isatty() else sys.stdin.readline()
    return link.strip()


def member(args: argparse.Namespace) -> dict:
    """The name, avatar and color of this Mac's member, when the person gave them."""
    values = {'member_name': args.member_name, 'avatar': args.avatar, 'color': args.color}
    return {name: value for name, value in values.items() if value is not None}


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('build', help='Compile the helper of the team log.')
    commands.add_parser('rooms', help='Print the rooms with their members and their sync state.')
    commands.add_parser('sync', help='Read each room from the relay now, then print the rooms.')
    create = commands.add_parser('create', help='Make a room. This Mac is its admin.')
    create.add_argument('--name', required=True, help='The name of the team.')
    create.add_argument('--relay', help='The HTTPS address of another relay.')
    join = commands.add_parser('join', help='Join a room with the invite link on standard input.')
    for command in (create, join):
        command.add_argument('--member-name', help='The name that the team sees for this Mac.')
        command.add_argument('--avatar')
        command.add_argument('--color')
    invite = commands.add_parser('invite', help='Print an invite link. Only the admin can make one.')
    invite.add_argument('room')
    invite.add_argument('--ttl', type=int, help='The life of the invite in seconds. The default is one day.')
    invite.add_argument('--uses', type=int, help='How many Macs can join with the link. The default is 1.')
    leave = commands.add_parser('leave', help='Leave a room, and delete its copy and its keys on this Mac.')
    leave.add_argument('room')
    share = commands.add_parser('share', help='Share an agent of this Mac and its team pages with a room.')
    unshare = commands.add_parser('unshare', help='Stop sharing an agent with a room.')
    for command in (share, unshare):
        command.add_argument('room')
        command.add_argument('bot', metavar='agent')
    args = parser.parse_args()
    if args.command == 'build':
        try:
            print(build(args.data_dir))
        except subprocess.CalledProcessError:
            sys.exit('The team helper did not build. Check the Xcode Command Line Tools.')
        return
    if args.command == 'rooms':
        answer = call(args.data_dir, 'GET', '/v1/team/rooms')
    elif args.command == 'sync':
        answer = call(args.data_dir, 'POST', '/v1/team/sync', {})
    elif args.command == 'create':
        body = dict(member(args), name=args.name)
        if args.relay:
            body['relay'] = args.relay
        answer = call(args.data_dir, 'POST', '/v1/team/rooms', body)
    elif args.command == 'join':
        answer = call(args.data_dir, 'POST', '/v1/team/join', dict(member(args), link=read_link()))
    elif args.command == 'invite':
        body = {name: value for name, value in (('ttl', args.ttl), ('uses', args.uses)) if value is not None}
        answer = call(args.data_dir, 'POST', f'/v1/team/rooms/{args.room}/invites', body)
    elif args.command == 'share':
        answer = call(args.data_dir, 'POST', f'/v1/team/rooms/{args.room}/bots', {'bot_id': args.bot})
    elif args.command == 'unshare':
        answer = call(args.data_dir, 'DELETE', f'/v1/team/rooms/{args.room}/bots/{quote(args.bot, safe="")}')
    else:
        answer = call(args.data_dir, 'DELETE', f'/v1/team/rooms/{args.room}')
    print(json.dumps(answer, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
