import copy
import io
import json
from http.client import IncompleteRead
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from coconut.pr_tracking import GitHubPRTracker, PRTrackingError, _NoRedirect, parse_pr_url


SHA = "a" * 40
URL = "https://github.com/example/project/pull/42"


def pull(**changes):
    result = {"head": {"sha": SHA}, "state": "open", "draft": False, "merged": False, "title": "Fix download"}
    result.update(changes)
    return result


def check(check_id=1, state="completed", conclusion="success", sha=SHA):
    return {"id": check_id, "head_sha": sha, "status": state, "conclusion": conclusion}


def statuses(state="pending", count=0, sha=SHA):
    return {"state": state, "sha": sha, "total_count": count, "statuses": [] if count == 0 else [{"state": state}]}


class PayloadTracker(GitHubPRTracker):
    def __init__(self, payloads):
        super().__init__(token="")
        self.payloads = list(payloads)
        self.paths = []
        self.reviews = []

    def _reviews(self, path, deadline):
        return self.reviews

    def _get(self, path, deadline):
        self.paths.append(path)
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return copy.deepcopy(payload)


def tracker_for(checks=(), status=None, before=None, after=None):
    before = before or pull()
    return PayloadTracker([
        before,
        {"total_count": len(checks), "check_runs": list(checks)},
        status or statuses(),
        after or before,
    ])


