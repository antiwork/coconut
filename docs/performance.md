# App performance

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
  --xctestrun .build/speed/Build/Products/Coconut_iphonesimulator26.5-arm64.xctestrun \
  --simulator <simulator-uuid> \
  --result /tmp/coconut-performance.xcresult
```

3. Compare repeated runs on the same simulator and build configuration.
4. Keep other builds and benchmarks idle during measurement.

The fixture uses a local HTTP server. The test cannot send a real message or invoke a bot.
XCTest skips the suite when the fixture URL is absent.

## Further investigation

Use Instruments on an iPhone to locate CPU work and missed frames during a slow interaction.
Check conversation opening, typing, and scrolling while messages stream from a connected Mac.
Compare short conversations with long conversations and long exchanges between bots.
Measure the same interaction before and after each proposed change.
Only keep changes that improve the measured result and preserve conversation behavior.

Apple documents the launch measurement in [XCTApplicationLaunchMetric](https://developer.apple.com/documentation/xctest/xctapplicationlaunchmetric).
