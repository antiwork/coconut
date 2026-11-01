import unittest

from coconut.native_routines import unsupported


class ScriptRoutineTests(unittest.TestCase):
    def test_unsupported_execution_stays_rejected(self):
        for job in ({'script': 'cleanup.py'}, {'no_agent': True},
                    {'script': 'cleanup.py', 'no_agent': True, 'monitor_url': 'https://example.com'},
                    {'script': 'cleanup.py', 'no_agent': True, 'deliver': 'telegram'}):
            with self.subTest(job=job):
                self.assertIsNotNone(unsupported(job))
