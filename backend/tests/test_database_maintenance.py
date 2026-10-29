from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
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


class ContextRetentionTests(ServiceCase):
    """An ended context of a settled decision loses its recovery text. A pending decision keeps it for a recovery."""

    LONG = 'q' * 5000

    def context(self, decision_id, status, **fields):
        row = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=str(uuid4()), decision_id=decision_id, status=status,
                   question=self.LONG, _context_recovery='r' * 3000, _recovery_unknown_tools=[{'x': 'y' * 3000}], _recovery_attempt=1,
                   _decision_revision=1, _conversation_epoch='epoch', error_code='decision_refresh_incomplete', **fields)
        with self.service.transaction():
            self.service._put('contexts', row)
        return row['id']

    def decision(self, status, **fields):
        row = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=str(uuid4()), status=status, revision=1, **fields)
        with self.service.transaction():
            self.service._put('decisions', row)
        return row['id']

    def stored(self, identity):
        return json.loads(self.service.db.execute('SELECT data FROM contexts WHERE id=?', (identity,)).fetchone()[0])

    def test_an_ended_context_of_a_settled_decision_loses_its_text_and_keeps_its_links(self):
        settled = self.decision('approved')
        ended = {status: self.context(settled, status) for status in ('completed', 'failed', 'cancelled')}
        self.assertEqual(self.service._prune_contexts(), 3)
        for identity in ended.values():
            row = self.stored(identity)
            self.assertNotIn('_context_recovery', row)
            self.assertNotIn('_recovery_unknown_tools', row)
            # The count goes with the text. Some code reads the text when the count is set.
            self.assertNotIn('_recovery_attempt', row)
            self.assertEqual(len(row['question']), database_maintenance.CONTEXT_QUESTION_CHARACTERS)
            self.assertEqual((row['decision_id'], row['_decision_revision'], row['error_code']), (settled, 1, 'decision_refresh_incomplete'))
        # A second pass finds nothing.
        self.assertEqual(self.service._prune_contexts(), 0)

    def test_a_pending_or_dismissed_decision_and_a_live_context_keep_their_text(self):
        pending = self.decision('pending')
        # A dismissed proposal returns to pending when a person restores it inside its Undo window.
        dismissed = self.decision('dismissed', inbox_undo_until='2026-09-28T12:00:10+00:00')
        settled = self.decision('approved')
        keep = [self.context(pending, 'failed'), self.context(dismissed, 'failed'),
                self.context(settled, 'working'), self.context(settled, 'queued')]
        self.assertEqual(self.service._prune_contexts(), 0)
        for identity in keep:
            row = self.stored(identity)
            self.assertEqual((len(row['question']), row['_context_recovery'][0]), (5000, 'r'))

    def test_a_dismissed_proposal_after_its_undo_window_loses_its_text(self):
        # finish_inbox_swipes clears the mark when the window ends, and then no restore is possible.
        dismissed = self.decision('dismissed', inbox_undo_until=None)
        identity = self.context(dismissed, 'failed')
        self.assertEqual(self.service._prune_contexts(), 1)
        self.assertNotIn('_context_recovery', self.stored(identity))

    def test_the_batches_cover_every_row(self):
        settled = self.decision('approved')
        ids = [self.context(settled, 'failed') for _ in range(7)]
        with patch.object(database_maintenance, 'BATCH_ROWS', 3), patch.object(self.service.stopping, 'wait'):
            self.assertEqual(self.service._prune_contexts(), 7)
        self.assertTrue(all('_context_recovery' not in self.stored(identity) for identity in ids))

    def test_the_pass_reports_the_contexts_in_the_hourly_maintenance(self):
        settled = self.decision('approved')
        self.context(settled, 'failed')
        result = self.service.maintain_database(NOW)
        self.assertEqual(result['contexts'], 1)


