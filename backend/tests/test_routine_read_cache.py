"""The routine tick reads a bot's native routines again only after one of the files of that read changes."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut.model_catalog import HermesModelCatalog
from coconut.providers import HermesProvider


class RoutineReadCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bots = [{'id': str(uuid4())}, {'id': str(uuid4())}]
        for bot in self.bots:
            (self.home(bot) / 'cron').mkdir(parents=True)
            (self.home(bot) / 'cron' / 'jobs.json').write_text('{"jobs": []}')
        self.global_signature = ('default',)
        catalog = Mock()
        catalog.data_dir = self.root
        catalog._profile_home = lambda identity: self.root / 'bots' / identity / 'home'
        catalog._signature = lambda: self.global_signature
        catalog.routines_signature = lambda identity: HermesModelCatalog.routines_signature(catalog, identity)
        self.provider = HermesProvider(self.root, catalog=catalog)
        self.reads = []
        def native_settings(operation='read', bots=(), **payload):
            self.assertEqual(operation, 'routines_read')
            self.reads.append([bot['id'] for bot in bots])
            return {'routines': [{'id': bot['id'] + '-job', 'bot_id': bot['id'],
                                  'jobs': (self.home(bot) / 'cron' / 'jobs.json').read_text()} for bot in bots]}
        patcher = patch.object(self.provider, 'native_settings', side_effect=native_settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def home(self, bot):
        return self.root / 'bots' / bot['id'] / 'home'

    def test_unchanged_files_reuse_the_saved_routines_in_the_order_of_the_bots(self):
        first = self.provider.native_routines(self.bots)
        first['routines'][0]['jobs'] = 'Changed by a caller.'
        second = self.provider.native_routines(list(reversed(self.bots)))
        self.assertEqual([r['bot_id'] for r in second['routines']], [self.bots[1]['id'], self.bots[0]['id']])
        self.assertEqual(second['routines'][1]['jobs'], '{"jobs": []}')
        self.assertEqual(self.reads, [[bot['id'] for bot in self.bots]])

    def test_each_file_of_the_read_makes_its_bot_read_again(self):
        self.provider.native_routines(self.bots)
        bot = self.bots[1]
        for name in ('cron/jobs.json', 'cron/executions.db', 'cron/executions.db-wal', 'config.yaml', '.env'):
            with self.subTest(name=name):
                path = self.home(bot) / name
                path.write_text(name + ' changed')
                self.reads.clear()
                routines = self.provider.native_routines(self.bots)['routines']
                self.assertEqual(self.reads, [[bot['id']]])
                self.assertEqual([r['bot_id'] for r in routines], [b['id'] for b in self.bots])
        self.reads.clear()
        self.global_signature = ('changed default',)
        self.provider.native_routines(self.bots)
        self.assertEqual(self.reads, [[b['id'] for b in self.bots]])

    def test_a_failed_read_saves_nothing(self):
        self.provider.native_settings.side_effect = OSError('Hermes is unavailable.')
        with self.assertRaises(OSError):
            self.provider.native_routines(self.bots)
        self.assertEqual(self.provider._routines, {})

    def test_a_bot_that_is_gone_loses_its_saved_routines(self):
        self.provider.native_routines(self.bots)
        self.provider.native_routines(self.bots[:1])
        self.assertEqual(set(self.provider._routines), {self.bots[0]['id']})


if __name__ == '__main__':
    unittest.main()
