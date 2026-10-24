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
The check can also record an answered card of a delegate that the outcome waits for. The outcome's own execution must
have linked its native task to the delegate's native outcome, the board must still have that edge, and the service must
have delivered the delegate's request from the same bot through an audited native relay. The relay's intake can own
that outcome, or it can have created it with kanban_create. Then adoption gave the outcome its own owner, and the
intake's own creation receipt must name that outcome. Only cards of the outcome's own execution count. The check shows
only the public answer fields of that card, including a typed reply. The pause ends when the answer changes, the link,
creation, or relay proof no longer holds, or the person gives new direction in the checked, delegate, intake, or
requesting execution.
Hermes moves a task to triage when it blocks again with the same kind. If the check turn itself ended in triage, Coconut queues no further check.
The task waits for the person, and Work shows it as paused. A message from the person starts a new turn, and the next block of that turn gets a check.
An answer to a card resumes the check turn, so a block after that answer also waits for a message.
The adapter does not treat an unknown tool result as permission to repeat it.

Coconut parks native retries that have no ready application input after native
reclaim and dependency promotion, before claiming ready tasks. Native scheduling
releases those retries without starting a worker or recording a false spawn
failure. Startup loads native bindings before accepting task replies.

## Failed routine continuations

An authenticated `coconut_continue_work` delivery records the exact input it authorizes.
A failed routine can use that input without changing the original native failure or firing the routine again.
While this continuation is queued or working, scheduled and manual runs reuse or wait for it, including gaps between tools.
Individual tools retain their native reservation fence without holding native locks across service calls.

A later input, including one created by `_native_retry`, cannot reuse that delivery.
A saved delivery from before input binding was added does not authorize a failed routine continuation.
The owner or requesting bot must send a new explicit `coconut_continue_work` message through the supported route.
Replaying the old message returns its saved receipt and does not authorize another input.
No migration changes the historical delivery or the failed occurrence.

The settle pass archives the native task of a failed occurrence (see the disagreement report below). That occurrence can still continue.
Hermes must have confirmed the failure of this job and owner, and the occurrence must have no Stop, no open input,
decision, background process, or unknown tool call, and no running worker.
The archived task must be the one that Coconut created for the request, in its tenant, ended without success by the run of its claim,
with no run or worker left. Its last activity must be less than seven days ago, so an idle archive from the phone stays final.
Coconut reads the runs with the adapter's `completion` operation. An adapter that does not answer admits nothing.
When a continuation fails again, the settle pass archives its successor too. That successor continues in the same way.
The reconciler cancels a request when Hermes archives the task of its accepted input. It keeps such a settled routine failure
failed instead, because no person stopped it. It checks this without the adapter, while the archive is new, and the task keeps the answer.
Hermes keeps the archived task and gives the new input a successor in the same execution and workspace.
The continuation proof then names the successor, but only while the successor's key names this request and this input.
Every other archived task stays final.

A continuation of a successful occurrence can also end with `kanban_schedule`, when its work is not finished.
Hermes ends the run as scheduled, and nothing re-gates the card, so the task completes. The settle pass archives the card a day later.
That card can still continue in the same execution. Hermes must have confirmed the success of this job and owner,
and the original card of the occurrence stays as it is. The archived card must be a successor whose key names this request
and an input of an authenticated `coconut_continue_work` delivery. It must hold the current input, which is authenticated too.
Its last run must be the run of that input's claim, ended as scheduled, and no run may have completed.
The other guards of a settled failure hold too. A completed task keeps its status when its card is archived, so the reconciler
does not cancel it. The new input gets a successor of the archived card in the same execution and workspace, as above.

