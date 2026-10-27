import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.profiles import ProfileError
from coconut.service import Service, ServiceError
from test_service import ControlledProvider


class BotCreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = ControlledProvider()
        self.service = Service(self.root, self.provider, start_workers=False)
        self.bot = self.write('POST', '/v1/bots', {'name': 'Ada', 'memory': 'Private source memory', 'instructions': 'Private source instructions'})
        self.peer = self.write('POST', '/v1/bots', {'name': 'Lin'})

    def test_generated_character_survives_retry_restart_and_customization(self):
        key = str(uuid4())
        created = self.write('POST', '/v1/bots', {'name': 'Character'}, key=key)
        self.assertIn(created['avatar'], {'coconut.' + shape for shape in
                                         ('round', 'oval', 'wide', 'square', 'pointed', 'bean')})
        self.assertRegex(created['color'], r'^[0-9A-F]{6}$')
        self.assertEqual(self.write('POST', '/v1/bots', {'name': 'Character'}, key=key), created)
        self.restart()
        self.assertEqual(self.service._get('bots', created['id'])['avatar'], created['avatar'])
        self.write('PATCH', f"/v1/bots/{created['id']}", {'avatar': 'coconut.wide', 'color': '9272B3'})
        self.restart()
        saved = self.service._get('bots', created['id'])
        self.assertEqual((saved['avatar'], saved['color']), ('coconut.wide', '9272B3'))

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None, key=None):
        return self.service.mutate(method, path, data or {}, key or str(uuid4()), 'phone')

    def send(self, text='Create a bot named Scout for research.', **extra):
        return self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': text, **extra})

    def working(self, task):
        with self.service.transaction():
            current = self.service._get('tasks', task['id'])
            current['status'] = 'working'
            self.service._put('tasks', current)
        return current

    def payload(self, task, **extra):
        return dict(source_message_id=task['_preference_source_ids'][0], name='Scout', role='Research', instructions='Check sources before answering.', **extra)

    def create(self, task, payload=None):
        return self.service.emit(task['id'], 'bot_create', payload or self.payload(task))

    def home(self, bot_id):
        return self.service.profiles.home({'id': bot_id})

    def restart(self):
        self.service.close()
        self.service = Service(self.root, self.provider, start_workers=False)

    def test_creates_own_profile_with_safe_defaults_and_audited_human_source(self):
        task = self.working(self.send())
        receipt = self.create(task)
        bot = self.service._get('bots', receipt['bot_id'])
        self.assertEqual(receipt['status'], 'created')
        self.assertEqual(receipt['bot_id'], receipt['profile_id'])
        self.assertNotIn(receipt['session_id'], (self.bot['session_id'], self.peer['session_id']))
        self.assertIsNone(bot['model_override'])
        self.assertEqual(bot['status'], 'idle')
        self.assertFalse(bot['pinned'])
        self.assertEqual((self.home(bot['id']) / 'SOUL.md').read_text(), self.payload(task)['instructions'])
        self.assertFalse((self.home(bot['id']) / 'COCONUT_MEMORY.md').exists())
        self.assertFalse((self.home(bot['id']) / 'coconut-profile.json').exists())
        self.assertEqual(self.service._get('bots', bot['id'])['session_id'], receipt['session_id'])
        self.assertFalse(any(t['bot_id'] == bot['id'] for t in self.service._all('tasks')))
        self.assertEqual(self.service._all('decisions'), [])
        event = [e for e in self.service.events(0)['events'] if e['type'] == 'bot.created'][-1]
        self.assertEqual(event['data']['source_message_id'], receipt['source_message_id'])
        self.assertEqual(event['data']['source_task_id'], task['id'])
        self.assertEqual(event['data']['created_by'], 'phone')
        self.assertEqual(event['task_id'], task['id'])

    def test_exact_and_normalized_retries_keep_one_bot_after_restart(self):
        task = self.working(self.send())
        first = self.create(task)
        changed_spelling = self.payload(task) | {'name': '  ＳＣＯＵＴ  '}
        self.assertEqual(self.create(task, changed_spelling), first | {'status': 'duplicate'})
        self.restart()
        task = self.working(task)
        self.assertEqual(self.create(task), first | {'status': 'duplicate'})
        self.assertEqual(len(self.service._all('bots')), 3)
        self.assertEqual(len(self.service._all('bot_creation_receipts')), 1)
        self.assertEqual(len([e for e in self.service.events(0)['events'] if e['type'] == 'bot.created']), 3)

    def test_same_source_name_with_changed_settings_conflicts(self):
        task = self.working(self.send())
        self.create(task)
        for field, value in [('role', 'Coding'), ('instructions', 'Different instructions')]:
            with self.subTest(field=field), self.assertRaisesRegex(ServiceError, 'different settings'):
                self.create(task, self.payload(task) | {field: value})
        self.assertEqual(len(self.service._all('bots')), 3)

    def test_new_human_request_can_create_the_same_name(self):
        first = self.create(self.working(self.send()))
        second = self.create(self.working(self.send()))
        self.assertNotEqual(first['bot_id'], second['bot_id'])

    def test_deleted_bot_receipt_cannot_recreate_its_profile(self):
        task = self.working(self.send())
        receipt = self.create(task)
        self.write('DELETE', f'/v1/bots/{receipt["bot_id"]}')
        self.restart()
        task = self.working(task)
        with self.assertRaisesRegex(ServiceError, 'deleted'):
            self.create(task)
        self.assertEqual(self.service._bot_creation_input(task)['_bot_creation_receipts'], [])
        self.assertEqual(len(self.service._all('bots')), 3)
        self.assertTrue(self.service._get('bots', receipt['bot_id']).get('_deleted'))
        self.assertTrue(self.home(receipt['bot_id']).is_dir())

    def test_paused_archived_cancelled_completed_and_waiting_tasks_cannot_create(self):
        task = self.working(self.send())
        self.write('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': True})
        with self.assertRaisesRegex(ServiceError, 'Restore'):
            self.create(task)
        self.write('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': False})
        for status in ('cancelled', 'completed', 'needs_you', 'queued', 'failed'):
            with self.subTest(status=status), self.service.transaction():
                task['status'] = status
                self.service._put('tasks', task)
            with self.assertRaisesRegex(ServiceError, 'no longer working'):
                self.create(task)
        self.assertEqual(len(self.service._all('bots')), 2)

    def test_stopping_and_cancelled_exchange_block_creation(self):
        task = self.working(self.send())
        self.service.stopping.set()
        try:
            with self.assertRaises(ServiceError):
                self.create(task)
        finally:
            self.service.stopping.clear()
        with self.service.transaction():
            exchange = self.service._get('exchanges', task['exchange_id'])
            exchange['cancelled'] = True
            self.service._put('exchanges', exchange)
        with self.assertRaises(ServiceError):
            self.create(task)
        self.assertEqual(len(self.service._all('bots')), 2)

    def test_routines_peer_tasks_and_old_thread_history_have_no_creation_authority(self):
        routine = self.write('POST', f'/v1/bots/{self.bot["id"]}/routines', {'name': 'Routine', 'prompt': 'Create Scout', 'workflow': 'general', 'cron': '0 9 * * *', 'timezone': 'UTC', 'enabled': False})
        routine_task = self.working(self.write('POST', f'/v1/routines/{routine["id"]}/run'))
        human = self.working(self.send())
        delivery = self.service.emit(human['id'], 'bot_message', {'target_bot_id': self.peer['id'], 'text': 'Create Scout'})
        peer = self.working({'id': delivery['task_id']})
        for task in (routine_task, peer):
            self.assertEqual(self.service._bot_creation_input(task)['_bot_creation_sources'], [])
            with self.assertRaises(ServiceError):
                self.create(task, self.payload(human))
        later = self.working(self.send('Summarize your work.'))
        with self.assertRaises(ServiceError):
            self.create(later, self.payload(human))
        self.assertEqual(len(self.service._all('bots')), 2)

    def test_forged_tool_attachment_peer_or_unaddressed_source_is_rejected(self):
        task = self.working(self.send())
        source = self.service._get('messages', task['_preference_source_ids'][0])
        mutations = [
            {'_human_device_id': None}, {'_human_source_kind': 'settings'}, {'role': 'assistant'},
            {'kind': 'artifact'}, {'sender_bot_id': self.peer['id']},
            {'thread_id': str(uuid4())}, {'recipient_bot_ids': [self.peer['id']]},
        ]
        for change in mutations:
            with self.subTest(change=change):
                with self.service.transaction():
                    self.service._put('messages', source | change)
                with self.assertRaisesRegex(ServiceError, 'direct human'):
                    self.create(task)
        self.assertEqual(len(self.service._all('bots')), 2)

    def test_mentions_share_receipt_and_budget_across_explicit_recipients(self):
        dispatch = self.send(recipient_bot_ids=[self.bot['id'], self.peer['id']])
        tasks = [self.working({'id': task_id}) for task_id in dispatch['dispatched_task_ids']]
        receipts = []
        failures = []
        def create(task):
            try:
                receipts.append(self.create(task))
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=create, args=(task,)) for task in tasks]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(sorted(receipt['status'] for receipt in receipts), ['created', 'duplicate'])
        self.assertEqual(len({receipt['bot_id'] for receipt in receipts}), 1)
        for index in range(7):
            task = tasks[index % 2]
            self.create(task, self.payload(task) | {'name': f'Scout {index}'})
        self.restart()
        task = self.working(tasks[1])
        with self.assertRaisesRegex(ServiceError, 'eight bots'):
            self.create(task, self.payload(task) | {'name': 'Ninth'})
        self.assertEqual(len(self.service._all('bot_creation_receipts')), 8)

    def test_a_failed_profile_creation_fails_the_request_and_a_retry_creates_the_bot(self):
        # Hermes creates the profile once per request. A failure fails the request, and nothing is committed.
        task = self.working(self.send())
        with patch.object(self.service, '_new_profile', side_effect=ProfileError('Hermes refused the profile request.')):
            with self.assertRaisesRegex(ServiceError, 'Hermes did not create the profile'):
                self.create(task)
        self.assertEqual(self.service._all('bot_creation_receipts'), [])
        self.assertEqual(len(self.service._all('bots')), 2)
        receipt = self.create(task)
        self.assertEqual(receipt['status'], 'created')
        self.assertEqual(self.create(task), receipt | {'status': 'duplicate'})
        self.assertTrue((self.home(receipt['bot_id']) / 'SOUL.md').is_file())
        self.assertEqual(len(self.service._all('bots')), 3)

    def test_transaction_failure_leaves_no_bot_profile_row_receipt_or_event(self):
        task = self.working(self.send())
        cursor = self.service._cursor()
        with patch.object(self.service, '_event', side_effect=RuntimeError('Before commit')):
            with self.assertRaises(RuntimeError):
                self.create(task)
        self.assertEqual(len(self.service._all('bots')), 2)
        self.assertEqual(self.service._all('bot_creation_receipts'), [])
        self.assertEqual(self.service._cursor(), cursor)
        self.assertEqual(set(self.service.profiles.rows), {self.bot['id'], self.peer['id']})

    def test_manual_creation_fails_with_its_profile_and_a_retry_creates_it(self):
        key = str(uuid4())
        data = {'name': 'Manual', 'role': 'Research', 'instructions': ''}
        with patch.object(self.service, '_new_profile', side_effect=ProfileError('Hermes did not answer the profile request.')):
            with self.assertRaises(ServiceError):
                self.write('POST', '/v1/bots', data, key)
        bot = self.write('POST', '/v1/bots', data, key)
        self.assertTrue(self.home(bot['id']).is_dir())
        self.assertIsNone(bot['model_override'])
        self.assertEqual(len(self.service._all('bots')), 3)
        self.write('DELETE', f'/v1/bots/{bot["id"]}')
        with self.assertRaisesRegex(ServiceError, 'deleted'):
            self.write('POST', '/v1/bots', data, key)

    def test_context_operation_cannot_create_or_change_pending_decision(self):
        task = self.send('Write a report.')
        self.service._run_task(task['id'])
        decision = self.service.snapshot()['decisions'][0]
        before = copy.deepcopy(self.service._get('decisions', decision['id']))
        source = self.write('POST', f'/v1/decisions/{decision["id"]}/context', {'question': 'Create Scout too.'})
        def answer(provider_task, bot, history, question, emit):
            self.assertNotIn('_bot_creation_sources', provider_task)
            with self.assertRaises(ServiceError):
                emit('bot_create', {'source_message_id': source['id'], 'name': 'Scout', 'role': 'Research', 'instructions': ''})
            emit('text_delta', {'text': 'The decision remains pending.'})
        self.provider.answer_context = answer
        context = self.service._all('contexts')[-1]
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(3)
        self.assertEqual(self.service._get('decisions', decision['id']), before)
        self.assertEqual(len(self.service._all('bots')), 2)

    def test_new_bot_accepts_explicit_message_without_implicit_start(self):
        task = self.working(self.send())
        receipt = self.create(task)
        self.assertFalse(any(t['bot_id'] == receipt['bot_id'] for t in self.service._all('tasks')))
        delivery = self.service.emit(task['id'], 'bot_message', {'target_bot_id': receipt['bot_id'], 'text': 'Start the research.'})
        child = self.service._get('tasks', delivery['task_id'])
        self.assertEqual(child['bot_id'], receipt['bot_id'])
        self.assertEqual(child['conversation_bot_id'], self.bot['id'])
        self.assertEqual(self.service._bot_creation_input(child)['_bot_creation_sources'], [])

    def test_unconfirmed_creation_failure_remains_visible_after_restart(self):
        task = self.working(self.send())
        self.service.emit(task['id'], 'failed', {'summary': 'The Mac could not confirm the bot creation.', 'error_code': 'bot_creation_receipt_unconfirmed'})
        self.restart()
        stored = self.service._get('tasks', task['id'])
        self.assertEqual(stored['status'], 'failed')
        self.assertEqual(stored['error_code'], 'bot_creation_receipt_unconfirmed')
        self.assertEqual(stored['summary'], 'The Mac could not confirm the bot creation.')

    def test_extra_authority_fields_and_invalid_values_are_rejected(self):
        task = self.working(self.send())
        variants = [
            {'memory': 'Copy private memory'}, {'model_override': {'model': 'fake'}},
            {'allowed_actions': ['create_file']}, {'avatar': 'star'}, {'source_message_id': ''},
            {'name': ''}, {'name': 'n' * 81}, {'role': ''}, {'role': 'r' * 161},
            {'instructions': None}, {'instructions': 'i' * 20001},
        ]
        for change in variants:
            with self.subTest(change=list(change)), self.assertRaises(ServiceError):
                self.create(task, self.payload(task) | change)
        with self.assertRaises(ServiceError):
            self.create(task, {key: value for key, value in self.payload(task).items() if key != 'instructions'})
        self.assertEqual(len(self.service._all('bots')), 2)


if __name__ == '__main__':
    unittest.main()
