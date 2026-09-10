# Coconut interaction design

Coconut has one home screen. Pinned bots sit above a fixed Inbox row. Other bot conversations follow in a compact list. There is no tab bar.

Inbox contains only pending decisions across bots. Each row opens the decision and its thread.
Resolved decisions leave Inbox. Results and history remain in bot conversations. There is no separate Review screen.

Long-press a bot to pin, unpin, or archive it. Pins persist on the Mac. Search finds bots by name, role, or message text.

Each compact Inbox row shows the bot, age, decision title, and one preview line. Inbox has one list and no filters.
Group pending decisions by thread. Place urgent decisions first, then older decisions.

A thread keeps its identity after a task finishes. A later reply starts a continuation within that thread. It never steers another thread.

A question about a pending decision links to that decision. The question does not approve, reject, or replace the proposal.

Hermes generates decision titles, explanations, and choices from the task and the person's instructions. The app renders those stored choices without adding fixed outcomes.

Tap a choice to submit that exact option. The card contains its title, a short explanation, and the joined choice list.

Keep context, details, modification, and rejection in the card menu. Do not add a second approval footer to a card with choices.

Details show resources, consequences, and exact proposed content. Asking for context keeps the same decision pending.

The service records the predicted choice before the response. The history records the selected choice, person, correction, and observed result.

The bot conversation includes its task history. Inbox opens the same stored messages filtered to one thread. The views do not copy decisions.

Show each answer once. Use cards for distinct outputs or decisions. Task completion alone does not create a card.

While a bot prepares a reply, show its small avatar and a progress indicator. Hide generic progress prose and duplicate completion summaries.
Record permission changes in the audit history. Do not add routine settings notices to chat.

Keep specific tool activity expandable. Preserve errors, cancellation, decisions, files, links, and receipts. Presentation filters do not delete stored history.

Group decision choices in one list with dividers. Use compact letter markers. Keep action details in the card menu.

The composer stays compact. Bot conversations show whether a message steers current work or queues a new task. Thread replies stay in their thread.

Place attachment, text, and send controls in one capsule. Show Send when the draft has content. Show Stop during work when the draft is empty.

Do not label a message with the current bot's name twice. Show recipient labels only when the message addresses other bots.

Tap the bot name to open its avatar, name, optional label, description, notifications, and routines.

The description edits native SOUL.md. Routines show a name and a short native schedule.

Hermes saves standing chat requests in its native profile memory and applies them to later tasks.

The bot can update native memory through chat. Access settings remain explicit.

Do not hardcode a Do nothing choice. Generate that choice when the person's request or task calls for it.

Routines accept a native Hermes schedule string. The Mac parses the schedule.

The Mac runs routines while the phone app is closed. Pausing a routine stops future occurrences. Existing tasks retain their history.

Typing `@` opens a compact bot picker. Selected recipients remain visible above the composer. Each recipient starts work in the same conversation.

The service routes mentions by stable bot IDs. Duplicate names require a picker selection. Editing or removing a mention updates its recipients.

Mentioning a bot keeps pending decisions open. It requests work without answering the proposal.

Both bots show their exchange as a collapsed row. Tapping either row opens the same read-only exchange. Incoming messages never change the open conversation.

There is no Task screen. Inbox items and notifications open the conversation that contains the decision card and its context.

A compact label names the executor before steering or stopping work. Shared conversation ownership does not change execution or permission ownership.

Model selection belongs only in app settings and bot settings. Hermes owns the shared default model and effort.
Each bot uses that default unless the person saves an override. Show the actual inherited values beside the default toggle.
Use model and effort choices from the Hermes catalog. Do not accept a freeform model name.
Connection health remains separate from task state. A closed phone app does not stop Mac work.

Support iPhone access over cellular and other Wi-Fi networks. Local execution and storage must not restrict access to the home network.

## Grok Bot observations

Computer use exposed the live Grok Bot window on this Mac. The user later supplied three mobile screenshots as the primary visual reference.

The mobile home uses large pinned avatars above compact contact rows. Small profile, search, and create controls replace a large title and dashboard sections.

Coconut adopts that hierarchy. Inbox remains a distinct row below the pins. Its count represents pending decisions, not unread messages.

Bot rows and pinned bots show separate unread markers. Status badges distinguish working, waiting for a decision, scheduled, and failed states.

The composer uses one compact row. Bot identity remains visible while conversation content receives most of the space.

The mobile conversation uses grey message bubbles and compact choice cards. Coconut keeps action details expandable and records outcomes from backend events.

App settings contain Mac details, native model defaults, native access defaults, and per-bot overrides.

No private conversation content appears in this document.

## Inbox density

The user requests Gmail-style density and no separate Review tab. Keep one pending-decision list. Use a small avatar and three short text lines.

Avoid large group headers, filters, and repeated status rows. Completed work stays in bot conversations. Keep attention marks tied to backend state.

The current API tracks unread counts per bot. It does not track unread state per thread. Pending marks must not claim otherwise.

Mobbin MCP was absent from the session tools. The public Explore link returned the logged-out home page. No Mobbin screen reference was available.

The fallback reference is [Google's current Gmail listing](https://apps.apple.com/us/app/gmail-email-by-google/id422689480). Its first screenshot shows compact sender, subject, and preview rows.

[Mobbin's setup page](https://mobbin.com/mcp) describes the connection needed for a future reference pass. This task does not create a subscription or new account.
