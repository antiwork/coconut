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

Hermes prevents automatic dispatch when a recent task comment links to a PR.
For new accepted input, Coconut claims that existing task through the native kernel after checking worker capacity and the other native guards.
The adapter records the input so an interrupted follow-up can retry without creating another task or workspace.
Pending cards and paused work remain ineligible. Failed follow-ups retain the normal native retry path.
A new request after completion creates a new native execution record and retains the previous result.

Coconut supplies readiness for application dependencies and scheduled requests.
Application waits use native `scheduled` state and do not increase the native failure count.
Explicit native blocks keep their reason and continuation eligibility across ticks;
native dependency waits retain `todo` until their parents finish.
Hermes owns selection and capacity after a request becomes ready.
Direct chats use separate Hermes sessions and share the configured host capacity.
One additional slot remains available for a direct reply when Work fills that capacity.
Independent bot chats can run together. Calls within the same session remain serial.
Queue positions use native priority and creation order.
Work uses the native title and progress note. Changes to either field trigger a phone update.
Native failures enter Coconut's existing recovery path, which checks tool receipts before another action.
A native outcome blocked for a missing resource or input gets an owner check after ten minutes, then on a backoff of up to six hours.
The check continues the work if the block cleared. Otherwise it asks the person with a card or schedules a recheck.
A pending card or a recorded pause on the outcome stops further checks. A declined card becomes a recorded pause.
The adapter does not treat an unknown tool result as permission to repeat it.

Coconut parks native retries that have no ready application input after native
reclaim and dependency promotion, before claiming ready tasks. Native scheduling
releases those retries without starting a worker or recording a false spawn
failure. Startup loads native bindings before accepting task replies.

## Workspaces

`coconut_workspace` calls the native Hermes workspace resolver.
It creates one Git worktree per outcome and repository.
The bot uses the returned directory for code changes and tests.
An existing linked worktree can be adopted once to retain earlier changes.
The registry rejects another outcome's worktree and an unverified branch switch.
A Git-confirmed branch rename keeps the same owner and files. Coconut updates its saved branch without a service restart.
The service keeps the registry under `workspaces/worktrees.json`.
Coconut retains these worktrees after a turn ends. It does not delete uncommitted work.

Once an hour, the work source poller retires the worktrees of idle outcomes.
An outcome is idle when no Coconut task of its execution is queued, working, scheduled, or waiting.
It also has no running worker and no pending background process.
Its native task, if one exists, is done, archived, blocked, in triage, in review, todo, or scheduled.
Its tasks have had no update for `WORKTREE_RETIRE_SECONDS` (six hours).
A failed outcome, a pending decision, and a blocked native task do not keep the worktree.
A turn restores the worktree before it starts, so work that resumes gets its files back.
The poller holds the service lock only to read this state. It runs Git outside the service lock.
Retirement does these steps:

1. It validates the binding and the Git worktree record.
2. It skips a locked worktree and a Git operation in progress.
   A bot can create worktrees inside its own worktree, for example to test a base commit.
   After the outcome and process checks, Coconut removes each such worktree with `git worktree remove`.
   It removes one only when Coconut does not track it, it is not locked, it has no change, no ignored file
   outside the rebuildable folders, and no Git operation in progress, and a branch or another ref holds its commit.
   Otherwise it skips the parent. A nested record whose folder is gone does not count.
3. It skips a worktree when `lsof` shows a process cwd or an open file in it, or when `lsof` is not available.
   Before this check, it stops the orphaned processes of the outcome that hold the worktree, and their child processes.
   An orphan is a process that launchd (PID 1) adopted, and that is older than `ORPHAN_AGE_SECONDS` (six hours).
   A service restart leaves such processes from earlier turns. launchd also starts apps and agents,
   so Coconut stops only an orphan whose environment has `COCONUT_WORKSPACE_OUTCOME` set to the outcome.
   It also stops the child processes of that orphan, because their parent proves who owns them.
   The worker of each turn sets this variable. If the system hides the environment of a process, Coconut does not stop it.
   macOS hides it for its platform binaries, such as `/bin/sleep`.
   It sends `SIGTERM`, waits at most 5 seconds, and then sends `SIGKILL`.
