# Information architecture audit

An audit of the iPhone app and the Mac window (`ios/Coconut/*.swift`, shared by the Mac target through
`mac/Sources/CoconutMac`), and the plan that follows from it.

## Current map

iPhone tabs (`HomeTab` in `CoconutApp.swift`):

| Tab | Root view | What it holds |
| --- | --- | --- |
| Inbox | `DecisionInboxView` | One pending decision card at a time. |
| Chats | `HomeLaunchView` | The Coconut chat, the starred agents as tiles, then every agent, team, named and direct chat. Archived agents. Create agent. |
| Business | `BusinessHomeView` | Functions, projects, reports, the ledger. |
| Library | `LibraryView` | Every page of every agent, grouped by agent. |

Mac sidebar (`MacSidebar` in `MacMainWindow.swift`), top to bottom: the Coconut chat, Inbox, Library, Business,
"Functions" (4 rows), "Projects", "Unreads", "To-dos", "Starred", "Notices", "People", "Agents", "Archived".

Other places: the Team screen (`TeamView`) opens from the title of a team chat on the iPhone and from the Settings
window on the Mac. `AppSettingsView` (Mac connection, Model, Access, Notifications, Connections, Team, the agents,
Parallel work) has no entry point on the iPhone since the gear went. The Coconut chat has a "This device" menu
(pairing, notifications, sign-ins).

## Problems

1. No start page. The app opens on one decision card. Unread chats, blocked work and a broken agent are on three
   different screens, so nothing answers "what needs me now".
2. Nothing warns. A rate-limited agent is a quiet line in a chat row. A blocked To-do is a red symbol in a folded
   sidebar section. A stale connection is a grey dot. A card that waits for hours looks like a card that arrived now.
3. An agent is only a chat. Its work (To-dos), its pages (Library), its routines and its settings are on four
   screens: a sidebar section, a global tab, and a settings page behind the chat title. There is no place that is
   "the agent".
4. Duplication. Unread chats are in "Unreads" and the Inbox badge counts only cards. The Library lists pages by agent,
   and the Mac inspector lists them by agent again. The agents are listed in Chats, in Settings and in the sidebar.
5. Dead ends. `AppSettingsView` is not reachable on the iPhone, so Model, Access, Connections and Parallel work
   defaults cannot be opened there. `SettingsAccessTests` still taps a gear that is gone. The Team screen is not
   reachable on the iPhone without a team chat to tap, so a person without a team cannot make one.
6. Wrong place. Business takes a main tab and eleven sidebar rows before the first chat, though most people have no
   business data. Notices of a team sit between starred chats and people. The person has no page: the name, this
   device and the version are spread over the Coconut chat menu and a hidden Settings sheet.
7. Naming. "Bots" remains in identifiers and a few labels next to "Agents".

## Plan

Five places, the same on the iPhone tabs and at the top of the Mac sidebar:

1. **Home** — "Needs attention" (red, only when a rule breaches), "Unread" (each chat with unread messages and each
   pending card, newest first), "In progress" (running turns, open To-dos, active notices). Replaces the Inbox tab and
   the Inbox row. A card row opens the existing card views.
2. **Chats** — every conversation. The Coconut chat stays on top, then Starred, then the rest. On the Mac this is
   the rest of the sidebar: Starred, Unreads, People, Agents, Archived.
3. **Agents** — an agent is a project. The list shows each agent with role, status, open To-dos and last activity.
   The agent page has a header and the sections Chat, To-dos, Pages, Routines and Settings. The Library tab goes;
   its pages live under their agent. Starred pages stay in the sidebar.
4. **People** — the teams, their chats, their members (a row opens the direct chat), the notice boards, invites, and
   the Team screen for the rest.
5. **Profile** — the person, this device (the Mac connection, notifications and work hours, sign-ins and
   connections), the defaults (Model, Access, Parallel work), the Coconut chat, Local or Cloud, Business, and the
   version. It takes the rows of `AppSettingsView`, so no Settings screen remains.

Business leaves the tabs and the sidebar. It has no agent of its own, so Profile links to it.

Rules of "Needs attention" live in one type, `AttentionRules`, with named thresholds:

| Breach | Rule | Opens |
| --- | --- | --- |
| Waiting on you | A pending card older than 4 hours. | The cards. |
| Agents that cannot run | `health != nil` on an agent that is not archived. | The agent page. |
| Blocked or failed work | A To-do that is blocked, or a task that failed, in the last 24 hours. | The agent chat. |
| Data is stale | Not connected, or the last sync is older than 10 minutes. | Profile, the Mac connection. |
| Team notices | An active Status or Deadline that is unseen, or a Deadline that ends within 24 hours. | The board. |

Steps, one commit each: this audit; `AttentionRules` and the banner; Home; Agents and the agent page; People and
Profile with the five tabs and the new sidebar; screenshots and the iPhone flow test; the result below.
