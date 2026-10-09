import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from unittest.mock import patch

from coconut.providers import FixtureProvider
from coconut.server import APIServer
from coconut.service import Service


class RequestResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)

    def test_table_reads_batch_decoding_without_changing_values_or_order(self):
        self.assertEqual(self.service._all('artifacts'), [])
        expected = [dict(id=str(200-index), text='Line one\n"quoted", café 🍉',
                         detail={'number': index, 'value': None, 'enabled': True}) for index in range(200)]
        with self.service.transaction():
            for item in expected:
                self.service._put('artifacts', item)
        with patch('coconut.service.json.loads', wraps=json.loads) as decode:
            actual = self.service._all('artifacts')
        self.assertEqual(actual, expected)
        self.assertEqual(decode.call_count, 1, 'Decode one database result instead of switching per row.')

    def test_table_reads_preserve_rows_when_combined_value_exceeds_sqlite_limit(self):
        expected = [dict(id=str(index), text='x' * 200) for index in range(20)]
        with self.service.transaction():
            for item in expected:
                self.service._put('artifacts', item)
        previous = self.service.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 512)
        try:
            self.assertEqual(self.service._all('artifacts'), expected)
        finally:
            self.service.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous)

    def test_read_view_reuses_lookups_only_within_the_same_snapshot(self):
        bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        with self.service._read_view() as view:
            queries = []
            view.db.set_trace_callback(queries.append)
            with view._read_transaction():
                first = view._get('bots', bot['id'])
                first['name'] = 'Changed copy'
                for _ in range(20):
                    self.assertEqual(view._get('bots', bot['id'])['name'], 'Desk')
                selects = [query for query in queries if query.startswith('SELECT data FROM bots WHERE')]
                self.assertEqual(len(selects), 1, 'Repeated Work links must reuse the snapshot lookup.')
            with self.service.transaction():
                bot['name'] = 'Updated Desk'
                self.service._put('bots', bot)
            with view._read_transaction():
                self.assertEqual(view._get('bots', bot['id'])['name'], 'Updated Desk')

    def test_table_read_reuses_rows_without_sharing_mutable_values(self):
        item = dict(id='first', detail={'text': 'Original'})
        with self.service.transaction():
            self.service._put('artifacts', item)
        with self.service._read_view() as view:
            queries = []
            view.db.set_trace_callback(queries.append)
            with view._read_transaction():
                view._all('artifacts')[0]['detail']['text'] = 'Changed copy'
                for _ in range(3):
                    self.assertEqual(view._get('artifacts', item['id']), item)
                    self.assertEqual(view._all('artifacts'), [item])
                selects = [query for query in queries if query.startswith('SELECT')]
                self.assertEqual(len(selects), 1, 'Reuse rows already loaded by this snapshot.')
                with self.service.transaction():
                    updated = dict(item, detail={'text': 'Updated'})
                    self.service._put('artifacts', updated)
                self.assertEqual(view._all('artifacts'), [item])
            with view._read_transaction():
                self.assertEqual(view._all('artifacts'), [updated])
                self.assertEqual(view._get('artifacts', item['id']), updated)

    def test_snapshot_batches_native_reads_and_observes_later_changes(self):
        from types import SimpleNamespace
        from coconut.providers import HermesProvider
        bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()))
        native = {'profiles': {bot['id']: {'name': 'Native Desk', 'instructions': 'Current instructions'}},
                  'approvals': {'default': 'off'}, 'parallel_work': {'effective_limit': 5},
                  'mcp_servers': [], 'routines': []}
        provider = object.__new__(HermesProvider)
        provider._profile_settings_lock, provider._profile_settings = threading.Lock(), {}
        calls = []
        def read(operation, **payload):
            calls.append((operation, payload['native_operation']))
            return native
        provider.catalog = SimpleNamespace(_call=read, _profile_home=lambda identity: self.service.root / identity,
                                           read_profiles=lambda identities: {})
        self.service.provider.observe_state = provider.observe_state
        first = self.service.snapshot()
        self.assertEqual(calls, [('native', 'snapshot_read')])
        self.assertEqual(first['bots'][0]['name'], 'Native Desk')
        self.assertEqual(first['approvals'], native['approvals'])
        self.assertEqual(first['parallel_work'], native['parallel_work'])
        native['profiles'][bot['id']]['name'] = 'Updated Desk'
        second = self.service.snapshot()
        self.assertEqual(second['bots'][0]['name'], 'Updated Desk')
        self.assertEqual(len(calls), 2, 'Each snapshot must read current Hermes settings.')

    def test_task_context_reads_do_not_block_another_tools_receipt(self):
        service = self.service
        bot = service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        first = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Check one.'}, str(uuid4()))
        second = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Check two.', 'mode': 'queue'}, str(uuid4()))
        with service.transaction():
            second = service._get('tasks', second['id'])
            second.update(status='working', _native_turn_id=str(uuid4()))
            service._put('tasks', second)
        reading, release = threading.Event(), threading.Event()
        original = Service._tracked_work_input
        def slow_read(view, task):
            reading.set()
            release.wait(3)
            return original(view, task)
        with patch.object(Service, '_tracked_work_input', slow_read), ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(service._run_task, first['id'])
            self.assertTrue(reading.wait(2))
            result = pool.submit(service.native_event, second['id'], 'native_call',
                                 {'name': 'read_file', 'arguments': {'path': 'README.md'}})
            try:
                self.assertEqual(result.result(timeout=.5)['status'], 'recorded')
            finally:
                release.set()
            worker.result(timeout=3)

    def test_cancel_during_context_reads_prevents_model_execution(self):
        service = self.service
        bot = service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        task = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Check one.'}, str(uuid4()))
        original = Service._tracked_work_input
        def cancel_during_read(view, current):
            with service.transaction():
                cancelled = service._get('tasks', task['id'])
                cancelled['status'] = 'cancelled'
                service._put('tasks', cancelled)
            return original(view, current)
        with patch.object(Service, '_tracked_work_input', cancel_during_read), patch.object(service.provider, 'start_task') as start:
            service._run_task(task['id'])
        start.assert_not_called()

    def test_archiving_during_context_reads_preserves_queued_work(self):
        service = self.service
        bot = service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        task = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Check one.'}, str(uuid4()))
        original = Service._tracked_work_input
        def archive_during_read(view, current):
            with service.transaction():
                archived = service._get('bots', bot['id'])
                archived['archived'] = True
                service._put('bots', archived)
            return original(view, current)
        with patch.object(Service, '_tracked_work_input', archive_during_read), patch.object(service.provider, 'start_task') as start:
            service._run_task(task['id'])
        start.assert_not_called()
        self.assertEqual(service._get('tasks', task['id'])['status'], 'queued')
        with service.transaction():
            service._put('bots', bot)
        with patch.object(service.provider, 'start_task') as start:
            service._run_task(task['id'])
        start.assert_called_once()

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
