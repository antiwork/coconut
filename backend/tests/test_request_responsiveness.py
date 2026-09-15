import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.server import APIServer
from coconut.service import Service


class RequestResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)

    def test_slow_settings_read_does_not_block_message_acceptance(self):
        service = self.service
        bot = service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        reading, release, accepted = threading.Event(), threading.Event(), threading.Event()

        def settings(operation='read', bots=()):
            if operation == 'routines_read':
                return {'routines': []}
            reading.set()
            release.wait(3)
            return {'approvals': {}, 'mcp_servers': []}

        service.provider.native_settings = settings
        def send():
            result = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Check the report.'}, str(uuid4()))
            accepted.set()
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            snapshot = pool.submit(service.snapshot)
            self.assertTrue(reading.wait(2))
            message = pool.submit(send)
            try:
                self.assertTrue(accepted.wait(.3), 'Reading settings must not hold the queue lock.')
            finally:
                release.set()
            self.assertEqual(message.result()['status'], 'queued')
            self.assertIn('approvals', snapshot.result())

    def test_snapshot_retries_when_settings_change_during_the_read(self):
        service = self.service
        reading, release = threading.Event(), threading.Event()
        servers = []
        reads = []
        def settings(operation='read', bots=(), **payload):
            if operation == 'routines_read':
                return {'routines': []}
            if operation == 'mcp':
                servers.append({'name': 'Reports'})
                return {'ok': True}
            result = {'approvals': {}, 'mcp_servers': list(servers)}
            reads.append(result)
            if len(reads) == 1:
                reading.set()
                release.wait(3)
            return result
        service.provider.native_settings = settings
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(service.snapshot)
            try:
                self.assertTrue(reading.wait(2))
                service.mutate('POST', '/v1/settings/mcp', {'action': 'add', 'name': 'Reports'}, str(uuid4()))
            finally:
                release.set()
            snapshot = pending.result()
        self.assertEqual(snapshot['mcp_servers'], [{'name': 'Reports'}])
        self.assertEqual(snapshot['cursor'], service._cursor())

    def test_busy_queue_does_not_block_http_acceptance(self):
        service = self.service
        checking = threading.Event()
        original = service.take_service_restart
        def check_restart():
            checking.set()
            return original()
        service.take_service_restart = check_restart
        server = APIServer(('127.0.0.1', 0), service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        try:
            with service.lock:
                thread.start()
                self.assertTrue(checking.wait(1))
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=.3)
                try:
                    connection.request('GET', '/health')
                    self.assertEqual(connection.getresponse().status, 200)
                finally:
                    connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_snapshot_retries_a_routine_changed_during_native_read(self):
        service = self.service
        bot = service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()))
        routine = dict(id=str(uuid4()), bot_id=bot['id'], _native_id='native-job', enabled=True)
        with service.transaction():
            service._put('routines', routine)
        reading, release = threading.Event(), threading.Event()
        calls = []
        def settings(operation='read', bots=(), **arguments):
            if operation == 'routines_read':
                result = dict(routine)
                calls.append(result)
                if len(calls) == 1:
                    reading.set()
                    release.wait(3)
                return {'routines': [result]}
            if operation == 'routine_update':
                routine.update(arguments['changes'])
                return dict(routine)
            return {'approvals': {}}
        service.provider.native_settings = settings
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(service.snapshot)
            try:
                self.assertTrue(reading.wait(2))
                service.mutate('PATCH', f"/v1/routines/{routine['id']}", {'enabled': False}, str(uuid4()))
            finally:
                release.set()
            snapshot = pending.result()
        self.assertFalse(snapshot['routines'][0]['enabled'])
        self.assertEqual(snapshot['cursor'], service._cursor())

    def test_app_reads_remain_available_during_worker_transaction(self):
        service = self.service
        token = service.pair({'code': service.pairing_code(), 'device_name': 'iPhone'})['token']
        bot = service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()))
        cursor = service._cursor()
        server = APIServer(('127.0.0.1', 0), service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        def read(path):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=1)
            try:
                connection.request('GET', '/v1/' + path, headers={'Authorization': 'Bearer ' + token})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                return json.load(response)
            finally:
                connection.close()
        try:
            with service.transaction():
                changed = service._get('bots', bot['id'])
                changed['name'] = 'Updated Desk'
                service._put('bots', changed)
                service._event('bot.updated', changed, bot['id'])
                snapshot = read('state')
                self.assertEqual(snapshot['bots'][0]['name'], 'Desk')
                self.assertEqual(snapshot['cursor'], cursor)
                self.assertEqual(read(f'events?after={cursor}&wait=0')['events'], [])
            self.assertEqual(read('state')['bots'][0]['name'], 'Updated Desk')
            self.assertEqual(len(read(f'events?after={cursor}&wait=0')['events']), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_snapshot_includes_routines_for_a_bot_created_during_read(self):
        service = self.service
        service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()))
        reading, release = threading.Event(), threading.Event()
        routines = []
        def settings(operation='read', bots=(), **arguments):
            if operation == 'routines_read':
                result = [dict(r) for r in routines if r['bot_id'] in {b['id'] for b in bots}]
                if not reading.is_set():
                    reading.set()
                    release.wait(3)
                return {'routines': result}
            if operation == 'routine_create':
                routine = dict(id=str(uuid4()), bot_id=arguments['bot_id'], _native_id='new-job',
                               name='Check updates', enabled=True)
                routines.append(routine)
                return routine
            return {'approvals': {}}
        service.provider.native_settings = settings
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(service.snapshot)
            try:
                self.assertTrue(reading.wait(2))
                bot = service.mutate('POST', '/v1/bots', {'name': 'Dev'}, str(uuid4()))
                routine = service.mutate('POST', f"/v1/bots/{bot['id']}/routines", {'name': 'Check updates'}, str(uuid4()))
            finally:
                release.set()
            snapshot = pending.result()
        self.assertIn(bot['id'], {b['id'] for b in snapshot['bots']})
        self.assertEqual(snapshot['routines'][0]['id'], routine['id'])
        self.assertEqual(snapshot['cursor'], service._cursor())


if __name__ == '__main__':
    unittest.main()
