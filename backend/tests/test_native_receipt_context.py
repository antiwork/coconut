import copy
import json
import unittest

from coconut.hermes_worker import conversation_input
from coconut.native_failure import native_failure
from coconut.native_receipt_context import CHUNK_SIZE, PAGE_SIZE, read_receipts, receipt_prompt


def receipts(count=305) -> list[dict]:
    return [dict(id='receipt-' + str(i), state='completed', action_hash='hash-' + str(i),
                 action={'name': 'terminal', 'arguments': {'command': 'exact command ' + str(i)}},
                 result=json.dumps({'output': 'payload-' + 'x' * 6000, 'exit_code': 0})) for i in range(count)]


class ReceiptContextTests(unittest.TestCase):
    def test_start_resume_and_context_bound_replay_preserve_exact_approval(self):
        saved = receipts()
        decision: dict = dict(status='approved', action_type='native_approval',
                        action={'name': 'terminal', 'arguments': {'command': 'ONLY exact command'}},
                        answer='Approve only this action. Do not publish.')
        original = copy.deepcopy(saved)
        for operation in ('start', 'resume', 'context'):
            request = dict(operation=operation, history=[], decision=decision, question='Exact question',
                           task=dict(title='Keep existing work', pending_decision=decision, _native_receipts=saved))
            prompt, _ = conversation_input(request)
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

    def test_reader_encodes_large_record_once_and_bounds_cache_per_snapshot(self):
        from unittest.mock import patch
        from coconut.native_receipt_context import receipt_reader
        saved = receipts(2)
        saved[0]['result'] = 'x' * 1500000
        reader = receipt_reader(saved)
        expected = json.dumps(saved[0], ensure_ascii=True)
        with patch('coconut.native_receipt_context.json.dumps', wraps=json.dumps) as encode:
            chunks, offset = [], 0
            while offset is not None:
                result = reader({'receipt_id': 'receipt-0', 'offset': offset})
                chunks.append(result['json_chunk'])
                offset = result['next_offset']
            self.assertEqual(''.join(chunks), expected)
            self.assertEqual(encode.call_count, 1)
            self.assertIn('error', reader({'receipt_id': 'foreign'}))
            self.assertEqual(encode.call_count, 1)
            reader({'receipt_index': 1})
            reader({'receipt_index': 0})
            self.assertEqual(encode.call_count, 3)  # One entry; the second evicts the first.
        other = receipt_reader(receipts(1))
        self.assertNotEqual(other({'receipt_index': 0})['total_characters'], len(expected))
        saved.reverse()
        self.assertEqual(reader({'receipt_index': 0})['total_characters'], len(expected))

    def test_read_rejects_foreign_lookup_and_invalid_offsets(self):
        saved = receipts(1)
        for args in ({'receipt_index': -1}, {'receipt_index': 1}, {'offset': -1}, {'offset': True},
                     {'receipt_index': None}, {'receipt_index': False}, {'path': '/other/task'}, []):
            self.assertIn('error', read_receipts(saved, args))
        self.assertEqual(read_receipts(saved, {'offset': 100})['receipts'], [])
        self.assertEqual(read_receipts(saved, {'receipt_index': 0, 'offset': 100000})['json_chunk'], '')
        self.assertEqual(receipt_prompt([]), '')


class NativeFailureTests(unittest.TestCase):
    def test_allowlisted_native_reasons_never_echo_private_result(self):
        for reason in ('context_overflow', 'rate_limit', 'auth', 'billing', 'timeout', 'session_busy'):
            result = native_failure({'failure_reason': reason, 'error': 'private-key',
                                     'final_response': 'private-key', 'provider': 'private-key'})
            self.assertEqual(result['error_code'], 'native_' + reason)
            self.assertNotIn('private-key', json.dumps(result))
        for reason in ('private-key', {'private-key': True}, None):
            self.assertEqual(native_failure({'failure_reason': reason})['error_code'], 'native_turn_failed')
        self.assertEqual(native_failure({'interrupted': True})['error_code'], 'native_interrupted')


