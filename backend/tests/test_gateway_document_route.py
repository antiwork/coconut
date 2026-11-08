"""The route of the tooling of the Mac on the real listener: the page it saves, and the token it needs.

`test_documents.py` covers the page rules through the service. This file goes through the listener that the adapter of
the Hermes gateway talks to, so the path of the route in GATEWAY_ROUTES, the token of the bridge file and the answer
the adapter reads are covered: a route that is missing from the table, or an answer the adapter cannot read, fails here
and nowhere else.
"""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from coconut.server import APIServer
from coconut.service import Service
from test_gateway_chat import Provider

PAGE = dict(key='dashboard-home', title='Home', format='markdown', summary='32 past SLA',
            content='32 items are past their SLA.\n\n| Queue | Open |\n|---|---|\n| Issues | 32 |\n')


class GatewayPageRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, Provider(), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'phone')
        self.home = str(self.service.profiles.home(self.bot))
        self.server = APIServer(('127.0.0.1', 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        # The service writes the address and the token of the bridge file that the adapter of the gateway reads.
        self.service.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_port}')
        self.token = json.loads((Path(self.temp.name) / 'gateway' / 'bridge.json').read_text())['token']

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def post(self, path, data, token=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        connection.request('POST', path, body=json.dumps(data), headers=headers)
        response = connection.getresponse()
        return response.status, json.load(response)

    def test_the_route_saves_a_page_for_the_bot_of_a_hermes_home(self):
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        self.assertEqual((status, answer), (200, {'status': 'saved', 'key': 'dashboard-home', 'version': 1,
                                                  'expires_at': None}))
        documents = self.service.documents()['documents']
        self.assertEqual([(row['key'], row['title'], row['bot_id']) for row in documents],
                         [('dashboard-home', 'Home', self.bot['id'])])
        self.assertEqual(self.service.document(documents[0]['id'])['content'], PAGE['content'])

    def test_a_second_save_of_the_same_page_writes_nothing(self):
        self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        self.assertEqual((status, answer['status'], answer['version']), (200, 'unchanged', 1))

    def test_the_route_needs_the_token_of_the_bridge(self):
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home))
        self.assertEqual(status, 401)
        self.assertIn('bridge.json', answer['error'])
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home), 'not-the-token')
        self.assertEqual(status, 401)
        self.assertEqual(self.service.documents()['documents'], [])

    def test_a_home_that_is_not_a_profile_of_a_bot_keeps_no_page(self):
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.temp.name), self.token)
        self.assertEqual(status, 404)
        self.assertIn('not the profile of a bot', answer['error'])
        self.assertEqual(self.service.documents()['documents'], [])


if __name__ == '__main__':
    unittest.main()
