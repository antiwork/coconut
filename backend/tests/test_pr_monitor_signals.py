"""PR checks resume their owner without treating optional checks as merge gates."""
from dataclasses import replace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from test_pr_tracking import PayloadTracker, pull, check, statuses, SHA, URL
import test_pull_request_reviews as fixtures
from test_service_pr import observation


class RequiredCheckTests(unittest.TestCase):
    def tracker(self, *, required=None, branch=None, review=None, after=None):
        pr = pull(draft=True, base={'ref': 'main'})
        ci = dict(check(), name='ci/green', app={'id': 9})
        runs = [ci] + ([review] if review else [])
        branch = branch or {'protected': True, 'protection': {'required_status_checks': {'contexts': [], 'checks': []}}}
        rules = [{'type': 'required_status_checks', 'parameters': {
            'required_status_checks': required if required is not None else [{'context': 'ci/green'}]}}]
        return PayloadTracker([pr, {'total_count': len(runs), 'check_runs': runs},
                               statuses('pending', 1) | {'statuses': [{'context': 'buildkite/gumroad', 'state': 'pending'}]},
                               branch, rules, after or pr])

    def test_required_ci_passes_while_optional_buildkite_remains_pending(self):
        result = self.tracker().observe(URL)
        self.assertTrue(result.observed)
        self.assertEqual(result.required_checks_state, 'passed')
        self.assertEqual(result.checks_state, 'running')
        self.assertEqual(result.state, 'preparing')

    def test_missing_required_check_or_wrong_app_does_not_pass(self):
        for required in [[{'context': 'missing'}], [{'context': 'ci/green', 'integration_id': 10}]]:
            with self.subTest(required=required):
                self.assertEqual(self.tracker(required=required).observe(URL).required_checks_state, 'running')

    def test_app_bound_status_requests_source_verification_without_claiming_pass(self):
        tracker = self.tracker(required=[{'context': 'app-status', 'integration_id': 10}])
        tracker.payloads[2] = statuses('pending', 2) | {'statuses': [
            {'context': 'buildkite/gumroad', 'state': 'pending'}, {'context': 'app-status', 'state': 'success'}]}
        self.assertEqual(tracker.observe(URL).required_checks_state, 'unverified')

    def test_classic_branch_protection_and_rules_both_apply(self):
        branch = {'protected': True, 'protection': {'required_status_checks': {
            'contexts': ['classic'], 'checks': [{'context': 'classic', 'app_id': None}]}}}
        self.assertEqual(self.tracker(branch=branch).observe(URL).required_checks_state, 'running')

    def test_rule_access_error_keeps_checks_observable_without_claiming_required_pass(self):
        tracker = self.tracker()
        tracker.payloads[4] = HTTPError(URL, 403, 'Unavailable', {}, None)
        result = tracker.observe(URL)
        self.assertTrue(result.observed)
        self.assertIsNone(result.required_checks_state)
        self.assertEqual(result.checks_state, 'running')

    def test_target_change_invalidates_required_check_result(self):
        result = self.tracker(after=pull(draft=True, base={'ref': 'release'})).observe(URL)
        self.assertFalse(result.observed)
        self.assertIsNone(result.required_checks_state)

    def test_required_checks_on_later_rule_pages_are_included(self):
        tracker = self.tracker()
        tracker.payloads[4:5] = [[{'type': 'pull_request'}] * 100, [{
            'type': 'required_status_checks', 'parameters': {'required_status_checks': [{'context': 'later-page-check'}]}}]]
        result = tracker.observe(URL)
        self.assertEqual(result.required_checks_state, 'running')
        self.assertTrue(any('page=2' in path for path in tracker.paths))

    def test_required_context_on_a_later_status_page_is_included(self):
        tracker = self.tracker(required=[{'context': 'required-status'}])
        first = statuses('pending', 101) | {'statuses': [
            {'context': 'optional-' + str(i), 'state': 'pending'} for i in range(100)]}
        second = statuses('pending', 101) | {'statuses': [{'context': 'required-status', 'state': 'success'}]}
        tracker.payloads[2:3] = [first, second]
        result = tracker.observe(URL)
        self.assertEqual(result.required_checks_state, 'passed')
        self.assertEqual(result.checks_state, 'running')
        self.assertTrue(any('/status?' in path and 'page=2' in path for path in tracker.paths))

    def test_new_failed_check_attempt_has_a_new_signal(self):
        first = self.tracker()
        first.payloads[1]['check_runs'][0].update(conclusion='failure', completed_at='2026-09-17T00:00:00Z')
        second = self.tracker()
        second.payloads[1]['check_runs'][0].update(conclusion='failure', completed_at='2026-09-17T00:02:00Z')
        a, b = first.observe(URL), second.observe(URL)
        self.assertEqual(a.checks_state, 'failed')
        self.assertEqual(b.checks_state, 'failed')
        self.assertTrue(a.check_failures)
        self.assertNotEqual(a.check_failures, b.check_failures)

    def test_greptile_completion_changes_signal_without_waiting_for_optional_ci(self):
        review = dict(check(2), name='Greptile Review', completed_at='2026-09-17T03:00:00Z')
        first = self.tracker(review=review).observe(URL)
        repeat = self.tracker(review=review).observe(URL)
        later = self.tracker(review=dict(review, id=3)).observe(URL)
        self.assertTrue(first.review_checks)
        self.assertEqual(first.review_checks, repeat.review_checks)
        self.assertNotEqual(first.review_checks, later.review_checks)