class TaskHistoryTests(ServiceCase):
    """A task that ended long ago keeps a short start of its claim context. Other tasks keep all of it."""

    CONTEXT = 'c' * 4000

    def task(self, status, updated_at=OLD, **fields):
        row = dict(id=str(uuid4()), bot_id=self.bot['id'], status=status, created_at=OLD, updated_at=updated_at,
                   _kanban_claim=dict(run_id=7, native_id='t_1', context=self.CONTEXT, pid=3),
                   _self_recovery=dict(attempt=1, previous_task_id='p', saved_tasks=[{'id': 'a'}], work=[{'id': 'w'}],
                                       decisions=[{'id': 'd'}]))
        row.update(fields)
        with self.service.transaction():
            self.service._put('tasks', row)
        return row['id']

    def stored(self, identity):
        return json.loads(self.service.db.execute('SELECT data FROM tasks WHERE id=?', (identity,)).fetchone()[0])

    def test_an_old_ended_task_shortens_its_claim_context_and_keeps_the_rest(self):
        ended = [self.task(status) for status in ('completed', 'failed', 'cancelled')]
        self.assertEqual(self.service._prune_tasks(NOW), 3)
        for identity in ended:
            row = self.stored(identity)
            self.assertEqual(len(row['_kanban_claim']['context']), database_maintenance.CLAIM_CONTEXT_CHARACTERS)
            self.assertEqual((row['_kanban_claim']['run_id'], row['_kanban_claim']['pid']), (7, 3))
            # A native retry copies the recovery history into its next recovery.
            self.assertEqual((row['_self_recovery']['work'], row['_self_recovery']['decisions']), ([{'id': 'w'}], [{'id': 'd'}]))
        self.assertEqual(self.service._prune_tasks(NOW), 0)

    def test_a_recent_or_active_task_keeps_everything(self):
        keep = [self.task('completed', updated_at=RECENT), self.task('working'), self.task('queued'), self.task('needs_you')]
        self.assertEqual(self.service._prune_tasks(NOW), 0)
        for identity in keep:
            self.assertEqual(self.stored(identity)['_kanban_claim']['context'], self.CONTEXT)

    def test_a_task_without_a_claim_gets_no_new_field(self):
        identity = self.task('completed')
        with self.service.transaction():
            row = self.stored(identity)
            row.pop('_kanban_claim')
            self.service._put('tasks', row)
        self.assertEqual(self.service._prune_tasks(NOW), 0)
        self.assertNotIn('_kanban_claim', self.stored(identity))

    def test_a_short_context_stays_and_the_pass_runs_in_batches(self):
        short = self.task('completed', _kanban_claim=dict(context='short'))
        ids = [self.task('failed') for _ in range(7)]
        with patch.object(database_maintenance, 'BATCH_ROWS', 3), patch.object(self.service.stopping, 'wait'):
            self.assertEqual(self.service._prune_tasks(NOW), 7)
        self.assertEqual(self.stored(short)['_kanban_claim']['context'], 'short')
        self.assertTrue(all(len(self.stored(i)['_kanban_claim']['context']) == 200 for i in ids))

    def test_the_pass_reports_the_tasks_in_the_hourly_maintenance(self):
        self.task('completed')
        self.assertEqual(self.service.maintain_database(NOW)['tasks'], 1)


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
        routine = lambda call: dict(action={'name': 'cronjob_manage', 'arguments': {'action': call}})
        # A list of every job has no reader. A created or run routine does.
        gone = {self.receipt(idle), self.receipt(idle, **routine('list'))}
        kept = {self.receipt(idle, state='executing', result=None), self.receipt(idle, 'kanban_create'),
                self.receipt(idle, **routine('create')), self.receipt(idle, **routine(' Run_Now '))}
        decided, carried, waited = self.receipt(idle), self.receipt(idle), self.receipt(idle)
        kept |= {decided, carried, waited}
        with self.service.transaction():
            self.service._put('decisions', dict(id=str(uuid4()), task_id=other['id'], bot_id=self.bot['id'],
                                                status='approved', _receipt_id=decided))
            self.service._put('tasks', dict(other, _continuation_receipt_ids=[carried]))
        # A pending receipt, here a wait, keeps every receipt of its task. It also names the receipt that it waits for.
        waiter = self.task()
        kept |= {self.receipt(waiter, 'process', _wait_process_receipt=waited), self.receipt(waiter)}
        self.assertEqual(self.service._prune_receipts(NOW), 2)
        self.assertEqual(self.remaining(), kept)
        self.assertFalse(gone & self.remaining())

    def test_an_idle_outcome_loses_its_terminal_results(self):
        # A terminal result without notify_on_complete makes PENDING_COMPLETIONS NULL. Such receipts hold most tool output.
        idle = self.task()
        gone = {self.receipt(idle, 'terminal', result='{"output":"long build log"}'),
                self.receipt(idle, 'terminal', result='not json')}
        background = self.task()
        kept = {self.receipt(background, 'terminal', result='{"session_id":"proc","notify_on_complete":true}'),
                # A withdrawal check reads a delivered process result later.
                self.receipt(idle, 'terminal', result='{"session_id":"done","notify_on_complete":true}', _process_delivery='check')}
        self.assertEqual(self.service._prune_receipts(NOW), 2)
        self.assertEqual(self.remaining(), kept)
        self.assertFalse(gone & self.remaining())

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


