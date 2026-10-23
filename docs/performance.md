# App performance

## Card checks, October 21, 2026

The live log showed `_queue_decision_refreshes` as the longest hold of the service lock: about 1.2 s on average, 1,706
times. It runs every 60 seconds and after each PR source read. Most of that time went to a full read and parse of the
task table: 17,035 rows and 83 MB, 163 ms to read and 203 ms to parse on the laptop.

The service now keeps the parsed task table between operations (`task_cache.py`). SQLite triggers write the ID of
each changed row to `task_changes` with a rising number, so a later read applies only those rows. The triggers commit and
roll back with the write, and a rollback also drops the cache. A row that a write moves in row order makes the next
read parse the whole table again. Read views keep their own copy. Each operation gets its own dict, but the task objects
are shared, so only code that reads may use `_task_map` and `_all_shared('tasks')`, as before.

On a copy of the live database with 25 pending cards and 3 task writes between passes (laptop, ms per call):

| | First call | Later calls |
| --- | --- | --- |
| Before | 4,982 | 790–964 |
| After | 2,516 | 271–340 |

All 25 card fingerprints and the queued card checks were the same before and after, in each of 6 passes.
With `COCONUT_VERIFY_TASK_CACHE=1`, each read compares the kept tasks with the table. CI runs the backend tests so, and the
full suite found no difference. A failed rollback and a sequence number that goes back both make the next read parse the
whole table.
The kept table costs about 208 MB of memory in the service process (1.25 GB before).

## Routine tick, October 21, 2026

The routine tick runs every 15 seconds. It read the native routines with a new Hermes process, rewrote the routines
table, and decoded the whole task table three times under the service lock. The Mac mini log of October 21 showed
lock holds of 1,197 ms on average for `refresh_native_routines` and 924 ms for `tick_native_routines`.

These measurements use a consistent copy of the Mac mini database: 16,973 tasks, 532 routines, and 5 bots. A stub
provider serves the stored routines, so the times leave out the Hermes process. The times come from a laptop.

| Step | Before | After |
| --- | ---: | ---: |
| `refresh_native_routines` | 286 ms | 21 ms |
| `tick_native_routines`, with the refresh | 796 ms | 25 ms |
| `_routine_index` | 252 ms | 0.16 ms |

- The provider keeps the routines of each bot until a file of that read changes: `cron/jobs.json`,
  `cron/executions.db` and its WAL file, `config.yaml`, `.env`, the receipts in `<data>/bots/<uuid>/runs`, or the default profile
  files. Most ticks start no Hermes process.
- The refresh reads the tasks only when Hermes returns a completion receipt.
- The refresh writes the routines table only when its rows change.
- The tick reads the tasks with an unfinished native job through the partial index `tasks_native_job_open`.
- `_routine_index` reads the open routine tasks, the queued or working ones, and the tasks that they started, through
  indexes. It used to decode every task.

On the copy, `_active_routine_task` gave the same answer for all 532 routines with the old and new index.

## Kanban pass reads, October 21, 2026

Each Kanban pass holds the service lock while it reads Coconut rows. These measurements use a consistent copy of the
Mac mini database: 16,973 tasks, 1,545 decisions, 4,411 contexts, and 2,622 work and file rows. A stub Kanban serves
the 1,531 rows of a copy of the live Kanban database. The times come from a laptop. The Mac mini is about 2 to 3 times
slower.

| Step of one pass | Before | After |
| --- | ---: | ---: |
| Finish expired Undo windows (`finish_inbox_swipes`) | 15.6 ms | 0.004 ms |
| Promote saved legacy work (`_migrate_native_work`) | 48.1 ms | 14.1 ms |
| Contexts of the ready rows (`_dispatch_kanban`) | 15.6 ms | 0.014 ms |

