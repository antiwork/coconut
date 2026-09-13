"""Read-only GitHub observations for registered pull request artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
import json
import os
import re
import subprocess
import shutil
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


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


class PRTrackingError(ValueError):
    """The poll cannot establish a complete, current observation."""


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

    def _get(self, path: str, deadline: float) -> dict[str, Any]:
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
        request = Request(_API + path, headers=headers, method="GET")
        with self._opener.open(request, timeout=min(self._timeout, remaining)) as response:
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
        if not isinstance(payload, dict):
            raise PRTrackingError("GitHub returned an unexpected response.")
        return payload

    def _checks(self, prefix: str, sha: str, deadline: float) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        expected: int | None = None
        for page in range(1, 11):
            payload = self._get(
                f"{prefix}/commits/{sha}/check-runs?filter=latest&per_page=100&page={page}",
                deadline,
            )
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

    def observe(self, url: str) -> PRObservation:
        """Errors carry no replacement state. Keep the last state and mark it stale."""
        checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        sha: str | None = None
        title: str | None = None
        try:
            owner, repo, number = parse_pr_url(url)
            url = f"https://github.com/{owner}/{repo}/pull/{number}"
            prefix = f"/repos/{owner}/{repo}"
            path = f"{prefix}/pulls/{number}"
            deadline = time.monotonic() + 20
            before = _pr_fields(self._get(path, deadline))
            sha, state, draft, merged, title = before
            checks_state = None
            if merged:
                result, detail = "merged", "GitHub confirms this pull request merged."
            elif state == "closed":
                result, detail = "failed", "The pull request closed without a merge."
            else:
                checks = self._checks(prefix, sha, deadline)
                status = self._get(f"{prefix}/commits/{sha}/status?per_page=100", deadline)
                after = _pr_fields(self._get(path, deadline))
                if before[:4] != after[:4]:
                    raise PRTrackingError("The pull request changed during refresh. Refresh again.")
                title = after[4]
                result, detail = _classify(draft, checks, status, sha)
                checks_state = {"failed": "failed", "checks_running": "running", "opened": "none", "ready_for_review": "passed"}[_classify(False, checks, status, sha)[0]]
            return PRObservation(result, sha, title, url, detail, checked_at, True, "merged" if merged else state, draft, checks_state)
        except HTTPError as error:
            detail = f"GitHub refresh failed (HTTP {error.code})."
            error.close()
        except (URLError, TimeoutError, OSError, HTTPException):
            detail = "GitHub is unreachable. The last state may be out of date."
        except PRTrackingError as error:
            detail = str(error)
        except (ValueError, TypeError, KeyError):
            detail = "GitHub returned data that could not be verified."
        return PRObservation(None, sha, title, url, detail, checked_at, False)
