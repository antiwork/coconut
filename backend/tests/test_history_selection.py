import json
import sqlite3
import unittest
from unittest.mock import patch

from coconut.service import Service


class HistorySelectionTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(Service)
        self.service.db = sqlite3.connect(':memory:')
        self.addCleanup(self.service.db.close)
        self.service.db.executescript('''
            CREATE TABLE messages (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE message_audience (bot_id TEXT, message_id TEXT, PRIMARY KEY(bot_id, message_id));
            CREATE INDEX messages_task ON messages (json_extract(data, '$.task_id'));
        ''')

    def add(self, identity, task, audience=('bot',), **fields):
        data = dict(id=identity, task_id=task, role='user', kind='text', text=identity) | fields
        self.service.db.execute('INSERT INTO messages VALUES (?,?)', (identity, json.dumps(data)))
        self.service.db.executemany('INSERT INTO message_audience VALUES (?,?)', [(bot, identity) for bot in audience])

    def test_selection_keeps_visibility_shared_messages_and_order(self):
        self.add('earlier', 'old')
        self.add('shared', 'peer', ('peer', 'bot'))
        self.add('current', 'task')
        self.add('hidden', 'task', _hidden=True)
        self.add('deleted', 'task', _deleted=True)
        self.add('private', 'task', ('peer',))
        self.add('latest', 'task', _private='Not public')
        shared = ['shared', 'current', 'private', 'missing']
        full = self.service._history('bot')
        expected = [m for m in full if m['task_id'] == 'task' or m['id'] in shared]
        selected = self.service._history('bot', task_id='task', shared_message_ids=shared)
        self.assertEqual(selected, expected)
        self.assertEqual([m['id'] for m in selected], ['shared', 'current', 'latest'])
        self.assertNotIn('_private', selected[-1])
        self.assertEqual(self.service._history('bot'), full)

    def test_current_input_does_not_decode_unrelated_history(self):
        for index in range(500):
            self.add(str(index), 'old', text='Large previous conversation. ' * 100)
        self.add('current', 'task')
        decode = json.loads
        sizes = []
        def load(value, *args, **kwargs):
            sizes.append(len(value))
            return decode(value, *args, **kwargs)
        with patch('coconut.service.json.loads', side_effect=load):
            selected = self.service._history('bot', task_id='task')
        self.assertEqual([m['id'] for m in selected], ['current'])
        self.assertLess(max(sizes), 1024)
