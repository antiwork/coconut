"""Check worker cleanup with the installed Hermes process registry."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from uuid import uuid4

from coconut.providers import HermesProvider
from hermes_env import needs_hermes, PYTHON, SOURCE

BACKEND = Path(__file__).resolve().parents[1]


@needs_hermes
class KanbanWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        self.provider = HermesProvider(self.root, python=PYTHON, hermes_root=SOURCE,
                                       catalog=SimpleNamespace(root=self.profile, worker_limit=lambda: None))
        self.addCleanup(self.provider.close)
        self.provider.environment = lambda bot: (dict(os.environ, HERMES_HOME=str(self.profile),
            PYTHONPATH=os.pathsep.join((str(BACKEND), SOURCE)), HERMES_DISABLE_TELEMETRY='1'), self.root)

    def test_chat_context_resolves_workspace_without_a_work_session(self):
        from unittest.mock import patch
        bot = {'id': str(uuid4()), 'session_id': str(uuid4())}
        task = {'id': str(uuid4()), 'bot_id': bot['id'], '_execution_session_id': None,
                '_kanban_external_id': str(uuid4())}
        self.provider.catalog.resolve_runtime = lambda *args, **kwargs: {}
        with patch.object(self.provider, 'observe_bot', return_value=bot), patch.object(
                self.provider, 'resolve_task_model', return_value={'model': 'test', 'provider': 'custom'}):
            # Workspace resolution must reach the claim check for a conversation context.
            with self.assertRaisesRegex(RuntimeError, 'no prepared worker'):
                self.provider._run_turn('context', task, bot, [], lambda *args: None, isolated=True)

    def spawn_background(self):
        env, cwd = self.provider.environment('same-bot')
        env['COCONUT_PROCESS_HOME'] = str(self.root / str(uuid4()))
        code = '''
import json, os, sys
from coconut.hermes_worker import configure_process_checkpoint
configure_process_checkpoint()
from hermes_constants import get_hermes_home
from tools.process_registry import process_registry, CHECKPOINT_PATH
child = process_registry.spawn_local('exec sleep 120', cwd=os.getcwd(), task_id='owner')
print(json.dumps(dict(pid=child.pid, home=str(get_hermes_home()), checkpoint=str(CHECKPOINT_PATH))), flush=True)
for line in sys.stdin:
 request=json.loads(line)
 if request.get('operation') == 'runtime_state':
  print(json.dumps(dict(turn_id=request['turn_id'], type='runtime_state', data=dict(live_processes=any(p['status']=='running' for p in process_registry.list_sessions())))),flush=True)
'''
        process = subprocess.Popen([PYTHON, '-c', code], cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        self.provider._runtime_envs[process] = (env, cwd)
        self.addCleanup(lambda: self.provider._terminate(process))
        data = json.loads(process.stdout.readline())
        self.assertEqual(data['home'], str(self.profile))
        return process, data

    def test_forced_cleanup_leaves_the_other_workers_process_alive(self):
        first, one = self.spawn_background()
        second, two = self.spawn_background()
        self.assertNotEqual(one['checkpoint'], two['checkpoint'])
        self.assertEqual(json.loads(Path(one['checkpoint']).read_text())[0]['pid'], one['pid'])
        self.assertEqual(json.loads(Path(two['checkpoint']).read_text())[0]['pid'], two['pid'])
        self.provider._terminate(first)
        os.kill(two['pid'], 0)
        self.assertIsNone(second.poll())
        self.assertEqual(json.loads(Path(one['checkpoint']).read_text()), [])

    def test_deleting_bot_stops_its_retained_runtime(self):
        first_bot, second_bot = {'id': str(uuid4())}, {'id': str(uuid4())}
        first_id, second_id = str(uuid4()), str(uuid4())
        self.provider.prepare_kanban_worker('first', first_bot, str(self.root), first_id)
        self.provider.prepare_kanban_worker('second', second_bot, str(self.root), second_id)
        first = self.provider._kanban_runtimes[first_id]
        second = self.provider._kanban_runtimes[second_id]
        self.provider._prepared.pop('first')
        self.provider.stop_bot(first_bot['id'])
        self.assertIsNotNone(first.poll())
        self.assertIsNone(second.poll())
        self.assertNotIn(first_id, self.provider._kanban_runtimes)
        with self.assertRaisesRegex(RuntimeError, 'stopped'):
            self.provider.prepare_kanban_worker('late', first_bot, str(self.root), str(uuid4()))

    def test_native_tasks_keep_distinct_workers_and_one_workspace_owner(self):
        from unittest.mock import patch
        bot = {'id': str(uuid4())}
        execution_id = str(uuid4())
        first_id, second_id = str(uuid4()), str(uuid4())
        first_pid = self.provider.prepare_kanban_worker('first', bot, str(self.root), execution_id, first_id)
        second_pid = self.provider.prepare_kanban_worker('second', bot, str(self.root), execution_id, second_id)
        self.assertNotEqual(first_pid, second_pid)
        self.assertEqual(self.provider._runtime_envs[self.provider._kanban_runtimes[first_id]][0]
                         ['COCONUT_WORKSPACE_OUTCOME'], execution_id)
        self.assertEqual(self.provider._runtime_envs[self.provider._kanban_runtimes[second_id]][0]
                         ['COCONUT_WORKSPACE_OUTCOME'], execution_id)
        items = [dict(id='first', native_worker_id=first_id, execution_session_id=execution_id),
                 dict(id='second', native_worker_id=second_id, execution_session_id=execution_id)]
        with patch.object(self.provider, '_process_completions',
                          side_effect=lambda _bot, group, key: [(key, group[0]['id'])]):
            self.assertEqual(set(self.provider.process_completions(bot, items)),
                             {(first_id, 'first'), (second_id, 'second')})

    def test_idle_eviction_preserves_a_server_without_notifications(self):
        first, one = self.spawn_background()
        second, _ = self.spawn_background()
        self.provider._kanban_runtimes.update(first=first, second=second)
        self.provider.trim_kanban_workers(set())
        self.assertIsNone(first.poll())
        os.kill(one['pid'], 0)
        self.assertIs(self.provider._kanban_runtimes['first'], first)

    def test_cancelled_preparation_can_be_evicted(self):
        bot = {'id': str(uuid4())}
        execution = str(uuid4())
        self.provider.prepare_kanban_worker('cancelled', bot, str(self.root), execution)
        first = self.provider._kanban_runtimes[execution]
        self.provider.cancel_task('cancelled')
        self.provider.prepare_kanban_worker('next', bot, str(self.root), str(uuid4()))
        self.provider.release_kanban_preparation('next')
        self.assertNotIn('cancelled', self.provider._prepared)
        # A cold worker can time out its first resource probe. Like the scheduler,
        # retry trimming: an unanswered probe must preserve the worker, not kill it.
        deadline = time.monotonic() + 30
        while first.poll() is None and time.monotonic() < deadline:
            self.provider.trim_kanban_workers(set())
            with contextlib.suppress(subprocess.TimeoutExpired):
                first.wait(timeout=0.1)
        self.assertIsNotNone(first.poll())

    def test_idle_eviction_preserves_native_browser_state_until_it_closes(self):
        env, cwd = self.provider.environment('same-bot')
        env['COCONUT_PROCESS_HOME'] = str(self.root / str(uuid4()))
        code = '''
import json, sys
from coconut.hermes_worker import configure_process_checkpoint, runtime_state
configure_process_checkpoint()
from tools import browser_tool
browser_tool._active_sessions['owner'] = {'session_id': 'saved-form'}
print('ready', flush=True)
for line in sys.stdin:
 request=json.loads(line)
 if request.get('operation') == 'clear_browser':
  browser_tool._active_sessions.clear()
 elif request.get('operation') == 'runtime_state':
  print(json.dumps(dict(turn_id=request['turn_id'], type='runtime_state', data=runtime_state())),flush=True)
'''
        process = subprocess.Popen([PYTHON, '-c', code], cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        self.provider._runtime_envs[process] = (env, cwd)
        self.addCleanup(lambda: self.provider._terminate(process))
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        self.provider._kanban_runtimes['browser'] = process
        self.provider.prepare_kanban_worker('spare', {'id': str(uuid4())}, str(self.root), str(uuid4()))
        self.provider.release_kanban_preparation('spare')
        self.provider.trim_kanban_workers(set())
        self.assertIsNone(process.poll())
        self.provider._send(process, dict(operation='clear_browser'))
        self.provider.trim_kanban_workers(set())
        self.assertIsNotNone(process.poll())


if __name__ == '__main__':
    unittest.main()
