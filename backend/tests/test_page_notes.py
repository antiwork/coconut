"""The notes of a save (documents.page_notes): what a Markdown page can do better, for the rules that Coconut can check
exactly. The pages have the shape of real pages, with neutral data."""
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

from coconut import documents
from coconut.documents import (NOTE_CALLOUT, NOTE_DASHES, NOTE_HEADER, NOTE_LONG_CALLOUT, NOTE_METRICS, NOTE_NAME,
                               page_notes)

ROOT = Path(__file__).resolve().parents[2]
PAGE_JS = ROOT / 'ios/Coconut/Resources/Library/page.js'
# The example page of the skill (skills/coconut/references/pages.md) and a weak page with each fault of the real pages:
# a callout with a list, the key numbers in the first paragraph and in a quote, a header of 18 characters, the same
# word first in each row, a cell with three facts and dashes, and a chart of 30 days as the numbers 1 to 30.
EXAMPLE = (ROOT / 'skills/coconut/references/pages.md').read_text(encoding='utf-8') \
    .split('````markdown\n', 1)[1].split('\n````', 1)[0]
WEAK = (Path(__file__).parent / 'fixtures/weak_page.md').read_text(encoding='utf-8')
# The texts with their expected answer. LibraryDocumentTests runs the same table through the pattern of page.js.
NUMBERS = json.loads((Path(__file__).parent / 'fixtures/numbers.json').read_text(encoding='utf-8'))

TABLE = '| Product | Orders |\n|---|---|\n| Ink sketching | 131 |\n| Watercolor basics | 168 |'
LONG = '| Product | Orders of the week |\n|---|---|\n| Ink | 131 |'
LEAD = 'The shop had 412 orders and 3 refunds.'


def notes(content):
    """The notes of a page from the reader without the guard of page_notes, so an error of the reader fails a test."""
    return documents._notes(content)


class NumberTests(unittest.TestCase):
    """page.js and the notes have one meaning of "number"."""

    def test_the_pattern_has_the_text_of_page_js(self):
        script = PAGE_JS.read_text(encoding='utf-8')
        source = re.search(r'const number = \(text\) => text\s*\.match\(/(.*?)/su\);', script, re.S)[1]
        self.assertEqual(documents._NUMBER.pattern, source)

    def test_each_example_of_the_table_gets_its_answer(self):
        self.assertGreater(len(NUMBERS), 30)
        for text, number in NUMBERS:
            with self.subTest(text=ascii(text)):
                self.assertEqual(bool(documents._NUMBER.fullmatch(text)), number)


