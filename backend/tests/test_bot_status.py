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

    def test_the_event_goes_out_only_when_the_public_row_changes(self):
        bot = self.bots[0]
        with self.service.transaction():
            self.service._refresh_bot(bot['id'])
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
        # A new last message or unread count is a change too: the phone reads the bots again for a bot event.
        with self.service.transaction():
            row = self.service._get('bots', bot['id'])
            row['last_message'] = 'A new streamed reply'
            self.service._put('bots', row)
            self.service._refresh_bot(bot['id'])
        self.assertEqual(len(self.events(bot['id'])), len(before) + 2)

    def test_a_rollback_forgets_only_the_bots_that_it_published(self):
        first, second = (bot['id'] for bot in self.bots)
        for bot_id in (first, second):
            with self.service.transaction():
                self.service._refresh_bot(bot_id)
        before = {bot_id: len(self.events(bot_id)) for bot_id in (first, second)}
        with self.assertRaises(RuntimeError):
            with self.service.transaction():
                self.service._put('tasks', dict(id=str(uuid4()), bot_id=first, status='working'))
                self.service._refresh_bot(first)
                self.service._refresh_bot(second)
                raise RuntimeError('The transaction fails.')
        # The second bot published nothing in that transaction, so its refresh stays quiet.
        with self.service.transaction():
            self.service._refresh_bot(second)
        self.assertEqual(len(self.events(second)), before[second])

    def test_a_settings_update_that_published_the_row_does_not_publish_it_twice(self):
        bot = self.bots[0]
        with self.service.transaction():
            self.service._refresh_bot(bot['id'])
        before = len(self.events(bot['id']))
        self.service.mutate('PATCH', f"/v1/bots/{bot['id']}", {'name': 'Renamed'}, str(uuid4()))
        after_patch = len(self.events(bot['id']))
        with self.service.transaction():
            self.service._refresh_bot(bot['id'])
        self.assertEqual(after_patch, before + 1)
        self.assertEqual(len(self.events(bot['id'])), after_patch)

    def test_a_rolled_back_event_is_published_again(self):
        bot = self.bots[0]
        with self.service.transaction():
            self.service._refresh_bot(bot['id'])
        before = len(self.events(bot['id']))
        with self.assertRaises(RuntimeError):
            with self.service.transaction():
                self.service._put('tasks', dict(id=str(uuid4()), bot_id=bot['id'], status='working'))
                self.service._refresh_bot(bot['id'])
                raise RuntimeError('The transaction fails after the refresh.')
        self.assertEqual(len(self.events(bot['id'])), before)
        with self.service.transaction():
            self.service._put('tasks', dict(id=str(uuid4()), bot_id=bot['id'], status='working'))
            self.service._refresh_bot(bot['id'])
        self.assertEqual(self.events(bot['id'])[before:], ['working'])

    def test_the_queries_use_indexes_and_no_sort(self):
        from coconut import service
        # The statements that _refresh_bot runs, not copies of them.
        queries = {'states': (service.BOT_TASK_STATES, 'tasks_bot_status'), 'latest': (service.BOT_LATEST_TASK, 'tasks_bot'),
                   'answering': (service.BOT_WORKING_CONTEXT, 'contexts_status')}
        for name, (statement, index) in queries.items():
            plan = ' | '.join(row[3] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + statement, ('x',)))
            self.assertIn(index, plan, (name, plan))
            self.assertNotIn('TEMP B-TREE', plan, (name, plan))

    def test_a_restart_publishes_one_event_for_each_bot(self):
        # scripts/migrate.py verify_started expects this: one bot.updated for each bot after a start, and nothing else.
        cursor = self.service.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        self.close(self.service)
        reopened = self.open()
        rows = reopened.db.execute('SELECT type, bot_id FROM events WHERE seq>?', (cursor,)).fetchall()
        self.assertEqual(sorted(rows), sorted(('bot.updated', bot['id']) for bot in self.bots))


if __name__ == '__main__':
    unittest.main()
