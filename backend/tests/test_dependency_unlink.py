"""Service/transport regressions; the journal tests use an explicit fake kernel."""
import contextlib
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut.kanban_runtime import Board
from coconut.service import ServiceError
import test_targeted_bot_continuation as fixtures
import test_provider as provider_fixtures


class DependencyUnlinkTests(unittest.TestCase):
    setUp = fixtures.TargetedBotContinuationTests.setUp
    tearDown = fixtures.TargetedBotContinuationTests.tearDown
    write = fixtures.TargetedBotContinuationTests.write
    prepare = fixtures.TargetedBotContinuationTests.prepare

    def setup_edge(self, caller='Desk'):
        child, source = self.prepare()
        child.update(status='queued', _native_outcome_id='t_22222222')
        self.service._put('tasks', child)
        care = self.write('/v1/bots', {'name': 'Care'})
        parent = dict(child, id=str(uuid4()), bot_id=care['id'], session_id=care['session_id'],
                      status='needs_you', _native_outcome_id='t_11111111')
        parent['_execution_session_id'] = parent['id']
        self.service._put('tasks', parent)
        self.service._put('decisions', dict(id='appearance', task_id=parent['id'], status='pending'))
        if caller != 'Desk':
            bot = self.dev if caller == 'Dev' else care
            source = dict(source, id=str(uuid4()), bot_id=bot['id'], session_id=bot['session_id'])
            source['_execution_session_id'] = source['id']
            self.service._put('tasks', source)
            # The original requester is not a worker in this fixture.
            original = self.service._get('tasks', self.parent['id'])
            original['status'] = 'waiting'
            self.service._put('tasks', original)
        self.requests = []
        self.hidden_snapshots = set()
        self.snapshot_rows = [dict(external_id=t['id'], id=t['_native_outcome_id'], status=status)
                              for t, status in ((parent, 'review'), (child, 'todo'))]

        def call(request):
            self.assertFalse(self.service.lock._is_owned(), 'adapter called with service lock held')
            if request['op'] == 'snapshot':
                rows = copy.deepcopy(self.snapshot_rows)
                known = {row['external_id'] for row in rows}
                for task_id in request.get('external_ids') or []:
                    if task_id in known or task_id in self.hidden_snapshots:
                        continue
                    rows.append(dict(external_id=task_id, id='observed', status='done'))
                return {'tasks': rows}
            if request['op'] == 'unlink_receipt':
                return {'receipt': None}
            self.assertTrue(self.service._dependency_unlinking)
            self.requests.append(request)
            return dict(status='unlinked', request_id=request['request_id'], parent_id=request['parent_id'], child_id=request['child_id'])
        self.service.kanban = Mock(tasks={t['external_id']: t for t in self.snapshot_rows}, call=Mock(side_effect=call))
        self.arguments = dict(parent_id=parent['_native_outcome_id'], child_id=child['_native_outcome_id'],
                              reason='The build resource was released; appearance review remains pending.')
        return child, parent, source

    def unlink(self, source, **changes):
        return self.service.emit(source['id'], 'dependency_unlink', self.arguments | changes)

    def test_dev_and_desk_can_unlink_without_changing_any_service_record(self):
        child, parent, source = self.setup_edge()
        for caller in (source, dict(source, id=str(uuid4()), bot_id=self.dev['id'])):
            caller['_execution_session_id'] = caller['id']
            self.service._put('tasks', caller)
            before = {table: self.service._all(table) for table in ('tasks', 'artifacts', 'decisions', 'contexts')}
            self.assertEqual(self.unlink(caller)['status'], 'unlinked')
            self.assertEqual(before, {table: self.service._all(table) for table in before})
            request = self.requests[-1]
            self.assertEqual(request['source_task_id'], caller['id'])
            self.assertEqual(request['source_bot_id'], caller['bot_id'])
            self.assertEqual(request['reason'], self.arguments['reason'])
        self.assertEqual(self.service._get('decisions', 'appearance')['status'], 'pending')

    def test_retries_share_an_identity_but_new_turns_do_not(self):
        child, parent, source = self.setup_edge()
        source['_native_turn_id'] = 'first-turn'
        self.service._put('tasks', source)
        first = self.unlink(source)
        self.assertEqual(self.unlink(source), first)
        source['_native_turn_id'] = 'second-turn'
        self.service._put('tasks', source)
        second = self.unlink(source)
        self.assertNotEqual(first['request_id'], second['request_id'])

    def test_registered_provider_calls_the_authenticated_service(self):
        child, parent, source = self.setup_edge()
        self.root = Path(self.temp.name) / 'provider'
        provider = provider_fixtures.ProviderTests.fake_hermes(self,
            "receipt = json.loads(registry.dispatch('coconut_unlink_dependency', " + repr(self.arguments) + "))\n"
            "assert receipt['status'] == 'unlinked', receipt\n"
            "return {'completed': True, 'final_response': 'Unlinked exactly one dependency.'}\n")
        seen, events = [], []
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'dependency_unlink':
                result = self.service.emit(source['id'], kind, data)
                seen.append(result)
                return result
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(source, self.desk, [], emit)
        self.assertEqual(len(seen), 1, events)
        self.assertEqual(seen[0]['child_id'], child['_native_outcome_id'])
        self.assertEqual(self.requests[0]['source_bot_id'], self.desk['id'])

    def test_provider_reports_real_service_authorization_denial(self):
        child, parent, source = self.setup_edge('Care')
        self.root = Path(self.temp.name) / 'provider'
        provider = provider_fixtures.ProviderTests.fake_hermes(self,
            "receipt = json.loads(registry.dispatch('coconut_unlink_dependency', " + repr(self.arguments) + "))\n"
            "assert receipt['status'] == 'rejected', receipt\n"
            "assert 'Only the child owner' in receipt['error'], receipt\n"
            "return {'completed': True, 'final_response': 'Authorization rejection verified.'}\n")
        events = []
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'dependency_unlink':
                return self.service.emit(source['id'], kind, data)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(source, self.service._get('bots', source['bot_id']), [], emit)
        self.assertEqual(self.requests, [])
        self.assertIn(('text_delta', {'text': 'Authorization rejection verified.'}), events)
        self.assertFalse(any(kind == 'failed' for kind, _ in events))

    def test_care_and_foreign_requester_are_denied(self):
        child, parent, source = self.setup_edge('Care')
        with self.assertRaisesRegex(ServiceError, 'Only the child owner'):
            self.unlink(source)
        child['source_bot_id'] = 'foreign'
        self.service._put('tasks', child)
        source['bot_id'] = self.desk['id']
        self.service._put('tasks', source)
        with self.assertRaisesRegex(ServiceError, 'Only the child owner'):
            self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_wrong_identity_and_untrimmed_ids_are_rejected(self):
        child, parent, source = self.setup_edge()
        for changes in ({'parent_id': 't_33333333'}, {'child_id': 't_33333333'},
                        {'child_id': ' t_22222222'}, {'parent_id': 't_22222222'}, {'reason': ''}):
            with self.subTest(changes=changes), self.assertRaises(ServiceError):
                self.unlink(source, **changes)
        self.assertEqual(self.requests, [])

    def test_child_decision_pause_cancellation_and_claim_guards(self):
        child, parent, source = self.setup_edge()
        self.service._put('decisions', dict(id='child-decision', task_id=child['id'], status='pending'))
        with self.assertRaisesRegex(ServiceError, 'decision'):
            self.unlink(source)
        self.service._put('decisions', dict(id='child-decision', task_id=child['id'], status='executed'))
        for changes in ({'status': 'paused'}, {'status': 'cancelled'}, {'status': 'working'}, {'_conversation_epoch': 'old'}):
            with self.subTest(changes=changes):
                self.service._put('tasks', child | changes)
                with self.assertRaises(ServiceError):
                    self.unlink(source)
        self.service._put('tasks', child)
        for method in ('_task_work_is_paused', '_background_pending'):
            with patch.object(self.service, method, return_value=True), self.assertRaises(ServiceError):
                self.unlink(source)
        self.service._kanban_claiming = 1
        with self.assertRaisesRegex(ServiceError, 'dispatch'):
            self.unlink(source)
        self.service._kanban_claiming = 0
        self.assertEqual(self.requests, [])

    def test_executing_tools_and_other_owner_workers_are_guarded(self):
        child, parent, source = self.setup_edge()
        other = dict(child, id=str(uuid4()), status='working', _native_outcome_id='t_44444444')
        other['_execution_session_id'] = other['id']
        other['_assignment_task_id'] = child['id']
        self.service._put('tasks', other)
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        other['status'] = 'completed'
        self.service._put('tasks', other)
        self.service._put('native_receipts', dict(id='link', task_id=source['id'], state='executing', action={'name': 'kanban_link'}))
        with self.assertRaisesRegex(ServiceError, 'graph operation'):
            self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_finished_and_failed_tasks_do_not_keep_stale_receipts_that_block_unlink(self):
        child, parent, source = self.setup_edge()
        other = dict(child, id=str(uuid4()), status='completed', _native_outcome_id='t_44444444')
        other['_execution_session_id'] = other['id']
        other['_assignment_task_id'] = child['id']
        self.service._put('tasks', other)
        self.service._put('native_receipts', dict(
            id='stale-read', task_id=other['id'], state='executing', action={'name': 'read_file'}))
        self.assertEqual(self.unlink(source)['status'], 'unlinked')
        failed = dict(other, id=str(uuid4()), status='failed', _native_outcome_id='t_55555555')
        failed['_execution_session_id'] = failed['id']
        self.service._put('tasks', failed)
        self.service._put('native_receipts', dict(
            id='stale-terminal', task_id=failed['id'], state='executing', action={'name': 'terminal'},
            _process_ended_at='2026-09-26T23:00:00Z'))
        self.assertEqual(self.unlink(source)['status'], 'unlinked')
        self.service.running[failed['id']] = SimpleNamespace(is_alive=lambda: True)
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.service.running.pop(failed['id'])
        self.snapshot_rows.append(dict(
            external_id=failed['id'], id='t_55555555', status='running', current_run_id='9', worker_pid='42'))
        self.service._put('native_receipts', dict(
            id='live-read', task_id=failed['id'], state='executing', action={'name': 'read_file'}))
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.snapshot_rows[:] = [row for row in self.snapshot_rows if row['external_id'] != failed['id']]
        self.service._put('native_receipts', dict(
            id='live-read', task_id=failed['id'], state='completed', action={'name': 'read_file'}))
        open_command = dict(failed, id=str(uuid4()), status='failed', _native_outcome_id='t_77777777')
        open_command['_execution_session_id'] = open_command['id']
        self.service._put('tasks', open_command)
        self.service._put('native_receipts', dict(
            id='open-terminal', task_id=open_command['id'], state='executing', action={'name': 'terminal'}))
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.service._put('native_receipts', dict(
            id='open-terminal', task_id=open_command['id'], state='completed', action={'name': 'terminal'},
            _process_ended_at='2026-09-26T23:00:00Z'))
        hidden = dict(failed, id=str(uuid4()), status='completed', _native_outcome_id='t_88888888')
        hidden['_execution_session_id'] = hidden['id']
        self.service._put('tasks', hidden)
        self.hidden_snapshots.add(hidden['id'])
        self.service._put('native_receipts', dict(
            id='hidden-read', task_id=hidden['id'], state='executing', action={'name': 'read_file'}))
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.service._put('native_receipts', dict(
            id='hidden-read', task_id=hidden['id'], state='completed', action={'name': 'read_file'}))
        working = dict(failed, id=str(uuid4()), status='working', _native_outcome_id='t_66666666')
        working['_execution_session_id'] = working['id']
        self.service._put('tasks', working)
        self.service._put('native_receipts', dict(
            id='live-read', task_id=working['id'], state='executing', action={'name': 'read_file'}))
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.service._put('tasks', working | {'status': 'completed'})
        self.service._put('native_receipts', dict(
            id='stale-link', task_id=working['id'], state='executing', action={'name': 'kanban_link'}))
        with self.assertRaisesRegex(ServiceError, 'graph operation'):
            self.unlink(source)
        self.assertEqual(len(self.requests), 2)

    def test_unlink_receipt_scan_ignores_unrelated_finished_tasks(self):
        child, parent, source = self.setup_edge()
        related = dict(child, id=str(uuid4()), status='completed', _native_outcome_id='t_44444444')
        related['_execution_session_id'] = related['id']
        related['_assignment_task_id'] = child['id']
        self.service._put('tasks', related)
        self.service._put('native_receipts', dict(
            id='related-read', task_id=related['id'], state='executing', action={'name': 'read_file'}))
        parent_peer = dict(parent, id=str(uuid4()), status='failed', _native_outcome_id='t_aaaaaaaa')
        parent_peer['_execution_session_id'] = parent['_execution_session_id']
        self.service._put('tasks', parent_peer)
        self.service._put('native_receipts', dict(
            id='parent-peer-read', task_id=parent_peer['id'], state='executing', action={'name': 'read_file'}))
        foreign = self.write('/v1/bots', {'name': 'Foreign'})
        unrelated = dict(child, id=str(uuid4()), bot_id=foreign['id'], session_id=foreign['session_id'],
                         status='failed', _native_outcome_id='t_99999999')
        unrelated['_execution_session_id'] = unrelated['id']
        self.service._put('tasks', unrelated)
        self.service._put('native_receipts', dict(
            id='unrelated-read', task_id=unrelated['id'], state='executing', action={'name': 'read_file'}))
        queried = []
        original = self.service._native_task_receipts

        def spy(task_id, **kwargs):
            queried.append(task_id)
            return original(task_id, **kwargs)

        with patch.object(self.service, '_native_task_receipts', side_effect=spy):
            self.assertEqual(self.unlink(source)['status'], 'unlinked')
        self.assertIn(related['id'], queried)
        self.assertIn(parent_peer['id'], queried)
        self.assertNotIn(unrelated['id'], queried)
        requested = [task_id for call in self.service.kanban.call.call_args_list
                     if call.args and call.args[0].get('op') == 'snapshot'
                     for task_id in call.args[0].get('external_ids') or []]
        self.assertIn(related['id'], requested)
        self.assertIn(parent_peer['id'], requested)
        self.assertNotIn(unrelated['id'], requested)

    def test_adapter_errors_reach_caller_and_release_all_fences(self):
        child, parent, source = self.setup_edge()
        call = self.service.kanban.call.side_effect
        def failing(request):
            if request['op'] == 'unlink_dependency':
                from coconut.kanban_scheduler import KanbanRequestError
                raise KanbanRequestError('The child must be TODO and both native outcomes must have no active run.')
            return call(request)
        self.service.kanban.call.side_effect = failing
        with self.assertRaisesRegex(ServiceError, 'child must be TODO'):
            self.unlink(source)
        self.assertFalse(self.service._dependency_unlinking)
        self.assertTrue(self.service.dispatch_gate.acquire(blocking=False))
        self.service.dispatch_gate.release()

    def test_error_after_native_commit_returns_verified_receipt(self):
        from coconut.kanban_scheduler import KanbanRequestError
        child, parent, source = self.setup_edge()
        call = self.service.kanban.call.side_effect
        saved = []
        def lost(request):
            if request['op'] == 'unlink_receipt':
                return {'receipt': saved[0] if saved else None}
            result = call(request)
            if request['op'] == 'unlink_dependency':
                saved.append(result)
                raise KanbanRequestError('Adapter error after native commit')
            return result
        self.service.kanban.call.side_effect = lost
        self.assertEqual(self.unlink(source), saved[0])
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.service._dependency_unlinking)

    def test_unresolved_transport_error_is_unconfirmed_not_rejected(self):
        child, parent, source = self.setup_edge()
        call = self.service.kanban.call.side_effect
        def lost(request):
            if request['op'] == 'unlink_dependency':
                raise RuntimeError('Lost adapter response')
            return call(request)
        self.service.kanban.call.side_effect = lost
        result = self.unlink(source)
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertEqual(result['child_id'], self.arguments['child_id'])
        self.assertFalse(self.service._dependency_unlinking)

    def test_idle_dispatch_wait_does_not_reject_unlink(self):
        child, parent, source = self.setup_edge()
        waiting = threading.Event()
        def idle_tail():
            with self.service.changed:
                waiting.set()
                self.service.changed.wait(.1)
        with patch.object(self.service, '_dispatch_pending_locked', side_effect=idle_tail), \
                patch.object(self.service, '_dispatch_foreground'):
            dispatcher = threading.Thread(target=self.service._dispatch_pending)
            dispatcher.start()
            try:
                self.assertTrue(waiting.wait(3))
                self.assertEqual(self.unlink(source)['status'], 'unlinked')
            finally:
                dispatcher.join(3)
            self.assertFalse(dispatcher.is_alive())

    def test_saved_receipt_is_read_after_child_starts_without_repeating_mutation(self):
        child, parent, source = self.setup_edge()
        receipt = self.unlink(source)
        self.service._put('tasks', child | {'status': 'working'})
        call = self.service.kanban.call.side_effect
        def saved(request):
            if request['op'] == 'unlink_receipt':
                return {'receipt': receipt}
            return call(request)
        self.service.kanban.call.side_effect = saved
        self.assertEqual(self.unlink(source), receipt)
        self.assertEqual(len(self.requests), 1)
        source['bot_id'] = parent['bot_id']
        self.service._put('tasks', source)
        with self.assertRaisesRegex(ServiceError, 'Only the child owner'):
            self.unlink(source)

    def test_receipt_lookup_revalidates_changed_authority(self):
        child, parent, source = self.setup_edge()
        call = self.service.kanban.call.side_effect
        def changed(request):
            if request['op'] == 'unlink_receipt':
                self.service._put('tasks', child | {'source_bot_id': 'foreign'})
                return {'receipt': {'status': 'unlinked'}}
            return call(request)
        self.service.kanban.call.side_effect = changed
        with self.assertRaisesRegex(ServiceError, 'Only the child owner'):
            self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_service_writes_and_dispatch_are_fenced_without_holding_service_lock(self):
        child, parent, source = self.setup_edge()
        entered, release, write_started, wrote = (threading.Event() for _ in range(4))
        errors = []
        call = self.service.kanban.call.side_effect
        def blocking(request):
            if request['op'] == 'unlink_dependency':
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test release timed out')
            return call(request)
        self.service.kanban.call.side_effect = blocking
        def unlink():
            try:
                self.unlink(source)
            except BaseException as error:
                errors.append(error)
        def pause():
            write_started.set()
            with self.service.transaction():
                self.service._put('tasks', child | {'status': 'paused'})
                wrote.set()
        worker = threading.Thread(target=unlink)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            writer = threading.Thread(target=pause)
            writer.start()
            self.assertTrue(write_started.wait(3))
            self.assertFalse(wrote.wait(.05))
            with self.service.lock:
                self.assertFalse(self.service.dispatch_gate.acquire(blocking=False))
                with patch.object(self.service, '_dispatch_chat') as chat:
                    self.service._dispatch_foreground()
                    chat.assert_not_called()
            self.assertEqual(self.service._get('tasks', child['id'])['status'], 'queued')
        finally:
            release.set()
            worker.join(3)
            if 'writer' in locals():
                writer.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(wrote.is_set())
        self.assertFalse(worker.is_alive())
        self.assertFalse(writer.is_alive())