class RuleTests(unittest.TestCase):
    """Each rule: a page that gets the note and a page that does not."""

    def test_the_key_numbers_in_the_first_paragraph_and_no_row_of_numbers(self):
        lead = 'The shop had 412 orders and $8,240 in revenue.'
        self.assertEqual(notes(lead + '\n\n' + TABLE), [NOTE_METRICS])
        self.assertEqual(notes(lead + '\n\n```chart\n{"type": "bar", "data": []}\n```'), [NOTE_METRICS])
        # A row of numbers, one number in the lead, or a page without a table or a chart gets no note.
        self.assertEqual(notes(lead + '\n\n| Orders | Revenue |\n|---|---|\n| 412 | $8,240 |\n\n' + TABLE), [])
        self.assertEqual(notes('The shop had 412 orders this week.\n\n' + TABLE), [])
        self.assertEqual(notes(lead + '\n\nThe orders came from 3 shops.'), [])
        # A date and an issue number are no numbers.
        self.assertEqual(notes('Issue #4404 is open since 2026-11-07.\n\n' + TABLE), [])
        # The first paragraph comes after a heading too, but a quote is not a paragraph of the page.
        self.assertEqual(notes('## Week\n\n' + lead + '\n\n' + TABLE), [NOTE_METRICS])
        self.assertEqual(notes('> ' + lead + '\n\nThe shop had a good week.\n\n' + TABLE), [])

    def test_a_date_and_a_year_are_no_key_numbers(self):
        for lead in ('As of Nov 13, 2026, sales are up.', 'Week of November 2, 2026: sales rose.',
                     'Sales in 2026 are above the sales of 2025.', 'On 13 November the shop sold 412 courses.'):
            with self.subTest(lead=lead):
                self.assertEqual(notes(lead + '\n\n' + TABLE), [])
        self.assertEqual(notes('On Nov 13 the shop had 412 orders and 3 refunds.\n\n' + TABLE), [NOTE_METRICS])

    def test_only_a_row_that_page_js_draws_as_tiles_counts(self):
        rows = {
            'bold numbers': ('| Orders | Revenue |', '|---|---|', '| **412** | **$8,240** |'),
            'seven columns': ('| A | B | C | D | E | F | G |', '|---|---|---|---|---|---|---|', '| 1 | 2 | 3 | 4 | 5 | 6 | 7 |'),
            'a missing cell': ('| A | B | C |', '|---|---|---|', '| 1 | 2 |'),
            'a cell of 25 characters': ('| Orders | Total |', '|---|---|', '| 412 | 1,000,000,000,000,000,000 |'),
            'a label of 25 characters': ('| Orders of the whole weeks | B |', '|---|---|', '| 1 | 2 |'),
            'a label in code': ('| `Orders` | B |', '|---|---|', '| 1 | 2 |'),
        }
        for name, row in rows.items():
            with self.subTest(row=name):
                self.assertIn(NOTE_METRICS, notes(LEAD + '\n\n' + '\n'.join(row) + '\n\n' + TABLE))
        # Tiles of 1 to 6 numbers with a short label each, also with a unit, an entity or an escape.
        for row in (('| Orders |', '|---|', '| 412 |'), ('| Steps | Sleep |', '|---|---|', '| 1,552 steps | 7.5 h |'),
                    ('| Orders | Revenue |', '| --- | :---: |', '| 412 | &euro;8,240 |'),
                    ('| A | B | C | D | E | F |', '|---|---|---|---|---|---|', '| 1 | 2 | 3 | 4 | 5 | 6 |'),
                    ('| Orders \\* | B |', '|---|---|', '| 1 | 2 |')):
            with self.subTest(row=row):
                self.assertEqual(notes(LEAD + '\n\n' + '\n'.join(row) + '\n\n' + TABLE), [])

    def test_a_callout_with_a_list_or_with_long_text(self):
        self.assertEqual(notes('Lead.\n\n> [!NOTE] Open work\n> - Ada fixes the checkout.\n> - Grace reviews it.'),
                         [NOTE_CALLOUT])
        self.assertEqual(notes('Lead.\n\n> [!WARNING]\n> 1. Ada fixes the checkout.'), [NOTE_CALLOUT])
        self.assertEqual(notes('Lead.\n\n> [!WARNING] Open work\n>\n> - Checkout fails\n> - Refund email'),
                         [NOTE_CALLOUT])
        self.assertEqual(notes('Lead.\n\n> [!NOTE]\n> Sales rose\n> - 3% is the refund rate.'), [NOTE_CALLOUT])
        # Long text without a list gets a note that says so.
        self.assertEqual(notes('Lead.\n\n> [!TIP]\n> ' + 'The shop sells well. ' * 15), [NOTE_LONG_CALLOUT])
        self.assertEqual(notes('Lead.\n\n> [!TIP]\n> ' + 'x' * 301), [NOTE_LONG_CALLOUT])
        self.assertEqual(notes('Lead.\n\n> [!TIP]\n> ' + 'x' * 300), [])
        # One or two sentences, a plain quote with a list, and a list after the callout get no note.
        self.assertEqual(notes('Lead.\n\n> [!IMPORTANT] Decision\n> Approve the new price before Friday.'), [])
        self.assertEqual(notes('Lead.\n\n> - Ada fixes the checkout.\n> - Grace reviews it.'), [])
        self.assertEqual(notes('Lead.\n\n> [!NOTE]\n> Approve it.\n\n- Ada fixes the checkout.'), [])
        # A wrapped line that starts with a number other than 1 does not start a list (marked).
        self.assertEqual(notes('> [!WARNING] Refunds\n> The refund rate rose in week\n> 46. That is the first time.'),
                         [])
        self.assertEqual(notes('> [!NOTE]\n> The plan ends in\n> 2026. Ada agrees.'), [])

    def test_a_long_column_header(self):
        self.assertEqual(notes('Lead.\n\n| Product | Orders of the week |\n|---|---|\n| Ink | 131 |\n| Paint | 1 |'),
                         [NOTE_HEADER.format('Orders of the week')])
        long = 'Number of orders in the shop of the Example Team'
        self.assertEqual(notes(f'Lead.\n\n| Product | {long} |\n|---|---|\n| Ink | 131 |\n| Paint | 1 |'),
                         [NOTE_HEADER.format(long[:39] + '…')])
        self.assertLessEqual(len(NOTE_HEADER.format(long[:39] + '…')), 160)
        # 16 characters, and a header with the marks of Markdown around 16 characters, get no note.
        self.assertEqual(notes('Lead.\n\n| Product | Orders this week |\n|---|---|\n| Ink | 131 |\n| Paint | 1 |'), [])
        self.assertEqual(notes('Lead.\n\n| Product | **Orders this week** |\n|---|---|\n| Ink | 131 |'), [])

    def test_a_table_inside_a_list_item_counts(self):
        self.assertEqual(notes('Lead.\n\n- Numbers:\n\n  | Product | Orders of the week |\n  |---|---|\n  | Ink | 131 |\n'),
                         [NOTE_HEADER.format('Orders of the week')])
        # Tiles in a list item are tiles too, and the paragraph of an item is not the first paragraph of the page.
        self.assertEqual(notes(LEAD + '\n\n- Key numbers\n\n  | Orders | Revenue |\n  |---|---|\n  | 412 | $8,240 |\n\n'
                               + TABLE), [])
        self.assertEqual(notes('- ' + LEAD + '\n\n' + TABLE), [])

    def test_the_same_text_first_in_each_row(self):
        rows = ''.join(f'| Issue | {title} | Ada | Open |\n' for title in ('Checkout', 'Refund email', 'Tax rate'))
        page = 'Lead.\n\n| Kind | Title | Owner | State |\n|---|---|---|---|\n' + rows
        self.assertEqual(notes(page), [NOTE_NAME.format('Issue')])
        # Three columns, two rows, or another text in one row get no note.
        self.assertEqual(notes(page.replace(' | Open |', ' |').replace('| State |', '|').replace('|---|---|---|---|',
                                                                                                '|---|---|---|')), [])
        self.assertEqual(notes(page.rsplit('| Issue |', 1)[0]), [])
        self.assertEqual(notes(page.replace('| Issue | Tax rate', '| Pull request | Tax rate')), [])

    def test_a_cell_with_several_dashes(self):
        cell = 'Issue — #4404 Checkout fails — since 2026-11-07'
        self.assertEqual(notes(f'Lead.\n\n| Item | Owner |\n|---|---|\n| {cell} | Ada |'), [NOTE_DASHES])
        self.assertEqual(notes('Lead.\n\n| Item | Owner |\n|---|---|\n| Issue – #4404 – since 2026-11-07 | Ada |'),
                         [NOTE_DASHES])
        # One dash, dashes without spaces, and dashes in running text get no note: a list item or a paragraph can use
        # dashes for an aside.
        self.assertEqual(notes('Lead.\n\n| Item | Owner |\n|---|---|\n| Checkout — open | Ada |\n| 2026-11-07—12 | Ada |'),
                         [])
        self.assertEqual(notes(f'Lead.\n\n- {cell}'), [])
        self.assertEqual(notes('Lead.\n\n- The shop — which opened in May — sells well.'), [])
        self.assertEqual(notes(f'{cell}.'), [])