An ordinary request, such as a delegated assignment, has the same gap. Its continuation can park its successor card with
`kanban_schedule`, the task completes, and the settle pass archives the card. That card can continue under the same rules,
without the routine ones. The request must have no routine record at all: no job, occurrence, completion receipt, or routine
trigger. Even an empty routine field counts, because only the routine code writes them. Its completed input proves nothing
more, so the authenticated continuations and the scheduled run carry the proof.
No person may ever have stopped the request. The owner or its requesting bot continues it, and the result keeps its route.
Pending decisions still hold the continuation. This covers a decision in the same execution, and a decision of another
execution that now holds the outcome's Work row. Continue that owner instead.
When an execution also has undelivered dependencies, Coconut reports its pending decision before its background work.
The refusal leaves the decision, dependency receipts, and execution unchanged.

The worker of a relayed request can also park the request's original card, the one under `coconut:<id>`, before any
continuation. Later input reuses that card while it is not done or archived, so the task can complete with the card still
scheduled, and the settle pass archives it. That card continues under the same rules. The key proves nothing alone, so
Coconut's records must prove the rest. Another bot relayed the request through its native envelope: the task holds that
envelope and that bot's message first, Coconut created the task once, and the first delivery of the task is that message
from the requesting task. A return callback does not count. The current input has its own proof: in that turn, the bridge
saved a completed `kanban_schedule` call on the card, and Hermes reported that the run of the claim scheduled the card.
A routine occurrence's original card stays final, as above. The new input gets a successor of the original card in the
same execution. The owner's turn restores its retired or discarded worktree before it starts, as any turn does.

A failed ordinary request has the gap too. Its owner or requesting bot can continue it while Hermes holds its task in
triage or blocked, and the settle pass then archives that task. The archived task still continues, under the rules of
a settled failure without the routine ones. The request has no routine record and no Stop, and Coconut created it once.
Its claim names the archived task and the current input, Coconut started the claim's run, and that run is the last one,
ended without success. No run completed, and no tool call is unknown. The PR facts or background result that the failed
turn never received go to the retry, and any other open input holds it.
When a continuation fails again, the settle pass archives its successor too. The reconciler keeps that failure, as it
does for a routine, when Coconut's records and the snapshot pass the same checks. It reads no adapter under the service
lock. The next continuation reads the successor's runs from the adapter before it starts.
A request without a retained native outcome gets the successor `coconut:<id>:<input>` for new input. Hermes retries a
provider stop (a rate or billing limit, an overload, a timeout, or a server error) of a card that Coconut owns. This
card is not owned, so the stop blocks it, and the settle pass archives it. That card continues under the same rules,
only when its key names the claim's input, the provider stop is the request's failure, and its last run ended blocked.
Any other failure of such a card stays final.

An open assignment can wait for the person. Its label is in `awaiting_labels` of the source (default `awaiting-human`), and its latest comment comes from another GitHub account.
The audit reads the latest comment of each labeled item with one GraphQL request. Only a card covers such an item: a pending card that names it, or an answer or dismissal that is not older than its latest change.
Bot work, a delegated ruling, or a scheduled recheck does not cover it. The audit tells the bot to ask with the source's own choices, and to post the answer to the source.
When the GraphQL read fails, the audit uses its earlier rules.

The work source audit records the source item that it gives a task in `_source_assignment`. A later audit of the same item
runs in a separate execution, and it can ask the person about that item while the request's card waits archived.
So this recovery also waits while the same bot has a pending decision about the exact same `source_id` and item `id`.
A card belongs to the item of its task's source work. That is the task's own source assignment, or the nearest one that it
continues: a turn that carries out a proposal of the audit that the person answered, or a task that the audit assigned.
The audit's answered cards use the same rule. A plain parent link does not count, because a person's reply has one too.
The request finds its own item the same way. Coconut compares the recorded values only. A link in a card, another item,
another source, another bot, a missing task, or a loop does not hold it. A malformed source assignment of the request holds it,
and so does a card from the oldest audits, which recorded the item without its source. The refusal changes nothing, and the
same continuation proceeds after the person answers. Coconut never answers that decision itself.