class PRMonitorTests(unittest.TestCase):
    setUp = fixtures.PRReviewTests.setUp
    tearDown = fixtures.PRReviewTests.tearDown
    write = fixtures.PRReviewTests.write
    green = fixtures.PRReviewTests.green

    def finish_assessment(self):
        # The owner records an external wait. No human merge choice exists yet.
        task = self.service._get('tasks', self.child_id)
        task.update(status='completed')
        self.service._put('tasks', task)

    def test_draft_waiting_on_checks_gets_assessed_when_required_checks_pass(self):
        result = replace(observation(), pr_state='open', draft=True, checks_state='running', required_checks_state='passed')
        self.tracker.observations.append(result)
        self.service.refresh_pull_requests()
        task = self.service._get('tasks', self.child_id)
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['_pr_review']['required_checks_state'], 'passed')
        self.assertEqual(task['_pr_review']['checks_state'], 'running')
        self.finish_assessment()
        self.tracker.observations.append(result)
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')

    def test_repeated_failure_after_green_resumes_again_for_the_same_head(self):
        for checks_state in ('failed', 'passed', 'failed'):
            self.green(draft=True, checks_state=checks_state)
            self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'queued')
            self.finish_assessment()
        self.assertEqual(len(self.service._get('artifacts', self.artifact_id)['_reviewed_states']), 1)

    def test_greptile_rerun_resumes_once_even_when_aggregate_state_is_unchanged(self):
        for signal in ('review-one', 'review-two'):
            self.green(draft=True, checks_state='running', review_checks=signal)
            self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'queued')
            self.finish_assessment()
        self.green(draft=True, checks_state='running', review_checks='review-two')
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')

    def test_failed_retry_resumes_after_an_intervening_running_observation(self):
        self.green(checks_state='failed', check_failures='attempt-one')
        self.finish_assessment()
        self.green(checks_state='running')
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')

        self.green(checks_state='failed', check_failures='attempt-two')
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'queued')
        self.finish_assessment()
        self.green(checks_state='failed', check_failures='attempt-two')
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')

    def test_unverified_required_source_resumes_the_owner_to_verify_it(self):
        self.green(draft=True, checks_state='running', required_checks_state='unverified')
        task = self.service._get('tasks', self.child_id)
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['_pr_review']['required_checks_state'], 'unverified')

    def test_new_review_result_preserves_a_recorded_merge_rejection(self):
        self.green(review_checks='review-one')
        self.finish_assessment()
        self.service._put('decisions', dict(id='declined', bot_id=self.dev['id'], task_id=self.child_id,
            status='rejected', merge_pr_url=URL, _merge_pr_head=SHA, created_at='2026-09-17T00:00:00Z'))
        self.green(review_checks='review-two')
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')

    def test_a_push_requires_results_for_the_new_head(self):
        self.green(draft=True, required_checks_state='passed')
        self.finish_assessment()
        self.green(draft=True, head_sha='b' * 40, checks_state='running')
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')
        self.green(draft=True, head_sha='b' * 40, checks_state='failed')
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'queued')

    def test_monitor_continues_after_an_unexpected_refresh_failure(self):
        calls = []
        def refresh():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError('Temporary refresh failure')
            self.service.stopping.set()
        with patch.object(self.service, 'refresh_pull_requests', side_effect=refresh), \
                patch.object(self.service.pr_refresh_requested, 'wait'), \
                self.assertLogs('coconut.service', level='ERROR'):
            self.service._poll_pull_requests()
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
