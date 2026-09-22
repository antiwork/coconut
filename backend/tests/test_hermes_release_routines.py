"""Verify saved routes against the September 11 Hermes scheduler contract."""
import tempfile
import subprocess
import sys
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coconut.native_routine_execution import job_context
from coconut.providers import HermesProvider
from coconut.model_catalog import HermesModelCatalog
from coconut import native_routines


class HermesReleaseRoutineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.provider = HermesProvider(self.temporary.name, catalog=Mock())
        self.provider.resolve_model = Mock(side_effect=ValueError('Current default is unavailable'))
        self.saved = dict(model_snapshot='saved-model', provider_snapshot='saved-provider', deliver='local', id='job')

    def test_saved_route_works_when_current_default_is_unavailable(self):
        result = self.provider.resolve_task_model({}, {'_native_job': self.saved})
        self.assertEqual(result, dict(model='saved-model', provider='saved-provider', reasoning_effort=None))
        self.provider.resolve_model.assert_not_called()

    def test_explicit_pins_override_saved_route(self):
        job = dict(self.saved, model='explicit-model', provider='explicit-provider')
        result = self.provider.resolve_task_model({}, {'_native_job': job})
        self.assertEqual(result['model'], 'explicit-model')
        self.assertEqual(result['provider'], 'explicit-provider')

    def test_partial_snapshot_uses_current_default_for_missing_axis(self):
        self.provider.resolve_model.side_effect = None
        self.provider.resolve_model.return_value = dict(model='current', provider='current-provider')
        result = self.provider.resolve_task_model({}, {'_native_job': {'model_snapshot': 'saved-model'}})
        self.assertEqual(result['model'], 'saved-model')
        self.assertEqual(result['provider'], 'current-provider')

    def test_job_owner_survives_settings_process_exit_and_stays_scoped(self):
        source = Path(self.temporary.name) / 'source'
        source.mkdir()
        (source / 'run_agent.py').touch()
        owners = []
        def run(args, **kwargs):
            owners.append(kwargs['env'].get('HERMES_MACHINE_ID'))
            return subprocess.CompletedProcess(args, 0, '{"result": {}}', '')
        with patch('coconut.model_catalog.subprocess.run', side_effect=run):
            for data in ('first', 'first', 'second'):
                catalog = HermesModelCatalog(source.parent / data, source=source, python=sys.executable)
                catalog._call('native', native_operation='routine_claim')
        self.assertTrue(owners[0].startswith('coconut-'))
        self.assertNotIn(':', owners[0])
        self.assertEqual(owners[0], owners[1])
        self.assertNotEqual(owners[0], owners[2])

    def test_new_scheduler_rejects_a_mismatched_route_before_execution(self):
        self.check_saved_route(SimpleNamespace(_snapshot_pin=Mock(
            side_effect=lambda job, axis, current, job_id: job.get(axis + '_snapshot', ''))))

    def test_scheduler_without_snapshot_helpers_preserves_saved_route(self):
        self.check_saved_route(SimpleNamespace())

    def check_saved_route(self, scheduler):
        guard = Mock()
        build = Mock(return_value='Native routine prompt')
        modules = {
            'native_routines': native_routines,
            'cron.scheduler': scheduler,
            'cron.scheduler_prompt': SimpleNamespace(_guard_job_credential_exfil=guard, _build_job_prompt=build),
            'gateway.session_context': SimpleNamespace(set_session_vars=Mock(), clear_session_vars=Mock()),
            'tools.terminal_tool': SimpleNamespace(record_session_cwd=Mock(), clear_session_cwd=Mock()),
        }
        request = dict(task={'id': 'task', '_native_job': self.saved}, model='changed', provider='saved-provider')
        with patch.dict('sys.modules', modules):
            with self.assertRaisesRegex(ValueError, 'scheduled model'):
                with job_context(request, {'provider': 'saved-provider'}, {}):
                    self.fail('The mismatched route must not run.')
            guard.assert_not_called()
            build.assert_not_called()
            request.update(model='saved-model', provider='changed-provider')
            with self.assertRaisesRegex(ValueError, 'scheduled provider'):
                with job_context(request, {'provider': 'changed-provider'}, {}):
                    self.fail('The mismatched provider must not run.')
            request['provider'] = 'saved-provider'
            request['model'] = 'saved-model'
            with job_context(request, {'provider': 'saved-provider'}, {}) as prompt:
                self.assertEqual(prompt, 'Native routine prompt')
            guard.assert_called_once_with(self.saved)
            build.assert_called_once_with(self.saved)


if __name__ == '__main__':
    unittest.main()