## Independent technical work

A pending decision normally holds every continuation of its execution. An owner can still need clear, already-authorized
technical work while a separate human choice waits, for example a CI fix while a visual review waits.
The owner or its requesting bot then adds `independent_technical_work` to `coconut_continue_work`:

```json
{"task_id": "t_26513095", "message": "Fix the CI findings locally.",
 "independent_technical_work": {"decision_id": "<pending card ID>", "decision_revision": 2,
                                "scope": ["diagnosis", "tests", "local_fixes"],
                                "authorization_decision_id": "<approved card ID>"}}
```

Coconut admits it only when the native outcome is in review with no run, its owner waits on exactly that pending question
at that revision, and the question holds no tool, merge, draft, or human answer. A person's refusal in the execution, a
Work pause, an unknown tool result, an open card request, active or uncertain native dispatch, a running worker, a foreign
bot, and an earlier conversation still refuse it. `local_fixes`, and only `local_fixes`, needs
`authorization_decision_id`: a native question of the same execution that a person approved with a selected option.
Coconut checks its recorded answer and gives the bot that exact question, choice, and sources as the whole fix scope.
A native approval, a file card, a merge question, a card of another execution, or a requester's message cannot authorize
product changes, and `diagnosis` or `tests` grant none. The continuation keeps the owner, session, worktree,
dependencies, and result route, and it leaves the question pending.

While the person has not answered, the service holds the native actions that Coconut can enforce: Kanban writes other
than notes on the owner's own task, bot messages, routines, new cards, decision answers, waits, continuations, dependency
changes, and Hermes approval requests. When the turn ends, the owner returns to the question and its native task to review.
A failed turn or a service restart during a lane tool does the same and sends no result. A follow-up stays saved, and an
unknown tool result stays unknown, so every continuation waits until someone checks it.
A follow-up that arrives during a completed lane turn runs as the next lane turn of the same owner. The approved scope
stays approved, and nothing is returned. Admitting the lane again for the same question keeps the effects of the turn
that asked it. A lane never leaves a turn with an unknown tool result.
The lane applies only to the owner's own task, so a callback of the owner keeps its normal route. It binds its own
turns: a lane turn keeps its holds until it ends, even if the question is answered, withdrawn, resolved, or deleted
meanwhile. Such a turn cannot finish the outcome, execute an approval, or return a result. It requeues the owner to
resume the question, and the dispatcher then moves the owner to a normal turn. An automated revision keeps the lane.
Ending the lane grants nothing: the normal task, decision, and pause rules apply again. Cancelling the owner's only
Work still stops the task.
The answered decision resumes in a new turn that carries the completed effects of the turn that asked it, as a normal
resume does. The lane's tool results stay history and are never returned as results of the resumed turn. If a lane
tool result is unknown, the lane turn stays current, so the existing receipt check stops the resume.

An owner whose earlier delegated PR merged and who now works on a follow-up PR for the same requester Work can continue
that open PR. A merged or closed PR of the same Work is no longer a writer. The requester's report of it exempts only
the requester's own run state. That requester is the direct requester, which waits for this owner, or the proven caller of
an audited or registered delegation. Its other branches, pending decisions,
unknown tool results, undelivered returns, and other waits still refuse the continuation, as do two open PRs, a PR of
another Work, or an open requester PR.
When the owner has two or more delegated PRs of that Work and every one of them merged or closed, no PR is the
outcome by itself, and Coconut never picks one. Not the first, the newest, or any other. The plain call refuses with
`Choose one retained Work item for this native outcome. Its delegated PRs of the same Work have all finished. Name one
of them with retained_pr_url.` The caller can then repeat the call with the optional `retained_pr_url`:

```json
{"task_id": "t_20b9d17a", "message": "Verify the installed change.",
 "retained_pr_url": "https://github.com/antiwork/gumroad/pull/8606"}
```

