"""Keep the manual native fixture faithful to dispatcher lifecycle boundaries."""
import ast
from pathlib import Path
import time
import unittest
from unittest.mock import Mock


class NativeProfileFixtureTests(unittest.TestCase):
    def setUp(self):
        script = Path(__file__).resolve().parents[2] / 'scripts/test_native_profiles.py'
        tree = ast.parse(script.read_text())
        programs = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == 'PROGRAMS' for t in node.targets))
        program = ast.parse(ast.literal_eval(programs)[1])
        driver = next(node for node in ast.walk(program)
                      if isinstance(node, ast.FunctionDef) and node.name == 'drive')
        self.service = Mock()
        namespace = {'service': self.service, 'time': time}
        exec(compile(ast.Module(body=[driver], type_ignores=[]), str(script), 'exec'), namespace)
        self.drive = namespace['drive']

    def test_pending_approval_and_terminal_states_are_never_executed(self):
        for status in ('needs_you', 'waiting', 'failed', 'cancelled', 'completed'):
            with self.subTest(status=status):
                self.service.reset_mock()
                self.service._get.return_value = {'status': status}
                self.drive('task')
                self.service._run_task.assert_not_called()
                self.service._ensure_execution_session.assert_not_called()

    def test_blocked_continuation_is_rejected_before_execution(self):
        self.service._get.return_value = {'status': 'queued'}
        self.service._dispatch_eligible.return_value = False
        with self.assertRaises(AssertionError):
            self.drive('task')
        self.service._ensure_execution_session.assert_not_called()
        self.service._run_task.assert_not_called()

    def test_each_continuation_assigns_ownership_before_running(self):
        task = {'status': 'queued'}
        self.service._get.side_effect = [task, task, {'status': 'needs_you'}]
        self.drive('task')
        calls = [call[0] for call in self.service.method_calls]
        self.assertEqual(calls, ['_get', '_dispatch_eligible', '_ensure_execution_session', '_run_task',
                                 '_get', '_dispatch_eligible', '_ensure_execution_session', '_run_task', '_get'])
        self.service._put.assert_not_called()
        self.service.mutate.assert_not_called()

    def test_unfinished_continuations_fail_at_the_bound(self):
        self.service._get.return_value = {'status': 'queued'}
        with self.assertRaisesRegex(AssertionError, 'Continuation limit exceeded'):
            self.drive('task')
        self.assertEqual(self.service._run_task.call_count, 8)
