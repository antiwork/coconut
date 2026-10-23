"""Run with COCONUT_HERMES_ROOT/PYTHON; never bypass native descendant fences."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from hermes_env import needs_hermes, PYTHON, SOURCE


PRELUDE = '''
import json
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from coconut.kanban_runtime import Board
from hermes_cli.kanban_db_connect import _dispatch_tick_lock
home = Path(sys.argv[1]) / 'kanban'
board = Board(home)
kb, conn = board.kb, board.conn

def task(external, tenant):
    workspace = home / external
    workspace.mkdir()
    identity = kb.create_task(conn, title=external, body='Isolated unlink fixture', assignee='default',
        tenant=tenant, workspace_kind='dir', workspace_path=str(workspace), initial_status='blocked')
    board.bind(identity, external)
    return identity

parent = task('parent', 'care')
other = task('other', 'care')
child = task('child', 'dev')
assert kb.request_review(conn, parent, summary='Appearance remains pending')
assert kb.request_review(conn, other, summary='Other dependency remains pending')
kb.link_tasks(conn, parent, child)
kb.link_tasks(conn, other, child)
assert kb.get_task(conn, child).status == 'todo'
request = dict(op='unlink_dependency', request_id='original-request', parent_id=parent, child_id=child,
    parent_external_id='parent', child_external_id='child', parent_bot_id='care', child_bot_id='dev',
    source_task_id='desk-task', source_bot_id='desk', source_native_id=None, source_turn_id='desk-turn',
    reason='Build resource released. Appearance is unchanged.')
'''


@needs_hermes
class NativeDependencyUnlinkTests(unittest.TestCase):
    def run_native(self, program, *, setup=True):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([PYTHON, '-c', (PRELUDE if setup else '') + textwrap.dedent(program), home],
                env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[1]),
                                                              str(Path(__file__).resolve().parent), SOURCE))),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

    def test_real_native_unlink_preserves_bindings_results_workspace_review_and_other_edges(self):
        self.run_native('''
            saved_parent = asdict(kb.get_task(conn, parent))
            saved_summary = kb.latest_summary(conn, parent)
            saved_events = [tuple(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=?', (parent,))]
            saved_child = asdict(kb.get_task(conn, child))
            bindings = list(conn.execute('SELECT * FROM coconut_bindings'))
            graph = [tuple(r) for r in conn.execute('SELECT * FROM task_links')]
            receipt = board.unlink_dependency(request)
            assert receipt['status'] == 'unlinked'
            assert asdict(kb.get_task(conn, parent)) == saved_parent
            assert kb.latest_summary(conn, parent) == saved_summary
            assert [tuple(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=?', (parent,))] == saved_events
            assert asdict(kb.get_task(conn, child)) == saved_child
            assert list(conn.execute('SELECT * FROM coconut_bindings')) == bindings
            assert [tuple(r) for r in conn.execute('SELECT * FROM task_links')] == [r for r in graph if r[:2] != (parent, child)]
            assert board.unlink_dependency(request) == receipt
            intent = json.loads(conn.execute('SELECT intent FROM coconut_unlinks').fetchone()[0])
            assert intent == request
            assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='unlinked'", (child,)).fetchone()[0] == 1
            second = request | dict(request_id='second-request', parent_id=other, parent_external_id='other')
            board.unlink_dependency(second)
            assert kb.get_task(conn, child).status == 'ready'
            print('native-unlink-preservation-readiness: PASS')
        ''')

    def test_missing_epoch_service_provider_preserves_native_graph_and_input(self):
        self.run_native('''
            import os
            import sys
            from pathlib import Path
            from types import SimpleNamespace
            from unittest.mock import patch
            from coconut.kanban_runtime import Board
            from coconut.kanban_scheduler import KanbanRequestError
            from coconut.service import ServiceError
            from test_dependency_unlink_epoch import MissingEpochUnlinkTests
            from test_provider import ProviderTests
            fixture = MissingEpochUnlinkTests()
            fixture.setUp()
            try:
                child, parent, source = fixture.setup_edge()
                service = fixture.service
                board = Board(Path(sys.argv[1]) / 'kanban')
                kb, conn = board.kb, board.conn
                workspace = Path(sys.argv[1]) / 'retained-workspace'
                workspace.mkdir()
                (workspace / 'owned.txt').write_text('Unpublished original work')
                def native(task):
                    identity = kb.create_task(conn, title=task['id'], body='Retained epoch fixture', assignee='default',
                        tenant=task['bot_id'], workspace_kind='dir', workspace_path=str(workspace), initial_status='blocked')
                    board.bind(identity, task['id'])
                    service._put('tasks', task | {'_native_outcome_id': identity})
                    return identity
                parent_id, child_id = native(parent), native(child)
                related_id = native(fixture.producer)
                assert kb.request_review(conn, related_id, summary='Related helper review')
                late_claim = []
                # Another execution by Dev must not block this exact edge.
                unrelated_workspace = Path(sys.argv[1]) / 'unrelated-workspace'
                unrelated_workspace.mkdir()
                unrelated = kb.create_task(conn, title='Unrelated Dev review', body='Independent work', assignee='default',
                    tenant=child['bot_id'], workspace_kind='dir', workspace_path=str(unrelated_workspace), initial_status='blocked')
                board.bind(unrelated, 'unrelated-review')
                assert kb.request_review(conn, unrelated, summary='Independent review')
                assert kb.claim_review_task(conn, unrelated) is not None
                other = kb.create_task(conn, title='Other dependency', body='Must remain', assignee='default',
                    tenant=parent['bot_id'], workspace_kind='dir', workspace_path=str(workspace), initial_status='blocked')
                assert kb.request_review(conn, parent_id, summary='Appearance remains pending')
                kb.link_tasks(conn, parent_id, child_id)
                kb.link_tasks(conn, other, child_id)
                def call(request):
                    assert not service.lock._is_owned()
                    if request['op'] == 'snapshot':
                        return {'tasks': board.snapshot(request.get('external_ids', []))}
                    if request['op'] == 'unlink_receipt':
                        return {'receipt': board._unlink_dependency(request | {'op': 'unlink_dependency'}, receipt_only=True)}
                    assert related_id in request['guarded_native_ids']
                    if not late_claim:
                        late_claim.append(kb.claim_review_task(conn, related_id))
                        assert late_claim[0] is not None
                    try:
                        return board.unlink_dependency(request)
                    except ValueError as error:
                        # The real scheduler translates confirmed adapter rejections.
                        raise KanbanRequestError(str(error)) from error
                service.kanban = SimpleNamespace(tasks={t['external_id']: t for t in board.snapshot()}, call=call, close=conn.close)
                arguments = fixture.arguments | {'parent_id': parent_id, 'child_id': child_id}
                graph_before_claim = [tuple(r) for r in conn.execute('SELECT * FROM task_links')]
                with fixture.assertRaisesRegex(ServiceError, 'related native work'):
                    service.emit(source['id'], 'dependency_unlink', arguments)
                assert [tuple(r) for r in conn.execute('SELECT * FROM task_links')] == graph_before_claim
                assert kb.complete_task(conn, related_id, summary='Related review completed',
                    expected_run_id=late_claim[0].current_run_id)
                fixture.root = Path(fixture.temp.name) / 'provider'
                provider = ProviderTests.fake_hermes(fixture,
                    "receipt = json.loads(registry.dispatch('coconut_unlink_dependency', " + repr(arguments) + "))\\n"
                    "assert receipt['status'] == 'unlinked', receipt\\n"
                    "return {'completed': True, 'final_response': 'Exact dependency removed.'}\\n")
                before = {table: service._all(table) for table in ('tasks', 'messages', 'artifacts', 'decisions', 'contexts')}
                bindings = [tuple(r) for r in conn.execute('SELECT * FROM coconut_bindings')]
                graph = [tuple(r) for r in conn.execute('SELECT * FROM task_links')]
                receipts = []
                def emit(kind, data):
                    if kind == 'dependency_unlink':
                        result = service.emit(source['id'], kind, data)
                        receipts.append(result)
                        return result
                with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
                    provider.start_task(source, fixture.desk, [], emit)
                assert len(receipts) == 1 and receipts[0]['status'] == 'unlinked', receipts
                assert before == {table: service._all(table) for table in before}
                assert bindings == [tuple(r) for r in conn.execute('SELECT * FROM coconut_bindings')]
                assert [tuple(r) for r in conn.execute('SELECT * FROM task_links')] == [r for r in graph if r[:2] != (parent_id, child_id)]
                assert kb.get_task(conn, parent_id).status == 'review'
                assert kb.get_task(conn, child_id).status == 'todo'
                assert kb.get_task(conn, unrelated).status == 'running'
                assert (workspace / 'owned.txt').read_text() == 'Unpublished original work'
                print('missing-epoch-native-service-provider: PASS')
            finally:
                fixture.tearDown()
                fixture.doCleanups()
        ''', setup=False)

    def test_dependency_created_with_the_child_can_be_removed(self):
        self.run_native('''
            identity = kb.create_task(conn, title='created-child', body='Created parent fixture', assignee='default',
                tenant='dev', workspace_kind='dir', workspace_path=str(home), parents=[parent], initial_status='blocked')
            board.bind(identity, 'created-child')
            assert kb.get_task(conn, identity).status == 'todo'
            created = request | dict(request_id='created-parent-request', child_id=identity,
                                     child_external_id='created-child')
            assert board.unlink_dependency(created)['status'] == 'unlinked'
            assert kb.get_task(conn, identity).status == 'ready'
            assert kb.get_task(conn, parent).status == 'review'
        ''')

    def test_real_adapter_dispatches_registered_unlink_operation(self):
        self.run_native('''
            import os
            from types import SimpleNamespace
            from coconut.kanban_scheduler import KanbanScheduler
            conn.close()
            adapter = KanbanScheduler(SimpleNamespace(hermes_root=Path(SOURCE),
                python=sys.executable), home.parent)
            try:
                assert adapter.call(request | {'op': 'unlink_receipt'}) == {'receipt': None}
                receipt = adapter.call(request)
                assert receipt['status'] == 'unlinked'
                assert adapter.call(request | {'op': 'unlink_receipt'}) == {'receipt': receipt}
                assert adapter.call(request) == receipt
                snapshot = adapter.call({'op': 'snapshot', 'external_ids': ['parent', 'child']})
                cards = {t['external_id']: t for t in snapshot['tasks']}
                assert cards['parent']['status'] == 'review'
                assert cards['child']['status'] == 'todo'
                assert [p['id'] for p in cards['child']['dependencies']] == [other]
            finally:
                adapter.close()
            print('native-adapter-unlink-transport: PASS')
        ''')

    def test_interrupted_native_commit_restart_and_relink_preserve_new_generation(self):
        self.run_native('''
            original = kb.unlink_tasks
            def lost(*args):
                original(*args)
                raise RuntimeError('Interrupted after native commit')
            with patch.object(kb, 'unlink_tasks', side_effect=lost):
                try:
                    board.unlink_dependency(request)
                    raise AssertionError('Expected interruption')
                except RuntimeError as error:
                    assert 'Interrupted' in str(error)
            kb.link_tasks(conn, parent, child)
            conn.close()
            board = Board(home)
            conn = board.conn
            before = [tuple(r) for r in conn.execute('SELECT * FROM task_links')]
            with patch.object(kb, 'unlink_tasks', side_effect=AssertionError('Replay must not mutate')):
                receipt = board.unlink_dependency(request)
                assert board.unlink_dependency(request) == receipt
            assert [tuple(r) for r in conn.execute('SELECT * FROM task_links')] == before
            assert kb.get_task(conn, child).status == 'todo'
            print('native-restart-relink: PASS')
        ''')

    def test_foreign_native_writer_cannot_replace_generation_before_delete(self):
        self.run_native('''
            import sqlite3
            original = kb.unlink_tasks
            competitor = Board(home)
            def racing(connection, parent_id, child_id):
                original(competitor.conn, parent_id, child_id)
                kb.link_tasks(competitor.conn, parent_id, child_id)
                return original(connection, parent_id, child_id)
            with patch.object(kb, 'unlink_tasks', side_effect=racing):
                try:
                    board.unlink_dependency(request)
                    raise AssertionError('The new edge was deleted')
                except sqlite3.IntegrityError as error:
                    assert 'generation changed' in str(error)
            assert conn.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?', (parent, child)).fetchone()
            assert kb.get_task(conn, child).status == 'todo'
            assert kb.get_task(conn, parent).status == 'review'
            assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='unlinked'", (child,)).fetchone()[0] == 1
            competitor.conn.close()
        ''')

    def test_native_lock_claims_binding_tenant_and_exact_edge_guards(self):
        self.run_native('''
            for change in [dict(child_bot_id='foreign'), dict(parent_external_id='other'), dict(parent_id=child)]:
                try:
                    board.unlink_dependency(request | change)
                    raise AssertionError('Invalid identity accepted')
                except ValueError:
                    pass
            with _dispatch_tick_lock(kb.kanban_db_path()) as acquired:
                assert acquired
                try:
                    board.unlink_dependency(request)
                    raise AssertionError('Lock contention accepted')
                except ValueError as error:
                    assert 'dispatch' in str(error)
            # A real independent reviewer claim is guarded even before a PID exists.
            claimed = kb.claim_review_task(conn, parent)
            assert claimed is not None
            try:
                board.unlink_dependency(request)
                raise AssertionError('Active reviewer accepted')
            except ValueError as error:
                assert 'active run' in str(error)
            assert conn.execute('SELECT COUNT(*) FROM coconut_unlinks').fetchone()[0] == 0
            print('native-guards: PASS')
        ''')

    def test_scoped_native_claims_and_incomplete_scope_are_rechecked(self):
        self.run_native('''
            peer = task('unrelated-dev', 'dev')
            assert kb.request_review(conn, peer, summary='Unrelated review')
            assert kb.claim_review_task(conn, peer) is not None
            for scope in ([], [parent], [parent, child, peer], [parent, child, 't_ffffffff']):
                try:
                    board.unlink_dependency(request | dict(guarded_native_ids=scope))
                    raise AssertionError('Invalid or active scope accepted')
                except ValueError as error:
                    assert 'scope' in str(error) or 'related native work' in str(error)
            receipt = board.unlink_dependency(request | dict(guarded_native_ids=[parent, child]))
            assert receipt['status'] == 'unlinked'
            assert kb.get_task(conn, peer).status == 'running'
            # A confirmed mutation is still readable after related work starts.
            assert board.unlink_dependency(request | dict(guarded_native_ids=[parent, child, peer])) == receipt
            print('scoped-native-claim-revalidation: PASS')
        ''')

    def test_existing_native_dispatch_lock_excludes_claims_and_rejects_fail_open(self):
        # No native DB writes: this still executes in descendant read-only sessions.
        self.run_native('''
            import contextlib
            import sys
            from pathlib import Path
            from unittest.mock import patch
            from hermes_cli import kanban_db_connect as locks
            from test_dependency_unlink import DependencyJournalTests
            fixture = DependencyJournalTests()
            fixture.setUp()
            board = fixture.board
            path = Path(sys.argv[1]) / 'fixture.db'
            board.kb.kanban_db_path.return_value = path
            original = board.kb.unlink_tasks.side_effect
            def unlink(*args):
                import threading
                claims = []
                def claim():
                    with locks._dispatch_tick_lock(path) as acquired:
                        claims.append(acquired)
                contender = threading.Thread(target=claim)
                contender.start()
                contender.join(3)
                assert not contender.is_alive()
                assert claims == [False], 'A supported claim entered during unlink'
                return original(*args)
            board.kb.unlink_tasks.side_effect = unlink
            assert board.unlink_dependency(fixture.arguments)['status'] == 'unlinked'
            with locks._dispatch_tick_lock(path) as acquired:
                assert acquired
                try:
                    board.unlink_dependency(fixture.arguments)
                    raise AssertionError('Busy dispatch lock accepted')
                except ValueError as error:
                    assert 'dispatch is in progress' in str(error)
            # Simulate the native helper's documented fail-open branch.
            with patch.object(locks, '_dispatch_tick_lock', return_value=contextlib.nullcontext(True)):
                try:
                    board.unlink_dependency(fixture.arguments)
                    raise AssertionError('Fail-open lock accepted')
                except ValueError as error:
                    assert 'lock is unavailable' in str(error)
            fixture.doCleanups()
            print('native-lock-exclusion-fail-closed: PASS')
        ''', setup=False)


if __name__ == '__main__':
    unittest.main()
