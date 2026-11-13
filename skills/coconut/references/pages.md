# Pages of the Library

A bot saves a page with `coconut_save_document`. The person reads it in the Library of the Coconut app, on the iPhone and on the Mac. Coconut sets the look of each page. The bot writes the structure. This reference gives the recipe of a good page, the Markdown of each block, and one complete example.

## The recipe of a report page

1. The first paragraph states the point in one or two sentences. The page shows it large.
2. Then the key numbers: one table with one row of numbers. Each number becomes a large tile with its header as the label. 2 to 4 key numbers work best. The tiles sit in even rows.
3. Then sections. Each section has a short heading and one table or one chart.
4. A callout only for one decision or one warning, in one or two sentences.

The Library shows the title above the page. Do not start the content with the title.

## The blocks

| Block | Markdown |
|---|---|
| Key numbers | A table with a header row and exactly one row of numbers |
| Table | A table with a header row and its rows |
| Chart | A ```` ```chart ```` block with JSON |
| Callout | A quote that starts with `[!NOTE]`, `[!TIP]`, `[!IMPORTANT]`, `[!WARNING]` or `[!CAUTION]` |
| Diagram | A ```` ```mermaid ```` block: flowchart, sequence, timeline, gantt or pie |
| Task list | `- [ ] open` and `- [x] done` |

A number is a sign, a currency sign, digits with separators, then a percent sign or one letter such as k or M. A unit of at most 16 characters can follow after a space. For example `412`, `$8,240`, `1.2%`, `-3`, `12k`, `6 days`. A cell with other text is not a number.

A chart takes these fields: `type` (`bar`, `line`, `area` or `dot`), `data` (a list of rows), `x` and `y` (the names of two fields of a row), and the optional `series`, `title` and `y_label`. Write a day as a date (`2026-11-02`), not as a day number. The chart then writes the labels as dates, and never more labels than fit.

A callout has an optional title after the marker on the same line:

```
> [!WARNING] Refunds
> The refund rate is above 2% for the first time this year.
```

## What the phone and the Mac do with a table

- A table that fits its column stays a table. A column of numbers aligns right.
- A table that does not fit is a list of records. Each row is one record.
- The first cell of a row is the name of the record, in bold.
- A short last column goes to the end of the first line of the record. Put a status there.
- The other cells follow on a quiet second line, with a dot between two cells.
- A number on that line has the label of its column before it, for example "Orders 168".

So put the column that names each row first, and a short status last.

## The rules for content

- A column header has at most 16 characters.
- A column holds one fact. Do not join several facts with dashes in one cell.
- Write an age as "6 days", not as a date.
- Write the days of a chart as dates.
- A heading does not repeat the title of the page.
- A heading has no count in it. The counts go in the tiles, the tables and the charts. A time window is fine, for example "Closed in 24 hours".
- A chart has a heading above it or a title under it, not both with the same words. Give the title the window of the data, for example "Nov 2 to Nov 8".
- A callout holds no list. Put a list of items in a table.

## The notes of a save

A save of a Markdown page always works. When the page breaks one of four rules, the answer of the save has `notes`: at most three fixed sentences, each with one thing to do. The four rules are a callout with a list or with more than 300 characters, a column header of more than 16 characters, the same first cell in each row of a table of four columns and three rows or more, and a table cell or a list item with two em dashes or more. A note names no place: look for the fault on the page. The notes skip a complete fenced block, such as a chart, and read on after it. They stop at code in a callout or in a list item, and at a fence without its end. The other rules of this page have no note. The person already sees the page. Follow the notes in the next save of the same key.

## Example: the weekly report of a shop

This page follows each rule. A save of it gets no notes.

````markdown
Sales of the shop rose 9% this week, and refunds fell. Grace must approve one new price before Friday.

| Orders | Revenue | Refund rate | Open issues |
|---|---|---|---|
| 412 | $8,240 | 1.2% | 3 |

## Orders by day

```chart
{"type": "bar", "x": "day", "y": "orders", "y_label": "Orders", "title": "Nov 2 to Nov 8", "data": [
  {"day": "2026-11-02", "orders": 52}, {"day": "2026-11-03", "orders": 61}, {"day": "2026-11-04", "orders": 58},
  {"day": "2026-11-05", "orders": 64}, {"day": "2026-11-06", "orders": 70}, {"day": "2026-11-07", "orders": 55},
  {"day": "2026-11-08", "orders": 52}]}
```

## Products

| Product | Orders | Revenue | Status |
|---|---|---|---|
| Watercolor basics | 168 | $3,360 | Live |
| Ink sketching | 131 | $2,620 | Live |
| Color theory | 113 | $2,260 | In review |

## Open issues

| Issue | Owner | Age | State |
|---|---|---|---|
| Checkout fails on old browsers | Ada | 6 days | Open |
| Refund email shows the wrong date | Grace | 2 days | Fixing |
| Tax rate is missing in docs | Ada | 1 day | Open |

> [!IMPORTANT] Decision
> Approve the new price of Color theory before Friday, so that the sale starts on Monday.
````
