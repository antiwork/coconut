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

Replies and reactions carry the exact referenced message into the new native turn.
Card answers and follow-up questions carry the original human input and the current recorded decision.
These references survive native compaction and continuation without restoring the full app transcript to model context.
A reaction gives feedback; it does not authorize a pending action.
A card answer uses its current revision and exact recorded action. Completed effects remain protected by execution receipts.
Read-only bot exchanges open at their most recent message. New messages do not move someone reading older messages.

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


Delegated work uses a task list from Hermes. Coconut saves the native snapshot with tool receipts and decisions, then restores it for the same assignment. The list stays out of the chat UI. A finished reply does not complete an assignment with pending items. Coconut queues a continuation unless a recorded process, handoff, or decision owns the next step. After two unsuccessful automatic continuations, Coconut reports an unfinished-work failure to the owner. New input or a recorded dependency result resets that retry budget.

Progress messages to the requesting bot preserve its dependency. They cannot suppress the final result. The model must verify each outcome before marking its native task items complete. Coconut does not infer completion, authorization, or future scheduling from prose. This guards lifecycle errors; it does not prove the model's assessment of task correctness.

### Native schedules in Coconut

Coconut's scheduler claims native Hermes jobs and queues durable bot turns. It does not require a standalone Hermes gateway.
The cron tool reports Coconut's observed scheduler health and each job's support status. A saved or paused job does not imply active execution.
Coconut uses Hermes to assemble scheduled prompts, load skills, include prior outputs, and check model drift. The worker scopes the job's working directory to its turn.
Script-only jobs, monitors, explicit model routes, profile cron model defaults, and toolset overrides remain unsupported. Coconut reports these fields and leaves their jobs unchanged.
Quiet runs use Hermes' native silence rule. Coconut records successful completion without publishing the silence marker or replacing prior useful output.
Scheduled turns retain Coconut's gateway approval path. They can pause for a decision and resume with the recorded answer.
