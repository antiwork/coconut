import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import Service, ServiceError
from test_service import ControlledProvider


class BotDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = ControlledProvider()
        self.service = Service(self.root, self.provider, start_workers=False)
        self.caller = self.bot('Coordinator')
        self.target = self.bot('Research')
        self.sibling = self.bot('Arithmetic')

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None, key=None):
        return self.service.mutate(method, path, data or {}, key or str(uuid4()), 'phone')

    def bot(self, name):
        return self.write('POST', '/v1/bots', {'name': name, 'role': name, 'instructions': 'Private instructions', 'memory': 'Private memory'})

    def send(self, bot=None, text='Delete Research.', **extra):
        return self.write('POST', f'/v1/bots/{(bot or self.caller)["id"]}/messages', {'text': text, 'mode': 'queue', **extra})

    def working(self, task):
        with self.service.transaction():
            current = self.service._get('tasks', task['id'])
            current['status'] = 'working'
            self.service._put('tasks', current)
        return current

    def payload(self, task, target=None):
        target = target or self.target
        return dict(source_message_id=task['_preference_source_ids'][0], target_bot_id=target['id'], expected_name=target['name'])

    def delete(self, task, payload=None):
        return self.service.emit(task['id'], 'bot_delete', payload or self.payload(task))

    def profile(self, bot=None):
        return self.service.profiles.home(bot or self.target)

    def status(self, bot=None):
        # Coconut keeps no status file in the profile. The database records the bot's state.
        row = self.service._get('bots', (bot or self.target)['id'])
        return 'deleted' if row.get('_deleted') else 'archived' if row.get('archived') else 'active'

    def restart(self):
        self.service.close()
        self.provider = ControlledProvider()
        self.service = Service(self.root, self.provider, start_workers=False)

    def deliver(self, source, target):
        receipt = self.service.emit(source['id'], 'bot_message', {'target_bot_id': target['id'], 'text': 'Check the report.'})
        return self.working(self.service._get('tasks', receipt['task_id']))

    def test_any_bot_deletes_another_bot_with_profile_and_human_attribution(self):
        task = self.working(self.send(self.sibling))
        receipt = self.delete(task)
        self.assertEqual(receipt, dict(status='deleted', bot_id=self.target['id'], profile_id=self.target['id'],
                                      name='Research', source_message_id=task['_preference_source_ids'][0], cancelled_task_ids=[]))
        self.assertNotIn(self.target['id'], {bot['id'] for bot in self.service.snapshot()['bots']})
        self.assertEqual(self.status(), 'deleted')
        self.assertEqual((self.profile() / 'SOUL.md').read_text(), 'Private instructions')
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'working')
        event = next(row for row in self.service.events(0)['events'] if row['type'] == 'bot.deleted')
        self.assertEqual(event['data']['deleted_by'], 'phone')
        self.assertEqual(event['data']['source_message_id'], receipt['source_message_id'])
        self.assertEqual(event['task_id'], task['id'])
        self.assertEqual(self.service._all('decisions'), [])

    def test_deletion_cancels_target_and_descendants_but_keeps_caller_and_sibling_working(self):
        caller = self.working(self.send())
        target = self.deliver(caller, self.target)
        independent = self.deliver(caller, self.sibling)
        descendant = self.deliver(target, self.sibling)
        direct = self.working(self.send(self.target))
        receipt = self.delete(caller)
        cancelled = {target['id'], descendant['id'], direct['id']}
        self.assertEqual(set(receipt['cancelled_task_ids']), cancelled)
        self.assertEqual(set(self.provider.cancelled), cancelled)
        for task_id in cancelled:
            self.assertEqual(self.service._get('tasks', task_id)['status'], 'cancelled')
        for task in (caller, independent):
            self.assertEqual(self.service._get('tasks', task['id'])['status'], 'working')
        self.assertFalse(self.service._get('exchanges', caller['exchange_id']).get('cancelled', False))
        self.service.emit(independent['id'], 'completed', {'summary': 'Still works.'})
        self.assertEqual(self.service._get('tasks', independent['id'])['status'], 'completed')

    def test_deletion_stops_pending_decision_context_and_routine(self):
        target = self.send(self.target, 'Write a report')
        self.service._run_task(target['id'])
        decision = self.service._all('decisions')[0]
        with patch.object(self.service, '_start_context'):
            self.write('POST', f'/v1/decisions/{decision["id"]}/context', {'question': 'What changes?'})
        context = self.service._all('contexts')[0]
        routine = self.write('POST', f'/v1/bots/{self.target["id"]}/routines',
                             {'name': 'Daily', 'prompt': 'Check', 'workflow': 'general', 'cron': '* * * * *', 'timezone': 'UTC'})
        self.delete(self.working(self.send()))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'rejected')
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'cancelled')
        saved = self.service._get('routines', routine['id'])
        self.assertFalse(saved['enabled'])
        self.assertIsNone(saved['next_run_at'])
        self.restart()
        self.assertFalse(self.service._get('routines', routine['id'])['enabled'])
        self.assertEqual(self.service._get('tasks', target['id'])['status'], 'cancelled')

    def test_retry_after_restart_never_deletes_a_same_name_replacement(self):
        task = self.working(self.send())
        receipt = self.delete(task)
        replacement = self.bot('Research')
        self.restart()
        task = self.working(task)
        self.assertEqual(self.delete(task), receipt | {'status': 'duplicate'})
        self.assertIn(replacement['id'], {bot['id'] for bot in self.service.snapshot()['bots']})
        self.assertEqual(self.status(replacement), 'active')
        self.assertEqual(len(self.service._all('bot_deletion_receipts')), 1)
        self.assertEqual(len([row for row in self.service.events(0)['events'] if row['type'] == 'bot.deleted']), 1)
        self.assertEqual(self.service._bot_deletion_input(task)['_bot_deletion_receipts'], [receipt])

    def test_rename_rejects_stale_target_name_before_any_effect(self):
        task = self.working(self.send())
        payload = self.payload(task)
        self.write('PATCH', f'/v1/bots/{self.target["id"]}', {'name': 'Changed'})
        with self.assertRaisesRegex(ServiceError, 'changed its name'):
            self.delete(task, payload)
        self.assertEqual(self.service._all('bot_deletion_receipts'), [])
        self.assertFalse(self.service._get('bots', self.target['id']).get('_deleted', False))

    def test_self_and_current_conversation_cannot_be_deleted(self):
        task = self.working(self.send(recipient_bot_ids=[self.sibling['id']]))
        for target in (self.sibling, self.caller):
            with self.subTest(target=target['name']), self.assertRaisesRegex(ServiceError, 'another conversation'):
                self.delete(task, self.payload(task, target))
        self.assertEqual(len(self.service.snapshot()['bots']), 3)

    def test_task_cannot_delete_its_dependency_and_cancel_itself(self):
        target = self.working(self.send(self.sibling, recipient_bot_ids=[self.target['id']]))
        caller = self.deliver(target, self.caller)
        with self.service.transaction():
            message = self.service._message(self.caller['id'], 'Delete Research.', task_id=caller['id'], role='user', recipient_bot_ids=[self.caller['id']])
            message.update(_human_device_id='phone', _human_source_kind='chat')
            self.service._put('messages', message)
            caller['_preference_source_ids'] = [message['id']]
            self.service._put('tasks', caller)
        with self.assertRaisesRegex(ServiceError, 'depends on that bot'):
            self.delete(caller)
        self.assertEqual(self.provider.cancelled, [])

    def test_foreign_source_and_peer_source_cannot_delete(self):
        human = self.working(self.send())
        other = self.working(self.send(self.sibling))
        with self.assertRaises(ServiceError):
            self.delete(other, self.payload(human))
        peer = self.deliver(human, self.sibling)
        with self.assertRaises(ServiceError):
            self.delete(peer, self.payload(human))
        self.assertEqual(self.service._bot_deletion_input(peer)['_bot_deletion_sources'], [])
        self.assertEqual(len(self.service.snapshot()['bots']), 3)

    def test_routine_and_forged_human_source_cannot_delete(self):
        task = self.working(self.send())
        for changes in ({'_human_device_id': None}, {'_human_source_kind': 'routine'},
                        {'sender_bot_id': self.target['id']}, {'recipient_bot_ids': [self.target['id']]}):
            source = self.service._get('messages', task['_preference_source_ids'][0])
            with self.service.transaction():
                self.service._put('messages', source | changes)
            with self.subTest(changes=changes), self.assertRaises(ServiceError):
                self.delete(task)
            with self.service.transaction():
                self.service._put('messages', source)
        self.assertEqual(self.service._all('bot_deletion_receipts'), [])

    def test_inactive_paused_and_cancelled_callers_cannot_delete(self):
        task = self.working(self.send())
        for status in ('queued', 'completed', 'cancelled', 'needs_you', 'paused'):
            with self.service.transaction():
                self.service._put('tasks', task | {'status': status})
            with self.subTest(status=status), self.assertRaises(ServiceError):
                self.delete(task)
        self.working(task)
        self.write('PATCH', f'/v1/bots/{self.caller["id"]}', {'archived': True})
        with self.assertRaises(ServiceError):
            self.delete(task)

    def test_directory_includes_archived_contacts_and_excludes_private_data(self):
        self.write('PATCH', f'/v1/bots/{self.target["id"]}', {'archived': True})
        task = self.working(self.send())
        directory = self.service._bot_deletion_input(task)['_deletable_bots']
        self.assertTrue(next(bot for bot in directory if bot['id'] == self.target['id'])['archived'])
        self.assertTrue(all(set(bot) == {'id', 'name', 'role', 'archived'} for bot in directory))
        self.assertNotIn('Private', json.dumps(directory))
        self.assertEqual(self.delete(task)['status'], 'deleted')

    def test_profile_failure_commits_once_and_exact_retry_finishes_cleanup(self):
        task = self.working(self.send())
        with patch.object(self.service.profiles, 'sync', side_effect=OSError('Disk failure')):
            with self.assertRaisesRegex(ServiceError, 'cleanup is unconfirmed'):
                self.delete(task)
        self.assertEqual(len(self.service._all('bot_deletion_receipts')), 1)
        # The database committed the deletion once. Only the cleanup waits for the retry.
        self.assertEqual(self.status(), 'deleted')
        self.assertEqual(self.delete(task)['status'], 'duplicate')
        self.assertEqual(self.status(), 'deleted')

    def test_restart_reconciles_profile_after_commit_before_cleanup(self):
        task = self.working(self.send())
        with patch.object(self.service, '_finish_bot_deletion', side_effect=RuntimeError('Crash after commit')):
            with self.assertRaises(RuntimeError):
                self.delete(task)
        self.restart()
        self.assertEqual(self.status(), 'deleted')
        self.assertEqual(self.delete(self.working(task))['status'], 'duplicate')

    def test_cancel_failure_is_not_a_success_receipt_and_retry_stops_work(self):
        target = self.working(self.send(self.target))
        caller = self.working(self.send())
        with patch.object(self.provider, 'cancel_task', side_effect=RuntimeError('Stop failed')):
            with self.assertRaisesRegex(ServiceError, 'cleanup is unconfirmed'):
                self.delete(caller)
        self.assertEqual(self.delete(caller)['status'], 'duplicate')
        self.assertIn(target['id'], self.provider.cancelled)

    def test_process_cancellation_runs_after_commit_without_database_lock(self):
        target = self.working(self.send(self.target))
        observations = []
        def cancel(task_id):
            def inspect():
                with self.service.lock:
                    observations.append((task_id, self.service._get('tasks', target['id'])['status'],
                                         len(self.service._all('bot_deletion_receipts'))))
            worker = threading.Thread(target=inspect)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive(), 'Cancellation must not hold the service lock.')
        with patch.object(self.provider, 'cancel_task', side_effect=cancel):
            self.delete(self.working(self.send()))
        self.assertEqual(observations, [(target['id'], 'cancelled', 1)])

    def test_failed_transaction_has_no_profile_process_or_receipt_effect(self):
        target = self.working(self.send(self.target))
        caller = self.working(self.send())
        with patch.object(self.service, '_event', side_effect=RuntimeError('Transaction failed')):
            with self.assertRaises(RuntimeError):
                self.delete(caller)
        self.assertEqual(self.service._get('tasks', target['id'])['status'], 'working')
        self.assertEqual(self.provider.cancelled, [])
        self.assertEqual(self.service._all('bot_deletion_receipts'), [])
        self.assertEqual(self.status(), 'active')

    def test_manual_delete_retry_also_reconciles_cleanup(self):
        key = str(uuid4())
        path = f'/v1/bots/{self.target["id"]}'
        with patch.object(self.service.profiles, 'sync', side_effect=OSError('Disk failure')):
            with self.assertRaises(ServiceError):
                self.write('DELETE', path, key=key)
        self.assertEqual(self.write('DELETE', path, key=key), {'ok': True})
        self.assertEqual(self.status(), 'deleted')

    def test_two_recipients_share_one_deletion_receipt(self):
        sent = self.send(recipient_bot_ids=[self.caller['id'], self.sibling['id']])
        tasks = [self.working(self.service._get('tasks', identifier)) for identifier in sent['dispatched_task_ids']]
        first, second = [self.delete(task) for task in tasks]
        self.assertEqual(second, first | {'status': 'duplicate'})
        self.assertEqual(len(self.service._all('bot_deletion_receipts')), 1)

    def test_deletion_waits_for_target_profile_preparation_before_confirmation(self):
        self.working(self.send(self.target))
        caller = self.working(self.send())
        bot_lock = self.service.bot_locks.setdefault(self.target['id'], threading.Lock())
        cancelled, finished = threading.Event(), threading.Event()
        receipts, errors = [], []
        def run():
            try:
                receipts.append(self.delete(caller))
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        bot_lock.acquire()
        with patch.object(self.provider, 'cancel_task', side_effect=lambda _: cancelled.set()):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(cancelled.wait(2))
                self.assertFalse(finished.wait(.05), 'Deletion must wait for the target worker to release its profile.')
                self.service.profiles.sync(self.target)
            finally:
                bot_lock.release()
                worker.join(2)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(receipts[0]['status'], 'deleted')
        self.assertEqual(self.status(), 'deleted')

    def test_cancellation_attempts_other_workers_even_when_one_stop_fails(self):
        first = self.working(self.send(self.target))
        second = self.working(self.send(self.target))
        caller = self.working(self.send())
        calls = []
        def cancel(task_id):
            calls.append(task_id)
            if len(calls) == 1:
                raise RuntimeError('First stop failed')
        with patch.object(self.provider, 'cancel_task', side_effect=cancel):
            with self.assertRaisesRegex(ServiceError, 'cleanup is unconfirmed'):
                self.delete(caller)
        self.assertEqual(set(calls), {first['id'], second['id']})

    def test_completed_target_has_no_pending_return_after_deletion(self):
        caller = self.working(self.send())
        target = self.deliver(caller, self.target)
        self.service.emit(target['id'], 'text_delta', {'text': 'A completed result.'})
        self.service.emit(target['id'], 'completed', {'summary': 'Done.'})
        self.assertIn('_return_pending', self.service._get('tasks', target['id']))
        self.delete(caller)
        self.assertNotIn('_return_pending', self.service._get('tasks', target['id']))

    def test_human_source_bounds_deletions_across_recipients(self):
        sent = self.send(recipient_bot_ids=[self.caller['id'], self.sibling['id']])
        tasks = [self.working(self.service._get('tasks', identifier)) for identifier in sent['dispatched_task_ids']]
        for index in range(8):
            target = self.bot(f'Temporary {index}')
            self.delete(tasks[index % 2], self.payload(tasks[index % 2], target))
        with self.assertRaisesRegex(ServiceError, 'eight bots'):
            self.delete(tasks[0])
        self.assertIn(self.target['id'], {bot['id'] for bot in self.service.snapshot()['bots']})


if __name__ == '__main__':
    unittest.main()
