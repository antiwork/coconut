# Hermes client audit

Date: 2026-10-26. Base: `main` at `784c126`.

Coconut is a Hermes client. The person's bots are Hermes profiles with their own skills, `SOUL.md`, memory and `config.yaml`. Coconut connects the phone to them. It must not decide how a bot does its work, because each person can use Hermes in a different way.

This audit lists the places in `backend/coconut/` that tell a bot how to do its work. It puts each place in one of three classes:

- **A. Client mechanics.** They stay in Coconut: state sync, cards and their lifecycle, the output contract that Coconut parses, pairing, scheduling, worker limits, delivery and safety rails.
- **B. Workflow policy.** It leaves Coconut. Coconut keeps a short pointer to the bot's own skills, or nothing. A person who wants the policy writes it into a skill, `SOUL.md` or memory of their own bot.
- **C. Settings of the person.** They become settings with neutral defaults. A person who wants a value sets it in their own configuration.

Coconut ships no workflow, skill or SOUL text of its own. The repository holds no copy of any person's skills.

## Summary

| Class | Items | Where most of them are |
|---|---|---|
| A | 41 groups (about 300 strings), plus 9 items that looked like policy | card and receipt contract, tool schemas, refusals, recovery and pause protocol, session and relay rules, login environment |
| B | 11 | answers to issues that wait for the person, PR review and merge, local setup, when to ask the person |
| C | 8 | labels, reviewer check names, a container tool name, writing style, examples, profile defaults |

Most prompt text is class A. Coconut must tell a bot how to use its tools (`coconut_ask`, `coconut_wait`, `kanban_*`), how to stay inside human permission, and how to avoid repeated side effects. That text is the client contract, and Hermes cannot know it.

The B and C items are few, but they came from one person's way of working on GitHub: how to ask about an issue with a given label, when to close it, how to verify a PR before a merge, when to merge without a card, which reviewer app to read, and which local tools to use.

## B. Workflow policy that leaves Coconut