Coconut accepts the URL only for that exact shape: the owner has no retained outcome yet, the authenticated Work is the
direct requester's, or the proven caller's of an audited or registered delegation, every delegated PR of the owner for
it finished, the owner retains nothing else, and any saved report of the owner is a report of that Work. The URL must
name one of those PRs (case does not matter). The named PR then becomes the owner's Work item, as a single delegated PR
does. Coconut creates no Work row or other artifact, and the other PRs, the Work, and its reports stay as they are.
The choice only says which existing PR presents the continuation. It approves nothing, and the task, its execution, its
result route, and the requester's Work stay the same. Every guard above still applies: pause, cancellation, the
requester's branches and writers, unknown tool results, pending decisions, and active work. In any other shape, for an
owner that already retains its Work item, or for a URL of an open PR, another Work, another bot, the requester, or no
PR of the owner, Coconut refuses the call with `retained_pr_url names no finished delegated PR that this continuation
can choose.` and changes nothing. The selection is part of the call's delivery key, and the technical lane takes the
same field through the same checks.

The lane is not a sandbox and grants no permission. Terminal, coding agents, and file tools keep full local access, as in
any turn. The bot receives exact scope instructions to stay local and not push, publish, mark ready, or merge.
Coconut does not inspect commands, so it cannot prevent those effects from a command.

## Requester callback continuations

A delegated owner can come from a return callback of the requester, not from the requester's root task.
The callback runs in the root's execution, and the owner's result goes to the root.
Coconut treats that root as the same requester only when the callback has the exact delivery of its producer,
the producer replies to the root, and the owner's result route names the root.
Then a same-PR report from the root's execution is the requester's own report, and a running return callback of the root can continue the owner.
Only that calling turn is exempt. The source callback is an earlier turn, so it blocks while it is live.
The root's other live branches, waits, returns, decisions, and unknown tool calls still block.
A completed callback whose native task Hermes promoted to ready also blocks until the settle pass ends that task.

## Disagreement report

Coconut and Hermes each keep a lifecycle for a request. The settle pass archives the native task of a finished request.
A request is finished when it is completed, cancelled or failed, and so is a decision context that ended. A request without
an outcome settles after 15 minutes, and an outcome after a day.
At most once in ten minutes, the dispatcher checks for a task that this pass should already have archived, one hour after its due time.
It also counts the native tasks that no request owns. A task must stay on the list through two reports, ten minutes apart.
If either count is not zero then, it logs one warning that starts with
`Coconut and Hermes disagree`. The health check of Care reads this line. The settle pass and the report share `_native_orphans`,
so they cannot use different rules.

## Failure of the worker before its agent exists

If the worker fails before it has an agent, the task fails with `worker_preparation_failed` and the summary "Coconut could not
prepare this conversation." The exception message can hold a request body or a key, so it is never stored. The worker keeps the
class of the exception and its last frame (file, line and function) in `error_detail`, and the service saves that on the task as
`_error_detail`. It stays on the Mac and is not part of the public task. A failure of a turn with an agent records the same detail.

## Workspaces

