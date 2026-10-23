# Hermes integration

Each Coconut bot maps to one native Hermes profile. Coconut's adapter runs outside the upstream source checkout.
Coconut runs on the Hermes that the person installed. It does not install its own copy.
`backend/coconut/hermes_runtime.py` finds that Hermes and checks it. See [The Hermes that Coconut uses](install-and-update.md#the-hermes-that-coconut-uses).

The compatibility CI tests revision `4d04303818b61bf3aa09278af48356315515965b` from stable release `v0.22.4`.
The contract check also passes on `v0.22.5` (`bf0203a`), installed with the official installer.
Its primary interfaces are [AIAgent](https://github.com/NousResearch/hermes-agent/blob/4d04303818b61bf3aa09278af48356315515965b/run_agent.py),
[native profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles/),
[native memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/),
[approvals](https://hermes-agent.nousresearch.com/docs/user-guide/security/), and
[cron jobs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/).

## Worker

Coconut retains one worker process per bot. Each turn reloads the bot's native settings and conversation.
After the turn, `AIAgent.release_clients()` releases model clients and preserves native terminal and browser state.
Background login processes remain available while the person authorizes them on their phone.
Coconut removes each turn's middleware and tools before accepting another turn.
Cancelling a paused task stops only its native background processes. A newer turn can continue in the same runtime.
Cancelling an active turn, deleting a bot, restarting a bot, or shutting down the service stops the corresponding runtime.
Coconut asks Hermes to stop native children before exiting. After a forced exit, Hermes recovers their checkpoints and checks process identities before cleanup.
A Mac or service restart can still interrupt an unfinished login. Saved credentials remain in the bot's native profile.

The worker uses native context files, `SOUL.md`, memory, tool configuration, and skill discovery.
Hermes sends completed commentary through its native `interim_assistant_callback`.
Coconut saves each commentary message at once so the phone shows the bot's next action while work continues.
Commentary does not complete the task or send a push alert. It remains visible after failure, cancellation, or restart.
Scheduled jobs and reaction-only turns publish only their final reply and preserve native silence rules.
Hermes removes hidden reasoning and duplicate commentary before calling the adapter.
The Codex app-server runtime also sends its final answer through this callback.
Coconut confirms that message after the turn succeeds, keeps the same bubble, and sends one final alert.
Coconut buffers draft response text until Hermes completes the turn, then publishes the full final reply once.
The app keeps the bot's thinking animation visible while work continues. Failed turns do not publish unfinished response text.
It resumes native `SessionDB` history across tasks and follows Hermes compression continuations.
Existing Coconut conversations enter native history through a one-time import.
See [continuous conversations](conversation-continuity.md) for restart and retrieval behavior.
The installed Hermes runtime resolves model credentials and effort. Model keys never enter phone API responses.
The default model comes from the active native profile, not an app constant.

New bot profiles use Hermes' `.no-bundled-skills` setting. Existing profiles receive this default once.
Native skill sync supplies only the essential skills that Hermes defines, currently its `hermes-agent` operating manual.
The larger bundled catalog stays absent across native syncs and updates. User-installed and bot-created skills remain available.
Later changes through `hermes skills opt-in` remain authoritative. Coconut keeps no separate list of essential skills.

Coconut registers tool middleware through `PluginContext.register_middleware` and `TOOL_EXECUTION_MIDDLEWARE`.
The middleware persists tool intent and completion through the parent service.
Native approval callbacks create durable Inbox cards. Context keeps the original decision pending.
The adapter uses a runtime hook for inherited approval configuration. It changes no upstream source files.

Coconut adds these tools:

- `coconut_ask` supplies model-generated questions and choices.
  Before asking, the bot checks existing permission and current source discussion.
  New GitHub cards include `source_revisions`, mapping each source URL to its issues API `updated_at`.
  Coconut verifies those revisions before it stores a card, pauses work, or sends a notification.
  A changed or unavailable source returns to the same worker for reassessment. It creates no card.
  A current source does not grant permission. The bot still checks whether human input is necessary.
  Before each task turn, Coconut reads the task's linked GitHub issues and PRs, at most three.
  Links come from the source assignment, the resumed decision, and the other decisions of the task.
  Coconut uses the bot's own `gh` login and reads outside the service lock.
  All reads for one turn share a 6-second budget. After the budget, Coconut skips the remaining sources.
  Coconut keeps a logged-in tracker for two minutes and a tracker without a login for 30 seconds.
  Thus each turn does not start `gh auth token`. Coconut removes expired trackers.
  The turn gets the newest activity since the last known revision as data: at most ten items, each at most 1,500 characters.
  Activity includes issue comments. For a PR, it also includes reviews and inline review comments.
  The issue record decides the kind: an `/issues/N` link can name a PR, and a `/pull/N` link can name an issue. The PR record `updated_at` also counts, because a review can leave the issue record unchanged.
  If Coconut does not show all new activity, the prompt tells the bot to read the rest itself.
  Without a `gh` login, Coconut does not read. The prompt tells the bot to read the source itself.
  Thus the per-turn reads do not use the shared limit for requests without a login.
  Each item includes its `author_association`. Only OWNER, MEMBER, and COLLABORATOR activity can change the scope.
  Coconut gives text only from these authors, and not from bot accounts. For other items, it gives the metadata and a link.
  The same rule applies to the thread title, which is at most 200 characters, and to review comment file paths.
  A repository role counts only in a repository of an operator owner. Other repositories give metadata only.
  Operator owners are the owners of work-source assignments and of the optional `COCONUT_GITHUB_TRUSTED_OWNERS` list. Tracking a PR does not make its owner trusted.
  To add owners, set `COCONUT_GITHUB_TRUSTED_OWNERS` to a comma-separated list.
  If the thread changed and no new item explains the change, the prompt tells the bot to read the thread itself.
  For example, someone edited the body, the title, the labels, or an older comment.
  The last known revision is the latest of these values:
  the revision that the bot proved when Coconut stored the card (`_source_seen`), the assignment `updated_at`, and the revision that Coconut recorded for the task.
  An older card without `_source_seen` uses its creation time.
  Coconut does not use `_source_facts`, because the background refresh changes it while the card waits.
  After the provider receives the turn, Coconut records the observed revision. The next turn shows only newer activity.
  The observed revision is the later of the thread `updated_at` and the newest item that Coconut read.
  Coconut records it on the task and on its assignment, so continuations use the same revision.
  GitHub times have whole seconds. Items at the recorded second count as new, so an item can repeat once.
  The per-turn list reads do not use the shared response cache.
  If the provider does not receive the turn, Coconut records nothing. The next turn shows the same activity again.
  If a read fails, the turn still starts. The prompt tells the bot to read the source itself before it acts.
- `coconut_propose_file` supplies exact file proposals and persistent artifact cards.
- `coconut_create_bot` creates a bot from a verified direct human request.
- `coconut_delete_bot` deletes another bot from a verified direct human request.

Native memory replaces the former custom preference tool.
A one-time import preserves existing preferences and notes in native memory files.
Subsequent Hermes edits remain authoritative.

## Native bot messages

Each native conversation has the canonical `Bot Chat` title and managed Bot Mode metadata.
Hermes injects its own `message_agent` tool and composes its native relay envelopes.
Coconut registers peer contacts through `tools.bot_relay.write_remote_roster`.
Each bot retains its separate `HERMES_HOME` directory.
These standalone profile homes communicate through the native relay transport on the same Mac.

Hermes defines the relay as file transport that a client must deliver.
The Mac adapter claims envelopes and commits a delivery task with its tool receipt in one SQLite transaction.
A local intent journal links the native envelope to that receipt across process crashes.
Recovery revalidates the target and reuses the original envelope ID.
An ambiguous envelope remains unresolved; the adapter does not invent a successful delivery or repeat it blindly.
A pending recipient does not block the sender or independent work.
The adapter writes the recipient result through native `write_reply` and schedules the sender continuation once.

The iPhone receives sender, recipient, and task IDs as structured data.
It renders compact exchange rows and a shared, read-only transcript between the same two bots.
That view spans tasks and creates no agent or Hermes session.
Each bot retains its own work updates in its normal conversation.

The Mac adapter still owns durable dispatch and recovery.
It uses `AIAgent.run_conversation` and native `SessionDB`; it does not run the native TUI gateway as its service.
The gateway probe works, but its event replay is process-local and live deliveries bind to session leases.
Replacing the adapter requires equivalent restart recovery first.
Coconut changes no Hermes source files.

## Routines

Native cron APIs create, edit, pause, resume, delete, claim, and finish jobs in each profile.
Coconut runs supported prompt jobs as conversation tasks while the phone is closed.
Native cron state determines recurrence and repeat limits.
Completion uses the pinned [native outcome and schedule helpers](https://github.com/NousResearch/hermes-agent/blob/4d04303818b61bf3aa09278af48356315515965b/cron/jobs.py).
Coconut records the completion owner in the same native write as the outcome.
This lets a retry confirm completion without advancing the schedule twice.
The compatibility check verifies these interfaces before accepting an update.
Jobs keep their saved model and provider when the bot default changes. Explicit job settings take priority.
Coconut supplies a stable native claim identity because its settings subprocess exits before the job finishes.
Native claim tokens, heartbeats, and completion checks still control ownership.
Script-only jobs (`no_agent=true`) call the native Hermes runner without resolving a model or loading model credentials.
Hermes handles script paths, time limits, cancellation, output redaction, and silent success.
Create these jobs through the bot with the native cron tool. Scripts must be inside the profile’s `scripts` directory.
The app shows the script and supports schedule changes, pause, manual run, and deletion.
Prompt jobs with pre-run scripts, monitors, external delivery targets, or unsupported execution overrides require the Hermes gateway.
The app displays that limitation instead of discarding the unsupported settings.

## Recovery and limits

A pending decision survives a backend restart. Other bots can finish before the user answers it.
The continuation receives completed tool receipts and the recorded answer.
Unknown tool results stop the task for inspection. Coconut never blindly repeats an uncertain native action.

Full access gives native tools the Mac account's authority. Separate profiles are not security sandboxes.
Coconut does not automatically export arbitrary native output files as artifact cards.
The adapter currently supplies text attachments to Hermes; binary inspection is not complete.

## Checks

At each start, the service runs `hermes_contract.verify_contract()` under the Python of the installed Hermes.
It checks the required APIs, tool registration, and native model settings.
The check blocks network access and uses a temporary Hermes home. It never writes into the person's Hermes home.
Without a usable Hermes, the service starts in refuse mode: it imports no Hermes code, runs no bots, and reports the reason in `GET /v1/setup`.
Run `python3 scripts/host.py hermes-status` to find and check the Hermes without starting the service.
Mac mini updates run this check against the new code before they start it.
A Hermes version outside the tested range runs when the check passes. `GET /v1/setup` and the startup log report it as untested. See [Supported Hermes versions](supported-hermes-versions.md).

From the development Mac, run `python3 scripts/test_native_profiles.py` for the native profile and restart milestone.
Run `python3 scripts/test_hermes_live.py` for bot creation, delegation, deletion, and restart.
These checks use real model requests in temporary profiles. They leave existing bot conversations untouched.
Run `python3 scripts/test_native_relay.py` for busy delivery, pending decisions, context, approval, and restart.
Local backend tests use API doubles and do not replace the real Hermes checks.

## MCP connections

Settings → Connections reads `mcp_servers` from the root Hermes configuration.
Changing the active Terminal profile does not change shared connections.
The app supports HTTPS servers with optional Bearer authentication.
Hermes stores tokens in the profile's `.env` and keeps variable references in `config.yaml`.
The app receives connection metadata and tool names, never saved tokens or headers.
Use Hermes on the Mac for other transports or advanced authentication.

Bot workers inherit shared MCP connections between turns. A bot's native entry overrides a shared entry with the same name.
Native Hermes code handles discovery, validation, connection testing, and tool execution.
When configuration changes, the worker refreshes MCP connections without stopping native terminal processes.
Coconut needs Hermes's `mcp` extra. The official installer includes it, and the check refuses a Hermes without it.

## Updates

An update changes Coconut only. It never installs or replaces Hermes.
After it replaces the source files, the update runs the contract check of the new code against the installed Hermes.
If that Hermes worked with the earlier version and fails with the new one, the update restores the earlier source files.
If Hermes was already missing or incompatible, the new version starts in refuse mode.
Update Hermes itself with its own tools, for example `hermes update`, then restart Coconut.
Run Coconut updates outside the Coconut worker because the update stops that worker.
A maintenance setup must check review, CI, and the user's merge approval before requesting installation.