ANCIENT = '2026-08-01T12:00:00Z'


class HistoryRetentionTests(ServiceCase):
    """A task that ended 30 days ago is retired. Its kept events and its completed receipts go, after one backup."""

    def setUp(self):
        super().setUp()
        self.service.db.execute('DELETE FROM events')
        for name, value in (('EVENT_MINIMUM_ROWS', 2), ('BATCH_ROWS', 3)):
            patcher = patch.object(database_maintenance, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def task(self, status='completed', updated_at=ANCIENT, **fields):
        task = dict(id=str(uuid4()), bot_id=self.bot['id'], title='Work', status=status, summary='',
                    created_at=updated_at, updated_at=updated_at) | fields
        with self.service.transaction():
            self.service._put('tasks', task)
        return task

    def history(self, task):
        """The events that the lookups keep for a task at any age."""
        return [self.event('task.created', {'id': task['id']}, task['id']),
                self.event('routine.triggered', {'task_id': task['id']}, task['id']),
                self.event('task.updated', {'id': task['id'], 'status': 'completed'}, task['id']),
                self.event('bot_message.delivered', {'task_id': task['id']}, task['id'])]

    def receipt(self, task, name='terminal', state='completed', **fields):
        record = dict(id=str(uuid4()), task_id=task['id'], turn_id=str(uuid4()), action={'name': name, 'arguments': {}},
                      state=state, created_at=ANCIENT, result='{"output":"' + 'x' * 100 + '"}') | fields
        with self.service.transaction():
            self.service._put('native_receipts', record)
        return record['id']

    def receipts(self):
        return {row[0] for row in self.service.db.execute('SELECT id FROM native_receipts')}

    def backups(self):
        return sorted((Path(self.temp.name) / 'backups').glob('retention-*.sqlite3'))

    def test_a_retired_task_loses_its_kept_events_and_the_first_pass_keeps_a_copy(self):
        retired = self.task(routine_id=str(uuid4()))
        recent = self.task(updated_at=OLD, routine_id=str(uuid4()))
        active = self.task(status='waiting', routine_id=str(uuid4()))
        gone = self.history(retired)
        kept = [self.event('bots.restarted')] + self.history(recent) + self.history(active)
        # Nothing tells that an unknown task ended, so its events stay.
        kept.append(self.event('task.created', {'id': 'unknown'}, 'unknown'))
        kept += [self.event('message.updated', created_at=RECENT) for _ in range(2)]
        result = self.service.maintain_database(NOW)
        self.assertTrue(result['retired'])
        self.assertEqual(self.seqs(), kept)
        [backup] = self.backups()
        with sqlite3.connect(backup) as db:
            self.assertEqual([row[0] for row in db.execute('SELECT seq FROM events ORDER BY seq')], gone)
            self.assertEqual(db.execute('SELECT data FROM events WHERE seq=?', (gone[0],)).fetchone()[0],
                             json.dumps({'id': retired['id']}, separators=(',', ':')))
        self.assertEqual(self.service._setting('retention_backup'), backup.name)
        self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)

    def test_a_retired_task_of_a_busy_bot_loses_its_completed_receipts(self):
        # Chat tasks share the execution session of their bot, so the bot's live task keeps the outcome busy.
        retired, recent = self.task(), self.task(updated_at=OLD)
        self.task(status='working', updated_at=RECENT)
        gone = self.receipt(retired)
        kept = {self.receipt(retired, state='executing'), self.receipt(retired, 'kanban_create'), self.receipt(recent),
                self.receipt(retired, action={'name': 'cronjob_manage', 'arguments': {'action': 'create'}})}
        named = self.receipt(retired)
        kept.add(named)
        with self.service.transaction():
            self.service._put('decisions', dict(id=str(uuid4()), task_id=recent['id'], bot_id=self.bot['id'],
                                                status='approved', _receipt_id=named))
        # Without the retirement the busy outcome keeps every receipt.
        self.assertEqual(self.service._prune_receipts(NOW), 0)
        self.assertEqual(self.service._prune_receipts(NOW, retire=True), 1)
        self.assertEqual(self.receipts(), kept)
        self.assertNotIn(gone, self.receipts())

    def test_a_retired_task_that_runs_again_between_batches_keeps_its_receipts(self):
        first, second = self.task(), self.task()
        self.task(status='working', updated_at=RECENT)
        gone, kept = self.receipt(first), self.receipt(second)
        wait = self.service.stopping.wait
        def resume(timeout):
            with self.service.transaction():
                self.service._put('tasks', dict(self.service._get('tasks', second['id']), status='queued'))
            return wait(timeout)
        with patch.object(database_maintenance, 'BATCH_ROWS', 1), patch.object(self.service.stopping, 'wait', side_effect=resume):
            self.service._prune_receipts(NOW, retire=True)
        self.assertEqual(self.receipts(), {kept})
        self.assertNotIn(gone, self.receipts())

    def test_later_passes_delete_without_a_new_copy(self):
        self.history(self.task())
        self.service.maintain_database(NOW)
        [backup] = self.backups()
        later = self.history(self.task())
        [self.event('message.updated', created_at=RECENT) for _ in range(2)]
        self.assertTrue(self.service.maintain_database(NOW + database_maintenance.DATABASE_MAINTENANCE_SECONDS)['retired'])
        self.assertFalse(set(later) & set(self.seqs()))
        self.assertEqual(self.backups(), [backup])
        with sqlite3.connect(backup) as db:
            self.assertFalse(db.execute('SELECT seq FROM events WHERE seq IN (%s)' % ','.join('?' * len(later)), later).fetchall())

    def test_a_pass_that_deletes_nothing_leaves_the_backup_to_the_first_retirement(self):
        # The store has no history to retire yet.
        self.assertTrue(self.service.maintain_database(NOW)['retired'])
        self.assertEqual((self.backups(), self.service._setting('retention_backup')), ([], None))
        gone = self.history(self.task())
        [self.event('message.updated', created_at=RECENT) for _ in range(2)]
        self.service.maintain_database(NOW + database_maintenance.DATABASE_MAINTENANCE_SECONDS)
        [backup] = self.backups()
        with sqlite3.connect(backup) as db:
            self.assertEqual([row[0] for row in db.execute('SELECT seq FROM events ORDER BY seq')], gone)
        self.assertEqual(self.service._setting('retention_backup'), backup.name)

    def test_a_disk_without_room_for_the_copy_keeps_the_history(self):
        kept = self.history(self.task()) + [self.event('message.updated', created_at=RECENT) for _ in range(2)]
        full = SimpleNamespace(free=database_maintenance.VACUUM_DISK_RESERVE_BYTES)
        with patch.object(database_maintenance.shutil, 'disk_usage', return_value=full), \
                self.assertLogs('coconut.database_maintenance', 'WARNING') as logs:
            self.assertFalse(self.service.maintain_database(NOW)['retired'])
        self.assertIn('coconut.retention_backup_waits', logs.output[0])
        self.assertEqual(self.seqs(), kept)
        self.assertEqual((self.backups(), self.service._setting('retention_backup')), ([], None))

    def test_a_stopped_first_pass_copies_again_in_the_next_pass(self):
        self.history(self.task())
        with patch.object(self.service, '_maintenance_stopped', return_value=True):
            self.service.maintain_database(NOW)
        self.assertIsNone(self.service._setting('retention_backup'))

    def test_an_old_backup_goes(self):
        backups = Path(self.temp.name) / 'backups'
        backups.mkdir()
        old, new = backups / 'retention-20260601.sqlite3', backups / 'retention-20260920.sqlite3'
        for path, stamp in ((old, NOW - 31 * 86400), (new, NOW - 8 * 86400)):
            path.write_bytes(b'')
            os.utime(path, (stamp, stamp))
        self.service.maintain_database(NOW)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


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
        # A disk without room for the rewrite keeps the file as it is.
        with patch.object(database_maintenance.shutil, 'disk_usage',
                          return_value=SimpleNamespace(free=database_maintenance.VACUUM_DISK_RESERVE_BYTES)):
            self.assertEqual(service._release_free_pages(), 0)
        self.assertEqual(self.pragma(service, 'auto_vacuum'), 0)
        room = SimpleNamespace(free=database_maintenance.VACUUM_DISK_RESERVE_BYTES + 2 * 1024 ** 3)
        with patch.object(database_maintenance.shutil, 'disk_usage', return_value=room), \
                self.assertLogs('coconut.database_maintenance', 'WARNING'):
            self.assertGreater(service._release_free_pages(), 0)
        self.assertEqual((self.pragma(service, 'auto_vacuum'), self.pragma(service, 'freelist_count')), (2, 0))


