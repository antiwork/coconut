"""The hook selects local regressions without turning every commit into a full suite."""
import unittest

import pre_commit


class PreCommitTests(unittest.TestCase):
    def test_docs_and_app_changes_leave_service_checks_to_the_agent(self):
        self.assertEqual(pre_commit.checks(['README.md', 'ios/Coconut/Models.swift']), [])

    def test_a_service_change_runs_three_smoke_cases_instead_of_the_whole_service_suite(self):
        commands = pre_commit.checks(['backend/coconut/service.py'])
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].count('-k'), 3)

    def test_a_changed_test_and_its_module_run_once(self):
        commands = pre_commit.checks(['backend/coconut/state_sync.py', 'backend/tests/test_state_sync.py'])
        self.assertEqual(sum('test_state_sync.py' in command for command in commands), 1)

    def test_script_changes_run_their_own_regressions(self):
        self.assertEqual(pre_commit.checks(['scripts/release_gate.py', 'scripts/test_release_gate.py']),
                         [['-m', 'unittest', 'discover', '-s', 'scripts', '-p', 'test_release_gate.py']])
