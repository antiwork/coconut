# Bot delegation in Grok Bot

## Source and method

Source: `ScreenRecording_09-10-2026 07-47-33_1.MP4` in iCloud Drive Downloads.
The recording lasts 63.055 seconds at 1180 × 2556 pixels.
FFmpeg extracts an overview every two seconds and transition frames every 250 milliseconds.
Additional frames show text and controls at larger sizes.
The audio track is silent.
Private frames remain in `.runtime/grok-recording`.

## Observed behavior

| Time | Observation |
| --- | --- |
| 00:02–00:15 | Desk shows its own messages and compact exchange rows in chronological order. |
| 00:15–00:16 | An exchange row opens a separate conversation with a horizontal navigation transition. |
| 00:16–00:33 | The conversation shows both bot avatars, sender names, full messages, dates, and a Read-only control. |
| 00:16–00:33 | Scrolling reveals messages about several tasks between the same two bots. |
| 00:33–00:34 | Back navigation restores Desk and its previous scroll position. |
| 00:43–00:55 | Gumroad Dev shows its own work updates, plus compact exchange rows involving Desk. |
| 00:55–00:56 | Its exchange row opens the conversation between the same two bots. |
| 00:56–00:62 | The full exchange remains read-only; it has no message composer. |
| 00:62 | Back navigation restores Gumroad Dev and its previous scroll position. |

Rows use `Messaged [bot]`, `Message from [bot]`, or `2 messages with [bot]`.
They use centered secondary text and a small bot avatar.
They do not show the full payload or a large task card.
The two avatars replace the normal bot title in the exchange view.
A bottom arrow provides access to the latest messages when the view is scrolled up.

The recording shows a conversation between two bots that spans several tasks.
It does not prove how Grok stores or groups that conversation internally.
It does not establish the exact grouping rule or the scroll target for every exchange link.
No composer interaction or human intervention occurs in this recording.

## Correction to the earlier proposal

Both bots use compact exchange rows.
The receiving bot does not expand all bot messages inside its normal chat.
Each bot keeps its own work updates in its normal chat.
Full bot messages belong in the shared exchange view.
Simply swapping Coconut's two existing views would preserve the wrong distinction.

## Original Coconut behavior

`Service._message` assigns every task message to `task.conversation_bot_id`.
This includes messages from delegated tasks and explicit messages between bots.
`ConversationView.messages` selects that display bot ID.
`ConversationView.sharedTasks` places links to those tasks in the executing bot's header.
Together, these rules put the detailed delegated output in Desk and leave links in the worker's chat.

## Implementation target

- Show each bot's own work updates in that bot's chat.
- Show Desk's summaries and decisions in Desk.
- Represent messages between bots with compact rows in both chats.
- Open the full exchange from either row with clear sender identity.
- Preserve the originating chat's scroll position when returning.
- Keep the exchange read-only; retain direct intervention in each bot's chat.
- Classify messages from sender, recipient, and task metadata.
- Keep stable task and decision links for the inbox.
- Preserve the underlying messages and Hermes sessions.

The role of a message determines its presentation.
Its length and the name Desk do not determine its presentation.
Coconut now implements this presentation through structured message metadata.
See [Hermes integration](hermes-integration.md) for native transport and recovery boundaries.
