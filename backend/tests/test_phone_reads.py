"""The phone's reads do not wait for the service lock, read only the rows they show, and leave a timing line."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.server import TunnelServer, request_timing
from coconut.service import Service
from coconut.state_sync import CONFIRMED_REQUESTS, READ_INDEXES
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()

PEER = 'ab' * 32
# The message page before the read used indexes. The tests compare the new page with it.
REFERENCE_PAGE = (
    "SELECT json_group_array(json_array(position,json(data))) FROM (SELECT rowid AS position,data FROM messages WHERE (? IS NULL OR rowid<?) "
    "AND EXISTS(SELECT 1 FROM bots WHERE id=json_extract(messages.data,'$.bot_id') AND NOT COALESCE(json_extract(bots.data,'$._deleted'),0)) "
    "AND NOT COALESCE(json_extract(data,'$._hidden'),0) "
    "AND NOT COALESCE(json_extract(data,'$._deleted'),0) "
    "AND (json_extract(data,'$.bot_id')=? OR ((json_extract(data,'$.kind')='bot_exchange' "
    "OR json_array_length(data,'$.exchange_recipient_bot_ids')>0) AND ("
    "json_extract(data,'$.sender_bot_id')=? OR EXISTS(SELECT 1 FROM json_each(messages.data,'$.recipient_bot_ids') WHERE value=?) "
    "OR EXISTS(SELECT 1 FROM json_each(messages.data,'$.exchange_recipient_bot_ids') WHERE value=?)))) "
    "ORDER BY rowid DESC LIMIT ?)")


# The read of the confirmed requests before it took the bot of each message from the index.
REFERENCE_CONFIRMED = (
    "SELECT json_group_array(DISTINCT json_extract(data,'$.client_request_id')) FROM messages "
    "WHERE json_type(data,'$.client_request_id')='text' AND EXISTS(SELECT 1 FROM bots "
    "WHERE id=json_extract(messages.data,'$.bot_id') AND NOT COALESCE(json_extract(bots.data,'$._deleted'),0))")


class PhoneReadCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.write('POST', '/v1/bots', {'name': 'Desk'})

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), 'phone')

    def hold_write_transaction(self, seconds=10):
        """Hold the service lock and an open write transaction in another thread, as a long card check does."""
        held, release = threading.Event(), threading.Event()
        def write():
            with self.service.transaction():
                self.service._message(self.bot['id'], 'Not committed yet')
                held.set()
                release.wait(seconds)
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        self.assertTrue(held.wait(5))
        def finish():
            release.set()
            writer.join(5)
        self.addCleanup(finish)
        return finish


class ReadsDuringWritesTests(PhoneReadCase):
    def test_phone_reads_finish_while_a_long_write_transaction_holds_the_lock(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check the release'})
        with self.service.transaction():
            self.service._put('decisions', dict(id='card', bot_id=self.bot['id'], task_id=task['id'], status='pending',
                                                revision=1, title='Release', action_type='question', created_at=task['created_at']))
        cursor = self.service._cursor()
        finish = self.hold_write_transaction()
        reads = {
            'state': lambda: self.service.snapshot(content_only=True, compact_history=True, recent_work=True),
            'full state': lambda: self.service.snapshot(),
            'updates': lambda: self.service.updates(cursor - 1, 0, collection_deltas=True, native_events=True, compact_history=True),
            'updates reset': lambda: self.service.updates(0, 0, collection_deltas=True, native_events=True, compact_history=True),
            'events': lambda: self.service.events(0),
            'history': lambda: self.service.message_page(self.bot['id']),
            'card': lambda: self.service.card('card'),
            'settings': lambda: self.service.native_snapshot(),
            'learning': lambda: self.service.learning(),
        }
        durations, results = {}, {}
        for name, read in reads.items():
            started = time.perf_counter()
            results[name] = read()
            durations[name] = (time.perf_counter() - started) * 1000
        finish()
        # The writer holds the lock for 10 seconds. Each read is done long before.
        self.assertLess(max(durations.values()), 2000, durations)
        # A read sees only committed rows.
        self.assertNotIn('Not committed yet', json.dumps(results['state']))
        self.assertEqual(results['updates']['cursor'], cursor)
        self.assertIn('Not committed yet', json.dumps(self.service.snapshot(content_only=True)))

    def test_a_tunnel_state_request_answers_during_a_long_write_and_logs_its_time(self):
        token = self.service.pair({'code': self.service.pairing_code(tunnel=True)}, PEER)['token']
        server = TunnelServer(0, self.service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(close)
        self.hold_write_transaction()
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        self.addCleanup(connection.close)
        started = time.perf_counter()
        with self.assertLogs('coconut.server', 'WARNING') as logs:
            connection.request('GET', '/v1/state', headers={'Authorization': 'Bearer ' + token, 'X-Coconut-Peer': PEER,
                                                            'X-Coconut-State': 'recent', 'X-Coconut-Work': 'recent'})
            response = connection.getresponse()
            body = json.load(response)
            elapsed = (time.perf_counter() - started) * 1000
            # The line is written after the response.
            deadline = time.monotonic() + 2
            while not logs.records and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual(response.status, 200)
        self.assertEqual(body['bots'][0]['id'], self.bot['id'])
        self.assertLess(elapsed, 2000)
        line = json.loads(logs.records[-1].getMessage().split(' ', 1)[1])
        self.assertEqual((line['method'], line['route'], line['status']), ('GET', '/v1/state', 200))
        self.assertLess(line['ms'], 2000)
        self.assertIn('auth', line['stages_ms'])
        self.assertIn('data', line['stages_ms'])


class RequestTimingTests(unittest.TestCase):
    def logged(self, *args):
        with self.assertLogs('coconut.server', 'WARNING') as logs:
            request_timing(*args)
        self.assertTrue(logs.output[0].startswith('WARNING:coconut.server:coconut.request_timing '))
        return json.loads(logs.records[0].getMessage().split(' ', 1)[1])

    def test_the_line_names_the_route_without_ids_or_query_values(self):
        line = self.logged('GET', f'/v1/cards/{uuid4()}?q=private', 200, 0.0123)
        self.assertEqual(line, {'method': 'GET', 'route': '/v1/cards/:id', 'status': 200, 'ms': 12.3})
        line = self.logged('POST', f'/v1/bots/{uuid4()}/messages', 409, 0.5, {'auth': 0.42})
        self.assertEqual((line['route'], line['status'], line['stages_ms']), ('/v1/bots/:id/messages', 409, {'auth': 0.4}))

    def test_a_long_poll_gives_its_wait(self):
        line = self.logged('GET', '/v1/updates?after=41&wait=20', 200, 20.1)
        self.assertEqual((line['route'], line['wait_s'], line['ms']), ('/v1/updates', 20.0, 20100.0))

    def test_only_the_tunnel_listener_logs(self):
        from coconut.server import APIServer
        service = SimpleNamespace(take_service_restart=lambda: False)
        server = APIServer(('127.0.0.1', 0), service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        with patch('coconut.server.request_timing') as timing:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            connection.request('GET', '/health', headers={'Connection': 'close'})
            response = connection.getresponse()
            self.assertEqual((response.status, json.load(response)), (200, {'ok': True, 'api_version': 1}))
            connection.close()
        timing.assert_not_called()


class MessagePageTests(PhoneReadCase):
    """The page reads the bot's rows and the exchange rows through indexes and returns what the old scan returned."""

    def setUp(self):
        super().setUp()
        self.other = self.write('POST', '/v1/bots', {'name': 'Care'})
        self.quiet = self.write('POST', '/v1/bots', {'name': 'Quiet'})
        self.gone = self.write('POST', '/v1/bots', {'name': 'Gone'})
        bots = [self.bot['id'], self.other['id'], self.quiet['id'], self.gone['id']]
        rows = []
        with self.service.transaction():
            for number in range(60):
                owner, peer = bots[number % 4], bots[(number + 1) % 4]
                row = dict(id=str(uuid4()), bot_id=owner, task_id=None, role='assistant', kind='text', text=str(number),
                           sender_bot_id=None, recipient_bot_ids=[], created_at='2026-10-29T00:00:00Z', streaming=False)
                if number % 5 == 0:
                    row.update(kind='bot_exchange', sender_bot_id=owner, recipient_bot_ids=[peer])
                if number % 7 == 0:
                    row['exchange_recipient_bot_ids'] = [bots[2]]
                if number % 11 == 0:
                    row['_hidden'] = True
                if number % 13 == 0:
                    row['_deleted'] = True
                self.service._put('messages', row)
                rows.append(row)
            # Triggers keep the exchange list when a row changes its audience or leaves.
            self.service._put('messages', dict(rows[5], recipient_bot_ids=[bots[2]]))
            self.service._put('messages', dict(rows[10], kind='text', sender_bot_id=None, recipient_bot_ids=[]))
            self.service._put('messages', dict(rows[3], kind='bot_exchange', sender_bot_id=bots[3], recipient_bot_ids=[bots[1]]))
            self.service.db.execute('DELETE FROM messages WHERE id=?', (rows[15]['id'],))
            gone = self.service._get('bots', self.gone['id'])
            gone['_deleted'] = True
            self.service._put('bots', gone)
        self.bots = bots[:3]

    def test_every_page_matches_the_full_scan(self):
        with self.service._read_view() as view, view._read_transaction():
            for bot_id in self.bots:
                before = None
                while True:
                    page = view._message_page(bot_id, before=before, limit=4)
                    rows = json.loads(view.db.execute(REFERENCE_PAGE, (before, before, bot_id, bot_id, bot_id, bot_id, 5)).fetchone()[0])
                    expected = [view._public(row[1]) for row in reversed(rows[:4])]
                    self.assertEqual(page['messages'], expected)
                    self.assertEqual(page['before'], rows[3][0] if len(rows) > 4 else None)
                    if page['before'] is None:
                        break
                    before = page['before']
                self.assertTrue(expected or bot_id == self.quiet['id'])

    def test_the_page_and_the_confirmed_requests_read_through_indexes(self):
        statements = []
        with self.service._read_view() as view, view._read_transaction():
            view.db.set_trace_callback(statements.append)
            view._message_page(self.quiet['id'])
            view.db.set_trace_callback(None)
            steps = [row[3] for row in view.db.execute('EXPLAIN QUERY PLAN ' + statements[-1])]
            self.assertIn('SEARCH m USING INDEX messages_chat (<expr>=?)', steps)
            self.assertIn('SEARCH a USING PRIMARY KEY (bot_id=?)', steps)
            # No step reads every message, and no step sorts: both parts stop at the newest rows of the bot.
            self.assertFalse([step for step in steps if step.startswith(('SCAN m', 'USE TEMP B-TREE'))], steps)
        statements.clear()
        with patch.object(Service, '_read_view', self.traced(statements)):
            self.service.snapshot(content_only=True, compact_history=True)
        confirmed = next(s for s in statements if 'client_request_id' in s)
        with self.service._read_view() as view:
            plan = ' '.join(row[3] for row in view.db.execute('EXPLAIN QUERY PLAN ' + confirmed))
        self.assertIn('messages_client_request', plan)

    def test_the_confirmed_requests_equal_the_ones_of_the_statement_that_opened_each_message(self):
        with self.service.transaction():
            for number, bot_id in enumerate([self.bot['id'], self.gone['id'], self.other['id']] * 200):
                # A long text, as a reply has it. Two messages share request 7, and a number is no confirmed request.
                request = {7: 'request-0006', 9: 9, 11: None}.get(number, f'request-{999 - number:04}')
                self.service._put('messages', dict(
                    id=str(uuid4()), bot_id=bot_id, task_id=None, role='user', kind='text', text='x' * 20_000,
                    client_request_id=request, created_at='2026-10-29T00:00:00Z', streaming=False))
        self.assertFalse(self.service.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='messages_client_request'").fetchone())
        def read(statement, runs=5):
            times = []
            for _ in range(runs):
                started = time.perf_counter()
                value = json.loads(self.service.db.execute(statement).fetchone()[0])
                times.append(time.perf_counter() - started)
            return value, min(times)
        # The index before this change held the request only, so a read of it opened every message for its bot.
        self.service.db.executescript(
            "DROP INDEX IF EXISTS messages_client_request_bot;\n"
            "CREATE INDEX IF NOT EXISTS messages_client_request ON messages (json_extract(data,'$.client_request_id'))\n"
            "    WHERE json_type(data,'$.client_request_id')='text';")
        expected, before = read(REFERENCE_CONFIRMED)
        # The read view of this change holds the request and the bot of each message, so the read opens none of them.
        self.service.db.executescript(READ_INDEXES)
        confirmed, after = read(CONFIRMED_REQUESTS)
        self.assertEqual(confirmed, expected)
        self.assertEqual(len(confirmed), 398)
        self.assertEqual(self.service.snapshot(content_only=True, compact_history=True)['confirmed_request_ids'], expected)
        # The old statement opened each message, here 8 MB of text. The new one reads the index only.
        self.assertLess(after * 4, before, (before, after))

    def test_a_search_names_the_bots_that_a_read_of_every_message_named(self):
        with self.service.transaction():
            for bot_id, text in [(self.other['id'], 'The STRASSE report'), (self.quiet['id'], 'Maße und Gewichte'),
                                 (self.gone['id'], 'The straße of a removed bot')]:
                self.service._put('messages', dict(id=str(uuid4()), bot_id=bot_id, task_id=None, role='assistant', kind='text',
                                                   text=text, created_at='2026-10-29T00:00:00Z', streaming=False))
            self.service._put('messages', dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=None, role='assistant',
                                               kind='activity', text='straße in an activity', created_at='2026-10-29T00:00:00Z',
                                               streaming=False))
        def reference(query):
            query = query.strip()[:200]
            if not query:
                return {'bot_ids': []}
            with self.service._read_view() as view, view._read_transaction():
                visible = {b['id'] for b in view._all('bots') if not b.get('_deleted')}
                return {'bot_ids': sorted({m['bot_id'] for m in view._all('messages')
                    if m['bot_id'] in visible and not m.get('_hidden') and not m.get('_deleted')
                    and m['kind'] == 'text' and query.casefold() in m['text'].casefold()})}
        for query in ['', '  ', '1', '0', '11', '13', '26', 'straße', 'STRASSE', 'masse', ' report ', 'no such text', 'x' * 300]:
            with self.subTest(query=query):
                self.assertEqual(self.service.search_conversations(query), reference(query))
        self.assertEqual(self.service.search_conversations('strasse')['bot_ids'], [self.other['id']])
        self.assertEqual(self.service.search_conversations('MASSE')['bot_ids'], [self.quiet['id']])
        with self.service._read_view() as view:
            plan = ' '.join(row[3] for row in view.db.execute(
                "EXPLAIN QUERY PLAN SELECT json_extract(data,'$.text') FROM messages WHERE json_extract(data,'$.bot_id')=? "
                "AND json_extract(data,'$.kind')='text' AND NOT COALESCE(json_extract(data,'$._hidden'),0) "
                "AND NOT COALESCE(json_extract(data,'$._deleted'),0)", ('bot',)))
        self.assertIn('messages_chat', plan)

    def test_a_page_does_not_read_more_with_a_longer_exchange_history(self):
        def add_exchanges(count):
            with self.service.transaction():
                for number in range(count):
                    sender = (self.other['id'], self.quiet['id'])[number % 2]
                    self.service._put('messages', dict(
                        id=str(uuid4()), bot_id=sender, task_id=None, role='assistant', kind='bot_exchange', text='x' * 200,
                        sender_bot_id=sender, recipient_bot_ids=[self.bot['id'] if number % 2 else self.other['id']],
                        created_at='2026-10-29T00:00:00Z', streaming=False))
        def steps():
            counted = []
            with self.service._read_view() as view, view._read_transaction():
                view.db.set_progress_handler(lambda: counted.append(1), 100)
                started = time.perf_counter()
                for bot_id in self.bots:
                    view._message_page(bot_id)
                elapsed = time.perf_counter() - started
            return len(counted), elapsed
        add_exchanges(200)
        short, _ = steps()
        add_exchanges(4000)
        long, elapsed = steps()
        # The old page sorted every exchange row for each bot, so its work grew with the history (here 20 times).
        self.assertLess(long, short * 1.5, (short, long))
        self.assertLess(elapsed, 0.5)

    def traced(self, statements):
        original = Service._read_view
        import contextlib
        @contextlib.contextmanager
        def view(service):
            with original(service) as read:
                read.db.set_trace_callback(statements.append)
                yield read
        return view


