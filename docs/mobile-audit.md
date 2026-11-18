# Mobile audit

An audit of the iPhone app, screen by screen, on November 17, after the consistency work of November 16 (one
vocabulary, the space tokens, one page scaffold, one icon per meaning, the shared rows, Finance as a tab). It lists
what was still inconsistent, the fix, and where the fix is. The views are `ios/Coconut/*.swift`. The Mac target
compiles the same files, so each fix was compiled for the Mac too.

Main changed during the audit. It started on three tabs (Home, Finance, Profile) and agents with letters. Main now
has five tabs (Finance, Support, To-dos, Chats, You), a chat hides the tab bar, an agent is a project without a
letter, and the pages for the model, the access and the parallel work of an agent are gone. The findings are checked
against that main.

## How the screens were taken

An iPhone 17 simulator with iOS 26.5 and a debug build. The app starts on one screen with the preview data:

```sh
xcrun simctl launch <udid> com.gumroad.coconut --demo --open <place>
```

`python3 scripts/qa.py open <place>` does the same in the QA simulator. The places are in `showPreviewPlace`
(`ios/Coconut/CoconutApp.swift`). This audit added `agent-usage`, `marketing`, `ledger`, `report-<key>` (overview,
accounting, support, risk, engineering) and `project-<key>`. Further arguments: `--team-preview` (a team with
notices), `--attention-preview` (Needs attention), `--bottom` (the end of a page), `--scrolling` (a chat as during a
scroll, with the pinned day pill) and `--period daily|yearly` (the cut of History).

The screenshots are outside the repository:

- Before: `~/projects/coconut-shots/mobile-audit/before/` (the three tabs, from the start of the audit)
- After: `~/projects/coconut-shots/mobile-audit/after/` (the five tabs, with the fixes)

A file has the name of its place (`chat.png`, `usage.png`, `profile-mac.png`). `dark-<place>.png` is dark mode,
`<place>-bottom.png` is the end of the page, and `tab-<name>.png` is the root of a tab. `chat-draft.png` is a chat
with a draft of two lines: the message box is higher, so the first day pill lies under the title, as in
`before/chat.png`.

Seen in light mode: Home (also with Needs attention and with a team), the cards, a chat, the chat deck, the list of
the projects, a project with its settings and its usage, All pages, a page, Archived, Live, Spend, Usage, Finance,
the functions, each report, History in its three cuts, Finance's Projects, the work of a project, the ledger, Profile
and each of its pages, the Team screen, the notice board, a team chat, a direct chat, the pairing screen, and the
roots of the five tabs. Seen in dark mode: the roots of the five tabs, a chat, the cards, the deck, the list of the
projects, a project, All pages, a page, Live, Spend, Usage, Accounting, History, the ledger, Notifications, the
notice board and a team chat.

## Findings

