# Coconut API v1

The app uses HTTPS with certificate pinning. Routes use `/v1` unless stated otherwise.
Authenticated requests use `Authorization: Bearer <device token>`.
Writes require `Idempotency-Key: UUID`. Reusing a key with different input returns 409.
Errors use `{error: string}`. Some errors add a `code` that the phone acts on. UUIDs do not encode host names or filesystem paths.

Without a usable Hermes on the Mac, the service runs in refuse mode. `/health`, `POST /pair`, and `GET /setup` still work. Every other route answers `503` with `{error, code: "hermes_unavailable", hermes}`, where `hermes` is the status that `GET /setup` returns.

## Read routes

- `GET /health` outside `/v1` reports service health.
- `GET /state` returns bots, tasks, messages, decisions, artifacts, routines, cursor, server time, model catalog, and approvals.
- `GET /state` with `X-Coconut-State: content` omits native settings and returns recent messages with history cursors.
- `GET /state` and `GET /updates` with `X-Coconut-Work: recent` leave out the Work items whose cards all finished more than
  7 days ago, and the tasks that only those cards name. An item stays when a message in the response shows one of its
  cards. The response lists in `older_work_bot_ids` the bots whose Task details can load such Work. Older servers ignore
  the header and send all Work.
- `GET /bots/{id}/work/older` returns `{artifacts, tasks, cursor}`: the Work that the recent state leaves out for the bot's
  Task details, with its tasks. Task details lists a bot's native Work and the native Work of tasks that started in its
  conversation.
- `GET /state/settings` returns native bot settings, model catalog, approvals, worker settings, MCP servers, and routines.
  It also asks Hermes for the live model lists of the providers, at most once in five minutes. The response carries the lists that
  Hermes has cached. The live lists arrive a moment later, and the service sends `model_catalog.updated`. If the live look fails,
  the service still sends the event, and it tries again after 30 seconds, when the settings are read next.
- `GET /updates?after=N&wait=20` returns changed tasks and messages, current decisions and Work, or a content reset.
  With `X-Coconut-Updates: 4`, the reset has no routines. The app keeps the routines of its native settings.
- `GET /cards/{id}` returns the current decision, task, conversation bot, owner, and cursor.
- `GET /history/{bot_id}?before=N` returns an older message page, its referenced tasks and cards, and the next history
  cursor.
- `GET /search?q=text` searches stored message text and returns matching bot IDs.
- `GET /events?after=N&wait=20` returns ordered events, a cursor, and server time.
- `GET /artifacts/{id}/content` returns authenticated artifact bytes.
- `GET /learning` returns agreement groups by bot and action type.
- `GET /setup` returns the account's own Hermes setup: `{found, profiles, logins, sign_ins, model, mcp_servers}`. Each profile has `id`, `name`, `soul`, `memories`, `skills`, and the `bot_id` of an earlier import. `logins` lists model API keys and `sign_ins` lists OAuth providers, as `{id, label}` pairs. `model` is the default `{model, provider}` of the setup, or null. `mcp_servers` lists `{id, transport, needs_setup}` for each MCP server in the setup's `config.yaml`. A server needs setup when it signs in with OAuth or refers to a `${NAME}` variable that the setup's `.env` does not define. `hermes` is `{status, version, reason, tested}` for the Hermes that Coconut runs on. `status` is `ok`, `missing`, or `incompatible`, and `fixture` for a service in fixture mode. `reason` says what to fix, or is null. `tested` is true when CI tested this Hermes version, false for an untested version that passed the contract check, and null when `status` is not `ok`. See [Supported Hermes versions](supported-hermes-versions.md). The response contains no paths or secret values.

The content response advertises `sync_version: 1`. The client then applies contiguous update batches and retains their cursor.
Older servers ignore the content header. The client retains support for their full snapshots and event stream.
Snapshot data includes native settings read from Hermes. Native files and SQLite do not share one atomic transaction.
A disconnected client displays cached state and connection health separately from task state.

## Bot routes

- `POST /bots`: `{name, role?, avatar?, color?, instructions?, pinned?}` returns Bot.
- `POST /setup/import`: `{profile_ids, logins, mcp_servers}` returns `{bots, logins, skipped_skills, mcp_servers, mcp_needs_setup}`. Each new bot gets a copy of the profile's `SOUL.md`, memory files, and skills. A skill with a file over 5 MiB stays behind and appears in `skipped_skills`. A profile that was imported before returns its existing bot. With `logins: true`, Coconut adds the API keys that its default Hermes home does not have yet. It never replaces an existing key and never copies an OAuth sign-in. With `mcp_servers: true`, the native Hermes settings add each MCP server that the default Hermes home does not have, by name. An existing server never changes. Coconut copies each variable that a new server refers to and the setup's `.env` defines, and keeps the values that it already has. A server that refers to a missing variable, or signs in with OAuth, is added with `enabled: false` and appears in `mcp_needs_setup`. Coconut never copies MCP OAuth tokens. `mcp_servers` defaults to false, and the phone sets the default model through `PUT /settings/model`.
- `PATCH /bots/{id}` changes specified fields and returns Bot.
- `DELETE /bots/{id}` cancels work and removes the contact. It retains the deleted native profile and receipts.
- `POST /bots/{id}/read` clears unread state.
- `POST /bots/{id}/messages`: `{text, mode: "steer"|"queue", task_id?, scheduled_at?, attachment_ids?, recipient_bot_ids?}` returns Task.
- `POST /tasks/{id}/replies`: `{text, attachment_ids?, decision_id?, recipient_bot_ids?}` returns Task with `reply_status`.
- `POST /tasks/{id}/cancel` cancels the exchange and active descendants.

