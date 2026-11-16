"""A migrated Work item continues after Coconut's settle pass archives the card that its continuation scheduled.

A requester relays work to another bot, and the request completes its original card. Its open PR stays, so migration
gives the retained Work item a card under the key coconut-outcome:<Work id>, and the request continues there. A
continuation of that card reuses it. Its worker parks the card with kanban_schedule, Hermes ends the run as scheduled,
the request completes, and a day later the settle pass archives the card. The work is unfinished, though.

Only that exact card continues, in the same execution, workspace, and result route: the card that migration made for
the request's own Work item, holding an input of an authenticated continuation of that card, whose last run is the
scheduled run of that input's claim, and which that run scheduled. A closed source still needs the explicit choice, and
a Stop stays final. Native CI sets COCONUT_REQUIRE_NATIVE, and then a missing Hermes install fails these tests instead
of skipping them.
"""
import copy
import json
from pathlib import Path
import threading
import time
import unittest
from uuid import uuid4

from coconut.kanban_scheduler import SETTLE_SECONDS
from coconut.native_outcomes import CLOSED_SOURCE_REFUSED
from coconut.service import ServiceError, now
from coconut.work_sources import STALE_NOTE
import legacy_work_sync
import test_archived_scheduled_assignment_continuation as assignment_fixtures
import test_archived_scheduled_routine_continuation as routine_fixtures
import test_native_kanban as native_fixtures
import hermes_env

setUpModule, tearDownModule = legacy_work_sync.scope()

REASON = routine_fixtures.REASON
stamp = routine_fixtures.stamp
URL = 'https://github.com/example/project/pull/9161'
NOT_ADMITTED = 'not blocked or completed or quiescent queued/review work'