`coconut_workspace` calls the native Hermes workspace resolver.
It creates one Git worktree per outcome and repository.
The bot uses the returned directory for code changes and tests.
An existing linked worktree can be adopted once to retain earlier changes.
The registry rejects another outcome's worktree and an unverified branch switch.
A Git-confirmed branch rename keeps the same owner and files. Coconut updates its saved branch without a service restart.
Git gc rewrites the worktree reflog and adds a tab before each empty message. Coconut still confirms a rename then,
but only when the saved entries match exactly apart from those tabs and nothing shows that Git expired a later entry.
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
A bot also keeps its own files in `bots/<id>/workspace`: clones, review checkouts, and evidence.
Nothing else removes them.
In the same pass, Coconut deletes an entry when nothing in it changed for 14 days (`BOT_WORKSPACE_STALE_SECONDS`).
A change is a new modification time or a new change time, so an extract or a copy of old files counts as new.
Below the low disk limit, the time is three days (`BOT_WORKSPACE_LOW_DISK_STALE_SECONDS`).
An entry is a file or a folder of the workspace. A folder that has a newer file stays, and Coconut then judges its entries one level down.
Coconut keeps a Git checkout that has uncommitted files, ignored files that a build does not make again (such as `.env`), commits that no remote has, or a stash. It keeps a bare repository.
It keeps a main checkout while one of its worktrees stays.
It keeps an entry that a process uses, a tracked worktree, a link, and a name that starts with a dot.
After it deletes a worktree, it prunes the record from the main checkout.
Coconut scans for open files again after it moves the entries, and before it releases an entry that a stop left, so a turn that opened an entry after the first scan puts it back.
It cleans a bot with work in progress too. A turn that only reads an old entry, without an open file during the pass, leaves no sign, so the age and the checks above are the only protection.

Retirement does these steps:

1. It validates the binding and the Git worktree record.
2. It skips a locked worktree and a Git operation in progress.
   A bot or a Hermes subagent can create worktrees inside its own worktree, for example to work in parallel or
   to test a base commit. After the outcome and process checks, Coconut removes each such worktree with
   `git worktree remove`. When one has staged, unstaged, untracked, or ignored files outside the rebuildable
   folders, Coconut first writes them to an archive in `workspaces/archive/nested/`. The archive name comes from
   the content, so a failed removal does not write a second copy. Coconut deletes it after 30 days
   (`NESTED_ARCHIVE_DAYS`), and deletes the oldest ones when all of them are larger than 2 GiB
   (`NESTED_ARCHIVE_LIMIT_BYTES`). The size limit never deletes an archive younger than one day. Its branch keeps its commits. Coconut checks that it can archive the parent's own files
   before it removes any nested worktree, and again under the registry lock just before the removal. So a parent
   that is too large to archive keeps its nested worktrees.
   Other refusals of the parent can come after the removal. The nested changes are then in their archives.
   Coconut does not restore a nested worktree when the outcome resumes.
   Coconut skips the parent when it tracks a nested worktree for an outcome, when one is locked, has a Git
   operation in progress or unmerged files, has a commit that no ref holds, has a populated submodule and changes,
   or has files that an archive cannot hold.
   It checks every nested worktree before it archives or removes any, and checks again under the registry lock.
   A nested record whose folder is gone does not count.
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

Retirement keeps work only while it can resume. Coconut discards the worktree of an idle outcome in these cases:

- All pull requests of the outcome merged or closed, and one of them is in the repository of the worktree.
- The checkout is already gone.
- Retirement refuses the worktree, and the outcome is idle for 2 days (`DISCARD_IDLE_SECONDS`) with no open pull
  request, for example after a branch switch, a nested repository, or ignored files larger than the archive limit.
- Retirement refuses the worktree, and the disk has less than 20 GiB free (`LOW_DISK_BYTES`). The pass then runs
  every 10 minutes.

A discard deletes ignored files and nested worktrees without an archive. So, except for a checkout that is already gone,
it needs every native card of the outcome to be done or archived, as for the scratch files below. A merged pull request
does not finish a scheduled, blocked, review, triage, or todo card, for example one that waits to accept an install.
Such an outcome gets retirement instead, which archives its ignored files exactly. If retirement refuses, the worktree
stays, on a low disk too, and only its build folders go. Before a discard, the pass reads the board again. If that
read fails, no worktree is discarded in that pass. Under the registry lock, the discard checks the native cards again.

