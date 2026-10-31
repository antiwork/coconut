# Architecture

The SwiftUI app consumes a versioned HTTP API through an end-to-end encrypted relay tunnel. It never parses Hermes CLI output.
The Mac service owns coordination and runs separate Hermes worker processes.
A future execution provider can move work to another host without changing bot, task, decision, or artifact IDs.

## Ownership

| Data | Owner |
| --- | --- |
| Bot identity | Stable UUID mapped to one Hermes profile |
| Name and optional label | Native `profile.yaml` |
| Description and instructions | Native `SOUL.md` |
| Chat preferences and memory | Native `memories/USER.md` and `memories/MEMORY.md` |
| Working conversation, compression, and searchable history | Native profile `state.db` |
| Model, effort, tool settings | Native `config.yaml` |
| Global and per-bot access | Native `approvals.mode` |
| Schedules and cron state | Native `cron/jobs.json` |
| Avatar, pinning, notification preference | Coconut presentation data |
| Pairing, transcript replay, Inbox, routing | Coconut SQLite database |
| Tool and dispatch receipts | Coconut coordination data |

Coconut retains small projections for phone display. Native files own behavior.
Inbox threads group decisions. They do not restrict a bot's recall of its own conversation.
Workers resume native history and compression continuations. The phone never displays those internal boundaries.
Model and access defaults resolve from the active native default profile.
Each bot stores its MCP servers, approval settings and API fallback in its own native configuration, as in Hermes. A bot without its own model uses the default model.

## Native integration

