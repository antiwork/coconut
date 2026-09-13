import copy
import json
import tempfile
import unittest
from uuid import uuid4

from coconut.service import Service
from coconut.work_progress import NativeWork, ROOT_ID
from test_background_processes import CompletionProvider


def progress(status='pending'):
    return {'todos': [{'id': ROOT_ID, 'content': 'Validate and publish the approved patch', 'status': status}], 'revision': 2}


class Provider(CompletionProvider):
    def __init__(self):
        super().__init__()
        self.mode = 'unfinished'

    def start_task(self, task, bot, history, emit):
        self.starts.append((task, bot, history))
        if bot['name'] == 'Desk':
            emit('text_delta', {'text': 'Received the current result.'})
            emit('completed', {'summary': 'Complete.'})
            return
        state = progress('completed' if self.mode == 'done' else 'pending')
        if self.mode == 'process':
            record = emit('native_call', {'name': 'terminal', 'arguments': {'background': True, 'notify': True}})
            emit('native_result', {'id': record['id'], 'result': json.dumps({'session_id': 'proc_test', 'notify_on_complete': True, 'status': 'running'}), '_work_todos': state})
        if self.mode == 'progress':
            record = emit('native_call', {'name': 'todo_list', 'arguments': {}, '_work_todos': state})
            emit('native_result', {'id': record['id'], 'result': json.dumps(state), '_work_todos': state})
            emit('bot_message', {'target_bot_id': task['source_bot_id'], 'text': 'Validation remains unfinished.'})
        if self.mode == 'decision':
            emit('native_question', {'title': 'Approve the preview?', 'options': [{'id': 'yes', 'label': 'Approve', 'explanation': 'Publish the checked patch.'}], '_work_todos': state})
            return
        emit('text_delta', {'text': 'Published the validated patch.' if self.mode == 'done' else 'The patch still needs validation.'})
        emit('completed', {'summary': 'Complete.', '_work_todos': state})


class WorkProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = Provider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.desk = self.write('/v1/bots', {'name': 'Desk', 'role': 'Coordinator'})
        self.dev = self.write('/v1/bots', {'name': 'Dev', 'role': 'Development'})
        self.parent = self.write(f'/v1/bots/{self.desk["id"]}/messages', {'text': 'Finish the approved patch'})
        with self.service.transaction():
            parent = self.service._get('tasks', self.parent['id'])
            parent['status'] = 'working'; self.service._put('tasks', parent)
            child = self.service._store_bot_message(parent['id'], {'target_bot_id': self.dev['id'], 'text': 'Validate and publish the approved patch'})
        self.child = child['task_id']
        self.service.emit(self.parent['id'], 'completed', {'summary': 'Delegated.'})

    def tearDown(self):
        self.service.close(); self.temp.cleanup()

    def write(self, path, data):
        return self.service.mutate('POST', path, data, str(uuid4()), 'test-phone')

    def test_unfinished_reply_resumes_before_returning_to_desk(self):
        users = len([m for m in self.service._all('messages') if m['role'] == 'user'])
        self.service._run_task(self.child)
        task = self.service._get('tasks', self.child)
        self.assertEqual(task['status'], 'queued')
        self.assertTrue(task['_work_resume'])
        self.assertNotIn('_return_pending', task)
        self.assertEqual(self.service._get('tasks', self.parent['id'])['status'], 'waiting')
        self.service._drain_returns()
        self.assertFalse(any(t.get('_is_return') for t in self.service._all('tasks')))
        self.provider.mode = 'done'
        self.service._run_task(self.child)
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'completed')
        self.service._drain_returns()
        returns = [t for t in self.service._all('tasks') if t.get('_is_return')]
        self.assertEqual(len(returns), 1)
        self.assertEqual(returns[0]['bot_id'], self.desk['id'])
        self.assertEqual(users, len([m for m in self.service._all('messages') if m['role'] == 'user']))

    def test_progress_message_keeps_dependency_and_cannot_suppress_final_return(self):
        self.provider.mode = 'progress'
        self.service._run_task(self.child)
        update = next(t for t in self.service._all('tasks') if t.get('_is_return'))
        self.assertNotIn('_resolved_dependency_id', update)
        self.service._run_task(update['id'])
        parent = self.service._get('tasks', self.parent['id'])
        self.assertEqual(parent['status'], 'waiting')
        self.assertIn(self.child, parent['_waiting_on'])
        self.provider.mode = 'done'
        self.service._run_task(self.child)
        self.service._drain_returns()
        returns = [t for t in self.service._all('tasks') if t.get('_is_return') and t['status'] == 'queued']
        self.assertEqual(len(returns), 1)
        self.assertEqual(returns[0]['_resolved_dependency_id'], self.child)
        self.service._run_task(returns[0]['id'])
        self.assertEqual(self.service._get('tasks', self.parent['id'])['status'], 'completed')

    def test_resolved_dependency_does_not_hide_remaining_native_work(self):
        with self.service.transaction():
            original = self.service._get('tasks', self.child)
            original['_waiting_on'] = ['finished-dependency']
            original['_work_todos'] = progress()
            self.service._put('tasks', original)
            continuation = dict(original, id=str(uuid4()), _assignment_task_id=self.child,
                                _resolved_dependency_id='finished-dependency', _waiting_on=[])
            self.service._put('tasks', continuation)
        self.service._run_task(continuation['id'])
        self.assertEqual(self.service._get('tasks', continuation['id'])['status'], 'queued')
        self.assertTrue(self.service._get('tasks', continuation['id'])['_work_resume'])

    def test_restart_preserves_one_continuation_and_native_list(self):
        self.service._run_task(self.child)
        turn = self.service._get('tasks', self.child)['_native_turn_id']
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.assertEqual(self.service._get('tasks', self.child)['_native_turn_id'], turn)
        self.provider.mode = 'done'
        self.service._run_task(self.child)
        self.assertEqual(self.provider.starts[-1][0]['_work_todos'], progress())
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'completed')

    def test_repeated_empty_turns_fail_and_notify_owner(self):
        for _ in range(3): self.service._run_task(self.child)
        task = self.service._get('tasks', self.child)
        self.assertEqual(task['status'], 'failed')
        self.assertIn('unfinished', task['summary'])
        self.assertEqual(task['_return_pending']['result_status'], 'failed')
        self.service._run_task(self.child)
        self.assertEqual(len(self.provider.starts), 3)

    def test_recorded_process_owns_the_next_wakeup(self):
        self.provider.mode = 'process'
        self.service._run_task(self.child)
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'waiting')
        self.assertNotIn('_work_retry', self.service._get('tasks', self.child))
        self.provider.results['proc_test'] = 'Validation completed'
        self.service._drain_process_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'queued')
        self.provider.mode = 'done'
        self.service._run_task(self.child)
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'completed')

    def test_decision_saves_list_without_automatic_retry(self):
        self.provider.mode = 'decision'
        self.service._run_task(self.child)
        task = self.service._get('tasks', self.child)
        self.assertEqual(task['status'], 'needs_you')
        self.assertEqual(task['_work_todos'], progress())
        self.assertNotIn('_work_retry', task)
        self.service._run_task(self.child)
        self.assertEqual(len(self.provider.starts), 1)

    def test_cancelled_continuation_does_not_run(self):
        self.service._run_task(self.child)
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task['status'] = 'cancelled'; self.service._put('tasks', task)
        self.service._run_task(self.child)
        self.assertEqual(len(self.provider.starts), 1)

    def test_real_input_resets_retry_budget(self):
        self.service._run_task(self.child)
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            self.service._advance_input(task)
            self.assertNotIn('_work_retry', task)
            self.assertNotIn('_work_resume', task)
            self.assertIn('_work_todos', task)


class NativeWorkTests(unittest.TestCase):
    def request(self, **extra):
        return {'operation': 'start', 'task': {'title': 'Validate and publish', 'source_bot_id': 'desk', **extra}}

    def test_delegation_has_required_native_root(self):
        work = NativeWork(self.request())
        self.assertTrue(work.enabled)
        self.assertEqual(work.initial()['todos'][0]['status'], 'pending')

    def test_context_and_read_only_review_do_not_resume_implementation(self):
        request = self.request(); request['operation'] = 'context'
        self.assertFalse(NativeWork(request).enabled)
        self.assertFalse(NativeWork(self.request(_pr_review={'url': 'https://github.com/a/b/pull/1'})).enabled)
        self.assertFalse(NativeWork({'operation': 'start', 'task': {'title': 'What is the status?'}}).enabled)

    def test_erasing_list_cannot_hide_unfinished_assignment(self):
        class Store:
            def snapshot(self): return {'todos': [], 'revision': 8}
        class Agent: _todo_store = Store()
        state = NativeWork(self.request()).payload(Agent())['_work_todos']
        self.assertEqual(state['todos'][0]['id'], ROOT_ID)
        self.assertEqual(state['todos'][0]['status'], 'pending')