class WorkerReceiptBoundaryTests(unittest.TestCase):
    def setUp(self):
        from test_worker_model_settings import WorkerModelProcessTests
        self.fixture = WorkerModelProcessTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_tool_registered_and_raw_receipts_read_without_reexecution(self):
        registry_path = self.fixture.source / 'tools/registry.py'
        registry_path.write_text(registry_path.read_text().replace(
            'return self.handlers[name](args)', 'return self.handlers[name](args, **kwargs)'))
        path = self.fixture.source / 'run_agent.py'
        source = path.read_text()
        source = source.replace("kwargs['stream_callback']('Ready.')", '''assert len(prompt) < 10000
        page = json.loads(registry.dispatch('coconut_read_receipts', {}))
        assert page['total'] == 305
        record = json.loads(registry.dispatch('coconut_read_receipts', {'receipt_index': 0}))
        assert 'exact command 0' in record['json_chunk']
        from native_turn_scope import TurnScope, current
        owner = current.get()
        other = TurnScope('other-task')
        token = current.set(other)
        try:
            assert 'error' in json.loads(registry.dispatch('coconut_read_receipts', {}))
            other.register(name='coconut_read_receipts', toolset='coconut', schema=owner.schemas['coconut_read_receipts'],
                           handler=lambda arguments, **kw: json.dumps({'other': True}))
            assert json.loads(registry.dispatch('coconut_read_receipts', {})) == {'other': True}
        finally:
            current.reset(token)
        assert json.loads(registry.dispatch('coconut_read_receipts', {}))['total'] == 305
        assert 'error' in json.loads(registry.dispatch('coconut_read_receipts', {}, task_id='other-task'))
        token = current.set(None)
        try:
            assert 'error' in json.loads(registry.dispatch('coconut_read_receipts', {}))
        finally:
            current.reset(token)
        kwargs['stream_callback']('Ready.')''')
        path.write_text(source)
        _, events, records, _ = self.fixture.run_worker('resume', effort=None, extract=False,
            task_context={'_native_receipts': receipts()})
        self.assertIn('coconut_read_receipts', records[0]['allowed_tools'])
        self.assertIn('completed', [event['type'] for event in events])

    def test_empty_receipt_view_is_valid_and_contains_no_previous_snapshot(self):
        path = self.fixture.source / 'run_agent.py'
        path.write_text(path.read_text().replace("kwargs['stream_callback']('Ready.')", '''assert json.loads(registry.dispatch('coconut_read_receipts', {})) == {'total': 0, 'receipts': [], 'next_offset': None}
        assert 'error' in json.loads(registry.dispatch('coconut_read_receipts', {'receipt_id': 'receipt-0'}))
        kwargs['stream_callback']('Ready.')'''))
        _, events, records, _ = self.fixture.run_worker('start', effort=None, extract=False)
        self.assertIn('coconut_read_receipts', records[0]['allowed_tools'])
        self.assertIn('completed', [event['type'] for event in events])

    def test_typed_native_failure_crosses_real_worker_event_boundary(self):
        path = self.fixture.source / 'run_agent.py'
        path.write_text(path.read_text().replace("return {'completed': True, 'final_response': 'Ready.'}",
            "return {'completed': False, 'failed': True, 'failure_reason': 'context_overflow', 'error': 'private-key', 'final_response': 'private-key'}"))
        _, events, _, _ = self.fixture.run_worker('resume', effort=None, extract=False)
        failures = [event['data'] for event in events if event['type'] == 'failed']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['error_code'], 'native_context_overflow')
        self.assertNotIn('private-key', json.dumps(events))
        self.assertNotIn('RuntimeError', json.dumps(events))
        self.assertNotIn('completed', [event['type'] for event in events])
        from test_service_bot_creation import BotCreationTests
        fixture = BotCreationTests('runTest')
        fixture.setUp()
        try:
            task = fixture.working(fixture.send('Check saved work.'))
            fixture.service.emit(task['id'], 'failed', failures[0])
            fixture.restart()
            saved = fixture.service._get('tasks', task['id'])
            self.assertEqual(saved['status'], 'failed')
            self.assertEqual(saved['error_code'], 'native_context_overflow')
            self.assertEqual(saved['summary'], failures[0]['summary'])
            for code in ('private-key', ['private-key']):
                task = fixture.working(fixture.send('Check another saved item.'))
                fixture.service.emit(task['id'], 'failed', {'summary': 'Static failure.', 'error_code': code})
                self.assertNotIn('error_code', fixture.service._get('tasks', task['id']))
        finally:
            fixture.tearDown()
