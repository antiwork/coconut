# Continuous bot conversations

A bot has one conversation from the user's point of view. Tasks organize work and Inbox decisions within that conversation.
Opening another task, running a routine, closing the phone app, or restarting the service does not reset the bot's context.

## Native history

Each worker opens the bot's native Hermes `SessionDB` and passes it to `AIAgent`.
Before the first native turn, Coconut imports the bot's existing saved messages once.
The import excludes other bots' private messages, hidden messages, and incomplete streaming responses.
If native history already exists, Coconut adds only the missing older prefix to its active context.
The import uses Hermes's atomic replacement API. Hermes preserves the previous rows, tool results, and reasoning data.
An interrupted import finds the existing prefix and does not insert it again.
The earlier import into a separate searchable record was insufficient. A one-time repair restores that prefix to the active conversation.

The worker resolves the native continuation and loads its active model history.
Hermes owns summaries, compression, tool history, and searchable archived messages.
Coconut does not append the full transcript again after compression.
The phone keeps its existing transcript and stable bot identity across native session changes.

The current request comes from its task ID. A later queued message cannot become the current request.
Earlier messages inform the reply but grant no new authority to create or delete bots.
Existing execution receipts continue to prevent repetition of completed effects during recovery.

## User experience

The bot continues directly from its active Hermes conversation. Search recovers older details absent from that context.
The bot handles context limits and compaction internally. The interface has no session controls or maintenance messages.
The bot still answers direct technical questions accurately and admits missing facts when retrieval fails.
Current service identity takes precedence over an old bot name in saved notes.

Settings contains one `Restart Bots` action for all bots.
It records a durable, idempotent request for fresh native working conversations after current work finishes.
It does not interrupt an executing action or repeat its effect.
Chats, searchable history, memory, pending decisions, routines, and profile settings remain available.
Ordinary service restarts retain the current native conversation.

## Design reference

[Designing Grok Bot](https://x.ai/news/designing-grok-bot) organizes the product around persistent bots and their responsibilities.
Coconut follows that approach: identity and history belong to the bot, and tasks remain internal coordination details.
Presence stays on the bot avatar. Decisions use structured cards in the transcript and Inbox.
Runtime maintenance stays out of normal conversation. Existing user choices, including the single green activity dot, remain authoritative.

## Verification

`python3.14 scripts/test_continuity.py` uses real Hermes and temporary profiles on the Mac mini.
It tests old-message import and continuation after service restart with tools forbidden.
Separate checks cover native compaction, native continuation, history search, and global restart.
It checks that another bot cannot see the test bot's private history.
The check does not modify upstream Hermes or existing bot conversations.
