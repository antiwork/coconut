# Business dashboard audit

An audit of every business dashboard of the iPhone app and the Mac window, and the plan that follows from it. The
views are `ios/Coconut/Business*.swift`; the Mac target compiles the same files.

## The screens

| Screen | View | Opens from | Data |
| --- | --- | --- | --- |
| Business home | `BusinessHomeView`, `BusinessHomeContent` | Profile, "Business" | `overview` report (`home.json`) |
| Business report | `BusinessReportView` + `MoneyDashboard` | Home GMV tile | `overview` (`home.json`) |
| Accounting report | `BusinessReportView` + `MoneyDashboard(accounting:)` | Home money rows, Accounting function | `accounting` (`home.json`) |
| Support report | `BusinessReportView` + `SupportDashboard` | Home attention rows, Support function | `support.json` |
| Risk report | `BusinessReportView` + `RiskDashboard` | Home "Risk review" | `risk.json` |
| Engineering report | `BusinessReportView` + `EngineeringDashboard` | Home "Pull requests" | `prs.json` |
| Function | `BusinessWorkspaceView` | Functions menu of Home | Overview, Work, Library |
| Work list, work, projects | `BusinessWorkListView`, `BusinessWorkDetailView`, `BusinessProjectsView` | Function, Projects | GitHub issues |
| Ledger | `BusinessLedgerView`, `BusinessLedgerFileView` | Accounting, Library | Beancount files |
| Report data | `BusinessDataExplorer` | "Explore all data" of a report | The whole JSON |

No business screen renders HTML. The web dashboards of `antiwork/dashboards` are only the publisher of the JSON.
The `WKWebView` pages of the Library are pages of agents, not dashboards, and are not part of this audit.

Before images: `~/projects/coconut-shots/bento/before/`.

## What looks like a web page

1. One column of full-width cards. Each screen is a stack of equal cards from top to bottom, as a responsive web
   page is. Nothing is bigger than anything else, so nothing leads. A 1100 point window shows three numbers and one
   chart, then needs a scroll.
2. Marketing headers. Each report opens with a tracked uppercase eyebrow ("GUMROAD · OPERATIONS") and a headline
   that sells ("The business, at a glance.", "Every number has a ledger.", "Keep the queue moving."). The
   navigation title already names the screen. This is landing page copy, not an app.
3. Tinted metric cards with a status dot. Light blue, purple and pink cards with a 6 point dot in the corner are
   the stock look of a web admin template. The blue and the purple are system colors, not colors of Coconut.
4. Chart defaults. The grouped bars use green, system blue and system purple with a legend row under the chart.
   The resolution histogram uses a strong orange gradient with dashed grid lines. Charts have full axes where a
   sparkline would do.
5. "↗" on every card. Each link card ends with `arrow.up.right`, the web sign for "opens in a new tab". These
   links push a screen of the app, where the sign is a chevron or nothing.
6. Three radii and three card styles. Home uses `BoardStyle.surface` with radius 24. Reports use ink at 2.5%
   opacity with a hairline and radius 18. The stale notice uses radius 12. No shape is continuous.
7. Controls from a form. The conversation list has a bordered search field and a pop-up button inside a card. The
   risk queue has a pop-up for five queues.
8. Small numbers. Home shows the money totals as `title3` at the end of a row. The largest number on a report is
   34 points, the same in each card.

## Data and information architecture problems

1. Home never shows the 30 day GMV total. It shows the change and the last day, but not the number the change is
   about. The published run rate and daily average are not shown anywhere.
2. Pull requests read "Not published" on Home. Home reads `prs.total`, but the published snapshot has
   `prs.data.<repository>.pullRequests.totalCount`. Only the test fixture has `total`.
3. The high priority row never has its detail. Home reads `high_priority.top[0].title`, but the snapshot publishes
   `numbers` and `titles` by number.
4. The watching queue of Risk has empty rows. Those records have no name, no username and no balance; they have
   an email, a revenue, an unpaid balance and a note. The row shows an empty title and "—".
5. Risk hides most of its snapshot: the split by payment platform, the count of transactions next to the disputes,
   the outcomes of 90 days, the suspended accounts and the negative balances.
6. Engineering shows the additions of a pull request but not the deletions, the author, the draft state, the
   review or the checks. The count is the accent color, which means "attention" elsewhere, also for a zero.
