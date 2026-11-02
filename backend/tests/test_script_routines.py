import unittest

from coconut.native_routines import unsupported


class ScriptRoutineTests(unittest.TestCase):
    def test_unsupported_execution_stays_rejected(self):
        for job in ({'no_agent': True},
                    {'script': 'cleanup.py', 'no_agent': True, 'monitor_url': 'https://example.com'},
                    {'script': 'cleanup.py', 'no_agent': True, 'deliver': 'telegram'}):
            with self.subTest(job=job):
                self.assertIsNotNone(unsupported(job))

    def test_a_script_that_gates_the_agent_is_a_routine(self):
        # Hermes runs the script first. Its last line {"wakeAgent": false} skips the agent run.
        self.assertIsNone(unsupported({'script': 'intake_gate.py', 'prompt': 'Triage new events.'}))
