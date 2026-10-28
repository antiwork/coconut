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

A bot of an earlier install has its home in `<data-dir>/bots/<uuid>/home` and no profiles row. It runs no turns until a person runs `scripts/migrate_bots_to_profiles.py`, and `GET /v1/setup` returns `migration_required: true`.
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

## The Hermes gateway

Coconut moves its bot turns from its own worker processes to the person's Hermes gateway, in phases. Phase 1 made each bot a Hermes profile.

1. Phase 2: plain chat turns run in the gateway, behind a setting. This version.
2. Phase 3: Coconut tools and cards become tools of the `coconut` plugin.
3. Phase 4: cron jobs and Kanban work run in the gateway.
4. Phase 5: Coconut deletes its worker runtime.

### Rules for the phases

- Coconut's own entries in the Hermes profiles (the skills folder, the plugin link, `plugins.enabled` and `gateway.platforms.coconut`) are set again at each update and at each service start. A later phase that needs another Coconut entry adds it to that same edit (`skill_dirs.py`, `gateway_plugin.py`). Coconut never changes other settings of the person.
- No manual migration scripts. A data change that the service needs runs inside the service at its start, after the running turns drain, with a backup.

### Phase 2: plain chat turns

- The setting `chat_runtime` is `worker` (the default) or `gateway`. `PUT /v1/settings/chat-runtime {"chat_runtime": "gateway"}` sets it. The iPhone has no control for it yet.
- Coconut ships the `coconut` gateway platform in `hermes_plugin/coconut`. Setup links it into `<home>/plugins/coconut` of each profile and enables the plugin and its platform there. See [the coconut skill](install-and-update.md#the-coconut-skill-for-hermes).
- The adapter connects to the main listener on loopback. It long-polls `POST /v1/gateway/poll` for the turns of its profile and reports `typing`, `send`, `edit`, `delete` and `done` to `POST /v1/gateway/events`. It sends the token of `<data>/gateway/bridge.json` (mode 0600), which the service writes at each start. The tunnel listener does not serve these routes.
- One chat is one bot: the chat ID is the bot ID. A multiplexing gateway has one adapter for each served profile, and Coconut matches the adapter to the bot by the Hermes home.
- `gateway_bridge.plain_chat_turn` is the routing rule. A new turn goes to the gateway when it is a person's text message in a bot's own chat, without attachments, quoted messages, mentions, cards, reactions, work tracking, bot messages or other Coconut context, and the adapter of the bot's profile is connected. Every other turn runs in the worker.
- The adapter takes turns only after the gateway restored its sessions after a start. Before that, the gateway queues each message and ends its processing without a reply.
- A turn that no adapter takes in 30 seconds runs in the worker. A taken turn fails when the adapter stops listing it for 90 seconds.
- `TurnEvents` maps the gateway events onto the worker's provider events, so the phone sees the same messages. Each `send` starts a segment, and `edit` replaces its text. A new segment keeps the earlier one as a complete interim message (`interim_seal`). A send that the gateway marks as interim, such as commentary, is an interim message. `done` completes or fails the task.
- With `gateway`, the service supervises one `hermes gateway run` process for the person's Hermes home, which Hermes multiplexes across the profiles. The process starts and stops with the service and with the setting. Its output goes to `<data>/gateway/gateway.log`. After an exit, the service waits 30 seconds before it starts the process again.
- When a gateway that the service did not start already runs for that home, the service starts no second one. It uses that gateway when the gateway loaded the platform. `GET /v1/setup` returns `gateway` with the state, the process, the reason and the connected profiles.
- A cancelled task sends `/stop` to the gateway session of the chat. An action that already runs can finish.

### Known gaps of phase 2

- The gateway keeps its own session for each chat. A gateway turn does not continue the worker's conversation of the bot, and the worker does not see the gateway turns.
- A gateway turn has the tools of the gateway only. Coconut tools, such as PR tracking, bot messages and bot creation, are not available. The routing rule sees only the inputs of the turn, not the tools that the answer needs.
- Hermes approvals and clarify questions arrive as interim text. The phone cannot answer them as cards.
- Live token streaming needs `streaming.enabled: true` in the Hermes config. Without it, the reply arrives at the end of the turn, as from the worker. Tool progress is off, so the phone shows only the phases "thinking" and "writing".
- The adapter refuses messages outside a Coconut turn, for example cron deliveries.
- Gateway notices, such as "No home channel is set", stay in the gateway log. They name gateway commands, and Coconut sends chat text without gateway commands.
- A profile without a connected adapter uses the worker. This happens when the gateway does not multiplex, or when the person created the profile outside Coconut after the last setup. A gateway that already runs loads the platform after its next restart.
- Steering during a turn waits for the next turn.

## Transport and future providers

The local Mac provider uses the relay tunnel (Noise, see [relay transport](relay-transport.md)), one-time pairing links, and device tokens bound to the phone key.
The app retains a cursor and replays ordered events after reconnecting.
The host continues while the phone is disconnected or suspended.

Provider operations cover task start, continuation, context, structured events, cancellation, and artifact retrieval through the host API.
The host owns scheduling and pending decisions. Cloud workers can implement the same operations later.
The mini encrypts notification previews and sends them through Coconut's shared Cloudflare Worker.
The iPhone decrypts previews locally. The relay owns the app's Apple credentials and requires verified app registration.
Agent execution and conversation storage stay on the user's Mac. Secure cloud artifacts remain a future service.
