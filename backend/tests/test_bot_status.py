"""The bot status comes from two indexed queries, and its event goes out only for a new status."""
import json
from pathlib import Path
import random
import tempfile
import unittest
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
        root = Path(self.temp.name)
        self.service = Service(root / 'coconut', FixtureProvider(root / 'coconut'), start_workers=False)
        self.addCleanup(self.service.close)
        self.bots = [self.service.mutate('POST', '/v1/bots', {'name': name, 'role': ''}, str(uuid4())) for name in ('One', 'Two')]

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

    def test_the_queries_use_indexes(self):
        plans = [row[3] for row in self.service.db.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT json_extract(data,'$.status') FROM tasks WHERE json_extract(data,'$.bot_id')=? "
            "AND json_extract(data,'$.status') IN ('working','needs_you','scheduled')", ('x',))]
        self.assertTrue(any('tasks_bot_status' in plan for plan in plans), plans)


if __name__ == '__main__':
    unittest.main()