Coconut installs no files into the Hermes source checkout.
Coconut ships its `coconut` skill in `skills/` of the install folder. Hermes loads it through `skills.external_dirs`: setup adds that folder to the person's Hermes profiles, which are also the bots, and a profile that Coconut creates gets it at creation. The person's other Hermes surfaces, such as the Telegram gateway, then know how to update, pair, and repair Coconut. See [the coconut skill](install-and-update.md#the-coconut-skill-for-hermes).
The worker loads `AIAgent`, native CLI tool configuration, profile context, memory, and skill discovery.
The worker records a revision of native identity and memory files in the session metadata.
Each file must contain valid UTF-8 in a regular file of at most 1 MiB.
When those files change, the worker clears the saved prompt through `SessionDB.update_system_prompt`.
Hermes rebuilds the prompt from current native files once.
Unchanged turns keep the saved prompt and native session hooks.
The same session retains its full history and tool receipts.
The recorded card choice keeps its exact action scope.
The worker attaches its captured revision to card and tool requests.
The service checks that revision before it records a current assessment.
Changes during a turn leave its cards stale until another check.
A runtime plugin registers middleware through Hermes's plugin API.
The middleware records tool intent and result through the parent service.
Coconut adapts native approvals through the gateway callback. Each profile's own approval settings apply.

Native `off`, `smart`, and `manual` modes retain their Hermes meaning.
Full access is the one-time initial default. Later launches preserve the configured mode.
Switching modes affects subsequent actions. It cannot undo an action already executing.

## Decisions and recovery

The service persists each question or approval before releasing its turn and publishing an event.
Other bots continue. Context requests produce linked answers while preserving the decision.
Pending card checks include the current profile revision.
A change to identity or memory schedules a new assessment of the same card.
This check does not answer the card.
After an answer, the service starts a continuation with the recorded choice and prior completed tool receipts.
A completed tool result can satisfy its matching replay call once. New turns use new receipt generations.

Native tool execution is not a transaction with SQLite.
If a process dies between an effect and its completion receipt, Coconut marks the task failed for inspection.
It does not promise exactly-once execution for arbitrary shell commands or external services.
Native bot deliveries commit their task and receipt together.
A local journal recovers a native envelope when the service loses its response.
The explicit Coconut file executor reconciles output identity and content before retrying.

Routines use native jobs and native execution claims.
A small receipt bridges a native claim to its Coconut task and reuses the same claim after a restart.
An interrupted, unconfirmed claim produces a visible error. It does not dispatch again automatically.
Native claim fences protect routine tool calls against an owner change.
Advanced native jobs that Coconut cannot preserve do not execute through its dispatcher.

## Isolation and routing

Each bot has a separate Hermes profile, session, workspace, and browser directory.

### Bots are Hermes profiles

Each bot is one Hermes profile of the person, keyed by the profile name. `default` is the Hermes home (`$HERMES_HOME`, else `~/.hermes`). Each other profile is `<Hermes home>/profiles/<name>`. The `profiles` table in `coconut.sqlite3` maps the bot UUID to the profile, so bot IDs never change.

- The service scans the profiles at its start and when `<Hermes home>/profiles` changes. A new profile folder becomes a new bot.
- A folder that disappears makes its bot `missing`. The bot keeps its history and returns with a folder of the same name. Coconut does not follow renames: a renamed folder is a missing bot and a new bot.
- A worker runs with `HERMES_HOME` set to the profile. Coconut chats, `hermes -p <name>`, and the gateway share the same skills, memories, config, and cron jobs. A worker belongs to one `HERMES_HOME`, so a changed folder starts a new worker.
- Settings are native. The default settings are those of `<Hermes home>/config.yaml`. Each other profile uses its own config.
- Coconut creates a profile only through the Hermes profile API, once. A failure fails the request. A folder that Hermes left behind becomes a bot at the next scan.
- A settings write to a missing profile fails. Coconut never creates a profile folder itself.
- Coconut keeps its own files of a bot in `<data-dir>/bots/<uuid>`: workspace, browser, temporary files, routine receipts, and terminal scripts.
- Deleting a bot means "Remove from Coconut": the folder stays, and a scan does not add it again. To delete the Hermes profile itself, the person sends `POST /v1/bots/<id>/profile/delete` with `{"confirm": "<profile name>"}`. Coconut uses Hermes's `delete_profile` and never deletes `default`. A bot cannot request this deletion.
`HOME` remains the Mac account home so macOS Keychain works.
`USER` identifies the Mac account so Claude Code selects its saved Keychain login.
Coconut scopes Codex, GitHub CLI, and XDG settings through their native environment variables.
Claude Code shares the Mac account's native login, settings, and session history across bots and managed Hermes Terminal.
Its configuration overrides remain unset so Claude Code and Hermes use the same native credential store.
These paths organize data; they do not restrict a full-access process from reading other paths.
Browser tools must use the intended profile. Shared desktop control needs serialized ownership.

Homebrew packages and trust share one scope across Coconut bots. Workers set `HOMEBREW_XDG_CONFIG_HOME` to `<data-dir>/shared`. Homebrew owns `shared/homebrew/trust.json` and its write lock. Hermes profiles, browser profiles, and supported tool settings remain separate. Keep the inherited `HOME` and tool configuration paths. Existing trust grants need an explicit move to this scope; Coconut does not merge private trust files automatically.
Coconut does not provide an operating system sandbox or universal desktop lock.

Hermes `message_agent` produces native relay envelopes.
The Mac adapter routes these envelopes with stable IDs and durable delivery receipts.
A delivery confirms queuing, not completion. Exchanges limit new requests and propagate cancellation.
Results can return after the request limit is reached.
An assignment waits while its dependencies run. Each reply resumes the original scope in the same Hermes conversation.
The final result returns to the original requester. Intermediate turns do not send completion notifications.
Native profile data is not copied into peer messages automatically.

### The model of a bot

A Hermes profile does not inherit settings from the default profile. Hermes merges a profile's config only over its built-in defaults. Coconut keeps the choice of each bot in `coconut.sqlite3`, in the `model_follows_default` field of the bot: the bot follows the default model, or it has its own model.

- A bot that follows the default gets the model keys of `<Hermes home>/config.yaml` in its own `config.yaml`: `model.default`, `model.provider`, `model.base_url`, the reasoning effort (`agent.reasoning_effort`) and the fallback models (`fallback_providers` and `fallback_model`). A bot with its own model keeps its own fallback. The other settings do not follow.
- A request only records state. The default lives in `<Hermes home>/config.yaml`, and each bot has only `model_follows_default`. `PATCH /v1/bots/<id>` with a model in `model_override` gives the bot its own model and writes it into its profile at once. `model_override: null` makes the bot follow the default again. The bot view shows `model_override: null` for a bot that follows the default.
- One reconcile step (`BotProfiles.reconcile_model_follow`) writes the keys into the profile of each active bot that follows the default. It is idempotent and keeps no state of a request. It runs at each service start after Coconut's Hermes entries, and in a background worker after a request commits, after a scan, and after a restore. A change outside the app, such as `hermes config set` on the default profile, reaches the bots at the next start or the next reconcile.
- The settings process writes each profile with the config writer of Hermes. A failed write stays on the bot as `model_follow_error` and goes into the log. The worker tries again after five minutes, and each reconcile also tries again. A success clears the error.
- Active means: the bot is not deleted or archived, and its profile row is `active`, not `removed` or `missing`. Coconut keeps the folder of a removed bot, and that folder belongs to the person. A restored bot gets the current default at the next reconcile.
- A bot that Coconut creates follows the default. A bot without a choice, from an earlier install or a profile made outside Coconut, gets one in the reconcile. It follows the default when its profile has no model or the model and provider of the default, else it keeps its own model. The first write after this choice keeps a copy of the earlier file in `<data-dir>/backups/model-follow`.
- The `default` profile is the default model, so Coconut never writes into it for this. A model for its bot changes the default and reaches the bots that follow it.

## The Hermes gateway

Coconut moves its bot turns from its own worker processes to the person's Hermes gateway, in phases. Phase 1 made each bot a Hermes profile.

1. Phase 2: plain chat turns run in the gateway, behind a setting.
2. Phase 3: Coconut tools and cards become tools of the `coconut` plugin, and both runtimes share one conversation.
3. Phase 4: routines run in the gateway as Coconut turns, and each profile keeps its model keys the Hermes way. Kanban stays with Coconut for now.
4. Phase 5: the gateway is always on. Then the other turn types move to the gateway, one group at a time, and Coconut deletes the worker code that served them. Group 1 moved attachments and reactions, group 2a card answers, and group 2b the work of a chat, group 2c recovery turns, and group 3 bot exchanges. Group 4 moved GitHub activity, PR reviews and bot changes. Work sessions in the gateway, part 1, is this version.

### Rules for the phases

- Coconut's own entries in the Hermes profiles (the skills folder, the plugin link, `plugins.enabled` and `gateway.platforms.coconut`) are set again at each update and at each service start. A later phase that needs another Coconut entry adds it to that same edit (`skill_dirs.py`, `gateway_plugin.py`). Coconut never changes other settings of the person.
- No manual migration scripts. A data change that the service needs runs inside the service at its start, after the running turns drain, with a backup.

### Phase 2: plain chat turns

- Phase 2 put these turns behind a setting, `chat_runtime`. Phase 5 removed the setting: the gateway is always on.
- Coconut ships the `coconut` gateway platform in `hermes_plugin/coconut`. Setup links it into `<home>/plugins/coconut` of each profile and enables the plugin and its platform there. See [the coconut skill](install-and-update.md#the-coconut-skill-for-hermes).
- The adapter connects to the main listener on loopback. It long-polls `POST /v1/gateway/poll` for the turns of its profile and reports `typing`, `send`, `edit`, `delete` and `done` to `POST /v1/gateway/events`. It sends the token of `<data>/gateway/bridge.json` (mode 0600), which the service writes at each start. The tunnel listener does not serve these routes.
- One chat is one bot: the chat ID is the bot ID. A multiplexing gateway has one adapter for each served profile, and Coconut matches the adapter to the bot by the Hermes home.
- `gateway_bridge.plain_chat_turn` is the routing rule. Phase 3 widens it (see below).
- The adapter takes turns only after the gateway restored its sessions after a start. Before that, the gateway queues each message and ends its processing without a reply.
- A turn that no adapter takes in 30 seconds goes back to the service, which waits for an adapter (phase 5). A taken turn fails when the adapter stops listing it for 90 seconds.
- `TurnEvents` maps the gateway events onto the worker's provider events, so the phone sees the same messages. Each `send` starts a segment, and `edit` replaces its text. A new segment keeps the earlier one as a complete interim message (`interim_seal`). A send that the gateway marks as interim, such as commentary, is an interim message. `done` completes or fails the task.
- The service supervises one `hermes gateway run` process for the person's Hermes home, which Hermes multiplexes across the profiles. The process starts and stops with the service. Its output goes to `<data>/gateway/gateway.log`. After an exit, the service waits 30 seconds before it starts the process again.
- When a gateway that the service did not start already runs for that home, the service starts no second one. It uses that gateway when the gateway loaded the platform. `GET /v1/setup` returns `gateway` with the state, the process, the reason and the connected profiles.
- A cancelled task sends `/stop` to the gateway session of the chat. An action that already runs can finish.

### Phase 3: one conversation, Coconut tools and cards

- One conversation. The worker resumes a bot's chat from a root session and records the newest session as the tip in the `state_meta` table of the profile's state.db (`native_conversation`). Each gateway turn names the root. The plugin's `pre_gateway_dispatch` hook points the gateway session of the chat at the tip with the session store that the hook receives. When the bot has no conversation yet, the gateway session becomes the tip. Both runtimes then write into the same native session and follow its compression lineage. Coconut chose this direction, the gateway joins the native conversation, because the worker already keeps it and the session store has a public switch for it.
- Coconut tools. The plugin registers the `coconut` toolset: `coconut_ask`, `coconut_track_pr` and `coconut_message_bot`. A tool finds its chat from the gateway session context (`gateway.session_context.get_session_env`), and the adapter of the process names the running turn. Since phase 5, group 2a, the card tools have the worker's schemas (see below). The tools call `POST /v1/gateway/tools`, which acts for the task of that turn. A `check_fn` that reads the session context hid the tools also in Coconut chats, so the tools show on each platform of the gateway, behind its tool search, and refuse to act outside a Coconut chat turn. A Coconut worker does not load these tools: it has its own.
- Questions of Hermes. A clarify question (`send_clarify`) and a command approval (`_send_exec_approval_prompt`) report a `question` event. The service shows it as a decision card, and the task stays working while the gateway turn waits. The answer goes to the adapter with its next poll. The adapter gives it to Hermes with `resolve_gateway_clarify` or `resolve_gateway_approval`. A typed reply to a command approval declines the command and gives Hermes the reason. A card that is still open when the turn ends closes as failed.
- Streaming. The plugin's `on_stream_*` hooks receive the text of each model call, also when the gateway streams nothing to platforms. The adapter sends the text in pieces (`delta`), and the phone sees the reply while the model writes. The person's `streaming` setting stays as it is. The first reply send of the gateway takes over from the streamed text.
- Routing. Each turn of a person's message in a bot's own chat goes to the gateway, also with a quote or a bot mention (they go into the message text). These turns stay in the worker: attachments, open cards that the bot answers, reactions, work items and claims, routines, bot exchanges and returns, GitHub source activity, PR reviews, bot creation and deletion confirmations, recovery and withdrawal turns, and each turn that continues an answered card.
- The startup restore of the runner has no public signal. The adapter reads `GatewayRunner._startup_restore_in_progress`, and the Coconut contract check (`hermes_contract.verify_gateway_contract`) requires it, with the other gateway interfaces that the plugin uses. A Hermes that changes them fails the contract check.

### Known gaps after phase 3

- Upstream asks to Hermes: a public signal that the gateway takes inbound messages after its startup restore; a public way for a platform to join a chat with an existing native session (the hook uses the session store's `switch_session`); a per-platform streaming switch that works when the global `streaming.enabled` is off (Coconut uses the stream observer hooks instead).
- A gateway turn did not see the recent Work updates of the bot. Phase 5, group 2b gives them as context, as the worker does.
- Only a few Coconut tools exist in the gateway. Work tracking, waits, and bot creation and deletion stay with the worker turns that need them.
- Tool progress is off, so the phone shows only the phases "thinking" and "writing".
- The adapter refused messages outside a Coconut turn, for example cron deliveries. Phase 4 delivers them (see [Messages outside a Coconut turn](#messages-outside-a-coconut-turn)).
- Gateway notices, such as "No home channel is set", stay in the gateway log. They name gateway commands, and Coconut sends chat text without gateway commands.
- A profile without a connected adapter has no chat runtime, and its chat turns wait (phase 5). This happens when the gateway does not multiplex, or when the person created the profile outside Coconut after the last setup. A gateway that already runs loads the platform after its next restart.
- Steering during a turn waits for the next turn.

### Phase 4: routines in the gateway

- Evidence. Every Hermes gateway ticks the cron store of each profile that it serves, also with one profile only (`gateway/run_bootstrap.py`, `_start_gateway_start_cron_and_housekeeping` and `_cron_tick_profile_homes`). Hermes has no setting that keeps a served profile or a job out of these ticks. A `cron.provider` of a plugin cannot replace the ticker of a multiplexing gateway (`cron/scheduler_provider.py`, `scheduler_for_profile_mode`). A cron run executes in a Hermes cron session, often in a separate worker process, never as an inbound turn of a platform. So the gateway cannot deliver a cron run "as a Coconut turn" through `deliver=coconut`: delivery carries only the final text.
- One firing owner. A gateway that serves a bot's profile fires the bot's routines. Since phase 5, the evidence is the connected coconut adapter of the profile (`gateway_chat.gateway_served_bots`), not the config. Coconut fires the routines of a bot whose profile no gateway serves, and a routine that the person runs now. The native fire claim decides a race at a switch, so a routine never fires twice.
- A cron run is a Coconut turn. The plugin's session hooks see a cron run through `cron.execution_identity.current_cron_execution`. At its start (`on_session_start`) the plugin opens a turn with `POST /v1/gateway/cron`, and the service makes a routine task in the bot's chat. The run reports to that turn: streamed text, commentary (`on_interim_message`), the answer (`post_llm_call`) and the end (`on_session_end`). The coconut tools act for that task. A card of `coconut_ask` ends the run, and its answer continues the run's Hermes session in a gateway turn (phase 5, group 2a).
- Model keys. Each profile reads its provider keys from its own `.env`, as Hermes keeps them. The Hermes entries of each update and start copy the provider keys of the default profile to each bot profile that does not have them (#284).

### Messages outside a Coconut turn

- Evidence (Hermes 0.22.4 and 0.22.5). Hermes runs its own inputs in the gateway session of a chat: the completion of a background process (`gateway/run_notifications.py`, `_inject_watch_notification`), the result of a delegated task, a Kanban wake (`gateway/wake.py`, `deliver_wake`) and a `/loop` tick. With a session authority, these go into the session's queue (`gateway/session_automation.py`), and the authority runs them through the runner, not through the adapter (`gateway/session_ingress.py`, `execute_admission`). The adapter sees no `on_processing_start`. At the end, `deliver_response` sends the reply with `send_final_ledgered` and `_send_with_retry`, and after a failed send Hermes tries a plain-text copy. Kanban notices (`gateway/kanban_watchers_notifier.py`), process watch notices, stall notices and cron deliveries call `adapter.send` directly.
- Until this change, the adapter refused each such send. On 2026-10-28 the Mac mini lost two replies this way. Both were final replies of Hermes turns in a bot's chat session. One of these turns read Kanban task `t_42d02397`. The other turn ran while Hermes delivered a message into a profile's Bot Chat.
- Now the adapter sends a message outside a turn to `POST /v1/gateway/messages` with an ID that starts with `outside:`. Its edits and its delete take the same route, because Hermes can stream the reply of its own turn. The service checks that the chat is the bot of the adapter's profile. It records the text as a message of the bot in a finished task of the bot's chat, and the push rules apply to it as to a reply. Messages that arrive within 10 minutes of each other share one task. The task pushes no separate completion. A streamed reply (a first send with `expect_edits`) pushes when the edit with `finalize` ends it, or when the next message arrives. A delete hides the message, takes it out of the unread count and restores the preview of the bot.
- Only a turn that the service gave is a Coconut turn. Hermes inputs that the gateway runs through the adapter, outside the authority, do not take over the turn of the chat, so their replies also take the route above.
- The adapter keeps these sends in the gateway log: gateway notices (`send_private_notice`, phase 2) and sends that the gateway marks as interim outside a turn, such as shutdown and restart notices. A send that the service refuses, for example to a chat that is not a bot of the profile, fails without a retry and without a plain-text copy.
- A cron delivery carries its job ID. The run of a job in a bot's profile is a routine task in the chat already (phase 4), so the service drops a delivery of a job with a routine task from the last 24 hours.
- Not covered: Hermes moves a "Bot Chat" title that it cannot own aside (`gateway/session_bot.py`, `_create_bot_chat`), and the bot's native conversation from the Coconut worker carries that title. This change does not alter that.

### A gateway that a Hermes client started

- A Hermes chat or TUI client that finds no gateway for its home starts one with `gateway run --idle-exit` (`hermes_cli/gateway_runtime_start.py`, `spawn_unmanaged_gateway`). Hermes stops that gateway after `gateway.unmanaged_idle_exit_seconds` (default 600) with nothing to do.
- Evidence. The idle check (`gateway/run_idle_exit.py`, `busy_reason`, the same in 0.22.4, 0.22.5 and main) keeps the gateway up while a messaging platform other than `local` is enabled in the config or has an adapter, connected or not (`_messaging_busy`). The check does not look at the traffic of an adapter. Only the clients of the gateway's own API and WebSocket stamp activity (`note_client_activity` in `gateway/run_api.py`). A running agent turn counts separately (`_active_work_count`).
- Coconut enables `gateway.platforms.coconut` in each profile. On a Hermes without the change below, a gateway that a client starts for a Coconut home never reaches its idle exit. The long poll of the adapter is not the cause: an adapter that never reaches the service also keeps the gateway up. On 2026-10-28 a gateway that `hermes -p desk chat` started on the Mac mini ran for about four hours.
- The change: NousResearch/hermes-agent#146209 (open) adds the class attribute `keeps_gateway_up` (default `True`) to `BasePlatformAdapter`. `_messaging_busy` skips a platform when each of its adapters sets the attribute to `False`. A running turn of the platform still counts through `_active_work_count`.
- The coconut adapter sets `keeps_gateway_up = False`, because Coconut starts its own gateway when no gateway runs. The attribute takes effect when Hermes ships that change. An earlier Hermes ignores it.
- A runnable cron job of a served profile still keeps an auto-started gateway up (`_cron_busy`), as Hermes intends for each gateway that ticks a cron store. The routines of the bots are such jobs.
- Coconut's own gateway runs without `--idle-exit`, so the idle check does not apply to it. When Coconut uses a gateway that a client started and that gateway exits, Coconut starts its own gateway.
- `test_native_gateway_idle_exit.py` runs client-started gateways on each tested Hermes, with a short idle time and no cron jobs. A gateway without the platform exits. A gateway with the platform and an idle Coconut service exits on a Hermes that has `keeps_gateway_up`, and stays up on an earlier Hermes. A running Coconut turn holds the gateway up on each Hermes.

### Phase 5: the gateway is always on

- The `chat_runtime` setting and its route are gone. A service on the person's Hermes always supervises the gateway, or uses a gateway that already runs for the Hermes home. The service removes a stored `chat_runtime` row at its start.
- A chat turn goes only to the gateway. The service no longer gives a chat turn to the worker when no adapter is connected or no adapter takes the turn.
- When no adapter of the bot's profile is connected, the turn waits, at most 5 minutes (`GATEWAY_WAIT_SECONDS`). The task stays working, and its summary says that it waits for the Hermes gateway. `GET /v1/setup` shows the state of the gateway, the reason and the connected profiles. When the wait ends without an adapter, the task fails with a reason that names the gateway log.
- While the supervised gateway starts again, the chat turns wait. The service starts it again 30 seconds after an exit, and the adapters connect after the gateway restored its sessions. The waiting turns then run. A turn that the gateway ran when it stopped fails, because the adapter stops listing it.
- The gateway fires the routines of each profile that it serves (phase 4). Coconut fires the routines of a profile that no gateway serves: a profile that the person parked in Hermes (`gateway.parked`), or any profile while the gateway starts again. Coconut does not remove a parked file, because that is the person's setting. `GET /v1/setup` lists these profiles in `gateway.unserved_profiles`. A chat turn of a parked profile fails at once and says why.
- Turns of the other types still run in the worker: questions about a Work item, native outcomes and their technical lane. See the sections below.
- A service without a Hermes home (fixture mode and tests) has no gateway. Its provider runs every turn.

### Phase 5, group 1: attachments and reactions

- A chat turn with attached files, a reaction, or earlier emoji feedback goes to the gateway. The service writes the same context into the text of the turn as the worker writes into its prompt (`gateway_chat.gateway_turn_text`, with the shared `native_conversation.attachment_context` and `reaction_feedback_context`). The text of a text file goes into the turn, up to 128000 characters. A binary file, such as an image, is named, and the bot is told that it cannot read it, as in the worker.
- A reaction turn may stay silent. The turn asks for `[SILENT]` when no useful reply exists. The adapter marks its message with `reply_expected=False`, because Hermes otherwise replaces a silence marker on a person's message with a notice (`gateway.response_filters.silence_allowed`). Hermes then drops the marker, but the plugin streams the text first. So the service takes back the streamed marker, shows no commentary, and ends the task as a silent completion, as the worker does (`TurnEvents` with `silent`, `silent_responses.is_silence_reply`).
- The service marks earlier emoji feedback as done after the turn, for both runtimes.
- The worker still builds the same context for a turn that also has an input of a later group, for example an attachment with an open card.

### Phase 5, group 2a: card answers

- Cards in the gateway work as cards of the worker. The plugin offers `coconut_ask`, `coconut_suggest` and `coconut_resolve_reply` with the worker's schemas. `backend/coconut/card_tools.py` holds them and the card rules, and imports nothing, so the plugin loads it by its path.
- A card of `coconut_ask` ends the turn. The service records the card as for the worker (`native_event`), and the task waits for the answer (`needs_you`). The tool result tells the model to stop. The plugin's `pre_tool_call` hook (`hooks.guard_tool`) blocks each further tool call of that turn. The service takes no more output of the turn. Earlier, the gateway turn waited up to 6 hours inside Hermes for the answer, and the card failed when the turn ended.
- The answer continues the task in a new gateway turn. Its text is the worker's text for the same answer (`native_conversation.resume_prompt`). A command approval of a worker turn still resumes in the worker: it allows the one tool call that the worker recorded (`gateway_chat.gateway_resume`). The answer to a card of a gateway cron run continues the Hermes session of that run, with the routine's prompt and tool results: the plugin names the session at the start of the run, and the turn joins it. A card that ends without an answer, for example a dismissed card, starts no turn.
- A chat message while cards are pending goes to the gateway with the pending cards and their human messages (`native_conversation.pending_decisions_context`). `coconut_resolve_reply` records a typed answer to one of them (`decision_inbox.resolve_chat_decision`).
- A follow-up question on a pending card runs in the gateway, in the bot's conversation. Its text is the worker's (`native_conversation.context_prompt`). The turn has the mode `context`: the hook blocks `coconut_ask`, `coconut_suggest`, `clarify`, `coconut_track_pr` and `coconut_message_bot`, and the service takes only text and `coconut_resolve_reply` from it. The answer is a message of the bot under the card, and the card stays pending. A check of a card (`_decision_refresh`) and the results of background research still run in the worker.
- The card rules of the worker (`DECISION_INSTRUCTIONS`) go into the system prompt of each Coconut session of the gateway, in three parts of at most 4000 characters (`register_system_prompt_section`).
- Hermes questions stay as before: a clarify question and a command approval wait inside the turn.
- The `ask` question kind, `POST /v1/gateway/answers` and the waiting `coconut_ask` of the adapter and of cron runs are gone.

### Phase 5, group 2b: the work of a chat

- A chat turn has the work context that the worker gives it: the recent work updates of the chat, the other work of the exchange that the turn can wait for, and for a tracked assignment the worker's work rules and the saved task list (`gateway_chat.work_notes`, with the shared `native_conversation.recent_work_note` and `dependencies_note`).
- `coconut_wait` is a tool of the plugin (`card_tools.GATEWAY_WAIT_SCHEMA`). It records a wait for another task or a card of the exchange (`work_progress.wait_for_task`), or lists them. Hermes starts and watches the background processes of a gateway turn, and Coconut has no record of them. So the gateway schema has no `process_id`: a turn waits for a process with the native `process` tool, or starts it with `notify_on_complete` and finishes, and Hermes continues the chat with the result.
- The plugin's `post_tool_call` hook (`hooks.save_todos`) reports each todo list of a Coconut turn. The service keeps it as the work of the task, as the worker keeps the todo store of its agent (`NativeWork.from_state`). Hermes restores the list of a session from its history, so the gateway needs no restore. Coconut does not change the list of Hermes, so it adds the root of the assignment to its saved copy each time, and completes that root when every step is completed or cancelled.
- An unfinished list continues in a later turn, as after a worker turn (`work_progress._continue_unfinished_work`). That turn starts with the worker's text for it (`WORK_RESUME`) and runs in the gateway. A cron run keeps its list in its own session: Coconut does not continue a routine itself.
- Still in the worker: a question about a Work item. Such a turn has its own work session (`_execution_session_id`), which the worker seeds with the messages of that work, and its own checkout of the repository (`coconut_workspace`). Kanban claims use the same work sessions. A status question about a Work item (`_work_status_only`) is a short answer of the worker without tools and without a session.

### Phase 5, group 2c: recovery turns

- A recovery turn of a chat task goes to the gateway with the worker's recovery rules, its saved recovery context and its Work record (`native_conversation.recovery_note`). Coconut sets such a context for a task that a bot reply continues, and for a native outcome. Both still run in the worker (bot exchanges and Kanban claims), so the gateway gets these turns when those groups move.
- A check of a blocked native task (`coconut_record_pause`) stays with the worker, with its native outcome.
- `coconut_correct_withdrawal` is a tool of the plugin, with the worker's schema (`card_tools.CORRECTION_SCHEMA`). It reopens a card that an automatic card check withdrew in error. The service check (`withdrawal_corrections.correct_withdrawal`) refuses a card outside the execution of the turn, and a card that a person handled. A follow-up on a pending card cannot call it.
- A typed reply to a card names the person's message (`_direct_reply_message_id`). Since group 2a such a turn went to the worker. It now continues in the gateway. The worker's note for a direct reply to a bot that works for another bot comes with it (`native_conversation.direct_reply_note`).
- The technical lane (`_technical_lane`) belongs to a native outcome and stays with the worker.

### Phase 5, group 3: bot exchanges

- The dispatchers give each task outside the bot's conversation its own work session, which only the worker has (`_ensure_execution_session`, the Kanban dispatch). With the gateway, a message of another bot, the return of a delegated task and the continuation of an accepted proposal of the conversation are now turns of the bot's conversation (`gateway_chat.join_conversation` sets `_chat_session`). The dispatchers start them as chat turns, and they run in the bot's gateway chat. A task that already has its own work session keeps it and runs in the worker, for example a coordinator that continues a failed worker in its saved workspace. Fixture mode keeps the worker's way.
- A turn of a bot exchange gets the worker's exchange text (`native_conversation.exchange_note`): the messages that other bots delivered, with the sender that the service knows; the human messages on a returned task; the messages that the task already sent; the rules of a return; and the original assignment. When no message of the person starts the turn, the turn names the task, as the worker does.
- `coconut_message_bot` (phase 3) sends a message to another bot. The service delivers it as for the worker, and it returns the result to the requester when the delegated task ends (`_store_bot_message`, `_drain_returns`).
- A delegated task in the worker can continue as a native outcome on Coconut's board (`_native_intake`). A gateway turn has no Kanban instructions of Coconut, as for a chat turn since phase 2, so the bot answers in the turn.
- Still in the worker: a continuation of a failed task with its own work session, and a reply into a native envelope of Hermes's `message_agent` (`_native_relay`), which only a worker turn creates.

### Phase 5, group 4: GitHub activity, PR reviews and bot changes

- GitHub source activity. A turn of the gateway gets the new activity of the GitHub threads that the cards of its task link, and the tracked PRs with their latest observations, as the worker gives them (`native_conversation.source_activity_note`, `tracked_note`).
- PR reviews. When Coconut sees new check or review results for a PR that a chat task tracks, the task continues in the gateway with the worker's text: `PR_REVIEW_INSTRUCTIONS`, the observed GitHub facts and the original task. The worker's rules for a merge card (`card_tools.PR_MERGE_INSTRUCTIONS`) go into the system prompt of each Coconut session. How a bot reviews and when it offers a merge card stay in its own Hermes skills, so the behavior stays the same.
- Bot changes. `coconut_create_bot` and `coconut_delete_bot` are tools of the plugin, with the worker's schemas and rules (`card_tools`). A chat turn lists the person's messages that can ask for a change, and the bots that the turn can delete (`bot_creation_context`, `bot_deletion_context`). The service checks each request against those messages (`_create_bot_from_chat`, `_delete_bot_from_chat`).
- A coordinator that names a failed gateway task in `continuation_task_id` no longer gets "Use the native workflow". That task has no work session to continue, so the message joins the target bot's gateway conversation as a new turn (`_bot_continuation_owner`).
- Still in the worker: the audit of a work source (`_source_audit_prompt`, `_source_answer_steps`). Coconut gives each audit its own work session, and the dispatchers run it as a Kanban claim beside the person's chats. The gateway has no separate session for each audit, and the dispatch of a chat turn keeps one place for the person's chats. A PR review of a task with its own work session stays with the worker too. The results of a worker's background process (`_background_completion`) and the receipts of a worker's tool calls (`_native_receipts`) stay with the worker that made them.

### Phase 5: work sessions in the gateway, part 1

- A task with its own work session (`_execution_session_id`) and no other worker input now runs in the gateway (`gateway_bridge.gateway_work_turn`). Examples: a scheduled message, a message that is not a person's chat message, the continuation of a proposal of such a task, its PR review, and the answer to its card.
- Such a turn runs in a chat of its own, `<bot ID>:work:<work session ID>` (`gateway_chat.work_chat`). Hermes gives each chat its own session, so the turn runs beside the bot's conversation, and later turns of the same work continue its session. The session root is the worker's root of that work session. A message that Hermes sends to such a chat outside a turn reaches the bot's chat.
- The dispatch stays the same: the Kanban dispatcher of Coconut claims the task, with its capacity and priorities. Hermes records a process for each claim and can end that process, for example after its run limit. For a task that the gateway runs, the claim holds a small placeholder process instead of a prepared worker (`kanban_worker_pid`). When Hermes ends that process, the gateway turn stops and the task fails with the reason. The turn ends the process when it finishes.
- A status question about a Work item (`_work_status_only`) runs in the gateway in a chat of its own, `<bot ID>:status:<task ID>`, with the worker's text and the saved Work context (`run_gateway_status`). The plugin blocks every tool call of the turn (mode `status`), as the worker answers without tools.
- Still in the worker: the audit of a work source. With native outcomes, its text tells the bot to record each accepted outcome with `kanban_comment` and `kanban_create` on Coconut's own board (`work_sources`). The worker binds these tools to that board (`HERMES_KANBAN_DB`), and the gateway's Kanban tools use the board of the Hermes home. So the audits stay with Coconut's Kanban path until Hermes can bind a gateway turn to another board, or until the Kanban spawn hook moves the claims.
- Also still in the worker: a question about a Work item (`_work_context_id`), which uses its own checkout of the repository (`coconut_workspace`), native outcomes and their technical lane, background research results, the results of a worker's background process and the receipts of a worker's tool calls.

### Phase 5: work sessions in the gateway, part 2

- An automatic check of a pending card (`_decision_refresh`) of a task that the gateway runs now runs in the gateway, like a follow-up on the card (`run_gateway_context`). The turn has mode `refresh`, and it is silent: a check that finds nothing to change ends without a reply.
- The plugin offers `coconut_refresh_decision` with the schema of the worker's tool (`card_tools.REFRESH_SCHEMA`). In a check turn, the plugin blocks the card tools and `coconut_resolve_reply`. In every other turn, it blocks `coconut_refresh_decision` (`hooks.guard_tool`).
- The service takes only `coconut_refresh_decision` from a check turn (`decision_freshness.refresh_decision`), and it sends no message to the chat. A follow-up on the card cannot record a check result.
- The results of background research stay in the worker. Only a worker turn makes them: Coconut records the processes of a worker turn as receipts. Hermes runs the processes of a gateway turn and tells its session when one ends.

### Phase 5: work sessions in the gateway, part 3

- A question about a Work item without a native outcome (`_work_context_id`) now runs in the gateway, in the chat of its work session (`gateway_bridge.gateway_work_turn`). The turn text has the worker's text for the Work item (`native_conversation.work_context_note`) and the worker's rule for the worktree of a work session (`card_tools.WORKSPACE_INSTRUCTIONS`).
- The placeholder process of the Kanban claim of a gateway work turn carries the outcome of its work session in `COCONUT_WORKSPACE_OUTCOME`, as a prepared worker does. Coconut then sees the outcome as running while the turn runs (`work_workspace.process_outcomes`).
- The plugin offers `coconut_workspace`. The service names the registry and the outcome of the turn's work session, and it refuses a turn without one (`gateway_chat.gateway_workspace`). The plugin then gets the worktree with `work_workspace.acquire` in the gateway, because `acquire` needs Hermes, and sets it as the working folder of the agent's terminal (`register_task_env_overrides`). The registry has its own lock across processes.
- A question about a Work item with a native outcome stays in the worker. Its worktree can continue the worktree of an earlier outcome on Coconut's board (`workspace_handoff`).
- A turn of a bot's conversation that owns a native outcome is not a chat turn (`work_capacity.chat_turn`). The Kanban scheduler dispatches it from the claim of the outcome's card, and the worker runs it. Before, the chat dispatcher started it without that claim, and the turn failed again and again.

### Phase 5: audits of work sources in the gateway

- An audit of a work source (`_source_audit_prompt`, `_source_answer_steps`) now runs in the gateway, in the chat of its work session. The turn text is the worker's audit text, with a note that names the board tools of this chat (`gateway_chat.AUDIT_BOARD_TOOLS`). An audit that becomes a native outcome stays with the worker.
- The audit text tells the bot to record facts with `kanban_comment` and to create a task with `kanban_create` on Coconut's own board. The worker binds the Hermes tools to that board (`HERMES_KANBAN_DB`). The gateway's own board tools write to another board, so the hooks block every `kanban_` tool in an audit turn (mode `audit`).
- The plugin offers `coconut_kanban_list` and `coconut_kanban_show`, which read only the tasks of the bot, so an audit finds the existing task of an outcome before it acts. It also offers `coconut_kanban_create` and `coconut_kanban_comment`. The service records each write as a receipt of the turn, the Kanban runtime runs the Hermes tool on Coconut's board for the bot's profile (`kanban_runtime.board_tool`), and the service records the result (`gateway_chat.gateway_kanban_tool`). `kanban_create` gets the worker's arguments (`native_bridge.kanban_arguments`): the bot's tenant, a blocked task and an idempotency key of the turn's input. The receipts then adopt a created task as an outcome and record a comment, as for a worker turn.
- The receipts of these board tools are no worker input (`gateway_bridge.worker_input`), so the continuation of an audit after a card answer runs in the gateway too. The claim of the task and its turn then agree: the claim holds the gateway's placeholder, and the gateway runs the turn.
- When a claimed task still goes to the worker, but its claim has no prepared worker, the task waits instead of failing (`kanban_scheduler.await_prepared_worker`). The placeholder stops, and the next claim prepares a worker (`_needs_worker`). Once, at a start, Coconut runs again the tasks that failed this way before the fix, with the person's card answer (`_recover_unprepared_workers`).
- Audits have their own capacity. The Kanban pass bounds them, and an audit that the gateway runs does not take a slot of the person's chats (`native_outcomes._dispatch_chat`).

### The context of a gateway turn is not saved

- Coconut gives each gateway turn its own text (`own`, gateway_chat.gateway_turn_own): the person's messages and their files, the recorded answer of a card, or the title of a turn that no message starts. Hermes saves only this text in the session.
- The rest of the turn text is context of this turn only: the notes and rules of the service, and the live state. The plugin gives it to each model call of the turn, after the last user message, with `llm_request` middleware (`hooks.add_turn_context`). So a later call does not send the context of each earlier turn again. A Hermes without request middleware gets the whole text as the message, as before.
- Each chat turn, work-session turn and continuation after a card answer ends with the cards that wait for the person now, live from the store (`cards_note`: ID, title and bot). A card that the conversation mentioned earlier may be answered or outdated, and only this list is current.
- On Desk's work session a10d62c8, 15 user messages held about 244,000 of 279,000 transcript tokens. With only their own text saved, the transcript is about 57,000 tokens.

### A message of the person goes first

- A bot's conversation runs one turn at a time, because each turn writes the same Hermes session. A person's turn (a message, a reaction or a card answer, `work_capacity.person_turn`) does not wait for bot traffic in that queue.
- In dispatch, a person's turn goes before bot exchanges, returns of delegated work, background results and audits (`Service._dispatch_order`).
- In capacity, a person's turn counts only the person's running turns. Bot traffic cannot fill its slots (`native_outcomes._dispatch_chat`).
- When the bot's conversation runs a bot-originated turn in the gateway, the person's message stops that turn, as a stop of the person does (`gateway_chat.preempt_for_person`). The stopped turn goes back to the queue with the same input, and its streamed reply goes. It runs again after the person's turn, with a note that tells the bot to continue it and not to repeat finished steps.
- A turn that ended before the stop reached it keeps its result. Work sessions and audits run in chats of their own, so they never block the person's chat. A card check that runs in the conversation still finishes first.

### Kanban: not yet

Coconut keeps its private board in `<data>/kanban/kanban.db` and dispatches it itself (`kanban_scheduler`, `kanban_runtime`). Its spawn function makes a Coconut task for each claim, so the work has Coconut cards, receipts and workspaces. The embedded dispatcher of the gateway calls `dispatch_once` with the default spawn of Hermes (`gateway/kanban_watchers_dispatcher.py`, `tick_once_for_board`), which starts a plain `hermes -p <assignee>` worker, and a plugin cannot give it another spawn function. It dispatches the boards of the Hermes home, not `<data>/kanban`. So the gateway does not serve Coconut's board, and the two dispatchers never share one. Upstream ask: a spawn hook for the embedded dispatcher.

#### Respawn guards of Hermes

Hermes refuses to start a ready task again for some reasons (`check_respawn_guard`). One is `active_pr`: a recent comment names a PR, so a new run could open a second PR. Coconut lets a run pass `active_pr` in two cases, and only when no run of the task is alive:

- The task continues accepted input on the same PR (`coconut_pr_followups`), or the input is new.
- The task recovers. Either the bot recovers an outcome (`_self_recovery`), or the claim of any task on the board was interrupted (`kanban_scheduler.interrupted_claim`, the row field `interrupted`), also of a task that Coconut put on the board without an adopted outcome: the board task is ready, its current run is not the run of the claim, and the process of the claim is gone. A restart of the service leaves this state, and before this rule the outcome never started again.

A task without a run keeps the guard: its creator may have opened the PR, and the task waits for new input. Comments and heartbeats do not repeat its guard event, so audits that comment on it leave one event (`_note_respawn_guard`).

A task that a guard holds for more than an hour shows: Coconut logs a warning once and puts the guard in the status of the task and of its Work item (`_note_respawn_guards`).

### What phase 5 deletes

- The worker turn types, one group at a time, after the gateway runs them: attachments and reactions; card answers, work items and claims, and recovery turns; bot exchanges and returns; GitHub source activity and PR reviews, and bot creation and deletion confirmations.
- Then the worker runtime: `hermes_worker.py`, `worker_protocol.py`, `worker_transport.py`, `worker_cli.py`, `worker_activity.py`, the worker parts of `providers.py`, and the `native_*` modules that serve worker turns.
- Coconut's claims of routines that the person runs now, when the gateway runs those too.
- `kanban_scheduler.py` and `kanban_runtime.py` only after Hermes gives its dispatcher a spawn hook. Until then they stay.

## Transport and future providers

The local Mac provider uses the relay tunnel (Noise, see [relay transport](relay-transport.md)), one-time pairing links, and device tokens bound to the phone key.
The app retains a cursor and replays ordered events after reconnecting.
The host continues while the phone is disconnected or suspended.

### Reads and retention

- The phone's reads (state, updates, events, history, cards, search, settings, learning) use their own read-only SQLite connection. A WAL reader sees the last commit. It does not wait for the service lock or for a write transaction.
- The state read pages each conversation through the `messages_chat` index and the `message_exchange_audience` list. The list holds, for each bot, the positions of the exchange rows that its conversation shows. Triggers keep it with the messages. A page reads only the newest rows of one bot. The state read also reads the tasks, the confirmed requests and the Work outcomes through indexes, so its time does not grow with the history.
- The service logs `coconut.request_timing` for each request from the phone: the route without IDs, the status, the time in ms, the wait of a long poll, and the stages of a state read.
- The hourly database maintenance deletes events older than 2 days, but keeps the newest 10,000. It deletes the completed receipts of outcomes that stayed idle for 7 days. It keeps the rows that a lookup still reads.
- A task that ended and did not change for 30 days is retired. Its kept events and its completed receipts go too, also when other tasks of its bot still run.
- The first pass that retires tasks copies each row that it deletes to `<data-dir>/backups/retention-<date>.sqlite3`. It starts only when the disk can keep 2 GiB free after a copy as large as the store. A backup goes after 30 days.

Provider operations cover task start, continuation, context, structured events, cancellation, and artifact retrieval through the host API.
The host owns scheduling and pending decisions. Cloud workers can implement the same operations later.
The mini encrypts notification previews and sends them through Coconut's shared Cloudflare Worker.
The iPhone decrypts previews locally. The relay owns the app's Apple credentials and requires verified app registration.
Agent execution and conversation storage stay on the user's Mac. Secure cloud artifacts remain a future service.
