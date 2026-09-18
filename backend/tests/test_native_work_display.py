"""Work displays current native state, without renewing old narrative."""
import unittest
from unittest.mock import patch
import test_native_kanban as native


@unittest.skipUnless(native.SOURCE and native.PYTHON, 'Set Coconut Hermes paths for native integration checks.')
class NativeWorkDisplayTests(unittest.TestCase):
    setUp = native.NativeKanbanTests.setUp
    write = native.NativeKanbanTests.write
    task = native.NativeKanbanTests.task
    native_program = native.NativeKanbanTests.native_program

    def outcome(self):
        self.provider.supports_native_outcomes = True
        origin = self.task('Complete the accepted fix')
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Fix the accepted defect', body='Verify the accepted outcome', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", self.bot['id'], origin['id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        return identity, self.service._get('artifacts', owner['_work_context_id'])

    def project(self, item):
        self.service.kanban.call({'op': 'snapshot'})
        return self.service._work_projection([dict(item)])[0]

    def test_blocked_work_does_not_present_an_old_summary_as_current_activity(self):
        identity, item = self.outcome()
        self.native_program("board.kb.block_task(board.conn, sys.argv[2], reason='Tests failed on old commit abc123', kind='capability')", identity)
        projected = self.project(item)
        self.assertNotIn('abc123', projected['activity'])
        self.assertEqual(projected['activity'], 'Blocked: a required resource is unavailable')
        self.assertEqual(projected['native_status'], 'blocked')

    def test_dependencies_come_from_native_graph_and_disappear_when_completed(self):
        identity, item = self.outcome()
        parent = self.native_program("""
parent=board.kb.create_task(board.conn,title='Prepare isolated tests',body='Verify setup',assignee='default',initial_status='blocked')
board.kb.link_tasks(board.conn,parent,sys.argv[2])
print(parent)
""", identity)
        projected = self.project(item)
        self.assertEqual(projected['activity'], 'Waiting for Prepare isolated tests')
        self.assertEqual(projected['work_dependencies'][0]['id'], parent)
        self.native_program("board.kb.complete_task(board.conn,sys.argv[2],summary='Setup verified')", parent)
        self.assertEqual(self.project(item)['work_dependencies'], [])

    def test_reading_work_does_not_refresh_the_native_observation(self):
        identity, item = self.outcome()
        self.service.kanban.observed_at = '2026-01-01T00:00:00Z'
        projected = self.service._work_projection([dict(item)])[0]
        self.assertEqual(projected['activity_observed_at'], '2026-01-01T00:00:00Z')

    def test_old_phase_expires_while_the_native_worker_remains_running(self):
        identity, item = self.outcome()
        self.native_program("""
board.kb.unblock_task(board.conn,sys.argv[2])
board.kb.claim_task(board.conn,sys.argv[2])
board.dispatch.heartbeat_worker(board.conn,sys.argv[2],note='Running the focused tests')
""", identity)
        self.assertEqual(self.project(item)['activity'], 'Running the focused tests')
        self.native_program("board.conn.execute(\"UPDATE task_events SET created_at=created_at-300 WHERE task_id=? AND kind='heartbeat'\",(sys.argv[2],)); board.conn.commit()", identity)
        projected = self.project(item)
        self.assertEqual(projected['work_state'], 'working')
        self.assertEqual(projected['activity'], 'Waiting for worker output')

    def test_native_change_sends_only_affected_work_and_queued_positions(self):
        first, item = self.outcome()
        second, other = self.outcome()
        self.native_program("board.kb.block_task(board.conn,sys.argv[2],reason='Waiting for setup'); board.kb.block_task(board.conn,sys.argv[3],reason='Waiting for setup')", first, second)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._publish_kanban_changes()
        cursor = self.service._cursor()
        self.native_program("assert board.kb.unblock_task(board.conn,sys.argv[2]); assert board.kb.block_task(board.conn,sys.argv[2],reason='Tool unavailable',kind='capability')", first)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._publish_kanban_changes()
        page = self.service.updates(cursor, collection_deltas=True, native_events=True)
        self.assertEqual({a['id'] for a in page['artifacts']}, {item['id']})
        self.native_program("board.kb.unblock_task(board.conn,sys.argv[2])", second)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._publish_kanban_changes()
        cursor = self.service._cursor()
        self.native_program("assert board.kb.unblock_task(board.conn,sys.argv[2]); assert board.kb.block_task(board.conn,sys.argv[2],reason='Waiting for input',kind='needs_input')", first)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._publish_kanban_changes()
        page = self.service.updates(cursor, collection_deltas=True, native_events=True)
        self.assertEqual({a['id'] for a in page['artifacts']}, {item['id'], other['id']})

    def test_native_restart_preserves_unknown_result_without_a_manual_reply_alert(self):
        identity, item = self.outcome()
        owner = self.service._get('tasks', item['task_id'])
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        self.native_program('assert board.kb.claim_task(board.conn,sys.argv[2])', identity)
        receipt = self.service.native_event(owner['id'], 'native_call',
            dict(name='terminal', arguments=dict(command='verify_saved_result')))
        before = len(self.service._all('messages'))
        with self.service.transaction():
            self.service._recover()
        recovered = self.service._get('tasks', owner['id'])
        self.assertEqual(recovered['status'], 'failed')
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'executing')
        self.assertEqual(len(self.service._all('messages')), before)
        self.native_program("board.dispatch._record_task_failure(board.conn,sys.argv[2],'Worker exited',outcome='worker_failed',release_claim=True,end_run=True)", identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        resumed = self.service._get('tasks', owner['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertIn(receipt['id'], [r['id'] for r in resumed['_self_recovery']['unknown_tools']])
        self.assertEqual(self.provider.started, [])
