"""`coconut`: the Coconut tools of a turn on mote (mote_runtime.py), as one shell command.

    coconut tools                 the tools, one line each
    coconut schema <tool>         the fields of one tool, as JSON Schema
    coconut <tool> '<json>'       call it with its fields as one JSON object; the answer is JSON
    coconut <tool> -              the same, with the JSON on stdin

The command acts for the Coconut turn that runs it. mote_runtime gives mote the ID of the turn (COCONUT_TURN) and the
file with the address and the token of the bridge (COCONUT_BRIDGE). The service acts for the task of that turn
(gateway_chat.gateway_tool), as for a tool of the `coconut` toolset of Hermes. The tools and their schemas are those of
that toolset (hermes_plugin/coconut/tools.py), without coconut_workspace, which needs Hermes.

A card of `coconut ask` and a recorded wait end the turn. The answer says so, and the command writes the file
COCONUT_PAUSE, so mote_runtime stops the turn.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SKIPPED = ('workspace',)
REQUEST_SECONDS = 180
ENDED = 'This ends the turn. Stop now: write nothing more and run no other command.'


def schemas() -> dict:
    """{tool: schema} of the coconut toolset of the Hermes plugin of this install."""
    spec = importlib.util.spec_from_file_location('coconut_plugin_tools', ROOT / 'hermes_plugin' / 'coconut' / 'tools.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = {}

    class Registry:
        def register_tool(self, name, schema, **_):
            found[name] = schema

    module.register(Registry())
    return {name[len('coconut_'):]: schema for name, schema in found.items()
            if name.startswith('coconut_') and name[len('coconut_'):] not in SKIPPED}


def call(tool: str, arguments: dict) -> dict:
    turn, bridge = os.environ.get('COCONUT_TURN'), os.environ.get('COCONUT_BRIDGE')
    if not turn or not bridge:
        return {'error': 'The command coconut works only in a Coconut turn on mote.'}
    try:
        address = json.loads(Path(bridge).read_text())
        request = urllib.request.Request(
            address['url'].rstrip('/') + '/v1/gateway/tools', method='POST',
            data=json.dumps({'turn_id': turn, 'tool': tool, 'arguments': arguments}).encode(),
            headers={'Authorization': 'Bearer ' + address['token'], 'Content-Type': 'application/json',
                     'User-Agent': 'coconut-mote'})
        with urllib.request.urlopen(request, timeout=REQUEST_SECONDS) as response:
            result = json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as error:
        # The service refused the call, for example a card without its fields. Its reason helps the model.
        try:
            result = {'error': json.loads(error.read() or b'{}').get('error', str(error))}
        except ValueError:
            result = {'error': str(error)}
    except (OSError, ValueError, KeyError) as error:
        result = {'error': f'The Coconut service did not answer ({type(error).__name__}).'}
    if not isinstance(result, dict):
        result = {'result': result}
    if result.get('paused') or result.get('ended'):
        pause = os.environ.get('COCONUT_PAUSE')
        if pause:
            Path(pause).touch()
        result['next'] = ENDED
    return result


def _line(schema: dict) -> str:
    text = ' '.join(str(schema.get('description') or '').split())
    first = text.split('. ')[0].rstrip('.')
    return first[:160]


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(__doc__.split('\n\n')[1])
        return 0
    tools = schemas()
    if argv[0] == 'tools':
        for name, schema in sorted(tools.items()):
            print(f'{name}: {_line(schema)}')
        return 0
    if argv[0] == 'schema':
        name = (argv[1] if len(argv) > 1 else '').removeprefix('coconut_')
        if name not in tools:
            print(json.dumps({'error': f'No tool {name!r}. `coconut tools` lists them.'}))
            return 1
        print(json.dumps({'description': tools[name].get('description'), 'parameters': tools[name].get('parameters')},
                         indent=1, ensure_ascii=False))
        return 0
    name = argv[0].removeprefix('coconut_')
    if name not in tools:
        print(json.dumps({'error': f'No tool {name!r}. `coconut tools` lists them.'}))
        return 1
    raw = argv[1] if len(argv) > 1 else '{}'
    if raw == '-':
        raw = sys.stdin.read()
    try:
        arguments = json.loads(raw or '{}')
    except ValueError as error:
        print(json.dumps({'error': f'The fields must be one JSON object ({error}).'}))
        return 1
    if not isinstance(arguments, dict):
        print(json.dumps({'error': 'The fields must be one JSON object.'}))
        return 1
    result = call(name, arguments)
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 1 if result.get('error') else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
