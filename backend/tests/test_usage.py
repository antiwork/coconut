"""The usage of an agent comes from the session store of its Hermes profile, by day."""
from datetime import datetime
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from uuid import uuid4

from coconut import usage
from coconut.providers import FixtureProvider
from coconut.server import TunnelServer
from coconut.service import Service

PEER = 'ab' * 32
SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, billing_provider TEXT, started_at REAL NOT NULL,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0, estimated_cost_usd REAL, actual_cost_usd REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, timestamp REAL NOT NULL);
CREATE TABLE session_model_usage (session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '', input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0, estimated_cost_usd REAL NOT NULL DEFAULT 0, actual_cost_usd REAL NOT NULL DEFAULT 0);
"""


def at(day, hour=12, minute=0, second=0):
    return datetime(2026, 11, day, hour, minute, second).timestamp()


NOW = at(16, 18)


class Store:
    def __init__(self, path, schema=SCHEMA):
        self.db = sqlite3.connect(path)
        self.db.executescript(schema)

    def session(self, session_id, started, **fields):
        fields = dict(id=session_id, source='coconut', model='opus', billing_provider='anthropic', started_at=started) | fields
        self.db.execute(f'INSERT INTO sessions ({", ".join(fields)}) VALUES ({", ".join("?" * len(fields))})', list(fields.values()))

    def model(self, session_id, model, **fields):
        fields = dict(session_id=session_id, model=model, billing_provider='anthropic') | fields
        self.db.execute(f'INSERT INTO session_model_usage ({", ".join(fields)}) VALUES ({", ".join("?" * len(fields))})', list(fields.values()))

    def messages(self, session_id, *rows):
        self.db.executemany('INSERT INTO messages (session_id, role, timestamp) VALUES (?, ?, ?)',
                            [(session_id, role, timestamp) for role, timestamp in rows])

    def close(self):
        self.db.commit()
        self.db.close()


class ReadUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'

    def read(self, days=3):
        report = usage.read_usage(self.path, days, NOW)
        return report, {day['day']: day for day in report['days']}

    def test_a_day_counts_its_turns_calls_tools_and_working_time(self):
        store = Store(self.path)
        store.session('chat', at(15))
        store.messages('chat',
                       ('user', at(15, 9)), ('assistant', at(15, 9, 0, 10)), ('tool', at(15, 9, 0, 40)), ('assistant', at(15, 9, 1)),
                       # A message of a routine hours later starts no work time: the gap is a pause.
                       ('assistant', at(15, 15)),
                       ('user', at(16, 8)), ('assistant', at(16, 8, 0, 30)))
        store.close()
        report, days = self.read()
        self.assertEqual([day['day'] for day in report['days']], ['2026-11-14', '2026-11-15', '2026-11-16'])
        self.assertEqual(days['2026-11-14'], usage._empty_day(datetime(2026, 11, 14).date()) | dict(cost_usd=0))
        self.assertEqual((days['2026-11-15']['turns'], days['2026-11-15']['model_calls'], days['2026-11-15']['tool_calls']), (1, 3, 1))
        self.assertEqual(days['2026-11-15']['working_seconds'], 60)
        self.assertEqual((days['2026-11-16']['turns'], days['2026-11-16']['working_seconds']), (1, 30))

    def test_a_day_gets_its_share_of_the_tokens_and_the_cost_of_a_session(self):
        store = Store(self.path)
        store.session('chat', at(10), input_tokens=1, output_tokens=1, estimated_cost_usd=99)
        # The per-model rows replace the totals of the session: they also have the calls of a side task.
        store.model('chat', 'opus', input_tokens=300, output_tokens=40, cache_read_tokens=4000, estimated_cost_usd=3)
        store.model('chat', 'haiku', task='background_review', input_tokens=100, output_tokens=0, estimated_cost_usd=1)
        # One call of four is before the window, one on the 15th, two today.
        store.messages('chat', ('assistant', at(10)), ('assistant', at(15)), ('assistant', at(16, 9)), ('assistant', at(16, 10)))
        # A session without the per-model rows and without messages counts on the day when it started.
        store.session('routine', at(15), source='cron', input_tokens=50, output_tokens=5, actual_cost_usd=0.5, estimated_cost_usd=0.2)
        store.close()
        report, days = self.read()
        self.assertEqual((days['2026-11-15']['input_tokens'], days['2026-11-15']['output_tokens']), (150, 15))
        self.assertEqual(days['2026-11-15']['cache_read_tokens'], 1000)
        self.assertEqual(days['2026-11-15']['cost_usd'], 1.5)
        self.assertEqual((days['2026-11-16']['input_tokens'], days['2026-11-16']['cost_usd']), (200, 2))
        self.assertEqual([(model['model'], model['input_tokens'], model['cost_usd']) for model in report['models']],
                         [('opus', 275, 2.75), ('haiku', 75, 0.75)])
        # One cost is an estimate of Hermes, so the sum is an estimate.
        self.assertEqual((report['cost_known'], report['cost_estimated']), (True, True))

    def test_a_store_without_a_price_knows_no_cost(self):
        store = Store(self.path)
        store.session('chat', at(16), input_tokens=10, output_tokens=2)
        store.messages('chat', ('user', at(16)), ('assistant', at(16, 12, 0, 5)))
        store.close()
        report, days = self.read()
        self.assertEqual((days['2026-11-16']['input_tokens'], days['2026-11-16']['cost_usd']), (10, 0))
        self.assertEqual((report['cost_known'], report['cost_estimated']), (False, False))

    def test_an_older_store_and_a_missing_store_give_empty_days(self):
        for prepare in (lambda: None, lambda: self.path.write_bytes(b''),
                        lambda: Store(self.path, 'CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL);').close()):
            prepare()
            report, days = self.read(2)
            self.assertEqual([day['turns'] for day in report['days']], [0, 0])
            self.assertEqual((report['models'], report['cost_known']), ([], False))
        # A store without the token columns still gives its turns.
        self.path.unlink()
        store = Store(self.path, 'CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL);'
                                 'CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL);')
        store.messages('chat', ('user', at(16)))
        store.close()
        self.assertEqual(self.read(1)[0]['days'][0]['turns'], 1)

    def test_the_read_does_not_change_the_store(self):
        store = Store(self.path)
        store.session('chat', at(16))
        store.close()
        before = self.path.read_bytes()
        self.read()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in Path(self.temp.name).iterdir()), ['state.db'])


class ServiceUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'phone')
        self.quiet = self.service.mutate('POST', '/v1/bots', {'name': 'Quiet'}, str(uuid4()), 'phone')
        home = self.service.profiles.home(self.bot)
        home.mkdir(parents=True, exist_ok=True)
        (home / 'state.db').unlink(missing_ok=True)
        store = Store(home / 'state.db')
        store.session('chat', time.time(), input_tokens=12, output_tokens=3, estimated_cost_usd=0.25)
        store.messages('chat', ('user', time.time() - 20), ('assistant', time.time() - 5))
        store.close()

    def test_each_agent_has_its_days(self):
        report = self.service.usage(2)
        self.assertEqual(report['days'], 2)
        self.assertEqual(set(report['agents']), {self.bot['id'], self.quiet['id']})
        today = report['agents'][self.bot['id']]['days'][-1]
        self.assertEqual(today['day'], report['today'])
        self.assertEqual((today['turns'], today['model_calls'], today['input_tokens'], today['cost_usd']), (1, 1, 12, 0.25))
        self.assertEqual(sum(day['turns'] for day in report['agents'][self.quiet['id']]['days']), 0)
        # The days have a bound, and a repeated read within a minute answers from memory.
        self.assertEqual(self.service.usage(100000)['days'], usage.MAX_DAYS)
        self.assertIs(self.service.usage(2), self.service.usage(2))

    def test_the_phone_reads_the_route(self):
        token = self.service.pair({'code': self.service.pairing_code(tunnel=True)}, PEER)['token']
        server = TunnelServer(0, self.service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        headers = {'Authorization': 'Bearer ' + token, 'X-Coconut-Peer': PEER}
        for path, status in (('/v1/usage?days=7', 200), ('/v1/usage?days=week', 400)):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            self.addCleanup(connection.close)
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            body = json.load(response)
            self.assertEqual(response.status, status)
        self.assertEqual(body, {'error': 'days must contain a number.'})


if __name__ == '__main__':
    unittest.main()
