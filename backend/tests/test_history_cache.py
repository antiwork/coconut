import json
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import Service
from test_service import ControlledProvider


class HistoryCacheTests(unittest.TestCase):
    """A turn reads the kept history of its bot. It is the same list as a read of the table."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'test')
        self.other = self.service.mutate('POST', '/v1/bots', {'name': 'Helper'}, str(uuid4()), 'test')
        self.task = self.service.mutate('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Check files', 'mode': 'queue'},
                                        str(uuid4()), 'test')

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def stored(self, db, bot_id):
        row = db.execute(
            "SELECT json_group_array(json(data)) FROM (SELECT m.data FROM message_audience a JOIN messages m "
            "ON m.id=a.message_id WHERE a.bot_id=? AND COALESCE(json_extract(m.data, '$._hidden'),0)=0 "
            "AND COALESCE(json_extract(m.data, '$._deleted'),0)=0 ORDER BY m.rowid)", (bot_id,)).fetchone()
        return [self.service._public(m) for m in json.loads(row[0])]

    def check(self):
        for bot in (self.bot, self.other):
            with self.service.lock:
                self.assertEqual(self.service._history(bot['id']), self.stored(self.service.db, bot['id']))
            with self.service._read_view() as view, view._read_transaction():
                self.assertEqual(view._history(bot['id']), self.stored(view.db, bot['id']))

    def message(self, text, **fields):
        with self.service.transaction():
            message = self.service._message(self.bot['id'], text, task_id=self.task['id'])
            message.update(fields)
            self.service._put('messages', message)
        return message

    def test_the_kept_history_follows_each_kind_of_change(self):
        self.check()
        first = self.message('First')
        second = self.message('Second')
        self.check()
        with self.service.transaction():
            first['text'] = 'First, edited'
            self.service._put('messages', first)
            second['_hidden'] = True
            self.service._put('messages', second)
        self.check()
        with self.service.transaction():
            second.pop('_hidden')
            self.service._put('messages', second)
            first['recipient_bot_ids'] = [self.other['id']]
            self.service._put('messages', first)
        self.check()
        with self.service.transaction():
            self.service.db.execute('DELETE FROM messages WHERE id=?', (second['id'],))
        self.check()
        history = [m['id'] for m in self.service._history(self.bot['id'])]
        self.assertIn(first['id'], history)
        self.assertNotIn(second['id'], history)

    def test_a_later_turn_reads_only_the_changed_messages(self):
        self.message('First')
        with self.service.lock:
            self.service._history(self.bot['id'])
        self.message('Second')
        with self.service.lock, patch.object(Service, '_read_history', side_effect=AssertionError('A full read.')):
            self.assertEqual(self.service._history(self.bot['id'])[-1]['text'], 'Second')

    def test_each_caller_gets_its_own_messages(self):
        self.message('First')
        with self.service.lock:
            history = self.service._history(self.bot['id'])
            history[-1]['text'] = 'Changed by a caller'
            self.assertNotEqual(self.service._history(self.bot['id'])[-1]['text'], 'Changed by a caller')

    def test_open_writes_read_the_table_and_share_nothing(self):
        with self.service.transaction():
            message = self.service._message(self.bot['id'], 'Not committed yet', task_id=self.task['id'])
            self.assertIn(message['id'], [m['id'] for m in self.service._history(self.bot['id'])])
            self.assertEqual(self.service._history_states.states, {})
        self.check()

    def test_a_renumbering_vacuum_starts_a_new_generation(self):
        self.check()
        self.service._forget_kept_history()
        self.assertEqual(self.service._history_states.states, {})
        self.check()


if __name__ == '__main__':
    unittest.main()
