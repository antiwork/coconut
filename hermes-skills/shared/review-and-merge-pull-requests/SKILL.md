---
name: review-and-merge-pull-requests
description: Verify a pull request before a merge card or a merge, assess merge readiness when Coconut reports new check or review results, and merge under standing approval.
---

# Review and merge pull requests

Use this skill when you assess a tracked pull request, prepare a merge card, or merge a pull request.

Coconut keeps the merge card contract: one merge card for each PR, a Leave open choice, `risk_level` with one risk sentence, `merge_pr_url`, and a head check before the merge. This skill says how Gianfranco wants the review done.

## Verify before a merge

1. Verify the current PR head, the required checks, independent reviews, unresolved findings, conflicts and required approvals.
2. Read the current-head Greptile findings, not only the successful Greptile check. Resolve the findings within the existing approval, and verify the result.
3. Tell required checks from optional checks. A pending optional deployment must not block the assessment.
4. While required checks run, finish the local verification that the merge decision needs, for example tests that CI does not select. Do this before you wait, when possible.
5. Before a merge card or an authorized merge, complete the required local verification for the current head. Do this also when you first resume after CI passes.
6. Reuse finished verification whose inputs did not change. Do the missing or invalidated checks within the approved scope.
7. When all required verification passes, follow the merge permission rules below. Do not start unrelated local work. Do not repeat verified work that did not change.
8. Use the existing worker result and review for the current head. Do not request another readiness audit unless a specific finding, a changed head or a missing result requires it.

## When Coconut reports new results

- If CI failed, inspect the failure and continue the authorized fix. Do not ask permission for a routine retry or review.
- A draft can be ready for a decision when its owner reports the current head ready. If it needs a merge decision, include marking it ready in the proposed action.
- If an external requirement remains, record its specific reason on the existing task.
- For a merged or closed PR, verify its current GitHub state and the original accepted scope. Record on the existing native task whether the accepted outcome is complete, with evidence.

## Merge permission

- Green CI alone does not grant permission.
- If standing approval covers the current changes and all its conditions pass, mark the draft ready and merge without another card.
- Otherwise, ask once with a merge card. Recommend Merge when the evidence justifies it.
- For a draft, the merge choice marks it ready and merges it. Offer a ready-only choice only when a standing human hold covers this exact PR.
- Green checks alone do not establish low risk. Base `risk_level` on what the change can break.
