"""The route of the tooling of the Mac on the real listener: the page it saves, and the token it needs.

`test_documents.py` covers the page rules through the service. This file goes through the listener that the adapter of
the Hermes gateway talks to, so the path of the route in GATEWAY_ROUTES, the token of the bridge file and the answer
the adapter reads are covered: a route that is missing from the table, or an answer the adapter cannot read, fails here
and nowhere else.
"""
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from coconut.documents import BOT_SHARE, KEY_RULE
from coconut.server import GATEWAY_ROUTES, PAGE_ROUTE, APIServer, TunnelServer
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
        return self.send('POST', path, data, token, server=server, headers=headers)

    def delete(self, data, token=None, *, server=None, headers=None):
        """DELETE /v1/gateway/document with a JSON body, as the tooling of the Mac sends it."""
        return self.send('DELETE', '/v1/gateway/document', data, token, server=server, headers=headers)

    def send(self, method, path, data, token=None, *, server=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', (server or self.server).server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = {'Content-Type': 'application/json', **(headers or {})}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        connection.request(method, path, body=json.dumps(data), headers=headers)
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

    def test_the_route_of_a_persons_mac_takes_no_audience(self):
        # Only the person shares a page with a team (test_team_pages.py). A team-only Mac takes the audience
        # (test_team_only.py).
        for audience in ('team', 'private', 'other'):
            with self.subTest(audience=audience):
                self.assertEqual(self.post('/v1/gateway/document', dict(PAGE, home=self.home, audience=audience),
                                           self.token), (400, {'error': BOT_SHARE}))
        self.assertEqual(self.service.documents()['documents'], [])
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        self.assertEqual((status, answer['status']), (200, 'saved'))
        self.assertEqual(self.service.documents()['documents'][0]['audience'], 'private')

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
        self.assertIn('not the profile of an agent', answer['error'])
        self.assertEqual(self.service.documents()['documents'], [])

    def test_a_document_tool_without_a_turn_reads_and_deletes_the_pages_of_the_bot_of_a_hermes_home(self):
        # The plugin sends coconut_read_document and coconut_delete_document of a conversation that is not a Coconut
        # chat turn to the tools route, with the Hermes home of the running profile in place of a turn.
        self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        read = dict(home=self.home, tool='read_document', arguments={'key': 'weekly-report'})
        status, answer = self.post('/v1/gateway/tools', read, self.token)
        self.assertEqual((status, answer['key'], answer['content']), (200, 'weekly-report', PAGE['content']))
        # Without the token of the bridge, and on the tunnel listener, the route reads nothing.
        self.assertEqual(self.post('/v1/gateway/tools', read)[0], 401)
        tunnel = self.serve(TunnelServer(0, self.service))
        status, answer = self.post('/v1/gateway/tools', read, self.token, server=tunnel, headers=PHONE)
        self.assertEqual((status, answer['error']), (401, 'Pair this phone with the Mac to continue.'))
        status, answer = self.post('/v1/gateway/tools', dict(read, tool='delete_document'), self.token, server=tunnel,
                                   headers=PHONE)
        self.assertEqual(status, 401)
        self.assertEqual(len(self.service.documents()['documents']), 1)
        status, answer = self.post('/v1/gateway/tools', dict(read, tool='delete_document'), self.token)
        self.assertEqual((status, answer), (200, {'status': 'deleted', 'key': 'weekly-report'}))
        self.assertEqual(self.service.documents()['documents'], [])
        # The other tools need a turn.
        status, answer = self.post('/v1/gateway/tools', dict(read, tool='ask', arguments={}), self.token)
        self.assertEqual(status, 409)
        self.assertIn('only during a Coconut chat turn', answer['error'])

    def test_an_archived_bot_and_a_deleted_bot_keep_no_page(self):
        def write(method, data=None):
            self.service.mutate(method, f'/v1/bots/{self.bot["id"]}', data or {}, str(uuid4()), 'phone')
        write('PATCH', {'archived': True})
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        self.assertEqual((status, answer['error']), (409, 'The agent is archived or deleted.'))
        # The profile of a deleted bot goes with it, so its Hermes home is no longer the profile of a bot.
        write('DELETE')
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        self.assertEqual(status, 404)
        self.assertIn('not the profile of an agent', answer['error'])
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM documents').fetchone()[0], 0)

    # The delete of the tooling

    def saved(self):
        """The documents of the store as (bot, key) pairs, and the count of their versions. An ended page counts."""
        rows = self.service.db.execute("SELECT json_extract(data,'$.bot_id'), json_extract(data,'$.key') "
                                       "FROM documents ORDER BY rowid").fetchall()
        versions = self.service.db.execute('SELECT count(*) FROM document_versions').fetchone()[0]
        return [tuple(row) for row in rows], versions

    def new_events(self, cursor):
        return self.service.db.execute('SELECT type, task_id FROM events WHERE seq>?', (cursor,)).fetchall()

    def test_the_tooling_deletes_a_page_with_all_its_versions(self):
        self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        self.post('/v1/gateway/document', dict(PAGE, home=self.home, content='4 tasks are open.'), self.token)
        self.assertEqual(self.saved(), ([(self.bot['id'], 'weekly-report')], 2))
        cursor = self.service._cursor()
        # Other fields of the body do not count.
        answer = self.delete(dict(PAGE, home=self.home, audience='team'), self.token)
        self.assertEqual(answer, (200, {'status': 'deleted', 'key': 'weekly-report'}))
        self.assertEqual(self.service.documents()['documents'], [])
        self.assertEqual(self.saved(), ([], 0))
        # No turn asked for the delete, so its event names no task, as a delete from the phone.
        self.assertEqual(self.new_events(cursor), [('document.deleted', None)])
        # A later save starts the page again.
        status, answer = self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        self.assertEqual((status, answer['status'], answer['version']), (200, 'saved', 1))

    def test_a_delete_of_a_key_without_a_page_answers_absent_and_writes_nothing(self):
        self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        self.assertEqual(self.delete({'home': self.home, 'key': 'weekly-report'}, self.token)[1]['status'], 'deleted')
        cursor = self.service._cursor()
        for key in ('weekly-report', 'never-saved'):
            with self.subTest(key=key):
                self.assertEqual(self.delete({'home': self.home, 'key': key}, self.token),
                                 (200, {'status': 'absent', 'key': key}))
        self.assertEqual(self.new_events(cursor), [])

    def test_a_delete_with_a_bad_key_answers_the_text_of_the_save(self):
        self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        for body in ({'home': self.home}, {'home': self.home, 'key': 7}, {'home': self.home, 'key': None},
                     {'home': self.home, 'key': 'Weekly Report'}, {'home': self.home, 'key': ''}):
            with self.subTest(body=body):
                self.assertEqual(self.delete(body, self.token), (400, {'error': KEY_RULE}))
        # The save answers the same text.
        self.assertEqual(self.post('/v1/gateway/document', dict(PAGE, home=self.home, key='Weekly Report'),
                                   self.token), (400, {'error': KEY_RULE}))
        self.assertEqual(self.saved()[0], [(self.bot['id'], 'weekly-report')])

    def test_a_delete_needs_the_home_of_a_bot(self):
        self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        status, answer = self.delete({'key': 'weekly-report'}, self.token)
        self.assertEqual(status, 400, answer)
        self.assertIn('home', answer['error'])
        status, answer = self.delete({'home': self.temp.name, 'key': 'weekly-report'}, self.token)
        self.assertEqual((status, answer['error']), (404, 'This Hermes home is not the profile of an agent.'))
        self.assertEqual(self.saved()[0], [(self.bot['id'], 'weekly-report')])

    def test_a_delete_keeps_the_page_of_another_bot_with_the_same_key(self):
        other = self.service.mutate('POST', '/v1/bots', {'name': 'Lin'}, str(uuid4()), 'phone')
        other_home = str(self.service.profiles.home(other))
        for home in (self.home, other_home):
            self.post('/v1/gateway/document', dict(PAGE, home=home), self.token)
        self.assertEqual(self.delete({'home': self.home, 'key': 'weekly-report'}, self.token)[1]['status'], 'deleted')
        self.assertEqual(self.saved(), ([(other['id'], 'weekly-report')], 1))

    def test_a_delete_needs_the_token_of_the_bridge(self):
        self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        device = self.service.pair({'code': self.service.pairing_code()})['token']
        body = {'home': self.home, 'key': 'weekly-report'}
        for token in (None, 'not-the-token', device):
            with self.subTest(token=token):
                status, answer = self.delete(body, token)
                self.assertEqual(status, 401)
                self.assertIn('bridge.json', answer['error'])
        self.assertEqual(len(self.service.documents()['documents']), 1)

    def test_the_tunnel_listener_does_not_serve_the_delete(self):
        self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        tunnel = self.serve(TunnelServer(0, self.service))
        secret = self.service.pairing_code(600, tunnel=True)
        device = self.service.pair({'code': secret}, PHONE['X-Coconut-Peer'])['token']
        body = {'home': self.home, 'key': 'weekly-report'}
        for headers in ({}, PHONE):
            with self.subTest(headers=headers):
                status, answer = self.delete(body, self.token, server=tunnel, headers=headers)
                self.assertEqual((status, answer['error']), (401, 'Pair this phone with the Mac to continue.'))
        status, answer = self.delete(body, device, server=tunnel, headers={**PHONE, 'Idempotency-Key': str(uuid4())})
        self.assertEqual((status, answer['error']), (404, 'This route was not found.'))
        self.assertEqual(len(self.service.documents()['documents']), 1)
        # The same token deletes the page on the main listener.
        self.assertEqual(self.delete(body, self.token)[1]['status'], 'deleted')

    def test_only_the_page_route_takes_a_delete(self):
        # Each other route of the gateway takes POST only. A DELETE there is a request of a phone, which the token of
        # the bridge does not open, so it never reaches the delete of a page.
        self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        body = {'home': self.home, 'key': 'weekly-report', 'tool': 'delete_document',
                'arguments': {'key': 'weekly-report'}}
        for path in GATEWAY_ROUTES:
            if path == PAGE_ROUTE:
                continue
            with self.subTest(path=path):
                status, answer = self.send('DELETE', path, body, self.token)
                self.assertEqual(status, 401, answer)
        self.assertEqual(self.saved(), ([(self.bot['id'], 'weekly-report')], 1))

    def test_an_archived_bot_keeps_its_page_and_a_deleted_bot_has_no_home(self):
        self.post('/v1/gateway/document', dict(PAGE, home=self.home), self.token)
        body = {'home': self.home, 'key': 'weekly-report'}
        self.service.mutate('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': True}, str(uuid4()), 'phone')
        self.assertEqual(self.delete(body, self.token), (409, {'error': 'The agent is archived or deleted.'}))
        self.assertEqual(self.saved()[0], [(self.bot['id'], 'weekly-report')])
        self.service.mutate('DELETE', f'/v1/bots/{self.bot["id"]}', {}, str(uuid4()), 'phone')
        status, answer = self.delete(body, self.token)
        self.assertEqual((status, answer['error']), (404, 'This Hermes home is not the profile of an agent.'))

    def test_a_delete_removes_a_page_that_has_ended(self):
        ends = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        self.post('/v1/gateway/document', dict(PAGE, home=self.home, expires_at=ends), self.token)
        self.service.db.execute("UPDATE documents SET data=json_set(data,'$.expires_at','2026-01-01T00:00:00Z')")
        # The Library does not show an ended page, but the store still has its rows.
        self.assertEqual(self.service.documents()['documents'], [])
        self.assertEqual(self.delete({'home': self.home, 'key': 'weekly-report'}, self.token),
                         (200, {'status': 'deleted', 'key': 'weekly-report'}))
        self.assertEqual(self.saved(), ([], 0))


if __name__ == '__main__':
    unittest.main()
