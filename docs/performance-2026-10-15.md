# Smaller state reset, October 15, 2026

A phone that was away for more than 1,000 events (about 3 hours of live use) gets a content reset from `/v1/updates`.
A phone that pairs gets the recent state from `/v1/state`. Both carried every Work card and every task those cards named.
Most of that data is finished Work that Task details hides behind **Show older**.

## Change

- The app sends `X-Coconut-Work: recent`. The server then leaves out each Work item whose cards all finished more than
  7 days ago. This is the window that Task details already uses, plus one hour for a phone clock that runs behind the
  Mac. The tasks that only those cards name drop out too. An item stays when a message in the reset shows one of its
  cards.
- The response lists the bots that have such Work in `older_work_bot_ids`. Task details for those bots shows
  **Show older**. A tap loads `GET /v1/bots/{id}/work/older` and shows the items.
- An older history page returns the cards that its messages show, so a conversation shows the same cards.
- A reset from `/v1/updates` has no routines. Every app that uses this protocol replaces them with the routines of its
  native settings. The first state after pairing keeps them.
- Older servers ignore the header. Older apps do not send it and get all Work.

## Method

The data is a consistent dump of the Mac mini database and of the Hermes Kanban database from October 15.
The bench serves it with the real backend on a laptop. The Kanban stub returns the Hermes task rows that the adapter
snapshot returns: status, last activity, and run summary. The Mac mini is about 2 to 3 times slower than the laptop.

An earlier bench used an empty Kanban stub. It showed 1,016 native cards as waiting and left out completion summaries,
so it reported 3.7 MB. With the Hermes rows, 1,412 cards are complete and the reset is 4.19 MB.

The app numbers decode the responses with the app's own `Models.swift` in an optimized build on the same laptop.

## Results

| Measurement | Before | After | Change |
| --- | ---: | ---: | ---: |
| Reset from `/v1/updates`, JSON | 4.19 MB | 1.54 MB | −63% |
| Reset from `/v1/updates`, gzip | 771 KB | 309 KB | −60% |
| Recent state after pairing, JSON | 4.19 MB | 1.99 MB | −53% |
| Recent state after pairing, gzip | 771 KB | 441 KB | −43% |
| Work cards in a reset | 1,607 | 439 | −73% |
| Tasks in a reset | 1,397 | 470 | −66% |
| Server JSON encoding and gzip of a reset | 80 ms | 35 ms | −56% |
| App decode of a reset | 105 ms | 36 ms | −66% |
| Saved `snapshot.json` | 3.42 MB | 1.72 MB | −50% |
| App decode of `snapshot.json` at launch | 34 ms | 14 ms | −59% |

The server build time of the reset did not change measurably: 1.1 to 1.7 s on the laptop before and after, depending
on load. Grouping the Work items adds about 30 ms, and the smaller task read saves about as much. Decision origins and
message pages take most of the time. The saved state keeps the routines from native settings in both cases.

An older history page that shows pull request cards now projects those cards: Desk's first 8 older pages took 62 ms
each instead of 9 ms and brought 12 cards. Most of the time is the projection's read of all pull request cards.

**Show older** loads one bot's older Work:

| Bot | Cards | Tasks | JSON | gzip | Server time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Desk | 662 | 474 | 1.38 MB | 184 KB | 0.31 s |
| Gumroad Dev | 458 | 328 | 0.97 MB | 123 KB | 0.30 s |
| Coconut Care | 146 | 108 | 0.31 MB | 43 KB | 0.31 s |
| Signal | 3 | 3 | 7 KB | 2 KB | 0.28 s |

## Task details shows the same items

A check ran the app's own `TrackedWorkItem` code for each bot on the old reset, on the new reset, and on the new reset
plus the bot's older Work page. Before the load, the active, idle and recently finished items are identical. After the
load, all four groups are identical, including the order and the text of each row. The check also passed 1, 2 and 6
days later, when some kept items move into the older group.

## Limits

An update after a decision or artifact event still resends every Work card, so older cards come back to the app until
the next reset. The saved state and the launch decode stay small only after that update path changes.
A conversation shows one "In <bot>" link for each delegated task that the app holds. With fewer old tasks in the
reset, a conversation shows fewer of these duplicate links.
