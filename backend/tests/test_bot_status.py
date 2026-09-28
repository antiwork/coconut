"""The bot status comes from two indexed queries, and its event goes out only for a new status."""
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service

TASK_STATES = ('queued', 'working', 'needs_you', 'scheduled', 'failed', 'completed', 'cancelled', 'paused')
CONTEXT_STATES = ('queued', 'working', 'completed', 'failed', 'cancelled')


def reference(tasks, contexts, bot_id):
    """The rule of the earlier implementation, which read every task and context."""
    mine = [t for t in tasks if t['bot_id'] == bot_id]
    states = {t['status'] for t in mine if t['status'] in ('working', 'needs_you', 'scheduled')}
    if mine and mine[-1]['status'] == 'failed':
        states.add('failed')
    answering = any(c['bot_id'] == bot_id and c['status'] == 'working' for c in contexts)
    return 'working' if answering else next((s for s in ('needs_you', 'working', 'scheduled', 'failed') if s in states), 'idle')


class BotStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'coconut'
        self.service = self.open()
        self.bots = [self.service.mutate('POST', '/v1/bots', {'name': name, 'role': ''}, str(uuid4())) for name in ('One', 'Two')]

    def open(self):
        service = Service(self.root, FixtureProvider(self.root), start_workers=False)
        self.addCleanup(self.close, service)
        return service

    @staticmethod
    def close(service):
        if not getattr(service, 'closed_by_test', False):
            service.closed_by_test = True
            service.close()

    def events(self, bot_id):
        rows = self.service.db.execute("SELECT data FROM events WHERE type='bot.updated' AND bot_id=? ORDER BY seq", (bot_id,)).fetchall()
        return [json.loads(row[0])['status'] for row in rows]

    def test_the_status_matches_the_earlier_rule_for_random_states(self):
        generator = random.Random(7)
        for round_number in range(120):
            with self.service.transaction():
                for table in ('tasks', 'contexts'):
                    self.service.db.execute(f'DELETE FROM {table}')
                tasks = [dict(id=str(uuid4()), bot_id=generator.choice([b['id'] for b in self.bots] + ['other']),
                              status=generator.choice(TASK_STATES)) for _ in range(generator.randint(0, 6))]
                contexts = [dict(id=str(uuid4()), bot_id=generator.choice([b['id'] for b in self.bots]),
                                 status=generator.choice(CONTEXT_STATES)) for _ in range(generator.randint(0, 3))]
                for task in tasks:
                    self.service._put('tasks', task)
                for context in contexts:
                    self.service._put('contexts', context)
                for bot in self.bots:
                    self.service._refresh_bot(bot['id'])
                    self.assertEqual(self.service._get('bots', bot['id'])['status'], reference(tasks, contexts, bot['id']),
                                     (round_number, tasks, contexts))
        with self.service.transaction():
            for table in ('tasks', 'contexts'):
                self.service.db.execute(f'DELETE FROM {table}')

    def test_the_event_goes_out_only_for_a_new_status(self):
        bot = self.bots[0]
        before = self.events(bot['id'])
        for _ in range(3):
            with self.service.transaction():
                self.service._refresh_bot(bot['id'])
        self.assertEqual(self.events(bot['id']), before)
        with self.service.transaction():
            self.service._put('tasks', dict(id=str(uuid4()), bot_id=bot['id'], status='working'))
            self.service._refresh_bot(bot['id'])
            self.service._refresh_bot(bot['id'])
        self.assertEqual(self.events(bot['id']), before + ['working'])

    def test_the_queries_use_indexes_and_no_sort(self):
        queries = {
            'states': ("SELECT DISTINCT json_extract(data,'$.status') FROM tasks WHERE json_extract(data,'$.bot_id')=? "
                       "AND json_extract(data,'$.status') IN ('working','needs_you','scheduled')", 'tasks_bot_status'),
            'latest': ("SELECT json_extract(data,'$.status') FROM tasks WHERE json_extract(data,'$.bot_id')=? "
                       'ORDER BY rowid DESC LIMIT 1', 'tasks_bot'),
            'answering': ("SELECT 1 FROM contexts WHERE json_extract(data,'$.status')='working' "
                          "AND json_extract(data,'$.bot_id')=? LIMIT 1", 'contexts_status'),
        }
        for name, (statement, index) in queries.items():
            plan = ' | '.join(row[3] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + statement, ('x',)))
            self.assertIn(index, plan, (name, plan))
            self.assertNotIn('TEMP B-TREE', plan, (name, plan))

    def test_a_restart_publishes_nothing_for_a_bot_whose_status_did_not_change(self):
        cursor = self.service.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        self.close(self.service)
        reopened = self.open()
        self.assertEqual(reopened.db.execute('SELECT COUNT(*) FROM events WHERE seq>?', (cursor,)).fetchone()[0], 0)


    def verify_started(self, extra_events):
        """Run migrate.verify_started on this store after it wrote `extra_events`. Only the event rule is under test."""
        import collections
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
        import migrate
        self.close(self.service)
        path = self.root / 'coconut.sqlite3'
        connection = sqlite3.connect(path)
        cursor = connection.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        prefix = sorted(connection.execute('SELECT * FROM events WHERE seq<=?', (cursor,)).fetchall(), key=repr)
        for kind, bot_id in extra_events:
            connection.execute('INSERT INTO events(type,bot_id,task_id,data,created_at) VALUES (?,?,?,?,?)', (kind, bot_id, None, '{}', 'now'))
        connection.commit()
        connection.close()
        state = dict(profiles=[[bot['id'], bot['id'], bot['session_id']] for bot in self.bots], tasks=[], decisions=[],
                     counts=collections.defaultdict(int), hashes=collections.defaultdict(str, events=migrate.deploy.sha256(migrate.deploy.canonical(prefix))))
        with patch.object(migrate, 'metadata', return_value=state):
            return migrate.verify_started(path, dict(state, cursor=cursor))

    def test_verify_started_accepts_none_or_one_event_for_each_bot(self):
        first, second = (bot['id'] for bot in self.bots)
        self.verify_started([])
        self.verify_started([('bot.updated', first)])
        self.verify_started([('bot.updated', first), ('bot.updated', second)])

    def test_verify_started_rejects_other_work(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
        import migrate
        first = self.bots[0]['id']
        # A second event for one bot, and any other kind of event, are unexpected work.
        self.setUp()
        with self.assertRaises(migrate.MigrationError):
            self.verify_started([('bot.updated', first), ('bot.updated', first)])
        self.setUp()
        with self.assertRaises(migrate.MigrationError):
            self.verify_started([('task.created', first)])


if __name__ == '__main__':
    unittest.main()
