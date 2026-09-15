import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4
from datetime import datetime, timezone
from unittest.mock import patch

from coconut.worker_activity import instrument, event_phase, progress_label, read_progress
from test_background_processes import BackgroundProcessTests


class WorkerActivityTests(unittest.TestCase):
    def test_wrapper_keeps_redirects_and_permission_flags(self):
        args = dict(command='claude --print --permission-mode bypassPermissions --output-format json < prompt.txt > result.json 2> err.txt', background=True)
        result = instrument(args, str(uuid4()))
        self.assertTrue(result['background'])
        self.assertIn('--permission-mode bypassPermissions', result['command'])
        self.assertIn('--output-format stream-json --verbose', result['command'])
        self.assertTrue(result['command'].endswith('< prompt.txt > result.json 2> err.txt'))
        for command in ['cd repo && claude -p hi', 'claude -p "$PROMPT"', 'cat prompt | claude -p', 'claude', 'codex login', 'claude -p hello # --dangerously-skip-permissions', 'claude -p hello > output # ignored']:
            self.assertEqual(instrument({'command': command}, str(uuid4())), {'command': command})

    def test_event_labels_do_not_expose_paths_or_tool_output(self):
        self.assertEqual(event_phase('claude', {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'Edit', 'input': {'file_path': '/secret'}}]}}), 'editing')
        self.assertEqual(event_phase('codex', {'type': 'item.started', 'item': {
            'type': 'command_execution', 'command': 'bundle exec rspec spec/example.rb'}}), 'testing')
        self.assertIsNone(event_phase('codex', {'type': 'unknown', 'text': 'tests passed'}))

    def test_redirected_json_result_keeps_live_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = str(uuid4())
            executable = root / 'claude'
            executable.write_text('#!' + sys.executable + '\n' + '''import json,sys,time
print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Edit','input':{}}]}}),flush=True)
time.sleep(0.4)
print(json.dumps({'type':'result','result':sys.stdin.read(),'is_error':False}),flush=True)
''')
            executable.chmod(0o700)
            (root / 'prompt').write_text('original prompt')
            with patch.dict(os.environ, HERMES_HOME=directory):
                args = instrument({'command': shlex.quote(str(executable)) + ' -p --output-format json < prompt > output'}, identity)
                process = subprocess.Popen(args['command'], shell=True, cwd=root)
                try:
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        progress = read_progress(identity)
                        if progress and progress['phase'] == 'editing':
                            break
                        time.sleep(.01)
                    self.assertEqual(progress['phase'], 'editing')
                    self.assertEqual((root / 'output').read_text(), '')
                    self.assertEqual(process.wait(timeout=5), 0)
                    self.assertEqual(json.loads((root / 'output').read_text())['result'], 'original prompt')
                    self.assertEqual(read_progress(identity)['phase'], 'finished')
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()

    def test_codex_stream_keeps_events_and_failure_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'codex'
            event = {'type': 'turn.failed', 'error': {'message': 'failure'}}
            executable.write_text('#!' + sys.executable + '\nimport sys\nprint(' + repr(json.dumps(event)) + ')\nsys.exit(7)\n')
            executable.chmod(0o700)
            identity = str(uuid4())
            with patch.dict(os.environ, HERMES_HOME=directory):
                args = instrument({'command': shlex.quote(str(executable)) + ' exec --json hello'}, identity)
                result = subprocess.run(args['command'], shell=True, capture_output=True, text=True)
                self.assertEqual(result.returncode, 7)
                self.assertEqual(json.loads(result.stdout), event)
                self.assertEqual(read_progress(identity)['phase'], 'failed')

    def test_claude_text_failure_keeps_error_without_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'claude'
            event = {'type': 'result', 'is_error': True, 'subtype': 'error_max_turns', 'errors': ['Turn limit reached']}
            executable.write_text('#!' + sys.executable + '\nimport sys\nprint(' + repr(json.dumps(event)) + ')\nsys.exit(1)\n')
            executable.chmod(0o700)
            with patch.dict(os.environ, HERMES_HOME=directory):
                command = instrument({'command': shlex.quote(str(executable)) + ' -p hello'}, str(uuid4()))['command']
                result = subprocess.run(command, shell=True, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, '')
                self.assertIn('Turn limit reached', result.stderr)

    def test_codex_text_failure_keeps_error_in_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'codex'
            event = {'type': 'turn.failed', 'error': {'message': 'Authentication expired'}}
            executable.write_text('#!' + sys.executable + '\nimport sys\nprint(' + repr(json.dumps(event)) + ')\nsys.exit(1)\n')
            executable.chmod(0o700)
            with patch.dict(os.environ, HERMES_HOME=directory):
                command = instrument({'command': shlex.quote(str(executable)) + ' exec hello'}, str(uuid4()))['command']
                result = subprocess.run(command, shell=True, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, '')
                self.assertIn('Authentication expired', result.stderr)

    def test_codex_text_stdout_contains_only_final_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'codex'
            events = [
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Checking files'}},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok": true}'}},
                {'type': 'turn.completed'}]
            executable.write_text('#!' + sys.executable + '\n' + '\n'.join('print(' + repr(json.dumps(e)) + ')' for e in events))
            executable.chmod(0o700)
            with patch.dict(os.environ, HERMES_HOME=directory):
                command = instrument({'command': shlex.quote(str(executable)) + ' exec hello'}, str(uuid4()))['command']
                result = subprocess.run(command, shell=True, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stdout), {'ok': True})
                self.assertEqual(result.stderr.strip(), 'Checking files')

    def test_alive_poll_does_not_refresh_progress_time(self):
        stamp = time.time()
        record = {'created_at': datetime.fromtimestamp(stamp - 300, timezone.utc).isoformat(),
                  '_worker_progress': {'phase': 'editing', 'at': stamp - 181},
                  '_process_observed_at': datetime.now(timezone.utc).isoformat()}
        self.assertEqual(progress_label(record, datetime.fromtimestamp(stamp, timezone.utc)),
                         'Waiting for worker output · 3 min without an update')


