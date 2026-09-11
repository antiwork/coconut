import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from coconut.profiles import HermesProfiles, ProfileError, inspect_profiles, merge_runtime_config, profile_id


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profiles = HermesProfiles(self.root)
        self.bot = dict(id=str(uuid4()), session_id=str(uuid4()), name='Ada', role='Reviewer',
                        instructions='Keep replies brief.', memory='Use metric units.', preferences=[], archived=False)
        self.bot['profile_id'] = self.bot['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_two_bots_have_stable_distinct_real_profile_homes(self):
        other = self.bot | {'id': str(uuid4()), 'session_id': str(uuid4())}
        other['profile_id'] = other['id']
        for bot in (self.bot, other):
            metadata = self.profiles.sync(bot)
            self.assertEqual(metadata['profile_id'], bot['id'])
            self.assertEqual(metadata['session_id'], bot['session_id'])
            self.assertTrue((self.profiles.home(bot) / 'config.yaml').is_file())
            self.assertIn(bot['instructions'], (self.profiles.home(bot) / 'SOUL.md').read_text())
        self.assertNotEqual(self.profiles.home(self.bot), self.profiles.home(other))
        self.assertEqual(self.profiles.home(self.bot), self.root / 'bots' / self.bot['id'] / 'home')

    def test_migration_preserves_existing_state_and_prior_soul(self):
        home = self.profiles.home(self.bot)
        home.mkdir(parents=True)
        (home / 'SOUL.md').write_text('Prior Hermes instructions')
        (home / 'state.db').write_bytes(b'preserved state')
        (home / 'config.yaml').write_text('model: existing-model\n')
        legacy = {key: value for key, value in self.bot.items() if key != 'profile_id'}
        self.profiles.sync(legacy)
        self.assertEqual((home / 'state.db').read_bytes(), b'preserved state')
        self.assertEqual((home / 'config.yaml').read_text(), 'model: existing-model\n')
        self.assertEqual((home / 'SOUL.before-coconut.md').read_text(), 'Prior Hermes instructions')
        self.profiles.sync(self.bot | {'name': 'New name', 'instructions': 'Use two sentences.'})
        self.assertEqual((home / 'SOUL.before-coconut.md').read_text(), 'Prior Hermes instructions')
        self.assertEqual(self.profiles.read_instructions(self.bot), 'Prior Hermes instructions')
        self.profiles.write_instructions(self.bot, 'Use two sentences.')
        self.assertEqual((home / 'SOUL.md').read_text(), 'Use two sentences.')
        self.profiles.write_instructions(self.bot, '')
        self.profiles.sync(self.bot)
        self.assertEqual(self.profiles.read_instructions(self.bot), '')

    def test_new_profiles_opt_out_of_the_bundled_catalog(self):
        self.profiles.sync(self.bot)
        self.assertTrue((self.profiles.home(self.bot) / '.no-bundled-skills').is_file())

    def test_existing_profile_gets_default_once_and_keeps_custom_skills(self):
        self.profiles.sync(self.bot)
        home = self.profiles.home(self.bot)
        marker = home / '.no-bundled-skills'
        marker.unlink()
        metadata_path = home / 'coconut-profile.json'
        metadata = json.loads(metadata_path.read_text())
        metadata.pop('skill_defaults_applied')
        metadata_path.write_text(json.dumps(metadata))
        skill = home / 'skills' / 'custom' / 'SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('Keep this custom skill.')

        self.profiles.sync(self.bot)
        self.assertTrue(marker.is_file())
        self.assertEqual(skill.read_text(), 'Keep this custom skill.')

        marker.unlink()  # Native Hermes opt-in removes this marker.
        self.profiles.sync(self.bot)
        self.assertFalse(marker.exists())

    def test_archive_preserves_identity_and_delete_prevents_resurrection(self):
        self.profiles.sync(self.bot)
        home = self.profiles.home(self.bot)
        self.assertEqual(self.profiles.sync(self.bot | {'archived': True})['status'], 'archived')
        self.assertEqual(self.profiles.sync(self.bot)['status'], 'active')
        before = (home / 'SOUL.md').read_bytes()
        self.assertEqual(self.profiles.sync(self.bot | {'_deleted': True})['status'], 'deleted')
        self.assertEqual((home / 'SOUL.md').read_bytes(), before)
        with self.assertRaisesRegex(ProfileError, 'deleted bot'):
            self.profiles.sync(self.bot)

    def test_deleting_unused_bot_does_not_create_a_profile(self):
        self.assertEqual(self.profiles.sync(self.bot | {'_deleted': True})['status'], 'deleted')
        self.assertFalse(self.profiles.home(self.bot).exists())

    def test_conflicting_identity_or_session_cannot_take_over_profile(self):
        with self.assertRaises(ProfileError):
            profile_id(self.bot | {'profile_id': str(uuid4())})
        self.profiles.sync(self.bot)
        with self.assertRaisesRegex(ProfileError, 'another bot or session'):
            self.profiles.sync(self.bot | {'session_id': str(uuid4())})

    def test_profile_and_managed_file_symlinks_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'bots').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ProfileError, 'symbolic link'):
            self.profiles.sync(self.bot)
        (self.root / 'bots').unlink()
        self.profiles.sync(self.bot)
        soul = self.profiles.home(self.bot) / 'SOUL.md'
        soul.unlink()
        target = outside / 'untouched'
        target.write_text('Keep this file.')
        soul.symlink_to(target)
        with self.assertRaisesRegex(ProfileError, 'symbolic link'):
            self.profiles.sync(self.bot)
        self.assertEqual(target.read_text(), 'Keep this file.')




    def test_inspection_uses_database_identity_and_omits_private_content(self):
        self.profiles.sync(self.bot)
        with sqlite3.connect(self.root / 'coconut.sqlite3') as connection:
            connection.execute('CREATE TABLE bots (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            connection.execute('INSERT INTO bots VALUES (?, ?)', (self.bot['id'], json.dumps(self.bot)))
        result = inspect_profiles(self.root)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['identity_recorded'])
        self.assertEqual(result[0]['home'], str(self.profiles.home(self.bot)))
        self.assertNotIn(self.bot['instructions'], json.dumps(result))
        self.assertNotIn(self.bot['memory'], json.dumps(result))
        deleted = self.bot | {'_deleted': True}
        self.profiles.sync(deleted)
        with sqlite3.connect(self.root / 'coconut.sqlite3') as connection:
            connection.execute('UPDATE bots SET data=?', (json.dumps(deleted),))
        self.assertEqual(inspect_profiles(self.root), [])
        self.assertEqual(inspect_profiles(self.root, self.bot['id'], include_deleted=True)[0]['status'], 'deleted')


if __name__ == '__main__':
    unittest.main()
