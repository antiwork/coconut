#!/usr/bin/env python3
"""Exercise the first milestone through the real HTTP API and process restarts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=['fixture', 'hermes'], default='fixture')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    runtime = ROOT / '.runtime'
    runtime.mkdir(exist_ok=True, mode=0o700)
    data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix=f'milestone-{args.provider}-', dir=runtime))
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (data_dir / 'coconut.sqlite3').exists():
        raise SystemExit('Use a new data directory for this milestone test.')
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    address = f'http://127.0.0.1:{port}'
    token = None
    process = None
    log = (data_dir / 'service.log').open('ab')

    def request(method, path, body=None, key=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if method != 'GET':
            headers['Idempotency-Key'] = key or str(uuid4())
        data = None if body is None else json.dumps(body).encode()
        with urlopen(Request(address + path, data=data, headers=headers, method=method), timeout=35) as response:
            return json.load(response)

    def state():
        return request('GET', '/v1/state')

    def start():
        nonlocal process
        process = subprocess.Popen([sys.executable, '-m', 'coconut', '--data-dir', str(data_dir),
                                    '--host', '127.0.0.1', '--port', str(port), '--provider', args.provider],
                                   cwd=ROOT / 'backend', stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f'The service exited. Inspect {data_dir / "service.log"}.')
            try:
                request('GET', '/health')
                return
            except (OSError, URLError):
                time.sleep(.1)
        raise TimeoutError('The service did not become healthy.')

    def stop(crash=False):
        if process and process.poll() is None:
            process.kill() if crash else process.terminate()
            process.wait(timeout=15)

    def until(predicate, description):
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            current = state()
            failed = [t for t in current['tasks'] if t['status'] == 'failed']
            if failed:
                raise AssertionError('A task failed: ' + failed[0]['summary'])
            if predicate(current):
                return current
            time.sleep(.2)
        raise TimeoutError(description)

    try:
        start()
        code_output = subprocess.check_output([sys.executable, '-m', 'coconut', '--data-dir', str(data_dir), '--pair'],
                                              cwd=ROOT / 'backend', text=True)
        code = re.search(r'\b[0-9]{10}\b', code_output).group(0)
        token = request('POST', '/v1/pair', {'code': code, 'device_name': 'Milestone test'})['token']
        bot_a = request('POST', '/v1/bots', {'name': 'Ada', 'role': 'Writes clear notes', 'avatar': 'pencil.line',
                                           'color': '#BA5D3B', 'instructions': 'Keep messages under three short sentences.'})
        bot_b = request('POST', '/v1/bots', {'name': 'Lin', 'role': 'Checks the details', 'avatar': 'checkmark.seal',
                                           'color': '#517568', 'instructions': 'Answer briefly. Complete independent work.'})
        first = request('POST', f'/v1/bots/{bot_a["id"]}/messages', {
            'text': 'Create morning-note.md containing a short morning review. Propose the file and ask for a decision before saving it.',
            'mode': 'queue', })
        second = request('POST', f'/v1/bots/{bot_b["id"]}/messages', {
            'text': 'Check that 17 multiplied by 19 equals 323. Give a brief answer. Do not create a file.',
            'mode': 'queue', })
        print('Both tasks started. The service works without a phone connection.', flush=True)
        before = until(lambda s: any(d['status'] == 'pending' for d in s['decisions']) and
                       any(t['id'] == second['id'] and t['status'] == 'completed' for t in s['tasks']),
                       'The first bot did not request a decision while the second completed.')
        pending = next(d for d in before['decisions'] if d['status'] == 'pending')
        cursor = before['cursor']
        stop(crash=True)
        start()
        after = state()
        assert next(d for d in after['decisions'] if d['id'] == pending['id'])['status'] == 'pending'
        assert next(t for t in after['tasks'] if t['id'] == second['id'])['status'] == 'completed'
        print('After a hard restart, the pending decision and completed work remain.', flush=True)
        request('POST', f'/v1/tasks/{first["id"]}/replies', {
            'text': 'What file will this create, and does it replace any existing file?',
            'decision_id': pending['id'],
        })
        context_state = until(lambda s: any(m['decision_id'] == pending['id'] and m['role'] == 'assistant' and m['kind'] == 'text'
                                           for m in s['messages']), 'The linked context answer did not arrive.')
        current = next(d for d in context_state['decisions'] if d['id'] == pending['id'])
        assert current['status'] == 'pending'
        key = str(uuid4())
        answer = {'choice': 'approve', 'revision': current['revision']}
        if current.get('options'):
            approval = next(option for option in current['options'] if option['answer'] == 'approve')
            answer['option_id'] = approval['id']
        first_response = request('POST', f'/v1/decisions/{pending["id"]}/answer', answer, key)
        retry_response = request('POST', f'/v1/decisions/{pending["id"]}/answer', answer, key)
        assert first_response == retry_response
        final = until(lambda s: any(t['id'] == first['id'] and t['status'] == 'completed' for t in s['tasks']) and
                      any(d['id'] == pending['id'] and d['status'] == 'executed' for d in s['decisions']),
                      'The approved task did not complete.')
        files = [a for a in final['artifacts'] if a['task_id'] == first['id'] and a['kind'] == 'file']
        assert len(files) == 1, f'Expected one file; observed {len(files)}.'
        final_decision = next(d for d in final['decisions'] if d['id'] == pending['id'])
        if current.get('options'):
            assert final_decision['selected_option_id'] == answer['option_id']
        continuation = request('POST', f'/v1/tasks/{second["id"]}/replies', {
            'text': 'Thanks. Confirm the same result in one short sentence. Do not create a file.',
        })
        assert continuation['id'] != second['id']
        assert continuation['thread_id'] == second['thread_id']
        assert continuation['bot_id'] == second['bot_id']
        final = until(lambda s: any(t['id'] == continuation['id'] and t['status'] == 'completed' for t in s['tasks']),
                      'The reply in the completed thread did not finish.')
        replay = request('GET', f'/v1/events?after={cursor}&wait=0')
        assert replay['events'] and replay['cursor'] > cursor
        stop(crash=True)
        start()
        final = state()
        assert len([a for a in final['artifacts'] if a['task_id'] == first['id'] and a['kind'] == 'file']) == 1
        with sqlite3.connect(data_dir / 'coconut.sqlite3') as connection:
            count = connection.execute('SELECT COUNT(*) FROM receipts WHERE decision_id=? AND state=?',
                                       (pending['id'], 'completed')).fetchone()[0]
        assert count == 1
        report = {
            'provider': args.provider, 'real_hermes': args.provider == 'hermes', 'data_dir': str(data_dir),
            'bots': [bot_a['id'], bot_b['id']], 'decision_id': pending['id'], 'file_count': len(files),
            'completed_receipts': count, 'hard_restarts': 2, 'context_preserved_decision': True,
            'identical_approval_retry': True, 'event_replay': True, 'passed': True,
            'selected_option_id': answer.get('option_id'), 'task_thread_context': True,
            'completed_thread_followup': True,
        }
        (data_dir / 'milestone-report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)
    finally:
        stop()
        log.close()


if __name__ == '__main__':
    main()
