# Coconut API v1

The app uses HTTPS with certificate pinning. Routes use `/v1` unless stated otherwise.
Authenticated requests use `Authorization: Bearer <device token>`.
Writes require `Idempotency-Key: UUID`. Reusing a key with different input returns 409.
Errors use `{error: string}`. UUIDs do not encode host names or filesystem paths.

## Read routes

- `GET /health` outside `/v1` reports service health.
- `GET /state` returns bots, tasks, messages, decisions, artifacts, routines, cursor, server time, model catalog, and approvals.
- `GET /state` with `X-Coconut-State: content` omits native settings and returns recent messages with history cursors.
- `GET /state/settings` returns native bot settings, model catalog, approvals, worker settings, MCP servers, and routines.
- `GET /updates?after=N&wait=20` returns changed tasks and messages, current decisions and Work, or a content reset.
- `GET /cards/{id}` returns the current decision, task, conversation bot, owner, and cursor.
- `GET /history/{bot_id}?before=N` returns an older message page, its referenced tasks, and the next history cursor.
- `GET /search?q=text` searches stored message text and returns matching bot IDs.
- `GET /events?after=N&wait=20` returns ordered events, a cursor, and server time.
- `GET /artifacts/{id}/content` returns authenticated artifact bytes.
- `GET /learning` returns agreement groups by bot and action type.

The content response advertises `sync_version: 1`. The client then applies contiguous update batches and retains their cursor.
Older servers ignore the content header. The client retains support for their full snapshots and event stream.
Snapshot data includes native settings read from Hermes. Native files and SQLite do not share one atomic transaction.
A disconnected client displays cached state and connection health separately from task state.

## Bot routes

- `POST /bots`: `{name, role?, avatar?, color?, instructions?, pinned?}` returns Bot.
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

`model_override: null` and `approval_override: null` restore inheritance.
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
