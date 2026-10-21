# Coconut interaction design

Coconut has one home screen. Pinned bots sit above a fixed Inbox row. Other bot conversations follow in a compact list. There is no tab bar.

Inbox contains only pending decisions across bots. Each row opens the decision and its thread.
Resolved decisions leave Inbox. Results and history remain in bot conversations. There is no separate Review screen.

Long-press a bot to pin, unpin, or archive it. Pins persist on the Mac. Search finds bots by name, role, or message text.

The home bar has separate circular controls for settings, search, and bot creation. Keep space between each control.

Sending immediately clears the composer, displays the message, and animates the bot while delivery starts. Keep the composer available for another draft.
Save outgoing messages on the phone until the Mac confirms them. Match confirmations by request ID, never by text.
Show an inline retry control when delivery is unconfirmed. A retry keeps the same request ID to prevent duplicate work.
If the Mac rejects a message, preserve its reason and offer editing before sending a new request.

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

While a bot prepares a reply, animate its coconut character. Hide generic progress prose and duplicate completion summaries.

Characters use eight Bloub outlines and ten colors. Keep the outlines smooth and preserve Coconut’s shell texture. New bots receive a generated character. Users can change its shape and color in bot settings. Idle characters share a raised, right-facing pose and blink at separate intervals. Thinking changes gaze, eyelids, and mouth shape, with pauses between expressions. Working characters use this motion in pinned tiles, contact rows, and conversations. Reduce Motion keeps the character still and preserves the green activity dot. The native SwiftUI drawing uses [Bloub’s shape profiles](https://github.com/jeremy-prt/bloub/blob/b4bb3c1b5f93c7b87a2e8d620f667c4093d97749/src/bot/skins.ts) and curve method under its MIT license. The app bundles the license. The supplied Grok Bot recording informs the expressions.
Record permission changes in the audit history. Do not add routine settings notices to chat.

Keep tool activity out of the conversation UI. The bot animation shows progress. Show failure notices as plain messages. Preserve decisions, files, links, and receipts. Presentation filters do not delete stored history.

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

Typing `@` opens a compact bot picker. A selected mention identifies a bot for the current bot. It does not send or share the message, start work, or change the recipient.

The service resolves references by stable bot IDs. Duplicate names require a picker selection. The current bot receives the reference and decides how it relates to the human request. Delegation requires a separate tool call when the request calls for it.

Mentioning a bot keeps pending decisions open. The mention alone does not answer a proposal or authorize work.

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

### Account connections

Users connect Google from Settings → Connections in Coconut. The system sign-in sheet returns directly to the app. Google is one shared connection for all bots on the paired Mac. The Mac stores and refreshes credentials once. Never ask users to run login commands, copy callback URLs, or configure a bot from Terminal. Publisher OAuth setup is separate from user onboarding.


## Work

Use three stable tabs: Bots, Work, and Inbox. Hide the tab bar inside every conversation.
Work lists active pull requests that a bot creates or works on, or that the person asks to follow.
Show one row per GitHub URL, with its title, repository, number, draft state, bot, and observed CI state.
The PR link opens GitHub. The bot name opens its conversation.
Keep draft state separate from CI. Passing checks do not establish merge readiness.
Keep merged and closed PRs below open PRs in Work. Show purple Merged and gray Closed labels. Preserve these confirmed outcomes after polling stops. Keep their artifacts in chat.
Mark failed or old observations as out of date. Refresh every 15 seconds while Work is visible and every minute otherwise.
Keep process logs, activity counts, and automatic CI decision cards out of Work.
Use each bot's own gh credentials. The Coconut adapter tracks PRs without changing Hermes source.

Use green for passed checks, yellow for running checks, and red for failed checks. Keep status text for clarity without color.
When CI passes on a tracked, non-draft PR, resume its owning bot once for that head.
The owner verifies the current head, reviews, findings, conflicts, and existing approval requirements.
For delegated work, return the verified result to the requesting bot. That bot offers one merge decision in chat and Inbox.
A passing CI observation never authorizes a merge. Recheck the head after explicit approval.
Keep pending decisions, cancelled work, archived bots, and reset conversations intact.

Inbox cards use compact padding and body-size titles. Keep option explanations, recommendations, and 44-point touch targets. Preserve the swipe stack and direct chat link.

### Decision card language

Coconut supplies the same writing rules to every bot on every turn. A card names the choice and its practical effects.
A card is as short as it can be while it stays clear on its own. Every sentence helps the person decide.
The context is one to three connected sentences, not a stack of clipped statements. It mentions what approval does not do only when the person would otherwise expect it.
The risk level and its one-sentence reason stay on the card. A risk reason always has its level, so the card never shows a bare "Risk" heading.
A merge card always states a low, medium, or high risk, on the card or on its merge choice. Coconut rejects a card that breaks these rules.
A merge card also shows the size of the PR, such as "+171 −25 · 4 files". Coconut refreshes the size with each PR poll.
Coconut rejects card text over its limits: 400 characters of context and 200 characters of risk, uncertainty, or option explanation.
The bot uses plain language and states the decisive fact once. The card names what changes and where.
The card distinguishes preparation from execution: local work, a draft PR, a merge, a deployment, or a direct setting change.
The bot verifies the delivery method before asking. It does not imply that a PR exists or that approval immediately changes production.
Option labels name the action. Their descriptions explain the result when the label alone is insufficient.
Low-level commands, steps, and supporting evidence stay in the original chat and action. Cards have no evidence section.
Cards retain uncertainty, costs, affected people, public changes, irreversible effects, and approval limits when these affect the choice.
Coconut does not shorten existing decisions or rewrite their meaning after the bot creates them.
