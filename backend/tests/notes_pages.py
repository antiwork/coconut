"""Pages for the tests of the notes (test_page_notes.py), with neutral data: 12 good pages of different kinds, which
must get no note, and 6 weak pages. The third review of the notes wrote them (pages_judge.py), with other names."""

NAMES = [f'Member {number}' for number in range(1, 25)]
CITIES = ['Lima', 'Berlin', 'Austin', 'Lisbon', 'Osaka', 'Nairobi']
TEAMS = ['Design', 'Platform', 'Support', 'Growth']

GOOD = {
    'a report with tiles (the recipe)': '''Sales of the shop rose 9% this week, and refunds fell.

| Orders | Revenue | Refund rate | Open issues |
|---|---|---|---|
| 412 | $8,240 | 1.2% | 3 |

## Products

| Product | Orders | Revenue | Status |
|---|---|---|---|
| Watercolor basics | 168 | $3,360 | Live |
| Ink sketching | 131 | $2,620 | Live |
| Color theory | 113 | $2,260 | In review |

> [!IMPORTANT] Decision
> Approve the new price of Color theory before Friday.
''',
    'a plain text page': '''The offsite is in Lisbon this year. The team meets for three days of planning and one free day.

## Travel

Book the flight before the end of the month. The company pays for the flight and for the hotel. Keep each receipt.

## What to bring

Bring a laptop, a charger and a passport. The hotel has adapters at the desk.

## Contact

Write to Grace for each question about travel. She answers in one day.
''',
    'a page with one chart': '''Signups grew each day of the week, and Saturday was the best day.

## Signups by day

```chart
{"type": "line", "x": "day", "y": "signups", "y_label": "Signups", "title": "Nov 2 to Nov 8", "data": [
  {"day": "2026-11-02", "signups": 52}, {"day": "2026-11-03", "signups": 61}, {"day": "2026-11-04", "signups": 58},
  {"day": "2026-11-05", "signups": 64}, {"day": "2026-11-06", "signups": 70}, {"day": "2026-11-07", "signups": 75}]}
```
''',
    'a long table of people': 'The directory of the Example Team. Write to Ada when a line is wrong.\n\n'
    '| Name | Team | City | Status |\n|---|---|---|---|\n' + ''.join(
        f'| {name} | {TEAMS[index % 4]} | {CITIES[index % 6]} | {"Away" if index % 7 == 0 else "Active"} |\n'
        for index, name in enumerate(NAMES)),
    'a how-to guide with steps and code': '''This guide shows how to pair a new iPhone with the Mac. It takes about five minutes.

## Steps

1. Open Coconut on the Mac.
2. Run the pair command in the terminal:

   ```sh
   coconut pair --name "iPhone of Ada"
   ```

3. Scan the code with the iPhone.
4. Wait for the green mark.

## When it fails

- Check that both devices are online.
- Run `coconut status` and read the last line.
- Start the pairing again.
''',
    'release notes with versions and a table': '''Coconut 1.4 is out for iOS 18 and macOS 15. It makes the Library faster and fixes the chat list.

## Changes

| Change | Area | Kind |
|---|---|---|
| Pages open at once | Library | Faster |
| The chat list keeps its place | Chat | Fix |
| Charts write days as dates | Library | New |

## Thanks

Thanks to each tester who sent a report.
''',
    'a runbook with a warning and a task list': '''Follow this runbook when the relay does not answer. Stop at the first step that fixes it.

> [!WARNING] Data
> Do not delete the data folder. A restart of the service is always safe.

## Checks

- [x] Read the status page of the relay
- [ ] Restart the service
- [ ] Ask the owner of the relay

## Commands

```sh
coconut status
coconut restart
```
''',
    'a comparison of plans': '''The three plans differ in seats and in support. Most teams start with Team.

| Plan | Seats | Price | Support |
|---|---|---|---|
| Solo | 1 | $9 | Email |
| Team | 10 | $49 | Chat |
| Company | 100 | $299 | Phone |
''',
    'a weekly digest with dates and tiles': '''Week of November 2, 2026: the team shipped the new Library and closed most of the old issues.

| Closed | Opened | Open now |
|---|---|---|
| 14 | 6 | 9 |

## Closed this week

| Issue | Owner | Age | State |
|---|---|---|---|
| Checkout fails on old browsers | Ada | 6 days | Closed |
| Refund email shows the wrong date | Grace | 2 days | Closed |
| Tax rate is missing in docs | Ada | 1 day | Closed |
''',
    'a schedule with times and office hours': '''The standup moves to 9 am, and the review moves to 3 pm. The table has the hours of each office.

| Office | Hours | Contact |
|---|---|---|
| Lima | Mon – Fri, 9:00 – 18:00 | Ada |
| Berlin | Mon – Thu, 8:30 – 17:00 | Grace |
| Osaka | Tue – Sat, 10:00 – 19:00 | Reporter |
''',
    'a page with a diagram': '''A message goes from the iPhone to the relay, and from the relay to the Mac. The relay cannot read it.

```mermaid
sequenceDiagram
  iPhone->>Relay: sealed message
  Relay->>Mac: sealed message
  Mac-->>iPhone: answer
```

## Keys

Each device has its own key. The pairing gives each side the key of the other side.
''',
    'the status of a sprint with a table': '''Sprint 12 ends in 3 days. The team is on track, and one task waits for a review.

| Task | Owner | Estimate | State |
|---|---|---|---|
| New Library list | Ada | 2 days | Done |
| Chart labels | Grace | 1 day | In review |
| Faster sync | Reporter | 3 days | Doing |
''',
}

# name: (the start of the note that the page gets, or None when its fault is outside the four rules, the page)
WEAK = {
    'key numbers in the first paragraph (outside the rules)': (None, '''This week the shop had 412 orders and $8,240 in revenue.

| Product | Orders |
|---|---|
| Ink sketching | 131 |
| Watercolor basics | 168 |
'''),
    'a list in a callout': ('Callout 1 holds a list.', '''The release has three open tasks.

> [!WARNING] Open work
> - Checkout fails on old browsers
> - The refund email shows the wrong date
> - The tax rate is missing
'''),
    'a long column header': ('Column 2 of table 1 has a header of 29 characters.', '''The products of the week.

| Product | Orders of the last seven days | Revenue |
|---|---|---|
| Ink sketching | 131 | $2,620 |
| Watercolor basics | 168 | $3,360 |
'''),
    'facts packed with spaced hyphens (outside the rules: a hyphen is no dash)': (None, '''The open issues of the shop.

| Item | Owner |
|---|---|
| Issue - #4404 Checkout fails on old browsers - since 2026-11-07 | Ada |
| Issue - #4410 Refund email shows the wrong date - since 2026-11-11 | Grace |
'''),
    'approximate key numbers in the first paragraph (outside the rules)': (None, '''This week the shop had ~410 orders, ~$8.2k in revenue and ~1% refunds.

| Product | Orders |
|---|---|
| Ink sketching | 131 |
| Watercolor basics | 168 |
'''),
    'a callout of five short sentences (outside the rules: under 300 characters)': (None, '''The release is late.

> [!NOTE] Plan
> Ada fixes the checkout today. Grace reviews the fix tomorrow. The Reporter tests it on old browsers. The release goes out on Friday. The team tells each customer on Monday.
'''),
}
