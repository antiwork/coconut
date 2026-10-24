import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from coconut.service import Service, ServiceError
from test_service import ControlledProvider


class ServiceProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = ControlledProvider()
        self.service = Service(self.root, self.provider, start_workers=False)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None):
        return self.service.mutate(method, path, data or {}, str(uuid4()), 'phone')

    def bot(self, name='Ada'):
        return self.write('POST', '/v1/bots', {'name': name, 'instructions': name + ' instructions', 'memory': name + ' memory'})

    def home(self, bot):
        return self.root / 'bots' / bot['id'] / 'home'

    def test_bots_have_distinct_stable_profile_ids_and_managed_context(self):
        first, second = self.bot(), self.bot('Lin')
        self.assertEqual(first['profile_id'], first['id'])
        self.assertNotEqual(first['profile_id'], second['profile_id'])
        self.assertIn('Ada instructions', (self.home(first) / 'SOUL.md').read_text())
        self.assertNotIn('Lin instructions', (self.home(first) / 'SOUL.md').read_text())
        self.assertFalse((self.home(first) / 'COCONUT_MEMORY.md').exists())
        self.service.close()
        self.service = Service(self.root, self.provider, start_workers=False)
        recovered = {bot['id']: bot for bot in self.service.snapshot()['bots']}
        self.assertEqual(recovered[first['id']]['profile_id'], first['profile_id'])
        self.assertEqual(recovered[first['id']]['session_id'], first['session_id'])

    def test_legacy_bot_migration_preserves_profile_files_and_session(self):
        bot = self.bot()
        home = self.home(bot)
        (home / 'config.yaml').write_text('custom_setting: retained\n')
        (home / 'private.txt').write_text('retained')
        with self.service.transaction():
            stored = self.service._get('bots', bot['id'])
            stored.pop('profile_id')
            self.service._put('bots', stored)
        self.service.close()
        self.service = Service(self.root, self.provider, start_workers=False)
        migrated = self.service._get('bots', bot['id'])
        self.assertEqual(migrated['profile_id'], bot['id'])
        self.assertEqual(migrated['session_id'], bot['session_id'])
        self.assertEqual((home / 'config.yaml').read_text(), 'custom_setting: retained\n')
        self.assertEqual((home / 'private.txt').read_text(), 'retained')

    def test_archive_delete_and_instruction_edits_sync_the_same_profile(self):
        bot = self.bot()
        home = self.home(bot)
        updated = self.write('PATCH', f'/v1/bots/{bot["id"]}', {'name': 'Ada renamed', 'instructions': 'New instructions', 'archived': True})
        self.assertEqual(updated['profile_id'], bot['id'])
        self.assertIn('New instructions', (home / 'SOUL.md').read_text())
        self.assertTrue(self.service._get('bots', bot['id'])['archived'])
        self.write('PATCH', f'/v1/bots/{bot["id"]}', {'archived': False})
        self.assertFalse(self.service._get('bots', bot['id'])['archived'])
        self.write('DELETE', f'/v1/bots/{bot["id"]}')
        self.assertTrue(self.service._get('bots', bot['id']).get('_deleted'))
        self.assertTrue((home / 'SOUL.md').exists())

    def test_profile_files_change_only_after_database_commit(self):
        bot = self.bot()
        before = (self.home(bot) / 'SOUL.md').read_text()
        with patch.object(self.service, '_event', side_effect=RuntimeError('Simulated transaction failure')):
            with self.assertRaises(RuntimeError):
                self.write('PATCH', f'/v1/bots/{bot["id"]}', {'instructions': 'Must roll back', 'archived': True})
        self.assertEqual((self.home(bot) / 'SOUL.md').read_text(), before)
        self.assertFalse(self.service._get('bots', bot['id'])['archived'])
        self.assertFalse(self.service._get('bots', bot['id'])['archived'])

    def test_profile_identity_cannot_be_reassigned(self):
        bot = self.bot()
        with self.assertRaisesRegex(ServiceError, 'original Hermes profile'):
            self.write('PATCH', f'/v1/bots/{bot["id"]}', {'profile_id': str(uuid4())})
        self.assertEqual(self.service._get('bots', bot['id'])['profile_id'], bot['id'])


if __name__ == '__main__':
    unittest.main()
