"""Check the fallback models and uncertain saves through the service."""
from copy import deepcopy
import json
import tempfile
import unittest
from uuid import uuid4

from coconut.native_fallback import kept_entries
from coconut.service import Service, ServiceError
from test_bot_model_settings import NativeProvider


class Provider(NativeProvider):
    def __init__(self, root):
        super().__init__(root)
        # The native chain. An entry can hold keys that never leave the Mac.
        self.chain = []
        self.lose_reply = False

    def state(self):
        return {'models': [{'model': entry['model'], 'provider': entry['provider']} for entry in self.chain]}

    def native_settings(self, operation='read', bots=(), **payload):
        self.outside_database_lock()
        if operation == 'routines_read':
            return {'routines': []}
        if operation == 'set_fallback':
            # The same mapping as the native save, with a plain route in place of the Hermes provider aliases.
            self.chain = kept_entries(self.chain, payload['models'], lambda entry: (entry['provider'].lower(), entry['model'].lower()))
            self.writes.append(deepcopy(payload['models']))
            if self.lose_reply:
                self.lose_reply = False
                raise OSError('Lost reply')
            return self.state()
        return dict(fallback=self.state(), approvals={'default': 'off', 'modes': []})


def model(name, provider):
    return {'model': name, 'provider': provider}


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.provider = Provider(self.folder.name)
        self.service = Service(self.folder.name, self.provider, start_workers=False)
        self.provider.service = self.service
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.service.close)

    def save(self, models, key=None):
        return self.service.mutate('PUT', '/v1/settings/fallback', {'models': models}, key or str(uuid4()))

    def test_set_a_chain_and_clear_it(self):
        self.assertEqual(self.save([model('model-b', 'native-b')]), {'models': [model('model-b', 'native-b')]})
        self.assertEqual(self.service.native_snapshot()['fallback'], {'models': [model('model-b', 'native-b')]})
        self.assertEqual(self.save([]), {'models': []})
        self.assertEqual(self.provider.chain, [])

    def test_a_save_wakes_the_model_follow(self):
        self.service.model_follow_wake.clear()
        self.save([model('model-b', 'native-b')])
        self.assertTrue(self.service.model_follow_wake.is_set())

    def test_lost_reply_does_not_repeat_the_native_write(self):
        key = str(uuid4())
        self.provider.lose_reply = True
        with self.assertRaises(ServiceError) as raised:
            self.save([model('model-b', 'native-b')], key)
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(self.save([model('model-b', 'native-b')], key), {'models': [model('model-b', 'native-b')]})
        self.assertEqual(self.provider.writes, [[model('model-b', 'native-b')]])
        with self.assertRaises(ServiceError) as raised:
            self.save([], key)
        self.assertEqual(raised.exception.status, 409)

    def test_a_pending_save_that_did_not_happen_becomes_an_error(self):
        key = str(uuid4())
        self.provider.lose_reply = True
        with self.assertRaises(ServiceError):
            self.save([model('model-b', 'native-b')], key)
        # Something else changed the chain before the retry.
        self.provider.chain = []
        with self.assertRaises(ServiceError) as raised:
            self.save([model('model-b', 'native-b')], key)
        self.assertEqual(raised.exception.status, 409)
        self.assertIn('could not confirm the fallback model', str(raised.exception))

    def test_bad_bodies_are_rejected(self):
        bad = [None, 'model-b', {'model': 'model-b', 'provider': 'native-b'}, [None], [{'model': 'model-b'}],
               [{'model': 'model-b', 'provider': 'native-b', 'base_url': 'https://b.example'}],
               [{'model': 1, 'provider': 'native-b'}], [{'model': ' ', 'provider': 'native-b'}],
               [model('model-b', 'native-b'), model('model-b', 'native-b')],
               [model(f'model-{index}', 'native-b') for index in range(33)]]
        for models in bad:
            with self.subTest(models=models), self.assertRaises(ServiceError) as raised:
                self.save(models)
            self.assertEqual(raised.exception.status, 400)
        for body in (None, {}, {'models': [], 'enabled': True}, {'enabled': True}):
            with self.subTest(body=body), self.assertRaises(ServiceError) as raised:
                self.service.mutate('PUT', '/v1/settings/fallback', body, str(uuid4()))
            self.assertEqual(raised.exception.status, 400)
        self.assertEqual(self.provider.writes, [])

    def test_an_existing_entry_outside_the_catalog_stays(self):
        self.provider.chain = [{'model': 'other-model', 'provider': 'other', 'base_url': 'https://other.example', 'key_env': 'OTHER_KEY'}]
        result = self.save([model('model-b', 'native-b'), model('other-model', 'other')])
        self.assertEqual(result, {'models': [model('model-b', 'native-b'), model('other-model', 'other')]})
        self.assertEqual(self.provider.chain[1], {'model': 'other-model', 'provider': 'other',
                                                  'base_url': 'https://other.example', 'key_env': 'OTHER_KEY'})
        self.assertNotIn('OTHER_KEY', str(self.service.native_snapshot()['fallback']))

    def test_a_new_entry_must_be_in_the_catalog_and_not_the_default(self):
        for models, message in (([model('other-model', 'other')], 'Select an available model'),
                                ([model('model-a', 'native-a')], 'other than the default model')):
            with self.subTest(models=models), self.assertRaises(ServiceError) as raised:
                self.save(models)
            self.assertEqual(raised.exception.status, 400)
            self.assertIn(message, str(raised.exception))
        self.assertEqual(self.provider.writes, [])

    def test_a_route_that_the_chain_has_twice_can_stay_twice(self):
        first = {'model': 'model-b', 'provider': 'native-b', 'base_url': 'https://one.example'}
        second = {'model': 'model-b', 'provider': 'native-b', 'base_url': 'https://two.example'}
        other = {'model': 'other-model', 'provider': 'other'}
        self.provider.chain = [deepcopy(first), deepcopy(other), deepcopy(second)]
        result = self.save([model('model-b', 'native-b'), model('model-b', 'native-b'), model('other-model', 'other')])
        self.assertEqual(result['models'], [model('model-b', 'native-b')] * 2 + [model('other-model', 'other')])
        self.assertEqual(self.provider.chain, [first, second, other])
        # The app removes the first model. The second copy keeps its own endpoint.
        self.save([model('model-b', 'native-b'), model('other-model', 'other')])
        self.assertEqual(self.provider.chain, [second, other])
        with self.assertRaises(ServiceError) as raised:
            self.save([model('model-b', 'native-b'), model('model-b', 'native-b')])
        self.assertEqual(raised.exception.status, 400)

    def test_choosing_a_model_that_the_chain_has_twice_keeps_both_entries(self):
        other = {'model': 'other-model', 'provider': 'other'}
        first = {'model': 'model-b', 'provider': 'native-b', 'base_url': 'https://one.example'}
        second = {'model': 'model-b', 'provider': 'native-b', 'base_url': 'https://two.example'}
        self.provider.chain = [deepcopy(other), deepcopy(first), deepcopy(second)]
        # The app replaces the first model with model-b and keeps the rest of the chain.
        self.assertEqual(self.save([model('model-b', 'native-b')] * 2), {'models': [model('model-b', 'native-b')] * 2})
        self.assertEqual(self.provider.chain, [first, second])

    def test_a_pending_record_of_the_old_switch_settles_as_an_error(self):
        old = str(uuid4())
        with self.service.transaction():
            self.service.db.execute('INSERT INTO requests VALUES (?,?,?)', (old, 'old-fingerprint', json.dumps({'_fallback_pending': True})))
        self.assertEqual(self.save([model('model-b', 'native-b')]), {'models': [model('model-b', 'native-b')]})
        response = json.loads(self.service.db.execute('SELECT response FROM requests WHERE key=?', (old,)).fetchone()[0])
        self.assertIn('could not confirm', response['_fallback_error'])

    def test_a_save_without_a_change_does_not_write(self):
        self.provider.chain = [{'model': 'model-b', 'provider': 'native-b', 'key_env': 'B_KEY'}]
        key = str(uuid4())
        self.service.model_follow_wake.clear()
        cursor = self.service._cursor()
        expected = {'models': [model('model-b', 'native-b')]}
        self.assertEqual(self.save([model('model-b', 'native-b')], key), expected)
        self.assertEqual(self.save([model('model-b', 'native-b')], key), expected)
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.service._cursor(), cursor)
        self.assertFalse(self.service.model_follow_wake.is_set())

    def test_a_lost_reply_wakes_the_model_follow(self):
        self.service.model_follow_wake.clear()
        self.provider.lose_reply = True
        with self.assertRaises(ServiceError):
            self.save([model('model-b', 'native-b')])
        self.assertTrue(self.service.model_follow_wake.is_set())

    def test_the_request_limit_allows_a_long_chain_of_hermes(self):
        long = [{'model': f'kept-{index}', 'provider': 'other'} for index in range(9)]
        self.provider.chain = deepcopy(long)
        result = self.save(list(reversed(long)))
        self.assertEqual(result['models'], list(reversed(long)))
        with self.assertRaises(ServiceError) as raised:
            self.save([model(f'model-{index}', 'native-b') for index in range(33)])
        self.assertEqual(raised.exception.status, 400)
