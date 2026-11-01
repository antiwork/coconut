"""Verify saved routes against the September 11 Hermes scheduler contract."""
import tempfile
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from coconut.providers import HermesProvider
from coconut.model_catalog import HermesModelCatalog


class HermesReleaseRoutineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.provider = HermesProvider(self.temporary.name, catalog=Mock())
        self.provider.resolve_model = Mock(side_effect=ValueError('Current default is unavailable'))
        self.saved = dict(model_snapshot='saved-model', provider_snapshot='saved-provider', deliver='local', id='job')

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


if __name__ == '__main__':
    unittest.main()
