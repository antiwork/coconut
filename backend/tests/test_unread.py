import tempfile
import unittest
from uuid import uuid4

from coconut.service import Service
from coconut import unread
from test_push import pair_phone
from test_service import ControlledProvider


class UnreadCountTests(unittest.TestCase):
    """The unread count of an agent chat counts the replies after the read mark, not the interim messages."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.device = pair_phone(self.service)
        self.bot = self.write('POST', '/v1/bots', {'name': 'Ada'})
        self.task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text='Check the test', mode='queue'))
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), self.device)

    def bot_row(self):
        return self.service._get('bots', self.bot['id'])

    def reply(self, text, **options):
        with self.service.transaction():
            return self.service._message(self.bot['id'], text, task_id=self.task['id'], **options)

    def test_interim_messages_do_not_count(self):
        self.service.emit(self.task['id'], 'interim_message', {'text': 'I will check the failing test.'})
        self.service.emit(self.task['id'], 'interim_message', {'text': 'The test file loads.'})
        self.assertEqual(self.bot_row()['unread_count'], 0)
        self.reply('The test passes.')
        self.assertEqual(self.bot_row()['unread_count'], 1)

    def test_a_sealed_stream_stops_counting(self):
        self.service.emit(self.task['id'], 'text_delta', {'text': 'Looking'})
        self.assertEqual(self.bot_row()['unread_count'], 1)
        from coconut import interim_messages
        with self.service.transaction():
            interim_messages.seal(self.service, self.service._get('tasks', self.task['id']))
        self.assertEqual(self.bot_row()['unread_count'], 0)

    def test_reading_sets_the_mark_and_the_next_reply_counts_again(self):
        first = self.reply('One')
        self.reply('Two')
        self.assertEqual(self.bot_row()['unread_count'], 2)
        self.write('POST', f'/v1/bots/{self.bot["id"]}/read', {})
        row = self.bot_row()
        self.assertEqual(row['unread_count'], 0)
        self.assertNotEqual(row['last_read_message_id'], first['id'])
        mark = row['last_read_message_id']
        third = self.reply('Three')
        row = self.bot_row()
        self.assertEqual((row['unread_count'], row['last_read_message_id']), (1, mark))
        self.assertNotEqual(third['id'], mark)

    def test_a_new_agent_counts_its_first_reply(self):
        self.reply('Hello')
        self.assertEqual(self.bot_row()['unread_count'], 1)

    def test_writing_in_the_chat_reads_it(self):
        self.reply('One')
        self.assertEqual(self.bot_row()['unread_count'], 1)
        self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text='Thanks', mode='queue'))
        self.assertEqual(self.bot_row()['unread_count'], 0)

    def test_a_withdrawn_reply_stops_counting(self):
        self.service.emit(self.task['id'], 'text_delta', {'text': 'Draft'})
        self.assertEqual(self.bot_row()['unread_count'], 1)
        with self.service.transaction():
            self.service._withdraw_stream(self.task['id'])
        self.assertEqual(self.bot_row()['unread_count'], 0)

    def test_a_chat_from_before_marks_keeps_at_most_its_old_count(self):
        for text in ('One', 'Two', 'Three'):
            self.reply(text)
        with self.service.transaction():
            bot = self.bot_row()
            bot.pop('last_read_message_id')
            bot['unread_count'] = 2
            unread.recount(self.service.db, bot)
            self.assertEqual(bot['unread_count'], 2)
            bot.pop('last_read_message_id')
            bot['unread_count'] = 0
            unread.recount(self.service.db, bot)
            self.assertEqual(bot['unread_count'], 0)

    def test_a_chat_from_before_marks_counts_a_new_reply(self):
        self.reply('One')
        with self.service.transaction():
            bot = self.bot_row()
            bot.pop('last_read_message_id')
            bot['unread_count'] = 0
            self.service._put('bots', bot)
        self.reply('Two')
        self.assertEqual(self.bot_row()['unread_count'], 1)


if __name__ == '__main__':
    unittest.main()
