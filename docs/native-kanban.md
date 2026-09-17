# Native Hermes dispatch

Coconut uses the installed Hermes Kanban kernel to claim queued requests, enforce capacity, and retain run history.
The adapter runs in the Hermes Python environment. Coconut does not modify Hermes source.
The adapter pins its database and resource paths under its own home. Inherited board settings cannot redirect it.

Coconut stores human input, Inbox decisions, tool receipts, and the visible work outcome.
The native board stores execution claims, worker PIDs, priority, and run results.
Coconut publishes input changes to the board. It starts a turn only after Hermes commits its claim.
A saved claim and generation identify the worker when a dispatch reply is lost.
A committed Coconut result remains available until Hermes records the finish.
A finish includes the native run ID. An old finish cannot close a newer run.

The `default` assignee identifies the adapter's delivery lane.
The task's tenant and body identify the real Coconut bot.
The adapter starts that bot's existing Hermes profile in a process dedicated to the work session.
A card maps to native `review`. Native review dispatch is disabled for this board because the human answers through Inbox.
Card replies reopen review through the native kernel. Duplicate phone requests retain their existing execution receipt.
A new request after completion creates a new native execution record and retains the previous result.

Coconut supplies readiness for application dependencies and scheduled requests.
Normal waits use native `scheduled` state and do not increase the native failure count.
Hermes owns selection and capacity after a request becomes ready.
Read-only status questions keep one separate native session so they can answer while work capacity is full.
Queue positions use native priority and creation order.
Work uses the native title and progress note. Changes to either field trigger a phone update.
Native failures enter Coconut's existing recovery path, which checks tool receipts before another action.
The adapter does not treat an unknown tool result as permission to repeat it.

## Workspaces

`coconut_workspace` calls the native Hermes workspace resolver.
It creates one Git worktree per outcome and repository.
The bot uses the returned directory for code changes and tests.
An existing linked worktree can be adopted once to retain earlier changes.
The registry rejects another outcome's worktree and rejects a changed branch.
The service keeps the registry under `workspaces/worktrees.json`.
Coconut retains these worktrees after a turn ends. It does not delete uncommitted work.
Native delegated workers also default to `delegation.worktree_isolation: true`.
An explicit profile setting takes precedence.

Each idle process retains its native session data on disk.
Coconut limits the idle cache and preserves runtimes with pending process receipts.
Coconut also checks the native process and browser registries before eviction.
Silent preview servers and live browser sessions keep their runtime.
Each worker uses a separate native process checkpoint. Cleanup recovers only that worker's checkpoint.
The adapter selects this checkpoint path because the pinned Hermes version otherwise shares one file per profile.
The bot keeps its shared Hermes config, memory, and session database.
Recovery keeps the outcome's execution ID and worktree when it replaces a request.
Each new turn applies the saved workspace through native task and session overrides before the agent starts.
A worktree isolates files. Tests still need separate database, Redis, and storage resources when they modify shared state.

## Validation

`test_native_kanban.py` checks capacity, review, duplicate answers, restart, and lost acknowledgements against the real kernel.
`test_work_workspace.py` checks separate branches, worktree adoption, and preservation of uncommitted changes.
`scripts/test_native_kanban.py` runs two real Hermes agents through Coconut against a local model fixture.
Each agent creates its worktree, writes a local file, waits for an Inbox answer, and records completion.
The deployment compatibility check and CI run this integration check.
No test sends messages, publishes changes, or uses a paid model.

## Replies

Coconut shows complete replies. Partial model output stays inside the worker until Hermes confirms the response.
The phone keeps its activity indicator visible while it waits. Events still deliver complete replies without a full reload.