4. It writes `workspaces/archive/<registry key>.tar.gz` with mode 0600. The archive contains these items:
   - `staged.patch` from `git diff --binary --cached HEAD`.
   - `unstaged.patch` from `git diff --binary`.
   - The untracked files that Git does not ignore, under `untracked/`.
   - The ignored files, under `ignored/`. Rebuildable folders are not in the archive:
     `node_modules` and `.cache` at any depth, and `tmp`, `log`, `public/packs`, `public/packs-test`, and `coverage` at the top.
   - `retirement.json` with the branch, the HEAD commit, the outcome, the repository, and the time.

   The archive stores a hard link as a separate regular file.
5. It skips the worktree when the archived ignored files are larger than 200 MB.
6. It reads the archive again and compares it with the worktree.
7. It checks the outcome state again. It waits at most 5 seconds for the service lock. It checks `lsof` again.
8. It takes `workspaces/.workspace.lock`. It checks the binding, the Git worktree record, the HEAD commit, and the changes again.
   It checks the outcome state again. It waits at most a quarter second for the service lock here.
9. It marks the binding `retired` with the time, the archive, and the HEAD commit.
   It runs `git worktree remove --force` with a 600-second limit. It keeps the branch and its commits.
10. It deletes the other archives of the same worktree that no binding names.

The log names the cause when Coconut does not retire a worktree, for example a busy service or a changed worktree.
Steps 1 to 7 do not hold the registry lock. Step 8 runs Git commands that do not wait for other processes.
A path hint for native dispatch reads `worktrees.json` without the registry lock, because dispatch holds the service lock.
The provider's verified lookup runs before a turn without the service lock. It can wait for the registry lock.
Each hourly run first deletes the unfinished `.*.tar.gz.tmp` files in `workspaces/archive`.

When `git worktree remove` fails or stops at its time limit, Coconut examines the path:

- If the path is gone, the retirement is complete. Coconut runs `git worktree prune`.
- If the checkout is unchanged, Coconut keeps the new archive and clears `retired`.
  Git can delete ignored files before it stops, so the archive stays. Coconut marks the binding `retire_failed`
  with the time, the error, and the archive path, and it does not try again for one day.
- If part of the checkout remains, Coconut keeps `retired` and the archive.
  It marks the binding `retired.restore_failed` with a message for the operator.

`coconut_workspace` and the provider's verified workspace lookup restore a retired worktree on demand.
The restore runs `git worktree add` at the same path on the saved branch.
It applies `staged.patch` with `git apply --index` and then `unstaged.patch` with `git apply`.
It extracts the untracked and ignored files. It recreates archived links with `os.symlink`, after all files, and does not follow them.
It checks the HEAD commit and the changes against the archive before it clears `retired`.
Then the worktree holds every archived change, so Coconut deletes the archive.
If the restore fails or stops at its time limit, Coconut removes the partial checkout.
It runs `git worktree remove --force`, deletes the remaining path, and runs `git worktree prune`. It keeps the archive.
It marks the binding `retired.restore_failed` with the time and the error, and does not try the restore again.
New work for the outcome continues without the worktree. `coconut_workspace` returns an error with the archive path and the branch.
To try the restore again, an operator removes `restore_failed` from the binding in `workspaces/worktrees.json`.
A path hint for native dispatch returns the saved path. It does not restore the worktree.
Coconut does not delete an archive that a `retired` binding names, also when the binding has `restore_failed`.
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

## Operator workspace recovery

A paired device can POST `/v1/maintenance/workspace-recovery` with its existing
Bearer authentication and a UUID `Idempotency-Key`. This is a managed-service
operator action, available before the blocked worker starts. It is not a bot tool
and the restart tool still accepts no arguments. The exact JSON fields are:

