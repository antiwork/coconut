# State sync

The app keeps cached conversations, navigation, and drafts during connection recovery.
Opening a system prompt does not disconnect the app. Returning from the background requests current state immediately.
Network changes trigger a new attempt. Failed reads retry with a short, capped delay.
Connection details and diagnostic text stay in Settings. Failed user actions keep their normal error feedback.

Each paired session has one event loop. A generation check rejects responses from an older connection attempt.
Concurrent reads share a snapshot request. Ordered message updates change the transcript without requesting a full snapshot.
Gaps, unknown messages, hidden messages, and structural changes require canonical state from the Mac.
The app advances its cursor only after it applies the complete batch or snapshot.
Display cache writes run off the main thread and combine rapid updates.

Only a working bot with a confirmed connection shows a green dot. The dot appears on the bot avatar.
An idle, waiting, scheduled, failed, archived, or disconnected bot shows no dot.
Connection loss does not change the stored task status or imply that execution stopped.

The simulator regression uses `scripts/test_state_sync.py` with a signed simulator build from `build-for-testing`.
It checks network loss, returning to the app, retained drafts, quiet recovery, and streaming without full-state requests.
The test server runs only on loopback and starts no Hermes process.
