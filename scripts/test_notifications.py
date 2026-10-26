#!/usr/bin/env python3
"""Test APNs delivery and task navigation on the paired physical iPhone."""
from pathlib import Path
import argparse
import json
import os
import plistlib
import subprocess
import tempfile
import uuid

import iphone
import remote

ROOT = Path(__file__).resolve().parents[1]
TESTS = {
    'conversation': 'NotificationFlowTests/testNotificationOpensTaskFromConversation',
    'closed': 'NotificationFlowTests/testNotificationOpensTaskAfterTermination',
}



def copy_cache(config, directory, name):
    """Copy one file of the app's cache from the iPhone, or return None."""
    destination = directory / name
    command = ['xcrun', 'devicectl', 'device', 'copy', 'from', '--device', config['device'],
               '--domain-type', 'appDataContainer', '--domain-identifier', config['bundle_id'],
               '--source', 'Library/Application Support/Coconut/' + name,
               '--destination', str(destination), '--timeout', '20']
    result = subprocess.run(command, capture_output=True, timeout=30)
    if result.returncode or not destination.exists():
        return None
    return json.loads(destination.read_text())

def prepare(source, destination, tests, expected):
    def absolute(value):
        if isinstance(value, str):
            return value.replace('__TESTROOT__', str(source.parent))
        if isinstance(value, list):
            return [absolute(item) for item in value]
        if isinstance(value, dict):
            return {key: absolute(item) for key, item in value.items()}
        return value

    document = absolute(plistlib.loads(source.read_bytes()))
    if 'TestConfigurations' in document:
        targets = [target for config in document['TestConfigurations'] for target in config['TestTargets']]
    else:
        targets = [target for name, target in document.items() if not name.startswith('__')]
    for target in targets:
        selected = target.get('BlueprintName') == 'CoconutUITests'
        target['IsEnabled'] = selected
        if selected:
            target.update(OnlyTestIdentifiers=tests, SkipTestIdentifiers=[], ParallelizationEnabled=False,
                          SystemAttachmentLifetime='keepNever', UserAttachmentLifetime='keepNever')
            target.setdefault('EnvironmentVariables', {}).update({
                'COCONUT_PUSH_TEST': '1',
                'COCONUT_PUSH_EXPECTED_TITLE': expected['title'],
                'COCONUT_PUSH_EXPECTED_BODY': expected['body'],
            })
    destination.write_bytes(plistlib.dumps(document))


def diagnostic(device_id, expected=None):
    config = remote.configuration()
    # This diagnostic references completed work. It does not run or change a task.
    code = '''from pathlib import Path
import sys,sqlite3,json,time
from types import SimpleNamespace
from uuid import uuid5,NAMESPACE_URL
root=Path(sys.argv[1]).resolve()
con=sqlite3.connect(root/'.runtime/host/coconut.sqlite3',timeout=15)
sys.path.insert(0,str(root/'backend'))
from coconut.push_relay import notification_alert
bots={r[0]:json.loads(r[1]) for r in con.execute('SELECT id,data FROM bots')}
tasks=[json.loads(r[0]) for r in con.execute('SELECT data FROM tasks ORDER BY rowid DESC')]
expected=json.loads(sys.argv[4])
task=next((t for t in tasks if (not expected or t['id']==expected['task_id'])
 and t['status']=='completed' and not t.get('source_task_id')
 and not bots[t['bot_id']].get('_deleted') and not bots[t['bot_id']].get('archived')
 and bots[t['bot_id']].get('notifications',True)),None)
if not task: raise SystemExit('Complete one task with notifications enabled before this test.')
device=sys.argv[2]
if not con.execute('SELECT 1 FROM push_devices WHERE device_id=? AND enabled=1',(device,)).fetchone():
 raise SystemExit('Enable notifications in the paired iPhone before this test.')
if not con.execute('SELECT 1 FROM push_routes WHERE device_id=?',(device,)).fetchone():
 raise SystemExit('Wait for this iPhone to register with the shared push service.')
event='notification-test:'+sys.argv[3]
id=str(uuid5(NAMESPACE_URL,device+':'+event))
turn=task.get('_native_turn_id') or task.get('updated_at') or task['created_at']
service=SimpleNamespace(db=con,_get=lambda table,id:json.loads(con.execute('SELECT data FROM '+table+' WHERE id=?',(id,)).fetchone()[0]))
alert=notification_alert(service,{'kind':'completed'},bots[task['bot_id']],task,None)
result=dict(task_id=task['id'],turn_id=turn,title=alert['title'],body=alert['body'])
if not expected:
 print(json.dumps(result))
 raise SystemExit(0)
if result!=expected: raise SystemExit('The selected task changed before the notification test.')
with con:
 con.execute("INSERT OR IGNORE INTO push_outbox (id,device_id,event_key,bot_id,task_id,decision_id,kind,turn_id,status,not_before,expires) VALUES (?,?,?,?,?,NULL,'completed',?,'queued',?,?)",(id,device,event,task['bot_id'],task['id'],turn,time.time()+2,time.time()+120))
print(json.dumps({'queued':True}))
'''
    result = remote.ssh(config, ['python3', '-c', code, config['directory'], device_id,
                                str(uuid.uuid4()), json.dumps(expected)])
    return json.loads(result.stdout)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--case', choices=['all', *TESTS], default='all')
    args = parser.parse_args()
    source = args.xctestrun.resolve()
    if not source.is_file() or 'iphoneos' not in str(source):
        raise SystemExit('Provide the physical-device xctestrun from build-for-testing.')
    config = iphone.load_config()
    tests = list(TESTS.values()) if args.case == 'all' else [TESTS[args.case]]
    with tempfile.TemporaryDirectory(prefix='coconut-push-test-') as temporary:
        folder = Path(temporary)
        pairing = copy_cache(config, folder, 'pairing.json')
        if not pairing or not pairing.get('deviceId'):
            raise SystemExit('Unlock and pair the iPhone before this test.')
        runfile = folder / 'Push.xctestrun'
        expected = diagnostic(pairing['deviceId'])
        prepare(source, runfile, tests, expected)
        command = ['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                   '-destination', 'platform=iOS,id=' + (config.get('device_udid') or config['device']),
                   '-parallel-testing-enabled', 'NO']
        command += ['-only-testing:CoconutUITests/' + test for test in tests]
        with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1) as process:
            try:
                for line in process.stdout:
                    print(line, end='', flush=True)
                    if 'COCONUT_PUSH_STAGE=backgrounded' in line:
                        diagnostic(pairing['deviceId'], expected)
                        print('Encrypted test notification queued.', flush=True)
            except BaseException:
                process.terminate()
                raise
            raise SystemExit(process.wait())


if __name__ == '__main__':
    main()
