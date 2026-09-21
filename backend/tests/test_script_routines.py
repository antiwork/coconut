import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from uuid import uuid4

from coconut.native_routines import unsupported
from coconut.providers import HermesProvider


class ScriptRoutineTests(unittest.TestCase):
    def test_native_script_needs_no_model(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = HermesProvider(folder, catalog=Mock())
            provider.resolve_model = Mock(side_effect=AssertionError('Model access is forbidden'))
            job = dict(script='cleanup.py', no_agent=True, deliver='local')
            self.assertIsNone(unsupported(job, {'cron': {'model': 'missing', 'model_provider': 'missing'}}))
            self.assertEqual(provider.resolve_task_model({'_model_error': 'unavailable'}, {'_native_job': job}),
                             dict(model=None, provider=None))
            provider.resolve_model.assert_not_called()

    def test_unsupported_execution_stays_rejected(self):
        for job in ({'script': 'cleanup.py'}, {'no_agent': True},
                    {'script': 'cleanup.py', 'no_agent': True, 'monitor_url': 'https://example.com'},
                    {'script': 'cleanup.py', 'no_agent': True, 'deliver': 'telegram'}):
            with self.subTest(job=job):
                self.assertIsNotNone(unsupported(job))

    def test_script_error_survives_service_result_handling(self):
        from coconut.providers import FixtureProvider
        from coconut.service import Service
        from coconut.native_routine_execution import run_script_job
        from coconut import native_routines
        with tempfile.TemporaryDirectory() as folder:
            service = Service(folder, FixtureProvider(folder), start_workers=False)
            bot = service.mutate('POST', '/v1/bots', {'name': 'Care'}, str(uuid4()))
            task = service.mutate('POST', '/v1/bots/' + bot['id'] + '/messages', {'text': 'Run cleanup'}, str(uuid4()))
            job = dict(no_agent=True, script='cleanup.py')
            with service.transaction():
                saved = service._get('tasks', task['id'])
                saved.update(status='working', _native_job=job)
                service._put('tasks', saved)
            scheduler = SimpleNamespace(run_job=lambda *a, **kw: (False, 'report', 'Error details', 'Script exited with code 7'),
                                        _is_cron_silence_response=lambda text: False)
            with patch.dict('sys.modules', {'cron.scheduler': scheduler, 'native_routines': native_routines}):
                result = run_script_job(job, None, lambda kind, data: service.emit(task['id'], kind, data))
            self.assertEqual(result, 1)
            saved = service._get('tasks', task['id'])
            self.assertEqual(saved['status'], 'failed')
            self.assertIn('Script exited with code 7', saved['summary'])
            self.assertTrue(any('Script exited with code 7' in message['text'] for message in service.snapshot()['messages']))
            service.close()
