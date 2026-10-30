import json
import tempfile
import unittest
from uuid import UUID, uuid4, uuid5

from coconut.service import Service, STREAMING_MESSAGE
from test_service import ControlledProvider


class StartSelectionTests(unittest.TestCase):
    """The start fixes old rows through SQLite selections, not a parse of every task and message."""

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

    def restart(self):
        self.service.close()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)

    def plan(self, statement, parameters=()):
        return ' '.join(row[-1] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + statement, parameters))

    def test_a_task_without_its_thread_fields_gets_them(self):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            for key in ('thread_id', 'conversation_bot_id', 'exchange_id'):
                task.pop(key)
            task['workflow'] = 'old'
            self.service.db.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(task), task['id']))
        self.restart()
        task = self.service._get('tasks', self.task['id'])
        self.assertEqual((task['thread_id'], task['conversation_bot_id']), (task['id'], self.bot['id']))
        self.assertIn('exchange_id', task)
        self.assertNotIn('workflow', task)

    def test_a_message_of_another_thread_moves_to_its_task_thread(self):
        with self.service.transaction():
            message = self.service._message(self.bot['id'], 'Done.', task_id=self.task['id'])
            message.update(thread_id='another-thread')
            message.pop('recipient_bot_ids')
            self.service._put('messages', message)
        self.restart()
        saved = self.service._get('messages', message['id'])
        self.assertEqual(saved['thread_id'], self.service._get('tasks', self.task['id'])['thread_id'])
        self.assertEqual(saved['recipient_bot_ids'], [])

    def test_a_delivery_without_its_exchange_message_gets_one(self):
        source = self.service.mutate('POST', f'/v1/bots/{self.other["id"]}/messages', {'text': 'Help', 'mode': 'queue'},
                                     str(uuid4()), 'test')
        delivery = dict(id=str(uuid4()), source_task_id=source['id'], target_bot_id=self.bot['id'], text='The answer.',
                        created_at='2026-10-30T00:00:00Z', receipt=dict(task_id=self.task['id']))
        with self.service.transaction():
            self.service._put('deliveries', delivery)
        self.restart()
        message = self.service._get('messages', str(uuid5(UUID(source['id']), delivery['id'])))
        self.assertEqual((message['kind'], message['sender_bot_id'], message['task_id'], message['text']),
                         ('bot_exchange', self.other['id'], self.task['id'], 'The answer.'))
        # The next start finds the exchange message and adds no second one.
        count = len(self.service._all('messages'))
        self.restart()
        self.assertEqual(len(self.service._all('messages')), count)

    def test_streaming_replies_and_working_turns_end_at_the_start(self):
        with self.service.transaction():
            message = self.service._message(self.bot['id'], 'Partial', task_id=self.task['id'], streaming=True)
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.restart()
        self.assertFalse(self.service._get('messages', message['id'])['streaming'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')

    def test_start_reads_use_indexes(self):
        plans = {
            "SELECT rowid, data FROM native_receipts INDEXED BY native_receipts_executing "
            "WHERE json_extract(data, '$.state')='executing' AND json_extract(data, '$.action.name')='message_agent'":
                'native_receipts_executing',
            'SELECT rowid, data FROM messages INDEXED BY messages_streaming WHERE ' + STREAMING_MESSAGE: 'messages_streaming',
            "SELECT rowid, data FROM contexts WHERE json_extract(data,'$.status')='working'": 'contexts_status',
            "SELECT rowid, data FROM tasks WHERE json_extract(data,'$.status')='working'": 'tasks_status',
            "SELECT d.data FROM deliveries d WHERE NOT EXISTS (SELECT 1 FROM messages m "
            "WHERE json_extract(m.data, '$.task_id') = json_extract(d.data,'$.receipt.task_id') "
            "AND json_extract(m.data,'$.kind')='bot_exchange' AND json_extract(m.data,'$.sender_bot_id') IS "
            "(SELECT json_extract(data,'$.bot_id') FROM tasks WHERE id = json_extract(d.data,'$.source_task_id'))) "
            "ORDER BY d.rowid": 'SEARCH m USING INDEX messages_task',
        }
        for statement, index in plans.items():
            with self.subTest(index=index):
                self.assertIn(index, self.plan(statement))

    def test_the_streaming_index_holds_only_streaming_messages(self):
        with self.service.transaction():
            streaming = self.service._message(self.bot['id'], 'Partial', task_id=self.task['id'], streaming=True)
            done = self.service._message(self.bot['id'], 'Done', task_id=self.task['id'])
        rows = {row[0] for row in self.service.db.execute(
            'SELECT id FROM messages INDEXED BY messages_streaming WHERE ' + STREAMING_MESSAGE)}
        self.assertIn(streaming['id'], rows)
        self.assertNotIn(done['id'], rows)


if __name__ == '__main__':
    unittest.main()
