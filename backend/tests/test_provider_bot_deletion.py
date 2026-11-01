import unittest
from uuid import uuid4

from coconut.bot_changes import bot_deletion_receipt
from coconut.card_tools import BOT_DELETION_SCHEMA
import test_provider


class BotDeletionProviderTests(unittest.TestCase):
    setUp = test_provider.ProviderTests.setUp
    tearDown = test_provider.ProviderTests.tearDown
    configure_peer = test_provider.ProviderTests.configure_peer

    def request(self):
        peer = self.configure_peer()
        source = {'id': str(uuid4()), 'text': 'Delete Lin.'}
        self.task['_bot_deletion_sources'] = [source]
        self.task['_deletable_bots'] = [peer | {'archived': False}]
        payload = dict(source_message_id=source['id'], target_bot_id=peer['id'], expected_name=peer['name'])
        receipt = dict(status='deleted', source_message_id=source['id'], bot_id=peer['id'],
                       profile_id=peer['id'], name=peer['name'], cancelled_task_ids=[str(uuid4())])
        return payload, receipt

    def test_schema_requires_exact_source_id_target_id_and_current_name(self):
        payload, unused = self.request()
        self.assertEqual(set(BOT_DELETION_SCHEMA['parameters']['required']), set(payload))
        self.assertEqual(set(BOT_DELETION_SCHEMA['parameters']['properties']), set(payload))

    def test_receipt_rejects_wrong_identity_name_source_or_cancelled_tasks(self):
        payload, receipt = self.request()
        for changes in ({'status': 'queued'}, {'source_message_id': 'foreign'}, {'bot_id': str(uuid4())},
                        {'profile_id': str(uuid4())}, {'name': 'lin'}, {'cancelled_task_ids': None},
                        {'cancelled_task_ids': ['invalid']}, {'cancelled_task_ids': receipt['cancelled_task_ids'] * 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bot_deletion_receipt(receipt | changes, payload, self.bot)
        self.assertEqual(bot_deletion_receipt(receipt | {'private': 'SECRET'}, payload, self.bot), receipt)
        with self.assertRaises(ValueError):
            bot_deletion_receipt(receipt, payload, {'id': receipt['bot_id']})


if __name__ == '__main__':
    unittest.main()
