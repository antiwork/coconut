"""Claim-only continuation through the installed kernel and service dispatcher."""
import copy
import json
from pathlib import Path
import subprocess
import threading
import unittest
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
                     'blocked': "block_task(board.conn, sys.argv[2], reason='Waiting for corrected facts')"}[state]
        self.native_program('board.kb.' + operation, identity)
        self.service.kanban.call({'op': 'snapshot'})
        owner.update(status='completed', summary='Original input complete', _kanban_claim=dict(
            native_id=identity, input=row['input'], run_id='completed-run'))
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

    def check_dispatch(self, state, caller='requester', interrupt_binding=False, aged=False):
        owner, source, identity, workspace = self.prepare(state, caller)
        if aged:
            self.age_binding_and_restart(owner)
        decision = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'],
                        status='rejected', title='Publish?', decided_by='human',
                        created_at=owner['created_at'], outcome='Keep the correction local.')
        self.service._put('decisions', decision)
        previous_runs = int(self.native_program('print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", '
                                               '(sys.argv[2],)).fetchone()[0])', identity))
        before = len(self.service._all('tasks'))
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
            self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='Follow-up verified locally')",
                                task['_native_outcome_id'])
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
        # Repeated sync/dispatch while the worker holds its claim cannot spawn twice.
        self.service._dispatch_pending()
        release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.provider.started, [owner['id']])
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