7. Long lists are not lazy. Support publishes a few hundred closed conversations and Risk two hundred accounts per
   queue. Each is a plain `VStack`, so a switch of the picker builds every row.
8. Team activity draws bars without their counts.
9. Months and days show as raw text ("Books closed through 2026-10").
10. The overview and the accounting report do not show the published margins or the write-off, which Home does.
11. The source dates of the sections (`sections.<key>.as_of`) are day texts, not timestamps. They do not parse, so
    they are never shown.
12. The demo had no risk and no engineering data and no money section, so those screens were empty in a demo and in
    screenshots. The first commit of this work fixes that.
13. The Functions menu is the only way from Home to Accounting, Marketing, Support and Projects. It is one toolbar
    button, so the functions are not visible on the page.

## The plan

One grid system and one tile style for every dashboard, in a new `ios/Coconut/Bento.swift`.

- **Grid.** `BentoGrid` is a SwiftUI `Layout`. A Mac width has 12 columns and an iPhone width has 2. A tile has a
  column span and a row span, and a different one for the narrow grid. Rows have one height, so the edges of the
  tiles line up as on a Geckoboard wall. A tile that spans the whole width can take the height of its content, for a
  list.
- **Tile.** `BoardStyle.surface` on `BoardStyle.paper`, a continuous corner of `BoardStyle.Radius.large`, a hairline
  of `BoardStyle.line`. A tile with a meaning gets a soft wash of its color: green for money, the accent for
  attention. A tile that opens a screen is a button that dims when pressed and lifts under the pointer on the Mac.
- **Numbers.** SF Pro Rounded, semibold, tabular figures, in three sizes: 52 for the hero, 34 for a tile, 24 for a
  small one. The label is one quiet line over the number, with its symbol.
- **Charts.** Swift Charts only. Sparklines and small bars without axes inside tiles. A full chart has three quiet
  grid lines and four dates. Colors are the colors of Coconut: green, the ink, the accent for a loss or a breach,
  and the blue and the purple of the letter faces for a second and a third series.
- **Copy.** No eyebrow and no headline. One status line: the dot, the snapshot time, the currency, "Sample data".
- **Chevron or nothing.** A tile that opens a screen has no "↗". Only a link to GitHub has it.

Per screen:

| Screen | Grid at a Mac width |
| --- | --- |
| Home | GMV hero 8×3 with the 30 day total, the change and the chart next to Attention 4×3. Revenue, EBITDA and net income 4×2 each with the bars of twelve months. Risk, pull requests, run rate and daily average 3×1. The functions and Projects 3×1. |
| Business report | GMV hero 8×3 next to revenue, EBITDA and net income 4×1. The three queues 4×1. Revenue and profit 8×3 next to run rate, daily average and the books 4×1. |
| Accounting | Revenue, EBITDA and net income 4×2 with bars. Revenue and profit 8×3 next to the margins, the write-off and the books. |
| Support | Open, unassigned and closed 4×1. Incoming volume 8×2 next to the median and the 90th percentile. The histogram 6×2 next to the team 6×2 with counts. The conversations as a lazy list over the whole width. |
| Risk | Review queue, disputes and amount 4×1. Chargebacks 8×2 next to the dispute count of the transactions and the outcomes. Platform split, suspended accounts and negative balances. The five queues as a segmented control over a lazy list. |
| Engineering | Open pull requests in total, then one tile per repository. A list per repository with the author, the lines, the draft state, the review and the checks. |
| Report library | Small tiles with a symbol, a title and one line of what the report holds. |

Rules that stay: the client does not compute a total, a change or a margin (`BusinessHomePresentation`). A missing
value shows as "—" or "Not published", never as zero. A failed or stale source never reads as "All clear".

Fixes that ship with the redesign: problems 1 to 11 and 13 above.

## Result

The redesign is in `Bento.swift` (the grid, the tile and the numbers), `BusinessHome.swift` and
`BusinessDashboard.swift`. `BusinessDashboardSnapshotTests` renders each screen whole at a Mac width and at an iPhone
width, in light and in dark; set `TEST_RUNNER_COCONUT_BUSINESS_SHOTS` to a folder to keep the images.
`MacWindowSnapshotTests` adds four windows of Business next to the sidebar. `BusinessBentoTests` covers the placement
of the grid and the fixes of the data.

Not changed: the work list, a work item, the projects, the ledger and the data explorer. They are native lists and
forms already.
