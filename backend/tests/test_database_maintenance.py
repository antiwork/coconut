from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut import database_maintenance
from coconut.log_rotation import rotate_log
from coconut.providers import FixtureProvider
from coconut.service import Service

NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc).timestamp()
OLD = '2026-09-10T12:00:00Z'
RECENT = '2026-09-28T11:00:00Z'


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'phone')

    def event(self, kind, data=None, task_id=None, created_at=OLD):
        with self.service.transaction():
            self.service._event(kind, data or {}, self.bot['id'], task_id)
            seq = self.service._cursor()
            self.service.db.execute('UPDATE events SET created_at=? WHERE seq=?', (created_at, seq))
        return seq

    def seqs(self):
        return [row[0] for row in self.service.db.execute('SELECT seq FROM events ORDER BY seq')]


class EventRetentionTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.service.db.execute('DELETE FROM events')
        for name, value in (('EVENT_MINIMUM_ROWS', 2), ('BATCH_ROWS', 3)):
            patcher = patch.object(database_maintenance, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_old_events_go_and_recent_events_and_the_cursor_stay(self):
        [self.event('message.updated') for _ in range(7)]
        recent = [self.event('message.updated', created_at=RECENT) for _ in range(3)]
        cursor = self.service._cursor()
        self.assertEqual(self.service._prune_events(NOW), 7)
        self.assertEqual(self.seqs(), recent)
        self.assertEqual(self.service._cursor(), cursor)
        # The sequence continues after the deleted events.
        self.assertEqual(self.event('message.updated', created_at=RECENT), cursor + 1)

    def test_the_newest_events_stay_at_any_age(self):
        seqs = [self.event('message.updated') for _ in range(5)]
        self.service._prune_events(NOW)
        self.assertEqual(self.seqs(), seqs[-2:])

    def test_lookups_keep_the_old_events_that_they_read(self):
        task, routine = str(uuid4()), str(uuid4())
        with self.service.transaction():
            self.service._put('tasks', dict(id=routine, bot_id=self.bot['id'], status='completed', routine_id=str(uuid4()),
                                            created_at=OLD, updated_at=OLD))
        kept = [self.event('bots.restarted'), self.event('task.created', {'id': task}, task)]
        self.event('task.updated', {'id': task, 'status': 'working'}, task)
        kept.append(self.event('task.updated', {'id': task, 'status': 'queued'}, task))
        self.event('bot_message.delivered', {'task_id': task}, task)
        kept.append(self.event('bot_message.delivered', {'task_id': task}, task))
        kept.append(self.event('routine.triggered', {'task_id': routine}, routine))
        kept += [self.event('task.updated', {'id': routine, 'status': status}, routine) for status in ('failed', 'completed')]
        kept.append(self.event('decision.updated', {'id': 'card', 'status': 'dismissed', 'inbox_swipe_id': 'swipe'}, task))
        self.event('decision.updated', {'id': 'card', 'status': 'dismissed'}, task)
        self.event('message.updated')
        recent = [self.event('message.updated', created_at=RECENT) for _ in range(2)]
        self.service._prune_events(NOW)
        self.assertEqual(self.seqs(), kept + recent)

    def test_a_phone_behind_the_deleted_events_gets_a_full_snapshot(self):
        after = self.event('bot.read')
        [self.event('bot.read') for _ in range(4)]
        [self.event('bot.read', created_at=RECENT) for _ in range(2)]
        self.assertNotIn('reset', self.service.updates(after, collection_deltas=True))
        self.service._prune_events(NOW)
        page = self.service.updates(after, collection_deltas=True)
        self.assertEqual(page['reset']['cursor'], self.service._cursor())


class ReceiptRetentionTests(ServiceCase):
    def task(self, status='completed', updated_at=OLD, **fields):
        task = dict(id=str(uuid4()), bot_id=self.bot['id'], title='Work', status=status, summary='',
                    created_at=updated_at, updated_at=updated_at, _execution_session_id=str(uuid4())) | fields
        with self.service.transaction():
            self.service._put('tasks', task)
        return task

    def receipt(self, task, name='read_file', state='completed', result='{"content":"text"}', **fields):
        record = dict(id=str(uuid4()), task_id=task['id'], turn_id=str(uuid4()), action={'name': name, 'arguments': {}},
                      action_hash='hash', state=state, created_at=OLD, completed_at=OLD, result=result) | fields
        with self.service.transaction():
            self.service._put('native_receipts', record)
        return record['id']

    def remaining(self):
        return {row[0] for row in self.service.db.execute('SELECT id FROM native_receipts')}

    def test_an_idle_outcome_loses_its_receipts_except_the_named_ones(self):
        idle, other = self.task(), self.task()
        gone = self.receipt(idle)
        kept = {self.receipt(idle, state='executing', result=None),
                self.receipt(idle, 'kanban_create'), self.receipt(idle, 'cronjob_manage')}
        decided, carried, waited = self.receipt(idle), self.receipt(idle), self.receipt(idle)
        kept |= {decided, carried, waited}
        with self.service.transaction():
            self.service._put('decisions', dict(id=str(uuid4()), task_id=other['id'], bot_id=self.bot['id'],
                                                status='approved', _receipt_id=decided))
            self.service._put('tasks', dict(other, _continuation_receipt_ids=[carried]))
        # A pending receipt, here a wait, keeps every receipt of its task. It also names the receipt that it waits for.
        waiter = self.task()
        kept |= {self.receipt(waiter, 'process', _wait_process_receipt=waited), self.receipt(waiter)}
        self.assertEqual(self.service._prune_receipts(NOW), 1)
        self.assertEqual(self.remaining(), kept)
        self.assertNotIn(gone, self.remaining())

    def test_live_recent_and_pending_work_keeps_its_receipts(self):
        control = self.receipt(self.task())
        shared = self.task()
        self.task(status='queued', _execution_session_id=shared['_execution_session_id'])
        recent = self.task(updated_at='2026-09-27T12:00:00Z')
        replying, asking, scoped, running = self.task(), self.task(), self.task(), self.task()
        owner = self.task(status='needs_you')
        with self.service.transaction():
            self.service._put('contexts', dict(id=str(uuid4()), task_id=replying['id'], status='working'))
            self.service._put('decisions', dict(id=str(uuid4()), task_id=asking['id'], bot_id=self.bot['id'], status='pending'))
            self.service._put('decisions', dict(id=str(uuid4()), task_id=owner['id'], bot_id=self.bot['id'], status='pending'))
        kept = {self.receipt(t) for t in (shared, recent, replying, asking, scoped, running)}
        scope = lambda decision, *args: ({scoped['id']} if decision['task_id'] == owner['id'] else set(), [])
        with patch.object(self.service, '_decision_scope', side_effect=scope), \
                patch.dict(self.service.running, {running['id']: threading.current_thread()}):
            self.service._prune_receipts(NOW)
        self.assertEqual(self.remaining(), kept)
        self.assertNotIn(control, self.remaining())

    def test_a_turn_that_starts_between_batches_keeps_its_receipts(self):
        first, second = self.task(), self.task()
        gone, kept = self.receipt(first), self.receipt(second)
        wait = self.service.stopping.wait
        def resume(timeout):
            with self.service.transaction():
                self.service._put('tasks', dict(self.service._get('tasks', second['id']), status='queued'))
            return wait(timeout)
        with patch.object(database_maintenance, 'BATCH_ROWS', 1), patch.object(self.service.stopping, 'wait', side_effect=resume):
            self.service._prune_receipts(NOW)
        self.assertEqual(self.remaining(), {kept})
        self.assertNotIn(gone, self.remaining())

    def between_batches(self, change):
        wait = self.service.stopping.wait
        calls = []
        def pause(timeout):
            if not calls:
                calls.append(True)
                with self.service.transaction():
                    change()
            return wait(timeout)
        with patch.object(database_maintenance, 'BATCH_ROWS', 1), patch.object(self.service.stopping, 'wait', side_effect=pause):
            self.service._prune_receipts(NOW)

    def test_a_decision_that_opens_between_batches_stops_the_pass(self):
        first, second, other = self.task(), self.task(), self.task()
        gone, kept = self.receipt(first), self.receipt(second)
        # The scope of the new decision is unknown to this pass, so no later batch runs.
        self.between_batches(lambda: self.service._put('decisions', dict(
            id=str(uuid4()), task_id=other['id'], bot_id=self.bot['id'], status='pending')))
        self.assertEqual(self.remaining(), {kept})
        self.assertNotIn(gone, self.remaining())

    def test_a_wait_that_starts_between_batches_keeps_the_receipt_it_names(self):
        first, second, waiter = self.task(), self.task(), self.task(status='working')
        gone, waited = self.receipt(first), self.receipt(second)
        def wait_for_it():
            self.service._put('native_receipts', dict(id='wait', task_id=waiter['id'], turn_id='turn', state='completed',
                action={'name': 'process', 'arguments': {}}, action_hash='hash', created_at=RECENT, _wait_process_receipt=waited))
        self.between_batches(wait_for_it)
        self.assertEqual(self.remaining(), {waited, 'wait'})
        self.assertNotIn(gone, self.remaining())


class FreePageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def open(self):
        service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(service.close)
        with service.transaction():
            for _ in range(400):
                service._event('bot.updated', {'instructions': os.urandom(4000).hex()})
        service.db.execute('DELETE FROM events')
        return service

    def pragma(self, service, name):
        return service.db.execute(f'PRAGMA {name}').fetchone()[0]

    def test_a_new_store_frees_its_pages_in_small_steps(self):
        service = self.open()
        self.assertEqual(self.pragma(service, 'auto_vacuum'), 2)
        self.assertGreater(self.pragma(service, 'freelist_count'), 3 * 64)
        with patch.object(database_maintenance, 'VACUUM_PAGES', 64), \
                self.assertNoLogs('coconut.database_maintenance', 'WARNING'):
            self.assertGreater(service._release_free_pages(), 0)
        self.assertEqual(self.pragma(service, 'freelist_count'), 0)

    def test_an_old_store_vacuums_once_when_no_turn_runs(self):
        with sqlite3.connect(Path(self.temp.name) / 'coconut.sqlite3') as db:
            db.execute('CREATE TABLE earlier (id TEXT)')
        service = self.open()
        self.assertEqual(self.pragma(service, 'auto_vacuum'), 0)
        with patch.dict(service.running, {'task': threading.current_thread()}):
            self.assertEqual(service._release_free_pages(), 0)
        self.assertEqual(self.pragma(service, 'auto_vacuum'), 0)
        with self.assertLogs('coconut.database_maintenance', 'WARNING'):
            self.assertGreater(service._release_free_pages(), 0)
        self.assertEqual((self.pragma(service, 'auto_vacuum'), self.pragma(service, 'freelist_count')), (2, 0))


class StartupReadTests(unittest.TestCase):
    def test_startup_parses_the_receipt_journal_only_until_the_process_migration_ran(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        provider = FixtureProvider(temp.name)
        # Relay recovery runs only for a provider with native settings.
        provider.native_settings = lambda operation, bots, **options: None
        service = Service(temp.name, provider, start_workers=False)
        bot = service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'phone')
        task = dict(id=str(uuid4()), bot_id=bot['id'], status='completed')
        # A later start must not repair this receipt again: runtime code registers new receipts.
        receipt = dict(id=str(uuid4()), task_id=task['id'], state='completed', action={'name': 'terminal', 'arguments': {}},
                       result=json.dumps({'session_id': 'proc', 'notify_on_complete': True}))
        with service.transaction():
            service._put('tasks', task)
            service._put('native_receipts', receipt)
        service.close()
        with patch.object(Service, '_all', side_effect=Service._all, autospec=True) as reads:
            service = Service(temp.name, provider, start_workers=False)
        self.addCleanup(service.close)
        self.assertNotIn('native_receipts', [call.args[1] for call in reads.call_args_list])
        self.assertEqual(service._get('native_receipts', receipt['id']), receipt)


class LogRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'error.log'
        self.fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self.addCleanup(os.close, self.fd)
        os.write(self.fd, b'earlier lines\n')

    def test_a_large_log_moves_aside_and_writing_continues_in_a_new_file(self):
        self.assertTrue(rotate_log(self.fd, self.path, limit=10))
        os.write(self.fd, b'later line\n')
        self.assertEqual(Path(str(self.path) + '.1').read_bytes(), b'earlier lines\n')
        self.assertEqual(self.path.read_bytes(), b'later line\n')

    def test_a_small_log_or_a_file_that_the_service_does_not_write_stays(self):
        self.assertFalse(rotate_log(self.fd, self.path, limit=100))
        other = Path(self.temp.name) / 'service.log'
        other.write_bytes(b'x' * 100)
        self.assertFalse(rotate_log(self.fd, other, limit=10))
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()), ['error.log', 'service.log'])

    def test_a_failed_pass_runs_again_after_five_minutes(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = Service(temp.name, FixtureProvider(temp.name), start_workers=False)
        self.addCleanup(service.close)
        with patch.object(service, '_prune_events', side_effect=[OSError('disk'), 0, 0]):
            with self.assertRaises(OSError):
                service.maintain_database(NOW)
            self.assertEqual(service.maintain_database(NOW + 60), {})
            self.assertIn('rotated', service.maintain_database(NOW + database_maintenance.DATABASE_RETRY_SECONDS))
            self.assertEqual(service.maintain_database(NOW + 2 * database_maintenance.DATABASE_RETRY_SECONDS), {})

    def test_maintenance_rotates_the_service_logs_once_an_hour(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = Service(temp.name, FixtureProvider(temp.name), start_workers=False)
        self.addCleanup(service.close)
        with patch.object(database_maintenance, 'rotate_log', return_value=False) as rotate:
            service.maintain_database(NOW)
            self.assertEqual(service.maintain_database(NOW + 60), {})
        logs = Path(temp.name).resolve() / 'logs'
        self.assertEqual([call.args for call in rotate.call_args_list], [(1, logs / 'service.log'), (2, logs / 'error.log')])


if __name__ == '__main__':
    unittest.main()
