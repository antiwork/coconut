"""A turn reads the bot's native profile settings again only after one of their files changes."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut.providers import HermesProvider


class ProfileSettingsCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bot = {'id': str(uuid4())}
        self.home = self.root / 'bots' / self.bot['id'] / 'home'
        self.home.mkdir(parents=True)
        (self.home / 'SOUL.md').write_text('First instructions.')
        self.global_signature = ('default',)
        from coconut.model_catalog import HermesModelCatalog
        catalog = Mock()
        catalog.data_dir = self.root
        catalog._profile_home = lambda identity: self.root / 'bots' / identity / 'home'
        catalog._signature = lambda: self.global_signature
        catalog.profile_signature = lambda identity: HermesModelCatalog.profile_signature(catalog, identity)
        catalog.read_profiles = lambda identities: {}
        self.provider = HermesProvider(self.root, catalog=catalog)
        self.reads = []
        def native_settings(operation='read', bots=(), **payload):
            self.reads.append([bot['id'] for bot in bots])
            return {'profiles': {bot['id']: {'instructions': (self.home / 'SOUL.md').read_text()} for bot in bots}}
        patcher = patch.object(self.provider, 'native_settings', side_effect=native_settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unchanged_files_reuse_the_saved_settings(self):
        first = self.provider.profile_settings([self.bot])
        first[self.bot['id']]['instructions'] = 'Changed by a caller.'
        self.assertEqual(self.provider.profile_settings([self.bot])[self.bot['id']]['instructions'], 'First instructions.')
        self.assertEqual(len(self.reads), 1)

    def test_each_turn_observes_the_bot_without_a_new_settings_read(self):
        for _ in range(3):
            self.assertEqual(self.provider.observe_bot(self.bot)['instructions'], 'First instructions.')
        self.assertEqual(len(self.reads), 1)

    def test_a_changed_profile_file_or_default_reads_again(self):
        self.provider.profile_settings([self.bot])
        (self.home / 'SOUL.md').write_text('Second, longer instructions.')
        self.assertEqual(self.provider.profile_settings([self.bot])[self.bot['id']]['instructions'], 'Second, longer instructions.')
        (self.home / 'config.yaml').write_text('approvals: {mode: smart}\n')
        self.provider.profile_settings([self.bot])
        self.global_signature = ('changed default',)
        self.provider.profile_settings([self.bot])
        self.assertEqual(len(self.reads), 4)


if __name__ == '__main__':
    unittest.main()
