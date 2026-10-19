"""Claim-only continuation through the installed kernel and service dispatcher."""
import copy
import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import ServiceError
import test_native_kanban as fixtures


@unittest.skipUnless(fixtures.SOURCE and fixtures.PYTHON, 'Set Coconut Hermes paths for native integration checks.')
class NativeClaimContinuationTests(unittest.TestCase):
    setUp = fixtures.NativeKanbanTests.setUp
    write = fixtures.NativeKanbanTests.write
    native_program = fixtures.NativeKanbanTests.native_program

    def prepare(self, state='done', caller='requester'):
        requester = self.write('/v1/bots', {'name': 'Original requester'})
        original = self.write('/v1/bots/' + requester['id'] + '/messages', {'text': 'Keep all changes local.'})
        original = self.service._get('tasks', original['id'])
        original.update(status='working', _chat_session=True)
        self.service._put('tasks', original)
        delegated = self.service._store_bot_message(original['id'], dict(
            target_bot_id=self.bot['id'], text='Verify the accepted local correction.'))
        owner = self.service._get('tasks', delegated['task_id'])
        owner.update(_execution_session_id=owner['id'],
                     _conversation_epoch=self.service._setting('conversation_epoch'))
        repo = Path(self.tmp.name) / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '--allow-empty', '-qm', 'Synthetic fixture'], check=True)
        binding = json.loads(self.native_program('import json\nfrom coconut.work_workspace import acquire\n'
            'print(json.dumps(acquire(sys.argv[2], sys.argv[3], sys.argv[4])))',
            str(self.service.root / 'workspaces'), owner['_execution_session_id'], str(repo)))
        workspace = Path(binding['path'])
        (workspace / 'pending.txt').write_text('Uncommitted correction')
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        identity = self.service.kanban.tasks[owner['id']]['id']
        operation = {'done': "complete_task(board.conn, sys.argv[2], summary='Original input complete')",
                     'scheduled': "schedule_task(board.conn, sys.argv[2], reason='Waiting for fresh input')",
                     'blocked': "block_task(board.conn, sys.argv[2], reason='Waiting for corrected facts')",
                     'review': "request_review(board.conn, sys.argv[2], summary='Original review evidence', metadata={'acceptance': 'Local previews only'})",
                     'ready': "add_comment(board.conn, sys.argv[2], 'default', 'https://github.com/example/app/pull/6')"}[state]
        self.native_program('board.kb.' + operation, identity)
        self.service.kanban.call({'op': 'snapshot'})
        owner.update(status='completed', summary='Original input complete', _kanban_claim=dict(
            native_id=identity, input=row['input'], run_id='completed-run'))
        if state == 'ready':
            owner['status'] = 'queued'
            self.assertEqual(self.service.kanban.tasks[owner['id']]['dispatch_blocker'], 'active_pr')
            self.assertIsNone(self.service.kanban.tasks[owner['id']]['current_run_id'])
        self.service._put('tasks', owner)
        original['status'] = 'completed'
        self.service._put('tasks', original)
        caller_bot = self.bot if caller == 'owner' else requester
        if caller == 'stranger':
            caller_bot = self.write('/v1/bots', {'name': 'Unrelated'})
        source = self.write('/v1/bots/' + caller_bot['id'] + '/messages', {'text': 'New review facts are available.'})
        source = self.service._get('tasks', source['id'])
        source.update(status='working', _chat_session=True)
        self.service._put('tasks', source)
        self.assertNotIn('_native_outcome_id', owner)
        self.assertNotIn('_work_context_id', owner)
        self.assertIsNone(self.service.kanban.tasks[owner['id']]['input_id'])
        return owner, source, identity, workspace

    def test_review_stale_cache_rejects_native_reviewer_claim(self):
        owner, source, identity, workspace = self.prepare('review')
        self.native_program("assert board.kb.claim_review_task(board.conn, sys.argv[2])", identity)
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'review')
        with self.assertRaises(ServiceError):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'running')
        self.service._reconcile_kanban()
        self.assertEqual(self.native_program(
            "print(board.kb.get_task(board.conn, sys.argv[2]).status)", identity), 'running')

    def test_review_claim_after_refresh_preserves_pending_input(self):
        self.check_late_reviewer_claim()

    def test_review_claim_during_reopen_preserves_pending_input(self):
        self.check_late_reviewer_claim(during_reopen=True)

    def test_review_claim_during_bind_preserves_pending_input(self):
        self.check_late_reviewer_claim(during_bind=True)

    def check_late_reviewer_claim(self, during_reopen=False, during_bind=False):
        owner, source, identity, workspace = self.prepare('review')
        evidence = self.review_evidence(identity)
        adapter_call = self.service.kanban.call
        def claim_after_snapshot(request, *args, **kwargs):
            result = adapter_call(request, *args, **kwargs)
            if request['op'] == 'snapshot':
                self.native_program("assert board.kb.claim_review_task(board.conn, sys.argv[2])", identity)
            return result
        if during_reopen or during_bind:
            receipt = self.resume(source, identity)
        else:
            with patch.object(self.service.kanban, 'call', side_effect=claim_after_snapshot):
                receipt = self.resume(source, identity)
        queued = self.service._get('tasks', owner['id'])
        row = next(r for r in self.service._kanban_rows() if r['id'] == owner['id'])
        self.native_program('''import json
row = json.loads(sys.argv[2])
if sys.argv[4] == 'True':
    bind = board.bind
    def raced_bind(identity, external_id):
        bind(identity, external_id)
        assert board.kb.claim_review_task(board.conn, identity)
    board.bind = raced_bind
if sys.argv[3] == 'True':
    reopen = board.kb.reopen_review_task
    def raced_reopen(conn, identity):
        assert board.kb.claim_review_task(conn, identity)
        return reopen(conn, identity)
    board.kb.reopen_review_task = raced_reopen
board.sync([row])
''', json.dumps(row), str(during_reopen), str(during_bind))
        self.service.kanban.call({'op': 'snapshot'})
        native = copy.deepcopy(self.service.kanban.tasks[owner['id']])
        self.assertEqual(native['status'], 'running')
        self.assertEqual(native['claim_source_status'], 'review')
        self.assertIsNone(native['worker_pid'])
        self.assertNotEqual(native['input_id'], queued['_native_turn_id'])
        self.service._reconcile_kanban()
        # Even a stale finish message that lacks the projected source cannot
        # turn the reviewer's PID-less run into an interrupted preparation.
        self.native_program('''task = board.kb.get_task(board.conn, sys.argv[2])
for state in ('ready', 'done', 'blocked', 'review', 'scheduled', 'archived'):
    assert not board.finish(task.id, task.current_run_id, state, 'Stale preparation')
assert board.kb.get_task(board.conn, task.id) == task
''', identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[owner['id']], native)
        self.assertEqual(self.service._get('tasks', owner['id']), queued)
        self.assertEqual(self.review_evidence(identity), evidence)
        self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='Reviewer accepted')", identity)
        captured, release = [], threading.Event()
        self.addCleanup(release.set)
        def run(task, bot, history, emit):
            captured.append(copy.deepcopy(task))
            release.wait(10)
            emit('completed', {'summary': 'Follow-up received'})
        self.provider.start_task = run
        self.service._dispatch_pending()
        worker = self.service.running.get(owner['id'])
        self.assertIsNotNone(worker)
        release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(captured), 1)
        current = self.service.kanban.tasks[owner['id']]
        self.assertNotEqual(current['id'], identity)
        self.assertEqual(current['input_id'], queued['_native_turn_id'])
        self.assertEqual(captured[0]['_execution_session_id'], owner['_execution_session_id'])
        self.assertEqual(captured[0]['_kanban_workspace'], str(workspace))
        self.assertEqual(self.resume(source, identity), receipt)
        self.assertEqual((workspace / 'pending.txt').read_text(), 'Uncommitted correction')

    def test_ready_claim_continues_during_service_idle_then_dispatches(self):
        self.check_dispatch('ready', dispatch_idle=True)

    def resume(self, source, identity):
        return self.service.emit(source['id'], 'work_continue', dict(
            task_id=identity, message='The review passed. Verify locally; do not publish.'))

    def age_binding_and_restart(self, owner):
        # Real bridge snapshots retain only the latest 32 legacy terminal bindings.
        self.native_program('''
import json
row = json.loads(sys.argv[2])
for index in range(33):
    newer = dict(row, id='newer-' + str(index), input='newer-' + str(index))
    board.sync([newer])
    native_id = board.conn.execute('SELECT native_id FROM coconut_bindings WHERE external_id=?',
                                   (newer['id'],)).fetchone()[0]
    board.kb.complete_task(board.conn, native_id, summary='Newer input complete')
''', json.dumps(self.service._kanban_row(owner, owner, 'ready', 10)))
        from coconut.kanban_scheduler import KanbanScheduler
        self.service.kanban.close()
        self.service.kanban = KanbanScheduler(self.provider, self.service.root)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertNotIn(owner['id'], self.service.kanban.tasks)

    def check_dispatch(self, state, caller='requester', interrupt_binding=False, aged=False, dispatch_idle=False):
        owner, source, identity, workspace = self.prepare(state, caller)
        review_evidence = self.review_evidence(identity) if state == 'review' else None
        if aged:
            self.age_binding_and_restart(owner)
        decision = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'],
                        status='rejected', title='Publish?', decided_by='human',
                        created_at=owner['created_at'], outcome='Keep the correction local.')
        self.service._put('decisions', decision)
        previous_runs = int(self.native_program('print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", '
                                               '(sys.argv[2],)).fetchone()[0])', identity))
        before = len(self.service._all('tasks'))
        if dispatch_idle:
            receipts = []
            def continue_at_idle(timeout=None):
                self.assertTrue(self.service._kanban_dispatching)
                receipts.append(self.resume(source, identity))
            with patch.object(self.service.changed, 'wait', side_effect=continue_at_idle):
                self.service._dispatch_pending()
            self.assertEqual(len(receipts), 1)
            receipt = receipts[0]
        else:
            receipt = self.resume(source, identity)
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual(self.resume(source, identity), receipt)
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(queued['_native_outcome_id'], identity)
        self.assertNotEqual(queued['_native_turn_id'], owner['_kanban_claim']['input'])
        for key in ('id', '_execution_session_id', 'thread_id', 'session_id', 'exchange_id',
                    'source_bot_id', 'source_task_id', 'conversation_bot_id'):
            self.assertEqual(queued[key], owner[key], key)
        self.assertEqual(queued['_self_recovery']['decisions'], [self.service._public(d)
            for d in self.service._all('decisions') if d['task_id'] == owner['id']])

        if interrupt_binding:
            row = next(r for r in self.service._kanban_rows() if r['id'] == owner['id'])
            # Lose the sync after committing the successor binding, before its
            # intake is initialized. Reconciliation can observe this half first.
            self.native_program('''
import json
row = json.loads(sys.argv[2])
def interrupted_bind(*args):
    raise RuntimeError('Interrupted after successor binding')
board.bind = interrupted_bind
try:
    board.sync([row])
except RuntimeError as error:
    assert str(error) == 'Interrupted after successor binding'
else:
    raise AssertionError('Expected an interrupted binding')
''', json.dumps(row))
            self.service.kanban.call({'op': 'snapshot'})
            self.service._reconcile_kanban()
            rebound = self.service._get('tasks', owner['id'])
            self.assertNotEqual(rebound['_native_outcome_id'], identity)
            self.assertEqual(rebound['_kanban_claim'], owner['_kanban_claim'])

        captured = []
        release = threading.Event()
        self.addCleanup(release.set)
        def run(task, bot, history, emit):
            captured.append(copy.deepcopy(task))
            self.provider.started.append(task['id'])
            if not release.wait(15):
                raise AssertionError('Test did not release the dispatched worker')
            self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='Follow-up verified locally', expected_run_id=int(sys.argv[3]))",
                                task['_native_outcome_id'], str(self.service._get('tasks', task['id'])['_kanban_claim']['run_id']))
            emit('completed', {'summary': 'Follow-up verified locally'})
        self.provider.start_task = run
        self.service._dispatch_pending()
        worker = self.service.running.get(owner['id'])
        self.assertIsNotNone(worker, 'A queued receipt must dispatch a real native claim')
        current = self.service.kanban.tasks[owner['id']]
        successor = current['id']
        self.assertEqual(current['status'], 'running')
        self.assertEqual(current['input_id'], queued['_native_turn_id'])
        self.assertEqual(current['workspace_path'], str(workspace))
        self.assertEqual(successor == identity, state != 'done')
        self.assertEqual(self.resume(source, identity), receipt)
        if state in ('ready', 'review'):
            with self.assertRaises(ServiceError):
                self.service.emit(source['id'], 'work_continue', dict(
                    task_id=identity, message='Do not start a second worker while this input runs.'))
        # Repeated sync/dispatch while the worker holds its claim cannot spawn twice.
        self.service._dispatch_pending()
        release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.provider.started, [owner['id']], self.service._get('tasks', owner['id']))
        self.assertEqual(len(captured), 1)
        from coconut.hermes_worker import conversation_input
        prompt = conversation_input(dict(operation='start', task=captured[0], history=[]))[0]
        self.assertIn('The review passed. Verify locally; do not publish.', prompt)
        self.assertIn('Keep the correction local.', prompt)
        self.assertEqual(captured[0]['_kanban_workspace'], str(workspace))
        self.assertEqual(captured[0]['_execution_session_id'], owner['_execution_session_id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        self.service._drain_returns()
        self.service._drain_returns()
        self.assertEqual(self.resume(source, identity), receipt)
        returned = [d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']]
        self.assertEqual(len(returned), 1)
        self.assertEqual(returned[0]['target_bot_id'], owner['source_bot_id'])
        self.assertEqual(returned[0]['text'], 'Follow-up verified locally')
        self.assertEqual(self.service._get('decisions', decision['id']), decision)
        if state == 'review':
            self.assertEqual(self.review_evidence(identity), review_evidence)
            self.assertIn('Original review evidence', review_evidence)
        self.assertEqual((workspace / 'pending.txt').read_text(), 'Uncommitted correction')
        item = self.service._get('artifacts', queued['_work_context_id'])
        self.assertEqual(item['_native_outcome_id'], successor)
        self.assertEqual(item['task_id'], owner['id'])
        self.assertEqual(self.native_program('print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", '
                                            '(sys.argv[2],)).fetchone()[0])', successor),
                         str(1 if state == 'done' else previous_runs + 1))
        if state == 'done':
            historical = next(n for n in self.service.kanban.tasks.values() if n['id'] == identity)
            self.assertEqual(historical['status'], 'done')
            self.assertEqual(historical['outcome_summary'], 'Original input complete')
            self.assertEqual(historical['input_id'], owner['_kanban_claim']['input'])
            self.assertEqual(self.native_program('print(board.conn.execute("SELECT count(*) FROM tasks WHERE '
                'idempotency_key LIKE ?", ("coconut-followup:" + sys.argv[2] + ":%",)).fetchone()[0])', owner['id']), '1')
            self.assertIn(identity, self.native_program(
                'print(board.kb.task_graph_contexts(board.conn, [sys.argv[2]]))', successor))

    def test_requester_callback_keeps_native_owner_and_dispatches_once(self):
        import os
        prepare = self.prepare
        def prepare_callback(*args):
            owner, unused, identity, workspace = prepare(*args)
            self.service._put('tasks', dict(unused, status='completed'))
            requester = self.service._get('tasks', owner['source_task_id'])
            requester.update(_execution_session_id=requester['id'], _waiting_on=[],
                             _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', requester)
            report = dict(owner, id=str(uuid4()), _execution_session_id=str(uuid4()),
                          _reply_to_task_id=requester['id'])
            report.pop('_kanban_claim', None)
            self.service._put('tasks', report)
            delivered = self.service._store_bot_message(report['id'], dict(
                target_bot_id=requester['bot_id'], text='The delegated result is saved.'), returning=True)
            caller = self.service._get('tasks', delivered['task_id'])
            self.service._ensure_execution_session(caller)
            caller.update(_native_turn_id='callback-input',
                          _conversation_epoch=requester['_conversation_epoch'])
            self.service._put('tasks', caller)
            rows = [self.service._kanban_row(t, t, 'ready', 10) for t in (report, caller)]
            self.native_program('import json; board.sync(json.loads(sys.argv[2]))', json.dumps(rows))
            self.service.kanban.call({'op': 'snapshot'})
            report_native = self.service.kanban.tasks[report['id']]['id']
            caller_native = self.service.kanban.tasks[caller['id']]['id']
            self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='Report saved')\n"
                "assert board.kb.claim_task(board.conn, sys.argv[3])\n"
                "board.dispatch._set_worker_pid(board.conn, sys.argv[3], int(sys.argv[4]))",
                report_native, caller_native, str(os.getpid()))
            self.service.kanban.call({'op': 'snapshot'})
            card = self.service.kanban.tasks[caller['id']]
            caller.update(status='working', _kanban_claim=dict(native_id=caller_native,
                external_id=caller['id'], input=rows[1]['input'], run_id=card['current_run_id'],
                pid=os.getpid(), context=''))
            self.service._put('tasks', report)
            self.service._put('tasks', caller)
            url = 'https://github.com/example/app/pull/6'
            from coconut.service import now
            for task, activity in ((owner, report), (requester, caller)):
                self.service._put('artifacts', dict(id=str(uuid4()), task_id=task['id'], bot_id=task['bot_id'],
                    created_at=now(), kind='pr', title='Retained PR', status='merged', pr_state='merged', url=url,
                    _review_task_id=task['id'], _activity_task_id=activity['id']))
            return owner, caller, identity, workspace
        self.prepare = prepare_callback
        self.check_dispatch('done')

    def test_requester_root_callback_waits_for_settled_ready_callback_then_dispatches_once(self):
        # The owner's source is a return callback of the requester's root, as in Desk #3748.
        # A completed sibling callback stays READY after its native dependency finished.
        import os
        from datetime import datetime, timedelta, timezone
        prepare = self.prepare
        def prepare_root_callback(*args):
            owner, unused, identity, workspace = prepare(*args)
            self.service._put('tasks', dict(unused, status='completed'))
            root = self.service._get('tasks', owner['source_task_id'])
            root.update(_execution_session_id=root['id'], _waiting_on=[],
                        _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', root)
            def returned(name, callback_owner=None):
                producer = dict(owner, id=str(uuid4()), _execution_session_id=str(uuid4()),
                                source_task_id=root['id'], parent_task_id=root['id'], _reply_to_task_id=root['id'],
                                _native_relay=dict(envelope_id=uuid4().hex, sender_bot_id=root['bot_id']))
                for key in ('_kanban_claim', '_requester_reply_task_id'):
                    producer.pop(key, None)
                if callback_owner:
                    producer['_requester_reply_task_id'] = callback_owner
                self.service._put('tasks', producer)
                delivered = self.service._store_bot_message(producer['id'], dict(
                    target_bot_id=root['bot_id'], text=name + ' result is saved.'), returning=True)
                callback = self.service._get('tasks', delivered['task_id'])
                self.service._ensure_execution_session(callback)
                callback.update(_native_turn_id=name + '-input', _conversation_epoch=root['_conversation_epoch'])
                self.service._put('tasks', callback)
                return producer, callback
            _, source = returned('source')
            owner.update(source_task_id=source['id'], parent_task_id=source['id'], _reply_to_task_id=root['id'],
                         _requester_reply_task_id=source['id'])
            self.service._put('tasks', owner)
            _, reporter = returned('reporter', source['id'])
            _, caller = returned('caller', source['id'])
            stale_producer, stale = returned('stale', source['id'])
            rows = [self.service._kanban_row(t, t, 'ready', 10) for t in (source, reporter, caller, stale)]
            self.native_program('import json; board.sync(json.loads(sys.argv[2]))', json.dumps(rows))
            self.service.kanban.call({'op': 'snapshot'})
            native = {t['id']: self.service.kanban.tasks[t['id']]['id'] for t in (source, reporter, caller, stale)}
            self.native_program('''
kb, conn = board.kb, board.conn
source, reporter, caller, stale, tenant, workspace, producer, pid = sys.argv[2:]
assert kb.complete_task(conn, source, summary='Source callback consumed')
assert kb.complete_task(conn, reporter, summary='Same-PR activity saved')
assert kb.claim_task(conn, caller)
board.dispatch._set_worker_pid(conn, caller, int(pid))
claimed = kb.claim_task(conn, stale)
dependency = kb.create_task(conn, title='Native dependency', body='Fixture', assignee='default', tenant=tenant,
                            workspace_kind='dir', workspace_path=workspace)
board.bind(dependency, producer)
kb.link_tasks(conn, dependency, stale, expected_child_run_id=claimed.current_run_id)
assert kb.block_task(conn, stale, reason='Waiting for the native dependency', kind='dependency',
                     expected_run_id=claimed.current_run_id)
assert kb.complete_task(conn, dependency, summary='Dependency finished')
kb.recompute_ready(conn)
assert kb.get_task(conn, stale).status == 'ready', kb.get_task(conn, stale).status
''', native[source['id']], native[reporter['id']], native[caller['id']], native[stale['id']],
                root['bot_id'], rows[3]['workspace'], stale_producer['id'], str(os.getpid()))
            self.service.kanban.call({'op': 'snapshot'})
            for task, row in zip((source, reporter, caller, stale), rows):
                card = self.service.kanban.tasks[task['id']]
                task.update(status='completed', _kanban_claim=dict(native_id=card['id'], external_id=task['id'],
                    input=row['input'], run_id=card['current_run_id'] or 'completed-run', pid=os.getpid(), context=''))
            caller['status'] = 'working'
            stale.update(summary='Complete.', updated_at=(datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat().replace('+00:00', 'Z'))
            for task in (source, reporter, caller, stale):
                self.service._put('tasks', task)
            card = self.service.kanban.tasks[stale['id']]
            self.assertEqual((card['status'], card['block_kind'], card['dependencies'], card['current_run_id']),
                             ('ready', 'dependency', [], None))
            url = 'https://github.com/example/app/pull/6'
            from coconut.service import now
            for task, review, activity in ((owner, owner, owner), (source, source, reporter)):
                self.service._put('artifacts', dict(id=str(uuid4()), task_id=task['id'], bot_id=task['bot_id'],
                    created_at=now(), kind='pr', title='Retained PR', status='merged', pr_state='merged', url=url,
                    _review_task_id=review['id'], _activity_task_id=activity['id']))
            # The READY card is not proof that its input is consumed. Reject until the settle pass ends it.
            with self.assertRaises(ServiceError):
                self.resume(caller, identity)
            self.assertEqual(self.service._get('tasks', owner['id']), owner)
            self.service._settle_native_orphans()
            self.service.kanban.call({'op': 'snapshot'})
            self.assertEqual(self.service.kanban.tasks[stale['id']]['status'], 'done')
            self.assertEqual(self.service._get('tasks', stale['id']), stale)
            return owner, caller, identity, workspace
        self.prepare = prepare_root_callback
        self.check_dispatch('done')

    def test_aged_completed_claim_dispatches_after_adapter_restart(self):
        self.check_dispatch('done', aged=True)

    def test_aged_completed_claim_dispatches_for_owner_after_restart(self):
        self.check_dispatch('done', 'owner', aged=True)

    def test_aged_completed_claim_keeps_pr_approval_after_restart(self):
        prepare = self.prepare
        def prepare_pr(*args):
            owner, source, identity, workspace = prepare(*args)
            self.service._put('artifacts', dict(id=str(uuid4()), task_id=owner['id'],
                bot_id=owner['bot_id'], kind='pr', title='Prepared draft', status='open',
                url='https://github.com/example/repo/pull/15'))
            self.service._put('decisions', dict(id=str(uuid4()), task_id=owner['id'],
                bot_id=owner['bot_id'], status='replied', answer='publish_draft', decided_by='human'))
            return owner, source, identity, workspace
        self.prepare = prepare_pr
        self.check_dispatch('done', aged=True)
        approvals = [d for d in self.service._all('decisions') if d.get('answer') == 'publish_draft']
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]['decided_by'], 'human')
        self.assertEqual(approvals[0]['status'], 'replied')

    def test_completed_cross_bot_pr_peer_keeps_original_execution(self):
        prepare = self.prepare
        peers = []
        def prepare_pr(*args):
            owner, source, identity, workspace = prepare(*args)
            peer = self.write('/v1/bots/' + owner['source_bot_id'] + '/messages', {'text': 'Track the same PR.'})
            peer = self.service._get('tasks', peer['id'])
            peer.update(status='completed', _execution_session_id=peer['id'],
                        _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', peer)
            row = self.service._kanban_row(peer, peer, 'ready', 10)
            self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
            self.service.kanban.call({'op': 'snapshot'})
            peer_native = self.service.kanban.tasks[peer['id']]['id']
            self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='PR observation complete')",
                                peer_native)
            peer['_kanban_claim'] = dict(native_id=peer_native, input=row['input'], run_id='peer-run')
            self.service._put('tasks', peer)
            self.service.kanban.call({'op': 'snapshot'})
            self.assertEqual(peer['status'], 'completed')
            self.assertNotEqual(peer['bot_id'], owner['bot_id'])
            for task in (owner, peer):
                item = dict(id=str(uuid4()), task_id=task['id'], bot_id=task['bot_id'],
                    kind='pr', title='Prepared draft', status='open', pr_state='open',
                    created_at=task['created_at'],
                    url='https://github.com/example/repo/pull/15',
                    _review_task_id=task['id'], _activity_task_id=task['id'])
                self.service._put('artifacts', item)
                if task == peer:
                    peers.append(item)
            return owner, source, identity, workspace
        self.prepare = prepare_pr
        self.check_dispatch('done', 'owner', aged=True)
        self.assertEqual(self.service._get('artifacts', peers[0]['id']), peers[0])

    def test_aged_claim_still_requires_requester_or_owner(self):
        owner, source, identity, _ = self.prepare(caller='stranger')
        self.age_binding_and_restart(owner)
        with self.assertRaisesRegex(ServiceError, 'owner or its requesting bot'):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertFalse(self.service._all('artifacts'))

    def test_aged_claim_still_requires_input(self):
        owner, source, identity, _ = self.prepare()
        self.age_binding_and_restart(owner)
        owner['_kanban_claim'].pop('input')
        self.service._put('tasks', owner)
        with self.assertRaisesRegex(ServiceError, 'Choose one existing native outcome'):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertFalse(self.service._all('artifacts'))

    def test_aged_claim_still_requires_matching_durable_binding(self):
        owner, source, identity, _ = self.prepare()
        self.age_binding_and_restart(owner)
        owner['_native_outcome_id'] = 'different'
        self.service._put('tasks', owner)
        with self.assertRaisesRegex(ServiceError, 'mapping does not match'):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertFalse(self.service._all('artifacts'))

    def test_completed_claim_dispatches_one_successor_for_original_requester(self):
        self.check_dispatch('done')

    def test_completed_claim_dispatches_one_successor_for_owner(self):
        self.check_dispatch('done', 'owner')

    def test_interrupted_successor_binding_dispatches_once(self):
        self.check_dispatch('done', interrupt_binding=True)

    def test_ready_claim_dispatches_fresh_input_for_requester(self):
        self.check_dispatch('ready')

    def review_evidence(self, identity):
        return self.native_program('import json; print(json.dumps([dict(r) for r in board.conn.execute('
            '"SELECT * FROM task_events WHERE task_id=? AND kind=\'review_requested\'", (sys.argv[2],))]))', identity)

    def test_review_claim_dispatches_once_for_requester_with_retained_evidence(self):
        self.check_dispatch('review')

    def test_review_claim_dispatches_once_for_owner_with_retained_evidence(self):
        self.check_dispatch('review', 'owner')

    def test_review_claim_rejects_wrong_owner(self):
        owner, source, identity, _ = self.prepare('review', 'stranger')
        with self.assertRaisesRegex(ServiceError, 'owner or its requesting bot'):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_review_claim_preserves_pending_decision(self):
        owner, source, identity, _ = self.prepare('review')
        decision = dict(id=str(uuid4()), task_id=owner['id'], status='pending')
        self.service._put('decisions', decision)
        with self.assertRaisesRegex(ServiceError, 'existing decision'):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('decisions', decision['id']), decision)

    def test_review_claim_rejects_live_reviewer(self):
        owner, source, identity, _ = self.prepare('review')
        self.native_program('assert board.kb.claim_review_task(board.conn, sys.argv[2])', identity)
        self.service.kanban.call({'op': 'snapshot'})
        with self.assertRaises(ServiceError):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'running')
        self.assertFalse(self.provider.started)

    def test_review_input_cannot_dispatch_past_native_dependency(self):
        owner, source, identity, _ = self.prepare('review')
        self.resume(source, identity)
        self.native_program('''
parent = board.kb.create_task(board.conn, title='Unfinished prerequisite')
board.kb.link_tasks(board.conn, parent, sys.argv[2])
''', identity)
        self.service._dispatch_pending()
        self.assertNotIn(owner['id'], self.provider.started)
        self.assertNotIn(owner['id'], self.service.running)
        self.assertIsNone(self.service.kanban.tasks[owner['id']]['current_run_id'])

    def test_ready_claim_dispatches_fresh_input_for_owner(self):
        self.check_dispatch('ready', 'owner')

    def test_ready_claim_rejects_wrong_owner(self):
        owner, source, identity, _ = self.prepare('ready', 'stranger')
        with self.assertRaisesRegex(ServiceError, 'owner or its requesting bot'):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_ready_claim_preserves_pending_decision(self):
        owner, source, identity, _ = self.prepare('ready')
        decision = dict(id=str(uuid4()), task_id=owner['id'], status='pending')
        self.service._put('decisions', decision)
        with self.assertRaisesRegex(ServiceError, 'existing decision'):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('decisions', decision['id']), decision)

    def test_ready_claim_rejects_live_native_run(self):
        owner, source, identity, _ = self.prepare('ready')
        self.native_program('assert board.kb.claim_task(board.conn, sys.argv[2])', identity)
        self.service.kanban.call({'op': 'snapshot'})
        with self.assertRaises(ServiceError):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'running')
        self.assertFalse(self.provider.started)

    def test_ready_accepted_input_cannot_dispatch_past_native_dependency(self):
        owner, source, identity, _ = self.prepare('ready')
        self.resume(source, identity)
        self.native_program('''
parent = board.kb.create_task(board.conn, title='Unfinished prerequisite')
board.kb.link_tasks(board.conn, parent, sys.argv[2])
''', identity)
        self.service._dispatch_pending()
        self.assertNotIn(owner['id'], self.provider.started)
        self.assertNotIn(owner['id'], self.service.running)
        self.assertIsNone(self.service.kanban.tasks[owner['id']]['current_run_id'])

    def test_scheduled_claim_dispatches_fresh_input(self):
        self.check_dispatch('scheduled')

    def test_blocked_claim_dispatches_fresh_input(self):
        self.check_dispatch('blocked')

    def test_stranger_cannot_reconcile_claim(self):
        owner, source, identity, _ = self.prepare(caller='stranger')
        with self.assertRaisesRegex(ServiceError, 'owner or its requesting bot'):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertFalse(self.service._all('artifacts'))

    def test_claim_mapping_mismatches_are_rejected_without_mutation(self):
        owner, source, identity, _ = self.prepare()
        for changes in ({'_native_outcome_id': 'different'},
                        {'_kanban_claim': dict(owner['_kanban_claim'], native_id='different')}):
            with self.subTest(changes=changes):
                changed = dict(owner, **changes)
                self.service._put('tasks', changed)
                with self.assertRaisesRegex(ServiceError, 'mapping does not match'):
                    self.resume(source, identity)
                self.assertEqual(self.service._get('tasks', owner['id']), changed)
                self.assertFalse(self.service._all('artifacts'))

    def test_human_pause_and_pending_decision_are_preserved(self):
        owner, source, identity, _ = self.prepare()
        decision = dict(id=str(uuid4()), task_id=owner['id'], status='pending')
        self.service._put('decisions', decision)
        with self.assertRaisesRegex(ServiceError, 'existing decision'):
            self.resume(source, identity)
        self.assertEqual(self.service._get('decisions', decision['id']), decision)
        decision.update(status='rejected', decided_by='human')
        self.service._put('decisions', decision)
        item = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], kind='work',
                    title=owner['title'], status='waiting', paused_by_decision_id=decision['id'])
        self.service._put('artifacts', item)
        with self.assertRaisesRegex(ServiceError, 'paused'):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('artifacts', item['id']), item)
        self.assertEqual(self.service._get('decisions', decision['id']), decision)


if __name__ == '__main__':
    unittest.main()
