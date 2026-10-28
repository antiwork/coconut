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

- A bot that follows the default gets the model keys of `<Hermes home>/config.yaml` in its own `config.yaml`: `model.default`, `model.provider`, `model.base_url` and the reasoning effort (`agent.reasoning_effort`). These are the keys of the app's default model setting. The API fallback and the other settings do not follow.
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
3. Phase 4: routines run in the gateway as Coconut turns, and each profile keeps its model keys the Hermes way. This version. Kanban stays with Coconut for now.
4. Phase 5: Coconut deletes its worker runtime.

### Rules for the phases

- Coconut's own entries in the Hermes profiles (the skills folder, the plugin link, `plugins.enabled` and `gateway.platforms.coconut`) are set again at each update and at each service start. A later phase that needs another Coconut entry adds it to that same edit (`skill_dirs.py`, `gateway_plugin.py`). Coconut never changes other settings of the person.
- No manual migration scripts. A data change that the service needs runs inside the service at its start, after the running turns drain, with a backup.

### Phase 2: plain chat turns

- The setting `chat_runtime` is `worker` (the default) or `gateway`. `PUT /v1/settings/chat-runtime {"chat_runtime": "gateway"}` sets it. The iPhone has no control for it yet.
- Coconut ships the `coconut` gateway platform in `hermes_plugin/coconut`. Setup links it into `<home>/plugins/coconut` of each profile and enables the plugin and its platform there. See [the coconut skill](install-and-update.md#the-coconut-skill-for-hermes).
- The adapter connects to the main listener on loopback. It long-polls `POST /v1/gateway/poll` for the turns of its profile and reports `typing`, `send`, `edit`, `delete` and `done` to `POST /v1/gateway/events`. It sends the token of `<data>/gateway/bridge.json` (mode 0600), which the service writes at each start. The tunnel listener does not serve these routes.
- One chat is one bot: the chat ID is the bot ID. A multiplexing gateway has one adapter for each served profile, and Coconut matches the adapter to the bot by the Hermes home.
- `gateway_bridge.plain_chat_turn` is the routing rule. Phase 3 widens it (see below).
- The adapter takes turns only after the gateway restored its sessions after a start. Before that, the gateway queues each message and ends its processing without a reply.
- A turn that no adapter takes in 30 seconds runs in the worker. A taken turn fails when the adapter stops listing it for 90 seconds.
- `TurnEvents` maps the gateway events onto the worker's provider events, so the phone sees the same messages. Each `send` starts a segment, and `edit` replaces its text. A new segment keeps the earlier one as a complete interim message (`interim_seal`). A send that the gateway marks as interim, such as commentary, is an interim message. `done` completes or fails the task.
- With `gateway`, the service supervises one `hermes gateway run` process for the person's Hermes home, which Hermes multiplexes across the profiles. The process starts and stops with the service and with the setting. Its output goes to `<data>/gateway/gateway.log`. After an exit, the service waits 30 seconds before it starts the process again.
- When a gateway that the service did not start already runs for that home, the service starts no second one. It uses that gateway when the gateway loaded the platform. `GET /v1/setup` returns `gateway` with the state, the process, the reason and the connected profiles.
- A cancelled task sends `/stop` to the gateway session of the chat. An action that already runs can finish.

### Phase 3: one conversation, Coconut tools and cards

- One conversation. The worker resumes a bot's chat from a root session and records the newest session as the tip in the `state_meta` table of the profile's state.db (`native_conversation`). Each gateway turn names the root. The plugin's `pre_gateway_dispatch` hook points the gateway session of the chat at the tip with the session store that the hook receives. When the bot has no conversation yet, the gateway session becomes the tip. Both runtimes then write into the same native session and follow its compression lineage. Coconut chose this direction, the gateway joins the native conversation, because the worker already keeps it and the session store has a public switch for it.
- Coconut tools. The plugin registers the `coconut` toolset: `coconut_ask`, `coconut_track_pr` and `coconut_message_bot`. A tool finds its chat from the gateway session context (`gateway.session_context.get_session_env`), and the adapter of the process names the running turn. `coconut_ask` shows a card and waits for the answer. The other tools call `POST /v1/gateway/tools`, which acts for the task of that turn. A `check_fn` that reads the session context hid the tools also in Coconut chats, so the tools show on each platform of the gateway, behind its tool search, and refuse to act outside a Coconut chat turn. A Coconut worker does not load these tools: it has its own.
- Cards. A clarify question (`send_clarify`), a command approval (`_send_exec_approval_prompt`) and `coconut_ask` report a `question` event. The service shows it as a decision card, and the task stays working while the gateway turn waits. The answer goes to the adapter with its next poll. The adapter gives it to Hermes with `resolve_gateway_clarify` or `resolve_gateway_approval`, or returns it to the waiting tool. A typed reply to a command approval declines the command and gives Hermes the reason. A card that is still open when the turn ends closes as failed.
- Streaming. The plugin's `on_stream_*` hooks receive the text of each model call, also when the gateway streams nothing to platforms. The adapter sends the text in pieces (`delta`), and the phone sees the reply while the model writes. The person's `streaming` setting stays as it is. The first reply send of the gateway takes over from the streamed text.
- Routing. With `gateway`, each turn of a person's message in a bot's own chat goes to the gateway, also with a quote or a bot mention (they go into the message text). These turns stay in the worker: attachments, open cards that the bot answers, reactions, work items and claims, routines, bot exchanges and returns, GitHub source activity, PR reviews, bot creation and deletion confirmations, recovery and withdrawal turns, and each turn that continues an answered card.
- The startup restore of the runner has no public signal. The adapter reads `GatewayRunner._startup_restore_in_progress`, and the Coconut contract check (`hermes_contract.verify_gateway_contract`) requires it, with the other gateway interfaces that the plugin uses. A Hermes that changes them fails the contract check.

### Known gaps after phase 3

- Upstream asks to Hermes: a public signal that the gateway takes inbound messages after its startup restore; a public way for a platform to join a chat with an existing native session (the hook uses the session store's `switch_session`); a per-platform streaming switch that works when the global `streaming.enabled` is off (Coconut uses the stream observer hooks instead).
- A gateway turn does not see the recent Work updates of the bot. The worker gives them as context.
- Only three Coconut tools exist in the gateway. Work tracking, waits, bot creation and deletion, and card replies stay with the worker turns that need them.
- Tool progress is off, so the phone shows only the phases "thinking" and "writing".
- The adapter refuses messages outside a Coconut turn, for example cron deliveries.
- Gateway notices, such as "No home channel is set", stay in the gateway log. They name gateway commands, and Coconut sends chat text without gateway commands.
- A profile without a connected adapter uses the worker. This happens when the gateway does not multiplex, or when the person created the profile outside Coconut after the last setup. A gateway that already runs loads the platform after its next restart.
- Steering during a turn waits for the next turn.

### Phase 4: routines in the gateway

- Evidence. Every Hermes gateway ticks the cron store of each profile that it serves, also with one profile only (`gateway/run_bootstrap.py`, `_start_gateway_start_cron_and_housekeeping` and `_cron_tick_profile_homes`). Hermes has no setting that keeps a served profile or a job out of these ticks. A `cron.provider` of a plugin cannot replace the ticker of a multiplexing gateway (`cron/scheduler_provider.py`, `scheduler_for_profile_mode`). A cron run executes in a Hermes cron session, often in a separate worker process, never as an inbound turn of a platform. So the gateway cannot deliver a cron run "as a Coconut turn" through `deliver=coconut`: delivery carries only the final text.
- One firing owner. When a gateway ticks a bot's profile, the gateway fires the bot's routines, and Coconut claims none of them (`gateway_chat.gateway_ticked_bots`). A host gateway of the Hermes home ticks each profile except a parked one. A profile with its own gateway is ticked by that gateway. Without a gateway, Coconut claims the routines as before, with the native fire claims. The native fire claim decides a race at a change, so a routine never fires twice.
- A cron run is a Coconut turn. The plugin's session hooks see a cron run through `cron.execution_identity.current_cron_execution`. At its start (`on_session_start`) the plugin opens a turn with `POST /v1/gateway/cron`, and the service makes a routine task in the bot's chat. The run reports to that turn: streamed text, commentary (`on_interim_message`), the answer (`post_llm_call`) and the end (`on_session_end`). The coconut tools act for that task. `coconut_ask` shows a card and waits for its answer with `POST /v1/gateway/answers`, because a cron worker process has no adapter that polls.
- `chat_runtime: worker`. Coconut starts no gateway, so Coconut fires the routines. A gateway that the person runs still ticks every profile that it serves: Hermes gives no way to stop that. Coconut then leaves those routines to that gateway, and its runs come to Coconut as turns through the plugin, as in `gateway` mode.
- Model keys. Each profile reads its provider keys from its own `.env`, as Hermes keeps them. The Hermes entries of each update and start copy the provider keys of the default profile to each bot profile that does not have them (#284).

### A gateway that a Hermes client started

- A Hermes chat or TUI client that finds no gateway for its home starts one with `gateway run --idle-exit` (`hermes_cli/gateway_runtime_start.py`, `spawn_unmanaged_gateway`). Hermes stops that gateway after `gateway.unmanaged_idle_exit_seconds` (default 600) with nothing to do.
- Evidence. The idle check (`gateway/run_idle_exit.py`, `busy_reason`, the same in 0.22.4, 0.22.5 and main) keeps the gateway up while a messaging platform other than `local` is enabled in the config or has an adapter, connected or not (`_messaging_busy`). The check does not look at the traffic of an adapter. Only the clients of the gateway's own API and WebSocket stamp activity (`note_client_activity` in `gateway/run_api.py`). A running agent turn counts separately (`_active_work_count`).
- Coconut enables `gateway.platforms.coconut` in each profile. On a Hermes without the change below, a gateway that a client starts for a Coconut home never reaches its idle exit. The long poll of the adapter is not the cause: an adapter that never reaches the service also keeps the gateway up. On 2026-10-28 a gateway that `hermes -p desk chat` started on the Mac mini ran for about four hours.
- The change: NousResearch/hermes-agent#146209 (open) adds the class attribute `keeps_gateway_up` (default `True`) to `BasePlatformAdapter`. `_messaging_busy` skips a platform when each of its adapters sets the attribute to `False`. A running turn of the platform still counts through `_active_work_count`.
- The coconut adapter sets `keeps_gateway_up = False`, because Coconut starts its own gateway or uses its worker when no gateway runs. The attribute takes effect when Hermes ships that change. An earlier Hermes ignores it.
- A runnable cron job of a served profile still keeps an auto-started gateway up (`_cron_busy`), as Hermes intends for each gateway that ticks a cron store. The routines of the bots are such jobs.
- Coconut's own gateway runs without `--idle-exit`, so the idle check does not apply to it. When Coconut uses a gateway that a client started and that gateway exits, Coconut starts its own gateway.
- `test_native_gateway_idle_exit.py` runs client-started gateways on each tested Hermes, with a short idle time and no cron jobs. A gateway without the platform exits. A gateway with the platform and an idle Coconut service exits on a Hermes that has `keeps_gateway_up`, and stays up on an earlier Hermes. A running Coconut turn holds the gateway up on each Hermes.

### Kanban: not yet

Coconut keeps its private board in `<data>/kanban/kanban.db` and dispatches it itself (`kanban_scheduler`, `kanban_runtime`). Its spawn function makes a Coconut task for each claim, so the work has Coconut cards, receipts and workspaces. The embedded dispatcher of the gateway calls `dispatch_once` with the default spawn of Hermes (`gateway/kanban_watchers_dispatcher.py`, `tick_once_for_board`), which starts a plain `hermes -p <assignee>` worker, and a plugin cannot give it another spawn function. It dispatches the boards of the Hermes home, not `<data>/kanban`. So the gateway does not serve Coconut's board, and the two dispatchers never share one. Upstream ask: a spawn hook for the embedded dispatcher.

### What phase 5 deletes

Phase 5 deletes the worker runtime when `gateway` is the only chat runtime:

- `hermes_worker.py`, `worker_protocol.py`, `worker_transport.py`, `worker_cli.py`, `worker_activity.py` and the worker parts of `providers.py`.
- The native bridge of the worker: `native_bridge.py`, `native_conversation.py` (the gateway joins the same session), `native_execution.py`, `native_relay.py`, `native_mcp.py` and the other `native_*` modules that serve worker turns.
- Coconut's routine claims: `native_routine_execution.py`, `native_routine_receipts.py` and the claim and finish code of `routine_coordinator.py`, after the gateway is the only firing owner.
- `kanban_scheduler.py` and `kanban_runtime.py` only after Hermes gives its dispatcher a spawn hook. Until then they stay.

While `worker` stays the default, both paths stay.

## Transport and future providers

The local Mac provider uses the relay tunnel (Noise, see [relay transport](relay-transport.md)), one-time pairing links, and device tokens bound to the phone key.
The app retains a cursor and replays ordered events after reconnecting.
The host continues while the phone is disconnected or suspended.

Provider operations cover task start, continuation, context, structured events, cancellation, and artifact retrieval through the host API.
The host owns scheduling and pending decisions. Cloud workers can implement the same operations later.
The mini encrypts notification previews and sends them through Coconut's shared Cloudflare Worker.
The iPhone decrypts previews locally. The relay owns the app's Apple credentials and requires verified app registration.
Agent execution and conversation storage stay on the user's Mac. Secure cloud artifacts remain a future service.
