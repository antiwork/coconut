"""Integration checks against the installed Hermes kernel and Coconut service."""
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service

SOURCE = os.environ.get('COCONUT_HERMES_ROOT')
PYTHON = os.environ.get('COCONUT_HERMES_PYTHON')


class Provider(FixtureProvider):
    def __init__(self, root):
        super().__init__(root)
        self.hermes_root, self.python = Path(SOURCE), PYTHON
        self.processes = []
        self.started = []
        self.release = threading.Event()
        self.release.set()

    def prepare_kanban_worker(self, identity, bot, workspace, execution_id):
        process = subprocess.Popen(['sleep', '90'])
        self.processes.append(process)
        return process.pid

    def start_task(self, task, bot, history, emit):
        self.started.append(task['id'])
        self.release.wait(15)
        super().start_task(task, bot, history, emit)

    def close(self):
        self.release.set()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


@unittest.skipUnless(SOURCE and PYTHON, 'Set Coconut Hermes paths for native integration checks.')
class NativeKanbanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = Provider(self.tmp.name)
        self.service = Service(self.tmp.name, self.provider, start_workers=False)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.service.close())
        self.bot = self.write('/v1/bots', {'name': 'Desk', 'role': ''})

    def write(self, path, data, key=None):
        return self.service.mutate('POST', path, data, key or str(uuid4()))

    def task(self, text):
        return self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': text, 'mode': 'queue'})

    def tick(self):
        self.service._dispatch_pending()

    def settle(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.tick()
            if predicate():
                return
        self.fail('The native queue did not settle.')

    def test_review_reply_resumes_same_claimed_task_once(self):
        task = self.task('Draft a note.md')
        self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'review')
        native_id = self.service.kanban.tasks[task['id']]['id']
        for _ in range(2):
            self.tick()
        self.assertEqual(self.provider.started, [task['id']])
        card = self.service._all('decisions')[0]
        path = '/v1/decisions/' + card['id'] + '/answer'
        data = {'option_id': 'full', 'revision': card['revision']}
        key = str(uuid4())
        result = self.write(path, data, key)
        self.assertEqual(result, self.write(path, data, key))
        self.settle(lambda: self.service.kanban.tasks[task['id']]['status'] == 'done')
        self.assertEqual(self.service.kanban.tasks[task['id']]['id'], native_id)
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM receipts').fetchone()[0], 1)
        self.assertEqual(len(self.provider.processes), 2)

    def test_native_capacity_and_fifo_then_cancellation(self):
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 2}, str(uuid4()))
        tasks = [self.task('Calculate item ' + str(i)) for i in range(4)]
        self.provider.release.clear()
        self.settle(lambda: len(self.provider.started) == 2)
        self.assertEqual(set(self.provider.started), {t['id'] for t in tasks[:2]})
        with self.service.lock:
            positions = self.service._queue_positions({t['id']: self.service._get('tasks', t['id']) for t in tasks}, time.time())
        self.assertEqual(positions[tasks[2]['id']], 1)
        self.assertEqual(positions[tasks[3]['id']], 2)
        cancelled = self.service._get('tasks', tasks[2]['id'])
        with self.service.transaction():
            cancelled['status'] = 'cancelled'
            self.service._put('tasks', cancelled)
        self.tick()
        self.provider.release.set()
        self.settle(lambda: self.service.kanban.tasks[tasks[3]['id']]['status'] == 'done')
        self.assertNotIn(tasks[2]['id'], self.provider.started)
        self.assertEqual(self.service.kanban.tasks[tasks[2]['id']]['status'], 'archived')

    def test_lost_dispatch_reply_uses_saved_claim(self):
        task = self.task('Calculate one item')
        original = self.service.kanban.call
        lost = []
        def call(request, spawn=None):
            result = original(request, spawn)
            if request['op'] == 'tick' and not lost:
                lost.append(True)
                raise ConnectionError('Lost acknowledgement after native claim commit')
            return result
        self.service.kanban.call = call
        with self.assertRaises(ConnectionError):
            self.tick()
        self.assertFalse(self.provider.started)
        self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'done')
        self.assertEqual(self.provider.started, [task['id']])
        self.assertEqual(len(self.provider.processes), 1)

    def test_lost_finish_reply_does_not_repeat_work(self):
        task = self.task('Calculate one item')
        original = self.service.kanban.call
        lost = []
        def call(request, spawn=None):
            result = original(request, spawn)
            if request['op'] == 'finish' and not lost:
                lost.append(True)
                raise ConnectionError('Lost finish acknowledgement')
            return result
        self.service.kanban.call = call
        with self.assertRaises(ConnectionError):
            for _ in range(4):
                self.tick()
        self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'done')
        self.assertEqual(self.provider.started, [task['id']])

    def interrupt_before_pid_commit(self, save_claim, cancel=False):
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 1}, str(uuid4()))
        task = self.task('Calculate one item')
        original = self.service.kanban.call
        interrupted = []
        def call(request, spawn=None):
            if request['op'] != 'tick' or interrupted:
                return original(request, spawn)
            def stop(claim):
                interrupted.append(True)
                if save_claim:
                    spawn(claim)
                self.service.kanban.process.kill()
                self.service.kanban.process.wait(timeout=5)
                raise ConnectionError('Adapter stopped before the native PID commit')
            return original(request, stop)
        self.service.kanban.call = call
        with self.assertRaises((OSError, RuntimeError)):
            self.tick()
        self.service.kanban.call = original
        if cancel:
            self.write('/v1/tasks/' + task['id'] + '/cancel', {})
        original(dict(op='snapshot'))
        native = self.service.kanban.tasks[task['id']]
        self.assertEqual(native['status'], 'running')
        self.assertIsNone(native['worker_pid'])
        self.service._reconcile_kanban()
        self.assertFalse(self.provider.started)
        original(dict(op='snapshot'))
        self.assertEqual(self.service.kanban.tasks[task['id']]['status'], 'archived' if cancel else 'ready')
        if cancel:
            task = self.task('Start the next item')
        self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'done')
        self.assertEqual(self.provider.started, [task['id']])

    def test_interrupted_preparation_before_coconut_claim_recovers(self):
        self.interrupt_before_pid_commit(save_claim=False)

    def test_interrupted_preparation_after_coconut_claim_recovers(self):
        self.interrupt_before_pid_commit(save_claim=True)

    def test_cancel_before_coconut_claim_save_releases_capacity(self):
        self.interrupt_before_pid_commit(save_claim=False, cancel=True)

    def test_cancel_after_coconut_claim_save_releases_capacity(self):
        self.interrupt_before_pid_commit(save_claim=True, cancel=True)

    def test_interrupted_schedule_unblock_replays_unchanged_readiness(self):
        task = self.task('Calculate one item')
        row = self.service._kanban_rows()[0]
        self.service.kanban.call(dict(op='tick', rows=[row], capacity=1),
            lambda claim: self.provider.prepare_kanban_worker(task['id'], self.bot, row['workspace'], task['id']))
        native = self.service.kanban.tasks[task['id']]
        program = '''
import sys
from coconut.kanban_runtime import Board
board = Board(sys.argv[1])
task = board.kb.get_task(board.conn, sys.argv[2])
def interrupted(*args, **kwargs):
    raise SystemExit(0)
board.kb.unblock_task = interrupted
board.finish(task.id, task.current_run_id, 'ready', 'Interrupted preparation')
raise RuntimeError('The interruption did not run')
'''
        result = subprocess.run([PYTHON, '-c', program, str(self.service.root / 'kanban'), native['id']],
            env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[1]), SOURCE))),
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.service.kanban.call(dict(op='snapshot'))
        self.assertEqual(self.service.kanban.tasks[task['id']]['status'], 'scheduled')
        self.tick()
        self.assertIn(self.service.kanban.tasks[task['id']]['status'], ('running', 'done'))
        self.settle(lambda: self.service.kanban.tasks[task['id']]['status'] == 'done')
        self.assertEqual(self.provider.started, [task['id']])

    def test_restart_preserves_review_without_dispatch(self):
        task = self.task('Draft a note.md')
        self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'review')
        native_id = self.service.kanban.tasks[task['id']]['id']
        self.service.close()
        self.provider = Provider(self.tmp.name)
        self.service = Service(self.tmp.name, self.provider, start_workers=False)
        self.tick()
        self.assertEqual(self.service.kanban.tasks[task['id']]['id'], native_id)
        self.assertEqual(self.service.kanban.tasks[task['id']]['status'], 'review')
        self.assertFalse(self.provider.started)

    def test_inherited_board_paths_cannot_redirect_coconut_dispatch(self):
        foreign = self.service.root / 'unrelated-board'
        foreign.mkdir()
        with patch.dict(os.environ, dict(HERMES_KANBAN_DB=str(foreign / 'kanban.db'),
                HERMES_KANBAN_BOARD='unrelated', HERMES_KANBAN_WORKSPACES_ROOT=str(foreign / 'workspaces'),
                HERMES_KANBAN_ATTACHMENTS_ROOT=str(foreign / 'attachments'))):
            task = self.task('Calculate one item')
            self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'done')
        self.assertTrue((self.service.root / 'kanban/kanban.db').is_file())
        self.assertEqual(list(foreign.iterdir()), [])
        self.assertEqual(self.provider.started, [task['id']])

    def test_repeated_dependency_waits_do_not_trigger_native_triage(self):
        task = self.task('Continue after each dependency result')
        row = self.service._kanban_rows()[0]
        for index in range(4):
            row.update(state='scheduled', input=str(index))
            self.service.kanban.call(dict(op='tick', rows=[row], capacity=1))
            native = self.service.kanban.tasks[task['id']]
            self.assertEqual(native['status'], 'scheduled')
            row['state'] = 'ready'
            self.service.kanban.call(dict(op='tick', rows=[row], capacity=1),
                lambda claim: self.provider.prepare_kanban_worker(task['id'], self.bot, row['workspace'], task['id']))
            native = self.service.kanban.tasks[task['id']]
            self.assertEqual(native['status'], 'running')
            self.assertEqual(native['block_recurrences'], 0)
            self.service.kanban.call(dict(op='finish', id=native['id'], run_id=native['current_run_id'],
                                          state='scheduled', summary='Waiting for a dependency'))
        row['state'] = 'review'
        self.service.kanban.call(dict(op='tick', rows=[row], capacity=1))
        self.assertEqual(self.service.kanban.tasks[task['id']]['status'], 'review')

    def test_queued_dependency_remains_available_to_wait(self):
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 1}, str(uuid4()))
        parent = self.task('Inspect the change')
        child = self.task('Check its dependencies')
        with self.service.transaction():
            saved = self.service._get('tasks', child['id'])
            saved['exchange_id'] = parent['exchange_id']
            self.service._put('tasks', saved)
        self.provider.release.clear()
        self.settle(lambda: self.provider.started == [parent['id']])
        self.assertEqual(self.service.kanban.tasks[child['id']]['status'], 'ready')
        result = self.service.wait_for_task(parent['id'], dict(task_id=child['id'], reason='Waiting for dependencies'))
        self.assertEqual(result['status'], 'waiting')

    def test_status_question_runs_when_native_capacity_is_full(self):
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 1}, str(uuid4()))
        owner = self.task('Inspect the change')
        original = self.provider.start_task
        def start(task, bot, history, emit):
            if task.get('_work_status_only'):
                emit('completed', {'summary': 'The worker is still checking the change.'})
            else:
                original(task, bot, history, emit)
        self.provider.start_task = start
        self.provider.release.clear()
        self.settle(lambda: owner['id'] in self.provider.started)
        item = self.service._register_work(owner['id'], dict(key='change', title='Inspect the change',
            activity='Checking the change', status='working'))
        question = self.write('/v1/bots/' + self.bot['id'] + '/messages', dict(text='How is this going?',
            work_artifact_id=item['id'], work_status_only=True, mode='queue'))
        self.settle(lambda: self.service._get('tasks', question['id'])['status'] == 'completed'
                    and owner['id'] in self.provider.started)
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'running')
        self.assertNotIn(question['id'], self.service.kanban.tasks)

    def test_native_permission_guard_enters_recovery_instead_of_staying_queued(self):
        task = self.task('Inspect this repository')
        def refused(*_args):
            raise PermissionError('Permission denied: worker executable')
        self.provider.prepare_kanban_worker = refused
        self.settle(lambda: self.service._get('tasks', task['id'])['status'] == 'failed')
        saved = self.service._get('tasks', task['id'])
        self.assertEqual(saved['error_code'], 'native_dispatch_failed')
        self.assertFalse(self.provider.started)

    def test_failed_dispatch_records_both_delegation_returns(self):
        dev = self.write('/v1/bots', {'name': 'Dev', 'role': ''})
        parent = self.task('Check this change with Dev')
        with self.service.transaction():
            saved = self.service._get('tasks', parent['id'])
            saved['status'] = 'working'
            self.service._put('tasks', saved)
            receipt = self.service._store_bot_message(parent['id'], dict(target_bot_id=dev['id'],
                text='Check this change', native_envelope_id=uuid4().hex))
            saved = self.service._get('tasks', parent['id'])
            saved['status'] = 'waiting'
            self.service._put('tasks', saved)
        def refused(*_args):
            raise PermissionError('Permission denied: worker executable')
        self.provider.prepare_kanban_worker = refused
        self.settle(lambda: self.service._get('tasks', receipt['task_id'])['status'] == 'failed')
        child = self.service._get('tasks', receipt['task_id'])
        self.assertEqual(child['_return_pending']['target_bot_id'], self.bot['id'])
        self.assertEqual(child['_return_pending']['result_status'], 'failed')
        self.assertTrue(child['_native_reply_pending']['failed'])
        self.assertEqual(child['_native_relay_result'], child['_native_reply_pending'])
        self.service._reconcile_kanban()
        self.assertEqual(self.service._get('tasks', child['id'])['_native_relay_result'], child['_native_relay_result'])

    def test_cancelled_receipt_owner_does_not_retain_a_runtime(self):
        task = self.task('Wait for the server')
        with self.service.transaction():
            saved = self.service._get('tasks', task['id'])
            saved['status'] = 'cancelled'
            self.service._put('tasks', saved)
        self.service._pending_completion_receipts = lambda: [dict(task_id=task['id'])]
        kept = []
        self.provider.trim_kanban_workers = lambda identities: kept.append(identities)
        self.tick()
        self.assertEqual(kept, [set()])


if __name__ == '__main__':
    unittest.main()