class WorkProjectionTests(PhoneReadCase):
    def test_the_indexed_projection_equals_a_search_of_each_list(self):
        self.service.provider.supports_native_outcomes = True
        natives, artifacts = {}, []
        created = [self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': f'Work {number}', 'mode': 'queue'})
                   for number in range(6)]
        with self.service.transaction():
            for number, row in enumerate(created):
                task = self.service._get('tasks', row['id'])
                task['_native_outcome_id'] = f't_{number % 4}'
                self.service._put('tasks', task)
                natives[task['id']] = dict(id=f't_{number % 4}', external_id=task['id'], title=f'Native {number}',
                                           status=('running', 'ready', 'done', 'blocked')[number % 4], priority=number,
                                           created_at=number, dependencies=[], block_kind='needs_input')
                artifacts.append(dict(id=f'w{number}', kind='work', task_id=task['id'], bot_id=self.bot['id'],
                                      status='working', title=f'Work {number}', created_at=task['created_at'],
                                      _native_outcome_id=f't_{number % 4}'))
                self.service._put('artifacts', artifacts[-1])
        kanban = SimpleNamespace(tasks=natives, observed_at='2026-10-29T00:00:00Z')
        original = Service._native_outcome_projection
        def searched(service, *args, natives=None, owners=None, **kwargs):
            return original(service, *args, **kwargs)
        with patch.object(self.service, 'kanban', kanban):
            with self.service._read_view() as view, view._read_transaction():
                indexed = view._native_work_view([dict(a) for a in artifacts])
            with patch.object(Service, '_native_outcome_projection', searched), \
                    self.service._read_view() as view, view._read_transaction():
                expected = view._native_work_view([dict(a) for a in artifacts])
        self.assertEqual(indexed, expected)
        self.assertEqual({a['work_state'] for a in indexed}, {'working', 'queued', 'completed', 'waiting'})
        # Outcomes t_0 and t_1 have two tasks each.
        self.assertEqual(sorted(len(a['work_attempts']) for a in indexed), [1, 1, 2, 2, 2, 2])


if __name__ == '__main__':
    unittest.main()
