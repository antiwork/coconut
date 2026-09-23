"""Read-only GitHub observations for registered pull request artifacts."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
import json
import hashlib
import os
import re
import subprocess
import shutil
import time
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import quote


_PR_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"([A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]{0,9})/?"
)
_SHA = re.compile(r"[0-9a-f]{40}")
_API = "https://api.github.com"
_PASS = {"success", "neutral", "skipped"}
_FAIL = {"action_required", "cancelled", "failure", "stale", "timed_out"}
_WAIT = {"queued", "in_progress", "waiting", "requested", "pending", "expected"}


@dataclass(frozen=True)
class PRObservation:
    state: str | None
    head_sha: str | None
    title: str | None
    url: str
    detail: str
    checked_at: str
    observed: bool
    pr_state: str | None = None
    draft: bool | None = None
    checks_state: str | None = None
    required_checks_state: str | None = None
    review_checks: str | None = None
    check_failures: str | None = None
    required_checks_observed: bool = False
    base_branch: str | None = None


class PRTrackingError(ValueError):
    """The poll cannot establish a complete, current observation."""


# Profile trackers are short-lived. Reuse only responses authorized by the same token.
_RESPONSE_CACHE = OrderedDict()
_RETRY_AFTER = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_BYTES = 8 * 1024 * 1024


def _remember_response(key, etag, body):
    with _CACHE_LOCK:
        _RESPONSE_CACHE.pop(key, None)
        if etag:
            _RESPONSE_CACHE[key] = (etag, bytes(body))
        while (len(_RESPONSE_CACHE) > 256
               or sum(len(value[1]) for value in _RESPONSE_CACHE.values()) > _CACHE_BYTES):
            _RESPONSE_CACHE.popitem(last=False)


def _rate_limit(scope, headers, status, *, secondary=False):
    if status not in (403, 429) and headers.get('X-RateLimit-Remaining') != '0':
        return
    if (status == 403 and not secondary and not headers.get('Retry-After')
            and headers.get('X-RateLimit-Remaining') != '0'):
        return
    now = time.time()
    try:
        until = max(now + float(headers.get('Retry-After', 0)),
                    float(headers.get('X-RateLimit-Reset', 0))
                    if headers.get('X-RateLimit-Remaining') == '0' else 0)
    except (TypeError, ValueError):
        until = now
    with _CACHE_LOCK:
        _RETRY_AFTER[scope] = max(_RETRY_AFTER.get(scope, 0), until if until > now else now + 60)
        _RETRY_AFTER.move_to_end(scope)
        while len(_RETRY_AFTER) > 256:
            _RETRY_AFTER.popitem(last=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never send the token to a location supplied by a response.
        return None


def parse_pr_url(url: str) -> tuple[str, str, str]:
    """Accept one exact GitHub PR location before any network request."""
    match = _PR_URL.fullmatch(url) if isinstance(url, str) else None
    if not match or match[2] in {".", ".."}:
        raise PRTrackingError("Use an HTTPS GitHub pull request URL.")
    return match[1], match[2], match[3]


def _pr_fields(pr: dict[str, Any]) -> tuple[str, str, bool, bool, str]:
    if not isinstance(pr, dict):
        raise PRTrackingError("GitHub returned incomplete pull request data.")
    sha = pr.get("head", {}).get("sha") if isinstance(pr.get("head"), dict) else None
    state, draft, merged, title = (pr.get(key) for key in ("state", "draft", "merged", "title"))
    if (
        not isinstance(sha, str)
        or not _SHA.fullmatch(sha)
        or state not in {"open", "closed"}
        or type(draft) is not bool
        or type(merged) is not bool
        or not isinstance(title, str)
        or (merged and state != "closed")
    ):
        raise PRTrackingError("GitHub returned incomplete pull request data.")
    return sha, state, draft, merged, title


def _classify(
    draft: bool, checks: list[dict[str, Any]], status: dict[str, Any], sha: str
) -> tuple[str, str]:
    total = status.get("total_count")
    combined = status.get("state")
    if (
        type(total) is not int
        or total < 0
        or status.get("sha") != sha
        or combined not in {"success", "pending", "failure"}
        or not isinstance(status.get("statuses"), list)
        or (total > 0 and not status["statuses"])
        or (total == 0 and status["statuses"])
    ):
        raise PRTrackingError("GitHub returned incomplete commit status data.")
    failed = total > 0 and combined == "failure"
    pending = total > 0 and combined == "pending"
    for check in checks:
        if check.get("head_sha") != sha:
            raise PRTrackingError("A check refers to another commit. Refresh again.")
        phase, conclusion = check.get("status"), check.get("conclusion")
        if phase == "failure":
            failed = True
        elif phase in _WAIT:
            pending = True
        elif phase == "completed" and conclusion in _FAIL:
            failed = True
        elif phase != "completed" or conclusion not in _PASS:
            raise PRTrackingError("GitHub returned an unknown check result.")
    if failed:
        return "failed", "A reported check failed or needs action."
    if draft:
        return "preparing", "The pull request is a draft."
    if pending:
        return "checks_running", "Checks are queued or running."
    if not checks and total == 0:
        return "opened", "No checks reported."
    return "ready_for_review", "Reported checks passed."


class GitHubPRTracker:
    """Poll GitHub with GET requests and return facts for durable storage."""

    def __init__(self, *, token: str | None = None, timeout: float = 5.0):
        if not 0 < timeout <= 10:
            raise ValueError("The GitHub timeout must be between 0 and 10 seconds.")
        self._token = os.environ.get("COCONUT_GITHUB_TOKEN", "") if token is None else token
        self._timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def _get(self, path: str, deadline: float) -> dict[str, Any] | list:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PRTrackingError("GitHub refresh exceeded its time limit.")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "Coconut-Local/1",
        }
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        scope = hashlib.sha256(self._token.encode()).digest()
        key = (scope, path)
        with _CACHE_LOCK:
            if _RETRY_AFTER.get(scope, 0) > time.time():
                raise PRTrackingError("GitHub rate limit reached. Refresh resumes after its reset time.")
            cached = _RESPONSE_CACHE.get(key) if self._token else None
            if cached:
                _RESPONSE_CACHE.move_to_end(key)
        if cached:
            headers['If-None-Match'] = cached[0]
        request = Request(_API + path, headers=headers, method="GET")
        try:
            response = self._opener.open(request, timeout=min(self._timeout, remaining))
        except HTTPError as error:
            try:
                secondary = False
                if error.code == 403:
                    message = error.read(4096).lower()
                    secondary = b'secondary rate limit' in message or b'abuse detection' in message
                _rate_limit(scope, error.headers, error.code, secondary=secondary)
                if error.code == 304 and cached:
                    return json.loads(cached[1])
            finally:
                error.close()
            raise
        with response:
            response_headers = getattr(response, 'headers', {})
            _rate_limit(scope, response_headers, response.status)
            if response.status != 200:
                raise PRTrackingError("GitHub did not return a complete response.")
            body = bytearray()
            while True:
                if time.monotonic() > deadline:
                    raise PRTrackingError("GitHub refresh exceeded its time limit.")
                chunk = response.read1(min(65_536, 2_000_001 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise PRTrackingError("GitHub returned too much data to refresh safely.")
        payload = json.loads(body)
        if not isinstance(payload, (dict, list)):
            raise PRTrackingError("GitHub returned an unexpected response.")
        if self._token:
            _remember_response(key, response_headers.get('ETag'), body)
        return payload

    def _checks(self, prefix: str, sha: str, deadline: float) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        expected: int | None = None
        for page in range(1, 11):
            payload = self._get(
                f"{prefix}/commits/{sha}/check-runs?filter=latest&per_page=100&page={page}",
                deadline,
            )
            if not isinstance(payload, dict):
                raise PRTrackingError("GitHub returned incomplete check data.")
            total, batch = payload.get("total_count"), payload.get("check_runs")
            if (
                type(total) is not int
                or total < 0
                or not isinstance(batch, list)
                or any(not isinstance(check, dict) for check in batch)
                or (expected is not None and expected != total)
            ):
                raise PRTrackingError("GitHub returned incomplete check data.")
            expected = total
            checks.extend(batch)
            if len(checks) == total:
                ids = [check.get("id") for check in checks]
                if any(type(check_id) is not int for check_id in ids) or len(set(ids)) != len(ids):
                    raise PRTrackingError("GitHub checks changed during refresh. Refresh again.")
                return checks
            if len(checks) > total or not batch:
                raise PRTrackingError("GitHub checks changed during refresh. Refresh again.")
        raise PRTrackingError("Too many checks to confirm the pull request state.")

    def _required_checks(self, prefix, pr, checks, status, deadline):
        """Use both branch protection and effective rules; unknown rules stay unknown."""
        base = pr.get('base')
        branch = base.get('ref') if isinstance(base, dict) else None
        if not isinstance(branch, str) or not branch:
            return None, False
        try:
            path = f'{prefix}/branches/{quote(branch, safe="")}'
            branch_data = self._get(path, deadline)
            if not isinstance(branch_data, dict) or type(branch_data.get('protected')) is not bool:
                return None, False
            rules = []
            for page in range(1, 11):
                batch = self._get(f'{prefix}/rules/branches/{quote(branch, safe="")}?per_page=100&page={page}', deadline)
                if not isinstance(batch, list):
                    return None, False
                rules.extend(batch)
                if len(batch) < 100:
                    break
            else:
                return None, False
            required = []
            if branch_data['protected']:
                protection = branch_data['protection']['required_status_checks']
                if not isinstance(protection.get('contexts'), list) or not isinstance(protection.get('checks'), list):
                    return None, False
                required.extend(protection['checks'])
                configured = {item['context'] for item in required}
                required.extend({'context': context} for context in protection['contexts'] if context not in configured)
            for rule in rules:
                if rule['type'] == 'required_status_checks':
                    required.extend(rule['parameters']['required_status_checks'])
            if not required:
                return None, True
            results = []
            for rule in required:
                context = rule['context']
                if not isinstance(context, str) or not context:
                    return None, False
                app = rule.get('integration_id', rule.get('app_id'))
                candidates = [c for c in checks if c.get('name') == context
                              and (app in (None, -1) or c.get('app', {}).get('id') == app)]
                states = [c.get('conclusion') if c.get('status') == 'completed' else 'pending' for c in candidates]
                matching_statuses = [s['state'] for s in status['statuses'] if s.get('context') == context]
                if app in (None, -1):
                    states.extend(matching_statuses)
                elif not states and matching_statuses and all(s in _PASS for s in matching_statuses):
                    # Commit statuses omit the app ID. Resume assessment without claiming that the expected app passed.
                    results.append('unverified')
                    continue
                if any(s in _FAIL or s == 'error' for s in states):
                    results.append('failed')
                elif states and all(s in _PASS for s in states):
                    results.append('passed')
                else:
                    results.append('running')
            return ('failed' if 'failed' in results else 'running' if 'running' in results else
                    'unverified' if 'unverified' in results else 'passed'), True
        except HTTPError as error:
            error.close()
        except (URLError, OSError, HTTPException, PRTrackingError, ValueError, TypeError, KeyError, AttributeError):
            pass
        return None, False

    def _statuses(self, prefix, sha, deadline):
        statuses = []
        first = None
        for page in range(1, 11):
            payload = self._get(f'{prefix}/commits/{sha}/status?per_page=100&page={page}', deadline)
            if (not isinstance(payload, dict) or type(payload.get('total_count')) is not int
                    or payload['total_count'] < 0 or not isinstance(payload.get('statuses'), list)):
                raise PRTrackingError('GitHub returned incomplete commit status data.')
            if first is None:
                first = payload
            elif any(payload.get(k) != first.get(k) for k in ('state', 'sha', 'total_count')):
                raise PRTrackingError('GitHub statuses changed during refresh. Refresh again.')
            statuses.extend(payload['statuses'])
            if len(statuses) == first['total_count']:
                return dict(first, statuses=statuses)
            if not payload['statuses'] or len(statuses) > first['total_count']:
                raise PRTrackingError('GitHub returned incomplete commit status data.')
        raise PRTrackingError('Too many statuses to confirm the pull request state.')

    @classmethod
    def for_profile(cls, profile):
        """Use only this bot's gh login. Never borrow another bot's credentials."""
        from .runtime_environment import profile_environment
        environment = dict(os.environ, **profile_environment(profile))
        for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
            environment.pop(key, None)
        executable = shutil.which('gh')
        token = ''
        if executable:
            try:
                result = subprocess.run([executable, 'auth', 'token', '--hostname', 'github.com'],
                                        env=environment, capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    token = result.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
        return cls(token=token)

    def observe_issue(self, url: str) -> dict | None:
        """Read issue facts without a model call. Failure keeps the last observation."""
        from .decision_sources import github_source
        source = github_source(url)
        if not source or source[0] != 'issues':
            return None
        try:
            issue = self._get('/repos/' + source[1], time.monotonic() + 10)
            if (not isinstance(issue, dict) or issue.get('state') not in ('open', 'closed')
                    or not isinstance(issue.get('updated_at'), str) or not issue['updated_at']
                    or not isinstance(issue.get('title'), str) or type(issue.get('comments')) is not int):
                return None
            return {key: issue.get(key) for key in ('state', 'title', 'updated_at', 'comments', 'state_reason')}
        except HTTPError as error:
            error.close()
        except (URLError, TimeoutError, OSError, HTTPException, PRTrackingError, ValueError, TypeError):
            pass
        return None

    def observe_discussion(self, url: str) -> str | None:
        """Fingerprint complete PR discussions and reviews. Never return their private text."""
        try:
            owner, repo, number = parse_pr_url(url)
            deadline = time.monotonic() + 20
            details = self._get(f'/repos/{owner}/{repo}/pulls/{number}', deadline)
            if not isinstance(details, dict) or 'body' not in details:
                return None
            records = [('details', {key: details.get(key) for key in (
                'body', 'updated_at', 'labels', 'assignees', 'requested_reviewers', 'milestone')})]
            for route in (f'issues/{number}/comments', f'pulls/{number}/comments', f'pulls/{number}/reviews'):
                for page in range(1, 11):
                    values = self._get(f'/repos/{owner}/{repo}/{route}?per_page=100&page={page}', deadline)
                    if not isinstance(values, list) or any(not isinstance(v, dict) or 'id' not in v for v in values):
                        return None
                    records.extend((route, {k: v.get(k) for k in (
                        'id', 'updated_at', 'submitted_at', 'state', 'body', 'commit_id')}) for v in values)
                    if len(values) < 100:
                        break
                else:
                    return None
            return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
        except HTTPError as error:
            error.close()
        except (URLError, TimeoutError, OSError, HTTPException, PRTrackingError, ValueError, TypeError):
            pass
        return None

    def _reviews(self, path: str, deadline: float) -> list:
        reviews = []
        for page in range(1, 11):
            values = self._get(f'{path}/reviews?per_page=100&page={page}', deadline)
            if (not isinstance(values, list) or any(not isinstance(value, dict)
                    or type(value.get('id')) is not int
                    or value.get('state') not in ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED', 'DISMISSED', 'PENDING')
                    for value in values)):
                raise PRTrackingError('GitHub returned incomplete pull request reviews.')
            reviews.extend(('review', value['id'], value['state'], value.get('commit_id'),
                            value.get('submitted_at'), value.get('body')) for value in values)
            if len(values) < 100:
                return reviews
        raise PRTrackingError('GitHub reviews exceed the refresh page limit.')

    def observe(self, url: str) -> PRObservation:
        """Errors carry no replacement state. Keep the last state and mark it stale."""
        checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        sha: str | None = None
        title: str | None = None
        base_branch: str | None = None
        try:
            owner, repo, number = parse_pr_url(url)
            url = f"https://github.com/{owner}/{repo}/pull/{number}"
            prefix = f"/repos/{owner}/{repo}"
            path = f"{prefix}/pulls/{number}"
            deadline = time.monotonic() + 20
            initial = self._get(path, deadline)
            before = _pr_fields(initial)
            sha, state, draft, merged, title = before
            base_branch = (initial.get("base") or {}).get("ref")
            checks_state = None
            required_checks_state = None
            required_checks_observed = False
            review_checks = None
            check_failures = None
            if merged:
                result, detail = "merged", "GitHub confirms this pull request merged."
            elif state == "closed":
                result, detail = "failed", "The pull request closed without a merge."
            else:
                checks = self._checks(prefix, sha, deadline)
                status = self._statuses(prefix, sha, deadline)
                if not isinstance(status, dict):
                    raise PRTrackingError('GitHub returned incomplete commit status data.')
                result, detail = _classify(draft, checks, status, sha)
                required_checks_state, required_checks_observed = self._required_checks(prefix, initial, checks, status, deadline)
                review_events = self._reviews(path, deadline)
                final = self._get(path, deadline)
                after = _pr_fields(final)
                sha, base_branch = after[0], (final.get("base") or {}).get("ref")
                if before[:4] != after[:4]:
                    raise PRTrackingError("The pull request changed during refresh. Refresh again.")
                if (initial.get('base') or {}).get('ref') != (final.get('base') or {}).get('ref'):
                    raise PRTrackingError('The target branch changed during refresh. Refresh again.')
                title = after[4]
                checks_state = {"failed": "failed", "checks_running": "running", "opened": "none", "ready_for_review": "passed"}[_classify(False, checks, status, sha)[0]]
                reviews = sorted(('check', c['id'], c.get('conclusion'), c.get('completed_at')) for c in checks
                                 if 'greptile' in str(c.get('name') or '').lower() and c.get('status') == 'completed')
                reviews.extend(review_events)
                if reviews:
                    review_checks = hashlib.sha256(json.dumps(sorted(reviews)).encode()).hexdigest()
                failures = [('check', str(c['id']), c.get('conclusion'), c.get('completed_at')) for c in checks
                            if c.get('status') == 'failure' or (c.get('status') == 'completed' and c.get('conclusion') in _FAIL)]
                failures += [('status', str(s.get('id')), s.get('context'), s.get('updated_at')) for s in status['statuses']
                             if s.get('state') in ('failure', 'error')]
                if failures:
                    check_failures = hashlib.sha256(json.dumps(sorted(failures)).encode()).hexdigest()
            return PRObservation(result, sha, title, url, detail, checked_at, True, "merged" if merged else state,
                                 draft, checks_state, required_checks_state, review_checks, check_failures, required_checks_observed, base_branch)
        except HTTPError as error:
            detail = f"GitHub refresh failed (HTTP {error.code})."
            error.close()
        except (URLError, TimeoutError, OSError, HTTPException):
            detail = "GitHub is unreachable. The last state may be out of date."
        except PRTrackingError as error:
            detail = str(error)
        except (ValueError, TypeError, KeyError):
            detail = "GitHub returned data that could not be verified."
        return PRObservation(None, sha, title, url, detail, checked_at, False, base_branch=base_branch)
