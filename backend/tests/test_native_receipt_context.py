import copy
import json
import unittest

from coconut.native_receipt_context import CHUNK_SIZE, PAGE_SIZE, read_receipts, receipt_prompt
from turn_text import turn_input


def receipts(count=305) -> list[dict]:
    return [dict(id='receipt-' + str(i), state='completed', action_hash='hash-' + str(i),
                 action={'name': 'terminal', 'arguments': {'command': 'exact command ' + str(i)}},
                 result=json.dumps({'output': 'payload-' + 'x' * 6000, 'exit_code': 0})) for i in range(count)]


class ReceiptContextTests(unittest.TestCase):
    def test_start_and_resume_text_index_the_receipts_and_preserve_exact_approval(self):
        saved = receipts()
        decision: dict = dict(status='approved', action_type='native_approval',
                        action={'name': 'terminal', 'arguments': {'command': 'ONLY exact command'}},
                        answer='Approve only this action. Do not publish.')
        original = copy.deepcopy(saved)
        # A follow-up on a card gets no receipt index: the gateway gives it only the card and the question.
        for operation in ('start', 'resume'):
            request = dict(operation=operation, history=[], decision=decision, question='Exact question',
                           task=dict(title='Keep existing work', pending_decision=decision, _native_receipts=saved))
            prompt, _ = turn_input(request)
            self.assertLess(len(prompt), 10000)
            self.assertIn('coconut_read_receipts', prompt)
            self.assertNotIn('payload-', prompt)
            if operation != 'start':
                self.assertIn(decision['answer'], prompt)
                self.assertIn(decision['action']['arguments']['command'], prompt)
        self.assertEqual(saved, original)

    def test_every_receipt_and_exact_raw_record_is_retrievable(self):
        saved = receipts(31)
        saved[0]['state'] = 'executing'
        saved[-1]['action']['arguments']['unicode'] = '☀️ exact approval'
        original = copy.deepcopy(saved)
        listed, offset = [], 0
        while offset is not None:
            page = read_receipts(saved, {'offset': offset})
            self.assertLessEqual(len(page['receipts']), PAGE_SIZE)
            listed.extend(page['receipts'])
            offset = page['next_offset']
        self.assertEqual([item['receipt_index'] for item in listed], list(range(len(saved))))
        self.assertEqual(listed[0]['state'], 'executing')
        for item in listed:
            chunks, offset = [], 0
            while offset is not None:
                chunk = read_receipts(saved, {'receipt_index': item['receipt_index'], 'offset': offset})
                self.assertLessEqual(len(chunk['json_chunk']), CHUNK_SIZE)
                chunks.append(chunk['json_chunk'])
                offset = chunk['next_offset']
            self.assertEqual(json.loads(''.join(chunks)), saved[item['receipt_index']])
        self.assertEqual(saved, original)
        self.assertIn('"uncertain": 1', receipt_prompt(saved))

    def test_untrusted_payload_and_long_identifiers_cannot_expand_index(self):
        saved = receipts(1)
        saved[0].update(id='i' * 100000, state='untrusted' * 100000)
        saved[0]['action']['name'] = 'n' * 100000
        self.assertLess(len(receipt_prompt(saved)), 1000)
        self.assertIsNone(read_receipts(saved, {})['receipts'][0]['id'])
        self.assertEqual(read_receipts(saved, {})['receipts'][0]['state'], 'unknown')

    def test_durable_id_survives_snapshot_reordering_and_rejects_foreign_receipts(self):
        saved = receipts(2)
        expected = json.loads(read_receipts(saved, {'receipt_id': saved[0]['id']})['json_chunk'])
        actual = json.loads(read_receipts(list(reversed(saved)), {'receipt_id': saved[0]['id']})['json_chunk'])
        self.assertEqual(actual, expected)
        self.assertIn('error', read_receipts(saved, {'receipt_id': 'other-task-receipt'}))
        self.assertIn('error', read_receipts(saved, {'receipt_id': saved[0]['id'], 'receipt_index': 0}))
        self.assertIn('error', read_receipts(saved + saved, {'receipt_id': saved[0]['id']}))

    def test_read_rejects_foreign_lookup_and_invalid_offsets(self):
        saved = receipts(1)
        for args in ({'receipt_index': -1}, {'receipt_index': 1}, {'offset': -1}, {'offset': True},
                     {'receipt_index': None}, {'receipt_index': False}, {'path': '/other/task'}, []):
            self.assertIn('error', read_receipts(saved, args))
        self.assertEqual(read_receipts(saved, {'offset': 100})['receipts'], [])
        self.assertEqual(read_receipts(saved, {'receipt_index': 0, 'offset': 100000})['json_chunk'], '')
        self.assertEqual(receipt_prompt([]), '')
