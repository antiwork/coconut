import json
import tempfile
import unittest
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import patch

from coconut.native_bridge import NativeBridge

from coconut.providers import FixtureProvider
from coconut.service import Service


class WorkQueueIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(self.tmp.name, FixtureProvider(self.tmp.name), start_workers=False)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))

    def task(self, title):
        return self.service.mutate('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': title, 'mode': 'queue'}, str(uuid4()))

    def test_interrupted_callback_does_not_fail_other_pending_work(self):
        intake = self.task('Check assigned work')
        interrupted = self.task('Inspect runtime')
        publication = self.task('Publish reviewed fix')
        with self.service.transaction():
            intake = self.service._get('tasks', intake['id'])
            intake.update(status='waiting', _waiting_on=[publication['id']])
            self.service._put('tasks', intake)
            interrupted = self.service._get('tasks', interrupted['id'])
            interrupted.update(status='working', _assignment_task_id=intake['id'], _native_turn_id='interrupted-turn')
            self.service._put('tasks', interrupted)
            publication = self.service._get('tasks', publication['id'])
            publication['_assignment_task_id'] = intake['id']
            self.service._put('tasks', publication)
        self.service.emit(interrupted['id'], 'native_call', {'name': 'unknown_tool', 'arguments': {}})
        with self.service.transaction():
            self.service._recover()
        self.assertEqual(self.service._get('tasks', interrupted['id'])['status'], 'failed')
        self.assertEqual(self.service._get('tasks', intake['id'])['status'], 'waiting')
        self.service._run_task(publication['id'])
        self.assertNotIn('original assignment stopped', self.service._get('tasks', publication['id'])['summary'])
        self.assertEqual(self.service._all('native_receipts')[0]['state'], 'executing')

    def test_closed_native_run_does_not_block_next_intake(self):
        intake = self.task('Old intake')
        review = self.task('Review the assigned PR')
        with self.service.transaction():
            intake = self.service._get('tasks', intake['id'])
            intake.update(status='failed', routine_id='intake', _native_job_finished=True)
            self.service._put('tasks', intake)
            review = self.service._get('tasks', review['id'])
            review.update(status='waiting', source_task_id=intake['id'])
            self.service._put('tasks', review)
        self.assertIsNone(self.service._active_routine_task('intake'))
        with self.service.transaction():
            intake.update(status='working', _native_job_finished=False)
            self.service._put('tasks', intake)
        self.assertEqual(self.service._active_routine_task('intake')['id'], intake['id'])

    def test_bot_can_wait_for_its_own_notified_process_without_duplicate_subscription(self):
        task = self.task('Watch the published review')
        with self.service.transaction():
            task = self.service._get('tasks', task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        receipt = self.service.emit(task['id'], 'native_call', {'name': 'terminal', 'arguments': {}})
        self.service.emit(task['id'], 'native_result', {'id': receipt['id'], 'result': json.dumps(
            {'session_id': 'review-watch', 'notify_on_complete': True})})
        listed = self.service.wait_for_task(task['id'], {})
        self.assertIn('review-watch', [p['process_id'] for p in listed['processes']])
        for _ in range(2):
            result = self.service.wait_for_task(task['id'], {'process_id': 'review-watch', 'reason': 'Review checks'})
            self.assertEqual(result['status'], 'waiting')
        self.assertEqual(len(self.service._all('native_receipts')), 1)
        self.service.emit(task['id'], 'completed', {'summary': 'Waiting for review checks.'})
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'waiting')

    def test_native_run_uses_the_coconut_queue_and_records_one_receipt(self):
        caller = self.task('Run intake now')
        routine_id = str(uuid4())
        job = dict(id='native-intake', fire_claim={'by': 'owner'}, prompt='Check new assignments')
        def native(operation, bots, **arguments):
            self.assertEqual(operation, 'routine_claim')
            return {'job': job}
        self.service.provider.native_settings = native
        with self.service.transaction():
            caller = self.service._get('tasks', caller['id'])
            caller['status'] = 'working'
            self.service._put('tasks', caller)
            self.service._put('routines', dict(id=routine_id, bot_id=self.bot['id'], _native_id=job['id'], name='Intake', enabled=True))
        middleware = {}
        class Context:
            def __init__(self, *args): pass
            def register_middleware(self, kind, callback):
                middleware['call'] = callback
                return SimpleNamespace(dispose=lambda: None)
        modules = {
            'tools.approval': SimpleNamespace(register_gateway_notify=lambda *a: None),
            'hermes_cli.plugins': SimpleNamespace(PluginContext=Context, PluginManifest=lambda **kw: None, get_plugin_manager=lambda: None),
            'hermes_cli.middleware': SimpleNamespace(TOOL_EXECUTION_MIDDLEWARE='tool'),
        }
        bridge = NativeBridge({'operation': 'start', 'task': caller}, None,
                              lambda kind, data: self.service.emit(caller['id'], kind, data))
        with patch.dict('sys.modules', modules):
            bridge.install(None)
            result = middleware['call']('cronjob_manage', {'action': 'run', 'job_id': job['id']},
                                        lambda args: self.fail('The standalone runner must not execute.'))
        queued = json.loads(result)
        self.assertTrue(queued['success'])
        self.assertEqual(queued['scheduler'], 'coconut')
        self.assertEqual(self.service._get('tasks', queued['task_id'])['routine_id'], routine_id)
        record = self.service._all('native_receipts')[0]
        self.assertEqual(record['state'], 'completed')
        self.assertEqual(record['result'], result)
        again = self.service.emit(caller['id'], 'native_call', {'name': 'cronjob_manage', 'arguments': {'action': 'run', 'job_id': job['id']}})
        self.assertEqual(json.loads(again['routine_result'])['task_id'], queued['task_id'])
        self.assertEqual(len([t for t in self.service._all('tasks') if t.get('routine_id') == routine_id]), 1)
