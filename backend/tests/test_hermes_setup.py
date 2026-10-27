"""GET /v1/setup reports only the state of the person's Hermes. The earlier onboarding import is gone."""
import tempfile
import unittest
from uuid import uuid4

from coconut.service import Service, ServiceError
from test_service import ControlledProvider


class SetupRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.device = self.service.pair({'code': self.service.pairing_code()})['device_id']

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def test_the_setup_route_has_only_the_hermes_status(self):
        self.assertEqual(self.service.hermes_setup(),
                         {'hermes': {'status': 'fixture', 'version': None, 'reason': None, 'tested': None}})
        self.service.hermes_status = {'status': 'ok', 'version': '0.22.5', 'reason': None, 'tested': True}
        self.assertEqual(self.service.hermes_setup()['hermes']['version'], '0.22.5')

    def test_the_import_route_is_gone(self):
        with self.assertRaises(ServiceError) as raised:
            self.service.mutate('POST', '/v1/setup/import', {'profile_ids': ['default'], 'logins': True}, str(uuid4()),
                                self.device)
        self.assertEqual(raised.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