- The Undo check read and decoded every decision. A partial index, `decisions_undo`, now holds the swiped cards.
- A work pause reads its pause decisions by ID. It used to decode every decision.
- The check for a recorded merge decision reads the cards of one PR through the index `decisions_merge_url`.
- The promotion of legacy work reads only pending decisions and queued or working contexts, through the status indexes.
- The dispatch reads the contexts of the ready rows by ID. It used to decode every context.

On the copy, the pause and merge answers were the same for all 1,986 PR and work rows with the old and new reads.

### PR poll

The PR poll runs every 60 seconds and when a bot tracks a PR. The copy had 7 open PR groups.

| Step | Before | After |
| --- | ---: | ---: |
| Card read of `schedule_sources`, every 0.2 s while a refresh waits | 17.2 ms | 0.026 ms |
| `_resolve_merged_pr_decisions`, once for each PR group and once at the end | 23.6 ms | 0.012 ms |
| `_queue_pr_reviews` for one PR group | 369 ms | 37 ms |
| `_queue_pr_reviews` for all PRs, at the end of a refresh | 589 ms | 161 ms |

- The poll reads the pending cards through the status index.
- The merged-card check reads pending merge cards first. It reads the merged PR rows only when such a card exists.
- The review queue reads the PR rows, not all rows. It reads the review tasks and their execution sessions by ID. It used
  to decode all 16,973 tasks for each PR group.
- Each PR group still schedules its own review as soon as GitHub answers. A slow PR does not delay the others.

To check the writes, both versions ran on a copy with the review marks of 607 PR rows cleared and 20 merged cards set to
pending. They wrote the same 20 resolved cards, 3 queued reviews, and 23 events.

## Server updates, October 15, 2026

These measurements use a new consistent copy of the Mac mini database: 13,976 tasks (64 MB of JSON), 2,103 work and
file rows, 1,341 cards, and 4,107 contexts. A stub Hermes Kanban serves the 1,032 task rows of a copy of the live
Kanban database. The bench replays the last 2,000 events one at a time, as a phone that keeps up: it caps the event
cursor at each event and sets the clock to 0.3 s after the event. "Before" is the server after the October 14 changes.
The times come from a laptop with no other load. The Mac mini is about 2 to 3 times slower.

| Measurement | Before | After | Change |
| --- | ---: | ---: | ---: |
| JSON for the 2,000 updates | 353.0 MB | 135.9 MB | −62% |
| Compressed size (gzip level 5) | 81.6 MB | 34.8 MB | −57% |
| Updates that resend all work cards | 82 | 0 | |
| Work cards in the 82 decision and Work item updates | 131,774 | 284 | |
| Server time for the 2,000 updates | 734.8 s | 21.4 s | −97% |
| Server time of one update, median | 379 ms | 12 ms | −97% |
| Server time of one update, 90th percentile | 424 ms | 21 ms | −95% |
| Slowest update | 602 ms | 53 ms | −91% |

Each update still sends about 64 KB of bots. That is now most of a typical update.

The bench applies each update as `StateUpdates.applying` does and compares the complete phone state after each of
the 2,000 updates. The state was the same, with one field that the app does not show: `activity_observed_at` of
finished cards without a native task. The old server set it to the time of each update that resent every card. Now
the field keeps the time of the last update that sent the card. The app reads this field only for unfinished cards.
The result was the same with a stub Kanban that has one native task for each bound task.

- A decision or a Work item event used to resend every work card. It now sends the cards of the outcomes that the
  event can change. For a Work item, that is its own outcome. For a decision, those are the outcomes that can show it
  as their first pending decision: the outcomes of a task with the same native task, the outcome whose root task asked
  the decision, the outcomes of its pull request, and the legacy outcome that it pauses. A changed row can also leave an
  outcome, so the version of the last earlier event counts too. When no earlier event recorded that version, every
  card is sent, as before.
- A structural update decoded the whole task, Work, context, and card tables. It now reads the rows that its events
  name and the tasks that own them, by ID or with the existing indexes. It reads the Work rows only when an outcome
  changed.
