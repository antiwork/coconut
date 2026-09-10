# Reference audit

## Decision

Keep Coconut's native client and small local coordinator. Use OpenMausBot for interaction and transport patterns. Use OpenBot for action enforcement patterns.

OpenMausBot is a credible reuse candidate. Its phone design already meets many requirements. A claim that it lacks a real mobile client would be wrong.

The deciding gap is the Hermes recovery contract. Neither inspected project establishes the required persistent decision, context request, restart, approval, and duplicate prevention sequence.

This recommendation limits integration scope. It does not establish that a rewrite costs less than a fork.

## OpenMausBot client

The iOS documentation describes a SwiftUI client with a Foundation core and no external client dependencies. The Mac owns execution and durable state.

Its documented design includes native approval cards, compact activity, roster roles, and a sheet for work, decisions, and review. It already separates transcript state from streaming deltas.

The client holds an SSE cursor and requests replay after reconnecting. Failed replay triggers hydration. Actions wait for server events before changing state.

Pairing uses a short-lived QR credential and stores the resulting device token in Keychain. Cleartext LAN access remains available for chat; credential submission requires HTTPS or Tailscale.

The stream stops when the app enters the background. The documented release lacks APNs delivery after termination.

Phone routes deliberately limit general bot edits because those edits can change execution permissions. Coconut needs narrow phone routes for its broader management controls.

These are documented behaviors. Attempts to retrieve `ChatView.swift` and `Client.swift` returned cache misses, so this audit does not verify their implementation.

Source: [iOS README](https://github.com/milind-soni/OpenMausBot/tree/main/ios).

## OpenMausBot backend contract

The inspected `ProviderAdapter` interface exposes `sendTurn`, `interruptTurn`, `respondToRequest`, optional `steer`, and `onEvent`. Inputs carry provider continuation data, instructions, and working directories.

Structured events distinguish text, tool activity, requests, request outcomes, and turn completion. Steering depends on a declared capability.

`respondToRequest` returns an explicit outcome. A missing answerer or dead broker produces `unavailable`, which callers treat as refusal. This prevents false approval claims.

That interface does not itself specify durable pending decisions, action idempotency keys, reconciliation, artifact retrieval, or recovery of a dead broker's request.

A Hermes adapter could implement this interface. Generic adapter support does not prove compatible Hermes callbacks or durable continuation.

Source: [provider contracts](https://github.com/milind-soni/OpenMausBot/blob/main/server/contracts.ts).

The project lists Claude, Codex, Grok, generic ACP engines, and OpenAI-compatible endpoints. Its README does not list Hermes. Model selection appears per bot and within conversations.

Coconut places model controls in Settings. Removing the picker alone would be a small fork change; it is not a strong reason to reject reuse.

Source: [project README](https://github.com/milind-soni/OpenMausBot/blob/main/README.md).

## OpenBot policy and audit

The inspected gateway evaluates current policy, awaits the audit write, and checks the forwarding decision before executing. It records execution failures separately.

The gateway also rejects stale element references before dispatch. A policy grant therefore does not become proof of successful execution.

Source: [gateway implementation](https://github.com/CopilotKit/OpenBot/blob/main/server/src/computer/gateway.ts).

The policy code checks deny rules before allow rules. Invalid deny expressions deny; invalid allow expressions grant nothing. Missing policy denies in enforcement mode.

Its `dry-run` mode records refusals while forwarding actions. Coconut's Review mode must not inherit that meaning.

Source: [policy implementation](https://github.com/CopilotKit/OpenBot/blob/main/server/src/computer/policy.ts).

## OpenBot reuse limits

The architecture documents an explicit permissive startup policy: `deny: []` and `allow: ["true"]`. That differs from Coconut's review-first requirements.

A configured supervisor gives each bot a container, workspace, and browser profile. Without that provider, bots can share one computer. Separate chats do not guarantee isolation.

Browser activity disappears on reload; the durable audit remains separate. Coconut needs persistent transcript cards as well as audit records.

The documented `ask_person` tool applies to built-in agents. Remote AG-UI agents do not receive that callback path. A Hermes endpoint therefore needs more than protocol translation.

Source: [architecture](https://github.com/CopilotKit/OpenBot/blob/main/docs/architecture.md).

OpenBot uses React, Hono, PostgreSQL, Docker services, and CopilotKit Intelligence. Intelligence requires its own project and license, with self-hosting available.

That stack adds operating scope to this small local milestone. Its AG-UI extension point remains useful as a future reference.

Source: [project requirements](https://github.com/CopilotKit/OpenBot#requirements).

## Licenses and evidence

OpenMausBot uses [Apache-2.0](https://github.com/milind-soni/OpenMausBot/blob/main/LICENSE). OpenBot uses [MIT](https://github.com/CopilotKit/OpenBot/blob/main/LICENSE). This audit inspected both license files.

No upstream code was copied. Future copying must preserve the applicable license and notices. OpenBot's repository license does not replace its dependency licenses.

This audit used public pages and selected source code. It did not run either project or test Hermes through either project.

The pages follow `main`, without a pinned commit. Browser caches returned different file snapshots, so the findings establish design evidence rather than exact-head certification.

Coconut must prove its first milestone through real Hermes processes, backend restarts, and execution receipts. Reference documentation cannot supply that proof.
