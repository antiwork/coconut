import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from coconut.pr_tracking import GitHubPRTracker
from test_pr_tracking import tracker_for, pull, check, PayloadTracker
from test_service_pr import PRServiceTests, observation, URL


class WorkObservationTests(unittest.TestCase):
    def test_draft_keeps_independent_running_checks(self):
        result = tracker_for([check(state='in_progress', conclusion=None)], before=pull(draft=True)).observe(URL)
        self.assertTrue(result.draft)
        self.assertEqual(result.pr_state, 'open')
        self.assertEqual(result.checks_state, 'running')

    def test_draft_keeps_independent_passed_checks(self):
        self.assertEqual(tracker_for([check()], before=pull(draft=True)).observe(URL).checks_state, 'passed')

    def test_closed_is_distinct_from_failed_checks(self):
        result = PayloadTracker([pull(state='closed')]).observe(URL)
        self.assertEqual(result.pr_state, 'closed')
        self.assertIsNone(result.checks_state)

    def test_bot_auth_does_not_inherit_global_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            # The one-time settings step keeps a bot's own gh login through its .env file.
            (home / '.env').write_text('OPENAI_API_KEY=not-a-folder\nexport GH_CONFIG_DIR="' + str(home / '.config/gh') + '"\n')
            with patch.dict(os.environ, {'GH_TOKEN': 'wrong-token', 'GITHUB_TOKEN': 'wrong-token', 'GH_CONFIG_DIR': '/service/gh'}), \
                 patch('coconut.pr_tracking.shutil.which', return_value='/opt/homebrew/bin/gh'), \
                 patch('coconut.pr_tracking.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='bot-token\n')) as run:
                tracker = GitHubPRTracker.for_profile(home)
                environment = run.call_args.kwargs['env']
                self.assertEqual(environment['GH_CONFIG_DIR'], str(home / '.config/gh'))
                (home / '.env').unlink()
                GitHubPRTracker.for_profile(home)
                # Without its own folder, the bot uses the Mac account's gh login, as its terminal does.
                self.assertNotIn('GH_CONFIG_DIR', run.call_args.kwargs['env'])
        self.assertNotIn('GH_TOKEN', environment)
        self.assertNotIn('GITHUB_TOKEN', environment)
        self.assertEqual(tracker._token, 'bot-token')


class WorkServiceTests(PRServiceTests):
    def test_closed_pr_stops_polling(self):
        self.run_task()
        result = observation('failed')
        from dataclasses import replace
        self.tracker.observations = [replace(result, pr_state='closed')]
        self.service.refresh_pull_requests()
        self.service.refresh_pull_requests()
        self.assertEqual(len(self.tracker.urls), 2)


class FollowPRTests(PRServiceTests):
    def test_explicit_follow_refreshes_a_reopened_pr(self):
        from dataclasses import replace
        self.tracker.observations = [replace(observation('failed'), pr_state='closed'),
                                     replace(observation('checks_running'), pr_state='open', checks_state='running')]
        closed = self.write('POST', '/v1/work/pull-requests', {'bot_id': self.bot['id'], 'url': URL})
        reopened = self.write('POST', '/v1/work/pull-requests', {'bot_id': self.bot['id'], 'url': URL})
        self.assertEqual(closed['id'], reopened['id'])
        self.assertEqual(reopened['pr_state'], 'open')
        self.assertEqual(len(self.tracker.urls), 2)

    def test_follow_existing_pr_without_starting_bot(self):
        result = self.write('POST', '/v1/work/pull-requests', {'bot_id': self.bot['id'], 'url': URL})
        self.assertIsNone(result['task_id'])
        self.assertEqual(result['url'], URL)
        before = self.service.snapshot()
        repeated = self.write('POST', '/v1/work/pull-requests', {'bot_id': self.bot['id'], 'url': URL})
        self.assertEqual(repeated['id'], result['id'])
        self.assertEqual(len(self.tracker.urls), 1)
        self.assertEqual(before['messages'], self.service.snapshot()['messages'])