class DependencyJournalTests(unittest.TestCase):
    def setUp(self):
        self.board = Board.__new__(Board)
        self.conn = self.board.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE coconut_bindings (external_id TEXT PRIMARY KEY, native_id TEXT, intent TEXT);
            CREATE TABLE coconut_unlinks (request_id TEXT PRIMARY KEY, intent TEXT, generation INTEGER, receipt TEXT);
            CREATE TABLE tasks (id TEXT PRIMARY KEY, tenant TEXT, status TEXT, current_run_id INTEGER, worker_pid INTEGER);
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);
        ''')
        for identity, external, tenant, state in [('t_11111111', 'parent', 'care', 'review'), ('t_22222222', 'child', 'dev', 'todo'),
                                                  ('t_33333333', 'other', 'care', 'review')]:
            self.conn.execute('INSERT INTO tasks VALUES (?,?,?,NULL,NULL)', (identity, tenant, state))
            self.conn.execute('INSERT INTO coconut_bindings VALUES (?,?,?)', (external, identity, 'original'))
        self.arguments = dict(op='unlink_dependency', request_id='stable-request', parent_id='t_11111111', child_id='t_22222222',
            parent_external_id='parent', child_external_id='child', parent_bot_id='care', child_bot_id='dev',
            source_task_id='desk-task', source_bot_id='desk', source_turn_id='turn', source_native_id=None, reason='Resource released.')
        self.link()
        self.conn.execute("INSERT INTO task_links VALUES ('t_33333333','t_22222222')")
        def get_task(conn, identity):
            row = conn.execute('SELECT * FROM tasks WHERE id=?', (identity,)).fetchone()
            return SimpleNamespace(**dict(zip(('id', 'tenant', 'status', 'current_run_id', 'worker_pid'), row))) if row else None
        self.board.kb = Mock(get_task=get_task, write_txn=lambda conn: contextlib.nullcontext(), unlink_tasks=Mock(side_effect=self.fake_unlink))

    def event(self, kind):
        return self.conn.execute('INSERT INTO task_events(task_id,kind,payload) VALUES (?,?,?)',
            (self.arguments['child_id'], kind, json.dumps({'parent': self.arguments['parent_id'], 'child': self.arguments['child_id']}))).lastrowid

    def link(self):
        self.conn.execute('INSERT INTO task_links VALUES (?,?)', (self.arguments['parent_id'], self.arguments['child_id']))
        self.event('linked')

    def fake_unlink(self, conn, parent, child):
        # Deliberately not the native kernel. Real-kernel acceptance is separate.
        self.assertFalse(conn.in_transaction)
        self.assertIsNotNone(conn.execute('SELECT intent FROM coconut_unlinks').fetchone())
        conn.execute('DELETE FROM task_links WHERE parent_id=? AND child_id=?', (parent, child))
        self.event('unlinked')
        return True

    def unlink(self, **changes):
        return self.board._unlink_dependency(self.arguments | changes)

    def test_journal_reason_identity_and_exact_native_mutation_receipt(self):
        bindings = list(self.conn.execute('SELECT * FROM coconut_bindings'))
        tasks = list(self.conn.execute('SELECT * FROM tasks'))
        receipt = self.unlink()
        intent, generation, saved = self.conn.execute('SELECT intent,generation,receipt FROM coconut_unlinks').fetchone()
        self.assertEqual(json.loads(intent), self.arguments)
        self.assertEqual(receipt, json.loads(saved))
        self.assertEqual(receipt['linked_event_id'], generation)
        self.assertEqual(self.conn.execute('SELECT kind FROM task_events WHERE id=?', (receipt['unlinked_event_id'],)).fetchone()[0], 'unlinked')
        self.assertEqual(list(self.conn.execute('SELECT * FROM task_links')), [('t_33333333', 't_22222222')])
        self.assertEqual(list(self.conn.execute('SELECT * FROM coconut_bindings')), bindings)
        self.assertEqual(list(self.conn.execute('SELECT * FROM tasks')), tasks)
        self.assertEqual(self.unlink(), receipt)
        self.board.kb.unlink_tasks.assert_called_once()

    def test_dependencies_recorded_in_creation_have_a_generation(self):
        self.conn.execute("DELETE FROM task_events WHERE task_id=?", (self.arguments['child_id'],))
        self.conn.execute('INSERT INTO task_events(task_id,kind,payload) VALUES (?,?,?)',
                          (self.arguments['child_id'], 'created', json.dumps({'parents': [self.arguments['parent_id']]})))
        receipt = self.unlink()
        self.assertEqual(receipt['status'], 'unlinked')
        self.assertEqual(self.unlink(), receipt)
        self.board.kb.unlink_tasks.assert_called_once()

    def test_lost_response_and_readded_edge_are_never_deleted_on_replay(self):
        receipt = self.unlink()
        # Crash before the Coconut receipt commit, followed by a native relink.
        self.conn.execute('UPDATE coconut_unlinks SET receipt=NULL')
        self.link()
        self.assertEqual(self.unlink(), receipt)
        self.board.kb.unlink_tasks.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM task_links').fetchone()[0], 2)
        self.assertEqual(self.unlink(), receipt)

    def test_generation_change_immediately_before_native_delete_preserves_new_edge(self):
        def racing(conn, parent, child):
            self.fake_unlink(conn, parent, child)
            self.link()
            return self.fake_unlink(conn, parent, child)
        self.board.kb.unlink_tasks.side_effect = racing
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'generation changed'):
            self.unlink()
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?',
                             (self.arguments['parent_id'], self.arguments['child_id'])).fetchone())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM coconut_unlink_guard').fetchone()[0], 0)
        # The prior generation was removed by the racer; replay only reports it.
        self.assertEqual(self.board._unlink_dependency(self.arguments, receipt_only=True)['status'], 'unlinked')
        self.board.kb.unlink_tasks.assert_called_once()

    def test_receipt_only_reconciliation_never_writes_or_repeats_unlink(self):
        self.assertIsNone(self.board._unlink_dependency(self.arguments, receipt_only=True))
        self.board.kb.unlink_tasks.assert_not_called()
        receipt = self.unlink()
        self.conn.execute('UPDATE coconut_unlinks SET receipt=NULL')
        self.conn.execute("UPDATE tasks SET status='running',current_run_id=10 WHERE id=?", (self.arguments['child_id'],))
        before = self.conn.total_changes
        self.assertEqual(self.board._unlink_dependency(self.arguments, receipt_only=True), receipt)
        self.assertEqual(self.conn.total_changes, before)
        self.board.kb.unlink_tasks.assert_called_once()

    def test_interruption_before_native_unlink_can_resume_same_generation(self):
        self.board.kb.unlink_tasks.side_effect = RuntimeError('interrupted before native call')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.unlink()
        self.board.kb.unlink_tasks.side_effect = self.fake_unlink
        self.assertEqual(self.unlink()['status'], 'unlinked')

    def test_interruption_after_native_commit_reconciles_without_mutation(self):
        def interrupted(*args):
            self.fake_unlink(*args)
            raise RuntimeError('lost native response')
        self.board.kb.unlink_tasks.side_effect = interrupted
        with self.assertRaisesRegex(RuntimeError, 'lost native'):
            self.unlink()
        self.assertEqual(self.unlink()['status'], 'unlinked')
        self.board.kb.unlink_tasks.assert_called_once()

    def test_relinked_generation_without_original_receipt_is_unresolved(self):
        self.board.kb.unlink_tasks.side_effect = RuntimeError('before mutation')
        with self.assertRaises(RuntimeError):
            self.unlink()
        self.event('linked')
        with self.assertRaisesRegex(ValueError, 'linked again'):
            self.unlink()
        self.board.kb.unlink_tasks.assert_called_once()

    def test_unlink_of_a_later_generation_is_not_an_original_receipt(self):
        self.board.kb.unlink_tasks.side_effect = RuntimeError('before mutation')
        with self.assertRaises(RuntimeError):
            self.unlink()
        self.event('linked')
        self.event('unlinked')
        with self.assertRaisesRegex(ValueError, 'linked again'):
            self.unlink()
        self.board.kb.unlink_tasks.assert_called_once()

    def test_absent_edge_without_native_receipt_is_unresolved(self):
        self.board.kb.unlink_tasks.side_effect = RuntimeError('before mutation')
        with self.assertRaises(RuntimeError):
            self.unlink()
        self.conn.execute('DELETE FROM task_links WHERE parent_id=?', (self.arguments['parent_id'],))
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.unlink()
        self.board.kb.unlink_tasks.assert_called_once()

    def test_exact_edge_tenant_binding_and_native_activity_are_revalidated(self):
        for changes in ({'parent_id': 't_33333333', 'parent_external_id': 'other'}, {'child_bot_id': 'foreign'},
                        {'child_external_id': 'other'}, {'parent_bot_id': 'foreign'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.unlink(**changes)
        for identity in ('t_11111111', 't_22222222'):
            for field, value in (('current_run_id', 7), ('worker_pid', 100), ('status', 'running')):
                before = self.conn.execute('SELECT '+field+' FROM tasks WHERE id=?', (identity,)).fetchone()[0]
                self.conn.execute('UPDATE tasks SET '+field+'=? WHERE id=?', (value, identity))
                with self.subTest(identity=identity, field=field), self.assertRaisesRegex(ValueError, 'TODO'):
                    self.unlink()
                self.conn.execute('UPDATE tasks SET '+field+'=? WHERE id=?', (before, identity))
        self.board.kb.unlink_tasks.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM coconut_unlinks').fetchone()[0], 0)

    def test_scoped_request_allows_unrelated_native_worker_without_changing_it(self):
        self.conn.execute("INSERT INTO tasks VALUES ('t_44444444','dev','running',99,123)")
        scope = ['t_11111111', 't_22222222']
        with self.assertRaisesRegex(ValueError, 'other native work'):
            self.unlink()
        receipt = self.unlink(guarded_native_ids=scope)
        self.assertEqual(receipt['status'], 'unlinked')
        self.assertEqual(self.conn.execute("SELECT status,current_run_id,worker_pid FROM tasks WHERE id='t_44444444'").fetchone(),
                         ('running', 99, 123))
        self.assertEqual(self.unlink(guarded_native_ids=scope + ['t_44444444']), receipt)
        self.board.kb.unlink_tasks.assert_called_once()

    def test_scoped_request_rejects_related_claims_and_missing_identity(self):
        scope = ['t_11111111', 't_22222222', 't_33333333']
        for field, value in (('current_run_id', 99), ('worker_pid', 123), ('status', 'running')):
            old = self.conn.execute('SELECT ' + field + " FROM tasks WHERE id='t_33333333'").fetchone()[0]
            self.conn.execute('UPDATE tasks SET ' + field + "=? WHERE id='t_33333333'", (value,))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'related native work'):
                self.unlink(guarded_native_ids=scope)
            self.conn.execute('UPDATE tasks SET ' + field + "=? WHERE id='t_33333333'", (old,))
        with self.assertRaisesRegex(ValueError, 'related native work'):
            self.unlink(guarded_native_ids=scope + ['missing'])
        self.board.kb.unlink_tasks.assert_not_called()

    def test_scoped_request_requires_both_endpoints(self):
        for scope in ([], ['t_11111111'], ['t_22222222'], 'bad', [None]):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, 'scope is incomplete'):
                self.unlink(guarded_native_ids=scope)
        self.board.kb.unlink_tasks.assert_not_called()

    def test_replay_cannot_change_reason_or_requester(self):
        self.unlink()
        for changes in ({'reason': 'Different permission'}, {'source_bot_id': 'care'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'identity changed'):
                self.unlink(**changes)


class DependencyAdapterSerializationTests(unittest.TestCase):
    def test_lost_response_reaps_real_adapter_before_releasing_request_lock(self):
        import queue
        import subprocess
        from coconut.kanban_scheduler import KanbanScheduler
        with tempfile.TemporaryDirectory() as home:
            adapter = KanbanScheduler(Mock(), home)
            process = adapter.process = subprocess.Popen([sys.executable, '-u', '-c',
                "import sys,time; sys.stdin.readline(); print('malformed frame'); time.sleep(30)"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.addCleanup(adapter.close)
            adapter.frames = queue.Queue()
            reader = threading.Thread(target=adapter._read, args=(process, adapter.frames), daemon=True)
            reader.start()
            with self.assertRaises(json.JSONDecodeError):
                adapter.call({'op': 'unlink_dependency'})
            self.assertIsNotNone(process.poll())
            reader.join(3)
            self.assertFalse(reader.is_alive())
            self.assertTrue(adapter.lock.acquire(blocking=False))
            adapter.lock.release()

    def test_bind_waits_for_unlink_on_the_existing_adapter_request_lock(self):
        import queue
        from coconut.kanban_scheduler import KanbanScheduler
        with tempfile.TemporaryDirectory() as home:
            adapter = KanbanScheduler(Mock(), home)
            adapter.frames = queue.Queue()
            entered, bind_started, bound = (threading.Event() for _ in range(3))
            writes, results, errors = [], [], []
            def write(line):
                request = json.loads(line)
                writes.append(request['op'])
                if request['op'] == 'unlink_dependency':
                    entered.set()
                else:
                    adapter.frames.put({'result': {'ok': True}})
            adapter.process = Mock()
            adapter.process.poll.return_value = None
            adapter.process.stdin.write.side_effect = write
            def call(op):
                try:
                    if op == 'bind':
                        bind_started.set()
                    results.append(adapter.call({'op': op}))
                    if op == 'bind':
                        bound.set()
                except BaseException as error:
                    errors.append(error)
            unlink = threading.Thread(target=call, args=('unlink_dependency',))
            bind = threading.Thread(target=call, args=('bind',))
            unlink.start()
            try:
                self.assertTrue(entered.wait(3))
                bind.start()
                self.assertTrue(bind_started.wait(3))
                self.assertFalse(bound.wait(.05))
                self.assertEqual(writes, ['unlink_dependency'])
            finally:
                adapter.frames.put({'result': {'status': 'unlinked'}})
                unlink.join(3)
                if bind.ident is not None:
                    bind.join(3)
            self.assertFalse(unlink.is_alive())
            self.assertFalse(bind.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(writes, ['unlink_dependency', 'bind'])
            self.assertEqual(results, [{'status': 'unlinked'}, {'ok': True}])


class DependencyUnlinkProviderTests(unittest.TestCase):
    setUp = provider_fixtures.ProviderTests.setUp
    tearDown = provider_fixtures.ProviderTests.tearDown
    fake_hermes = provider_fixtures.ProviderTests.fake_hermes

    def test_registered_tool_transport_preserves_success_and_actionable_error(self):
        arguments = dict(parent_id='t_11111111', child_id='t_22222222', reason='Resource released.')
        provider = self.fake_hermes(
            "receipt = json.loads(registry.dispatch('coconut_unlink_dependency', " + repr(arguments) + "))\n"
            "assert receipt['status'] == 'unlinked', receipt\n"
            "receipt = json.loads(registry.dispatch('coconut_unlink_dependency', " + repr(arguments) + "))\n"
            "assert receipt == {'status': 'rejected', 'error': 'Only the child owner or its requesting bot can unlink this dependency.'}, receipt\n"
            "return {'completed': True, 'final_response': 'Receipt verified.'}\n")
        seen = []
        def emit(kind, data):
            if kind == 'dependency_unlink':
                seen.append(data)
                if len(seen) == 1:
                    return {'status': 'unlinked', **arguments}
                raise ServiceError(403, 'Only the child owner or its requesting bot can unlink this dependency.')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(seen, [arguments, arguments])


if __name__ == '__main__':
    unittest.main()
