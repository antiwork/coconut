import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from coconut.profiles import MAX_PROMPT_FILE_BYTES, HermesProfiles, ProfileError, inspect_profiles, profile_id


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        from profile_layout import LayoutRows
        self.profiles = HermesProfiles(self.root, LayoutRows(self.root))
        self.bot = dict(id=str(uuid4()), session_id=str(uuid4()), name='Ada', role='Reviewer',
                        instructions='Keep replies brief.', memory='Use metric units.', preferences=[], archived=False)
        self.bot['profile_id'] = self.bot['id']

    def tearDown(self):
        self.temp.cleanup()

    def create(self):
        """Lay out a profile as Hermes creates it, with the memories folder that Hermes adds at its first turn."""
        (self.profiles.home(self.bot) / 'SOUL.md').write_text(self.bot['instructions'])
        (self.profiles.home(self.bot) / 'memories').mkdir(exist_ok=True)

    def test_each_bot_uses_the_folder_of_its_profiles_row(self):
        other = self.bot | {'id': str(uuid4()), 'session_id': str(uuid4())}
        other['profile_id'] = other['id']
        for bot in (self.bot, other):
            metadata = self.profiles.sync(bot)
            self.assertEqual(metadata['profile_id'], bot['id'])
            self.assertEqual(metadata['session_id'], bot['session_id'])
            self.assertFalse(metadata['created'])
        self.assertNotEqual(self.profiles.home(self.bot), self.profiles.home(other))

    def test_a_bot_without_a_row_has_no_home(self):
        profiles = HermesProfiles(self.root)
        with self.assertRaisesRegex(ProfileError, 'has no Hermes profile'):
            profiles.home(self.bot)
        self.assertEqual(profiles.sync(self.bot | {'_deleted': True})['status'], 'deleted')
        self.assertFalse((self.root / 'bots').exists())

    def test_a_missing_profile_folder_stays_missing(self):
        home = self.profiles.home(self.bot)
        home.rmdir()
        self.assertEqual(self.profiles.sync(self.bot)['status'], 'missing')
        with self.assertRaisesRegex(ProfileError, 'is missing'):
            self.profiles.write_instructions(self.bot, 'New instructions.')
        self.assertFalse(home.exists())

    def test_an_existing_profile_stays_unchanged(self):
        home = self.profiles.home(self.bot)
        files = {'SOUL.md': 'Prior Hermes instructions', 'state.db': 'preserved state', 'config.yaml': 'model: existing-model\n'}
        for name, text in files.items():
            (home / name).write_text(text)
        for bot in (self.bot, self.bot | {'name': 'New name', 'instructions': 'Use two sentences.'}, self.bot | {'archived': True}):
            metadata = self.profiles.sync(bot)
            self.assertFalse(metadata['created'])
            self.assertEqual({path.name: path.read_text() for path in home.iterdir()}, files)
        self.assertEqual(self.profiles.read_instructions(self.bot), 'Prior Hermes instructions')
        self.profiles.write_instructions(self.bot, 'Use two sentences.')
        self.assertEqual((home / 'SOUL.md').read_text(), 'Use two sentences.')
        self.profiles.write_instructions(self.bot, '')
        self.profiles.sync(self.bot)
        self.assertEqual(self.profiles.read_instructions(self.bot), '')

    def test_prompt_revision_tracks_content_and_missing_files(self):
        self.create()
        home = self.profiles.home(self.bot)
        original = self.profiles.prompt_revision(self.bot)
        soul = home / 'SOUL.md'
        soul.touch()
        self.assertEqual(self.profiles.prompt_revision(self.bot), original)
        memories = home / 'memories'
        memories.mkdir(exist_ok=True)
        user = memories / 'USER.md'
        user.touch()
        empty = self.profiles.prompt_revision(self.bot)
        self.assertNotEqual(empty, original)
        user.write_text('Only issue #3664 requires manual merges.')
        self.assertNotEqual(self.profiles.prompt_revision(self.bot), empty)
        user.unlink()
        self.assertEqual(self.profiles.prompt_revision(self.bot), original)

    def test_prompt_revision_rejects_memory_symlinks(self):
        self.create()
        memories = self.profiles.home(self.bot) / 'memories'
        memories.mkdir(exist_ok=True)
        outside = self.root / 'outside-memory'
        outside.write_text('Private content outside this profile.')
        (memories / 'USER.md').symlink_to(outside)
        with self.assertRaisesRegex(ProfileError, 'symbolic link'):
            self.profiles.prompt_revision(self.bot)

    def test_prompt_revision_rejects_a_fifo_without_waiting_for_a_writer(self):
        self.create()
        home = self.profiles.home(self.bot)
        code = '''
import os, sys
from pathlib import Path
from unittest.mock import patch
from coconut.profiles import ProfileError, prompt_revision
home = Path(sys.argv[1])
path = home / 'memories/USER.md'
original = os.open
def replace(target, flags, *args, **kwargs):
    if Path(target) == path:
        path.unlink()
        os.mkfifo(path)
    return original(target, flags, *args, **kwargs)
if sys.argv[2] == 'replace':
    path.write_text('A valid policy before the open.')
else:
    os.mkfifo(path)
try:
    with patch('coconut.profiles.os.open', replace):
        prompt_revision(home)
except ProfileError as error:
    assert 'regular file' in str(error), error
    print('FIFO rejected without a writer.')
else:
    raise AssertionError('The profile accepted a FIFO.')
'''
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        for mode in ('existing', 'replace'):
            with self.subTest(mode=mode):
                try:
                    result = subprocess.run([sys.executable, '-c', code, str(home), mode],
                                            env=environment, capture_output=True, text=True, timeout=3)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('FIFO rejected without a writer.', result.stdout)
                finally:
                    (home / 'memories/USER.md').unlink(missing_ok=True)

    def test_prompt_revision_rejects_a_directory(self):
        self.create()
        path = self.profiles.home(self.bot) / 'memories/USER.md'
        path.mkdir()
        with self.assertRaisesRegex(ProfileError, 'regular file'):
            self.profiles.prompt_revision(self.bot)

    def test_prompt_revision_bounds_each_file_without_truncating_its_hash(self):
        self.create()
        home = self.profiles.home(self.bot)
        for name in ('SOUL.md', 'memories/MEMORY.md', 'memories/USER.md'):
            with self.subTest(name=name):
                path = home / name
                original = path.read_bytes() if path.exists() else None
                try:
                    path.write_bytes(b'a' * MAX_PROMPT_FILE_BYTES)
                    revision = self.profiles.prompt_revision(self.bot)
                    path.write_bytes(b'a' * (MAX_PROMPT_FILE_BYTES - 1) + b'b')
                    self.assertNotEqual(self.profiles.prompt_revision(self.bot), revision)
                    with path.open('ab') as handle:
                        handle.write(b'c')
                    with self.assertRaisesRegex(ProfileError, 'exceeds 1 MiB'):
                        self.profiles.prompt_revision(self.bot)
                finally:
                    if original is None:
                        path.unlink()
                    else:
                        path.write_bytes(original)

    def test_prompt_revision_rejects_invalid_utf8_in_each_file(self):
        self.create()
        home = self.profiles.home(self.bot)
        for name in ('SOUL.md', 'memories/MEMORY.md', 'memories/USER.md'):
            with self.subTest(name=name):
                path = home / name
                original = path.read_bytes() if path.exists() else None
                try:
                    path.write_bytes(b'A policy with invalid UTF-8: \xff')
                    with self.assertRaisesRegex(ProfileError, 'valid UTF-8'):
                        self.profiles.prompt_revision(self.bot)
                    path.write_text('保留此范围。')
                    self.assertTrue(self.profiles.prompt_revision(self.bot))
                finally:
                    if original is None:
                        path.unlink()
                    else:
                        path.write_bytes(original)

    def test_archive_and_delete_keep_the_profile(self):
        self.create()
        home = self.profiles.home(self.bot)
        before = {path.name: path.read_bytes() for path in home.iterdir() if path.is_file()}
        self.assertEqual(self.profiles.sync(self.bot | {'archived': True})['status'], 'archived')
        self.assertEqual(self.profiles.sync(self.bot)['status'], 'active')
        self.assertEqual(self.profiles.sync(self.bot | {'_deleted': True})['status'], 'deleted')
        self.assertEqual({path.name: path.read_bytes() for path in home.iterdir() if path.is_file()}, before)

    def test_conflicting_identity_cannot_take_over_profile(self):
        with self.assertRaises(ProfileError):
            profile_id(self.bot | {'profile_id': str(uuid4())})

    def test_profile_symlinks_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        home = self.profiles.home(self.bot)
        home.rmdir()
        home.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ProfileError, 'symbolic link'):
            self.profiles.sync(self.bot)
        self.assertEqual(list(outside.iterdir()), [])

    def test_inspection_uses_database_identity_and_omits_private_content(self):
        from coconut import profile_registry
        self.create()
        with sqlite3.connect(self.root / 'coconut.sqlite3') as connection:
            connection.execute('CREATE TABLE bots (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            connection.execute('INSERT INTO bots VALUES (?, ?)', (self.bot['id'], json.dumps(self.bot)))
            profile_registry.ensure_table(connection)
            profile_registry.write(connection, self.profiles.row(self.bot))
        result = inspect_profiles(self.root)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['home_exists'])
        self.assertEqual(result[0]['home'], str(self.profiles.home(self.bot)))
        self.assertNotIn(self.bot['instructions'], json.dumps(result))
        self.assertNotIn(self.bot['memory'], json.dumps(result))
        deleted = self.bot | {'_deleted': True}
        with sqlite3.connect(self.root / 'coconut.sqlite3') as connection:
            connection.execute('UPDATE bots SET data=?', (json.dumps(deleted),))
        self.assertEqual(inspect_profiles(self.root), [])
        self.assertEqual(inspect_profiles(self.root, self.bot['id'], include_deleted=True)[0]['status'], 'deleted')


if __name__ == '__main__':
    unittest.main()