Below 20 GiB free, an outcome counts as idle after 15 minutes without a task update (`URGENT_IDLE_SECONDS`), and not after
6 hours. A queued task that did not start for 12 hours (`URGENT_QUEUED_SECONDS`) does not keep the worktree of its
outcome, except while a claim is in progress. Retirement archives such a worktree exactly. The discard of a worktree that
retirement refuses, and the clearing of a scratch folder, wait 2 hours (`URGENT_DISCARD_IDLE_SECONDS`), because neither has an exact
archive. A running task, a recent task, and a process that uses the
worktree always keep it. The worktree comes back from its branch and its archive when the outcome resumes.

A discard keeps the branch and a ref `refs/coconut/discarded/<identity>/<time>-<commit>` for each HEAD, nested ones
too. A detached HEAD gets a branch `wt/<name>-detached` at its commit. The archive in `workspaces/archive/done/` holds the
staged and unstaged patches (up to 200 MB), the untracked files (up to 200 MB), and the ignored files that are not build
output (up to 200 MB), such as `.env`. Rebuildable folders and nested worktrees go without an archive. Only a locked
worktree, a worktree of another outcome, or a process that is not an old orphan keeps the worktree. For a discard
without the outcome variable, an orphan counts only when its command line names the worktree path or whose working folder is inside it, and never a
session program or a machine-wide daemon. A nested repository or a clone goes without an archive, and the log names it.

A later turn gets a new checkout of the saved branch, and Coconut puts the archived changes back. When the branch moved,
a patch does not fit, or the archive is gone, the checkout stays clean, and `coconut_workspace` tells the bot which
archive holds the changes. The refs go after 30 days. The archive goes after 30 days too. The archives that no binding
names stay within 2 GiB together, the newer ones first, and the archives that a binding names within 4 GiB together, and
this limit can delete a young one.

The same pass deletes the files in the scratch workspace (`workspaces/<outcome>`) of such an outcome. Under the service
lock, it checks the outcome again and moves the folder to `workspaces/.scratch-trash`, so no turn can start in it
meanwhile. The deletion runs after the lock. The folder stays, empty, because a later turn uses it as its working
directory. A scratch folder that no task names goes after 2 days
without changes. A bot's own workspace and a folder that a process uses stay.

Before it retires any worktree, each hourly run also stops the old orphans of every idle outcome. It checks the
outcome's worktrees and its scratch workspace (`workspaces/<outcome>`), with one `lsof` scan, and uses the same
orphan rules. This runs even when retirement refuses the worktree, for example for a locked nested worktree.
Such orphans, for example an Android emulator or a test script left by a crashed worker, otherwise hold disk space,
CPU, and memory for days. This step also runs for an outcome that has only a scratch workspace. Before it stops the
orphans of an outcome, it checks again that the outcome is idle, because a turn can start during the `lsof` scan.
A failed check is logged, and retirement continues.

When retirement refuses the worktree of an idle outcome, for example for a nested repository or a branch switch,
Coconut deletes the rebuildable folders of that worktree. These are the folders that the archive leaves out, and a build
creates them again. Coconut deletes only folders that Git ignores, never follows a link, and keeps a folder that holds a
worktree, another repository, a folder that it cannot read, or the saved path of another outcome. The worktree must belong to the saved repository, the outcome must
still be idle, and no process may use the worktree. Coconut checks the binding and the folders again under
`workspaces/.workspace.lock`. Under that lock, it moves the folders to `workspaces/build-trash`, and it deletes
them after the lock, so a new turn does not wait for a long deletion. A folder on another disk than the registry stays,
and the log says so. The next pass empties a trash that a stop left.
The worktree must be registered and not locked. The other files and the branch stay.

Coconut never stops a shared service that a turn started, such as OrbStack, Docker Desktop, CoreSimulator,
`launchd_sim`, or the `adb` server, because every outcome uses it. `SHARED_APP_BUNDLES`,
`SHARED_EXECUTABLE_PREFIXES`, `SHARED_EXECUTABLE_NAMES`, and `ADB_SERVER_ARGUMENTS` in `work_workspace.py` name
these services. `SHARED_APP_BUNDLES` also names the client folders of each app bundle. An `adb` client,
a client program such as `docker`, the JDK, Xcode, and the Command Line Tools are not shared. A shared service whose cwd is in a worktree does not keep
the worktree. An open file of a shared service still keeps it.

