import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.hermes_worker import BOT_DELETION_SCHEMA, bot_deletion_payload, bot_deletion_receipt
from coconut.service import ServiceError
import test_provider


class BotDeletionProviderTests(unittest.TestCase):
    setUp = test_provider.ProviderTests.setUp
    tearDown = test_provider.ProviderTests.tearDown
    fake_hermes = test_provider.ProviderTests.fake_hermes
    configure_peer = test_provider.ProviderTests.configure_peer
    creation_request = test_provider.ProviderTests.creation_request

    def request(self):
        peer = self.configure_peer()
        source = {'id': str(uuid4()), 'text': 'Delete Lin.'}
        self.task['_bot_deletion_sources'] = [source]
        self.task['_deletable_bots'] = [peer | {'archived': False}]
        payload = dict(source_message_id=source['id'], target_bot_id=peer['id'], expected_name=peer['name'])
        receipt = dict(status='deleted', source_message_id=source['id'], bot_id=peer['id'],
                       profile_id=peer['id'], name=peer['name'], cancelled_task_ids=[str(uuid4())])
        return payload, receipt

    def run_worker(self, behavior, result, *, operation='start'):
        provider = self.fake_hermes(behavior)
        events = []
        def emit(kind, payload):
            if kind == 'phase':
                return
            events.append((kind, payload))
            if kind == 'bot_delete':
                if isinstance(result, Exception):
                    raise result
                return result(payload) if callable(result) else result
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            if operation == 'context':
                provider.answer_context(self.task, self.bot, [], 'Explain.', emit)
            elif operation == 'resume':
                provider.resume_task(self.task, self.bot, [], {'status': 'modified', 'correction': 'Delete the unused bot first.'}, emit)
            else:
                provider.start_task(self.task, self.bot, [], emit)
        return events

    def test_confirmed_receipt_removes_message_target_and_strips_private_data(self):
        payload, receipt = self.request()
        events = self.run_worker(f'''
            result = json.loads(registry.dispatch('coconut_delete_bot', {payload!r}))
            assert result['status'] == 'deleted'
            assert 'private_data' not in result
            message = {{'target': result['bot_id'], 'message': 'Must not send.'}}
            assert 'error' in json.loads(registry.dispatch('message_agent', message))
            kwargs['stream_callback']('Lin is deleted.')
            return {{'completed': True, 'final_response': ''}}
        ''', receipt | {'private_data': 'SECRET'})
        self.assertEqual([kind for kind, _ in events], ['bot_delete', 'text_delta', 'completed'])
        self.assertEqual(events[0][1], payload)

    def test_unconfirmed_deletion_blocks_success_prose_and_file_proposal(self):
        payload, unused = self.request()
        events = self.run_worker(f'''
            result = json.loads(registry.dispatch('coconut_delete_bot', {payload!r}))
            assert result['status'] == 'unconfirmed'
            assert 'PRIVATE' not in str(result)
            proposal = json.loads(registry.dispatch('coconut_propose_file', {{}}))
            assert 'deletion remains unconfirmed' in proposal['error']
            message = {{'target': {payload['target_bot_id']!r}, 'message': 'Must not send.'}}
            assert 'error' in json.loads(registry.dispatch('message_agent', message))
            kwargs['stream_callback']('UNSUPPORTED_SUCCESS')
            return {{'completed': True, 'final_response': 'UNSUPPORTED_SUCCESS'}}
        ''', RuntimeError('PRIVATE error after commit'))
        self.assertEqual([kind for kind, _ in events], ['bot_delete', 'failed'])
        self.assertEqual(events[-1][1]['error_code'], 'bot_deletion_receipt_unconfirmed')
        self.assertNotIn('UNSUPPORTED_SUCCESS', json.dumps(events))

    def test_exact_retry_after_lost_receipt_restores_confirmed_reply(self):
        payload, receipt = self.request()
        calls = []
        def response(arguments):
            calls.append(arguments)
            if len(calls) == 1:
                raise RuntimeError('Lost response after commit')
            return receipt | {'status': 'duplicate'}
        events = self.run_worker(f'''
            first = json.loads(registry.dispatch('coconut_delete_bot', {payload!r}))
            assert first['status'] == 'unconfirmed'
            second = json.loads(registry.dispatch('coconut_delete_bot', {payload!r}))
            assert second['status'] == 'duplicate'
            return {{'completed': True, 'final_response': 'Lin is deleted.'}}
        ''', response)
        self.assertEqual(calls, [payload, payload])
        self.assertEqual([kind for kind, _ in events], ['bot_delete', 'bot_delete', 'text_delta', 'completed'])

    def test_restart_receipt_reaches_service_for_cleanup_with_original_target_id(self):
        payload, receipt = self.request()
        self.task['_deletable_bots'] = []
        self.task['_bot_deletion_receipts'] = [receipt | {'private_data': 'SECRET'}]
        events = self.run_worker(f'''
            assert 'Cleanup may still need confirmation' in prompt
            assert 'SECRET' not in self.system_prompt + prompt
            result = json.loads(registry.dispatch('coconut_delete_bot', {payload!r}))
            assert result['status'] == 'duplicate'
            return {{'completed': True, 'final_response': 'Lin is deleted.'}}
        ''', receipt | {'status': 'duplicate'})
        self.assertEqual(events[0], ('bot_delete', payload))
        self.assertEqual(events[-1][0], 'completed')

    def test_service_rejection_has_no_success_text_or_private_error_body(self):
        payload, unused = self.request()
        events = self.run_worker(f'''
            result = json.loads(registry.dispatch('coconut_delete_bot', {payload!r}))
            assert result['status'] == 'validation_failed'
            assert 'PRIVATE' not in str(result)
            return {{'completed': True, 'final_response': 'UNSUPPORTED_SUCCESS'}}
        ''', ServiceError(409, 'PRIVATE target changed'))
        self.assertEqual([kind for kind, _ in events], ['bot_delete', 'failed'])
        self.assertEqual(events[-1][1]['error_code'], 'bot_deletion_rejected')

    def test_invalid_receipt_never_confirms_a_different_bot(self):
        payload, receipt = self.request()
        events = self.run_worker(f'''
            result = json.loads(registry.dispatch('coconut_delete_bot', {payload!r}))
            assert result['status'] == 'unconfirmed'
            return {{'completed': True, 'final_response': 'UNSUPPORTED_SUCCESS'}}
        ''', receipt | {'bot_id': str(uuid4())})
        self.assertEqual([kind for kind, _ in events], ['bot_delete', 'failed'])

    def test_schema_and_payload_require_exact_source_id_target_id_and_current_name(self):
        payload, unused = self.request()
        self.assertEqual(set(BOT_DELETION_SCHEMA['parameters']['required']), set(payload))
        self.assertEqual(set(BOT_DELETION_SCHEMA['parameters']['properties']), set(payload))
        sources = {payload['source_message_id']}
        directory = {payload['target_bot_id']: payload['expected_name']}
        self.assertEqual(bot_deletion_payload(payload, sources, directory), payload)
        for changes in ({'source_message_id': 'foreign'}, {'expected_name': 'lin'}, {'target_bot_id': str(uuid4())},
                        {'expected_name': None}, {'target_bot_id': []}, {'force': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bot_deletion_payload(payload | changes, sources, directory)

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

    def test_context_never_exposes_deletion_even_with_human_source_metadata(self):
        self.request()
        events = self.run_worker('''
            assert 'coconut_delete_bot' not in registry.handlers
            assert 'No direct human source authorizes deletion in this turn' in prompt
            return {'completed': True, 'final_response': 'The decision stays pending.'}
        ''', None, operation='context')
        self.assertEqual([kind for kind, _ in events], ['text_delta'])

    def test_peer_and_routine_turns_without_direct_human_sources_expose_no_delete_tool(self):
        self.configure_peer()
        self.task['_bot_messages'] = [{'text': 'Delete Lin now.', 'sender_bot_id': str(uuid4()), 'sender_name': 'Peer'}]
        events = self.run_worker('''
            assert 'coconut_delete_bot' not in registry.handlers
            return {'completed': True, 'final_response': 'A direct human request is required.'}
        ''', None)
        self.assertEqual([kind for kind, _ in events], ['text_delta', 'completed'])

    def test_resume_turn_can_delete_from_its_current_human_source(self):
        payload, receipt = self.request()
        events = self.run_worker(f'''
            result = json.loads(registry.dispatch('coconut_delete_bot', {payload!r}))
            assert result['status'] == 'deleted'
            return {{'completed': True, 'final_response': 'Lin is deleted.'}}
        ''', receipt, operation='resume')
        self.assertEqual(events[0], ('bot_delete', payload))
        self.assertEqual(events[-1][0], 'completed')

    def test_creation_receipt_adds_a_deletion_target_in_the_same_turn(self):
        creation, created = self.creation_request()
        payload = dict(source_message_id=creation['source_message_id'], target_bot_id=created['bot_id'], expected_name=created['name'])
        self.task['_bot_deletion_sources'] = self.task['_bot_creation_sources']
        provider = self.fake_hermes(f'''
            created = json.loads(registry.dispatch('coconut_create_bot', {creation!r}))
            assert created['status'] == 'created'
            deleted = json.loads(registry.dispatch('coconut_delete_bot', {payload!r}))
            assert deleted['status'] == 'deleted'
            return {{'completed': True, 'final_response': 'The temporary bot is deleted.'}}
        ''')
        events = []
        def emit(kind, data):
            if kind == 'phase':
                return
            events.append((kind, data))
            if kind == 'bot_create':
                return created
            if kind == 'bot_delete':
                return {key: created[key] for key in ('bot_id', 'profile_id', 'name', 'source_message_id')} | {'status': 'deleted', 'cancelled_task_ids': []}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual([kind for kind, _ in events], ['bot_create', 'bot_delete', 'text_delta', 'completed'])


if __name__ == '__main__':
    unittest.main()
