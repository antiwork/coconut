# Coconut reassessment: deployed results

Date: September 18, 2026.

Hermes owns durable outcomes. Coconut presents those outcomes, delivers messages, and records human choices.
The Work tab remains. Active Work reads native task state instead of reconstructing status from conversation history.
Hermes source remains unchanged at `345cd2b057a452236de401d3534b8502a7465e8d` (`v2026.9.14`).

## Deployed changes

Each code stage passed focused tests and a clean autoreview before publication.
The supported installer deployed each runtime stage to the Mac mini.
The recovery deployment, `ecba9ae`, passed the authenticated HTTPS health check with the exact server certificate.

| Change | Result | Commit or final correction |
| --- | --- | --- |
| Native task ownership | Native outcomes retain completion, dependencies, the original execution, and the workspace. Conversation completion does not complete an outcome. | `c487e64`, `94212f7`, `cb20c72` |
| Concurrent conversations | Independent sessions share available host capacity. Each session remains serial. A direct reply has one reserved slot. | `b30f080` |
| Compact updates | Idle updates omit unchanged Work. Native changes use durable state and cursors. | `1554c15` |
| Retained history | Completed reports use verified saved results. Changed report content invalidates that result. Active reads exclude unrelated history. | `c7dc805`, `701ea9b` |
| Workspace continuity | A verified branch rename preserves the original workspace without a service restart. A checkout change still requires verification. | `0a606ed` |
| Current decisions | Changed GitHub facts trigger reassessment. An unchanged card's age alone does not trigger a model call. | `c64edfc` |
| Recent phone state | Initial state includes pending and recently referenced decisions. Older history remains available through history reads. | `952d9fa` |
| Short reconnect reads | Short reads use separate 4-second request and 6-second resource timeouts. Long requests retain their separate limits. | `952d9fa` |
| PR ownership | Remaining active PR reports migrate to one native owner. Original waits, workers, and return routes remain intact. | `e433183` |
| Direct merge decisions | The implementing worker can present its verified merge card directly. Desk no longer needs an extra turn solely to present it. | `be660e9` |
| Response timing | Logs distinguish queue time, worker preparation, first worker output, completed output, and reply creation. iOS logs reply application. | `e8846f7` |
| Bounded reply context | Reply preparation reads recent matching progress, pending decisions, and relevant deliveries. | `45e9449` |
| Final PR results | A newly merged or closed PR wakes its existing idle owner once. The worker verifies the accepted outcome. | `13bf809` |
| Recovery after publication | A saved recovery resumes the same native owner and input even when an existing PR blocks duplicate dispatch. Authentication and quota guards remain. | `ecba9ae` |
| One visible Work row | Reports for the same outcome share a canonical identity. The phone groups aliases and linked PRs into one row. | `5fad4ef` |

The private Desk routine also stops duplicating the deterministic assigned-issue search.
It retains its other intake duties, source checks, and installation-specific routing.
No Codex monitoring automation was created.

Native tasks no longer receive `coconut_track_work` as a second lifecycle tool.
The compatibility path remains for older retained conversations and unfinished migration cases.
Completed history remains readable without recurring reconstruction of its task chains.

## Performance

The [recorded samples](coconut-reassessment-metrics.json) contain four reads per measurement in each cohort.
Requests use authenticated HTTPS from the Mac mini to its configured Tailscale address.
The after cohort uses the new recent-state contract and update protocol 4.

| Measurement | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| Initial read, median | 734 ms | 223 ms | 70% |
| Initial response | 2,278,751 bytes | 957,440 bytes | 58% |
| Idle update, median | 437 ms | 16 ms | 96% |
| Idle response | 536,896 bytes | 157 bytes | 99.97% |
| Unchanged artifacts per idle response | 282 | 0 | 100% |

The new payload contract directly removes bytes and reconstruction work.
Elapsed-time changes also reflect host load and cache state. Four samples do not establish a reliable p95.
These results do not measure the physical phone, mobile Tailscale routing, or complete bot response time.

