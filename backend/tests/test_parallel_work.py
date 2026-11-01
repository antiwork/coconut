from copy import deepcopy
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.parallel_work import automatic_limit, save, state
from coconut.service import Service, ServiceError
from test_bot_model_settings import NativeProvider


class SettingsProvider(NativeProvider):
    def __init__(self, root):
        super().__init__(root)
        self.limit = None
        self.limits = {}
        self.fail_after_save = False

    def native_settings(self, operation='read', bots=(), **payload):
        self.outside_database_lock()
        if operation == 'routines_read':
            return {'routines': []}
        if operation == 'set_parallel_work':
            self.limit = payload['limit']
            self.writes.append(('workers', self.limit))
            if self.fail_after_save:
                self.fail_after_save = False
                raise OSError('Lost response')
        value = dict(limit=self.limit, effective_limit=self.limit or 4, automatic_limit=4)
        if operation == 'set_parallel_work':
            return value
        return dict(approvals={'default': 'off', 'modes': []}, parallel_work=value)

    def set_bot_settings(self, bot, changes):
        self.outside_database_lock()
        if 'worker_limit' in changes:
            self.limits[bot['id']] = changes['worker_limit']

    def observe_bot(self, bot):
        return dict(super().observe_bot(bot), worker_limit=self.limits.get(bot['id']))


class ParallelWorkTests(unittest.TestCase):
    def test_native_config_keeps_the_profile_override_separate(self):
        configs = {'default': {'delegation': {'worktree_isolation': True}}, 'bot': {'delegation': {'max_concurrent_children': 3}}}
        native = types.SimpleNamespace(raw_config=lambda home: deepcopy(configs[str(home)]),
                                      save_config=lambda home, config: configs.update({str(home): config}))
        with patch('coconut.parallel_work.raw_config', native.raw_config), patch('coconut.parallel_work.save_config', native.save_config), \
                patch('coconut.parallel_work.hardware', return_value=(10, 16 * 1024 ** 3)):
            self.assertEqual(state('default')['effective_limit'], 5)
            save('default', 8)
            self.assertEqual(state('default')['effective_limit'], 8)
            self.assertEqual(configs['bot']['delegation']['max_concurrent_children'], 3)
            self.assertTrue(configs['default']['delegation']['worktree_isolation'])
            save('default', None)
            self.assertEqual(state('default')['effective_limit'], 5)
            self.assertNotIn('max_concurrent_children', configs['default']['delegation'])
            save('bot', None, inherit=True)
            self.assertIn('max_concurrent_children', configs['bot']['delegation'])
            self.assertIsNone(configs['bot']['delegation']['max_concurrent_children'])

    def test_automatic_limit_reserves_resources_and_handles_unknown_memory(self):
        self.assertEqual(automatic_limit(10, 16 * 1024 ** 3), 5)
        self.assertEqual(automatic_limit(32, 8 * 1024 ** 3), 2)
        self.assertEqual(automatic_limit(32, 128 * 1024 ** 3), 10)
        self.assertEqual(automatic_limit(1, 2 * 1024 ** 3), 1)
        self.assertEqual(automatic_limit(16, None), 2)

    def test_save_restart_retry_and_bot_override(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = SettingsProvider(directory)
            service = Service(directory, provider, start_workers=False)
            provider.service = service
            def write(path, payload, key=None, method='PUT'):
                return service.mutate(method, path, payload, key or str(uuid4()), 'phone')
            try:
                bot = write('/v1/bots', {'name': 'Dev'}, method='POST')
                result = write('/v1/settings/parallel-work', {'limit': 6})
                self.assertEqual(result['effective_limit'], 6)
                result = write('/v1/bots/' + bot['id'], {'worker_limit': 2}, method='PATCH')
                self.assertEqual(result['worker_limit'], 2)
                key = str(uuid4())
                provider.fail_after_save = True
                with self.assertRaises(ServiceError):
                    write('/v1/settings/parallel-work', {'limit': 8}, key)
                service.close()
                provider.service = None
                service = Service(directory, provider, start_workers=False)
                provider.service = service
                self.assertEqual(write('/v1/settings/parallel-work', {'limit': 8}, key)['effective_limit'], 8)
                self.assertEqual(provider.writes.count(('workers', 8)), 1)
                snapshot = service.snapshot()
                self.assertEqual(snapshot['parallel_work']['effective_limit'], 8)
                self.assertEqual(snapshot['bots'][0]['worker_limit'], 2)
                result = write('/v1/bots/' + bot['id'], {'worker_limit': None}, method='PATCH')
                self.assertIsNone(result['worker_limit'])
                for invalid in (True, 0, 65, '4', 1.5):
                    with self.assertRaises(ServiceError):
                        write('/v1/settings/parallel-work', {'limit': invalid})
                    with self.assertRaises(ServiceError):
                        write('/v1/bots/' + bot['id'], {'worker_limit': invalid}, method='PATCH')
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