- `removed_artifacts` listed every hidden row in each update, about 500 IDs. The phone holds only rows that were
  visible, and a row leaves through its own event or through its task. So the list now names the rows that this update
  hides, and the rows of a task that this update hides.
- The search for retained legacy work skips rows without links in SQLite when they have a native task or are merged
  or closed pull requests. It checks each linked outcome once. It runs only when an update starts a new 15-second
  interval.
- The Work projection reads the links of each pull request in SQLite and decodes only the rows of the projected
  outcomes. It decodes the root tasks and the tasks of the projected native outcomes, not every Kanban task.

## Measurements from October 14, 2026

The September fixture was much smaller than the real data. It had 818 messages, one task, no work cards, and an older
synchronization protocol, so it could not show the costs below. The October measurements use a consistent copy of the
Mac mini database: 27,885 messages, 1,321 cards, 13,767 tasks, and 2,042 work cards. The largest conversation has
14,991 messages with replies of about 900 characters. The recent state that the phone loads is 3.7 MB of JSON.

The app ran on an iPhone 17 Pro simulator with iOS 27.0, in a Debug build with `-O` optimization. The real backend served
the copy with a stub Hermes Kanban and an `artifact.observed` event every 3 seconds. Each value is the median of five
samples after XCTest's warmup iteration. Both builds ran in the same session against the original server code.

| Measurement | App CPU, original | App CPU, new | CPU | Instructions |
| --- | ---: | ---: | ---: | ---: |
| Reading a conversation for 20 s while updates arrive | 0.473 s | 0.300 s | −37% | −62% |
| Loading one older page | 0.287 s | 0.188 s | −34% | −43% |
| Opening and closing the long conversation | 0.569 s | 0.517 s | −9% | −22% |
| Four scroll gestures in the long conversation | 1.421 s | 1.437 s | no change | −25% |
| Opening a conversation and switching tabs | 1.218 s | 1.088 s | −11% | −11% |
| Typing one sentence | 0.312 s | 0.307 s | no change | no change |

Launch to the first responsive frame took 2.62 s with both builds. Simulator start-up dominates it.
CPU time of the scroll test varied between runs: the original app measured 1.68 s in an earlier session.
The count of retired instructions varies less, so it is the better signal for scrolling.

`scripts/test_app_performance.py` reproduces this data shape with synthetic records. On the same simulator it measured:

| Measurement | CPU | Instructions |
| --- | ---: | ---: |
| Reading a conversation for 20 s while updates arrive | −41% | −63% |
| Opening and closing the long conversation | −8% | −10% |
| Loading one older page | −7% | −13% |
| Four scroll gestures in the long conversation | −2% | −13% |
| Opening a conversation and switching tabs | −4% | −8% |
| Typing one sentence | no change | no change |

The app changes:

- An update assigns a store property only when its value changes. Observation notifies the readers of a property on
  every assignment, so each update used to redraw the open conversation.
- The saved state is written at most every 10 seconds while updates arrive, and at once when the app leaves the
  foreground. Encoding the 3 MB state was the largest cost of each update.
- The conversation keeps its transcript until the store changes. Scrolling changes view state several times per swipe,
  and each change rebuilt the transcript.
- The transcript filter reads each message once. It used to copy and lowercase the full text of every reply, and to
  scan all messages for each completion label.
- A conversation reads only the saved task answers, not every work card. Work cards change on most updates.

The server changes, measured on the same copy with a laptop. The Mac mini is about 2 to 3 times slower.

| Measurement | Original server | New server | Change |
| --- | ---: | ---: | ---: |
| JSON for the last 2,000 events, one update for each event | 1,226 MB | 352 MB | −71% |
| Updates that resend all 1,581 work cards | 593 of 2,000 | 107 of 2,000 | −82% |
| Build of the recent state | 656 ms | 420 ms | −36% |
| Card read when a person opens a notification | 270 ms | 23 ms | −91% |

