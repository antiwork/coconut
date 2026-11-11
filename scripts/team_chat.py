#!/usr/bin/env python3
"""Let a bot of a team-only Mac answer in the team chat, or stop it: `coconut team connect-chat` and `disconnect-chat`.

    python3 scripts/team_chat.py connect <bot> [--confirm]
    python3 scripts/team_chat.py disconnect <bot>

Coconut changes nothing in Hermes by itself. Only these commands of the operator change the Hermes home of a bot.
`connect` without `--confirm` changes nothing: it prints each change that it would make and the command that makes
them. With `--confirm` it makes them (backend/coconut/team_chat_config.py, under the Python of Hermes, in a process of
its own), then sets `chat` on the record of the bot in the service. `disconnect` first sets `chat` to false, so no new
turn starts, then takes the changes away. Neither command starts, stops or restarts a Hermes gateway: the operator
restarts the gateway of the profile.

A Mac with a person refuses both: its bots answer only that person.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from shlex import quote as quoted
import subprocess
import sys
import urllib.error
import urllib.request

import host

FULL_MAC = ('This Mac has a person, so its bots answer only that person. A bot answers in the team chat only on a Mac '
            'that runs Coconut for a team only.')
# Requests go to loopback only, never through a proxy from the environment.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Stop(Exception):
    """A line for the operator, and the end of the command with status 1."""


def call(data: Path, method: str, path: str, body: dict | None = None) -> dict:
    """One request to the team routes of the service, with the token of the bridge file."""
    try:
        bridge = json.loads((data / 'gateway/bridge.json').read_text())
    except (OSError, ValueError):
        raise Stop('The Coconut service did not start yet. Run coconut status, then try again.') from None
    request = urllib.request.Request(bridge['url'] + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'Authorization': 'Bearer ' + bridge['token'],
                                              'Content-Type': 'application/json'})
    try:
        with OPENER.open(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            text = json.loads(error.read()).get('error')
        except ValueError:
            text = None
        raise Stop(text or f'The Coconut service answered {error.code}.') from None
    except OSError:
        raise Stop('The Coconut service does not answer. Run coconut status, then try again.') from None


def find_bot(data: Path, argument: str) -> dict:
    """The bot with this ID, else with this name. Case does not matter."""
    bots = call(data, 'GET', '/v1/team/bots')['bots']
    found = [bot for bot in bots if bot['id'] == argument] or [
        bot for bot in bots if bot['name'].casefold() == argument.casefold()]
    if len(found) != 1:
        names = ', '.join(bot['name'] for bot in bots) or 'none'
        raise Stop(f'This Mac has no bot with the name or ID "{argument}". The bots of this Mac: {names}.')
    return found[0]


def edit(action: str, home: str, bridge: Path) -> list[str]:
    """Run team_chat_config.py under the Python of the installed Hermes. Return the lines of the changes."""
    runtime_module = host.hermes_runtime()
    environment = host.service_environment()
    try:
        runtime = runtime_module.find(environment)
    except runtime_module.HermesUnavailable as error:
        raise Stop(f'Coconut found no Hermes that it can use, so it changed nothing: {error.reason}') from None
    with runtime_module.isolated(environment) as (isolated, folder):
        # The edit runs in the home of the bot. The isolated environment keeps the model keys out of the process.
        result = subprocess.run([str(runtime.python), '-I', '-B', str(host.ROOT / 'backend/coconut/team_chat_config.py'),
                                 action, str(runtime.root), home, str(bridge)], cwd=folder, env=isolated,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    try:
        answer = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Stop('Hermes could not run the change, so Coconut changed nothing. '
                   + runtime_module.last_line(result.stderr)) from None
    if 'error' in answer:
        raise Stop(answer['error'])
    return answer['changes']


def numbered(lines: list[str]) -> list[str]:
    return [f'{number}. {line}' for number, line in enumerate(lines, start=1)]


def connect(data: Path, argument: str, confirm: bool) -> list[str]:
    bot = find_bot(data, argument)
    name, home = bot['name'], bot['home']
    command = f'coconut team connect-chat {quoted(name)} --confirm'
    who = (f'Then {name} answers each member of its teams who names it in the team chat. It uses the tools that the '
           f'Hermes config gives to the channel coconut (platform_toolsets.coconut in {home}/config.yaml).')
    bridge = (data / 'gateway/bridge.json').resolve()
    if not confirm:
        changes = edit('plan', home, bridge)
        if not changes:
            lines = [f'The Hermes home {home} has the team chat already. Nothing changes there.']
        else:
            lines = [f'This command changes nothing yet. With --confirm, it makes these changes in the Hermes home '
                     f'{home}:', *numbered(changes)]
        return lines + [who, 'Coconut does not restart the Hermes gateway. You restart it after the change.',
                        f'To make the change, run: {command}']
    changes = edit('connect', home, bridge)
    call(data, 'PATCH', f'/v1/team/bots/{bot["id"]}', {'chat': True})
    lines = ([f'Coconut made these changes in the Hermes home {home}:', *numbered(changes)] if changes else
             [f'The Hermes home {home} had the team chat already. Nothing changed there.'])
    return lines + [f'The team chat of {name} is connected.',
                    f'Next step: restart the Hermes gateway of {home} yourself. Coconut does not restart it.',
                    'To check, run: coconut status']


def disconnect(data: Path, argument: str) -> list[str]:
    bot = find_bot(data, argument)
    name, home = bot['name'], bot['home']
    # No new turn starts while the plugin goes away.
    call(data, 'PATCH', f'/v1/team/bots/{bot["id"]}', {'chat': False})
    changes = edit('disconnect', home, (data / 'gateway/bridge.json').resolve())
    lines = ([f'Coconut took these changes away from the Hermes home {home}:', *numbered(changes)] if changes else
             [f'The Hermes home {home} has no team chat of Coconut. Nothing changed there.'])
    return lines + [f'{name} no longer answers in the team chat.',
                    f'Next step: restart the Hermes gateway of {home} yourself, so that it unloads the plugin. Coconut '
                    'does not restart it.']


def main(arguments=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=['connect', 'disconnect'])
    parser.add_argument('bot')
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--data-dir', type=Path, default=host.DEFAULT_DATA, help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    try:
        if not host.backend_module('service_base').team_folder(args.data_dir):
            raise Stop(FULL_MAC)
        if args.action == 'disconnect' and args.confirm:
            raise Stop('coconut team disconnect-chat takes no --confirm.')
        lines = connect(args.data_dir, args.bot, args.confirm) if args.action == 'connect' else disconnect(
            args.data_dir, args.bot)
    except Stop as stop:
        print(stop, file=sys.stderr)
        return 1
    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