```json
{
  "task_id": "existing-coconut-task-uuid",
  "native_task_id": "t_existing",
  "expected_owner": "retained-execution-uuid",
  "repository": "/canonical/repository",
  "path": "/canonical/repository/.worktrees/existing-worktree",
  "old_branch": "saved-branch",
  "new_branch": "verified-current-branch"
}
```

The device authorizes only this binding transition for the existing task. The
service verifies its execution identity and native binding, canonical paths,
registered linked worktree and actual branch. It saves a durable request before
draining. No new native, chat or context turn starts during the drain. Existing
turns may finish. This recovery drain is not released by the ordinary restart
tool's 60-second dispatch pause limit. Before the managed exit it checks native claims, runtime
resources, background receipts, process checkpoints, delegation ledgers and
unaccounted workers. Active, reused or ambiguous PIDs or process groups cause a
held receipt. A dead checkpoint wrapper is insufficient: its group must also be
absent. Recorded systemd scopes remain held for inspection because this path
cannot prove their descendants have stopped. No writer is killed to make the
repair possible. Unmanaged editors still require operator exclusion.

Startup applies the request under `service.lock`, before native recovery or
dispatch, and repeats the writer and ownership checks. Under the workspace lock
it compares the authorized binding with its saved value, verifies the current Git branch, and
atomically replaces only the authorized binding's branch and recovery marker.
A change to another binding, for example a worktree retirement, does not hold the request.
It fsyncs both file and directory. A crash before replacement retains the old
binding. A crash after replacement recognizes the exact recorded result including
its request marker. A different task input or a newer value of the binding is held;
it is never overwritten. Git contents, approvals and execution ownership stay
unchanged.

Repeat the exact POST with the same request key to read the durable receipt:
`draining`, `applied`, `held`, or `superseded`. Reusing a key for different scope
is rejected. A held recovery keeps dispatch stopped while its checks fail;
startup repeats the checks using the same exact authorization. After inspecting
and resolving the reported writer or stale-state issue, a new
device request with a new key can authorize a fresh transition. A successful
receipt supplies `/v1/tasks/{task_id}/replies`. Send the intended continuation
there using normal device authentication and a new idempotency key. Pending
native approvals must still be answered through their existing decision routes.
An unfinished native outcome resumes in place. Terminal outcomes use the existing
native successor route, including legacy PR owners without `_work_context_id`.

## Validation

`test_native_kanban.py` checks capacity, review, duplicate answers, restart, and lost acknowledgements against the real kernel.
`test_work_workspace.py` checks separate branches, worktree adoption, and preservation of uncommitted changes.
`test_worktree_retirement.py` checks which outcomes retire, the archive contents, and restore after retirement.
`test_workspace_recovery.py` exercises the authenticated API, managed drain,
startup lock and crash replay, writer refusals, native continuation, and the real
provider's preferred workspace using a disposable local model server.
`scripts/test_native_kanban.py` runs two real Hermes agents through Coconut against a local model fixture.
Each agent creates its worktree, writes a local file, waits for an Inbox answer, and records completion.
The deployment compatibility check and CI run this integration check.
No test sends messages, publishes changes, or uses a paid model.

## Replies

Coconut shows complete replies. Partial model output stays inside the worker until Hermes confirms the response.
The phone keeps its activity indicator visible while it waits. Events still deliver complete replies without a full reload.

## Completed history

Native tasks supply current Work state. Retained completed reports pass the existing completion checks once during migration.
Coconut then keeps their verified result as history. It does not reconstruct their conversation chains on each phone read.
A changed report invalidates this imported result. An unfinished retained report keeps its decisions and receipts during native migration.
PR closure and explicit cancellation remain authoritative facts. Missing native state never implies completion.
Content snapshots decode only active tasks and tasks referenced by visible records.


Pending cards follow source changes
---------------------------------

