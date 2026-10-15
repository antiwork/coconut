# State sync

The app keeps cached conversations, navigation, and drafts during connection recovery.
Opening a system prompt does not disconnect the app. Returning from the background requests current state immediately.
Network changes trigger a new attempt. Failed reads retry with a short, capped delay.
Connection details and diagnostic text stay in Settings. Failed user actions keep their normal error feedback.

Each paired session has one event loop. A generation check rejects responses from an older connection attempt.
Concurrent reads share a snapshot request. Current servers send changed tasks and messages with current decisions and Work.
Idle responses refresh Work observations without advancing the event cursor.
Hidden messages use explicit removal IDs. Cursor gaps, unknown events, and structural changes return a content reset.
An update sends the work cards of the outcomes that its events change. Its removal IDs name the rows that it hides.
Older servers retain the full snapshot and event path.
The app advances its cursor only after it applies the complete batch or snapshot.
Display cache writes run off the main thread and combine rapid updates.

Only a working bot with a confirmed connection shows a green dot. The dot appears on the bot avatar.
An idle, waiting, scheduled, failed, archived, or disconnected bot shows no dot.
Connection loss does not change the stored task status or imply that execution stopped.

The simulator regression uses `scripts/test_state_sync.py` with a signed simulator build from `build-for-testing`.
It checks network loss, returning to the app, retained drafts, quiet recovery, and streaming without full-state requests.
The test server runs only on loopback and starts no Hermes process.

## Content, settings, and history

Initial content includes 60 recent messages per bot and the discussion for each pending card. Earlier messages load on demand with stable database cursors.
History reads cannot replace newer records already present in the app. Search still covers stored history.
Confirmed send IDs travel separately from messages, so pagination cannot leave accepted sends waiting.
The conversation keeps its position after earlier messages load.

Native settings load separately. The app retries failed settings reads while content remains usable.
Confirmed settings writes update the app from their response. A failed refresh does not turn a saved change into a failed mutation.
Native routine results retain their recorded task links.

Notifications fetch the selected card directly. Card reads do not advance the shared event cursor.
The app rejects older responses that started before the card read.
Task relationship indexes exist only during one Work projection. Later projections rebuild them from current records.
Hermes remains the owner of native settings and execution. These changes do not alter Hermes source.