class PRStateTests(unittest.TestCase):
    def test_issue_observation_uses_safe_read_only_routes(self):
        value = dict(state='open', title='Follow up', updated_at='2026-09-18T00:00:00Z', comments=2)
        tracker = PayloadTracker([value])
        self.assertEqual(tracker.observe_issue('https://github.com/example/project/issues/12#issuecomment-9'),
                         dict(value, state_reason=None))
        self.assertEqual(tracker.paths, ['/repos/example/project/issues/12'])
        self.assertIsNone(tracker.observe_issue('https://example.invalid/project/issues/12'))
        self.assertIsNone(tracker.observe_issue('https://github.com/example/../issues/12'))
        self.assertIsNone(PayloadTracker([dict(state='open')]).observe_issue('https://github.com/example/project/issues/12'))
        self.assertIsNone(PayloadTracker([URLError('offline')]).observe_issue('https://github.com/example/project/issues/12'))

    def test_pr_discussion_signal_includes_edits_reviews_and_pagination(self):
        first = PayloadTracker([{'body': 'Scope'}, [dict(id=1, body='Before', updated_at='same')], [], [dict(id=2, state='APPROVED')]])
        second = PayloadTracker([{'body': 'Scope'}, [dict(id=1, body='After', updated_at='same')], [], [dict(id=2, state='APPROVED')]])
        self.assertNotEqual(first.observe_discussion(URL), second.observe_discussion(URL))
        before = PayloadTracker([{'body': 'Old scope'}, [], [], []])
        after = PayloadTracker([{'body': 'New scope'}, [], [], []])
        self.assertNotEqual(before.observe_discussion(URL), after.observe_discussion(URL))
        paged = PayloadTracker([{'body': 'Scope'}, [dict(id=i) for i in range(100)], [dict(id=101)], [], []])
        self.assertIsNotNone(paged.observe_discussion(URL))
        self.assertIn('/repos/example/project/issues/42/comments?per_page=100&page=2', paged.paths)
        self.assertIsNone(PayloadTracker([URLError('offline')]).observe_discussion(URL))

    def test_same_head_human_review_changes_readiness_signal(self):
        before = tracker_for([check()])
        after = tracker_for([check()])
        after.reviews = [('review', 7, 'APPROVED', SHA, '2026-09-21T12:00:00Z', 'Looks good')]
        self.assertNotEqual(before.observe(URL).review_checks, after.observe(URL).review_checks)

    def test_review_pagination_and_access_failure(self):
        tracker = PayloadTracker([[dict(id=i, state='COMMENTED', commit_id=SHA) for i in range(100)],
                                  [dict(id=101, state='APPROVED', commit_id=SHA)]])
        reviews = GitHubPRTracker._reviews(tracker, '/repos/example/project/pulls/42', 0)
        self.assertEqual(len(reviews), 101)
        self.assertEqual(reviews[-1][1:4], (101, 'APPROVED', SHA))
        self.assertIn('/repos/example/project/pulls/42/reviews?per_page=100&page=2', tracker.paths)
        denied = tracker_for([check()])
        with patch.object(denied, '_reviews', side_effect=URLError('offline')):
            self.assertFalse(denied.observe(URL).observed)
        with self.assertRaises(PRTrackingError):
            GitHubPRTracker._reviews(PayloadTracker([[dict(id=1, state='unknown')]]), '/pulls/42', 0)

    def test_no_checks_stays_opened(self):
        observation = tracker_for().observe(URL)
        self.assertTrue(observation.observed)
        self.assertEqual(observation.state, "opened")
        self.assertEqual(observation.detail, "No checks reported.")

    def test_success_checks_ignore_empty_combined_pending(self):
        tracker = tracker_for([check()])
        observation = tracker.observe(URL)
        self.assertEqual(observation.state, "ready_for_review")
        self.assertEqual(observation.head_sha, SHA)
        self.assertEqual(observation.url, URL)
        self.assertEqual(observation.title, "Fix download")
        self.assertTrue(all(SHA in path for path in tracker.paths[1:3]))
        self.assertEqual(tracker.paths[0], tracker.paths[-1])

    def test_success_commit_status_without_checks_is_ready(self):
        self.assertEqual(tracker_for(status=statuses("success", 1)).observe(URL).state, "ready_for_review")

    def test_draft_stays_preparing_when_checks_pass(self):
        self.assertEqual(tracker_for([check()], before=pull(draft=True)).observe(URL).state, "preparing")

    def test_each_pending_check_state_is_running(self):
        for state in ("queued", "in_progress", "waiting", "requested", "pending", "expected"):
            with self.subTest(state=state):
                self.assertEqual(tracker_for([check(state=state, conclusion=None)]).observe(URL).state, "checks_running")

    def test_pending_commit_status_overrides_passed_checks(self):
        self.assertEqual(tracker_for([check()], statuses("pending", 1)).observe(URL).state, "checks_running")

    def test_failed_commit_status_overrides_running_check(self):
        result = tracker_for([check(state="queued", conclusion=None)], statuses("failure", 1)).observe(URL)
        self.assertEqual(result.state, "failed")

    def test_each_failed_check_conclusion_is_failed(self):
        for conclusion in ("failure", "timed_out", "cancelled", "action_required", "stale"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(tracker_for([check(conclusion=conclusion)]).observe(URL).state, "failed")

    def test_failed_check_status_is_failed(self):
        self.assertEqual(tracker_for([check(state="failure", conclusion=None)]).observe(URL).state, "failed")

    def test_neutral_and_skipped_match_github_success_rules(self):
        result = tracker_for([check(1, conclusion="neutral"), check(2, conclusion="skipped")]).observe(URL)
        self.assertEqual(result.state, "ready_for_review")

    def test_only_explicit_merged_flag_proves_merge(self):
        tracker = PayloadTracker([pull(state="closed", merged=True)])
        self.assertEqual(tracker.observe(URL).state, "merged")
        self.assertEqual(len(tracker.paths), 1)
        self.assertEqual(PayloadTracker([pull(state="closed")]).observe(URL).state, "failed")

    def test_open_pr_with_merge_commit_sha_does_not_prove_merge(self):
        result = tracker_for(before=pull(merge_commit_sha="b" * 40)).observe(URL)
        self.assertEqual(result.state, "opened")

    def test_new_head_during_refresh_never_reports_ready(self):
        result = tracker_for([check()], after=pull(head={"sha": "b" * 40})).observe(URL)
        self.assertFalse(result.observed)
        self.assertIsNone(result.state)
        self.assertIn("changed during refresh", result.detail)

    def test_draft_or_closed_change_during_refresh_invalidates_observation(self):
        for change in ({"draft": True}, {"state": "closed", "merged": True}):
            with self.subTest(change=change):
                result = tracker_for([check()], after=pull(**change)).observe(URL)
                self.assertFalse(result.observed)
                self.assertIsNone(result.state)

    def test_mismatched_check_head_or_status_head_invalidates_observation(self):
        for tracker in (tracker_for([check(sha="b" * 40)]), tracker_for([check()], statuses("success", 1, "b" * 40))):
            result = tracker.observe(URL)
            self.assertFalse(result.observed)
            self.assertIsNone(result.state)

    def test_unknown_check_conclusion_cannot_report_ready(self):
        result = tracker_for([check(conclusion="future_result")]).observe(URL)
        self.assertFalse(result.observed)
        self.assertIsNone(result.state)

    def test_missing_fields_never_report_ready_or_merged(self):
        for field in ("head", "state", "draft", "merged", "title"):
            payload = pull(state="closed", merged=True)
            payload.pop(field)
            with self.subTest(field=field):
                result = PayloadTracker([payload]).observe(URL)
                self.assertFalse(result.observed)
                self.assertIsNone(result.state)

    def test_incomplete_status_response_never_reports_ready(self):
        for status in ({}, statuses("success", 1) | {"statuses": []}, statuses() | {"total_count": True}):
            with self.subTest(status=status):
                result = tracker_for([check()], status or {"state": "success"}).observe(URL)
                self.assertFalse(result.observed)
                self.assertIsNone(result.state)

    def test_http_and_network_errors_do_not_replace_state_or_leak_details(self):
        errors = [
            HTTPError(URL, 403, "secret body", {}, None),
            URLError("secret host detail"),
            TimeoutError("secret token"),
            ValueError("secret payload"),
            IncompleteRead(b"secret partial response"),
        ]
        for error in errors:
            with self.subTest(error=type(error)):
                result = PayloadTracker([error]).observe(URL)
                self.assertFalse(result.observed)
                self.assertIsNone(result.state)
                self.assertNotIn("secret", result.detail)

    def test_pagination_includes_failure_on_next_page(self):
        first = [check(index) for index in range(1, 101)]
        tracker = PayloadTracker([
            pull(),
            {"total_count": 101, "check_runs": first},
            {"total_count": 101, "check_runs": [check(101, conclusion="failure")]},
            statuses(),
            pull(),
        ])
        self.assertEqual(tracker.observe(URL).state, "failed")
        self.assertIn("page=2", tracker.paths[2])

    def test_changed_count_and_duplicate_pages_cannot_report_ready(self):
        for second in (
            {"total_count": 3, "check_runs": [check(2)]},
            {"total_count": 2, "check_runs": [check(1)]},
        ):
            result = PayloadTracker([pull(), {"total_count": 2, "check_runs": [check(1)]}, second]).observe(URL)
            self.assertFalse(result.observed)
            self.assertIsNone(result.state)

    def test_pagination_is_bounded(self):
        pages = [{"total_count": 11, "check_runs": [check(index)]} for index in range(10)]
        tracker = PayloadTracker([pull(), *pages])
        result = tracker.observe(URL)
        self.assertFalse(result.observed)
        self.assertEqual(len(tracker.paths), 11)


class PRTransportTests(unittest.TestCase):
    def test_allowlist_rejects_unsafe_locations_before_network(self):
        locations = [
            "http://github.com/a/b/pull/1", "https://127.0.0.1/a/b/pull/1",
            "https://github.com.evil.test/a/b/pull/1", "https://github.com@evil.test/a/b/pull/1",
            "https://user@github.com/a/b/pull/1", "https://github.com:443/a/b/pull/1",
            "https://github.com/a/../pull/1", "https://github.com/a/b/pull/0",
            URL + "?target=https://127.0.0.1", URL + "#fragment", URL + "\n",
            "https://github.com/a/%2e%2e/pull/1", "https://github.com/a/b/pull/1/files",
        ]
        for location in locations:
            with self.subTest(location=location):
                tracker = PayloadTracker([])
                result = tracker.observe(location)
                self.assertFalse(result.observed)
                self.assertEqual(tracker.paths, [])

    def test_trailing_slash_is_canonicalized(self):
        self.assertEqual(parse_pr_url(URL + "/"), ("example", "project", "42"))
        self.assertEqual(tracker_for().observe(URL + "/").url, URL)

    def test_redirects_never_forward_authorization(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))

    def test_request_uses_get_fixed_host_version_token_and_timeout(self):
        tracker = GitHubPRTracker(token="example-token", timeout=2)
        response = io.BytesIO(json.dumps(pull(state="closed", merged=True)).encode())
        response.status = 200
        with patch.object(tracker._opener, "open", return_value=response) as open_request:
            self.assertEqual(tracker.observe(URL).state, "merged")
        request = open_request.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/example/project/pulls/42")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer example-token")
        self.assertEqual(request.get_header("X-github-api-version"), "2026-03-10")
        self.assertEqual(open_request.call_args.kwargs["timeout"], 2)

    def test_oversized_or_non_object_response_fails_closed(self):
        for body in (b" " * 2_000_001, b"[]", b"not json"):
            tracker = GitHubPRTracker(token="")
            response = io.BytesIO(body)
            response.status = 200
            with patch.object(tracker._opener, "open", return_value=response):
                result = tracker.observe(URL)
            self.assertFalse(result.observed)
            self.assertIsNone(result.state)

    def test_invalid_timeout_is_rejected(self):
        for timeout in (0, -1, 11, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                GitHubPRTracker(timeout=timeout)


if __name__ == "__main__":
    unittest.main()
