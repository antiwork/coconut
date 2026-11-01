import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from coconut import native_routines, native_routine_receipts
from coconut.providers import FixtureProvider
from coconut.service import Service


class SchedulerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(self.tmp.name, FixtureProvider(self.tmp.name), start_workers=False)
        self.addCleanup(lambda: self.service.close())
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        self.job = dict(id='native-job', prompt='Refresh the private ledger.', skills=['work-triage'],
                        skill='work-triage', context_from=['self'], provider_snapshot='native',
                        workdir=self.tmp.name, enabled=True, deliver='local')

    def test_native_tool_job_with_skills_and_continuity_is_supported(self):
        self.assertIsNone(native_routines.unsupported(self.job))
        for field in ('script', 'no_agent', 'monitor_url', 'enabled_toolsets'):
            with self.subTest(field=field):
                self.assertIn(field, native_routines.unsupported(dict(self.job, **{field: 'configured'})))

    def test_newly_observed_routine_can_be_changed_before_scheduler_import(self):
        routine = dict(id=str(uuid4()), bot_id=self.bot['id'], name='Refresh', enabled=True,
                       _native_id=self.job['id'], next_run_at='2000-01-01T00:00:00Z')
        calls = []
        def native(operation='read', bots=(), **arguments):
            calls.append(operation)
            if operation == 'routines_read':
                return {'routines': [routine]}
            if operation == 'read':
                return {'approvals': {}}
            if operation == 'routine_update':
                return dict(routine, **arguments['changes'])
            raise AssertionError(operation)
        self.service.provider.native_settings = native
        self.assertEqual(self.service.snapshot()['routines'][0]['id'], routine['id'])
        self.assertEqual(self.service._all('routines'), [])
        updated = self.service.mutate('PATCH', f"/v1/routines/{routine['id']}", {'enabled': False}, str(uuid4()))
        self.assertFalse(updated['enabled'])
        self.assertIn('routine_update', calls)

    def test_due_job_creates_one_durable_task_and_survives_restart(self):
        identity = str(uuid4())
        routine = dict(id=identity, bot_id=self.bot['id'], name='Refresh', enabled=True,
                       _native_id=self.job['id'], next_run_at='2000-01-01T00:00:00Z')
        with self.service.transaction():
            self.service._put('routines', routine)
        job = dict(self.job, fire_claim={'by': 'owner'})
        calls = []
        def native(operation, bots, **arguments):
            calls.append(operation)
            if operation == 'routine_claim':
                return {'job': job}
            if operation == 'routine_heartbeat':
                return {'ok': True}
            raise AssertionError(operation)
        self.service.provider.native_settings = native
        self.service.refresh_native_routines = lambda: None
        self.service.tick_native_routines()
        tasks = self.service._all('tasks')
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['_native_job'], job)
        self.assertTrue(self.service.native_scheduler_status()['scheduler_running'])
        self.service._native_cron_next = 0
        self.service.tick_native_routines()
        self.assertEqual(calls.count('routine_claim'), 1)
        self.service.close()
        self.service = Service(self.tmp.name, FixtureProvider(self.tmp.name), start_workers=False)
        self.assertEqual(self.service._all('tasks')[0]['_native_job'], job)
        self.assertFalse(self.service.native_scheduler_status()['scheduler_running'])

    def test_silent_completion_preserves_prior_output_and_records_success(self):
        task = self.service.mutate('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Refresh'}, str(uuid4()))
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored.update(status='working', routine_id='routine', _native_job=dict(self.job, fire_claim={'by': 'owner'}))
            self.service._put('tasks', stored)
        self.service.emit(task['id'], 'completed', {'summary': 'Complete.', 'routine_silent': True})
        calls = []
        def native(operation, bots, **arguments):
            calls.append((operation, arguments))
            return {'ok': True}
        self.service.provider.native_settings = native
        self.service.refresh_native_routines = lambda: None
        self.service.tick_native_routines()
        self.assertEqual(calls[0][0], 'routine_finish')
        self.assertTrue(calls[0][1]['success'])
        self.assertEqual(calls[0][1]['output'], '')
        self.assertTrue(self.service._get('tasks', task['id'])['_native_job_finished'])
        self.assertFalse(any(m['role'] == 'assistant' for m in self.service._all('messages')))

    def test_silence_cannot_hide_exhausted_continuation_failure(self):
        task = self.service.mutate('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Refresh'}, str(uuid4()))
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored.update(status='working', _native_job=self.job, _work_retry=2,
                          _work_todos={'todos': [{'id': 'coconut-assignment', 'content': 'Refresh', 'status': 'pending'}]})
            self.service._put('tasks', stored)
        self.service.emit(task['id'], 'completed', {'summary': 'Complete.', 'routine_silent': True})
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'failed')
        self.assertFalse(self.service._get('tasks', task['id'])['_routine_silent'])
        self.assertTrue(any(m['kind'] == 'status' and 'unfinished' in m['text'] for m in self.service._all('messages')))

    def test_tool_receipt_reports_observed_scheduler_health(self):
        task = self.service.mutate('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'List schedules'}, str(uuid4()))
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        def status():
            return self.service.emit(task['id'], 'native_call', {'name': 'cronjob_manage', 'arguments': {'action': 'list'}})
        self.assertFalse(status()['scheduler_running'])
        self.service._native_cron_last_success = time.monotonic()
        self.assertTrue(status()['scheduler_running'])
        self.service._native_cron_last_success -= 61
        self.assertFalse(status()['scheduler_running'])
        self.service._native_cron_last_success = time.monotonic()
        self.service._native_cron_error = True
        self.assertFalse(status()['scheduler_running'])

    def test_sync_failure_never_reports_a_running_scheduler(self):
        self.service._native_cron_last_success = time.monotonic()
        self.service.refresh_native_routines = lambda: (_ for _ in ()).throw(RuntimeError())
        self.service.tick_native_routines()
        self.assertFalse(self.service.native_scheduler_status()['scheduler_running'])


class NativeRoutineViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = dict(id='job', prompt='Refresh', skills=['triage'], context_from=['self'],
                        provider_snapshot='native', workdir=self.tmp.name, enabled=True, deliver='local')
        # The Hermes modules that a view reads, without a Hermes install.
        modules = {
            'native_routines': native_routines,
            'native_routine_receipts': native_routine_receipts,
            'hermes_cli.config': SimpleNamespace(load_config_readonly=lambda: {}),
            'hermes_constants': SimpleNamespace(get_hermes_home=lambda: self.tmp.name),
            'cron': SimpleNamespace(jobs=SimpleNamespace(get_job=lambda identity: self.job)),
        }
        self.modules = patch.dict('sys.modules', modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_view_tells_a_one_time_job_and_its_state(self):
        bot = str(uuid4())
        once = dict(self.job, schedule={'kind': 'once', 'run_at': '2026-10-29T12:15:00+00:00', 'display': 'once in 15m'},
                    state='completed', enabled=False, next_run_at=None)
        routine = native_routines.view(bot, once)
        self.assertTrue(routine['one_time'])
        self.assertEqual(routine['state'], 'completed')
        self.assertEqual(routine['run_at'], '2026-10-29T12:15:00+00:00')
        self.assertEqual(routine['cron'], 'once in 15m')

    def test_view_tells_a_recurring_job(self):
        daily = dict(self.job, schedule={'kind': 'cron', 'expr': '0 8 * * *', 'display': '0 8 * * *'},
                     state='scheduled', next_run_at='2026-10-30T08:00:00+00:00')
        routine = native_routines.view(str(uuid4()), daily)
        self.assertFalse(routine['one_time'])
        self.assertEqual(routine['state'], 'scheduled')
        self.assertIsNone(routine['run_at'])
        self.assertEqual(routine['next_run_at'], '2026-10-30T08:00:00+00:00')

    def test_routines_read_still_sends_every_one_time_job(self):
        # Other code reads finished one-time jobs, so the server keeps them. The phone hides them.
        jobs = [dict(self.job, id=identity, schedule={'kind': 'once', 'run_at': '2026-10-01T00:00:00+00:00'}, state=state)
                for identity, state in (('done', 'completed'), ('waiting', 'scheduled'), ('stopped', 'paused'))]
        cron = SimpleNamespace(jobs=SimpleNamespace(list_jobs=lambda include_disabled: jobs))
        constants = SimpleNamespace(get_hermes_home=lambda: self.tmp.name, set_hermes_home_override=lambda home: None,
                                    reset_hermes_home_override=lambda token: None)
        with patch.dict('sys.modules', {'cron': cron, 'hermes_constants': constants}):
            result = native_routines.worker('routines_read', {}, {str(uuid4()): self.tmp.name})
        self.assertEqual([(r['_native_id'], r['one_time'], r['state']) for r in result['routines']],
                         [('done', True, 'completed'), ('waiting', True, 'scheduled'), ('stopped', True, 'paused')])
