import tempfile
import unittest
from pathlib import Path

from coconut.providers import FixtureProvider
from coconut.service import Service


class ReceiptLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = Service(self.root, FixtureProvider(self.root), start_workers=False)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)

    def test_uncertain_receipt_matches_task_turn_and_current_state(self):
        service = self.service
        record = dict(id='receipt', task_id='task', state='executing')
        service._put('native_receipts', record)
        self.assertTrue(service._has_uncertain_receipt('task', None))
        self.assertFalse(service._has_uncertain_receipt('other', None))
        self.assertFalse(service._has_uncertain_receipt('task', 'turn'))
        record['turn_id'] = 'turn'
        service._put('native_receipts', record)
        self.assertFalse(service._has_uncertain_receipt('task', None))
        self.assertTrue(service._has_uncertain_receipt('task', 'turn'))
        record['state'] = 'completed'
        service._put('native_receipts', record)
        self.assertFalse(service._has_uncertain_receipt('task', 'turn'))

    def test_completed_history_does_not_add_work_to_a_receipt_lookup(self):
        service = self.service
        with service.transaction():
            for index in range(5000):
                service._put('native_receipts', dict(id=str(index), task_id=str(index),
                    turn_id='old', state='completed', result='Saved tool output.'))
        # Bound database work without depending on the speed of the CI machine.
        service.db.set_progress_handler(lambda: 1, 2000)
        try:
            self.assertFalse(service._has_uncertain_receipt('current', 'current'))
        finally:
            service.db.set_progress_handler(None, 0)

    def test_history_reads_only_the_bots_messages_and_tracks_audience_changes(self):
        service = self.service
        with service.transaction():
            for index in range(5000):
                service._put('messages', dict(id=str(index), sender_bot_id='other', text='Unrelated.'))
            service._put('messages', dict(id='first', sender_bot_id='desk', recipient_bot_ids=['desk', 'dev'], text='One'))
            service._put('messages', dict(id='second', sender_bot_id=None, recipient_bot_ids=['desk'], text='Two'))
            service._put('messages', dict(id='hidden', recipient_bot_ids=['desk'], _hidden=True, text='Hidden'))
        service.db.set_progress_handler(lambda: 1, 2000)
        try:
            self.assertEqual([m['text'] for m in service._history('desk')], ['One', 'Two'])
        finally:
            service.db.set_progress_handler(None, 0)
        service._put('messages', dict(id='first', sender_bot_id='dev', recipient_bot_ids=[], text='Moved'))
        self.assertEqual([m['text'] for m in service._history('desk')], ['Two'])
        service._put('messages', dict(id='hidden', recipient_bot_ids=['desk'], _hidden=False, text='Visible'))
        service.db.execute("DELETE FROM messages WHERE id='second'")
        self.assertEqual([m['text'] for m in service._history('desk')], ['Visible'])
        import json
        service.db.execute('INSERT OR REPLACE INTO messages VALUES (?,?)',
            ('hidden', json.dumps(dict(id='hidden', recipient_bot_ids=['dev'], text='Replaced'))))
        self.assertEqual(service._history('desk'), [])
        self.assertEqual([m['text'] for m in service._history('dev')], ['Moved', 'Replaced'])

    def test_history_index_migrates_existing_messages_once(self):
        service = self.service
        for name in ('insert', 'update', 'delete'):
            service.db.execute('DROP TRIGGER message_audience_' + name)
        service.db.execute('DROP TABLE message_audience')
        service.db.execute("DELETE FROM settings WHERE key='message_audience_indexed'")
        service._put('messages', dict(id='old', sender_bot_id='desk', recipient_bot_ids=['dev'], text='Before upgrade'))
        service._index_message_audience()
        self.assertEqual([m['text'] for m in service._history('dev')], ['Before upgrade'])
        service._index_message_audience()
        self.assertEqual(len(service._history('desk')), 1)


if __name__ == '__main__':
    unittest.main()
