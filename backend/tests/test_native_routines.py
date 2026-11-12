"""The routines of Hermes in Coconut: their view, their changes, and the settings write that a lost response repeats.

These tests come from the test files of Coconut's own routine claims, which are gone. They cover what stays.
"""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from coconut import native_routines
from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError


class ApprovalRecoveryTests(unittest.TestCase):
    def test_ambiguous_native_write_reconciles_without_repeating_or_overwriting(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = temp.name
        service = Service(root, FixtureProvider(root), start_workers=False)
        self.addCleanup(service.close)
        state = {'default': 'off', 'modes': [{'id': 'off'}, {'id': 'manual'}]}
        writes = []
        def native(operation='read', **values):
            if operation == 'set_default_approval':
                writes.append(values['mode'])
                state['default'] = values['mode']
                raise ConnectionError('Lost response after write.')
            return {'approvals': dict(state)}
        service.provider.native_settings = native
        key = str(uuid4())
        with self.assertRaises(ServiceError):
            service.mutate('PUT', '/v1/settings/approvals', {'mode': 'manual'}, key)
        result = service.mutate('PUT', '/v1/settings/approvals', {'mode': 'manual'}, key)
        self.assertEqual(result['default'], 'manual')
        state['default'] = 'off'
        service.mutate('PUT', '/v1/settings/approvals', {'mode': 'manual'}, key)
        self.assertEqual(state['default'], 'off')
        self.assertEqual(writes, ['manual'])


class RoutineChangeTests(unittest.TestCase):
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
        # A script that gates the agent run, and a script-only job, are routines too.
        self.assertIsNone(native_routines.unsupported(dict(self.job, script='intake_gate.py')))
        self.assertIsNone(native_routines.unsupported(dict(self.job, script='report.py', no_agent=True)))
        for field in ('no_agent', 'monitor_url', 'enabled_toolsets'):
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


class NativeRoutineViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = dict(id='job', prompt='Refresh', skills=['triage'], context_from=['self'],
                        provider_snapshot='native', workdir=self.tmp.name, enabled=True, deliver='local')
        # The Hermes modules that a view reads, without a Hermes install.
        modules = {
            'native_routines': native_routines,
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
        # A routine of Coconut's earlier claims has no claim to recover.
        self.assertNotIn('_recover_claim', routine)

    def test_a_routine_with_a_script_check_shows_its_script_and_an_edit_keeps_it(self):
        bot = str(uuid4())
        gated = dict(self.job, script='/home/scripts/intake_gate.py', schedule={'kind': 'cron', 'expr': '*/5 * * * *'})
        routine = native_routines.view(bot, gated)
        self.assertEqual((routine['error'], routine['script_check'], routine['script']), (None, 'intake_gate.py', None))
        self.assertEqual(routine['prompt'], 'Refresh')
        # A script-only job shows its script as before, with no check.
        alone = native_routines.view(bot, dict(gated, no_agent=True))
        self.assertEqual((alone['script'], alone['script_check']), ('/home/scripts/intake_gate.py', None))
        updates = []
        jobs = SimpleNamespace(update_job=lambda identity, changes: updates.append(changes),
                               list_jobs=lambda include_disabled: [gated], pause_job=lambda identity: None,
                               resume_job=lambda identity: None, parse_schedule=lambda expression: {'expr': expression})
        constants = SimpleNamespace(get_hermes_home=lambda: self.tmp.name, set_hermes_home_override=lambda home: None,
                                    reset_hermes_home_override=lambda token: None)
        with patch.dict('sys.modules', {'cron': SimpleNamespace(jobs=jobs), 'hermes_constants': constants}):
            native_routines.worker('routine_update', {'bot_id': bot, 'native_id': 'job', 'changes': {
                'name': 'Intake', 'prompt': 'Triage new events.', 'cron': '*/10 * * * *', 'enabled': True}},
                {bot: self.tmp.name})
        self.assertEqual(updates, [{'name': 'Intake', 'prompt': 'Triage new events.', 'schedule': {'expr': '*/10 * * * *'}}])

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
        self.assertEqual(set(result), {'routines'})


if __name__ == '__main__':
    unittest.main()
