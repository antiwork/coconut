#!/usr/bin/env python3
"""Measure the real app against a local Mac with stored data of the size of a busy Mac.

The fixture generates synthetic records. It does not contain private messages. Its counts and text lengths
match the Mac mini data of October 14, 2026: five conversations with 14,991, 10,275, 2,209, 203, and 141
messages, assistant replies of about 900 characters with Markdown, 1,373 tasks, 1,581 work cards, and
238 routines. The app reads the recent state, older history pages, and an update every 3 seconds.
"""
import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import plistlib
import random
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
HISTORY = [14991, 10275, 2209, 203, 141]
PAGE = 60
WORDS = ('the check retries each request once and records the receipt before it sends the next one so a restart '
         'cannot repeat a charge while the queue drains the worker reads the saved state and compares the head '
         'commit with the reviewed revision then it reports the result to the conversation').split()


def stamp(value):
    return value.isoformat().replace('+00:00', 'Z')


def reply(rng):
    """A Markdown reply with the paragraph, list, and emphasis mix of real replies."""
    parts = []
    while sum(len(part) for part in parts) < rng.choice((300, 600, 900, 1200, 1900, 3000)):
        words = ' '.join(rng.choice(WORDS) for _ in range(rng.randint(12, 40)))
        shape = rng.random()
        if shape < .3:
            parts.append('\n'.join(f'- **{rng.choice(WORDS)}** {words[:rng.randint(30, 90)]}.' for _ in range(rng.randint(2, 5))))
        elif shape < .4:
            parts.append(f'**{words[:30].capitalize()}**')
        else:
            parts.append(words.capitalize() + f' with `{rng.choice(WORDS)}_{rng.randint(1, 99)}`.')
    return '\n\n'.join(parts)