| Screen | What is wrong | Fix | Status |
| --- | --- | --- | --- |
| Chat, team chat | The day pill ("Today") of a row that scrolled under the bar showed half behind the title pill. | The pill in its place hides when it passes the line of the pinned pill. The pinned pill shows the day during a scroll, below the title. | Fixed in f701b02 |
| Usage | Text that is not data: the title twice, the subtitle "All agents on your Mac", a note under each number, the legend "In, Out, By day", a paragraph at the end, and the same symbol on each model row. | The bar has the title. A number has its name only. The chart draws one number for each bar, so it needs no legend. A note shows only when the cost did not load ("No price"). | Fixed in 1a2c06f |
| Usage | The last date under the chart was cut ("No…"), and so was the line of each row ("665 turns · 295M tokens ·…"). | A date for each fifth part of the range, where it has room. A narrow row leaves the time out. | Fixed in 1a2c06f |
| Usage, Spend | Each page had its own spinner and its own "No usage yet" state for a failed load. | `BoardLoading` and `BoardLoadNotice`, with one way to try again. | Fixed in 1a2c06f, 3b8d072 |
| Usage, Spend | The list that the Projects page calls Projects had the title "Agents". | "Projects". | Fixed in 3b8d072 |
| Usage, Spend, in the demo | The demo counted each further chat as a project: five rows with Releases and Support twice, and a total above the sum of the projects. | The demo reports the usage of a project with a row for each chat, as the Mac does. | Fixed in 586d18d |
| Project, Usage card | Notes under the numbers ("Estimate at list prices", "8.9M tokens") and a caption under the chart. | Numbers with their names. | Fixed in 1a2c06f |
| Spend, Live | Their own scroll view, padding and width, not the page of the other tile pages. | `BoardPage(width: .wide)`. `BentoPage` is gone. | Fixed in 3b8d072 |
| Live | The working line was cut in two places ("Running a com… · 4m 23s · Previe…"). | The line leaves the model out when the tile is narrow. | Fixed in 7b7f231 |
| Projects | A cost of today wrapped onto two lines ("$8.4" over "0"). The queue of a lane was cut ("1 running · 2 waiting · 1 needs you · 3…"). | The age and the cost keep one line. The queue takes a second line. | Fixed in e175a21 |
| Projects | Spend was under Library, apart from Live and Usage. The Usage row had a line of help that was cut. The page had its title in the content and none in the bar, so the title ran under the back button. | Library has the pages and the archive. Usage has Live, Spend and Usage. An iPhone has the title in the bar, as each pushed page. | Fixed in e175a21 |
| Projects | A narrow row cut the line under the name of a project ("1 p…"), and the small chart of the usage of a project had no dates. | A narrow row shows the role of the project instead of its counts. The small chart shows its dates. | Fixed in 5d8858b |
| Projects, project | Work that runs had the play symbol in the queue and the dotted circle everywhere else. | The dotted circle. | Fixed in e175a21 |
| Project | "Chats 1" stood over two chats: the count was the unread messages, where each other section counts its rows. | The count of Chats is the chats. A row has its unread messages. | Fixed in 28a6e4c |
| You | No bar, so the page ran under the clock when it scrolled. | The bar has "You", as Finance and To-dos have their names. A person without a name has the name of the device in the header, not "You" a second time. | Fixed in d8372cd |
| You | The row "Sign-ins and connections" opened a page named "Connections". Usage and Spend sat in a section named Coconut, and Version had a section of its own. Spend had no number until another page had loaded the usage. The help under Ask Coconut is longer than one line. | The row is "Connections". Usage is a section with Spend and Usage, as on the Projects page. Version is in Coconut. The page loads the usage. The help takes a second line. | Fixed in d8372cd |
| Your Mac | "Restart Agents" in title case. The header "When the app closes" was smaller and lighter than "Connection". | Sentence case. The header has the look of each other header. | Fixed in 480c428 |
| Menus | Title case was left in "Copy Link", "Open in Browser", "Copy Message", and in "Send Now", "Move Up" and "Move Down" of a queued message. | Sentence case. | Fixed in 480c428 |
| Finance | The Functions menu said "Reports" for the page and the tile named "History". | The menu says "History". | Fixed in 313db62 |
| History | The axis wrote "500,000" where each other chart writes "$500.0K", and it stood at the leading edge. The daily cut wrote a sentence for the average, and its thirty dates ran into each other. The yearly chart had a line between the bars that the two other cuts do not have. | The axis uses `BusinessFormat.money` at the trailing edge. "Daily average" is a name and a number, as on Finance. The daily cut has a date for each seventh day. The three cuts have one axis. | Fixed in 313db62 |
| Marketing | A segmented control with one segment, "Work". | A function without a report shows no control. | Fixed in 313db62 |
| Ledger | A system list among boards, with a line of help as its first row. The demo showed "Connect your Mac to continue." | A board with the rows of Work and Projects. The demo has a small synthetic ledger. | Fixed in 1685857 |
| Notice board | The kind in capitals ("STATUS") where the banner of the chat writes "Status". In dark mode the page was black, not the page color of the Team screen. | Sentence case, and the page color of the settings pages. | Fixed in ff86047 |

Three more findings left with the pages that main removed during the audit: the switch "Use default" on the Access
page of an agent, the row "Name, icon and instructions", and the cut value of Parallel work on Profile.

## Not changed

These need a choice of the person, or belong to a run that works on them now.

- Two pages are named Projects. Finance has Projects (the repositories of the business, "Gumroad ·
  antiwork/gumroad-private"), and the list that was Agents is Projects since this morning. One of them needs another
  name.
- Agent or project. Main says project for the thing in a list ("New project", "Delete project"), and about 80
  strings in 26 files still say agent: the settings of a project, Notifications, the team screens, "Deleted agent" in
  a chat. This audit changed only the titles above. A rule would settle the rest: project for the thing, agent for
  the worker.
- Support has two doors: the Support tab, and Finance, Functions, Support.
- Chats is the one tab root without its name in the bar. Chats, the message box with its project chip and each New
  chat path belong to another run. Seen there and left: a held message offers "Send in New Chat" in title case.
- Spend keeps the legend of Cost by day. The bars are split by project, and the names are the labels of the split.
- All pages lists the pages in its own rows with the report tiles above them. The rows are not the shared rows yet.
- Settings pages (Notifications, Connections, the Team screen) are grouped system forms on the grouped page color.
  The boards are sections on paper. Both are used on purpose: a form edits, a board reads.
- Spacing with a number in place of a token is left in the chat, the cards, the Library and the Team views. Most of
  the numbers are equal to a token.
- The demo: the image of a Live tile does not load, each chat shows the same spend, and yesterday is $252.5K on
  Finance and $274,000 in History.
