import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import Service, ServiceError
from tests.test_service import ControlledProvider
from tests.test_google_connection import MemoryProfile
from coconut.google_connection import GoogleConnections, SCOPES


class GoogleServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.service = Service(self.folder.name, ControlledProvider(), start_workers=False)
        self.manager = GoogleConnections(self.folder.name, '123-test.apps.googleusercontent.com', MemoryProfile)
        self.factory = patch('coconut.google_connection.GoogleConnections', return_value=self.manager)
        self.factory.start()

    def tearDown(self):
        self.factory.stop()
        self.service.close()
        self.folder.cleanup()
        MemoryProfile.saved.clear()

    def request(self, action, **data):
        return self.service.mutate('POST', '/v1/settings/google/' + action, data, str(uuid4()), 'phone')

    def test_phone_route_connects_without_persisting_oauth_secrets_in_events_or_requests(self):
        flow = self.request('start')
        grant = dict(access_token='private-access', refresh_token='private-refresh', expires_in=3600, scope=' '.join(SCOPES))
        with patch('coconut.google_connection.request_json', side_effect=[grant, dict(email='owner@example.com', email_verified=True, sub='owner')]):
            result = self.request('finish', state=flow['state'], code='private-code')
        self.assertTrue(result['connected'])
        event = self.service.events(0)['events'][-1]
        self.assertEqual(event['type'], 'google.updated')
        self.assertIsNone(event['bot_id'])
        self.assertEqual(event['data'], result)
        rows = str(list(self.service.db.execute('SELECT * FROM requests'))) + str(self.service.events(0))
        for secret in ['private-access', 'private-refresh', 'private-code', flow['state']]:
            self.assertNotIn(secret, rows)
        self.assertFalse(self.request('disconnect')['connected'])

    def test_connection_is_available_before_any_bot_exists(self):
        self.assertEqual(self.service.snapshot()['bots'], [])
        self.assertFalse(self.service.google_connection('status', {}, 'phone')['connected'])
        self.assertIn('authorization_url', self.request('start'))
