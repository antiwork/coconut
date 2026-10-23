"""Run with COCONUT_HERMES_ROOT/PYTHON. A provider failure must not park a task for good in the Hermes respawn guard."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

from coconut.native_failure import TRANSIENT_ERROR_CODES, TRANSIENT_NATIVE_ERROR, _FAILURES
from hermes_env import needs_hermes, PYTHON, SOURCE


PRELUDE = '''
import sys
from pathlib import Path
from coconut.kanban_runtime import Board
home = Path(sys.argv[1]) / 'kanban'
board = Board(home)
kb, conn = board.kb, board.conn

def claimed(name):
    workspace = home / name
    workspace.mkdir()
    identity = kb.create_task(conn, title=name, body='Fixture', assignee='default', tenant='care',
        workspace_kind='dir', workspace_path=str(workspace))
    board.bind(identity, name)
    conn.execute('INSERT OR IGNORE INTO coconut_outcomes(native_id) VALUES (?)', (identity,))
    task = kb.claim_task(conn, identity)
    assert task is not None and task.status == 'running'
    return identity, task.current_run_id
'''


@needs_hermes
class ProviderFailureRetryTests(unittest.TestCase):
    def run_native(self, program):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([PYTHON, '-c', PRELUDE + textwrap.dedent(program), home],
                env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[1]), SOURCE))),
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_billing_limit_is_booked_as_an_infrastructure_failure_that_the_guard_spaces_and_then_releases(self):
        self.run_native('''
            import os
            identity, run = claimed('billing')
            assert board.finish(identity, run, 'blocked', 'The model provider reported a billing limit.', 'native_billing')
            task = kb.get_task(conn, identity)
            assert task.status == 'ready' and task.consecutive_failures == 0, task
            assert task.last_failure_error == %r, task.last_failure_error
            # Hermes spaces the retries, and it does not park the task for good.
            assert board.dispatch.check_respawn_guard(conn, identity) == 'infrastructure_cooldown'
            os.environ['HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS'] = '1'
            import time
            time.sleep(1.2)
            assert board.dispatch.check_respawn_guard(conn, identity) is None
        ''' % TRANSIENT_NATIVE_ERROR)

    def test_a_guarded_ready_task_gets_one_event_per_reason_and_not_one_per_pass(self):
        self.run_native('''
            identity, run = claimed('parked')
            board.dispatch._record_task_failure(conn, identity, 'The model provider reported a billing limit.',
                outcome='worker_failed', release_claim=True, end_run=True)
            assert board.dispatch.check_respawn_guard(conn, identity) == 'blocker_auth'
            row = dict(id='parked', state='ready', input='one', previous_input=None, recovering=False, native_id=None)
            def events():
                return conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='respawn_guarded'", (identity,)).fetchone()[0]
            spawned = []
            for _ in range(5):
                result = board._dispatch_ready([row], 1, lambda claim, workspace: spawned.append(claim))
                assert result.respawn_guarded == [(identity, 'blocker_auth')], result
            # The dispatcher passes about once a second. The task gets its one event, and no spawn.
            assert events() == 1 and not spawned, (events(), spawned)
            # A new reason is news, and the same reason again is not.
            board._note_respawn_guard(identity, 'active_pr')
            board._note_respawn_guard(identity, 'active_pr')
            assert events() == 2, events()
            # Another event in between makes the same reason news again.
            kb._append_event(conn, identity, 'commented', {'author': 'default'})
            board._note_respawn_guard(identity, 'active_pr')
            assert events() == 3, events()
        ''')

    def test_a_task_that_the_guard_lets_through_still_spawns(self):
        self.run_native('''
            identity, run = claimed('free')
            assert board.finish(identity, run, 'blocked', 'Hermes could not finish this reply.', 'native_turn_failed')
            assert board.dispatch.check_respawn_guard(conn, identity) is None
            row = dict(id='free', state='ready', input='one', previous_input=None, recovering=False, native_id=None)
            claims = []
            board._dispatch_ready([row], 1, lambda claim, workspace: claims.append(claim.id))
            assert claims == [identity], claims
        ''')

    def test_another_failure_still_counts_against_the_breaker(self):
        self.run_native('''
            identity, run = claimed('crash')
            assert board.finish(identity, run, 'blocked', 'Hermes could not finish this reply.', 'native_turn_failed')
            task = kb.get_task(conn, identity)
            assert task.consecutive_failures == 1, task
        ''')


class TransientCodeTests(unittest.TestCase):
    def test_the_transient_codes_are_the_provider_reasons_that_end_on_their_own(self):
        self.assertTrue(TRANSIENT_ERROR_CODES <= {'native_' + reason for reason in _FAILURES})
        for reason in ('billing', 'rate_limit', 'timeout'):
            self.assertIn('native_' + reason, TRANSIENT_ERROR_CODES)
        for reason in ('auth_permanent', 'model_not_found', 'content_policy_blocked', 'context_overflow'):
            self.assertNotIn('native_' + reason, TRANSIENT_ERROR_CODES)


if __name__ == '__main__':
    unittest.main()