class WorkerActivityLifecycleTests(unittest.TestCase):
    setUp = BackgroundProcessTests.setUp
    tearDown = BackgroundProcessTests.tearDown
    write = BackgroundProcessTests.write
    start = BackgroundProcessTests.start

    def test_progress_persists_and_failed_worker_resumes_once(self):
        task_id = self.start()
        stamp = time.time()
        self.provider.process_completions = lambda bot, processes: [dict(id=p['id'], observation='running',
            progress={'phase': 'testing', 'at': stamp}) for p in processes]
        self.service._drain_process_completions()
        record = next(r for r in self.service._all('native_receipts') if r.get('_worker_progress'))
        self.assertEqual(record['_worker_progress']['phase'], 'testing')
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'waiting')
        count = self.service.db.execute('select count(*) from events').fetchone()[0]
        self.service._drain_process_completions()
        self.assertEqual(self.service.db.execute('select count(*) from events').fetchone()[0], count)
        self.assertEqual(self.service._get('native_receipts', record['id'])['_worker_progress']['at'], stamp)
        self.provider.process_completions = lambda bot, processes: [dict(id=p['id'], text='Worker exited with code 1. Inspect saved results before retrying.') for p in processes]
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', task_id)['status'], 'queued')
        self.service._drain_process_completions()
        self.assertEqual(len(self.service._all('tasks')), 1)


class WorkProgressProjectionTests(unittest.TestCase):
    import test_work_status as fixtures
    setUp = fixtures.WorkStatusTests.setUp
    tearDown = fixtures.WorkStatusTests.tearDown
    write = fixtures.WorkStatusTests.write
    run_task = fixtures.WorkStatusTests.run_task
    artifact = fixtures.WorkStatusTests.artifact
    row = fixtures.WorkStatusTests.row
    status = fixtures.WorkStatusTests.status

    def test_process_phase_overrides_report_and_expires_without_restarting(self):
        self.artifact('Reviewing old changes')
        self.status('waiting')
        record = dict(id=str(uuid4()), task_id=self.task['id'], action={'name': 'terminal'}, state='completed',
                      result=json.dumps(dict(session_id='worker', notify_on_complete=True)),
                      turn_id=self.service._get('tasks', self.task['id']).get('_native_turn_id'),
                      created_at=datetime.now(timezone.utc).isoformat(),
                      _process_status='running', _process_observed_at=datetime.now(timezone.utc).isoformat(),
                      _worker_progress={'phase': 'testing', 'at': time.time()})
        with self.service.transaction():
            self.service._put('native_receipts', record)
        self.assertEqual(self.row()['activity'], 'Running tests')
        record['_worker_progress']['at'] -= 181
        with self.service.transaction():
            self.service._put('native_receipts', record)
        self.assertEqual(self.row()['activity'], 'Waiting for worker output · 3 min without an update')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'waiting')


class WorkerBridgeTests(unittest.TestCase):
    def test_adapter_preserves_original_receipt_for_recovery(self):
        from types import SimpleNamespace
        from coconut.native_bridge import NativeBridge
        callbacks, records = {}, []
        class Context:
            def __init__(self, *args): pass
            def register_middleware(self, kind, callback):
                callbacks['execute'] = callback
                return SimpleNamespace(dispose=lambda: None)
        modules = {
            'tools.approval': SimpleNamespace(register_gateway_notify=lambda *a: None),
            'hermes_cli.plugins': SimpleNamespace(PluginContext=Context, PluginManifest=lambda **kw: None, get_plugin_manager=lambda: None),
            'hermes_cli.middleware': SimpleNamespace(TOOL_EXECUTION_MIDDLEWARE='tool'),
        }
        identity = str(uuid4())
        def receipt(kind, payload):
            records.append((kind, payload))
            return {'status': 'recorded', 'id': identity}
        bridge = NativeBridge({'operation': 'start', 'task': {'id': 'parent'}}, None, receipt)
        with patch.dict('sys.modules', modules):
            bridge.install(None)
        args = {'command': 'claude -p --output-format json hello', 'background': True}
        calls = []
        result = callbacks['execute']('terminal', args, lambda actual: calls.append(actual) or 'result')
        self.assertEqual(result, 'result')
        self.assertEqual(records[0][1]['arguments'], args)
        self.assertIn(identity, calls[0]['command'])
        self.assertIn('worker_cli.py', calls[0]['command'])
        self.assertEqual(records[-1][1]['result'], 'result')