class ArchivedMigratedOutcomeNativeTests(unittest.TestCase):
    """The installed Hermes kernel: migration, dispatch, schedule, settle archive, and then the continuation."""

    write = native_fixtures.NativeKanbanTests.write
    native_program = native_fixtures.NativeKanbanTests.native_program
    tick = native_fixtures.NativeKanbanTests.tick
    native = routine_fixtures.ArchivedScheduledRoutineNativeTests.native
    completed = routine_fixtures.ArchivedScheduledRoutineNativeTests.completed
    dispatched = assignment_fixtures.ArchivedScheduledAssignmentNativeTests.dispatched
    update = assignment_fixtures.ArchivedScheduledAssignmentNativeTests.update
    settle_returns = assignment_fixtures.ArchivedScheduledAssignmentNativeTests.settle_returns
    assertQuiescent = assignment_fixtures.ArchivedScheduledAssignmentNativeTests.assertQuiescent
    continue_work = assignment_fixtures.ArchivedScheduledAssignmentNativeTests.continue_work
    completed_assignment = assignment_fixtures.ArchivedScheduledAssignmentNativeTests.completed_assignment

    def setUp(self):
        hermes_env.require(self)
        try:
            native_fixtures.NativeKanbanTests.setUp(self)
        except RuntimeError as error:
            # A Hermes descendant fence makes the fixture's pinned board read-only. Report Hermes' own reason.
            self.addCleanup(self.tmp.cleanup)
            log = Path(self.tmp.name) / 'kanban' / 'adapter.log'
            reason = log.read_text().strip().splitlines()[-1:] if log.exists() else []
            self.fail('The native fixture board did not start: %s %s' % (error, reason))
        self.assertTrue((Path(self.tmp.name) / 'kanban' / 'kanban.db').is_file())
        self.provider.tasks = []
        self.owner_id = None
        start = self.provider.start_task

        def record(task, bot, history, emit):
            if task['id'] == self.owner_id:
                self.provider.tasks.append(copy.deepcopy(task))
                claim = task['_kanban_claim']
                # The worker parks its card with kanban_schedule, and the bridge saves the call. Then its turn completes.
                call = self.service.emit(task['id'], 'native_call', dict(name='kanban_schedule', arguments=dict(
                    task_id=claim['native_id'], reason=REASON)))
                self.native_program("assert board.kb.schedule_task(board.conn, sys.argv[2], reason=sys.argv[4], "
                                    "expected_run_id=int(sys.argv[3]))", claim['native_id'], str(claim['run_id']), REASON)
                self.service.emit(task['id'], 'native_result', dict(id=call['id'], result=json.dumps(dict(
                    ok=True, task_id=claim['native_id'], run_id=claim['run_id'], status='scheduled', reason=REASON))))
            start(task, bot, history, emit)
        self.provider.start_task = record

    def completion(self, identity):
        return self.service.kanban.call({'op': 'completion', 'id': identity})['card']

    def card(self, owner):
        self.service.kanban.call({'op': 'snapshot', 'external_ids': [owner['id']]})
        return copy.deepcopy(self.service.kanban.tasks[owner['id']])

    def admits(self, owner, card=None, completion=None):
        """Whether settled_scheduled_continuation keeps the archived card of the request, read as continue_work reads it."""
        card = card or self.card(owner)
        return self.service.settled_scheduled_continuation(self.service._get('tasks', owner['id']), card,
                                                           completion or (lambda: self.completion(card['id'])))

    def migrated(self):
        """Lead relays the request to Desk, and Desk completes its original card. The request's open PR stays, so
        migration gives its Work item a card. The migration turn parks the card. Lead continues the Work, and that turn
        parks the same card too. A day after the request completed, the settle pass archives the card."""
        owner, source, original, workspace = self.completed_assignment()
        with self.service.transaction():
            self.service._put('artifacts', dict(id=str(uuid4()), kind='pr', task_id=owner['id'], _review_task_id=owner['id'],
                                                bot_id=owner['bot_id'], title='Correct the exported totals', url=URL,
                                                status='ready_for_review', pr_state='open', observed_at=now(),
                                                created_at=now()))
        self.provider.supports_native_outcomes = True
        self.service._migrate_native_work()
        owner = self.service._get('tasks', owner['id'])
        work = self.service._get('artifacts', owner['_work_context_id'])
        identity = owner['_native_outcome_id']
        self.assertNotEqual(identity, original)
        self.assertEqual((work['kind'], work['task_id'], work['_native_outcome_id']), ('work', owner['id'], identity))
        native = self.native(identity)
        self.assertEqual((native['key'], native['parents']), ('coconut-outcome:' + work['id'], [original]))
        self.assertEqual((native['tenant'], native['path']), (owner['bot_id'], workspace))
        self.assertEqual(owner['status'], 'queued')
        migration = self.dispatched(1)
        self.assertEqual(migration['_native_outcome_id'], identity)
        self.completed(owner)
        self.tick()
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'scheduled')
        receipt = self.continue_work(source, owner, identity, 'Continue the exported totals PR.')
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=identity, status='queued'))
        second = self.dispatched(2)
        self.assertEqual(second['_native_outcome_id'], identity)
        self.completed(owner)
        self.tick()
        self.update(owner['id'], updated_at=stamp(SETTLE_SECONDS + 1))
        self.service._settle_native_orphans()
        self.service.kanban.call({'op': 'snapshot'})
        self.tick()
        native = self.native(identity)
        self.assertEqual(native['status'], 'archived')
        self.assertEqual([run[2] for run in native['runs']], ['scheduled', 'scheduled'])
        self.assertEqual(native['key'], 'coconut-outcome:' + work['id'])
        owner = self.service._get('tasks', owner['id'])
        self.assertEqual((owner['status'], owner['_native_outcome_id'], owner['_native_turn_id']),
                         ('completed', identity, second['_native_turn_id']))
        return owner, source, original, identity, workspace, work

    def test_continuation_dispatches_a_successor_of_the_archived_migrated_card(self):
        owner, source, original, migrated, workspace, work = self.migrated()
        history = self.native(migrated), self.native(original)
        receipt = self.continue_work(source, owner, migrated, 'Finish the exported totals PR.')
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=migrated, status='queued'))
        continued = self.service._get('tasks', owner['id'])
        for key in ('id', 'bot_id', 'session_id', 'thread_id', 'exchange_id', 'conversation_bot_id', 'source_bot_id',
                    'source_task_id', '_reply_to_task_id', '_execution_session_id', '_work_context_id'):
            self.assertEqual(continued.get(key), owner.get(key), key)
        self.assertEqual(continued['status'], 'queued')
        self.assertNotEqual(continued['_native_turn_id'], owner['_native_turn_id'])
        task = self.dispatched(3)
        successor = task['_native_outcome_id']
        self.assertNotIn(successor, (original, migrated))
        native = self.native(successor)
        self.assertEqual(native['parents'], [migrated])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'])
        self.assertEqual((native['tenant'], native['path']), (owner['bot_id'], workspace))
        self.assertEqual((task['_native_turn_id'], task['_kanban_workspace']), (continued['_native_turn_id'], workspace))
        self.assertEqual(task['_execution_session_id'], owner['_execution_session_id'])
        # The archived card and the original card keep their history. Hermes runs only the successor.
        self.assertEqual((self.native(migrated), self.native(original)), history)
        # A replay returns the saved receipt and dispatches nothing more.
        self.assertEqual(self.service.emit(source['id'], 'work_continue',
                                           dict(task_id=migrated, message='Finish the exported totals PR.')), receipt)
        self.completed(owner)
        self.tick()
        self.assertEqual(len(self.provider.tasks), 3)
        # The same Work item follows the successor.
        item = self.service._get('artifacts', work['id'])
        self.assertEqual((item['task_id'], item['_native_outcome_id']), (owner['id'], successor))
        self.assertFalse(any(json.loads(row[0]).get('status') == 'cancelled' for row in self.service.db.execute(
            "SELECT data FROM events WHERE task_id=? AND type='task.updated'", (owner['id'],))))

    def changed(self, table, identity, change):
        """Change one saved row in a committed transaction. The result restores the row."""
        with self.service.transaction():
            row = self.service._get(table, identity)
            changed = copy.deepcopy(row)
            change(changed)
            self.service._put(table, changed)
        def restore():
            with self.service.transaction():
                self.service._put(table, row)
        return restore

    def setting(self, key):
        with self.service.transaction():
            self.service.db.execute('INSERT INTO settings VALUES (?,?)', (key, 'true'))
        def restore():
            with self.service.transaction():
                self.service.db.execute('DELETE FROM settings WHERE key=?', (key,))
        return restore

    def delivery(self, owner, change):
        """Change the saved delivery of the current continuation of the request. The result restores it."""
        rowid, raw = self.service.db.execute("SELECT rowid, data FROM deliveries WHERE json_extract(data,'$.receipt.task_id')=? "
                                             "AND json_extract(data,'$.turn_id')=?", (owner['id'], owner['_native_turn_id'])).fetchone()
        data = json.loads(raw)
        change(data)
        with self.service.transaction():
            self.service.db.execute('UPDATE deliveries SET data=? WHERE rowid=?', (json.dumps(data), rowid))
        def restore():
            with self.service.transaction():
                self.service.db.execute('UPDATE deliveries SET data=? WHERE rowid=?', (raw, rowid))
        return restore

    def schedule_receipt(self, owner):
        return next(r for r in self.service._native_task_receipts(owner['id'], turn_id=owner['_native_turn_id'], state='completed')
                    if r['action']['name'] == 'kanban_schedule')

    def assertRefused(self, caller, migrated, owner, pattern, **payload):
        before = self.service._get('tasks', owner['id'])
        with self.assertRaisesRegex(ServiceError, pattern):
            self.service.emit(caller['id'], 'work_continue', dict(task_id=migrated, message='Finish the exported totals PR.',
                                                                 **payload))
        self.assertEqual(self.service._get('tasks', owner['id']), before)

    def test_only_the_proved_migrated_card_continues(self):
        owner, source, original, migrated, workspace, work = self.migrated()
        lead = self.service._get('bots', source['bot_id'])
        other = dict(work, id=str(uuid4()), _native_outcome_id=original, _work_key='retained-pr:' + URL + '/other')
        with self.service.transaction():
            self.service._put('artifacts', other)
        identity = self.service._work_identity(work)
        turn = owner['_native_turn_id']
        text = json.loads(self.service.db.execute("SELECT data FROM deliveries WHERE json_extract(data,'$.turn_id')=?",
                                                  (turn,)).fetchone()[0])['text']
        alive = threading.Event()
        writer = threading.Thread(target=alive.wait, args=(30,), daemon=True)

        def live_writer():
            writer.start()
            self.service.running[owner['id']] = writer
            def restore():
                self.service.running.pop(owner['id'], None)
                alive.set()
                writer.join(5)
            return restore

        def receipt(change):
            record = self.schedule_receipt(owner)
            return self.changed('native_receipts', record['id'], change)

        # Each change ends the proof by itself, and the same continuation is then refused without writing anything.
        controls = dict(
            work_of_another_request=lambda: self.changed('artifacts', work['id'], lambda w: w.update(task_id=source['id'])),
            work_of_another_bot=lambda: self.changed('artifacts', work['id'], lambda w: w.update(bot_id=lead['id'])),
            work_of_another_card=lambda: self.changed('artifacts', work['id'], lambda w: w.update(_native_outcome_id=original)),
            cancelled_work=lambda: self.changed('artifacts', work['id'], lambda w: w.update(status='cancelled')),
            hidden_work=lambda: self.changed('artifacts', work['id'], lambda w: w.update(_hidden=True)),
            deleted_work=lambda: self.changed('artifacts', work['id'], lambda w: w.update(_deleted=True)),
            merged_work=lambda: self.changed('artifacts', work['id'], lambda w: w.update(_same_outcome_as=other['id'])),
            not_a_work_item=lambda: self.changed('artifacts', work['id'], lambda w: w.update(kind='pr', url=URL)),
            work_cancelled_by_a_person=lambda: self.setting('work.cancelled.' + identity),
            work_dismissed_by_a_person=lambda: self.setting('work.dismissed.' + identity),
            request_of_another_work=lambda: self.changed('tasks', owner['id'], lambda t: t.update(_work_context_id=other['id'])),
            request_without_work=lambda: self.changed('tasks', owner['id'], lambda t: t.pop('_work_context_id')),
            routine_record=lambda: self.changed('tasks', owner['id'], lambda t: t.update(_native_occurrence={})),
            unknown_tool_effect=lambda: self.changed('tasks', owner['id'], lambda t: t.update(
                _self_recovery=dict(t.get('_self_recovery') or {}, unknown_tools=[dict(id='unknown', task_id=owner['id'])]))),
            undelivered_result=lambda: self.changed('tasks', owner['id'], lambda t: t.update(
                _return_pending=dict(target_bot_id=lead['id']))),
            unread_input=lambda: self.changed('tasks', owner['id'], lambda t: t.update(_live_messages=['Check the totals.'])),
            failed_request=lambda: self.changed('tasks', owner['id'], lambda t: t.update(status='failed')),
            executing_tool=lambda: self.changed('native_receipts', self.schedule_receipt(owner)['id'],
                                                lambda r: r.update(state='executing')),
            active_native_writer=live_writer,
            continuation_of_another_card=lambda: self.delivery(owner, lambda d: d['receipt'].update(native_task_id=original)),
            continuation_of_another_bot=lambda: self.delivery(owner, lambda d: d.update(target_bot_id=lead['id'])),
            unauthenticated_continuation=lambda: self.changed('tasks', owner['id'], lambda t: t.update(
                _bot_messages=[m for m in t['_bot_messages'] if m.get('text') != text])),
            schedule_of_another_run=lambda: receipt(lambda r: r.update(result=json.dumps(dict(
                json.loads(r['result']), run_id=owner['_kanban_claim']['run_id'] + 1)))),
            schedule_of_another_card=lambda: receipt(lambda r: r['action']['arguments'].update(task_id=original)),
            failed_schedule=lambda: receipt(lambda r: r.update(result=json.dumps(dict(json.loads(r['result']), ok=False)))),
            schedule_of_another_turn=lambda: receipt(lambda r: r.update(turn_id='turn-earlier')),
        )
        self.assertTrue(self.admits(owner))
        for name, change in controls.items():
            with self.subTest(name):
                restore = change()
                try:
                    self.assertFalse(self.admits(owner))
                    self.assertRefused(source, migrated, owner, '.')
                finally:
                    restore()
                self.assertTrue(self.admits(owner))
        # Hermes's own record of the card ends the proof too: each change below is the only one.
        card = self.card(owner)
        native = self.completion(migrated)
        runs = native['runs']
        self.assertEqual((native['status'], native['input_id'], [r['outcome'] for r in runs]),
                         ('archived', turn, ['scheduled', 'scheduled']))
        hermes = dict(
            completed_run=dict(native, runs=[*runs[:-1], dict(runs[-1], outcome='completed')]),
            earlier_completed_run=dict(native, runs=[dict(runs[0], outcome='completed'), *runs[1:]]),
            run_left=dict(native, current_run_id=runs[-1]['id']),
            worker_left=dict(native, worker_pid=4242),
            open_run=dict(native, runs=[*runs[:-1], dict(runs[-1], ended=False)]),
            run_of_another_claim=dict(native, runs=[*runs[:-1], dict(runs[-1], id=runs[-1]['id'] + 1)]),
            failed_run=dict(native, runs=[*runs[:-1], dict(runs[-1], outcome='crashed')]),
            other_input=dict(native, input_id='turn-earlier'),
            other_key=dict(native, idempotency_key='coconut-outcome:' + owner['id']),
            other_tenant=dict(native, tenant=lead['id']),
            not_archived=dict(native, status='done'),
        )
        for name, answer in hermes.items():
            with self.subTest(name):
                self.assertFalse(self.admits(owner, card, lambda: answer))
        snapshot = dict(
            key_of_the_request=dict(card, idempotency_key='coconut-outcome:' + owner['id']),
            key_of_another_work=dict(card, idempotency_key='coconut-outcome:' + other['id']),
            key_of_the_original_card=dict(card, idempotency_key='coconut:' + owner['id']),
            other_tenant=dict(card, tenant=lead['id']),
            other_creator=dict(card, created_by='default'),
            other_card=dict(card, id=original),
            other_input=dict(card, input_id='turn-earlier'),
            old_idle_archive=dict(card, last_activity_at=time.time() - 30 * 86400),
        )
        for name, changed in snapshot.items():
            with self.subTest(name):
                self.assertFalse(self.admits(owner, changed, lambda: native))
        self.assertTrue(self.admits(owner, card, lambda: native))
        # The guards after the archive check still hold the proved card.
        stranger = self.write('/v1/bots', {'name': 'Stranger'})
        stranger = self.update(self.write('/v1/bots/' + stranger['id'] + '/messages', {'text': 'Finish it.'})['id'],
                               status='working', _chat_session=True)
        self.assertRefused(stranger, migrated, owner, 'Only the owner or its requesting agent')
        decision = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], status='pending', revision=1,
                        title='Merge the exported totals PR?', merge_pr_url=URL)
        with self.service.transaction():
            self.service._put('decisions', decision)
        self.assertTrue(self.admits(owner))
        self.assertRefused(source, migrated, owner, 'existing decision must be answered')
        with self.service.transaction():
            self.service.db.execute('DELETE FROM decisions WHERE id=?', (decision['id'],))
        restore = self.changed('tasks', owner['id'], lambda t: t.update(_waiting_on=[stranger['id']]))
        self.assertTrue(self.admits(owner))
        self.assertRefused(source, migrated, owner, 'existing dependencies')
        restore()
        self.assertRefused(source, migrated, owner, 'continue_closed_source applies only', continue_closed_source=True)
        # After every control, the proved card still continues, and nothing was dispatched before.
        self.assertEqual(len(self.provider.tasks), 2)
        receipt = self.continue_work(source, owner, migrated, 'Finish the exported totals PR.')
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=migrated, status='queued'))
        self.assertEqual(self.native(self.dispatched(3)['_native_outcome_id'])['parents'], [migrated])

    def test_closed_source_keeps_its_explicit_choice(self):
        owner, source, original, migrated, workspace, work = self.migrated()
        # The assignment pass archives a card and comments STALE_NOTE at once. Here that comment follows the archive,
        # and the source of the card closed before it.
        self.native_program("board.kb.add_comment(board.conn, sys.argv[2], 'coconut', sys.argv[3])", migrated, STALE_NOTE)
        archived = self.completion(migrated)['archive']
        self.assertEqual((archived['author'], archived['note']), ('coconut', STALE_NOTE))
        with self.service.transaction():
            self.service.db.execute('INSERT INTO settings VALUES (?,?)', ('work-source:assigned', json.dumps(dict(
                native_links={URL: dict(updated_at=now(), task_ids=[migrated], decision_ids=[])},
                source_left={URL: archived['at'] - 1}))))
        card = self.card(owner)
        self.assertTrue(self.service.settled_closed_source(owner, card, lambda: self.completion(migrated)))
        self.assertTrue(self.admits(owner))
        # Closure comes first, so the plain continuation is refused even though the scheduled-card rule holds too.
        history = self.native(migrated), self.native(original)
        self.assertRefused(source, migrated, owner, CLOSED_SOURCE_REFUSED)
        self.tick()
        self.assertEqual(len(self.provider.tasks), 2)
        self.assertEqual((self.native(migrated), self.native(original)), history)
        receipt = self.service.emit(source['id'], 'work_continue', dict(
            task_id=migrated, message='Finish the exported totals PR.', continue_closed_source=True))
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=migrated, status='queued'))
        self.assertEqual(self.native(self.dispatched(3)['_native_outcome_id'])['parents'], [migrated])

    def test_stop_stays_final(self):
        owner, source, _, migrated, _, _ = self.migrated()
        with self.service.transaction():
            self.service._cancel_one(self.service._get('tasks', owner['id']), [], include_finished=True)
        stopped = self.service._get('tasks', owner['id'])
        self.assertFalse(self.admits(stopped))
        self.assertRefused(source, migrated, stopped, '.')
        self.tick()
        self.assertEqual(len(self.provider.tasks), 2)


if __name__ == '__main__':
    unittest.main()