The latest 20 startup records show a 670 ms median for service preparation.
The proposal's earlier 100 records showed 2,519 ms. These are different workloads, so the comparison shows correlation only.
Provider preparation remains about 306 ms in the latest cohort.

Worker preparation still has outliers. MCP discovery reaches 24 seconds in the latest 20-record cohort.
Separate native discovery probes return zero Gumclaw tools after approximately 8–12 seconds.
HTTP probes observe a 403 response with code 1010 and a 502 response with an explicit client identifier.
This establishes an external connection failure. It does not prove that the credentials are wrong.

The native lazy-discovery option does not improve those probes because Gumclaw's schema cache has a zero lifetime.
No configuration override extends that lifetime. No Hermes source change hides the failure.

Recent completed worker traces show first output after approximately 3–19 seconds and total turns of 14–176 seconds.
Those turns include model reasoning and tool work. First output is not necessarily the first model token.
No controlled same-context comparison with the native public run API or physical-phone timing is claimed.

## Live outcome audit

The audit checks native records and current GitHub state. It does not treat a bot's final reply as completion evidence.

- Provider disclosure: the original worker verifies merged PR #7785. Native task `t_eae8b2d6` is `done`.
- India payout warning: PR #7791 is merged. Its original native task, `t_e1baee85`, is `done`.
- X reconnection: PR #7789 receives a new push at `2a104792`. Required jobs are still running.
  After deployment, original task `t_afbef2d0` resumes automatically as native run `3534`, with an active process and heartbeat.
  The audit sends no additional prompt to resume that worker.
- Public review identity: the recorded conflicting-edit pause remains. Its native title now matches the saved accepted outcome.
- Checkout wording: the implementation remains archived after its recorded stop. The dependent publication task remains blocked.

The active work audit observes two independent native workers running together before PR #7791 completes.
The host's automatic outcome capacity is five. No measured resource problem justifies increasing it here.
The queue is not empty: unfinished review work and explicit pauses remain.
No merge action or permission was fabricated to reduce the count.

## Boundaries retained after review

The installed native API exposes runs, events, approvals, stopping, steering, and sessions.
Its run launch does not expose Coconut's complete workspace, profile environment, native claim, and receipt contract.
Replacing the worker adapter now would remove required isolation and delivery behavior.
The narrow adapter remains until an equivalent native interface passes those contracts.