The existing GitHub reader observes the issues and PRs linked to pending cards.
It reuses PR observations within each bot profile. It never borrows another profile's credentials.
A changed source, review, task result, or human message can queue one card check.
An unchanged card does not start a model merely because it is old.
Failed checks retry after the existing delay. Source failures retain the last known facts.
The check cannot approve a choice. Human replies and current card revisions take priority.
Other sources still use the configured intake routines and their recorded evidence.

## Archived Work with divergent checkout histories

`POST /v1/maintenance/checkout-recovery` is a separate paired-device, managed-service
operation for an **idle archived native Work owner** whose saved branch and attached
branch diverged. It requires a UUID `Idempotency-Key`, and exactly these JSON strings:
`task_id`, `native_task_id`, `expected_owner`, `repository`, `path`, `saved_branch`,
`attached_branch`, `saved_head`, `attached_head`, `recovery_branch`, `message`.
The recovery branch must be `wt/recovered-` followed by the request UUID. The
message is the operator's authenticated continuation input, not an approval.

The request verifies the one original Work owner, its native archived binding,
branch heads and exact worktree, then saves a durable receipt before draining.
Before any Git write at startup it verifies native/background quiescence, unchanged
owner, Work, heads, binding and dirty tracked file contents. Git merges the two
heads into a **new local-only two-parent commit**; conflicts, staged or unmerged
changes, custom merge drivers, disabled Git file-mode tracking, or paths that
might overwrite dirty/untracked/ignored files hold admission for manual inspection.
Changed-tree path obstructions and checkout filters are rechecked
immediately before switching with `--no-overwrite-ignore`; the repair disables
repository checkout hooks. The original refs are never moved. The new branch is switched
in the same worktree and the manifest is updated only after verifying its checkout
and dirty bytes. A crash after creating the branch, switching, or replacing the
manifest replays against the recorded commit and exact input without repeating the
switch or dropping edits. No Git operation publishes or pushes.

While dispatch remains fenced, the service submits the paired device's message
through the normal archived Work reply lifecycle, creating a linked native
successor at reconciliation rather than reopening the immutable archived result.
The receipt is `applied` only after the successor has been committed; it contains
`successor_task_id` and `resume_path`. Pending decisions, foreign Work, changed
inputs or unprovable writers hold the request. Do not send a second continuation
for the same incident. As with other workspace recovery, unrelated shell writers
and editors must be excluded by the operator; the service cannot fence arbitrary
processes outside Coconut's native lifecycle and workspace lock.
If a held checkout has not changed its original branch, heads, dirty tracked
files or manifest and has not created its recovery ref, the same paired device
may submit a fresh authorization for the same incident with a new UUID and
recovery branch; the held receipt is marked `superseded` only after the new
request validates. A partially applied checkout instead replays its recorded request
or stays held for manual inspection. Staged edits, merge conflicts and Git
versions without `merge-tree --write-tree` require separate manual resolution.

## Native sessions

Each bot keeps one open chat session. Hermes compression continues it in a new session, and resume follows that
chain. After each turn, Coconut records the newest session in Hermes `state_meta` (`coconut.session-tip.<root>`).
The next turn starts from it, so a prune of older ended sessions in the chain does not break the chat.

Each outcome has its own work session. Once an hour, with worktree retirement, Coconut ends the newest session of
each idle outcome with Hermes `end_session` and the reason `coconut_idle`. It never ends a chat session. Then it runs
Hermes `maybe_auto_prune_and_vacuum` for each bot with `SESSION_RETENTION_DAYS` (7). Hermes deletes only ended
sessions that were inactive that long, runs at most once a day, and vacuums only when enough space is free.

When an idle outcome resumes, Coconut reopens its session with `reopen_session`. If Hermes has already deleted it,
Coconut seeds a new session from its own saved messages of that work. The native card keeps the work record.
Coconut does not change Hermes. It uses only these `SessionDB` methods, which the Hermes contract check requires.
