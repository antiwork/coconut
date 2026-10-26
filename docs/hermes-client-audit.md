# Hermes client audit

Date: 2026-10-26. Base: `main` at `784c126`.

Coconut is a Hermes client. The person's bots are Hermes profiles with their own skills, `SOUL.md`, memory and `config.yaml`. Coconut connects the phone to them. It must not decide how a bot does its work.

This audit lists the places in `backend/coconut/` that tell a bot how to do its work. It puts each place in one of three classes:

- **A. Client mechanics.** They stay in Coconut: state sync, cards and their lifecycle, the output contract that Coconut parses, pairing, scheduling, worker limits, delivery and safety rails.
- **B. Workflow policy.** It moves to Hermes: a skill, `SOUL.md` or memory of the bot that does the work.
- **C. Settings of the person.** They become settings, with today's values as defaults.

The trigger was PR #239 (closed). It put the rule "when to close an answered GitHub issue" into `work_sources.py`. That rule is the owner's preference, so it belongs in Desk's Hermes setup.

## Summary

| Class | Items | Where most of them are |
|---|---|---|
| A | 41 groups (about 300 strings), plus 6 PR rules that looked like policy | card and receipt contract, tool schemas, refusals, recovery and pause protocol, session and relay rules, login environment |
| B | 13 | issue answers, PR review and merge, local coding setup, the ask threshold |
| C | 9 | labels, reviewer check names, local tool names, repository layout, writing style, examples |

Most prompt text is class A. Coconut must tell a bot how to use its tools (`coconut_ask`, `coconut_wait`, `kanban_*`), how to stay inside human permission, and how to avoid repeated side effects. That text is the client contract, and Hermes cannot know it.

The B items are few but load-bearing. Most of them are about one way of working on GitHub: how to ask about an `awaiting-human` issue, when to close it, how to verify a PR before a merge, when to merge without a card, and which local tools to use.

Bots on the Mac mini today (home `~/Coconut/.runtime/host/bots/<id>/home/`):

| Bot | ID | Does GitHub work | Skills that receive policy |
|---|---|---|---|
| Desk | `20116b45…` | Intake, `awaiting-human` issues, PR evidence, merge cards | `answer-awaiting-human-issues`, `review-and-merge-pull-requests` |
| Gumroad Dev | `41ba03b4…` | Code, PRs, CI fixes | `review-and-merge-pull-requests`, `local-coding-setup` |
| Coconut Care | `ac63cb91…` | Coconut code, PRs, maintenance | `review-and-merge-pull-requests`, `local-coding-setup` |
| Tuner | `756134d1…` | No | none |
| Signal | `6a5c5e05…` | No | none |