The log names the cause when Coconut does not retire a worktree, for example a busy service or a changed worktree.
When the service is busy, Coconut tries that worktree again once in the same pass, 30 seconds later
(`BUSY_RETRIES`, `BUSY_RETRY_SECONDS`). A stop or a pending restart ends the pass, and the log names each busy
worktree. When the first idle check of a pass finds the service busy, the next pass runs 30 seconds later.
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

A worktree has one outcome. A native successor can take over a retired worktree of an earlier outcome when it passes
the saved path as `existing_worktree`. The worker reads the proof from its native claim and from the Kanban database,
never from tool arguments. Under the registry lock, Coconut hands over the binding only when all of these hold:

- The turn holds the native claim it names: the task is running with the same claim lock, run, tenant, and Coconut request.
- The native task is a direct child of exactly one task that Coconut created for the old outcome
  (idempotency key `coconut:<outcome>`, or its `:<input>` and follow-up forms), in the same tenant, at the same path.
  No other parent names the path.
- No native task of the old outcome is unfinished or holds a run or a worker, and no process of the old outcome runs.
- The repository, the exact saved path, the archive, and its owner, branch, and commit match the binding.
  The branch still points at the archived commit.

Coconut then moves the binding to the new outcome and restores it as above, on the same path and branch.
It keeps the archive, and the binding records the handoff. A refusal or a failed restore changes no binding and keeps the archive.
A live worktree of another outcome is never handed over.
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
A delegate can hold both a hidden report of the requester's Work and the one PR
that it delegated for that Work. The continuation retains the PR. The report must
name the owner as its reporting task and the requester's Work and task as its
parent. Two PRs, another Work, or a missing link still need one chosen Work item.
Another task of the requester can own the Work of a delegated PR. The continuation retains the PR only when that task
registered the wait: its saved `kanban_link` result makes the Work's card a child of the owner's current card, or of a
card that the owner completed with `kanban_complete` without naming a card. Hermes completes an unnamed card only for a
claimed worker, so that card is the owner's own. A turn without a claim can complete any card that it names, so a named
card does not count. The board must still have that edge. The audit that delegated must have finished. The Work stays the parent
presentation. Both requester tasks and their live branches, waits, decisions, and unknown tool calls still block.
Only the calling task is exempt: one of those two tasks, or a running return callback of one of them.
A shared PR URL, bot, or comment does not link them.

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

## Idle Work in Task details

Task details groups a bot's Work into active, idle and finished items. Hermes supplies the facts:
each native outcome carries `native_last_activity_at`, the time of the task's last Hermes event, and
`native_diagnostics`, the output of Hermes' `compute_task_diagnostics` for a task in triage, todo,
blocked, review or scheduled. An archive event does not count as activity.

A task in one of those states without activity for 7 days is idle. The phone reads the state from the
Work item that owns the Hermes task, also for a pull request row and for Work that the bot delegated.
The phone lists idle tasks with their age and first diagnostic, and offers one archive button behind a
confirmation. `POST /v1/bots/{bot_id}/work/archive-idle` takes `native_task_ids` (at most 500) and calls
Hermes' `archive_task` only for that bot's tasks that are still in a waiting state. Hermes keeps each task
under the bot of its Work item, so the phone sends each task to that bot's route. A task that started
again stays. Finished Work from the last 7 days shows by default; older Work stays behind
**Show older**. The phone's recent state leaves that older Work out, and **Show older** loads it
from `GET /v1/bots/{bot_id}/work/older`. A stopped checkout that offers recovery stays at the top for 7 days, then moves to
finished Work with its recovery button.

## Archived Work with divergent checkout histories

