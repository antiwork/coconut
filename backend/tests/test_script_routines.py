import tempfile
import unittest
from unittest.mock import Mock

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
