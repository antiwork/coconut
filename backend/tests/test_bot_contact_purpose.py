"""Native routing metadata stays separate from private bot instructions."""
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch
from uuid import uuid4
from coconut.hermes_worker import bot_directory, BOT_ROLE_INSTRUCTIONS
from coconut import profiles


def layout(root):
    """The worker reads each contact's profile folder from the profiles table of COCONUT_DATA_DIR."""
    import contextlib
    import os
    stack = contextlib.ExitStack()
    stack.enter_context(patch.dict(os.environ, {'COCONUT_DATA_DIR': str(root)}))
    stack.enter_context(patch('coconut.profile_registry.lookup', lambda data, bot_id: dict(
        bot_id=bot_id, name=bot_id, home=str(Path(root) / 'bots' / bot_id / 'home'), state='active')))
    return stack


class BotContactPurposeTests(unittest.TestCase):
    def test_native_description_replaces_stale_label_without_reading_private_files(self):
        with tempfile.TemporaryDirectory() as root:
            current, peer = str(uuid4()), str(uuid4())
            home = Path(root) / 'bots' / current / 'home'
            peer_home = Path(root) / 'bots' / peer / 'home'
            peer_home.mkdir(parents=True)
            (peer_home / 'profile.yaml').write_text(json.dumps({'description': 'Review Coconut code and maintain Hermes compatibility.'}))
            (peer_home / 'SOUL.md').write_text('A private instruction and a secret.')
            native = ModuleType('hermes_cli.profiles')
            native.read_profile_meta = lambda path: json.loads((path / 'profile.yaml').read_text())
            request = {'bot': {'id': current}, 'task': {'_available_bots': [{'id': peer, 'name': 'Dev', 'role': 'Stale label'}]}}
            with patch.dict(sys.modules, {'hermes_cli.profiles': native, 'profiles': profiles}), layout(root):
                result = bot_directory(request, home)
                self.assertEqual(result, [{'id': peer, 'name': 'Dev', 'role': 'Review Coconut code and maintain Hermes compatibility.'}])
                (peer_home / 'profile.yaml').write_text(json.dumps({'description': 'Research product needs.'}))
                self.assertEqual(bot_directory(request, home)[0]['role'], 'Research product needs.')
            self.assertNotIn('private', str(result))
            self.assertNotIn('secret', str(result))

    def test_unreadable_contact_does_not_stop_later_descriptions(self):
        with tempfile.TemporaryDirectory() as root:
            current, damaged, healthy = str(uuid4()), str(uuid4()), str(uuid4())
            home = Path(root) / 'bots' / current / 'home'
            native = ModuleType('hermes_cli.profiles')
            def read_meta(path):
                if path.parent.name == damaged:
                    raise OSError('Unreadable profile')
                return {'description': 'Review code.'}
            native.read_profile_meta = read_meta
            request = {'bot': {'id': current}, 'task': {'_available_bots': [
                {'id': damaged, 'name': 'Damaged', 'role': 'Old role'},
                {'id': healthy, 'name': 'Dev', 'role': ''}]}}
            with patch.dict(sys.modules, {'hermes_cli.profiles': native}), layout(root):
                result = bot_directory(request, home)
            self.assertEqual(result[0]['role'], 'Old role')
            self.assertEqual(result[1]['role'], 'Review code.')

    def test_missing_native_metadata_keeps_existing_contacts(self):
        current, peer = str(uuid4()), str(uuid4())
        request = {'bot': {'id': current}, 'task': {'_available_bots': [
            {'id': current, 'name': 'Desk', 'role': ''}, {'id': peer, 'name': 'Dev', 'role': 'Development'}]}}
        self.assertEqual(bot_directory(request), [{'id': peer, 'name': 'Dev', 'role': 'Development'}])

    def test_role_maintenance_uses_native_metadata_and_does_not_ask_the_user(self):
        self.assertIn('hermes_cli.profiles.write_profile_meta', BOT_ROLE_INSTRUCTIONS)
        self.assertIn('description_auto=True', BOT_ROLE_INSTRUCTIONS)
        self.assertIn('Do not ask the user', BOT_ROLE_INSTRUCTIONS)
        self.assertIn('Exclude private instructions', BOT_ROLE_INSTRUCTIONS)