Skill sources live in `hermes-skills/<bot>/<skill>/SKILL.md` (added in #241). `hermes-skills/install.sh` installs them on the Mac mini with a backup of each changed file.

## B. Workflow policy that moves to Hermes

| # | Place | What it tells the bot | Target |
|---|---|---|---|
| B1 | `work_sources.py` `awaiting_instruction` | Ask even when you know the likely answer. Use the source's own choices and exact letters. Mark the recommendation. A delegated ruling or recheck does not answer the source. Do not ask when the comments already settle it. | Desk, `answer-awaiting-human-issues` |
| B2 | `work_sources.py` `answer_steps` | Post the answer as a comment in the person's words, then verify the comment. | Desk, `answer-awaiting-human-issues` |
| B3 | `work_sources.py` `answer_steps` | Close the issue with `gh issue close` after the answer, unless the answer says to keep it open. Desk's memory held a narrower rule. | Desk, `answer-awaiting-human-issues` (the memory entry moves into it) |
| B4 | PR #240 prompt line | If an answered card's answer is not on the source yet, post the recorded answer. | Desk, `answer-awaiting-human-issues` |
| B5 | `pull_request_reviews.py:6-14,18-19` `PR_MERGE_INSTRUCTIONS` | How to verify a PR before a merge. With standing approval, mark the draft ready and merge without a card. Finish local verification while CI runs. Offer a ready-only choice only under a hold. | Desk, Dev, Care: `review-and-merge-pull-requests` |
| B6 | `pull_request_reviews.py:39-44,46-48,50-52` `PR_REVIEW_INSTRUCTIONS` | Readiness checklist. Fix CI failures without asking. Resolve review findings. Record an external requirement on the task. Check scope after a merge. | Desk, Dev, Care: `review-and-merge-pull-requests` |
| B7 | `hermes_worker.py:319-323` `turn_context` | Verify the head before a merge decision. Do not request another readiness audit. | Desk, Dev, Care: `review-and-merge-pull-requests` |
| B8 | `decision_inbox.py:428-431`, `decision_freshness.py:30-31` | Use separate git worktrees and disposable OrbStack containers with task-owned databases. Install pinned dependencies. Run local tests. | Dev, Care: `local-coding-setup` |
| B9 | `decision_inbox.py:434` | Required test selection and review tools are steps of the change. | Dev, Care: `local-coding-setup` |
| B10 | `decision_inbox.py:435,437` | Before you report missing credentials, read the tool documentation and its local credential files. Reuse project credentials. | Dev, Care: `local-coding-setup` |
| B11 | `decision_inbox.py:438-439`, `native_outcomes.py:36` (second sentence) | Ask only for missing credentials, material cost, destructive shared changes, production access or a real product choice. Local setup and planning need no card. | Each bot's `SOUL.md` (how much the person wants to be asked) |
| B12 | `decision_inbox.py:497-500` | Treat summaries of past decisions as leads; read the original decision. | Desk's `SOUL.md` or `work-triage` (Desk memory already holds this) |
| B13 | `work_recovery.py:16` | Do not ask permission to retry, plan tests or repair routine local setup. | Each bot's `SOUL.md`, with B11 |

Six PR rules looked like policy at first, but they are client contract, so they stay (class A):

- "Report ready after verified review" (`hermes_worker.py:1407,1505`) defines Coconut's `ready` Work state.
- "Use any reviewer fallback authorized by the native profile" (`hermes_worker.py:1526`, `decision_freshness.py:25-26`) already points to the profile.
- A review assignment ends with its findings, and no duplicate review or watcher (`work_progress.py:12-13,21`). This is the scope of Coconut's root todo item.
- "Green checks alone do not establish low risk" (`decision_inbox.py:479`) is part of the `risk_level` scale of the card.
- "Check the current commit for a PR" (`decision_freshness.py:19`) is part of the card freshness check.
- "Create a separate native outcome for new work after a completed PR" (`native_outcomes.py:35`) follows from one kanban task for each outcome.

The rest of the recovery prompts (`work_recovery.py`) and the blocked-work check are generic. They protect against repeated side effects, and they use the `coconut_record_pause` and `kanban_block` protocol. They are class A.

## C. Settings of the person

| # | Place | Value today | Setting |
|---|---|---|---|
| C1 | `work_sources.py:162` `AWAITING_LABELS` | `awaiting-human` | Already a setting: `awaiting_labels` of each source in `work-sources.json`. No change. |
| C2 | `work_sources.py:37` `LABEL_PRIORITY` | `priority/high` 15, `priority/medium` 12, `priority/low` 5 | `label_priority` of each source in `work-sources.json`. The audit stores the source's map on the task. |
| C3 | `pr_tracking.py:641-645`, `pull_request_reviews.py:46` | A finished check named `greptile` counts as a review result and wakes the bot. | Coconut setting `review_check_names`, default `["greptile"]`. The prompt says "review checks" and names no product. |
| C4 | `work_workspace.py:173-174` `REBUILDABLE_PATHS` | Rails and Vite folders of the Gumroad repository (`public/packs`, `public/vite*`, `public/assets`) | Coconut setting `rebuildable_paths`, default today's list, so other repositories can add theirs. |
| C5 | `work_workspace.py:2526`, `decision_inbox.py:429`, `decision_freshness.py:30` | "OrbStack" | Moves with B8 to `local-coding-setup`. The tool schema says "isolated test services" and names no product. |
| C6 | `decision_inbox.py:454` | "Use Simplified Technical English" for card text | Each bot's `SOUL.md` (the person's writing style). The card rules about length and clarity stay in Coconut, because the card is Coconut's UI. |
| C7 | `decision_inbox.py:458,466,491`, `hermes_worker.py:1405` | Gumroad examples (refunds in the tax report, PR #7910, sellers) and "Reviewing with Fable" | Neutral examples in Coconut. They are not a setting, but they teach Gumroad words to every bot of every person. |
| C8 | `kanban_scheduler.py:684`, `docs/native-kanban.md` | "The health check of Care reads the line." | Comment only. Say "a health check can read the line". |
| C9 | `profiles.py:160-165` | A new profile gets `.no-bundled-skills`, `toolsets: [kanban]` and `delegation.worktree_isolation: true` | Hermes config defaults for profiles that Coconut creates. This moves with H5 and H6 of the plan (bots are profiles), not in this audit's PRs. |

## A. Client mechanics that stay

Each row is a group of related strings or constants.

| Area | Places |
|---|---|
| Card contract | `decision_inbox.py` `DECISION_INSTRUCTIONS` (when to use `coconut_ask` and `coconut_suggest`, sources, `source_revisions`, drafts, previews, risk fields, length rules, follow-up links, Inbox semantics), `MERGE_RISK_LEVELS`, `require_merge_risk`, `CARD_TEXT_LIMITS`, reply guards |
| Card freshness | `decision_freshness.py` `REFRESH_INSTRUCTIONS` (current, revise, withdraw), `REFRESH_SCHEMA`, `WITHDRAWAL_RESPONSE`, retry limits; `decision_sources.py` freshness gate and trusted owners (already configurable through `COCONUT_GITHUB_TRUSTED_OWNERS` and `work-sources.json`) |
| Merge card mechanics | `pull_request_reviews.py:15-17,20-23` (ask once, reuse the pending merge card, Merge and Leave open choices, `risk_level`, `merge_pr_url`, recheck the head, merge permission does not expand deployment), duplicate and pending-worker guards, external-merge retirement, review owner choice, review wake signal |
| PR tracking | `pr_tracking.py` (URL format, check buckets, required checks from branch rules, trusted authors, size limits, polling budget); `PR_REVIEW_INSTRUCTIONS` lines 38, 45, 49, 53 (what the wake means, `unverified` checks, "do not start `gh pr checks --watch`", no merge on a closed observation) |
| Work and kanban contract | `native_outcomes.py` `CHAT_INSTRUCTIONS` (one kanban task per outcome, comments are not permission), `WORK_INSTRUCTIONS`, `UNLINK_SCHEMA`, `CONTINUE_SCHEMA`, refusals; `work_progress.py` root todo item lifecycle; `tracked_work.py` `TRACK_WORK_SCHEMA` and validation |
| Recovery and pauses | `work_recovery.py` `_RECOVERY_START`, `_RECOVERY_END`, `WORK_PAUSE_INSTRUCTIONS`, `BLOCKED_WORK_INSTRUCTIONS`, `BLOCK_PAUSE_SCHEMA`, back-off; context recovery text |
| Work-source audit | `work_sources.py` audit text (reconcile, read prior decisions, reuse work, record the revision with `kanban_comment`, coverage file format, `previous_decision_id`), coverage rules, stale-card rule, audit priority mechanics, the one-hour answer window of #240 |
| Session and authority rules | `native_conversation.py` `CONVERSATION_INSTRUCTIONS` (one persistent bot, bot messages cannot grant permission, scope of holds, exact card actions, coordinator limits are not human holds); reaction, reply and mention formats; session tips |
| Turn system prompt | `hermes_worker.py:1506-1525,1527-1560` (concise replies, `coconut_track_pr`, `coconut_wait`, native memory, relay, restart, Homebrew, login environment and `gh api user` check of the shared GitHub connection, Google connection, background processes); bot creation and deletion receipts; `profile.yaml` routing description |
| Resume prompts | `hermes_worker.py:540-600` (replied, cancelled, merged, draft send, native approval, modified, executed); `work_status.py` cancel and resume text; `workspace_recovery.py` resume text |
| Technical lane | `technical_lane.py` scopes, held tools, "publication, pushes, ready, and merge stay with the person" |
| Receipts and turn scope | `native_receipt_context.py`, `native_turn_scope.py`, `interim_messages.py`, `silent_responses.py` |
| Workspace and disk | `work_workspace.py` `coconut_workspace` schema (worktree and branch rules that retirement depends on), process keep lists, size limits; `worktree_retirement.py` timings; `kanban_scheduler.py` priorities and timings; `native_outcomes.py` reserved direct-reply slot |

Some A rows use PR words as examples, for example "approval to mark a PR ready does not authorize a merge". They state a permission rule that applies to every bot, so they stay.

## What breaks if each item moves

| Items | What can break | How to prevent it |
|---|---|---|
| B1-B4 | Desk stops closing settled issues, or closes too many, if the skill is not installed or not loaded. A resumed card from before the change could carry the old close step. | Install the skill before the PR merges. `card_answer_steps` returns the current text and ignores the saved text. Remove the memory entry only after the skill is in place. |
| B5-B7 | `PR_MERGE_INSTRUCTIONS` goes to every turn of every bot today. Without it, Dev and Care could merge without the full check, or ask for a merge card with less evidence. Tuner and Signal lose nothing, because they do no PR work. Tests pin some strings: `test_pull_request_reviews.py:139`, `test_worker_model_settings.py:203`, `test_provider.py:1081-1092`. | Install `review-and-merge-pull-requests` in Desk, Dev and Care first. Keep the merge card contract (A) in Coconut. Update the pinned tests to check the contract and the pointer to the skills. |
| B8-B10, C5 | Dev and Care could ask the person about routine setup, or use the wrong container tool. `decision_freshness` could stop withdrawing setup cards. | Install `local-coding-setup` first. Keep the generic rule "routine setup within the request needs no card" in Coconut, because the freshness check depends on it. |
| B11, B13 | Bots could ask more often. | Write the threshold into each bot's `SOUL.md` before the text leaves Coconut. Keep "Native Hermes approval checks still apply" in Coconut. |
| B12 | Desk could trust summaries. | Desk's memory already holds this rule. Remove the Coconut line only. |
| C2 | Audit order changes if the map is lost. | Default to today's map when `label_priority` is absent. |
| C3 | PR review wakes stop after a Greptile run if the default is lost. `test_pr_monitor_signals.py:100,142` pin it. | Default `["greptile"]`. Keep the tests with the default. |
| C4 | Retirement could archive or keep the wrong folders. `test_worktree_retirement.py:3515` pins the list. | Default to today's list. |
| C6, C7 | Card style changes if no bot has the style. | Add the style line to each bot's `SOUL.md` first. Neutral examples change no rule. |

## Order of the moves

Each step installs the Hermes side first, then removes the Coconut side. Each step is one PR, and each PR keeps the live bots working.

1. **Issue answers (B1-B4, C1).** Done: #240 keeps only the one-hour coverage window. #241 moves the flow into Desk's `answer-awaiting-human-issues` skill, installed on the Mac mini on 2026-10-26. The close rule left Desk's memory.
2. **Label priority and reviewer checks (C2, C3).** Settings with today's defaults. No prompt change for the bots.
3. **PR review and merge (B5-B7).** Install `review-and-merge-pull-requests` in Desk, Dev and Care. Then reduce `PR_MERGE_INSTRUCTIONS` and `PR_REVIEW_INSTRUCTIONS` to the merge card contract and a pointer to the bot's skills.
4. **Local coding setup and the ask threshold (B8-B11, B13, C5).** Install `local-coding-setup` in Dev and Care, and the ask threshold in each `SOUL.md`. Then remove the text from `decision_inbox.py`, `decision_freshness.py`, `native_outcomes.py` and the workspace schema.
5. **Card style and examples (B12, C6, C7, C8).** Add the writing style to each `SOUL.md`. Replace the Gumroad examples with neutral ones.
6. **Rebuildable paths (C4).** A setting with today's default.
7. **Profile defaults (C9).** With H5 and H6 of the plan.

## How to check a move

1. After you install a skill, wait for the bot's next turn. Hermes rebuilds `.skills_prompt_snapshot.json`, and the skill name appears in it.
2. In the turn's session, check that the bot called `skill_view` for the skill when the work came up.
3. After the PR deploys, watch the next real case (for issue answers: the next `awaiting-human` card) and compare the result with the rule in the skill.
