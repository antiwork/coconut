# Pull request tracking

Coconut can observe existing GitHub pull requests through structured provider artifacts.
The tracker sends only GET requests.
It cannot create repositories, open pull requests, change checks, or merge work.
The service verifies each PR before creating its persistent card.
The service refreshes registered cards every 60 seconds while it runs.
GitHub requests release the service lock so other bots can continue.
An unexpected refresh error does not stop the monitor.

The monitor checks the current head after each push.
It resumes the existing owner when CI fails, checks pass, or a Greptile check finishes.
The owner reads the review findings and assesses merge readiness, including for draft PRs.
An unchanged result does not repeat the assessment on each poll.

The monitor reads required checks from classic branch protection and effective branch rules.
Passing required checks can trigger assessment while optional checks remain pending.
Unknown branch rules do not establish passing required checks.
When a status omits the required app identity, the owner verifies its source before recommending merge.
The displayed aggregate check state still includes optional checks.

Assessment does not grant merge permission or waive a deployment requirement.
The owner creates one merge decision when the current evidence supports it.
Recorded choices for the same head remain effective after a check reruns.
Bots use `coconut_track_pr` and finish their turn while checks run.
They do not need a separate `gh pr checks --watch` process.

The provider supplies `kind: "pr"` and an exact `https://github.com/{owner}/{repo}/pull/{number}` URL.
Assistant prose cannot set the card state.
The URL validator rejects credentials, ports, query strings, fragments, and other hosts.
The HTTP client sends requests only to `api.github.com` and rejects redirects.

For private repositories, set `COCONUT_GITHUB_TOKEN` in the Mac service environment.
Use a token with read access to pull requests, checks, and commit statuses.
The token stays on the Mac.
Public repositories can use unauthenticated requests with lower rate limits.

## State rules

| State | Evidence |
| --- | --- |
| `preparing` | GitHub reports a draft. |
| `opened` | GitHub reports an open PR without reported checks or statuses. |
| `checks_running` | A reported check or status waits or runs. |
| `ready_for_review` | The PR is not a draft, and all reported checks and statuses pass. |
| `merged` | The PR response explicitly reports `merged: true`. |
| `failed` | A reported check fails, or the PR closes without a merge. |

Failures take precedence over draft and running states.
GitHub treats `success`, `neutral`, and `skipped` check conclusions as successful results.
`ready_for_review` describes reported checks for the current head.
It does not establish merge permission, required reviews, missing required checks, or checks on a test merge commit.

The tracker reads checks and combined statuses for the exact head SHA.
It reads the PR again before accepting the result.
A changed head, draft state, or closed state invalidates the observation.
No checks means `opened`; an empty status response does not mean checks are running.

Refresh failures return no replacement state.
The service preserves the last observed state and puts the refresh error in the card preview.
`checked_at` records the last poll, and `observed_at` records the last successful observation.
The service emits a durable `artifact.updated` event after each poll.
The service stops polling after a confirmed merge or bot deletion.
The tracker limits each socket wait to five seconds and each response to 2 MB.
A poll has a 20-second deadline and reads at most ten pages of checks.
An active socket read can finish after that deadline, within its socket timeout.

## Sources

The implementation uses these GitHub contracts:

- [Get a pull request](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request).
- [List check runs for a Git reference](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference).
- [Get the combined status for a reference](https://docs.github.com/en/rest/commits/statuses#get-the-combined-status-for-a-specific-reference).
- [Status checks](https://docs.github.com/en/pull-requests/reference/status-checks).
- [Troubleshoot required checks](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks).

Tests use fixed GitHub response data and a mocked HTTP transport.
They do not establish live access to a private repository.