Bot fields include `id, profile_id, session_id, name, role, avatar, color, instructions, notifications, pinned, archived`.
Native settings add `model_override, effective_model, approval_override, effective_approval`.
`role` is an optional label. Empty `role` and `instructions` values clear those native fields.
The app submits only edited fields. An omitted setting remains unchanged.
Workflow policies and custom preference routes no longer exist.

## Native settings

- `PUT /settings/model`: `{model, provider, reasoning_effort?}` edits the active native default.
- `PUT /settings/approvals`: `{mode}` edits the active native default access mode.
- `PATCH /bots/{id}` accepts `model_override` and `approval_override`.

`model_override: null` makes the bot use the default model. `approval_override: null` removes the bot's approval mode, so Hermes asks for flagged actions.
A model override contains `{model, provider, reasoning_effort}`.
Null effort uses that model's native default. Effective values remain visible separately.
The service rejects model and effort choices outside the current Hermes catalog.

`approvals` contains `default` and native `modes`, each with `id, label, description`.
Native modes are `off`, `smart`, and `manual` in the tested Hermes revision.
`manual` asks for actions flagged by Hermes. It does not ask for every action.
Native changes use durable request intents. Retries read the result before repeating a write.

## Routines

- `POST /bots/{id}/routines`: `{name, prompt, cron, enabled?}` creates a native Hermes job.
- `PATCH /routines/{id}` changes native name, prompt, schedule, or enabled state.
- `DELETE /routines/{id}` removes the native job.
- `POST /routines/{id}/run` returns the current active task or claims a new native run.

`cron` carries a Hermes schedule string. Hermes parses and schedules it server-side.
A routine includes `id, bot_id, name, prompt, cron, timezone, enabled, next_run_at, last_run_at, last_task_id, error`.
The public ID derives from bot ID and native job ID. It does not depend on the host address.
An unsupported native job or uncertain dispatch returns a visible `error`.
A run that cannot obtain a native claim returns 409, not an empty Task.

## Decisions and attachments

- `POST /decisions/{id}/context`: `{question}` returns a persisted, linked question Message.
- `POST /decisions/{id}/answer`: `{revision?, choice?, option_id?, correction?}` returns Decision.
- `POST /attachments`: `{bot_id, filename, content_base64}` returns Artifact.

A decision includes its stable ID, task, proposed action, choices, recommendation, status, actor, time, and outcome.
Each option has `id, label, answer, explanation`. Native questions carry one to eight model-generated choices.
Native approvals carry the backend's allow-once and deny choices. The app does not construct these outcomes.
Context leaves the decision pending. Choosing an answer queues the continuation and preserves the decision in its transcript.
Completion follows structured native receipts, not assistant prose.
Inbox filters pending decisions only. Completed work remains in conversations.

Attachments contain at most 4 MB. A message can attach at most 20 files belonging to its bot.
Cancellation persists immediately. An already executing action can finish before its process stops.

## Pairing and providers

`POST /pair` accepts `{code, device_name}` and returns `{token, device_id}`.
Pairing uses an expiring one-time code and the pinned server certificate.
The device retains its token across connection loss. Reconnecting Tailscale does not require pairing again.
The server stores device token hashes. A lost pairing response requires explicit reconciliation or a fresh code.

The execution provider supports task start, resume, context, cancellation, and structured event delivery.
The host API supplies artifact retrieval. The phone does not consume raw Hermes stdout.
Native events include `native_call`, `native_result`, `native_approval`, and `native_question`.
Other events cover streaming text, activity, decisions, artifacts, bot routing, completion, and failure.
A `phase` event names what the running turn does now, for example `reading` or `testing`. It never stops a turn.
A working task carries `phase` and `turn_started_at`. The Mac removes both when the task stops working.
The Mac owns scheduling, pending decisions, and execution receipts independently of the phone.

## Message presentation

Message bodies contain Markdown. Clients render formatting without rewriting stored text.
`kind: "bot_exchange"` identifies a native message between bots.
`sender_bot_id` and `recipient_bot_ids` identify the participants.
`exchange_recipient_bot_ids` links an existing bot result to an exchange without duplicating its body.
Clients show that result in its author's chat and a compact exchange row in both chats.
The shared exchange view selects messages by the participant IDs across tasks.
It creates no session and accepts no input.
Delegated task updates use the executing bot's `bot_id`.
Explicit human mentions retain their original conversation and sender attribution.
The service migrates older message projections without modifying native Hermes history.
