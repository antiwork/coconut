import base64
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from coconut.service import Service, ServiceError, digest
from coconut.server import APIServer, create_pairing_code
from coconut.answer_undo import AnswerUndo
# These tests check what an answer does, so it applies at once. test_answer_undo.py tests the Undo window.
AnswerUndo.answer_undo_seconds = 0


class ControlledProvider:
    name = 'fixture-test'
    ready = True
    model = 'scripted'

    def __init__(self):
        self.starts = []
        self.resumes = []
        self.contexts = []
        self.cancelled = []
        self.hold = threading.Event()
        self.started = threading.Event()
        self.model = 'scripted'
        self.base_url = None

    def configure(self, model, base_url=None):
        self.model = model
        self.base_url = base_url

    def start_task(self, task, bot, history, emit):
        self.starts.append((task, bot, history))
        if task['title'].startswith('Hold') and len(self.starts) == 1:
            self.started.set()
            self.hold.wait(3)
            emit('completed', {'summary': 'First turn finished.'})
        elif task['title'].startswith('Write'):
            emit('activity', {'text': 'Preparing the file.'})
            emit('decision', {'title': 'Create report.md?', 'explanation': 'Save the prepared report.', 'action_type': 'create_file', 'action': {'type': 'create_file', 'filename': 'report.md', 'content': 'A complete report.\n'}, 'predicted_choice': 'approve'})
        else:
            emit('text_delta', {'text': 'The independent task is done.'})
            emit('completed', {'summary': 'Ready to review.'})

    def resume_task(self, task, bot, history, decision, emit):
        self.resumes.append(decision)
        if decision['status'] == 'modified':
            emit('decision', {'title': 'Save the revised report?', 'action_type': 'create_file', 'action': {'type': 'create_file', 'filename': 'report.md', 'content': decision['correction']}, 'predicted_choice': 'approve'})
        else:
            emit('completed', {'summary': 'The approved report is ready.'})

    def answer_context(self, task, bot, history, question, emit):
        self.contexts.append(question)
        self.last_context_task = task
        emit('text_delta', {'text': 'This creates one private text file on this Mac.'})

    def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        self.hold.set()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None, key=None):
        return self.service.mutate(method, path, data or {}, key or str(uuid4()), 'test-phone')

    def bot(self, name='Ada'):
        return self.write('POST', '/v1/bots', {'name': name, 'role': 'Research'})

    def send(self, bot, text='Write a report', **values):
        return self.write('POST', f'/v1/bots/{bot["id"]}/messages', dict(text=text, mode='queue', **values))

    def wait(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = self.service.snapshot()
            if predicate(state):
                return state
            time.sleep(.02)
        self.fail(f'Timed out. State: {self.service.snapshot()}')

    def pending(self, bot=None):
        bot = bot or self.bot()
        task = self.send(bot)
        self.service._run_task(task['id'])
        return self.service.snapshot()['decisions'][-1]

    def answer(self, decision, choice='approve', **values):
        return self.write('POST', f'/v1/decisions/{decision["id"]}/answer', dict(choice=choice, revision=decision['revision'], **values))

    def test_restart_milestone_keeps_decision_context_and_single_artifact(self):
        first, second = self.bot('Ada'), self.bot('Ken')
        first_task, second_task = self.send(first), self.send(second, 'Read the status')
        self.service.start()
        state = self.wait(lambda s: len(s['decisions']) == 1 and any(t['id'] == second_task['id'] and t['status'] == 'completed' for t in s['tasks']))
        decision = state['decisions'][0]
        before_cursor = state['cursor']
        self.service.close()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider)
        state = self.service.snapshot()
        self.assertEqual(state['decisions'][0]['id'], decision['id'])
        self.assertEqual(state['decisions'][0]['status'], 'pending')
        self.write('POST', f'/v1/decisions/{decision["id"]}/context', {'question': 'What changes?'})
        state = self.wait(lambda s: any(m['decision_id'] == decision['id'] and 'private text file' in m['text'] for m in s['messages']))
        self.assertEqual(state['decisions'][0]['status'], 'pending')
        key = str(uuid4())
        data = {'choice': 'approve', 'revision': 1}
        first_reply = self.write('POST', f'/v1/decisions/{decision["id"]}/answer', data, key)
        self.assertEqual(first_reply, self.write('POST', f'/v1/decisions/{decision["id"]}/answer', data, key))
        state = self.wait(lambda s: any(t['id'] == first_task['id'] and t['status'] == 'completed' for t in s['tasks']))
        self.assertEqual(len(state['artifacts']), 1)
        self.assertEqual(state['decisions'][0]['status'], 'executed')
        self.assertEqual(self.service.artifact_content(state['artifacts'][0]['id'])[0], b'A complete report.\n')
        self.assertGreater(self.service.events(before_cursor)['cursor'], before_cursor)
        self.service.close()
        self.service = Service(self.temp.name, ControlledProvider(), start_workers=False)
        self.assertEqual(len(self.service.snapshot()['artifacts']), 1)
        self.assertEqual(self.service.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)

    def test_idempotency_rejects_payload_and_device_collisions(self):
        key = str(uuid4())
        bot = self.write('POST', '/v1/bots', {'name': 'Ada'}, key)
        self.assertEqual(bot, self.write('POST', '/v1/bots', {'name': 'Ada'}, key))
        with self.assertRaises(ServiceError) as caught:
            self.write('POST', '/v1/bots', {'name': 'Other'}, key)
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(ServiceError):
            self.service.mutate('POST', '/v1/bots', {'name': 'Ada'}, key, 'another-phone')
        self.assertEqual(len(self.service.snapshot()['bots']), 1)

    def test_message_request_id_survives_retry_and_restart(self):
        bot = self.bot()
        path = f'/v1/bots/{bot["id"]}/messages'
        key = str(uuid4())
        data = dict(text='Keep this message once.', mode='queue')
        task = self.write('POST', path, data, key)
        self.assertEqual(task, self.write('POST', path, data, key))
        self.service.close()
        self.service = Service(self.temp.name, ControlledProvider(), start_workers=False)
        self.assertEqual(task, self.write('POST', path, data, key))
        messages = [m for m in self.service.snapshot()['messages'] if m.get('client_request_id') == key]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['text'], data['text'])
        # Identical text with another key is a separate, intentional message.
        other = str(uuid4())
        self.write('POST', path, data, other)
        self.assertEqual(len([m for m in self.service.snapshot()['messages'] if m.get('client_request_id') == other]), 1)
        self.assertIsNone(self.service._request_key)

    def test_mentioned_message_keeps_request_id_only_on_human_text(self):
        first, second = self.bot(), self.bot('Second')
        key = str(uuid4())
        task = self.write('POST', f'/v1/bots/{first["id"]}/messages',
                          dict(text='Check this.', mode='queue', recipient_bot_ids=[second['id']]), key)
        with self.service.transaction():
            self.service._message(second['id'], 'A reply.', task_id=task['id'])
        messages = self.service.snapshot()['messages']
        self.assertEqual(len([m for m in messages if m.get('client_request_id') == key]), 1)
        self.assertNotIn('client_request_id', messages[-1])

    def test_single_process_owns_recovery(self):
        with self.assertRaises(ServiceError):
            Service(self.temp.name, ControlledProvider())

    def test_pairing_expiry_single_use_and_rate_limit(self):
        code = self.service.pairing_code()
        pair = self.service.pair({'code': code, 'device_name': 'Phone'})
        self.assertEqual(self.service.authenticate(pair['token']), pair['device_id'])
        with self.assertRaises(ServiceError):
            self.service.pair({'code': code})
        expired = self.service.pairing_code(-1)
        with self.assertRaises(ServiceError):
            self.service.pair({'code': expired})
        for _ in range(2):
            with self.assertRaises(ServiceError):
                self.service.pair({'code': 'wrong'})
        with self.assertRaises(ServiceError) as caught:
            self.service.pair({'code': self.service.pairing_code()})
        self.assertEqual(caught.exception.status, 429)

    def test_offline_pairing_initializes_only_pairing_storage(self):
        with tempfile.TemporaryDirectory() as root:
            code = create_pairing_code(root)
            service = Service(root, ControlledProvider(), start_workers=False)
            try:
                self.assertIn('token', service.pair({'code': code}))
            finally:
                service.close()

    def test_modify_never_approves_revised_action(self):
        decision = self.pending()
        revised = self.answer(decision, 'modify', correction='Use the corrected report.')
        self.assertEqual(revised['status'], 'modified')
        self.service._run_task(decision['task_id'])
        state = self.service.snapshot()
        self.assertEqual(len(state['decisions']), 2)
        self.assertEqual(state['decisions'][-1]['status'], 'pending')
        self.assertEqual(state['decisions'][-1]['replaces_decision_id'], decision['id'])
        self.assertEqual(state['decisions'][0]['replaced_by_decision_id'], state['decisions'][-1]['id'])
        self.assertEqual(state['artifacts'], [])
        self.assertEqual(self.service.learning()['groups'][0]['agreed'], 0)

    def test_invalid_file_corrections_do_not_change_the_pending_decision(self):
        for correction in ('x' * 10001, ['revise'], {'text': 'revise'}, False, 123):
            decision = self.pending()
            before = self.service.snapshot()
            with self.subTest(correction_type=type(correction).__name__):
                with self.assertRaises(ServiceError) as error:
                    self.answer(decision, 'modify', correction=correction)
                self.assertEqual(error.exception.status, 400)
                after = self.service.snapshot()
                for field in ('decisions', 'tasks', 'messages', 'cursor'):
                    self.assertEqual(after[field], before[field])

    def test_file_correction_at_the_limit_preserves_its_final_instruction(self):
        decision = self.pending()
        correction = 'x' * 9990 + ' Keep blue'
        revised = self.answer(decision, 'modify', correction=correction)
        self.assertEqual(revised['correction'], correction)
        message = next(m for m in self.service.snapshot()['messages'] if m['role'] == 'user' and m['decision_id'] == decision['id'])
        self.assertEqual(message['text'], correction)

    def test_stale_revision_and_pause_prevent_approval(self):
        decision = self.pending()
        with self.assertRaises(ServiceError):
            self.write('POST', f'/v1/decisions/{decision["id"]}/answer', {'choice': 'approve', 'revision': 99})
        self.write('PATCH', f'/v1/bots/{decision["bot_id"]}', {'archived': True})
        with self.assertRaises(ServiceError):
            self.answer(decision, 'approve')

    def test_receipt_reconciles_crash_after_file_write(self):
        decision = self.pending()
        self.answer(decision)
        original = self.service._complete_receipt
        def crash(*args):
            raise KeyboardInterrupt('crash after durable file creation')
        self.service._complete_receipt = crash
        with self.assertRaises(KeyboardInterrupt):
            self.service._run_task(decision['task_id'])
        self.service._complete_receipt = original
        self.assertEqual(len(list((Path(self.temp.name) / 'artifacts').iterdir())), 1)
        self.service.close()
        self.service = Service(self.temp.name, ControlledProvider(), start_workers=False)
        state = self.service.snapshot()
        self.assertEqual(len(state['artifacts']), 1)
        self.assertEqual(state['decisions'][0]['status'], 'executed')
        self.service._run_task(decision['task_id'])
        self.assertEqual(len(self.service.snapshot()['artifacts']), 1)

    def test_execution_failure_updates_the_decision_card(self):
        from unittest.mock import patch
        decision = self.pending()
        self.answer(decision)
        with patch('coconut.service.os.open', side_effect=OSError('The disk is full.')):
            self.service._run_task(decision['task_id'])
        state = self.service.snapshot()
        self.assertEqual(state['decisions'][0]['status'], 'failed')
        self.assertIn('disk is full', state['decisions'][0]['outcome'])
        self.assertEqual(state['tasks'][0]['status'], 'failed')
        self.assertEqual(state['artifacts'], [])
        self.service.close()
        self.service = Service(self.temp.name, ControlledProvider(), start_workers=False)
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'failed')
        self.assertEqual(self.service.snapshot()['artifacts'], [])

    def test_ambiguous_partial_file_never_repeats(self):
        decision = self.pending()
        self.answer(decision)
        with self.service.transaction():
            task = self.service._get('tasks', decision['task_id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
            artifact_id = str(uuid4())
            self.service.db.execute('INSERT INTO receipts VALUES (?,?,?,?,?)', (decision['id'], 'executing', artifact_id, digest('A complete report.\n'), 'crash'))
            (Path(self.temp.name) / 'artifacts' / artifact_id).write_text('partial')
        self.service.close()
        self.service = Service(self.temp.name, ControlledProvider(), start_workers=False)
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'failed')
        self.assertEqual(self.service.snapshot()['tasks'][0]['status'], 'failed')
        self.assertEqual((Path(self.temp.name) / 'artifacts' / artifact_id).read_text(), 'partial')

    def test_context_request_survives_restart_before_dispatch(self):
        decision = self.pending()
        data = {'question': 'Why save this?'}
        key = str(uuid4())
        response = self.write('POST', f'/v1/decisions/{decision["id"]}/context', data, key)
        self.service.close()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider)
        self.assertEqual(response, self.write('POST', f'/v1/decisions/{decision["id"]}/context', data, key))
        self.wait(lambda s: any('private text file' in m['text'] for m in s['messages']))
        self.assertEqual(self.provider.contexts, ['Why save this?'])
        self.assertEqual(self.provider.last_context_task['pending_decision']['action']['content'], 'A complete report.\n')
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'pending')

    def test_automatic_send_delivers_followup_once_at_next_safe_turn(self):
        bot = self.bot()
        task = self.send(bot, 'Hold this task')
        self.service.start()
        self.assertTrue(self.provider.started.wait(2))
        data = {'text': 'Use the updated scope.', 'mode': 'auto', 'task_id': 'stale-client-id'}
        key = str(uuid4())
        first = self.write('POST', f'/v1/bots/{bot["id"]}/messages', data, key)
        self.assertEqual(first['id'], task['id'])
        self.assertEqual(self.write('POST', f'/v1/bots/{bot["id"]}/messages', data, key), first)
        self.assertFalse(any(m['kind'] == 'activity' for m in self.service.snapshot()['messages']))
        self.provider.hold.set()
        state = self.wait(lambda state: len(self.provider.starts) == 2 and state['tasks'][0]['status'] == 'completed')
        self.assertEqual(len(state['tasks']), 1)
        self.assertEqual(sum(m['text'] == data['text'] for m in self.provider.starts[1][2]), 1)

    def test_automatic_send_joins_queued_work_and_survives_restart(self):
        bot = self.bot()
        first = self.write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'First request', 'mode': 'auto'})
        second = self.write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Additional request', 'mode': 'auto'})
        self.assertEqual(first['id'], second['id'])
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.service._run_task(first['id'])
        texts = [m['text'] for m in self.provider.starts[0][2] if m['role'] == 'user']
        self.assertEqual(texts, ['First request', 'Additional request'])

    def test_automatic_send_after_completion_keeps_native_session(self):
        bot = self.bot()
        first = self.send(bot, 'Initial request')
        self.service._run_task(first['id'])
        second = self.write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Follow up', 'mode': 'auto', 'task_id': first['id']})
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['session_id'], second['session_id'])
        self.service._run_task(second['id'])
        self.assertIn('Initial request', [m['text'] for m in self.provider.starts[-1][2]])

    def test_automatic_send_keeps_pending_decision_and_allows_other_work(self):
        bot = self.bot()
        decision = self.pending(bot)
        next_task = self.write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Another request', 'mode': 'auto'})
        self.assertNotEqual(next_task['id'], decision['task_id'])
        self.service._run_task(next_task['id'])
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'pending')
        self.assertEqual(self.provider.contexts, [])

    def test_automatic_send_does_not_attach_to_scheduled_work(self):
        bot = self.bot()
        future = self.send(bot, 'Later', scheduled_at='2099-01-01T12:00:00Z')
        current = self.write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Now', 'mode': 'auto'})
        self.assertNotEqual(current['id'], future['id'])
        self.assertEqual(current['status'], 'queued')

    def test_live_steering_runs_at_next_safe_turn(self):
        bot = self.bot()
        task = self.send(bot, 'Hold this task')
        self.service.start()
        self.assertTrue(self.provider.started.wait(2))
        response = self.write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Use the updated scope.', 'mode': 'steer', 'task_id': task['id']})
        self.assertEqual(response['steering_status'], 'next_safe_turn')
        self.provider.hold.set()
        self.wait(lambda s: len(self.provider.starts) == 2 and s['tasks'][0]['status'] == 'completed')
        self.assertTrue(any(m['text'] == 'Use the updated scope.' for m in self.provider.starts[1][2]))
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)

    def test_attachment_is_scoped_and_reaches_provider(self):
        bot, other = self.bot(), self.bot('Ken')
        artifact = self.write('POST', '/v1/attachments', {'bot_id': bot['id'], 'filename': 'notes.txt', 'content_base64': base64.b64encode(b'hello').decode()})
        with self.assertRaises(ServiceError):
            self.send(other, 'Read notes', attachment_ids=[artifact['id']])
        task = self.send(bot, 'Read notes', attachment_ids=[artifact['id']])
        self.service._run_task(task['id'])
        self.assertEqual(self.provider.starts[0][0]['attachments'][0]['content_base64'], base64.b64encode(b'hello').decode())
        self.assertNotIn('attachments', self.service.snapshot()['tasks'][0])
        self.assertEqual(self.service.artifact_content(artifact['id'])[0], b'hello')
        self.assertTrue(any(m['artifact_id'] == artifact['id'] and m['role'] == 'user' for m in self.service.snapshot()['messages']))

    def test_cancelled_decision_cannot_execute(self):
        decision = self.pending()
        self.write('POST', f'/v1/tasks/{decision["task_id"]}/cancel')
        with self.assertRaises(ServiceError):
            self.answer(decision)
        self.assertEqual(self.service.snapshot()['artifacts'], [])

    def test_public_proposal_matches_exact_executable_content(self):
        decision = self.pending()
        self.assertEqual(decision['proposal_preview'], 'A complete report.\n')
        self.assertEqual(len(decision['proposal_hash']), 64)
        self.assertNotIn('_action', decision)

    def test_persisted_model_applies_on_restart(self):
        self.write('PUT', '/v1/settings/model', {'model': 'local-model', 'base_url': 'http://127.0.0.1:11434/v1'})
        self.service.close()
        provider = ControlledProvider()
        self.service = Service(self.temp.name, provider, start_workers=False)
        self.assertEqual(provider.model, 'local-model')

    def test_model_endpoint_survives_model_only_save_until_explicit_clear(self):
        endpoint = 'http://127.0.0.1:11434/v1'
        self.write('PUT', '/v1/settings/model', {'model': 'first-model', 'base_url': endpoint})
        response = self.write('PUT', '/v1/settings/model', {'model': 'second-model'})
        self.assertEqual(self.provider.base_url, endpoint)
        self.assertEqual(response['base_url'], endpoint)
        self.assertEqual(self.service.snapshot()['base_url'], endpoint)

        self.service.close()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.assertEqual(self.provider.model, 'second-model')
        self.assertEqual(self.provider.base_url, endpoint)
        self.assertEqual(self.service.snapshot()['base_url'], endpoint)

        response = self.write('PUT', '/v1/settings/model', {'model': 'second-model', 'base_url': ''})
        self.assertEqual(response['base_url'], '')
        self.assertIsNone(self.provider.base_url)
        self.service.close()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.assertEqual(self.service.snapshot()['base_url'], '')
        self.assertIsNone(self.provider.base_url)

    def test_cancelled_context_never_starts(self):
        decision = self.pending()
        self.write('POST', f'/v1/decisions/{decision["id"]}/context', {'question': 'What changes?'})
        self.write('POST', f'/v1/tasks/{decision["task_id"]}/cancel')
        context = self.service._all('contexts')[0]
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(1)
        self.assertEqual(self.provider.contexts, [])
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'cancelled')

    def test_shutdown_cancels_active_context_provider(self):
        decision = self.pending()
        started = threading.Event()
        def blocked_context(task, bot, history, question, emit):
            started.set()
            self.provider.hold.wait(3)
            emit('text_delta', {'text': 'Context output'})
        self.provider.answer_context = blocked_context
        self.write('POST', f'/v1/decisions/{decision["id"]}/context', {'question': 'What changes?'})
        self.service.start()
        self.assertTrue(started.wait(2))
        self.service.close()
        self.assertIn(decision['task_id'], self.provider.cancelled)
        self.service = Service(self.temp.name, ControlledProvider(), start_workers=False)
        self.assertEqual(self.service._all('contexts')[0]['status'], 'queued')
        self.assertFalse(any(m['text'] == 'Context output' for m in self.service.snapshot()['messages']))

    def test_option_is_never_silently_ignored(self):
        decision = self.pending()
        with self.assertRaises(ServiceError) as caught:
            self.answer(decision, option='Create something different')
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'pending')

    def test_missing_prediction_is_excluded_from_learning(self):
        def start_without_prediction(task, bot, history, emit):
            emit('decision', {'title': 'Save report?', 'action_type': 'create_file', 'action': {'type': 'create_file', 'filename': 'report.txt', 'content': 'Report'}})
        self.provider.start_task = start_without_prediction
        decision = self.pending()
        self.answer(decision, 'reject')
        self.assertEqual(self.service.learning()['groups'], [])

    def option_pending(self, *, predicted='full', recommended='full', bot=None):
        base = {'type': 'create_file', 'filename': 'report.txt', 'content': 'Full report.\n'}
        brief = {'type': 'create_file', 'filename': 'brief.txt', 'content': 'Brief report.\n'}
        payload = {
            'title': 'Choose a report', 'action_type': 'create_file', 'action': base,
            'recommended_option_id': recommended, 'predicted_option_id': predicted,
            'options': [
                {'id': 'full', 'label': 'Full report', 'explanation': 'Save the complete report.', 'answer': 'approve', 'action': base},
                {'id': 'brief', 'label': 'Brief report', 'explanation': 'Save only the short report.', 'answer': 'approve', 'action': brief},
                {'id': 'skip', 'label': 'Skip the file', 'explanation': 'Create no file.', 'answer': 'reject'},
                {'id': 'revise', 'label': 'Add the source', 'explanation': 'Prepare a new proposal.', 'answer': 'modify', 'modification': 'Add the source before saving.'},
            ],
        }
        self.provider.start_task = lambda task, bot, history, emit: emit('decision', payload)
        return self.pending(bot)

    def choose(self, decision, option_id, **extra):
        return self.write('POST', f'/v1/decisions/{decision["id"]}/answer', dict(option_id=option_id, revision=decision['revision'], **extra))

    def test_selected_file_variant_survives_restart_and_executes_once(self):
        decision = self.option_pending()
        self.assertEqual(decision['options'][1]['action']['content'], 'Brief report.\n')
        selected = self.choose(decision, 'brief')
        self.assertEqual(selected['selected_option_id'], 'brief')
        self.assertEqual(selected['proposal_preview'], 'Brief report.\n')
        self.assertEqual(selected['resource'], 'brief.txt')
        self.service.close()
        self.service = Service(self.temp.name, ControlledProvider(), start_workers=False)
        self.service._run_task(decision['task_id'])
        state = self.service.snapshot()
        self.assertEqual(len(state['artifacts']), 1)
        self.assertEqual(self.service.artifact_content(state['artifacts'][0]['id']), (b'Brief report.\n', 'brief.txt'))
        self.assertEqual(state['decisions'][0]['selected_option_label'], 'Brief report')
        self.assertEqual(self.service.learning()['groups'][0]['agreed'], 0)
        audit = self.service._get('decisions', decision['id'])['_answers'][0]
        self.assertEqual(audit['option_id'], 'brief')
        self.assertEqual(audit['predicted_option_id'], 'full')
        self.assertNotEqual(audit['action_hash'], audit['original_action_hash'])
        self.service._run_task(decision['task_id'])
        self.assertEqual(len(self.service.snapshot()['artifacts']), 1)

    def test_option_agreement_uses_exact_choice(self):
        decision = self.option_pending(predicted='brief')
        self.choose(decision, 'brief')
        self.assertEqual(self.service.learning()['groups'][0]['agreed'], 1)
        self.assertEqual(self.service.learning()['groups'][0]['total'], 1)

    def test_choice_requires_valid_id_and_matching_answer(self):
        decision = self.option_pending()
        for values in ({'choice': 'approve'}, {'option_id': 'missing'}, {'option_id': 'skip', 'choice': 'approve'}):
            with self.assertRaises(ServiceError):
                self.write('POST', f'/v1/decisions/{decision["id"]}/answer', {'revision': 1, **values})
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'pending')
        self.assertEqual(self.service.snapshot()['artifacts'], [])

    def test_reject_option_creates_no_file(self):
        decision = self.option_pending(predicted='skip')
        rejected = self.choose(decision, 'skip')
        self.assertEqual(rejected['status'], 'rejected')
        self.assertEqual(rejected['selected_option_id'], 'skip')
        self.assertEqual(self.service.snapshot()['artifacts'], [])
        self.assertEqual(self.service.learning()['groups'][0]['agreed'], 1)

    def test_modify_option_uses_its_correction_and_requires_new_approval(self):
        decision = self.option_pending()
        modified = self.choose(decision, 'revise')
        self.assertEqual(modified['status'], 'modified')
        self.assertEqual(modified['correction'], 'Add the source before saving.')
        self.service._run_task(decision['task_id'])
        state = self.service.snapshot()
        self.assertEqual(state['decisions'][-1]['status'], 'pending')
        self.assertEqual(state['artifacts'], [])


    def test_changed_option_is_rejected_before_human_approval(self):
        decision = self.option_pending()
        with self.service.transaction():
            private = self.service._get('decisions', decision['id'])
            private['options'][1]['action']['content'] = 'Unexpected change'
            self.service._put('decisions', private)
        with self.assertRaises(ServiceError):
            self.choose(decision, 'brief')
        self.assertEqual(self.service.snapshot()['artifacts'], [])

    def test_completed_reply_keeps_thread_and_recalls_the_whole_bot_conversation(self):
        bot = self.bot()
        first = self.send(bot, 'Read alpha')
        self.service._run_task(first['id'])
        other = self.send(bot, 'Read beta')
        self.service._run_task(other['id'])
        reply = self.write('POST', f'/v1/tasks/{first["id"]}/replies', {'text': 'Read gamma'})
        self.assertNotEqual(reply['id'], first['id'])
        self.assertEqual(reply['thread_id'], first['thread_id'])
        self.assertEqual(reply['parent_task_id'], first['id'])
        self.assertEqual(reply['reply_status'], 'queued_continuation')
        self.service._run_task(reply['id'])
        user_text = [m['text'] for m in self.provider.starts[-1][2] if m['role'] == 'user']
        self.assertEqual(user_text, ['Read alpha', 'Read beta', 'Read gamma'])
        messages = [m for m in self.service.snapshot()['messages'] if m['task_id'] == reply['id']]
        self.assertTrue(all(m['thread_id'] == first['thread_id'] for m in messages))

    def test_queued_reply_never_steers_another_running_task(self):
        bot = self.bot()
        working = self.send(bot, 'Read working')
        queued = self.send(bot, 'Read queued')
        with self.service.transaction():
            private = self.service._get('tasks', working['id'])
            private['status'] = 'working'
            self.service._put('tasks', private)
        reply = self.write('POST', f'/v1/tasks/{queued["id"]}/replies', {'text': 'Apply this only to the queued task.'})
        self.assertEqual(reply['id'], queued['id'])
        self.assertEqual(reply['reply_status'], 'queued')
        private = self.service._get('tasks', working['id'])
        self.assertFalse(private.get('_steering'))
        message = next(m for m in self.service.snapshot()['messages'] if m['text'] == 'Apply this only to the queued task.')
        self.assertEqual(message['thread_id'], queued['thread_id'])

    def test_pending_thread_reply_requires_explicit_linked_decision(self):
        first = self.pending()
        second = self.pending(self.bot('Ken'))
        route = f'/v1/tasks/{first["task_id"]}/replies'
        with self.assertRaises(ServiceError):
            self.write('POST', route, {'text': 'Explain this.'})
        with self.assertRaises(ServiceError):
            self.write('POST', route, {'text': 'Explain this.', 'decision_id': second['id']})
        reply = self.write('POST', route, {'text': 'Explain this.', 'decision_id': first['id']})
        self.assertEqual(reply['reply_status'], 'context_requested')
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'pending')
        context = self.service._all('contexts')[0]
        self.assertEqual(context['decision_id'], first['id'])
        self.assertEqual(context['task_id'], first['task_id'])

    def test_thread_ids_migrate_and_survive_restart(self):
        task = self.send(self.bot(), 'Read this thread')
        with self.service.transaction():
            private = self.service._get('tasks', task['id'])
            del private['thread_id']
            self.service._put('tasks', private)
            for message in self.service._all('messages'):
                message.pop('thread_id', None)
                self.service._put('messages', message)
        self.service.close()
        self.service = Service(self.temp.name, ControlledProvider(), start_workers=False)
        state = self.service.snapshot()
        self.assertEqual(state['tasks'][0]['thread_id'], task['id'])
        self.assertEqual(state['messages'][0]['thread_id'], task['id'])


    def test_bot_pin_persists_across_restart_and_can_clear(self):
        bot = self.bot()
        self.assertFalse(bot['pinned'])
        pinned = self.write('PATCH', f'/v1/bots/{bot["id"]}', {'pinned': True})
        self.assertTrue(pinned['pinned'])
        self.service.close()
        self.service = Service(self.temp.name, ControlledProvider(), start_workers=False)
        restored = self.service.snapshot()['bots'][0]
        self.assertEqual(restored['id'], bot['id'])
        self.assertEqual(restored['name'], bot['name'])
        self.assertTrue(restored['pinned'])
        cleared = self.write('PATCH', f'/v1/bots/{bot["id"]}', {'pinned': False})
        self.assertFalse(cleared['pinned'])

    def test_existing_bot_pin_migrates_to_false_without_other_changes(self):
        bot = self.bot()
        with self.service.transaction():
            existing = self.service._get('bots', bot['id'])
            existing.pop('pinned')
            self.service._put('bots', existing)
        self.service.close()
        self.service = Service(self.temp.name, ControlledProvider(), start_workers=False)
        migrated = self.service.snapshot()['bots'][0]
        self.assertFalse(migrated['pinned'])
        self.assertEqual({k: v for k, v in migrated.items() if k != 'pinned'}, existing)

    def test_bot_pin_create_and_type_validation(self):
        bot = self.write('POST', '/v1/bots', {'name': 'Pinned bot', 'pinned': True})
        self.assertTrue(bot['pinned'])
        for value in ('true', 1, None):
            with self.assertRaises(ServiceError):
                self.write('PATCH', f'/v1/bots/{bot["id"]}', {'pinned': value})
            with self.assertRaises(ServiceError):
                self.write('POST', '/v1/bots', {'name': 'Invalid', 'pinned': value})
        self.assertTrue(self.service.snapshot()['bots'][0]['pinned'])

    def test_receipt_preserves_authenticated_device_name(self):
        pair = self.service.pair({'code': self.service.pairing_code(), 'device_name': 'Test iPhone'})
        device_id = self.service.authenticate(pair['token'])
        decision = self.pending()
        with self.service.transaction():
            previous = self.service._get('decisions', decision['id'])
            previous.pop('decided_by_name', None)
            self.service._put('decisions', previous)
        reply = self.service.mutate('POST', f'/v1/decisions/{decision["id"]}/answer', {'choice': 'approve', 'revision': 1, 'decided_by_name': 'Untrusted request name'}, str(uuid4()), device_id)
        self.assertEqual(reply['decided_by'], device_id)
        self.assertEqual(reply['decided_by_name'], 'Test iPhone')
        with self.service.transaction():
            self.service.db.execute('UPDATE devices SET name=? WHERE id=?', ('Renamed iPhone', device_id))
        self.service.close()
        self.service = Service(self.temp.name, ControlledProvider(), start_workers=False)
        receipt = self.service.snapshot()['decisions'][0]
        self.assertEqual(receipt['decided_by_name'], 'Test iPhone')
        answer = self.service._get('decisions', decision['id'])['_answers'][0]
        self.assertEqual(answer['decided_by_name'], 'Test iPhone')

    def test_system_receipts_use_readable_actor_names(self):
        cancelled = self.pending()
        self.write('POST', f'/v1/tasks/{cancelled["task_id"]}/cancel')
        receipt = self.service.snapshot()['decisions'][0]
        self.assertEqual(receipt['decided_by'], 'cancellation')
        self.assertEqual(receipt['decided_by_name'], 'Task cancellation')
    def test_generic_completion_preserves_result_and_full_bot_preview(self):
        def stream_result(task, bot, history, emit):
            emit('text_delta', {'text': 'The report includes '})
            emit('text_delta', {'text': 'three verified sources.'})
            emit('completed', {'summary': 'Ready to review.'})
        self.provider.start_task = stream_result
        task = self.send(self.bot(), 'Read the sources')
        self.service._run_task(task['id'])
        state = self.service.snapshot()
        self.assertEqual(state['bots'][0]['last_message'], 'The report includes three verified sources.')
        self.assertEqual(state['tasks'][0]['summary'], 'Ready to review.')
        self.assertEqual(state['tasks'][0]['status'], 'completed')
        self.assertFalse(any(m['text'] == 'Ready to review.' for m in state['messages']))
        self.assertEqual(len([m for m in state['messages'] if m['role'] == 'assistant' and m['kind'] == 'text']), 1)
        self.assertTrue(any(e['type'] == 'task.updated' and e['data']['status'] == 'completed' for e in self.service.events(0)['events']))

    def test_generic_completion_without_result_uses_activity(self):
        self.provider.start_task = lambda task, bot, history, emit: emit('completed', {'summary': 'Ready to review.'})
        task = self.send(self.bot(), 'Read this')
        self.service._run_task(task['id'])
        completion = next(m for m in self.service.snapshot()['messages'] if m['text'] == 'Ready to review.')
        self.assertEqual(completion['kind'], 'activity')

    def test_specific_completion_summary_remains_visible(self):
        self.provider.start_task = lambda task, bot, history, emit: emit('completed', {'summary': 'The report includes three verified sources.'})
        task = self.send(self.bot(), 'Read this')
        self.service._run_task(task['id'])
        state = self.service.snapshot()
        completion = next(m for m in state['messages'] if m['text'] == 'The report includes three verified sources.')
        self.assertEqual(completion['kind'], 'text')
        self.assertEqual(state['bots'][0]['last_message'], completion['text'])

    def test_http_authentication_and_idempotency(self):
        import urllib.request
        import urllib.error
        import json
        server = APIServer(('127.0.0.1', 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        try:
            with urllib.request.urlopen(root + '/health') as response:
                self.assertTrue(json.load(response)['ok'])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(root + '/v1/state')
            self.assertEqual(caught.exception.code, 401)
            token = self.service.pair({'code': self.service.pairing_code()})['token']
            headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Idempotency-Key': str(uuid4())}
            request = urllib.request.Request(root + '/v1/bots', data=b'{"name":"Ada"}', headers=headers)
            with urllib.request.urlopen(request) as response:
                first = json.load(response)
            with urllib.request.urlopen(request) as response:
                self.assertEqual(first, json.load(response))
            status = urllib.request.Request(root + '/v1/device/push', headers={'Authorization': f'Bearer {token}'})
            with urllib.request.urlopen(status) as response:
                self.assertEqual(json.load(response)['registered'], False)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