Sources: [native run launch](https://github.com/NousResearch/hermes-agent/blob/345cd2b057a452236de401d3534b8502a7465e8d/gateway/platforms/api_server_runs.py), [native capabilities](https://github.com/NousResearch/hermes-agent/blob/345cd2b057a452236de401d3534b8502a7465e8d/gateway/platforms/api_server.py).

Verified branch renames no longer require global recovery.
An unverified checkout change still needs the supported recovery boundary and proof that writers have stopped.
An arbitrary shell process can write outside its assigned workspace. A per-task pause alone cannot prove exclusive access.

Non-GitHub decision sources still depend on their configured native routines and source evidence.
iOS suspension still requires reconnection. Shorter reads and durable delivery do not guarantee an uninterrupted VPN route.
Gumclaw's external connection failure remains unresolved in this installation.

## Validation and release

Focused suites cover native ownership, callbacks, decisions, unknown results, cancellations, workspaces, source freshness, and synchronization.
Important stage results include 235 migration checks, 330 bounded-read checks, 167 source-freshness checks, and 124 native-readiness checks.
These suites overlap. Their counts must not be added as distinct tests.

The final PR-result stage passes 132 combined checks before the snapshot guard, then 49 focused checks after that guard.
The Work alias stage passes its native integration regression and 13 iOS tests.
The independent-result suite passes 100 checks after correcting a proven stale test expectation.
The corrected test preserves the visible failed result while allowing an independent result to finish.
The final recovery stage passes all 85 native task tests. Its regression fails before the fix.
The regression preserves authentication blocking and rejects an unmarked task. The final autoreview reports no findings.

Each behavior regression fails against the preceding implementation.
Autoreview accepts one PR-result finding: an absent native snapshot must not permit a completed outcome to resume.
The corrected patch passes review. The Work alias and test-only stages also pass review.

Review command: `autoreview --mode local`, with stage-specific context, output, and JSON files.
Earlier committed stages use `autoreview --mode branch --base origin/main`.
Documentation-only closeout skips autoreview because it changes no executable behavior.

The iOS release uses production notifications and the existing private TestFlight group.
Build 159 contains the compact synchronization, short-read, timing, and Work alias changes.
Apple marks build 159 as `VALID`. The existing private TestFlight group includes it.
Its App Store Connect build ID is `0f2d2000-6c35-427a-80ea-9d386e1e31a5`.
The later backend recovery fix requires no additional iOS build.

## Further system simplification

The second pass changes the codebase and its ownership rules. It does not add another monitoring agent.

| Change | Removed behavior | Commit |
| --- | --- | --- |
| Native source links | Native bots no longer maintain a second status file. Source checks derive ownership from native tasks and confirmed receipts. | `d1e921c` |
| Current Work descriptions | Old completion summaries no longer become current progress. Native dependencies and block reasons determine the label. | `ac8fd17` |
| Explicit PR deliverables | A referenced PR no longer becomes the current task's deliverable automatically. Native PR tools no longer accept duplicate progress state. | `cf04faf` |
| Scoped Work updates | One task change no longer projects every unrelated Work row. Ready rows remain included when queue positions can change. | `8ae2555` |
| Saved revision compatibility | Null timestamps in older source links no longer stop assignment checks. A valid saved revision remains intact. | `e9dcc59` |
| Consistent worker instructions | Native workers no longer receive directions to write the removed PR activity fields. Repeated tracking instructions are removed. | `ed4fbc1` |

Hermes still owns execution, retries, dependencies, and completion. Coconut retains message delivery, human decisions, source links, and display state.
Source links record identity and source revision. They contain no second lifecycle status.
The older provider path remains for retained work. Removing that path now would discard unfinished records.
No Hermes source changes are required.

The native task snapshot expires phase descriptions after 120 seconds without a phase update.
A running task then displays `Waiting for worker output`. Reading Work does not renew the observation time.
This label does not prove that a worker is stuck. Hermes remains responsible for worker execution.

The scoped-update regression sends one affected row instead of two rows when the other task remains blocked.
It also verifies that ready rows refresh when queue positions can change.
This measures payload selection in the integration suite. It is not a new latency benchmark.
Structural reads still load the stored artifact collection. No claim of bounded database reads is made for this change.

The deployment audit found a null revision in existing source records. That record exposed a compatibility bug in the first stage.
The added regression reproduces the failure. The correction preserves valid source links and restores live assignment checks.
At 22:34 UTC, the Mac mini records a successful source check with 54 native source links and no error.
Three changed assignments still require reconciliation. A recorded source gap does not prove that its native work is absent.

The audit clears two incorrect links from Coconut PR #6 to the checkout wording outcome through the authenticated API.
It preserves the PR records and all native execution state.
The checkout implementation remains stopped. The public review identity task retains its recorded pause.
The X follow-up has draft PR #7792. Its required CI passes, but Greptile reports an unresolved finding.
The original native owner resumes automatically during verification. No additional recovery prompt or merge card is sent.
Two native tasks run concurrently in the observed sample. The configured automatic capacity remains five.

Validation includes 39 scoped-update checks, 29 source checks, 88 PR and instruction checks, and 104 native integration checks.
These suites overlap. Their counts must not be added as distinct tests.
The behavioral regressions fail against the preceding implementation. Each executable stage passes a clean autoreview.
These backend changes use the existing iOS contract. They do not require another TestFlight build.