- An update in a new 15-second interval resent every work card. It now adds only retained legacy work, whose activity
  depends on time. Native work cards change only through events. A task event also sends the work of that task,
  because a card shows the status of its tasks.
- A card finds its original request in the messages of its task. It used to read the whole history of the bot.
- An index on `decision_id` finds the messages of a card.

Not kept: a separate observation time for native work cards gave no measurable gain on its own, so it was removed.

## Measurements from September 13, 2026

The first measurements use commit `1edcac0c0be3136db9f2f4f1227ca66937bb6ec5` plus the new performance tests.
The fixture contains synthetic messages for nine bots, with 818 messages in total.
The largest conversation contains 427 messages.
These counts match the stored history inspected that day. The fixture does not contain private messages.
It includes one task and one decision. It does not reproduce the full task history or a live connection.

The tests use a Debug build on an iPhone 15 Pro simulator with iOS 26.5.
Each test records three samples after XCTest's warmup iteration.

| Measurement | Original code | Proposed history cache |
| --- | ---: | ---: |
| Process launch to first responsive frame | 0.995 s | 0.987 s |
| App CPU during conversation and tab switching | 1.344 s | 1.342 s |
| App CPU during four scroll gestures | 1.520 s | 1.547 s |

The cache did not establish a performance improvement. We removed it.
The final change adds measurements only. It does not change the app or Hermes.
The first typing comparison had different initial text lengths between samples. We excluded those results and corrected the test.
The typing test now clears the composer outside each measured interval.
The corrected test passes and measures 0.264 seconds of app CPU time per 32-character sentence.
That result establishes a new typing baseline. It is not a comparison with the rejected cache.

The launch test restarts the process with saved history. It does not measure a first installation or a device restart.
CPU time measures the app's work during a sequence. It does not measure tap latency or keystroke latency.
XCTest's clock measurement includes automation overhead. Do not use it as time from a tap to a visible screen.
Scroll duration does not establish the number of missed frames.
Simulator measurements do not establish performance on an iPhone.

## Run the measurements

1. Build the test products with `xcodebuild build-for-testing` for the target simulator.
2. Run the fixture with the generated `.xctestrun` file:

```sh
python3 scripts/test_app_performance.py \
  --xctestrun .build/speed/Build/Products/Coconut_iphonesimulator27.0-arm64.xctestrun \
  --simulator <simulator-uuid> \
  --result /tmp/coconut-performance.xcresult
```

3. Compare repeated runs on the same simulator and build configuration.
4. Keep other builds and benchmarks idle during measurement.

The fixture generates synthetic records with the counts and text lengths of the October data. It contains no private
messages. It serves the recent state, older history pages, and an update every 3 seconds. The test cannot send a real
message or invoke a bot. XCTest skips the suite when the fixture URL is absent.

To measure a copy of real data, run the backend on a consistent dump of the database and set
`COCONUT_PERFORMANCE_TEST_URL` and `COCONUT_PERFORMANCE_CONVERSATION` (the accessibility identifier of the conversation
row) in the `.xctestrun` file. Every pairing code must return one device for the suite to pair before each test.

## Further investigation

- Decision and work-card events still resend every work card. They are about 5% of updates and most of the remaining bytes.
- Each structural update still decodes the whole task table in its read view, about 0.4 s on the laptop.
- A reset leaves out finished work older than 7 days, which Task details loads on request. On the live data of
  October 15 it fell from 4.19 MB to 1.54 MB. See [the reset measurements](performance-2026-10-15.md).
- Opening and scrolling a conversation spends most of its time in SwiftUI layout of Markdown rows.
- Use Instruments on an iPhone to confirm these results on a device.

Apple documents the launch measurement in [XCTApplicationLaunchMetric](https://developer.apple.com/documentation/xctest/xctapplicationlaunchmetric).