def fixture():
    rng = random.Random(14)
    preview = json.loads((ROOT / 'ios/Coconut/Fixtures/PreviewState.json').read_text())
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    bots = [dict(preview['bots'][1], id=f'00000000-0000-4000-8000-00000000000{index + 2}', name=f'Bot {index + 1}',
                 pinned=False, status='idle', unread_count=0, archived=False) for index in range(len(HISTORY))]
    tasks = []
    for index in range(1373):
        status = 'completed' if index < 1277 else ('failed', 'waiting', 'cancelled', 'queued')[index % 4]
        task_id = f'task-{index}'
        tasks.append(dict(preview['tasks'][1], id=task_id, bot_id=bots[index % len(bots)]['id'], status=status,
                          title=' '.join(rng.choice(WORDS) for _ in range(8)).capitalize(), thread_id=task_id,
                          created_at=stamp(start + timedelta(minutes=index)), updated_at=stamp(start + timedelta(minutes=index))))
    history = {}
    for bot, count in zip(bots, HISTORY):
        rows = []
        for index in range(count):
            user = index % 2 == 0
            task = tasks[(index // 2) % len(tasks)]
            rows.append(dict(id=f"{bot['id']}-{index}", bot_id=bot['id'], task_id=task['id'], thread_id=task['id'],
                             role='user' if user else 'assistant', kind='text', streaming=False,
                             text=(' '.join(rng.choice(WORDS) for _ in range(rng.randint(8, 30))).capitalize() + '.') if user else reply(rng),
                             created_at=stamp(start + timedelta(seconds=40 * index))))
        history[bot['id']] = rows
    artifacts = []
    for index in range(1581):
        task = tasks[index % len(tasks)]
        pr = index < 1044
        artifact = dict(id=f'work-{index}', bot_id=task['bot_id'], task_id=None if pr else task['id'],
                        kind='pr' if pr else 'work', title=' '.join(rng.choice(WORDS) for _ in range(6)).capitalize(),
                        status='merged' if pr else 'completed', preview='', created_at=task['created_at'],
                        observed_at=task['created_at'], work_state='completed', activity_state='completed', activity='Completed',
                        activity_observed_at=task['created_at'], completion_evidence=reply(rng)[:rng.choice((20, 600))])
        if pr:
            artifact.update(url=f'https://github.com/example/repo/pull/{index}', pr_state='merged', draft=False,
                            checked_at=task['created_at'], head_sha=f'{index:040x}')
        else:
            artifact.update(url=None, native_task_id=f't_{index}', activity_bot_id=task['bot_id'],
                            completion_condition=artifact['title'], native_diagnostics=[])
        artifacts.append(artifact)
    routines = [dict(id=f'routine-{index}', bot_id=bots[index % len(bots)]['id'], name=f'Routine {index}',
                     prompt=reply(rng)[:1400], cron='0 9 * * *', timezone='UTC', enabled=True,
                     next_run_at=stamp(start), last_run_at=stamp(start), last_task_id=tasks[index]['id'],
                     created_at=stamp(start), updated_at=stamp(start)) for index in range(238)]
    decisions = [dict(preview['decisions'][0], id=f'decision-{index}', bot_id=bots[0]['id'], task_id=tasks[index]['id'],
                      status='executed', title=f'Review report {index}?') for index in range(11)]
    return dict(bots=bots, tasks=tasks, history=history, artifacts=artifacts, routines=routines, decisions=decisions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    data = fixture()
    lock = threading.Lock()
    clock = dict(cursor=1000)

    def recent():
        messages = [row for rows in data['history'].values() for row in rows[-PAGE:]]
        return dict(bots=data['bots'], tasks=data['tasks'], messages=sorted(messages, key=lambda m: (m['created_at'], m['id'])),
                    decisions=data['decisions'], artifacts=data['artifacts'], routines=data['routines'],
                    history_before={bot: len(rows) - PAGE for bot, rows in data['history'].items()},
                    cursor=clock['cursor'], server_time=stamp(datetime.now(timezone.utc)), provider='fixture',
                    model='', base_url='', model_configured=False, content_only=True, sync_version=1)
    print(f"Performance fixture: {sum(HISTORY)} messages, {len(data['tasks'])} tasks, {len(data['artifacts'])} work cards.", flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def send(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass

        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == '/v1/state':
                self.send(recent())
            elif url.path == '/v1/state/settings':
                self.send(dict(cursor=clock['cursor'], bots=data['bots'], model_catalog=None, model_configured=False,
                               mcp_servers=[], routines=data['routines'], push_configured=False))
            elif url.path.startswith('/v1/history/'):
                rows = data['history'].get(url.path.removeprefix('/v1/history/'), [])
                before = int(query.get('before', ['0'])[0])
                self.send(dict(messages=rows[max(0, before - PAGE):before], tasks=[], decisions=[],
                               before=before - PAGE if before > PAGE else None))
            elif url.path == '/v1/updates':
                after = int(query.get('after', ['0'])[0])
                if float(query.get('wait', ['0'])[0]) > 0:
                    # The Mac records work observations every few seconds while a person reads.
                    time.sleep(3)
                    with lock:
                        if after == clock['cursor']:
                            clock['cursor'] += 1
                            card = dict(data['artifacts'][clock['cursor'] % 1044], observed_at=stamp(datetime.now(timezone.utc)))
                            self.send(dict(after=after, cursor=clock['cursor'], server_time=card['observed_at'], bots=data['bots'],
                                           tasks=[], messages=[], removed_messages=[], artifacts=[card], collection_deltas=True,
                                           native_work_observed_at=card['observed_at']))
                            return
                self.send(dict(after=after, cursor=max(after, clock['cursor']), server_time=stamp(datetime.now(timezone.utc)),
                               collection_deltas=True))
            elif url.path == '/v1/work':
                self.send(dict(artifacts=[]))
            else:
                self.send({})

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if self.path == '/v1/pair':
                self.send(dict(token='performance-test', device_id='performance-test', server_name='Test Mac'))
            else: self.send(dict(ok=True))

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        source = args.xctestrun.resolve()
        def absolute(value):
            if isinstance(value, str): return value.replace('__TESTROOT__', str(source.parent))
            if isinstance(value, list): return [absolute(item) for item in value]
            if isinstance(value, dict): return {key: absolute(item) for key, item in value.items()}
            return value
        document = absolute(plistlib.loads(source.read_bytes()))
        targets = ([target for config in document['TestConfigurations'] for target in config['TestTargets']]
                   if 'TestConfigurations' in document else [value for value in document.values() if isinstance(value, dict)])
        for target in targets:
            if target.get('BlueprintName') == 'CoconutUITests':
                target.setdefault('EnvironmentVariables', {})['COCONUT_PERFORMANCE_TEST_URL'] = f'http://127.0.0.1:{server.server_port}'
        with tempfile.TemporaryDirectory(prefix='coconut-performance-') as folder:
            runfile = Path(folder) / 'Performance.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                            '-destination', 'platform=iOS Simulator,id=' + args.simulator,
                            '-only-testing:CoconutUITests/AppPerformanceTests', '-parallel-testing-enabled', 'NO',
                            '-resultBundlePath', str(args.result)], check=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
