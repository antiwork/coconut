"""Check native fallback settings and uncertain saves through the service."""
from copy import deepcopy
import tempfile
import unittest
from uuid import uuid4

from coconut.service import Service, ServiceError
from test_bot_model_settings import NativeProvider


class Provider(NativeProvider):
    def __init__(self, root):
        super().__init__(root)
        self.fallback = dict(enabled=False, available=True, model='gpt-6-astra', can_enable=True)
        self.lose_reply = False

    def native_settings(self, operation='read', bots=(), **payload):
        self.outside_database_lock()
        if operation == 'routines_read':
            return {'routines': []}
        if operation == 'set_api_fallback':
            self.fallback['enabled'] = payload['enabled']
            self.writes.append(payload['enabled'])
            if self.lose_reply:
                self.lose_reply = False
                raise OSError('Lost reply')
            return deepcopy(self.fallback)
        return dict(api_fallback=deepcopy(self.fallback), approvals={'default': 'off', 'modes': []})


class APIFallbackTests(unittest.TestCase):
    def test_toggle_and_lost_reply_do_not_repeat_the_native_write(self):
        with tempfile.TemporaryDirectory() as root:
            provider = Provider(root)
            service = Service(root, provider, start_workers=False)
            provider.service = service
            def save(enabled, key=None):
                return service.mutate('PUT', '/v1/settings/api-fallback', {'enabled': enabled}, key or str(uuid4()))
            try:
                self.assertTrue(save(True)['enabled'])
                self.assertTrue(service.native_snapshot()['api_fallback']['enabled'])
                key = str(uuid4())
                provider.lose_reply = True
                with self.assertRaises(ServiceError):
                    save(False, key)
                self.assertFalse(save(False, key)['enabled'])
                self.assertEqual(provider.writes, [True, False])
                with self.assertRaises(ServiceError):
                    save(True, key)
                for value in (None, 'true', 1):
                    with self.assertRaises(ServiceError):
                        save(value)
                provider.fallback['can_enable'] = False
                with self.assertRaises(ServiceError):
                    save(True)
                self.assertFalse(save(False)['enabled'])
            finally:
                service.close()
