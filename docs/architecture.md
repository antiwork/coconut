# Architecture

The SwiftUI app consumes a versioned HTTP API through an end-to-end encrypted relay tunnel. It never parses Hermes CLI output.
The Mac service owns coordination. The person's Hermes gateway runs each turn of a bot, through Coconut's `coconut`
gateway plugin (`hermes_plugin/coconut`).
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
| Team rooms, their members and the copy of each team log | Coconut SQLite database (`team_*` tables), copied from the relay. See [team log](team-log.md#mac). |
| Keys and member tokens of the team rooms | One private file for each room in `<data>/team` |

Coconut retains small projections for phone display. Native files own behavior.
Inbox threads group decisions. They do not restrict a bot's recall of its own conversation.
The gateway resumes native history and compression continuations. The phone never displays those internal boundaries.
Model and access defaults resolve from the active native default profile.
Each bot stores its MCP servers, approval settings and fallback models in its own native configuration, as in Hermes. A bot without its own model uses the default model.
The Model page shows the fallback chain of the default profile (`fallback_providers`, then the legacy `fallback_model`). Only the model and the provider of each entry leave the Mac. The page sets the first fallback model from the same list as the default model. `PUT /v1/settings/fallback` replaces the whole chain: an entry that the chain has keeps its other keys, and a new entry gets only its provider and model.

## Native integration

Coconut installs no files into the Hermes source checkout.
Coconut ships its `coconut` skill in `skills/` of the install folder. Hermes loads it through `skills.external_dirs`: setup adds that folder to the person's Hermes profiles, which are also the bots, and a profile that Coconut creates gets it at creation. The person's other Hermes surfaces, such as the Telegram gateway, then know how to update, pair, and repair Coconut. See [the coconut skill](install-and-update.md#the-coconut-skill-for-hermes).
The gateway of the person's Hermes runs each turn with the bot's profile: its tools, context, memory and skills.
The `coconut` plugin adds Coconut's tools and hooks through Hermes's plugin API.
The service records the board tools of a turn as receipts of the turn.
The recorded card choice keeps its exact action scope.
A card check records the revision of native identity and memory files of the bot. The service checks that revision
before it records a current assessment. Changes during a turn leave its cards stale until another check.
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
After an answer, the service starts a continuation with the recorded choice and the index of the completed tool
receipts (`coconut_read_receipts`). A turn does not repeat a completed effect.

Native tool execution is not a transaction with SQLite.
If a process dies between an effect and its completion receipt, Coconut marks the task failed for inspection.
It does not promise exactly-once execution for arbitrary shell commands or external services.
Bot deliveries commit their task and receipt together.
The explicit Coconut file executor reconciles output identity and content before retrying.

Routines are native jobs. The gateway of the bot's profile runs them (see "Phase 4: routines in the gateway").

## Isolation and routing

Each bot has a separate Hermes profile, session, workspace, and browser directory.

### Bots are Hermes profiles

Each bot is one Hermes profile of the person, keyed by the profile name. `default` is the Hermes home (`$HERMES_HOME`, else `~/.hermes`). Each other profile is `<Hermes home>/profiles/<name>`. The `profiles` table in `coconut.sqlite3` maps the bot UUID to the profile, so bot IDs never change.

- The service scans the profiles at its start and when `<Hermes home>/profiles` changes. A new profile folder becomes a new bot.
- A folder that disappears makes its bot `missing`. The bot keeps its history and returns with a folder of the same name. Coconut does not follow renames: a renamed folder is a missing bot and a new bot.
- Coconut chats, `hermes -p <name>`, and the gateway share the same skills, memories, config, and cron jobs of the profile.
- Settings are native. The default settings are those of `<Hermes home>/config.yaml`. Each other profile uses its own config.
- Coconut creates a profile only through the Hermes profile API, once. A failure fails the request. A folder that Hermes left behind becomes a bot at the next scan.
- A settings write to a missing profile fails. Coconut never creates a profile folder itself.
- Coconut keeps its own files of a bot in `<data-dir>/bots/<uuid>`: workspace, browser, temporary files, and terminal scripts.
- Deleting a bot means "Remove from Coconut": the folder stays, and a scan does not add it again. To delete the Hermes profile itself, the person sends `POST /v1/bots/<id>/profile/delete` with `{"confirm": "<profile name>"}`. Coconut uses Hermes's `delete_profile` and never deletes `default`. A bot cannot request this deletion.
`HOME` remains the Mac account home so macOS Keychain works.
`USER` identifies the Mac account so Claude Code selects its saved Keychain login.
Coconut scopes Codex, GitHub CLI, and XDG settings through their native environment variables.
Claude Code shares the Mac account's native login, settings, and session history across bots and managed Hermes Terminal.
Its configuration overrides remain unset so Claude Code and Hermes use the same native credential store.
These paths organize data; they do not restrict a full-access process from reading other paths.
Browser tools must use the intended profile. Shared desktop control needs serialized ownership.

Homebrew packages and trust share one scope across Coconut bots. The processes of a bot set `HOMEBREW_XDG_CONFIG_HOME` to `<data-dir>/shared`. Homebrew owns `shared/homebrew/trust.json` and its write lock. Hermes profiles, browser profiles, and supported tool settings remain separate. Keep the inherited `HOME` and tool configuration paths. Existing trust grants need an explicit move to this scope; Coconut does not merge private trust files automatically.
Coconut does not provide an operating system sandbox or universal desktop lock.

A bot sends a message to another bot with `coconut_message_bot`. The service delivers it with a stable ID and a
durable delivery receipt.
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
- `TurnEvents` maps the gateway events onto the worker's provider events, so the phone sees the same messages. Each `send` starts a segment with its complete text, and `edit` replaces that text. A new segment keeps the earlier one as a complete interim message (`interim_seal`). A send that the gateway marks as interim, such as commentary, is an interim message. `done` completes or fails the task.
- The service supervises one `hermes gateway run` process for the person's Hermes home, which Hermes multiplexes across the profiles. The process starts and stops with the service. Its output goes to `<data>/gateway/gateway.log`. After an exit, the service waits 30 seconds before it starts the process again.
- When a gateway that the service did not start already runs for that home, the service starts no second one. It uses that gateway when the gateway loaded the platform. `GET /v1/setup` returns `gateway` with the state, the process, the reason and the connected profiles.
- A cancelled task sends `/stop` to the gateway session of the chat. An action that already runs can finish.

### The plugin mode `team`

A Mac that runs Coconut for a team only has no person, and its bots answer the members of its teams in the team chat (see [A bot answers](team-log.md#a-bot-answers)). The plugin has a second mode for that Mac. `gateway.platforms.coconut.extra.mode: team` in the config of a profile turns it on, and only the command of the operator `coconut team connect-chat --confirm` writes it (`scripts/team_chat.py`, `backend/coconut/team_chat_config.py`).

- The plugin reads the mode from the config of the profile when Hermes loads it (`coconut.mode`). Without the mode, and when the config does not read, it registers what it registers for a full install.
- In the mode `team`, it registers the platform with the adapter of `hermes_plugin/coconut/team.py`, and nothing else: no tool, no hook, no middleware, no skill and no change of cron. The hint of the platform names the team chat, not the cards of a phone.
- The adapter takes the turns of the team-only service with the same poll, events and token as above. It gives a turn to the gateway as a message of a group chat: the chat ID is `<bot ID>:team:<room ID>` for the Team chat and `<bot ID>:team:<room ID>:<chat entry ID>` for a named chat of the team (see [Chats of a team](team-log.md#chats-of-a-team)), so two bots of one Mac in one team, and two chats of one team, never share a session. The name is the team, or "<team> · <chat>" for a named chat. The sender is the member of the team, the messages of the same chat since the last message of the bot there are `channel_context`, and the rules of the chat are `channel_prompt`. A reply to the bot names its message (`reply_to_message_id`, `reply_to_is_own_message`). Hermes keeps a session for each member of a group, as `group_sessions_per_user` says.
- The platform is `trusted_inbound`, so Hermes checks no sender. The team-only service is the gate: only a member of the team starts a turn, and only with a message that names the bot.
- What the team chat needs of Hermes:
  - the fields of a message event that the adapter sets (`team.EVENT_FIELDS`) and the methods of the base adapter that it calls (`team.BASE_METHODS`);
  - the fields `trusted_inbound` and `display_overrides` of the registration (`PlatformEntry`, `team.REGISTRATION_FIELDS`). Without `trusted_inbound`, Hermes refuses each member as an unknown user ("Unauthorized user") and ends the turn with no reply. Without `display_overrides`, Hermes sends its tool progress into the chat as messages of the bot (the tier `high` and the global defaults show it).
  - The other options of the registration can go. Without `display_tier`, the overrides go over the global defaults, which give the same display. Without `platform_hint`, the rules of the chat still come with each turn (`channel_prompt`). Without `max_message_length`, Hermes does not split a reply. Without `emoji`, only the label in a list of Hermes changes.
  - Coconut supports Hermes 0.22.4 or later (`backend/coconut/hermes_versions.json`), and each of these versions has all of this.
- An approval and a clarify question go to the service as questions. The service denies each approval and puts a clarify question into the chat (`team_turns.Reply`). The adapter opens no turn for a run that Hermes starts by itself: such a send goes to `POST /v1/gateway/messages`. It compresses no session and keeps no photo file: Hermes sends its notice for a file.
- A production agent runs in that gateway. Each entry point of the adapter catches its own faults, so a fault of Coconut never stops the gateway or its other channels. A Hermes without a field of the message event or a method that the adapter needs (`team.missing`), or a registration that raises, gives one log line, and the gateway runs without the Coconut channel.
- A Hermes that lacks only a field of the registration (`team.unregistered`) gets the channel in a form that takes no message. At the start of the gateway, the plugin writes one log line: "Coconut: the team chat takes no message, because this Hermes lacks <names>. Update Hermes." The adapter polls, and each poll names the fields (`lacking`). The bridge keeps the text while the polls of that home name it, takes only a short list of names, and gives that adapter no turn. The service answers each trigger at once with a notice (`team_turns.OUTDATED`), and `coconut status` says that this Hermes is too old for the team chat. The adapter also gives no turn to Hermes when one reaches it.

### Phase 3: one conversation, Coconut tools and cards

- One conversation. The worker resumes a bot's chat from a root session and records the newest session as the tip in the `state_meta` table of the profile's state.db (`native_conversation`). Each gateway turn names the root. The plugin's `pre_gateway_dispatch` hook points the gateway session of the chat at the tip with the session store that the hook receives. When the bot has no conversation yet, the gateway session becomes the tip. Both runtimes then write into the same native session and follow its compression lineage. Coconut chose this direction, the gateway joins the native conversation, because the worker already keeps it and the session store has a public switch for it.
- Coconut tools. The plugin registers the `coconut` toolset: `coconut_ask`, `coconut_track_pr` and `coconut_message_bot`. A tool finds its chat from the gateway session context (`gateway.session_context.get_session_env`), and the adapter of the process names the running turn. Since phase 5, group 2a, the card tools have the worker's schemas (see below). The tools call `POST /v1/gateway/tools`, which acts for the task of that turn. A `check_fn` that reads the session context hid the tools also in Coconut chats, so the tools show on each platform of the gateway, behind its tool search, and refuse to act outside a Coconut chat turn. The document tools are the exception (see Documents in [Messages outside a Coconut turn](#messages-outside-a-coconut-turn)). A Coconut worker does not load these tools: it has its own.
- Questions of Hermes. A clarify question (`send_clarify`) and a command approval (`_send_exec_approval_prompt`) report a `question` event. The service shows it as a decision card, and the task stays working while the gateway turn waits. The answer goes to the adapter with its next poll. The adapter gives it to Hermes with `resolve_gateway_clarify` or `resolve_gateway_approval`. A typed reply to a command approval declines the command and gives Hermes the reason. A card that is still open when the turn ends closes as failed.
- No streaming. The phone shows a message of the bot only when it is complete, never as partial text that grows. While the bot works, the chat shows that it works (`typing`, the phases "thinking" and "writing"). The adapter sets `SUPPORTS_MESSAGE_EDITING = False`, so Hermes streams no text to the platform (`gateway/run_turn_runner.py`, `_setup_stream_consumer`), also when the person's `streaming` setting is on. That setting stays as it is for the other platforms. Hermes still sends commentary as complete interim messages, and it can still edit a message with a complete text. Until 2026-11, the plugin's `on_stream_*` hooks sent the text of each model call in pieces (`delta`). Coconut removed them, because a text that grows on screen felt clunky. A gateway that still runs the earlier plugin sends `delta` until it restarts: the service accepts the event and drops it. The split of a reply into separate messages stays the same.
- Routing. Each turn of a person's message in a bot's own chat goes to the gateway, also with a quote or a bot mention (they go into the message text). These turns stay in the worker: attachments, open cards that the bot answers, reactions, work items and claims, routines, bot exchanges and returns, GitHub source activity, PR reviews, bot creation and deletion confirmations, recovery and withdrawal turns, and each turn that continues an answered card.
- The startup restore of the runner has no public signal. The adapter reads `GatewayRunner._startup_restore_in_progress`, and the Coconut contract check (`hermes_contract.verify_gateway_contract`) requires it, with the other gateway interfaces that the plugin uses. The check also requires the fields `trusted_inbound` and `display_overrides` of `PlatformEntry`: without them, Hermes refuses the messages of the phone or sends tool progress into the chat (`require_registration_fields`). A Hermes that changes them fails the contract check.

### Known gaps after phase 3

- Upstream asks to Hermes: a public signal that the gateway takes inbound messages after its startup restore; a public way for a platform to join a chat with an existing native session (the hook uses the session store's `switch_session`); a public way for a platform to refuse streamed text other than `SUPPORTS_MESSAGE_EDITING` (Coconut sets it to `False`, and the remote agent path of `gateway.proxy_url` does not read it).
- A gateway turn did not see the recent Work updates of the bot. Phase 5, group 2b gives them as context, as the worker does.
- Only a few Coconut tools exist in the gateway. Work tracking, waits, and bot creation and deletion stay with the worker turns that need them.
- Tool progress is off, so the phone shows only the phases "thinking" and "writing".
- The adapter refused messages outside a Coconut turn, for example cron deliveries. Phase 4 delivers them (see [Messages outside a Coconut turn](#messages-outside-a-coconut-turn)).
- Gateway notices, such as "No home channel is set", stay in the gateway log. They name gateway commands, and Coconut sends chat text without gateway commands. The exceptions are the commands that the person may type (see "Compression of gateway chats").
- A profile without a connected adapter has no chat runtime, and its chat turns wait (phase 5). This happens when the gateway does not multiplex, or when the person created the profile outside Coconut after the last setup. A gateway that already runs loads the platform after its next restart.
- Steering during a turn waits for the next turn.

### Phase 4: routines in the gateway

- Evidence. Every Hermes gateway ticks the cron store of each profile that it serves, also with one profile only (`gateway/run_bootstrap.py`, `_start_gateway_start_cron_and_housekeeping` and `_cron_tick_profile_homes`). Hermes has no setting that keeps a served profile or a job out of these ticks. A `cron.provider` of a plugin cannot replace the ticker of a multiplexing gateway (`cron/scheduler_provider.py`, `scheduler_for_profile_mode`). A cron run executes in a Hermes cron session, often in a separate worker process, never as an inbound turn of a platform. So the gateway cannot deliver a cron run "as a Coconut turn" through `deliver=coconut`: delivery carries only the final text.
- One firing owner. A gateway that serves a bot's profile fires the bot's routines. Since phase 5, the evidence is the connected coconut adapter of the profile (`gateway_chat.gateway_served_bots`), not the config. The routines of a bot whose profile no gateway serves are paused (see phase 5). A run that the person starts is a trigger in Hermes, which the gateway runs too. Coconut fires no routine itself.
- A cron run is a Coconut turn. The plugin's session hooks see a cron run through `cron.execution_identity.current_cron_execution`. At its start (`on_session_start`) the plugin opens a turn with `POST /v1/gateway/cron`, and the service makes a routine task in the bot's chat. The run reports to that turn: commentary (`on_interim_message`), the answer (`post_llm_call`) and the end (`on_session_end`). The coconut tools act for that task. A card of `coconut_ask` ends the run, and its answer continues the run's Hermes session in a gateway turn (phase 5, group 2a).
- Model keys. Each profile reads its provider keys from its own `.env`, as Hermes keeps them. The Hermes entries of each update and start copy the provider keys of the default profile to each bot profile that does not have them (#284).

### Messages outside a Coconut turn

- Evidence (Hermes 0.22.4 and 0.22.5). Hermes runs its own inputs in the gateway session of a chat: the completion of a background process (`gateway/run_notifications.py`, `_inject_watch_notification`), the result of a delegated task, a Kanban wake (`gateway/wake.py`, `deliver_wake`) and a `/loop` tick. With a session authority, these go into the session's queue (`gateway/session_automation.py`), and the authority runs them through the runner, not through the adapter (`gateway/session_ingress.py`, `execute_admission`). The adapter sees no `on_processing_start`. At the end, `deliver_response` sends the reply with `send_final_ledgered` and `_send_with_retry`, and after a failed send Hermes tries a plain-text copy. Kanban notices (`gateway/kanban_watchers_notifier.py`), process watch notices, stall notices and cron deliveries call `adapter.send` directly.
- Until this change, the adapter refused each such send. On 2026-10-28 the Mac mini lost two replies this way. Both were final replies of Hermes turns in a bot's chat session. One of these turns read Kanban task `t_42d02397`. The other turn ran while Hermes delivered a message into a profile's Bot Chat.
- Now the adapter sends a message outside a turn to `POST /v1/gateway/messages` with an ID that starts with `outside:`. Its edits and its delete take the same route. The service checks that the chat is the bot of the adapter's profile. It records the text as a message of the bot in a finished task of the bot's chat, and the push rules apply to it as to a reply. Messages that arrive within 10 minutes of each other share one task. The task pushes no separate completion. Hermes streams no text to the platform, so each send is a complete message, and an edit replaces its text without a push. A delete hides the message, takes it out of the unread count and restores the preview of the bot.
- Only a turn that the service gave is a Coconut turn. Hermes inputs that the gateway runs through the adapter, outside the authority, do not take over the turn of the chat, so their replies also take the route above.
- Photos. Hermes takes each Markdown image out of a reply (`extract_images`) and calls `send_image` or `send_animation` with its address and alternative text. Without its own method, Hermes would send the caption and the address as two lines of text. The adapter sends one Markdown image line, `![caption](address)`. The phone shows that line, and the older two-line form, as a photo. An app version without photos shows the image in the text.
- Photo files. For a photo that a bot saved on the Mac (a `MEDIA:` path), Hermes calls `send_image_file`. Without its own method, Hermes would send a notice that the image could not be sent. The adapter reads a file of at most 4 MB that Hermes accepts as a delivery path (`validate_media_delivery_path`) and sends its bytes to `POST /v1/gateway/files`. The service checks that the chat is the bot of the adapter's profile and that the bytes are a PNG, JPEG, GIF, WebP or HEIC image, and keeps them as an attachment of the bot. The adapter then sends `![caption](coconut-file://<artifact id>)`. The phone loads the bytes through `GET /v1/artifacts/<id>/content`. A larger file, another type, or a refused file gets the notice of Hermes, which names no path.
- Documents. A bot keeps pages in the Library of the phone with `coconut_save_document`, `coconut_read_document` and `coconut_delete_document` (`backend/coconut/documents.py`). A document belongs to the bot that saved it, under a key that the bot chooses. A save with the same key replaces the content and adds a version, and the service keeps the 10 latest versions. A document is Markdown, which the phone draws natively, or HTML, which the phone shows in a closed web view. Coconut owns the style of a page: the web view gives the page the design tokens as CSS variables, the system font, `coconut.palette`, d3 and Observable Plot, and a content security policy that blocks the network. The service refuses a page that sets a font, writes a color as a value, or loads something, and names the reason to the bot. A save of a Markdown page always works, and its answer can have `notes`: at most three sentences that say what the page can do better, for four rules that the lines of the page show without a guess: a callout with a list or with long text, a long column header, the same first cell in each row of a wide table, and facts packed with em dashes (`documents.page_notes`). The check reads only the first 2,000 lines of the first 200,000 characters, each cut to 1,000 characters, so its work has a bound, and it skips a complete fenced block, such as a chart, but reads nothing after code that it cannot skip. A note is one fixed sentence for each rule: it names no place and repeats no text of the page. Both save routes give them, also with the answer `unchanged`. A delete has no notes. The skill reference `skills/coconut/references/pages.md` has the recipe of a good page. A document can end on a date; after it, the Library no longer shows the document. The phone reads `GET /v1/documents` and `GET /v1/documents/<id>`, and deletes with `DELETE /v1/documents/<id>`. The person pins a document with `PATCH /v1/documents/<id>`, and the Library shows the pinned documents first. A pin is how this Mac shows the Library to its person, not content of the page: a save keeps it, and a bot does not read it. The tooling of the Mac keeps a page for a bot without a turn (`POST /v1/gateway/document`): no turn asked for it, so it shows no card in the chat and a save that changes nothing writes nothing, which lets the tooling run on a timer and keep a page current. The tooling deletes the page with `DELETE /v1/gateway/document`. A save with `audience: "team"` also shares the page with each team that the person shared the bot with, and `GET /v1/documents` also lists the team pages of the other Macs of each team, with `team_bots` for their bots. See [team log](team-log.md#bots-and-pages).
- Documents outside a Coconut chat. Hermes shows the `coconut` toolset on each platform, so a bot also has the document tools in a conversation that is not a Coconut chat turn, for example a chat of another messaging app, a session of an MCP client, or a cron run without a Coconut turn. A team uses one shared bot from such places too, so the three document tools work there for the bot of the profile that runs the conversation. The other Coconut tools act for the task of a turn, so they still refuse there.
  - The bot. The tool reads the Hermes home of the running profile (`hermes_constants.get_hermes_home`) and sends it with the request. The service matches the home to the bot of an active profile, as for the adapter and the tooling (`gateway_chat._gateway_profile`). The model cannot name a home: the plugin sets it after the arguments. A multiplexing gateway runs each turn under a context override of the home of its profile (`set_hermes_home_override`), and Hermes carries that context into its tool threads. A process of one profile has that home in `HERMES_HOME`. So a bot never reaches the documents of another bot. The contract check (`hermes_contract.verify_gateway_contract`) requires that the home follows the override.
  - The routes. A save goes to `POST /v1/gateway/document`, the route of the tooling: the same page rules, `expires_at` and `audience` as a save in a turn, no card in a chat, and a save that changes nothing writes nothing. A read and a delete go to `POST /v1/gateway/tools` with `home` in place of `turn_id` (`gateway_chat.gateway_page_tool`). That route refuses each other tool without a turn. The token of the bridge file authorizes both routes, and the tunnel listener serves neither. The tooling of the Mac also deletes its page with `DELETE /v1/gateway/document` and `{home, key}` (`gateway_chat.gateway_document_delete`). The answer is `deleted`, or `absent` for a key without a page, so the tooling can repeat a delete. The plugin does not use that route: a delete of the bot outside a turn still goes to the tools route. The plugin finds the bridge file in the config of the running profile (`gateway.platforms.coconut.extra.bridge`), so it needs no coconut adapter in the process.
  - The answer. A save gives the model the status, the key, the version, the notes of the page when it has some, and a note: the page is in the Library of the Coconut app, no chat shows a card, so the bot tells the person where to find it. A delete has an event without a task, as a delete from the phone.
  - Versions. An older plugin refuses each Coconut tool outside a Coconut chat turn and sends nothing, as before. A newer plugin with an older service saves through the route of the tooling, which each service since that route (#418) has. That service refuses a read or a delete without a turn ("This Coconut turn no longer runs."), and the plugin tells the model that the service is older. In each case the tool returns an error to the model, and the conversation goes on.
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
- The gateway fires the routines of each profile that it serves (phase 4). The routines of a profile that no gateway serves do not run: a profile that the person parked in Hermes (`gateway.parked`), or any profile while the gateway starts again. Hermes runs an agent cron job only in the profile's gateway (`hermes cron run` sends the job to that gateway, and `hermes cron tick` skips agent jobs without one), so Coconut has no other way to run them without its worker. Each such routine shows that it is paused, and why (`routine_coordinator._pause_unserved_routines`, the routine's `error`), in the style of the refusal of a parked chat. It runs again on its schedule when a gateway serves the profile, without a catch-up by Coconut. Coconut does not remove a parked file, because that is the person's setting. `GET /v1/setup` lists these profiles in `gateway.unserved_profiles`. A chat turn of a parked profile fails at once and says why.
- Coconut starts no worker with the gateway on. See "No worker with the gateway".
- A service without a Hermes home (fixture mode and tests) has no gateway. Its provider runs every turn.

### Phase 5, group 1: attachments and reactions

- A chat turn with attached files, a reaction, or earlier emoji feedback goes to the gateway. The service writes the same context into the text of the turn as the worker writes into its prompt (`gateway_chat.gateway_turn_text`, with the shared `native_conversation.attachment_context` and `reaction_feedback_context`). The text of a text file goes into the turn, up to 128000 characters. A binary file, such as an image, is named, and the bot is told that it cannot read it, as in the worker.
- A reaction turn may stay silent. The turn asks for `[SILENT]` when no useful reply exists. The adapter marks its message with `reply_expected=False`, because Hermes otherwise replaces a silence marker on a person's message with a notice (`gateway.response_filters.silence_allowed`). Hermes then drops the marker. When a reply is only the marker, the service takes it back, shows no commentary, and ends the task as a silent completion, as the worker does (`TurnEvents` with `silent`, `silent_responses.is_silence_reply`).
- The service marks earlier emoji feedback as done after the turn, for both runtimes.
- The worker still builds the same context for a turn that also has an input of a later group, for example an attachment with an open card.

### Phase 5, group 2a: card answers

- Cards in the gateway work as cards of the worker. The plugin offers `coconut_ask`, `coconut_suggest` and `coconut_resolve_reply` with the worker's schemas. `backend/coconut/card_tools.py` holds them and the card rules, and imports nothing, so the plugin loads it by its path.
- A card of `coconut_ask` ends the turn. The service records the card as for the worker (`native_event`), and the task waits for the answer (`needs_you`). The tool result tells the model to stop. The plugin's `pre_tool_call` hook (`hooks.guard_tool`) blocks each further tool call of that turn. The service takes no more output of the turn. Earlier, the gateway turn waited up to 6 hours inside Hermes for the answer, and the card failed when the turn ended.
- A recorded `coconut_wait` and a recorded card check (`coconut_refresh_decision`) end their turn in the same way. After such a tool, the model often writes no text. Hermes treats an empty reply after a tool call as a failure: it nudges the model, tries again three times after a wait, then tries the fallback model (`agent/turn_empty_response.py`). So the plugin's `llm_execution` middleware (`hooks.end_turn_text`) gives an empty reply of such a turn the text `adapter.END_TEXT`, in the response format that the transport of Hermes reads. Hermes then ends the turn at once. The middleware also closes an empty reply at once in a run that Hermes started by itself, for example for a finished background process, because no person waits for its reply. In another turn, the first empty reply after a tool result stays as it is, and Hermes reminds the model once. The middleware marks the turn (`adapter.NUDGED`) and closes the next empty reply of the turn. An empty reply without a tool result before it, in a turn without the mark, stays as it is, so a typed question keeps the retries and the fallback model of Hermes. A reply that holds only reasoning counts as empty. The adapter does not send `END_TEXT` to the chat (`adapter.ends_quietly`), and it reports the end of the turn as `quiet`. When the turn sent no other text, `TurnEvents` completes it as a silent response: the chat shows no message, and the chat list keeps its last message and its unread count. A reply with text stays as it is. A Hermes without execution middleware retries as before.
- The answer continues the task in a new gateway turn. Its text is the worker's text for the same answer (`native_conversation.resume_prompt`). A command approval of a worker turn still resumes in the worker: it allows the one tool call that the worker recorded (`gateway_chat.gateway_resume`). The answer to a card of a gateway cron run continues the Hermes session of that run, with the routine's prompt and tool results: the plugin names the session at the start of the run, and the turn joins it. A card that ends without an answer, for example a dismissed card, starts no turn.
- A chat message while cards are pending goes to the gateway with the pending cards and their human messages (`native_conversation.pending_decisions_context`). A turn without a human message that can answer a card gets only the fields that name each card: id, title, kind, revision, task, and the PR or resource. `coconut_resolve_reply` records a typed answer to one of them (`decision_inbox.resolve_chat_decision`).
- A follow-up question on a pending card runs in the gateway, in the bot's conversation. Its text is the worker's (`native_conversation.context_prompt`). The turn has the mode `context`: the hook blocks `coconut_ask`, `coconut_suggest`, `clarify`, `coconut_track_pr` and `coconut_message_bot`, and the service takes only text and `coconut_resolve_reply` from it. The answer is a message of the bot under the card, and the card stays pending. A check of a card (`_decision_refresh`) and the results of background research still run in the worker.
- The card rules of the worker (`DECISION_INSTRUCTIONS`) go into the system prompt of each Coconut session of the gateway, with the hint of the platform (`platform_hint` of the `coconut` platform). The prompt sections of plugins (`register_system_prompt_section`) share a budget of 8000 characters in Hermes. The rules are longer, so Hermes skipped two of their three parts in each session.
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
- The receipts of these board tools do not route a turn to the worker, so the continuation of an audit after a card answer runs in the gateway too. The claim of the task and its turn then agree: the claim holds the gateway's placeholder, and the gateway runs the turn.
- When a claimed task still goes to the worker, but its claim has no prepared worker, the task waits instead of failing (`kanban_scheduler.await_prepared_worker`). The placeholder stops, and the next claim prepares a worker (`_needs_worker`). Once, at a start, Coconut runs again the tasks that failed this way before the fix, with the person's card answer (`_recover_unprepared_workers`).
- Audits have their own capacity. The Kanban pass bounds them, and an audit that the gateway runs does not take a slot of the person's chats (`native_outcomes._dispatch_chat`).
- A source in `work-sources.json` can set `"track_pull_requests": true`. Then Coconut tracks each open pull request that the source reports (an item with `kind` `pull_request`) for the bot of the source, unless the bot already tracks it with a review owner (`work_sources.pull_requests_to_track`, `_track_source_pr`). The review owner is the audit of the PR: the new one of this pass, or else the newest audit or bound task of that item, so a PR that already has a card or a no-card record is tracked too. This is the tracking that `coconut_track_pr` does, without a model call. Coconut then resumes the bot when the checks or reviews of the PR change, and the bot's own skills decide about a merge card. A PR that another agent opens, for example under a shared GitHub account, gets the same review and card flow as a PR that the bot opened itself.

### Native outcomes in the gateway

- A native outcome (`_native_outcome_id`) runs in the gateway, in the chat of its work session, as a turn of that session (`gateway_bridge.gateway_work_turn`). A question about the Work item of an outcome is a turn of that outcome, so it runs there too: its message about the item goes with the native task of the outcome (`gateway_turn_text`). Its technical lane runs there too (see "The technical lane in the gateway").
- The Kanban claim of the card holds the gateway's placeholder, not a prepared worker (`kanban_worker_pid`). Hermes's dispatcher keeps a claim with a live placeholder: it extends an expired claim and does not reclaim it, as for a worker. A placeholder that dies is a crashed worker, and Hermes runs the card again.
- The turn text has what the worker gives the turn: the native task (`build_worker_context`), the outcome rules (`native_outcomes.WORK_INSTRUCTIONS`), the receipts of earlier attempts of the turn, and the rules of a check of a blocked task. The terminal of the turn runs in the workspace of the claim (`folder`, `hooks.guard_tool`), as the worker's TERMINAL_CWD. `coconut_workspace` carries the proof of the claim, so an outcome can continue the retired worktree of an earlier outcome (`workspace_handoff.native_successor`).
- The board tools. Hermes refuses a plugin tool with the name of one of its tools, so each board tool of a worker has a Coconut name: `coconut_kanban_show`, `_comment`, `_heartbeat`, `_complete`, `_block`, `_schedule`, `_request_review`, `_link`, `_create`, `_list` and `_unblock`. The turn text maps each `kanban_` name to its Coconut name (`gateway_chat.OUTCOME_BOARD_TOOLS`), so the rules and the skills of a bot keep working. The schemas of the tools that only a worker had are the schemas of Hermes's tools, without the board (`tools.claim_schemas`).
- Each Coconut turn has these board tools, as each worker turn had the board tools of Hermes on Coconut's board: a chat turn can show, list, create, comment, link and unblock. Only a turn of a native outcome has the tools that act on the run of its card (`RUN_TOOLS`: complete, block, schedule, request_review and heartbeat). The hooks block the gateway's own board tools in each Coconut turn, because they write to another board, and the block message names the Coconut tool. So a bot profile can drop `toolsets: [kanban]` without breaking its skills.
- The service runs each tool on Coconut's board (`gateway_chat.gateway_kanban_tool`). It first checks that the turn holds the claim of its outcome. Then the Kanban runtime runs Hermes's tool with the worker env of that claim (`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_CLAIM_LOCK`, `kanban_runtime.board_tool`), so Hermes refuses a call for another task or for an earlier run, as for a worker. The runtime takes one request at a time, so the env of one call never reaches another. `kanban_list` and `kanban_unblock` route the board, and Hermes refuses them in a worker. They run without the claim: `kanban_list` shows only the tasks of the bot, and `kanban_unblock` changes only a task of the bot.
- A tool that ends the run (`kanban_complete`, `kanban_block`, `kanban_schedule`, `kanban_request_review`) also stops the placeholder of the claim. The turn then finishes its reply without a claim, and the stopped placeholder is no lost claim (`claim_finished`).
- A turn of an outcome that ends without one of these tools is a failed run, as for a worker: Coconut finishes the run as a protocol violation (`Board.finish`), and Hermes runs the card again.
- Each write is a receipt of the turn, so adoption and completion proofs read it as before.
- When Nous merges the spawn hook of the Hermes dispatcher (NousResearch/hermes-agent#146543), the dispatcher can start these turns itself, and the placeholder and the claim plumbing go.

### The technical lane in the gateway

- A turn of the technical lane of a native outcome (`_technical_lane`, technical_lane.py) runs in the gateway, as a turn of the outcome's work session. Its text adds the lane rules and the lane record, as the worker gives them (`technical_lane.instructions`).
- The holds of the lane stay as for the worker. The board tools of the turn make receipts, so the service holds a Kanban write other than a note on the outcome's own card (`_refuse_technical_tool`). The Coconut tools that are events of the worker (cards, answers, waits, withdrawal corrections, bot changes, bot messages, tracked PRs, pauses) get the same refusal (`gateway_chat.LANE_EVENTS`, `_refuse_technical_event`). The hooks block the Hermes tools that the lane holds: clarify, cronjob_manage, message_agent, and the gateway's send_message (mode `technical`). Hermes also runs its own turns in the chat of the lane, for example for the completion of a background process of the lane. These turns keep the same holds and get no board tools, until the next Coconut turn of the chat in another mode (`adapter.LANE_CHATS`).
- A Hermes approval request in a lane gets a denial at once, and a clarify gets the hold of the lane, without a card (`record_gateway_question`, `LANE_QUESTION`), as the worker denies it.
- A finished lane turn returns the owner to its pending decision, and the native card parks as before (`_finish_technical_lane`, `_technical_native_state`).

### Receipts are no worker input

- The receipts of a turn and of the turns that it continues no longer send the turn to the worker. A gateway turn gets their index in its text, and `coconut_read_receipts` reads them (`gateway_chat.work_notes`, `gateway_read_receipts`). The worker also replayed a completed tool call by its hash; a gateway turn has the rule of the index instead: do not repeat a completed effect.

### Results of background processes in the gateway

- A turn that continues a task with the results of its background processes (`_background_completion`) runs in the gateway. Its text is the worker's text for the results, with the person's new message after them (`native_conversation.background_results_prompt`). Hermes saves this text as the turn's own text, as the worker's session saved it.
- Only a worker's process makes such results: Coconut watches the processes that a worker's receipts name (background_processes). Hermes delivers the completion of a gateway turn's process itself, as a turn of its own (gateway_message). A follow-up on a card with such results runs in the gateway too (see "Card follow-ups in the gateway").

### Card follow-ups in the gateway

- A follow-up on a card of a task with its own work session (a native outcome, an audit, bot traffic) runs in the gateway, in the chat of that work session, as the worker answers it in that session (`gateway_chat.gateway_context_turn`, `run_gateway_context`). Before, only a follow-up on a card of the bot's conversation ran there.
- The results of background research that returned to a follow-up continue it in the gateway too, with the worker's text for them (`context_prompt`).
- The Kanban claim of such a follow-up holds the gateway's placeholder, not a prepared worker (`kanban_worker_pid`). The turn stops the placeholder when it ends.
- A status question about a Work item already runs in the gateway, without tools (`run_gateway_status`).

### Bot traffic stays out of the person's chat

- The person's chat (the bot's conversation, `_chat_session`) gets only the turns that the person starts: messages, reactions, card answers and their follow-ups, and the continuation of a proposal that the person accepted.
- A message of another bot, a return, a delegated result and a retry of a failed exchange are bot traffic. Each one runs in a work session of the bot (`_ensure_execution_session`), in the chat of that session (`<bot>:work:<id>`, gateway_work_turn). Delivery, returns and waits work as before. The reply of such a turn still shows in the person's chat in Coconut, but the bot's Hermes session of the person's chat does not hold the bot traffic.
- Before, bot traffic joined the person's chat (#308). On Desk's main chat, 49 of 53 active turns were bot traffic: about 159,000 of 160,500 transcript tokens. The chat was the bots' inbox, and its prompt grew to about 293,000 tokens.
- Migration: no data moves. Tasks that already joined the person's chat stay there and run as before (the person's turn still goes first). New deliveries go to work sessions. The bot traffic that the Hermes session already holds goes when Hermes compacts the session, or with /new.

### The context of a gateway turn is not saved

- Coconut gives each gateway turn its own text (`own`, gateway_chat.gateway_turn_own): the person's messages and their files, the recorded answer of a card, or the title of a turn that no message starts. Hermes saves only this text in the session.
- The rest of the turn text is context of this turn only: the notes and rules of the service, and the live state. The plugin gives it to each model call of the turn, after the last user message, with `llm_request` middleware (`hooks.add_turn_context`). So a later call does not send the context of each earlier turn again. A Hermes without request middleware gets the whole text as the message, as before.
- Each chat turn, work-session turn and continuation after a card answer ends with the cards that wait for the person now, live from the store (`cards_note`: ID, title and bot). A card that the conversation mentioned earlier may be answered or outdated, and only this list is current.
- On Desk's work session a10d62c8, 15 user messages held about 244,000 of 279,000 transcript tokens. With only their own text saved, the transcript is about 57,000 tokens.

### A session gets each record of its work once

- Some context grows with the work, and each turn sent all of it: the comment thread of the native task (about 12,900 tokens in Desk's latest turn), the messages of other bots (7,300) and the messages that the task sent (6,000). These are now records of the turn (`native_conversation.turn_records`). Each has a stable ID: the comment ID, the position and text of a delivered message, or the text and task state of a sent message. A sent message whose task changed is a new record.
- The context of the turn no longer has them (`work_notes`, `exchange_note(records=True)`). A Kanban claim gives the worker context of Hermes without its comment thread, and the newest 50 comments with their IDs apart (`kanban_runtime.worker_context`, which builds the context of Hermes with no comments).
- The plugin adds to the turn's own text only the records that the Hermes session of the chat does not hold yet (`hooks.add_records`, in the `pre_gateway_dispatch` hook, after the turn joined its session). Hermes saves that text. When the turn ends, the plugin notes the IDs of the added records for that session, in the `state_meta` table of Hermes (`coconut.records.<session ID>`, `hooks.commit_records`). A turn that the gateway skipped as busy notes nothing.
- A session holds a record when both are true: the key of the session has the record's ID, and a user message that the session holds now still has the record's text. The plugin reads the live messages of the session at the start of each turn (`hooks.session_text`, `SessionDB.get_messages_as_conversation`). It compares the texts with each run of white space as one space, because Hermes removes the white space at the end of a saved message.
- Current Hermes compresses a session in place, under the same session ID (`compression.in_place`, the default). It replaces the earlier messages with a summary and keeps the key. With `compression.in_place: false`, Hermes continues in a newer session of the lineage, which has no key. In both cases, the next turn gets every record again whose text the session lost. A record in a message that the compression kept stays held.
- Known limit: the plugin decides before the turn. When Hermes compresses in a turn (the hygiene of the gateway before the turn, or the agent during the turn), that turn gets only its new records. The next turn of the session gets every lost record again.
- A session that does not hold the records gets every record: the first turn of a conversation, the turn after /new, and the turn after Hermes lost the conversation's session. A reset of the chat's session in the gateway does not lose them: the turn joins the conversation's session again, which holds them.
- A turn without a session of the bot's conversation, or a Hermes without request middleware, gets every record in each turn.

### Compression of gateway chats

- Hermes compresses a session before a model call only when it knows the size of the call from the real usage of an earlier call (the usage anchor of the session). A gateway turn restores the anchor only when it matches the conversation history exactly. On Desk's main chat, the anchor of 293,292 prompt tokens had a base count of 167, but its fingerprint matched message 170, and the gateway's agent history had "Hey" at index 168. So each turn started without a known size, Hermes deferred the check, and a turn of one model call never compressed. The exact cause is in Hermes.
- Coconut compresses as a safety net. After each model call of a Coconut turn, the plugin compares the prompt tokens with the compression threshold of Hermes, from the `compression` settings of the profile (`hooks.note_prompt_size`, `compression_threshold`). When a call reached it, the adapter sends `/compress` for the chat after the turn (`CoconutAdapter._compress`). Hermes's notice of that compression is a reply to the command, so it stays in the gateway log.
- On Desk (claude-opus-5-5, a window of 1,000,000 tokens, `threshold: 0.15`), the threshold is 150,000 tokens. The main chat's last prompt of 293,292 tokens is above it, so the next turn of that chat ends with a compression.
- The person may type `/compress` and `/new` in a Coconut chat. When the whole message is one of these commands, it reaches Hermes as the command, without Coconut's context. Only a direct message that the person typed on the phone counts, as the service checks it for a bot change (`gateway_chat.gateway_command`). Bot traffic never becomes a command, also when its text starts with one. Hermes asks to confirm `/new`, and the person answers with `/approve`, `/always` or `/cancel`, as Hermes's text says. Other text that starts with "/" stays a chat message.
- After `/new`, the bot's conversation continues in the new session (`hooks.follow_new_session`, the `on_session_reset` hook). Without this, the next turn would join the old session again.

### A message of the person goes first

- A bot's conversation runs one turn at a time, because each turn writes the same Hermes session. A person's turn (a message, a reaction or a card answer, `work_capacity.person_turn`) does not wait for bot traffic in that queue.
- In dispatch, a person's turn goes before bot exchanges, returns of delegated work, background results and audits (`Service._dispatch_order`).
- In capacity, a person's turn counts only the person's running turns. Bot traffic cannot fill its slots (`native_outcomes._dispatch_chat`).
- When the bot's conversation runs a bot-originated turn in the gateway, the person's message stops that turn, as a stop of the person does (`gateway_chat.preempt_for_person`). The stopped turn goes back to the queue with the same input, and the reply that it sent so far goes. It runs again after the person's turn, with a note that tells the bot to continue it and not to repeat finished steps.
- A turn that ended before the stop reached it keeps its result. Work sessions and audits run in chats of their own, so they never block the person's chat. A card check that runs in the conversation still finishes first.

### No worker with the gateway

- With the gateway on, Coconut starts no worker. Each Kanban claim holds the gateway's placeholder process (`kanban_worker_pid`), also for a task that the gateway does not run.
- A turn of the bot's conversation that owns a native outcome (`gateway_bridge.conversation_outcome_turn`) runs in the gateway, in the bot's chat, as the worker ran it in the bot's conversation. It has the outcome rules, the workspace of its claim and the board tools of its claim, but no worktree, as in the worker. Its card answers and follow-ups run there too. A follow-up with a Kanban claim runs in the workspace of that claim, in any chat (`run_gateway_context`).
- A turn that the gateway does not run, a continuation after its card answer, and a follow-up on its card fail at once with the reason (`gateway_chat.gateway_unsupported`, `UNSUPPORTED`). Examples: a task from an earlier version that waits for a worker (`_native_job`), and a command approval that a worker recorded. Before, such a turn went to the worker.
- A run of a routine that the person starts ("Run now") is a trigger in Hermes (`jobs.trigger_job`, `native_routines` operation `routine_trigger`). The gateway's cron runs the job at its next tick. Until then, the routine's task waits (`_gateway_trigger`), and the cron run of the gateway takes that task as its turn (`gateway_chat.gateway_cron`). A task that no run takes in 5 minutes fails with the reason (`routine_coordinator._expire_routine_triggers`). Coconut no longer claims a scheduled routine: the gateway fires it.
- Fixture mode has no gateway, so its provider still runs every turn. It runs no routine: only the gateway runs one, and Coconut refuses a run that the person starts there (`ROUTINES_NEED_GATEWAY`).

### The worker is gone

Coconut has no worker runtime of its own. The gateway runs each turn, and a turn that it does not run fails with the
reason (`gateway_unsupported`).

- Deleted: the worker (`hermes_worker.py`), its transport and protocol (`worker_transport.py`, `worker_protocol.py`,
  `worker_cli.py`, `worker_activity.py`), the worker parts of `providers.py`, and the modules that only served worker
  turns (`native_bridge.py`, `native_turn_scope.py`, `native_compression_head.py`, `native_routine_execution.py`,
  `native_processes.py`, `native_relay.py`, `live_messages.py`).
- `HermesProvider` keeps the settings, the models, the profiles and the routines of Hermes. Its turn methods refuse a
  turn. `FixtureProvider` runs each turn of fixture mode and the tests.
- Each Kanban claim holds a placeholder process (`kanban_worker_pid`), also in fixture mode. The placeholder ends with
  the service (`HOLDER`, `_close_gateway_chat`). The board runs when the provider has a Hermes Kanban runtime
  (`HermesProvider.native_board`).
- The relay of the worker is gone: its envelope delivery, its recovery and the reply into an envelope
  (`_native_reply_pending`). The service keeps the saved `_native_relay` of earlier tasks as history, for the proofs that
  read it (`_completed_peer_continuation`, `_block_check_relay_lineage`, `_settled_original_card`). Each of these proofs also accepts a message of `coconut_message_bot`. That message has no
  envelope, so its proof is the service's own record of the delivery: from the sending task to the bot, with the
  receiving task in its receipt, and not a progress note. `_block_check_message_intake` proves the intake of the
  delegated block check this way.
- The watch of a worker's background processes is gone: the process subscriptions of its receipts, their delivery,
  the waits for a process, and the checks of interrupted tool calls. A local command that an earlier worker left
  without a result keeps the state `executing`, so its effect stays unknown. Hermes watches the processes of a gateway
  turn and
  delivers their results itself. `coconut_wait` waits for a task or a card (`card_tools.WAIT_SCHEMA`), and the index of
  pending waits is `native_receipts_pending_wait`.
- The bot change context of a turn moved to `bot_changes.py`. The routing of a turn has no worker inputs:
  `gateway_bridge.WORK_SESSION_INPUTS` and `OTHER_PATHS` choose its chat.
- The Hermes contract checks only the interfaces that Coconut still uses (`hermes_contract.verify_contract`).

Known gaps of the gateway, which the worker covered and no turn covers now:
- No live steering of a running turn (`steer_task`), no `continuation_task_id` in `coconut_message_bot`, and no receipt
  index in the text of a follow-up on a card.
- The global limit of parallel helpers of the Coconut settings does not reach a profile that inherits it.
- A work session that session maintenance ended as idle is not reopened. Its next turn starts a new Hermes session.

### The tools of the worker in the gateway

- `coconut_continue_work` continues a quiescent native outcome with new facts, and its `independent_technical_work`
  opens a technical lane (`native_outcomes.continue_native_work`). The plugin loads its schema from `technical_lane`.
- `coconut_track_work` tracks the work of a work session in Coconut Work (`tracked_work`). A turn of the bot's chat
  records an outcome with `coconut_kanban_create`, and a native outcome reports on its card, so the service refuses the
  tool there with that rule (`TRACK_WORK_CHAT`, `TRACK_WORK_OUTCOME`), as the worker did not offer it there.
- A follow-up on a card and a technical lane refuse both tools, as they refuse the card tools.
- `coconut_unlink_dependency` and `coconut_restart_service` are gone, with their service handlers: the unlink of a native
  dependency and its fence, and the restart that a bot asked for. A new Hermes, an update drain and a workspace
  recovery still restart the service.

### Kanban: not yet

Coconut keeps its private board in `<data>/kanban/kanban.db` and dispatches it itself (`kanban_scheduler`, `kanban_runtime`). Its spawn function makes a Coconut task for each claim, so the work has Coconut cards, receipts and workspaces. The embedded dispatcher of the gateway calls `dispatch_once` with the default spawn of Hermes (`gateway/kanban_watchers_dispatcher.py`, `tick_once_for_board`), which starts a plain `hermes -p <assignee>` worker, and a plugin cannot give it another spawn function. It dispatches the boards of the Hermes home, not `<data>/kanban`. So the gateway does not serve Coconut's board, and the two dispatchers never share one. Upstream ask: a spawn hook for the embedded dispatcher.

#### Respawn guards of Hermes

Hermes refuses to start a ready task again for some reasons (`check_respawn_guard`). One is `active_pr`: a recent comment names a PR, so a new run could open a second PR. Coconut lets a run pass `active_pr` in two cases, and only when no run of the task is alive:

- The task continues accepted input on the same PR (`coconut_pr_followups`), or the input is new.
- The task recovers. Either the bot recovers an outcome (`_self_recovery`), or the claim of any task on the board was interrupted (`kanban_scheduler.interrupted_claim`, the row field `interrupted`), also of a task that Coconut put on the board without an adopted outcome: the board task is ready, its current run is not the run of the claim, and the process of the claim is gone. A restart of the service leaves this state, and before this rule the outcome never started again.

A task without a run keeps the guard: its creator may have opened the PR, and the task waits for new input. Comments and heartbeats do not repeat its guard event, so audits that comment on it leave one event (`_note_respawn_guard`).

A task that a guard holds for more than an hour shows: Coconut logs a warning once and puts the guard in the status of the task and of its Work item (`_note_respawn_guards`).

### What phase 5 deletes

- Done: the worker turn types, one group at a time, after the gateway ran them, and then the worker runtime (see "The
  worker is gone").
- Done: Coconut's own claims of routines. The `native_routines` operations `routine_claim`, `routine_heartbeat`,
  `routine_finish` and `routine_prune_completions`, `native_routine_receipts.py`, the release of a waiting occurrence,
  the interception of a worker's `cronjob_manage` run, and the reports of routines that an owner created
  (`routine_reports.py`) are gone. The gateway fires each routine, and a gateway cron run records no `cronjob_manage`
  receipt that such a report needs. Tasks of earlier claims keep their `_native_job` as history, and the continuation
  guards still read it. The receipt files of earlier claims in `<data-dir>/bots/<uuid>/runs` stay, and nothing reads
  them.
- `kanban_scheduler.py` and `kanban_runtime.py` only after Hermes gives its dispatcher a spawn hook. Until then they stay.

## Team-only mode

A Mac can run a production Hermes agent with no person at it and no Coconut. To let that agent publish pages to a
team, such a Mac runs the service with `--team-only` or `COCONUT_MODE=team` (`backend/coconut/team_only.py`). The
service then keeps only the team rooms and the pages of the bots that the Mac declares. It imports nothing of Hermes and
nothing of the full service, changes no Hermes file, starts no gateway and pairs no phone. A full service and a
team-only service never start on the data folder of the other. See [team-only mode](team-log.md#team-only-mode).

## Transport and future providers

The local Mac provider uses the relay tunnel (Noise, see [relay transport](relay-transport.md)), one-time pairing links, and device tokens bound to the phone key.
The app retains a cursor and replays ordered events after reconnecting.
The host continues while the phone is disconnected or suspended.

### Reads and retention

- The phone's reads (state, updates, events, history, cards, search, settings, learning) use their own read-only SQLite connection. A WAL reader sees the last commit. It does not wait for the service lock or for a write transaction.
- The state read pages each conversation through the `messages_chat` index and the `message_exchange_audience` list. The list holds, for each bot, the positions of the exchange rows that its conversation shows. Triggers keep it with the messages. A page reads only the newest rows of one bot. The state read also reads the tasks, the confirmed requests and the Work outcomes through indexes, so its time does not grow with the history.
- With `X-Coconut-Work: recent`, the state read and the reset of an update leave out finished Work, except the cards that the loaded messages show. A history page brings the cards of its own messages. The app has no Work list of its own, so Coconut keeps no route that stops, resumes, dismisses or archives Work.
- The service logs `coconut.request_timing` for each request from the phone: the route without IDs, the status, the time in ms, the wait of a long poll, and the stages of a state read.
- The hourly database maintenance deletes events older than 2 days, but keeps the newest 10,000. It deletes the completed receipts of outcomes that stayed idle for 7 days. It keeps the rows that a lookup still reads.
- A task that ended and did not change for 30 days is retired. Its kept events and its completed receipts go too, also when other tasks of its bot still run.
- The first pass that retires tasks copies each row that it deletes to `<data-dir>/backups/retention-<date>.sqlite3`. It starts only when the disk can keep 2 GiB free after a copy as large as the store. A backup goes after 30 days.

Provider operations cover task start, continuation, context, structured events, cancellation, and artifact retrieval through the host API.
The host owns scheduling and pending decisions. Cloud workers can implement the same operations later.
The mini encrypts notification previews and sends them through Coconut's shared Cloudflare Worker.
The iPhone decrypts previews locally. The relay owns the app's Apple credentials and requires verified app registration.
Agent execution and conversation storage stay on the user's Mac. Secure cloud artifacts remain a future service.