class NoteTests(unittest.TestCase):
    def test_at_most_three_notes_in_the_order_of_the_rules(self):
        self.assertEqual(notes(WEAK), [NOTE_METRICS, NOTE_CALLOUT, NOTE_HEADER.format('Orders of the week')])
        for note in (NOTE_METRICS, NOTE_CALLOUT, NOTE_LONG_CALLOUT, NOTE_HEADER, NOTE_NAME, NOTE_DASHES):
            with self.subTest(note=note):
                self.assertLessEqual(len(note.format('x' * 40)), 160)
                self.assertTrue(note.endswith('.'))

    def test_the_example_of_the_skill_has_no_note(self):
        self.assertEqual(notes(EXAMPLE), [])
        self.assertIn('```chart', EXAMPLE)
        self.assertEqual(EXAMPLE, EXAMPLE.strip())

    def test_the_notes_of_the_weak_page_lead_as_far_as_they_can_see(self):
        """A bot applies the notes of each save in its next save, as the skill says, until the page has none."""
        fixes = {
            NOTE_METRICS: lambda page: page.replace(
                '3 open issues for the Example Team.\n',
                '3 open issues for the Example Team.\n\n| Orders | Revenue | Refund rate | Open issues |\n'
                '|---|---|---|---|\n| 412 | $8,240 | 1.2% | 3 |\n', 1),
            NOTE_CALLOUT: lambda page: re.sub(
                r'> \[!WARNING\] Open work\n(?:> - .*\n)+',
                '> [!WARNING] Open work\n> Three issues wait for a fix. The table at the end names them.\n', page),
            NOTE_HEADER.format('Orders of the week'): lambda page: page.replace('Orders of the week', 'Orders'),
            NOTE_NAME.format('Issue'): lambda page: re.sub(r'\| Issue \| (Issue — .*?) \|', r'| \1 |', page)
            .replace('| Kind | Item | Owner | State |\n|---|---|---|---|', '| Issue | Owner | State |\n|---|---|---|'),
            NOTE_DASHES: lambda page: re.sub(r'\| Issue — (#\d+) (.*?) — since (\S+) \|', r'| \2 | \1 | \3 |', page)
            .replace('| Issue | Owner | State |\n|---|---|---|', '| Issue | Number | Since | Owner | State |\n'
                     '|---|---|---|---|---|'),
        }
        page, seen = WEAK, []
        for _ in range(5):
            found = notes(page)
            if not found:
                break
            seen.append(found)
            for note in found:
                page = fixes[note](page)
        self.assertEqual(seen, [[NOTE_METRICS, NOTE_CALLOUT, NOTE_HEADER.format('Orders of the week')],
                                [NOTE_NAME.format('Issue'), NOTE_DASHES]])
        self.assertEqual(notes(page), [])
        self.assertIn('| Checkout fails on old browsers | #4404 | 2026-11-07 | Ada | Open |', page)
        # The notes cannot see each fault: the chart still has the days 1 to 30 as labels and the numbers of the
        # quote line stay. No rule covers them, because a check of them needs a guess. The page is better, not good.
        self.assertIn('{"day": 1, "orders": 52}', page)
        self.assertIn('> Orders 412 · Revenue $8,240', page)


