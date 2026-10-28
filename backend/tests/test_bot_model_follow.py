"""A bot follows the default model unless the person gives it its own model.

A request only records the choice in `model_follows_default`. One reconcile step (BotProfiles.reconcile_model_follow)
writes the default model into the profile of each active bot that follows it. test_model_follow_native.py checks that
write with the real config writer of Hermes.
"""
from copy import deepcopy
import logging
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut import bot_profiles
from coconut.model_catalog import HermesModelCatalog
from coconut.providers import HermesProvider
from coconut.service import Service
from test_bot_model_settings import NativeProvider


class FollowingProvider(NativeProvider):
    """Keeps the model of each profile, and follows the default as model_catalog._follow_default does."""
    python = sys.executable
    hermes_root = '/hermes/source'

    def __init__(self, root):
        super().__init__(root)
        self.profiles = {}
        self.calls = []
        self.failing = set()

    def route(self):
        return {key: self.default[key] for key in ('model', 'provider', 'reasoning_effort')}

    def follow_default_model(self, bot_ids, classify=()):
        self.outside_database_lock()
        self.calls.append((sorted(bot_ids), sorted(classify)))
        result = {}
        for bot_id in bot_ids:
            if bot_id in self.failing:
                result[bot_id] = {'error': 'Hermes could not set the default model in this profile (OSError).'}
                continue
            entry, own = {}, self.profiles.get(bot_id)
            if bot_id in classify:
                entry['follows'] = own is None or (own['model'], own['provider']) == (self.default['model'], self.default['provider'])
            if entry.get('follows', True):
                entry['changed'] = own != self.route()
                self.profiles[bot_id] = self.route()
            result[bot_id] = entry
        return result

    def set_bot_model(self, bot, selection):
        self.outside_database_lock()
        assert selection is not None, 'A request writes only the own model of a bot.'
        self.writes.append(('bot', bot['id'], deepcopy(selection)))
        self.profiles[bot['id']] = deepcopy(selection)

    def observe_bot(self, bot):
        observed = super().observe_bot(bot)
        observed['model_override'] = None if bot.get('model_follows_default') else deepcopy(self.profiles.get(bot['id']))
        return observed


class BotModelFollowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root, self.hermes = base / 'data', base / 'hermes'
        (self.hermes / 'profiles').mkdir(parents=True)
        (self.hermes / 'config.yaml').write_text('model: {}\n')
        self.provider = FollowingProvider(self.root)
        patcher = patch('coconut.bot_profiles.run_profile_command', side_effect=self.create_profile)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = Service(self.root, self.provider, start_workers=False, hermes_home=self.hermes)
        self.provider.service = self.service
        self.addCleanup(self.service.close)

    @staticmethod
    def create_profile(python, source, hermes_home, operation, **payload):
        """Stands in for hermes_cli.profiles.create_profile."""
        home = Path(hermes_home) / 'profiles' / payload['name']
        home.mkdir()
        (home / 'SOUL.md').write_text('')
        return {'home': str(home)}

    def write(self, method, path, data, key=None):
        return self.service.mutate(method, path, data, key or str(uuid4()), 'test-phone')

    def bot(self, name):
        return self.write('POST', '/v1/bots', {'name': name})

    def stored(self, bot_id):
        return self.service._get('bots', bot_id)

    def by_profile(self, name):
        return next(bot_id for bot_id, row in self.service.profiles.rows.items() if row['name'] == name)

    def woke(self):
        """Whether a request asked for a reconcile since the last check."""
        woke = self.service.model_follow_wake.is_set()
        self.service.model_follow_wake.clear()
        return woke

    def reconcile(self):
        self.provider.calls.clear()
        return self.service.reconcile_model_follow()

    def targeted(self):
        return {bot_id for bot_ids, _ in self.provider.calls for bot_id in bot_ids}

    def selection(self):
        return dict(model='model-b', provider='native-b', reasoning_effort=None)

    def test_a_new_bot_records_that_it_follows_and_the_reconcile_writes_its_profile(self):
        self.woke()
        bot = self.bot('Ada')
        self.assertTrue(self.stored(bot['id'])['model_follows_default'])
        self.assertTrue(self.woke())
        # The request writes no profile.
        self.assertNotIn(bot['id'], self.provider.profiles)
        self.assertFalse(self.reconcile())
        self.assertEqual(self.provider.profiles[bot['id']], self.provider.route())
        self.assertIsNone(self.service.snapshot()['bots'][-1]['model_override'])

    def test_a_default_change_records_the_default_and_the_reconcile_reaches_only_following_bots(self):
        following, own = self.bot('Ada'), self.bot('Lin')
        self.write('PATCH', '/v1/bots/' + own['id'], {'model_override': self.selection()})
        self.assertFalse(self.stored(own['id'])['model_follows_default'])
        self.reconcile()
        self.woke()
        response = self.write('PUT', '/v1/settings/model', dict(model='model-a', provider='native-a', reasoning_effort='high'))
        self.assertNotIn('model_follow_errors', response)
        self.assertTrue(self.woke())
        self.reconcile()
        self.assertEqual(self.provider.profiles[following['id']], dict(model='model-a', provider='native-a', reasoning_effort='high'))
        self.assertEqual(self.provider.profiles[own['id']], self.selection())
        self.assertNotIn(own['id'], self.targeted())

    def test_null_records_the_choice_and_a_retry_needs_no_recovery(self):
        bot = self.bot('Ada')
        self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': self.selection()})
        writes = len(self.provider.writes)
        key = str(uuid4())
        response = self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': None}, key)
        self.assertIsNone(response['model_override'])
        self.assertTrue(self.stored(bot['id'])['model_follows_default'])
        self.assertEqual(self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': None}, key), response)
        # null writes no profile. The reconcile does.
        self.assertEqual(len(self.provider.writes), writes)
        self.assertEqual(self.provider.profiles[bot['id']], self.selection())
        self.reconcile()
        self.assertEqual(self.provider.profiles[bot['id']], self.provider.route())

    def test_a_failed_write_is_recorded_shown_and_retried_by_the_next_reconcile(self):
        bot = self.bot('Ada')
        self.provider.failing.add(bot['id'])
        with self.assertLogs('coconut.bot_profiles', logging.WARNING) as logs:
            self.assertTrue(self.reconcile())
        self.assertIn('Ada', logs.output[0])
        self.assertIn('OSError', self.service.snapshot()['bots'][-1]['model_follow_error'])
        self.assertTrue(self.stored(bot['id'])['model_follows_default'])
        self.provider.failing.clear()
        self.assertFalse(self.reconcile())
        self.assertNotIn('model_follow_error', self.stored(bot['id']))
        self.assertEqual(self.provider.profiles[bot['id']], self.provider.route())

    def test_the_worker_retries_a_failed_write_without_a_wake(self):
        bot = self.bot('Ada')
        self.provider.failing.add(bot['id'])
        with self.assertLogs('coconut.bot_profiles', logging.WARNING):
            self.reconcile()
        self.woke()
        self.provider.failing.clear()
        with patch.object(bot_profiles, 'SCAN_SECONDS', 0.01), patch.object(bot_profiles, 'FOLLOW_RETRY_SECONDS', 0):
            worker = threading.Thread(target=self.service._model_follow_loop, daemon=True)
            worker.start()
            deadline = time.monotonic() + 5
            while 'model_follow_error' in self.stored(bot['id']) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.service.stopping.set()
            worker.join(5)
        self.assertNotIn('model_follow_error', self.stored(bot['id']))

    def test_the_reconcile_skips_deleted_archived_removed_and_missing_profiles(self):
        deleted, archived, removed, missing, active = (self.bot(name) for name in ('Ada', 'Lin', 'Kai', 'Mo', 'Sam'))
        self.reconcile()
        self.write('DELETE', '/v1/bots/' + deleted['id'], {})
        self.assertEqual(self.service.profiles.rows[deleted['id']]['state'], 'removed')
        self.write('PATCH', '/v1/bots/' + archived['id'], {'archived': True})
        with self.service.transaction():
            self.service._write_profile_row(dict(self.service.profiles.rows[removed['id']], state='removed'))
        home = Path(self.service.profiles.rows[missing['id']]['home'])
        (home / 'SOUL.md').unlink()
        home.rmdir()
        self.service.rescan_profiles()
        self.assertEqual(self.service.profiles.rows[missing['id']]['state'], 'missing')
        self.write('PUT', '/v1/settings/model', self.selection())
        self.reconcile()
        self.assertEqual(self.targeted() & {deleted['id'], archived['id'], removed['id'], missing['id']}, set())
        self.assertEqual(self.provider.profiles[active['id']], self.provider.route())
        for bot in (deleted, archived, removed, missing):
            self.assertEqual(self.provider.profiles[bot['id']]['model'], 'model-a')

    def test_a_restored_bot_gets_the_current_default(self):
        bot = self.bot('Ada')
        self.reconcile()
        self.write('PATCH', '/v1/bots/' + bot['id'], {'archived': True})
        self.write('PUT', '/v1/settings/model', self.selection())
        self.reconcile()
        self.assertEqual(self.provider.profiles[bot['id']]['model'], 'model-a')
        self.woke()
        self.write('PATCH', '/v1/bots/' + bot['id'], {'archived': False, 'model_override': None})
        self.assertTrue(self.woke())
        self.reconcile()
        self.assertEqual(self.provider.profiles[bot['id']], self.selection())

    def test_a_default_set_outside_the_app_reaches_the_bots_at_the_next_start(self):
        bot = self.bot('Ada')
        self.reconcile()
        # `hermes config set model.default model-b` on the default profile.
        self.provider.default = dict(self.provider.default, model='model-b', provider='native-b')
        with patch.object(self.service, '_set_hermes_entries', return_value=True) as entries:
            self.service._reconcile_hermes_profiles(sys.executable, '/hermes/source')
        entries.assert_called_once_with(sys.executable, '/hermes/source')
        self.assertEqual(self.provider.profiles[bot['id']]['model'], 'model-b')

    def test_earlier_bots_and_found_profiles_get_their_choice_by_their_model(self):
        same, other = self.bot('Ada'), self.bot('Lin')
        # Bots of an earlier install have no choice yet.
        with self.service.transaction():
            for bot_id in (same['id'], other['id']):
                bot = self.stored(bot_id)
                bot.pop('model_follows_default')
                self.service._put('bots', bot)
        self.provider.profiles[same['id']] = dict(model='model-a', provider='native-a', reasoning_effort=None)
        self.provider.profiles[other['id']] = self.selection()
        # A profile that the person made outside Coconut, with its own model, and one without a model.
        for name in ('outside', 'empty'):
            (self.hermes / 'profiles' / name).mkdir()
            (self.hermes / 'profiles' / name / 'SOUL.md').write_text('')
        self.service.rescan_profiles()
        outside, empty, default = self.by_profile('outside'), self.by_profile('empty'), self.by_profile('default')
        self.provider.profiles[outside] = self.selection()
        self.assertNotIn('model_follows_default', self.stored(outside))
        self.reconcile()
        self.assertEqual(self.provider.calls[-1][1], sorted([same['id'], other['id'], outside, empty, default]))
        for bot_id, follows in ((same['id'], True), (other['id'], False), (outside, False), (empty, True), (default, True)):
            with self.subTest(bot=bot_id):
                self.assertEqual(self.stored(bot_id)['model_follows_default'], follows)
        self.assertEqual(self.provider.profiles[other['id']], self.selection())
        self.assertEqual(self.provider.profiles[same['id']], self.provider.route())
        # A later reconcile changes only the following bots and asks for no choice again.
        self.provider.default = dict(self.provider.default, model='model-b', provider='native-b')
        self.reconcile()
        self.assertEqual(self.provider.calls[-1], (sorted([same['id'], empty, default]), []))
        self.assertEqual(self.provider.profiles[outside], self.selection())

    def test_a_failed_classification_leaves_no_choice_and_records_the_error(self):
        bot = self.bot('Ada')
        with self.service.transaction():
            stored = self.stored(bot['id'])
            stored.pop('model_follows_default')
            self.service._put('bots', stored)
        self.provider.failing.add(bot['id'])
        with self.assertLogs('coconut.bot_profiles', logging.WARNING):
            self.assertTrue(self.reconcile())
        self.assertNotIn('model_follows_default', self.stored(bot['id']))
        self.assertIn('OSError', self.stored(bot['id'])['model_follow_error'])
        self.provider.failing.clear()
        self.reconcile()
        self.assertTrue(self.stored(bot['id'])['model_follows_default'])
        self.assertNotIn('model_follow_error', self.stored(bot['id']))

    def test_an_own_model_for_the_default_profile_bot_reaches_the_following_bots(self):
        self.reconcile()
        following, default = self.bot('Ada')['id'], self.by_profile('default')

        def write_default(bot, selection):
            # The profile of this bot is the Hermes home, so its model is the default model.
            self.provider.default = dict(self.provider.default, **selection)
        self.provider.set_bot_model = write_default
        self.woke()
        self.write('PATCH', '/v1/bots/' + default, {'model_override': self.selection()})
        self.assertTrue(self.woke())
        self.reconcile()
        self.assertEqual(self.provider.profiles[following], self.selection())


class GuardTests(unittest.TestCase):
    def test_a_following_bot_shows_no_own_model_and_a_bot_with_its_own_model_shows_it(self):
        provider = object.__new__(HermesProvider)
        own = dict(model='model-b', provider='native-b', reasoning_effort=None)
        provider.catalog = Mock()
        provider.catalog.read_profiles.return_value = {
            'a': {'model_override': own, 'effective_model': dict(own, base_url=None)},
            'b': {'model_override': own, 'effective_model': dict(own, base_url=None)}}
        observed = provider.observe_bots([{'id': 'a', 'model_follows_default': True},
                                          {'id': 'b', 'model_follows_default': False}], settings={})
        self.assertIsNone(observed['a']['model_override'])
        self.assertNotIn('_native_effective_model', observed['a'])
        self.assertEqual(observed['b']['model_override'], own)
        self.assertEqual(observed['b']['_native_effective_model'], dict(own, base_url=None))

    def test_the_settings_process_never_starts_for_a_profile_that_is_not_active(self):
        catalog = object.__new__(HermesModelCatalog)
        catalog._lock, catalog._profile_cache = threading.RLock(), {}
        catalog.profile_rows = {'a': {'state': 'removed'}, 'b': {'state': 'missing'}}
        with patch.object(catalog, '_call', side_effect=AssertionError('No write may start.')):
            result = catalog.follow_default(['a', 'b', 'c'])
        self.assertEqual(set(result), {'a', 'b', 'c'})
        self.assertTrue(all('skipped' in value for value in result.values()))


if __name__ == '__main__':
    unittest.main()