| # | Place on `main` | What it tells every bot | Change |
|---|---|---|---|
| B1 | `work_sources.py` `awaiting_instruction` | Ask even when you know the likely answer. Use the source's own choices and exact letters. Mark the recommendation. A delegated ruling or recheck does not answer the source. Do not ask when the comments already settle it. | Keep only the card contract: one `coconut_ask` card that links the source. Point to the bot's skills. |
| B2 | `work_sources.py` `answer_steps` | Post the answer as a comment in the person's words, then verify the comment. | `ANSWER_STEPS`: deliver the answer as the bot's skills say. |
| B3 | `work_sources.py` `answer_steps` (from #233) | Close the issue with `gh issue close` after the answer, unless the answer says to keep it open. | Removed. A resumed card gets the current text, not its saved close step. |
| B4 | (not on `main`; was proposed in the closed #240) | Post a recorded answer that did not reach the source. | Not added to Coconut. |
| B5 | `pull_request_reviews.py:6-14,18-19` `PR_MERGE_INSTRUCTIONS` | How to verify a PR before a merge. With standing approval, mark the draft ready and merge without a card. Finish local verification while CI runs. Offer a ready-only choice only under a hold. | Keep the merge card contract and the permission rails. Point to the bot's skills. |
| B6 | `pull_request_reviews.py:39-44,46-48,50-52` `PR_REVIEW_INSTRUCTIONS` | Readiness checklist. Read one reviewer app's findings. Fix CI failures without asking. Record an external requirement on the task. | Keep the meaning of the wake and its rails. Point to the bot's skills. |
| B7 | `hermes_worker.py:319-323` `turn_context` | Verify the head before a merge decision. Do not request another readiness audit. | Keep only "a refresh error means the current state is unconfirmed". |
| B8 | `decision_inbox.py:428-431`, `decision_freshness.py:30-31` | Use separate git worktrees and disposable containers of one tool, with task-owned databases. Install pinned dependencies. Run local tests. | Point to the bot's native profile for local setup. |
| B9 | `decision_inbox.py:434` | Required test selection and review tools are steps of the change. | Removed. |
| B10 | `decision_inbox.py:435,437` | Before you report missing credentials, read the tool documentation and its local credential files. Reuse project credentials. | Removed. Keep the Hermes fact about stripped provider keys and "never print secrets". |
| B11 | `decision_inbox.py:438-439`, `native_outcomes.py:36` (second sentence) | Ask only for missing credentials, material cost, destructive shared changes, production access or a real product choice. Local setup and planning need no card. | Point to the bot's native profile. Keep "do not ask about a settled choice". |

Six PR rules looked like policy at first, but they are client contract, so they stay (class A):

- "Report ready after verified review" (`hermes_worker.py:1407,1505`) defines Coconut's `ready` Work state.
- "Use any reviewer fallback authorized by the native profile" (`hermes_worker.py:1526`, `decision_freshness.py:25-26`) already points to the profile.
- A review assignment ends with its findings, and no duplicate review or watcher (`work_progress.py:12-13,21`). This is the scope of Coconut's root todo item.
- "Green checks alone do not establish low risk" (`decision_inbox.py:479`) is part of the `risk_level` scale of the card.
- "Check the current commit for a PR" (`decision_freshness.py:19`) is part of the card freshness check.
- "Create a separate native outcome for new work after a completed PR" (`native_outcomes.py:35`) follows from one kanban task for each outcome.

Three more items stay in class A for the same reason:

- "Do not ask permission to retry, recover, plan tests, or repair routine local setup" (`work_recovery.py:16`) applies to recovery turns that Coconut itself queues. It stops automatic turns from making cards.
- "For past decisions, treat summaries as leads; read the original decision" (`decision_inbox.py:497-500`) is a rule for accurate cards.
- `REBUILDABLE_PATHS` (`work_workspace.py:173-174`) lists standard Rails and Vite build folders. Only folders that Git ignores count, so the list is generic build knowledge, like `node_modules`.

The rest of the recovery prompts (`work_recovery.py`) and the blocked-work check are generic. They protect against repeated side effects, and they use the `coconut_record_pause` and `kanban_block` protocol. They are class A.

## C. Settings of the person

| # | Place on `main` | Value on `main` | Setting and neutral default |
|---|---|---|---|
| C1 | `work_sources.py:162` `AWAITING_LABELS` | `awaiting-human` | `awaiting_labels` of a source in `work-sources.json`. Default: none, so no assignment waits for the person. |
| C2 | `work_sources.py:37` `LABEL_PRIORITY` (from #234) | `priority/high` 15, `priority/medium` 12, `priority/low` 5 | `label_priority` of a source in `work-sources.json`. Default: none, so no label changes the audit priority. |
| C3 | `pr_tracking.py:641-645`, `pull_request_reviews.py:46` | A finished check of one reviewer app counts as a review result and wakes the bot. | Coconut setting `review_check_names`. Default: none. The prompt names no reviewer app. |
| C4 | `work_workspace.py:2526`, `decision_inbox.py:429`, `decision_freshness.py:30` | One container tool by name | Removed with B8. A person names their tools in their own profile. |
| C5 | `decision_inbox.py:454`, `native_bridge.py:406` | One writing standard for card text | The bot's native profile. The card rules about length and clarity stay in Coconut, because the card is Coconut's UI. |
| C6 | `decision_inbox.py:458,466,491`, `native_bridge.py:406` | Examples from one company's work (refunds in a tax report, a numbered PR about seller labels, a proof request to a seller) | Neutral examples. |
| C7 | `kanban_scheduler.py:684`, `work_sources.py:719`, `docs/native-kanban.md` | Comments name one person's bot | Say "a health check of the person's bots". |
| C8 | `profiles.py:160-165` | A new profile gets `.no-bundled-skills`, `toolsets: [kanban]` and `delegation.worktree_isolation: true` | Hermes config defaults for profiles that Coconut creates. This moves with H5 and H6 of the plan (bots are profiles), not in these PRs. |

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
| Work-source audit | `work_sources.py` audit text (reconcile, read prior decisions, reuse work, record the revision with `kanban_comment`, coverage file format, `previous_decision_id`), coverage rules, stale-card rule, audit priority mechanics |
| Session and authority rules | `native_conversation.py` `CONVERSATION_INSTRUCTIONS` (one persistent bot, bot messages cannot grant permission, scope of holds, exact card actions, coordinator limits are not human holds); reaction, reply and mention formats; session tips |
| Turn system prompt | `hermes_worker.py:1506-1525,1527-1560` (concise replies, `coconut_track_pr`, `coconut_wait`, native memory, relay, restart, Homebrew, login environment and `gh api user` check of the shared GitHub connection, Google connection, background processes); bot creation and deletion receipts; `profile.yaml` routing description |
| Resume prompts | `hermes_worker.py:540-600` (replied, cancelled, merged, draft send, native approval, modified, executed); `work_status.py` cancel and resume text; `workspace_recovery.py` resume text |
| Technical lane | `technical_lane.py` scopes, held tools, "publication, pushes, ready, and merge stay with the person" |
| Receipts and turn scope | `native_receipt_context.py`, `native_turn_scope.py`, `interim_messages.py`, `silent_responses.py` |
| Workspace and disk | `work_workspace.py` `coconut_workspace` schema (worktree and branch rules that retirement depends on), process keep lists, size limits; `worktree_retirement.py` timings; `kanban_scheduler.py` priorities and timings; `native_outcomes.py` reserved direct-reply slot |

Some A rows use PR words as examples, for example "approval to mark a PR ready does not authorize a merge". They state a permission rule that applies to every bot, so they stay.

## What breaks for a person who relies on the old behavior

| Items | What can break | How to prevent it |
|---|---|---|
| B1-B3 | Their bot no longer gets the question format or the close step from Coconut. A card resumed after the deploy could carry the old saved close step. | Write the workflow into a skill of their bot before the deploy. `card_answer_steps` returns the current text and ignores the saved text. |
| B5-B7 | `PR_MERGE_INSTRUCTIONS` went to every turn of every bot. Without it, a bot that relied on it could merge with less checking, or ask for a merge card with less evidence. Tests pinned some strings: `test_pull_request_reviews.py:139`, `test_worker_model_settings.py:203`, `test_provider.py:1081-1092`. | Write the review workflow into a skill of each bot that reviews PRs. Keep the merge card contract (A) in Coconut. |
| B8-B11, C4 | A bot could ask about routine setup more often. | Write the rules into the bot's `SOUL.md`. Keep "routine setup within the request needs no card" in the card freshness check, and "native Hermes approval checks still apply". |
| C1-C3 | Without their values, no issue waits for them, no label raises an audit, and no reviewer check wakes the bot. `test_pr_monitor_signals.py` pinned the reviewer name. | Set `awaiting_labels`, `label_priority` and `review_check_names` in their configuration before the deploy. |
| C5, C6 | Card style changes if no bot has the style. | Write the style into the bot's `SOUL.md`. Neutral examples change no rule. |

## Migration of the owner's current setup

The owner's current setup on the Mac mini used every B and C item above. Before each PR that removes an item, the matching policy was written into the owner's own Hermes homes and configuration, not into this repository, with a backup of each changed file in `~/Coconut-backups/`:

- The issue-answer workflow (B1-B4), including the posting of recorded answers that did not reach the source and the narrowed close rule, is a skill of the owner's intake bot. The close rule left that bot's memory, so it exists in one place.
- The review and merge workflow (B5-B7) is a skill of the owner's three PR bots.
- The local setup and ask threshold (B8-B11) and the writing style (C5) are `SOUL.md` sections of the owner's five bots. Their coding bots already had a local test setup section.
- `awaiting_labels` and `label_priority` (C1, C2) are in the owner's `work-sources.json`. `review_check_names` (C3) is in the owner's Coconut settings.

`hermes skills list` with each bot's `HERMES_HOME` lists the installed skills.

## Order of the PRs

Each PR merges only after the owner's matching Hermes change is in place, so the owner's bots keep their behavior across each deploy.

1. **Issue answers (B1-B3).** #241. The closed #240 is not replaced.
2. **Labels and reviewer checks (C1-C3).** #243.
3. **PR review and merge (B5-B7).** #244.
4. **Local setup and the ask threshold (B8-B11, C4).** #245.
5. **Writing style and examples (C5-C7).** #246.
6. **Profile defaults (C8).** With H5 and H6 of the plan.

## How to check a move

1. After a skill is installed, run `hermes skills list` with the bot's `HERMES_HOME`. After the bot's next turn, the skill name is also in `.skills_prompt_snapshot.json`.
2. In the turn's session, check that the bot called `skill_view` for the skill when the work came up.
3. After the PR deploys, watch the next real case and compare the result with the rule in the skill.