class ReaderTests(unittest.TestCase):
    """Text that could break a reader of Markdown gives the notes of marked, and never an error."""

    def test_an_html_page_and_content_that_is_not_text_get_no_notes(self):
        self.assertEqual(page_notes('html', WEAK), [])
        for content in (None, 12, b'x', ['x'], ''):
            with self.subTest(content=content):
                self.assertEqual(page_notes('markdown', content), [])

    def test_an_escaped_pipe_stays_in_its_cell_and_a_pipe_in_code_splits_it(self):
        # With the escape, the header has one cell of 24 characters. Without it, it would have two short cells.
        self.assertEqual(notes('Lead.\n\n| Orders \\| refunds of week | Use |\n|---|---|\n| 1 | 2 |'),
                         [NOTE_HEADER.format('Orders | refunds of week')])
        # GitHub Markdown splits a cell at a pipe in code too: this header has two cells and the delimiter one, so it is
        # no table.
        self.assertEqual(notes('Lead.\n\n| `Orders of | the whole week` |\n|---|\n| 1 |'), [])

    def test_a_table_inside_a_callout_counts(self):
        page = ('Lead.\n\n> [!NOTE] Orders\n> | Product | Orders of the week |\n> |---|---|\n> | Ink | 131 |')
        self.assertEqual(notes(page), [NOTE_HEADER.format('Orders of the week')])

    def test_an_unclosed_fence_hides_the_rest_of_the_page(self):
        self.assertEqual(notes('Lead.\n\n```\n' + LONG), [])
        self.assertEqual(notes('Lead with 412 orders and 3 refunds.\n\n```chart\n{"type": "bar"'), [NOTE_METRICS])
        # A fence in a callout ends with the callout.
        self.assertEqual(notes('> [!NOTE]\n> ```\n> x\n\n' + LONG), [NOTE_HEADER.format('Orders of the week')])
        # A longer fence ends only with a fence of its length.
        self.assertEqual(notes('````\n```\n' + LONG + '\n````'), [])

    def test_a_quote_line_inside_a_fence_stays_code(self):
        example = '```markdown\n> [!NOTE] Open work\n> - Ada fixes the checkout.\n> - Grace reviews it.\n```'
        self.assertEqual(notes('How to write a callout.\n\n' + example + '\n\nDo not do that.'), [])
        # The fence ends at its own end, so the table after it counts.
        self.assertEqual(notes('Lead.\n\n```sh\necho a\n> [!NOTE]\n> - a\n```\n\n' + LONG),
                         [NOTE_HEADER.format('Orders of the week')])

    def test_a_fence_in_a_list_item_ends_at_its_indented_end(self):
        self.assertEqual(notes('Lead.\n\n- Run it:\n\n  ```\n  | a | b — c — d |\n  |---|---|\n  ```\n\n' + LONG),
                         [NOTE_HEADER.format('Orders of the week')])

    def test_blocks_that_marked_shows_as_something_else(self):
        # A photo is no first paragraph with numbers, a lazy line belongs to its quote, indented code and a definition
        # of a link show no paragraph or table, and only the info text "chart" draws a chart.
        self.assertEqual(notes('![Photo 1 of 2](coconut-file://x)\n\nLead.\n\n' + TABLE), [])
        self.assertEqual(notes('> [!NOTE]\n> Approve it\nand 412 and 3 too.\n\n' + TABLE), [])
        self.assertEqual(notes('Lead.\n\n    | Product | Orders of the week |\n    |---|---|\n    | Ink | 131 |'), [])
        self.assertEqual(notes('[1]: https://example.com "412 and 3"\n\nLead.\n\n' + TABLE), [])
        self.assertEqual(notes(LEAD + '\n\n```chart extra\n{}\n```'), [])
        # The marks of emphasis and an entity do not count in a header, and trim() of JavaScript removes a BOM.
        self.assertEqual(notes('Lead.\n\n| Product | _Orders this week_ |\n|---|---|\n| Ink | 131 |'), [])
        self.assertEqual(notes(LEAD + '\n\n| Orders | Revenue |\n|---|---|\n| 412 | \ufeff8,240 |\n\n' + TABLE), [])

    def test_headings_and_lines_under_a_paragraph_are_no_tables(self):
        self.assertEqual(notes('A heading | with a pipe\n---\n\n| Item | Owner |\n|---|---|\n| a — b — c | Ada |'),
                         [NOTE_DASHES])
        self.assertEqual(notes('The shop had 412 orders and 3 refunds.\n===\n\n' + TABLE), [])

    def test_lines_of_each_kind_never_raise(self):
        parts = ['', '>', '> [!NOTE]', '```', '~~~', '```chart', '| a |', '|---|', '---', '===', '- x', '1. y',
                 '# h', '  - z', '  | b |', 'text 1 2', '| `|` | \\| |', '\t', '> > nested — a — b', '****',
                 '- [ ] task', '46. x', '    ```']
        for first in parts:
            for second in parts:
                for third in parts:
                    page = '\n'.join((first, second, third, first))
                    self.assertIsInstance(notes(page), list)


