"""A bot follows the default model unless the person gives it its own model.

Hermes profiles do not inherit from the default profile, so Coconut writes the default model into the profile of each
bot that follows it. test_model_follow_native.py checks that write with the real config writer of Hermes.
"""
from copy import deepcopy
import logging
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut.providers import HermesProvider
from coconut.service import Service, ServiceError
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
        self.writes.append(('bot', bot['id'], deepcopy(selection)))
        if selection is None:
            result = self.follow_default_model([bot['id']])[bot['id']]
            if 'error' in result:
                raise ValueError(result['error'])
        else:
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
        self.service = None
        self.open()

    def tearDown(self):
        self.service.close()

    @staticmethod
    def create_profile(python, source, hermes_home, operation, **payload):
        """Stands in for hermes_cli.profiles.create_profile."""
        home = Path(hermes_home) / 'profiles' / payload['name']
        home.mkdir()
        (home / 'SOUL.md').write_text('')
        return {'home': str(home)}

    def open(self):
        if self.service is not None:
            self.service.close()
        self.service = Service(self.root, self.provider, start_workers=False, hermes_home=self.hermes)
        self.provider.service = self.service

    def write(self, method, path, data, key=None):
        return self.service.mutate(method, path, data, key or str(uuid4()), 'test-phone')

    def bot(self, name):
        bot = self.write('POST', '/v1/bots', {'name': name})
        self.service.model_follow_thread.join(5)
        return bot

    def stored(self, bot_id):
        return self.service._get('bots', bot_id)

    def by_profile(self, name):
        return next(bot_id for bot_id, row in self.service.profiles.rows.items() if row['name'] == name)

    def follow(self):
        with self.service.model_settings_lock:
            return self.service._follow_default_model()

    def selection(self):
        return dict(model='model-b', provider='native-b', reasoning_effort=None)

    def test_a_new_bot_follows_the_default_and_its_profile_gets_the_default_model(self):
        bot = self.bot('Ada')
        self.assertTrue(self.stored(bot['id'])['model_follows_default'])
        self.assertIn(([bot['id']], []), self.provider.calls)
        self.assertEqual(self.provider.profiles[bot['id']], self.provider.route())
        self.assertIsNone(self.service.snapshot()['bots'][-1]['model_override'])

    def test_a_default_change_reaches_the_following_bots_only(self):
        following, own = self.bot('Ada'), self.bot('Lin')
        self.write('PATCH', '/v1/bots/' + own['id'], {'model_override': self.selection()})
        self.assertFalse(self.stored(own['id'])['model_follows_default'])
        response = self.write('PUT', '/v1/settings/model', dict(model='model-a', provider='native-a', reasoning_effort='high'))
        self.assertNotIn('model_follow_errors', response)
        self.assertEqual(self.provider.profiles[following['id']],
                         dict(model='model-a', provider='native-a', reasoning_effort='high'))
        self.assertEqual(self.provider.profiles[own['id']], self.selection())
        self.assertNotIn(own['id'], self.provider.calls[-1][0])

    def test_null_returns_a_bot_to_the_default_model(self):
        bot = self.bot('Ada')
        self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': self.selection()})
        self.assertEqual(self.provider.profiles[bot['id']], self.selection())
        response = self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': None})
        self.assertIsNone(response['model_override'])
        self.assertEqual(response['effective_model']['model'], 'model-a')
        self.assertTrue(self.stored(bot['id'])['model_follows_default'])
        self.assertEqual(self.provider.profiles[bot['id']], self.provider.route())

    def test_a_start_classifies_earlier_bots_and_found_profiles_by_their_model(self):
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
        with patch.object(self.service, '_set_hermes_entries', return_value=True) as entries:
            self.service._reconcile_hermes_profiles(sys.executable, '/hermes/source')
        entries.assert_called_once_with(sys.executable, '/hermes/source')
        self.assertEqual(sorted(self.provider.calls[-1][1]), sorted([same['id'], other['id'], outside, empty, default]))
        for bot_id, follows in ((same['id'], True), (other['id'], False), (outside, False), (empty, True), (default, True)):
            with self.subTest(bot=bot_id):
                self.assertEqual(self.stored(bot_id)['model_follows_default'], follows)
        # The bot of its own model keeps it. A following bot gets the whole default, effort too.
        self.assertEqual(self.provider.profiles[other['id']], self.selection())
        self.assertEqual(self.provider.profiles[same['id']], self.provider.route())
        # A later start changes only the following bots and asks for no choice again.
        self.provider.default = dict(self.provider.default, model='model-b', provider='native-b')
        self.follow()
        self.assertEqual(self.provider.calls[-1], (sorted([same['id'], empty, default]), []))
        self.assertEqual(self.provider.profiles[outside], self.selection())
        self.assertEqual(self.provider.profiles[empty]['model'], 'model-b')

    def test_a_choice_of_the_person_stays_when_a_classification_ends_later(self):
        bot = self.stored(self.bot('Ada')['id'])
        bot.pop('model_follows_default')
        self.service._apply_follow_result(bot, {'follows': False, 'changed': False})
        self.assertFalse(bot['model_follows_default'])
        self.service._apply_follow_result(bot, {'follows': True, 'changed': True})
        self.assertFalse(bot['model_follows_default'])

    def test_a_failed_write_is_logged_kept_on_the_bot_and_cleared_by_a_later_success(self):
        bot = self.bot('Ada')
        self.provider.failing.add(bot['id'])
        with self.assertLogs('coconut.bot_profiles', logging.WARNING) as logs:
            response = self.write('PUT', '/v1/settings/model', self.selection())
        self.assertIn('Ada', logs.output[0])
        self.assertEqual([failure['bot_id'] for failure in response['model_follow_errors']], [bot['id']])
        self.assertIn('OSError', self.service.snapshot()['bots'][-1]['model_follow_error'])
        # The bot still follows the default, and the next start tries again.
        self.assertTrue(self.stored(bot['id'])['model_follows_default'])
        self.provider.failing.clear()
        self.assertEqual(self.follow(), [])
        self.assertNotIn('model_follow_error', self.stored(bot['id']))
        self.assertEqual(self.provider.profiles[bot['id']]['model'], 'model-b')

    def test_a_null_that_hermes_cannot_write_fails_and_keeps_the_own_model(self):
        bot = self.bot('Ada')
        self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': self.selection()})
        self.provider.failing.add(bot['id'])
        key = str(uuid4())
        with self.assertRaises(ServiceError):
            self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': None}, key)
        # A retry checks the profile itself. The profile still has its own model, so the change is not confirmed.
        with self.assertRaises(ServiceError) as raised:
            self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': None}, key)
        self.assertEqual(raised.exception.status, 409)
        self.assertFalse(self.stored(bot['id'])['model_follows_default'])
        self.assertEqual(self.provider.profiles[bot['id']], self.selection())

    def test_a_retried_null_confirms_the_change_when_the_profile_gets_the_default(self):
        bot = self.bot('Ada')
        self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': self.selection()})
        self.provider.failing.add(bot['id'])
        key = str(uuid4())
        with self.assertRaises(ServiceError):
            self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': None}, key)
        self.provider.failing.clear()
        self.assertIsNone(self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': None}, key)['model_override'])
        self.assertTrue(self.stored(bot['id'])['model_follows_default'])
        self.assertEqual(self.provider.profiles[bot['id']], self.provider.route())

    def test_a_recovered_default_change_reaches_the_following_bots(self):
        bot = self.bot('Ada')
        saved = self.provider.set_model_default

        def lost_answer(selection):
            saved(selection)
            raise RuntimeError('The answer of Hermes was lost.')
        self.provider.set_model_default = lost_answer
        key = str(uuid4())
        with self.assertRaises(ServiceError):
            self.write('PUT', '/v1/settings/model', self.selection(), key)
        self.provider.set_model_default = saved
        self.assertEqual(self.write('PUT', '/v1/settings/model', self.selection(), key)['model'], 'model-b')
        self.assertEqual(self.provider.profiles[bot['id']]['model'], 'model-b')

    def test_a_retry_of_a_default_change_returns_the_same_failures(self):
        bot = self.bot('Ada')
        self.provider.failing.add(bot['id'])
        key = str(uuid4())
        with self.assertLogs('coconut.bot_profiles', logging.WARNING):
            first = self.write('PUT', '/v1/settings/model', self.selection(), key)
        self.assertEqual(self.write('PUT', '/v1/settings/model', self.selection(), key)['model_follow_errors'],
                         first['model_follow_errors'])

    def test_a_bot_without_a_choice_keeps_the_failure_and_gets_its_choice_later(self):
        bot = self.bot('Ada')
        with self.service.transaction():
            stored = self.stored(bot['id'])
            stored.pop('model_follows_default')
            self.service._put('bots', stored)
        self.provider.failing.add(bot['id'])
        with self.assertLogs('coconut.bot_profiles', logging.WARNING):
            self.assertEqual([failure['bot_id'] for failure in self.follow()], [bot['id']])
        self.assertNotIn('model_follows_default', self.stored(bot['id']))
        self.assertIn('OSError', self.stored(bot['id'])['model_follow_error'])
        self.provider.failing.clear()
        self.follow()
        self.assertTrue(self.stored(bot['id'])['model_follows_default'])
        self.assertNotIn('model_follow_error', self.stored(bot['id']))

    def test_a_model_for_the_default_profile_bot_reaches_the_following_bots(self):
        self.follow()
        following, default = self.bot('Ada')['id'], self.by_profile('default')

        def write_default(bot, selection):
            # The profile of this bot is the Hermes home, so its model is the default model.
            self.provider.writes.append(('bot', bot['id'], deepcopy(selection)))
            self.provider.default = dict(self.provider.default, **selection)
        self.provider.set_bot_model = write_default
        self.write('PATCH', '/v1/bots/' + default, {'model_override': self.selection()})
        self.assertEqual(self.provider.profiles[following], self.selection())


    # Coconut never writes into the profile of a deleted or archived bot, or into a removed or missing profile.

    def targeted(self):
        return {bot_id for bot_ids, _ in self.provider.calls for bot_id in bot_ids}

    def test_a_bot_removed_after_a_failed_reset_gets_no_write_on_the_retry_or_later(self):
        bot = self.bot('Ada')
        self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': self.selection()})
        self.provider.failing.add(bot['id'])
        key = str(uuid4())
        with self.assertRaises(ServiceError):
            self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': None}, key)
        self.write('DELETE', '/v1/bots/' + bot['id'], {})
        self.assertEqual(self.service.profiles.rows[bot['id']]['state'], 'removed')
        self.provider.failing.clear()
        self.provider.calls.clear()
        with self.assertRaises(ServiceError) as raised:
            self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': None}, key)
        self.assertEqual(raised.exception.status, 409)
        self.write('PUT', '/v1/settings/model', dict(model='model-a', provider='native-a', reasoning_effort='high'))
        self.follow()
        self.assertNotIn(bot['id'], self.targeted())
        self.assertEqual(self.provider.profiles[bot['id']], self.selection())

    def test_a_start_skips_a_removed_profile_and_a_missing_one(self):
        removed, missing, kept = self.bot('Ada'), self.bot('Lin'), self.bot('Kai')
        with self.service.transaction():
            row = self.service.profiles.rows[removed['id']]
            self.service._write_profile_row(dict(row, state='removed'))
        missing_home = Path(self.service.profiles.rows[missing['id']]['home'])
        for path in missing_home.iterdir():
            path.unlink()
        missing_home.rmdir()
        self.service.rescan_profiles()
        self.assertEqual(self.service.profiles.rows[missing['id']]['state'], 'missing')
        self.provider.calls.clear()
        self.provider.default = dict(self.provider.default, model='model-b', provider='native-b')
        with patch.object(self.service, '_set_hermes_entries', return_value=True):
            self.service._reconcile_hermes_profiles(sys.executable, '/hermes/source')
        self.assertIn(kept['id'], self.targeted())
        self.assertFalse({removed['id'], missing['id']} & self.targeted())
        self.assertEqual(self.provider.profiles[kept['id']]['model'], 'model-b')
        self.assertEqual(self.provider.profiles[removed['id']]['model'], 'model-a')

    def test_an_archived_bot_gets_the_default_model_when_the_person_restores_it(self):
        bot = self.bot('Ada')
        self.write('PATCH', '/v1/bots/' + bot['id'], {'archived': True})
        self.provider.calls.clear()
        self.write('PUT', '/v1/settings/model', self.selection())
        self.assertNotIn(bot['id'], self.targeted())
        self.assertEqual(self.provider.profiles[bot['id']]['model'], 'model-a')
        self.write('PATCH', '/v1/bots/' + bot['id'], {'archived': False})
        self.assertEqual(self.provider.profiles[bot['id']], self.selection())

    def test_the_settings_process_never_starts_for_a_profile_that_is_not_active(self):
        from coconut.model_catalog import HermesModelCatalog
        import threading
        catalog = object.__new__(HermesModelCatalog)
        catalog._lock, catalog._profile_cache = threading.RLock(), {}
        catalog.profile_rows = {'a': {'state': 'removed'}, 'b': {'state': 'missing'}}
        with patch.object(catalog, '_call', side_effect=AssertionError('No write may start.')):
            result = catalog.follow_default(['a', 'b', 'c'])
        self.assertEqual(set(result), {'a', 'b', 'c'})
        self.assertTrue(all('skipped' in value for value in result.values()))


class ObservedModelTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