`POST /v1/maintenance/checkout-recovery` is a separate paired-device, managed-service
operation for an **idle archived native Work owner** whose saved branch and attached
branch diverged. It requires a UUID `Idempotency-Key`, and exactly these JSON strings:
`task_id`, `native_task_id`, `expected_owner`, `repository`, `path`, `saved_branch`,
`attached_branch`, `saved_head`, `attached_head`, `recovery_branch`, `message`.
The recovery branch must be `wt/recovered-` followed by the request UUID. The
message is the operator's authenticated continuation input, not an approval.

For stopped Work, open the original bot conversation's **Bot options → Task details**
and choose **Recover checkout**. The phone first calls the paired read-only
`GET /v1/tasks/{task_id}/checkout-recovery` preview. Reading the preview does not
resume Work or plan a Git merge. A separate confirmation adds `resume_confirmed: true`
and the preview's `resume_consent` to the POST. The consent binds the paired device,
original owner, cancellation generation, saved checkout and local tracked edits.
A later Stop invalidates it, including another Stop while the Work is already stopped.
The service clears only that Work's cancellation during successful successor admission.
Installing the action never resumes stopped Work by itself.

The phone retains the confirmed request and key so a lost reply can be reconciled by
retrying that exact request. A `draining` receipt is not completed recovery. Check
its status until it is `applied` or held; a held receipt permits a new preview only
when the original-checkout guards below still pass. Pairing changes cannot send the
retained request to a different Mac.

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

Each outcome has its own work session. Once an hour, with worktree retirement, Coconut runs one Hermes script per bot:

1. It records the newest session of each chat and of each work conversation updated in the last 30 days. A prune can remove the ended
   compression ancestors of an open chain, so this runs before any prune, also on the first run of an existing install.
2. It ends the newest session of each idle outcome with Hermes `end_session` and the reason `coconut_idle`.
   It checks again that the outcome is idle just before each bot. It never ends a chat session.
3. With Hermes `prune_sessions`, it deletes idle work sessions (`coconut_idle`) inactive for `SESSION_RETENTION_DAYS` (7),
   and other ended `coconut` sessions, such as compressed parts of a chat, inactive for `SESSION_HISTORY_DAYS` (30).
   So native `session_search` finds a month of chat history. Sessions of other sources, such as cron or CLI, keep the profile's own Hermes settings.
4. At most once a week, it runs Hermes `vacuum`, which first merges the search indexes. Two tests decide, so a VACUUM that a holder deferred runs later.
   More than a quarter of the file is free, or the store is over 512 MiB and 1.5 times as large as after the last VACUUM.
   The second test exists because the search indexes fragment and keep their pages. One 4.5 GB store had 16% of its pages free,
   and a merge plus VACUUM took it to 0.95 GB. The disk must also hold two copies of the used pages plus 2 GiB, as the VACUUM of the Coconut database requires. Otherwise the result of the run says `vacuum_skipped`.
   A bot with a due VACUUM and no running turn goes on hold. It keeps its queue, but no new turn starts, and each of its ready
   tasks waits as `scheduled`. A run that Coconut claimed before the hold starts its turn after the hold. Only a claim for this
   bot counts as a live turn. Then Coconut rewrites the store and ends the hold, even if the rewrite fails.
   A bot with a running turn is not held at first. Maintenance tries again after 15 minutes, up to three times. Then the bot goes
   on hold, and Coconut waits up to two minutes for its running turns. If one is still running, or if the pass ends without a
   VACUUM for another reason, for example because another process holds the store, the bot goes on hold again only after six hours.

When an idle outcome resumes, Coconut reopens its session with `reopen_session`. If Hermes has already deleted it,
Coconut seeds a new session from its own saved messages of every task in that work. The native card keeps the work record.
Coconut does not change Hermes. It uses only these `SessionDB` methods and the `hermes_state_holders` checks, which the Hermes contract check requires.