SIZE = 512 * 1024
EMOJI = '\U0001F600'


def hostile_pages() -> dict:
    """Pages of at most 512 KB that make a careless reader slow: each one also ends with an emoji, because a page with
    a character outside Latin-1 takes another, slower form of text in Python."""
    table = '\n\n| a | b |\n|---|---|\n| x | y |\n'
    pages = {
        'brackets': '[' * (SIZE - 60) + table,
        'brackets in a callout': '> [!NOTE]\n> ' + '[' * (SIZE - 20),
        'brackets in a cell': '| a |\n|---|\n| ' + '[' * (SIZE - 30) + ' |',
        'links without an end': '> [!NOTE]\n> ' + '[x](' * ((SIZE - 20) // 4),
        'images without an end': '> [!NOTE]\n> ' + '![' * ((SIZE - 20) // 2),
        'list markers': '- ' * (SIZE // 2 - 2),
        'cells of dashes': 'p | q\n' + '| - ' * (SIZE // 4 - 4),
        'stars': '*' * (SIZE - 4),
        'spaces': ' ' * (SIZE - 5) + 'x',
        'quote marks': '>' * (SIZE - 5),
        'quote marks with spaces': '> ' * (SIZE // 2 - 3),
        'pipes and dashes': '|-' * (SIZE // 2 - 2),
        'short lines': 'ab\n' * 100_000,
        'list with lazy lines': '- x\n' + 'a\n' * (SIZE // 2 - 4),
        'deep quotes': ''.join('>' * depth + ' x\n' for depth in range(1, 1000)),
        'no space': 'x' * 500_000,
        'digits with commas': '1,' * ((SIZE - 60) // 2) + 'x' + table,
        'wide header and delimiter': '| ' + ' | '.join(['x'] * 60_000) + ' |\n' + '|' + '-|' * 60_000 + '\n',
        'many rows of many cells': '| a |\n|---|\n' + ('|' * 700 + '\n') * 700,
        'fences': '```\n' * (SIZE // 4 - 1),
    }
    pages.update({name + ' and an emoji': page[:-1] + EMOJI for name, page in list(pages.items())})
    return pages


class TimeTests(unittest.TestCase):
    """A save runs the notes, and Python runs one thread at a time, so a slow check stops the whole service."""

    BOUND = 2.0

    def test_a_hostile_page_takes_little_time(self):
        pages = hostile_pages()
        for name, page in pages.items():
            self.assertLessEqual(len(page.encode()), SIZE, name)
        # Each page runs in its own process with a time limit, so a slow page fails the test and does not stop the run.
        child = ('import sys, time\nfrom coconut import documents\nfrom test_page_notes import hostile_pages\n'
                 'page = hostile_pages()[sys.argv[1]]\nstarted = time.perf_counter()\ndocuments._notes(page)\n'
                 'print(time.perf_counter() - started)\n')
        environment = {'PYTHONPATH': f'{ROOT / "backend"}:{Path(__file__).parent}', 'PYTHONDONTWRITEBYTECODE': '1'}
        for name in pages:
            with self.subTest(page=name):
                try:
                    run = subprocess.run([sys.executable, '-c', child, name], capture_output=True, text=True,
                                         timeout=self.BOUND + 8, env=environment)
                except subprocess.TimeoutExpired:
                    self.fail(f'{name}: more than {self.BOUND + 8} seconds')
                self.assertEqual(run.returncode, 0, run.stderr[-2000:])
                self.assertLess(float(run.stdout), self.BOUND)


if __name__ == '__main__':
    unittest.main()
