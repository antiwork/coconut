"""Publish the latest main hourly, or publish an explicit urgent release, after its build succeeds.

GitHub deployment records keep the publication time and source commit across runs. Existing workflow history
supplies the first record while moving from per-push releases. No credentials leave the gh process.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess

CHANNELS = {
    'mac': ('coconut-mac-update', 'mac-release.yml', 'Deploy the feed to the Worker coconut-updates'),
    'iphone': ('coconut-iphone-update', 'testflight.yml', 'Sign for distribution and upload'),
}
IPHONE_INPUTS = ('ios/', 'scripts/iphone.py', 'scripts/push-envelope.swift', 'scripts/testflight.py',
                 'scripts/release_gate.py', '.github/workflows/testflight.yml')
MAC_INPUTS = ('backend/', 'hermes_plugin/', 'ios/', 'mac/', 'scripts/', 'skills/', 'updates/',
              '.github/workflows/mac-release.yml')


def api(method, path, body=None):
    command = ['gh', 'api', '--method', method, f'repos/{os.environ["GH_REPO"]}/{path}']
    if body is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def previous(channel, request=api):
    environment, workflow, publication = CHANNELS[channel]
    deployments = request('GET', f'deployments?environment={environment}&per_page=1')
    if deployments:
        deployment = deployments[0]
        statuses = request('GET', f'deployments/{deployment["id"]}/statuses?per_page=1')
        status = statuses[0] if statuses else {}
        return dict(sha=deployment['sha'], at=status.get('created_at', deployment['created_at']),
                    state=status.get('state', 'in_progress'))
    # The first run also respects a release made by the previous workflow, including one that failed after upload.
    runs = request('GET', f'actions/workflows/{workflow}/runs?status=completed&per_page=20')['workflow_runs']
    for run in runs:
        jobs = request('GET', f'actions/runs/{run["id"]}/jobs?per_page=100')['jobs']
        for job in jobs:
            for step in job.get('steps', []):
                if step['name'] == publication and step['conclusion'] == 'success':
                    return dict(sha=run['head_sha'], at=step['completed_at'], state='success')
    return None


def cooling_down(last, now):
    return last is not None and now < datetime.fromisoformat(last['at'].replace('Z', '+00:00')) + timedelta(hours=1)


def should_release(channel, last, now, changed, urgent=False):
    if not urgent and cooling_down(last, now):
        return False, 'The previous update is less than one hour old.'
    inputs = MAC_INPUTS if channel == 'mac' else IPHONE_INPUTS
    if last and last['state'] == 'success' and not any(path.startswith(inputs) for path in changed):
        return False, 'No app inputs changed since the published build.'
    return True, 'Build the latest main.'


def output(**values):
    if path := os.environ.get('GITHUB_OUTPUT'):
        with open(path, 'a') as stream:
            stream.writelines(f'{key}={value}\n' for key, value in values.items())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'begin', 'finish'))
    parser.add_argument('--channel', choices=CHANNELS)
    parser.add_argument('--id')
    parser.add_argument('--state', choices=('success', 'failure', 'cancelled'))
    parser.add_argument('--urgent', action='store_true', help='Bypass the hourly cooldown for an urgent release.')
    args = parser.parse_args()
    if args.command == 'finish':
        if not args.id or not args.state:
            parser.error('finish needs --id and --state')
        api('POST', f'deployments/{args.id}/statuses', {'state': 'success' if args.state == 'success' else 'failure'})
        return
    if not args.channel:
        parser.error('--channel is required')
    last = previous(args.channel)
    now = datetime.now(timezone.utc)
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    if args.command == 'plan':
        changed = ['ios/', 'backend/']
        if last and last['state'] == 'success':
            diff = subprocess.run(['git', 'diff', '--name-only', last['sha'], sha], capture_output=True, text=True)
            if diff.returncode == 0:
                changed = diff.stdout.splitlines()
        release, reason = should_release(args.channel, last, now, changed, urgent=args.urgent)
        print(reason)
        output(skip=str(not release).lower(), sha=sha,
               previous_sha=last['sha'] if last and last['state'] == 'success' else '')
    else:
        if not args.urgent and cooling_down(last, now):
            raise SystemExit('Another update was published within the last hour.')
        deployment = api('POST', 'deployments', dict(ref=sha, auto_merge=False, required_contexts=[],
                          environment=CHANNELS[args.channel][0],
                          description='Urgent Coconut app update' if args.urgent else 'Hourly Coconut app update'))
        output(id=deployment['id'])
        api('POST', f'deployments/{deployment["id"]}/statuses', {'state': 'in_progress'})


if __name__ == '__main__':
    main()
