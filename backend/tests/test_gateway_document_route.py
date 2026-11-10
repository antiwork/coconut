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

from coconut.server import APIServer, TunnelServer
from coconut.service import Service
from test_gateway_chat import Provider

PAGE = dict(key='weekly-report', title='Weekly report', format='markdown', summary='3 open tasks',
            content='3 tasks are open.\n\n| State | Tasks |\n|---|---|\n| Open | 3 |\n| Done | 5 |\n')
# The static key of a paired phone, as the tunnel sidecar names it.
PHONE = {'X-Coconut-Peer': 'a' * 64}


class GatewayPageRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, Provider(), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Ada'}, str(uuid4()), 'phone')
        self.home = str(self.service.profiles.home(self.bot))
        self.server = self.serve(APIServer(('127.0.0.1', 0), self.service))
        # The service writes the address and the token of the bridge file that the adapter of the gateway reads.
        self.service.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_port}')
        self.token = json.loads((Path(self.temp.name) / 'gateway' / 'bridge.json').read_text())['token']

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        self.addCleanup(self.close, server, thread)
        return server

    def close(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def post(self, path, data, token=None, *, server=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', (server or self.server).server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = {'Content-Type': 'application/json', **(headers or {})}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        connection.request('POST', path, body=json.dumps(data), headers=headers)
        response = connection.getresponse()
        return response.status, json.load(response)

    def test_the_route_saves_a_page_for_the_bot_of_a_hermes_home(self):
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        self.assertEqual((status, answer), (200, {'status': 'saved', 'key': 'weekly-report', 'version': 1,
                                                  'expires_at': None}))
        documents = self.service.documents()['documents']
        self.assertEqual([(row['key'], row['title'], row['bot_id']) for row in documents],
                         [('weekly-report', 'Weekly report', self.bot['id'])])
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

    def test_a_device_token_does_not_open_the_route(self):
        device = self.service.pair({'code': self.service.pairing_code()})['token']
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home), device)
        self.assertEqual(status, 401)
        self.assertIn('bridge.json', answer['error'])
        self.assertEqual(self.service.documents()['documents'], [])

    def test_the_tunnel_listener_does_not_serve_the_route(self):
        # The tunnel listener carries the requests of a paired phone. A phone keeps no page for a bot.
        tunnel = self.serve(TunnelServer(0, self.service))
        secret = self.service.pairing_code(600, tunnel=True)
        device = self.service.pair({'code': secret}, PHONE['X-Coconut-Peer'])['token']
        page = dict(PAGE, home=self.home)
        # The token of the bridge is not the token of a device, with the key of a phone and without it.
        for headers in ({}, PHONE):
            with self.subTest(headers=headers):
                status, answer = self.post('/v1/gateway/document', page, self.token, server=tunnel, headers=headers)
                self.assertEqual((status, answer['error']), (401, 'Pair this phone with the Mac to continue.'))
        # The phone has no such route.
        status, answer = self.post('/v1/gateway/document', page, device, server=tunnel,
                                   headers={**PHONE, 'Idempotency-Key': str(uuid4())})
        self.assertEqual((status, answer['error']), (404, 'This route was not found.'))
        self.assertEqual(self.service.documents()['documents'], [])
        # The same token saves the page on the main listener.
        self.assertEqual(self.post('/v1/gateway/document', page, self.token)[1]['status'], 'saved')

    def test_a_home_that_is_not_a_profile_of_a_bot_keeps_no_page(self):
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.temp.name), self.token)
        self.assertEqual(status, 404)
        self.assertIn('not the profile of a bot', answer['error'])
        self.assertEqual(self.service.documents()['documents'], [])

    def test_an_archived_bot_and_a_deleted_bot_keep_no_page(self):
        def write(method, data=None):
            self.service.mutate(method, f'/v1/bots/{self.bot["id"]}', data or {}, str(uuid4()), 'phone')
        write('PATCH', {'archived': True})
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        self.assertEqual((status, answer['error']), (409, 'The bot is archived or deleted.'))
        # The profile of a deleted bot goes with it, so its Hermes home is no longer the profile of a bot.
        write('DELETE')
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        self.assertEqual(status, 404)
        self.assertIn('not the profile of a bot', answer['error'])
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM documents').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
