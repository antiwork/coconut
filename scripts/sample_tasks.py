#!/usr/bin/env python3
"""Prepare two real Hermes tasks for the first local trial."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from coconut.server import create_pairing_code


def main() -> None:
    data = ROOT / '.runtime/host'
    token = None

    def request(method, path, body=None):
        headers = {'Content-Type': 'application/json', 'Idempotency-Key': str(uuid4())}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        payload = json.dumps(body).encode() if body is not None else None
        with urlopen(Request('http://127.0.0.1:8960' + path, data=payload, headers=headers,
                             method=method), timeout=35) as response:
            return json.load(response)

    token = request('POST', '/v1/pair', {
        'code': create_pairing_code(data), 'device_name': 'Coconut local setup',
    })['token']
    if request('GET', '/v1/state')['bots']:
        print('The host already has bots. Existing conversations remain unchanged.')
        return
    ada = request('POST', '/v1/bots', {
        'name': 'Ada', 'role': 'Plans and writes', 'avatar': 'pencil.line', 'color': '#BA5D3B',
        'instructions': 'Use concise messages. Ask with concrete choices. Explain only what needs a decision.',
        'memory': 'This is a local Coconut trial. Store all results through Coconut file proposals.',
    })
    lin = request('POST', '/v1/bots', {
        'name': 'Lin', 'role': 'Checks the details', 'avatar': 'checkmark.seal', 'color': '#517568',
        'instructions': 'Be brief. Complete independent checks while another bot waits.',
    })
    first = request('POST', f'/v1/bots/{ada["id"]}/messages', {
        'text': 'Prepare morning-note.md with a short plan for testing Coconut. Offer a short checklist and a fuller note as choices. Ask before saving.',
        'mode': 'queue', 'workflow': 'notes',
    })
    second = request('POST', f'/v1/bots/{lin["id"]}/messages', {
        'text': 'Check that 17 multiplied by 19 equals 323. Give the result in one short sentence. Do not create a file.',
        'mode': 'queue', 'workflow': 'checks',
    })
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        state = request('GET', '/v1/state')
        if any(task['status'] == 'failed' for task in state['tasks']):
            raise SystemExit('A sample task failed. Open its thread to inspect the error.')
        pending = any(d['task_id'] == first['id'] and d['status'] == 'pending' for d in state['decisions'])
        completed = any(t['id'] == second['id'] and t['status'] == 'completed' for t in state['tasks'])
        if pending and completed:
            print('Ada has a pending choice in Inbox. Lin has completed its independent check in its conversation.')
            return
        time.sleep(.3)
    raise SystemExit('The tasks still need time. Their current state remains visible in the bot conversations.')


if __name__ == '__main__':
    main()