class StartupReadTests(unittest.TestCase):
    def test_startup_reads_no_whole_receipt_journal(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        provider = FixtureProvider(temp.name)
        # Relay recovery runs only for a provider with native settings.
        provider.native_settings = lambda operation, bots, **options: None
        service = Service(temp.name, provider, start_workers=False)
        bot = service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'phone')
        task = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Ask Dev'}, str(uuid4()), 'phone')
        relay = dict(id=str(uuid4()), task_id=task['id'], state='executing', action={'name': 'message_agent', 'arguments': {}})
        with service.transaction():
            service._put('tasks', dict(service._get('tasks', task['id']), status='working'))
            service._put('native_receipts', relay)
        service.close()
        recovered = []
        provider.native_settings = lambda operation, bots, **options: recovered.append(options.get('call_id'))
        with patch.object(Service, '_all', side_effect=Service._all, autospec=True) as reads:
            service = Service(temp.name, provider, start_workers=False)
        self.addCleanup(service.close)
        self.assertNotIn('native_receipts', [call.args[1] for call in reads.call_args_list])
        # Relay recovery still finds the executing relay of a working task.
        self.assertIn(relay['id'], recovered)

    def test_the_process_delivery_migration_runs_once(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = Service(temp.name, FixtureProvider(temp.name), start_workers=False)
        bot = service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'phone')
        task = dict(id=str(uuid4()), bot_id=bot['id'], status='completed')
        receipt = dict(id=str(uuid4()), task_id=task['id'], state='completed', action={'name': 'terminal', 'arguments': {}},
                       result=json.dumps({'session_id': 'proc', 'notify_on_complete': True}))
        with service.transaction():
            service._put('tasks', task)
            service._put('native_receipts', receipt)
        service.close()
        statements = []
        service = Service(temp.name, FixtureProvider(temp.name), start_workers=False)
        self.addCleanup(service.close)
        # The first start ran the migration. Runtime code registers new receipts, so a later start reads none.
        self.assertEqual(service._get('native_receipts', receipt['id']), receipt)
        service.db.set_trace_callback(statements.append)
        service._migrate_process_deliveries()
        service.db.set_trace_callback(None)
        self.assertFalse([s for s in statements if 'native_receipts' in s])


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
