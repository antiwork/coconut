#!/usr/bin/env python3
"""Post an alert to the alerts feed of Coconut, list the alerts, and resolve one.

An alert is an operational notice: a cron job that failed, a finding of a watchdog, a deploy that ended. The Coconut
app shows it in its alerts feed, and a critical alert also notifies the phone. The service does the work
(backend/coconut/alerts.py). This script calls its alert routes on the main listener, with the token of
<data>/gateway/bridge.json. A script without an agent raises an alert with it.

    python3 scripts/alerts.py post --level critical --source cron:daily-ledger \\
        --title "daily-ledger failed" --body "Request 20261116T1200 status=error" [--url https://...] [--dedupe-key k]
    python3 scripts/alerts.py list [--limit 20]
    python3 scripts/alerts.py resolve <alert ID>

`post` prints the ID of the alert. A post with the `--dedupe-key` of an alert of the last 24 hours prints the ID of that
alert and adds nothing, so a job that retries posts one alert.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import http.client
import json
import os
from urllib.parse import quote, urlsplit

from host import DEFAULT_DATA

LEVELS = ('info', 'notice', 'critical')


def call(data_dir: Path, method: str, path: str, body: dict | None = None) -> dict:
    """Send one request to the alert routes of the service. Return the JSON object of the answer."""
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
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=30)
    try:
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        answer = json.loads(response.read())
    except (OSError, ValueError, http.client.HTTPException):
        raise SystemExit('The Coconut service did not answer. Check that it runs, then try again.') from None
    finally:
        connection.close()
    if response.status not in (200, 201):
        raise SystemExit(answer.get('error') or f'The service answered {response.status}.')
    return answer


def line(alert: dict) -> str:
    """One alert as one line of the list: its state, level, time, ID, source and title."""
    state = 'resolved' if alert['resolved_at'] else 'read' if alert['read_at'] else 'unread'
    return (f'{state:8}  {alert["level"]:8}  {alert["created_at"][:19]}Z  {alert["id"]}  '
            f'{alert["source"]}: {alert["title"]}')


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    commands = parser.add_subparsers(dest='command', required=True)
    post = commands.add_parser('post', help='Post an alert, and print its ID.')
    post.add_argument('--level', required=True, choices=LEVELS, help='Only a critical alert notifies the phone.')
    post.add_argument('--source', required=True, help='The producer, for example cron:daily-ledger.')
    post.add_argument('--title', required=True)
    post.add_argument('--body', default='')
    post.add_argument('--url', help='An http or https address that the alert opens.')
    post.add_argument('--dedupe-key', help='A post with the same key inside 24 hours adds no alert.')
    listing = commands.add_parser('list', help='Print the alerts: the ones that are not resolved first, newest first.')
    listing.add_argument('--limit', type=int, default=20)
    listing.add_argument('--json', action='store_true', help='Print the answer of the service as JSON.')
    resolve = commands.add_parser('resolve', help='Resolve an alert.')
    resolve.add_argument('alert', metavar='alert-id')
    args = parser.parse_args()
    if args.command == 'post':
        body = {name: value for name, value in (
            ('level', args.level), ('source', args.source), ('title', args.title), ('body', args.body),
            ('url', args.url), ('dedupe_key', args.dedupe_key)) if value is not None}
        print(call(args.data_dir, 'POST', '/v1/alerts', body)['id'])
    elif args.command == 'list':
        answer = call(args.data_dir, 'GET', f'/v1/alerts?limit={args.limit}')
        if args.json:
            print(json.dumps(answer, indent=2, ensure_ascii=False))
            return
        for alert in answer['items']:
            print(line(alert))
        print(f'{answer["unread"]} unread')
    else:
        alert = call(args.data_dir, 'POST', f'/v1/alerts/{quote(args.alert, safe="")}/resolve', {})
        print(f'{alert["id"]} resolved at {alert["resolved_at"]}')


if __name__ == '__main__':
    main()
