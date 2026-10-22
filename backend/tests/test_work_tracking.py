import os
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
        with patch.dict(os.environ, {'GH_TOKEN': 'wrong-token', 'GITHUB_TOKEN': 'wrong-token'}), \
             patch('coconut.pr_tracking.shutil.which', return_value='/opt/homebrew/bin/gh'), \
             patch('coconut.pr_tracking.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='bot-token\n')) as run:
            tracker = GitHubPRTracker.for_profile('/tmp/bot-home')
        environment = run.call_args.kwargs['env']
        self.assertEqual(environment['GH_CONFIG_DIR'], '/tmp/bot-home/.config/gh')
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

    def test_manual_refresh_is_coalesced(self):
        self.write('POST', '/v1/work/refresh', {})
        self.assertTrue(self.service.pr_refresh_requested.is_set())
        self.service.pr_refresh_requested.clear()
        self.write('POST', '/v1/work/refresh', {})
        self.assertFalse(self.service.pr_refresh_requested.is_set())


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


class NativePRToolTests(unittest.TestCase):
    def test_native_tool_waits_for_artifact_receipt(self):
        from test_provider import ProviderTests
        fixture = ProviderTests()
        fixture.setUp()
        try:
            provider = fixture.fake_hermes("import json\nresult = json.loads(registry.dispatch('coconut_track_pr', {'url': 'https://github.com/example/project/pull/42'}))\nassert result['id'] == 'saved-pr'\nreturn {'completed': True, 'final_response': 'Tracked.'}\n")
            events = []
            def emit(kind, data):
                if kind == 'phase':
                    return
                events.append((kind, data))
                return {'id': 'saved-pr', 'status': 'checks_running'} if kind == 'artifact' else None
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
                provider.start_task(fixture.task, fixture.bot, [], emit)
            self.assertEqual(events[0], ('artifact', {'kind': 'pr', 'url': URL}))
            self.assertEqual(events[-1][0], 'completed')
        finally:
            fixture.tearDown()
            fixture.doCleanups()
