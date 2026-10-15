# App performance

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
- Each structural update decodes the whole task table on the server, about 0.4 s on the laptop.
- A reset leaves out finished work older than 7 days, which Task details loads on request. On the live data of
  October 15 it fell from 4.19 MB to 1.54 MB. See [the reset measurements](performance-2026-10-15.md).
- Opening and scrolling a conversation spends most of its time in SwiftUI layout of Markdown rows.
- Use Instruments on an iPhone to confirm these results on a device.

Apple documents the launch measurement in [XCTApplicationLaunchMetric](https://developer.apple.com/documentation/xctest/xctapplicationlaunchmetric).
