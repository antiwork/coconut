"""The notes of a save (documents.page_notes): what a Markdown page can do better, for four rules that its lines show
without a guess. Each note is one fixed sentence that names no place. A good page never gets a note, and each rule of
the reader can only remove a note. The pages have the shape of real pages, with neutral data."""
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

from coconut import documents
from coconut.documents import NOTE_CALLOUT, NOTE_DASHES, NOTE_HEADER, NOTE_NAME, page_notes
from notes_pages import GOOD, WEAK

ROOT = Path(__file__).resolve().parents[2]
# The example page of the skill (skills/coconut/references/pages.md) and a weak page with each fault of the real pages.
EXAMPLE = (ROOT / 'skills/coconut/references/pages.md').read_text(encoding='utf-8') \
    .split('````markdown\n', 1)[1].split('\n````', 1)[0]
WEAK_PAGE = (Path(__file__).parent / 'fixtures/weak_page.md').read_text(encoding='utf-8')
LONG = '| Product | Orders of the week |\n|---|---|\n| Ink | 131 |\n'
NOTES = (NOTE_CALLOUT, NOTE_HEADER, NOTE_NAME, NOTE_DASHES)


def notes(content):
    """The notes of a page from the reader without the guard of page_notes, so an error of the reader fails a test."""
    return documents._notes(content)


def named(rows=3, columns=4, first='Issue'):
    """A table of `columns` columns and `rows` rows, each row with the same first cell."""
    head = '| ' + ' | '.join(['Kind', 'Title', 'Owner', 'State', 'Age'][:columns]) + ' |\n'
    under = '|' + '---|' * columns + '\n'
    body = ''.join('| ' + ' | '.join([first, f'Task {row}', 'Ada', 'Open', '2 days'][:columns]) + ' |\n'
                   for row in range(rows))
    return head + under + body


class PageTests(unittest.TestCase):
    def test_no_good_page_gets_a_note(self):
        for name, page in GOOD.items():
            with self.subTest(page=name):
                self.assertEqual(notes(page), [])
        self.assertEqual(notes(EXAMPLE), [])

    def test_a_weak_page_gets_the_note_of_its_fault_or_none_when_its_fault_is_outside_the_rules(self):
        expected = {'Callout': NOTE_CALLOUT, 'Column': NOTE_HEADER}
        for name, (start, page) in WEAK.items():
            with self.subTest(page=name):
                self.assertEqual(notes(page), [] if start is None else [expected[start.split()[0]]])

    def test_the_notes_of_the_weak_page_lead_as_far_as_they_can_see(self):
        """A bot applies the notes of each save in its next save, as the skill says, until the page has none."""
        fixes = {
            NOTE_CALLOUT: lambda page: re.sub(
                r'> \[!WARNING\] Open work\n(?:> - .*\n)+',
                '> [!WARNING] Open work\n> Three issues wait for a fix. The table at the end names them.\n', page),
            NOTE_HEADER: lambda page: page.replace('Orders of the week', 'Orders'),
            NOTE_NAME: lambda page: re.sub(r'\| Issue \| (Issue — .*?) \|', r'| \1 |', page)
            .replace('| Kind | Item | Owner | State |\n|---|---|---|---|', '| Issue | Owner | State |\n|---|---|---|'),
            NOTE_DASHES: lambda page: re.sub(r'\| Issue — (#\d+) (.*?) — since (\S+) \|', r'| \2 | \1 | \3 |', page)
            .replace('| Issue | Owner | State |\n|---|---|---|', '| Issue | Number | Since | Owner | State |\n'
                     '|---|---|---|---|---|'),
        }
        page, seen = WEAK_PAGE, []
        for _ in range(5):
            found = notes(page)
            if not found:
                break
            seen.append(found)
            for note in found:
                page = fixes[note](page)
        self.assertEqual(seen, [[NOTE_CALLOUT, NOTE_HEADER, NOTE_NAME], [NOTE_DASHES]])
        self.assertEqual(notes(page), [])
        self.assertIn('| Checkout fails on old browsers | #4404 | 2026-11-07 | Ada | Open |', page)
        # The notes cannot see each fault: the chart still has the days 1 to 30 as labels, and the key numbers stay in
        # the first paragraph and in a quote. No rule covers them, because a check of them needs a guess.
        self.assertIn('{"day": 1, "orders": 52}', page)
        self.assertIn('> Orders 412 · Revenue $8,240', page)

    def test_a_note_is_one_of_four_fixed_sentences_and_comes_once(self):
        page = '\n'.join(('> [!NOTE]\n> - a\n', '> [!TIP]\n> - b\n', LONG, LONG, named(), named(), '- a — b — c'))
        self.assertEqual(notes(page), [NOTE_CALLOUT, NOTE_HEADER, NOTE_NAME])
        for note in NOTES:
            with self.subTest(note=note):
                self.assertLessEqual(len(note), 160)
                self.assertTrue(note.endswith('.'))
                self.assertNotRegex(note.replace('16', ''), r'[0-9]')


class RuleTests(unittest.TestCase):
    """Each rule, exactly at its limit and above it."""

    def test_a_callout_with_a_list(self):
        for item in ('- Ada fixes it.', '* Ada fixes it.', '+ Ada fixes it.', '1. Ada fixes it.', '1) Ada fixes it.'):
            with self.subTest(item=item):
                self.assertEqual(notes(f'Lead.\n\n> [!NOTE] Open work\n> {item}'), [NOTE_CALLOUT])
        self.assertEqual(notes('> [!NOTE]\n> One.\n\n> [!TIP]\n> Open work:\n> - Two.'), [NOTE_CALLOUT])
        # The mark counts in small letters too, as in page.js.
        self.assertEqual(notes('> [!note]\n> - Two.'), [NOTE_CALLOUT])
        # A plain quote with a list, a list after the callout, and a break get no note.
        self.assertEqual(notes('Lead.\n\n> - Ada fixes the checkout.\n> - Grace reviews it.'), [])
        self.assertEqual(notes('Lead.\n\n> [!NOTE]\n> Approve it.\n\n- Ada fixes the checkout.'), [])
        self.assertEqual(notes('> [!NOTE]\n> Approve it.\n>\n> - - -'), [])
        # A numbered line in a callout counts only when it starts at 1: a wrapped line can start so. Errs toward no
        # note.
        self.assertEqual(notes('> [!NOTE]\n> The count of open issues is\n> 2. That is low.'), [])
        self.assertEqual(notes('> [!WARNING] Refunds\n> The refund rate rose in week\n> 46. That is the first time.'),
                         [])

    def test_a_long_callout(self):
        self.assertEqual(notes('> [!TIP]\n> ' + 'x' * 300), [])
        self.assertEqual(notes('> [!TIP]\n> ' + 'x' * 301), [NOTE_CALLOUT])
        self.assertEqual(notes('> [!TIP]\n> ' + 'x' * 150 + '\n> ' + 'x' * 151), [NOTE_CALLOUT])
        # The title does not count, and the size of one callout does not add to the next one.
        self.assertEqual(notes('> [!TIP] ' + 'x' * 400 + '\n> Short.'), [])
        self.assertEqual(notes('> [!TIP]\n> ' + 'x' * 200 + '\n\n> [!NOTE]\n> ' + 'x' * 200), [])

    def test_a_long_column_header(self):
        self.assertEqual(notes('Lead.\n\n| Product | ' + 'x' * 16 + ' |\n|---|---|\n| Ink | 1 |\n'), [])
        self.assertEqual(notes('Lead.\n\n| Product | ' + 'x' * 17 + ' |\n|---|---|\n| Ink | 1 |\n'), [NOTE_HEADER])
        self.assertEqual(notes('Lead.\n\n| Product | ' + 'x' * 17 + '|\n|---|---|\n'), [NOTE_HEADER])
        # A header shows fewer characters than it has: the marks of emphasis, the address or label of a link, a photo
        # and an entity do not count. A header that shows at least 17 characters gets the note anyway.
        for cell in ('**Orders this week**', '_Orders this week_', 'Orders &amp; refunds',
                     '[Orders this week](https://example.com/orders/of/the/week)', '[Quarterly result][q3-report]',
                     '![A photo of the shop of the week](coconut-file://x)', '<Orders this week>'):
            with self.subTest(cell=cell):
                self.assertEqual(notes(f'Lead.\n\n| Product | {cell} |\n|---|---|\n| Ink | 131 |\n'), [])
        self.assertEqual(notes('Lead.\n\n| **[Average order value](https://example.org/help/aov)** |\n|---|\n| $31 |\n'),
                         [NOTE_HEADER])

    def test_the_same_text_first_in_each_row(self):
        self.assertEqual(notes('Lead.\n\n' + named()), [NOTE_NAME])
        self.assertEqual(notes('Lead.\n\n' + named(rows=2)), [])
        self.assertEqual(notes('Lead.\n\n' + named(columns=3)), [])
        self.assertEqual(notes('Lead.\n\n' + named(rows=4, columns=5)), [NOTE_NAME])
        # Another text in the last row and an empty first cell get no note.
        self.assertEqual(notes('Lead.\n\n' + named() + '| Pull request | Task 4 | Ada | Open |\n'), [])
        self.assertEqual(notes('Lead.\n\n' + named(first='')), [])

    def test_dashes_in_a_cell_or_an_item(self):
        self.assertEqual(notes('Lead.\n\n| Item | Owner |\n|---|---|\n| a — b — c | Ada |\n'), [NOTE_DASHES])
        self.assertEqual(notes('Lead.\n\n| Item | Owner |\n|---|---|\n| a — b | Ada — c |\n'), [])
        for item in ('- a — b — c', '* a — b — c', '+ a — b — c', '1. a — b — c', '12. a — b — c', '   - a — b — c'):
            with self.subTest(item=item):
                self.assertEqual(notes('Lead.\n\n' + item), [NOTE_DASHES])
        self.assertEqual(notes('Lead.\n\n- a — b'), [])
        # Dashes without spaces, an en dash (a range), a hyphen, dashes in a paragraph, a header, a plain quote, a link
        # and a callout get no note.
        for page in ('- a—b—c', '- Mon – Fri – Sun', '- a - b - c', 'a — b — c.', '| a — b — c |\n|---|\n| x |\n',
                     '> - a — b — c', '- [The handbook](https://example.org/a "Part 1 — rules — 2026")',
                     '> [!NOTE]\n> Short — very — short.', '12.a — b — c'):
            with self.subTest(page=page):
                self.assertEqual(notes('Lead.\n\n' + page), [])


class ReaderTests(unittest.TestCase):
    """Each rule of the reader for a kind of doubt, with the probe of the review that showed the doubt. Each can only
    remove a note."""

    def test_the_reader_skips_a_complete_fenced_block_and_reads_on(self):
        chart = '```chart\n{"type": "bar", "data": []}\n```\n'
        self.assertEqual(notes('Lead.\n\n' + chart + '\n' + LONG), [NOTE_HEADER])
        self.assertEqual(notes('Lead.\n\n' + chart + LONG), [NOTE_HEADER])
        self.assertEqual(notes('Lead.\n\n~~~\n| a — b — c |\n~~~\n\n' + LONG), [NOTE_HEADER])
        self.assertEqual(notes('Lead.\n\n' + chart + '\n```mermaid\nflowchart LR\n```\n\n' + LONG), [NOTE_HEADER])
        # A longer closing fence closes, a shorter one or one with text does not, and an info string is allowed.
        self.assertEqual(notes('```\n' + LONG + '`````\n\n' + named()), [NOTE_NAME])
        self.assertEqual(notes('````\n```\n' + LONG + '````\n\n' + named()), [NOTE_NAME])
        self.assertEqual(notes('```js\n' + LONG + '```js\n```\n\n' + named()), [NOTE_NAME])
        self.assertEqual(notes('~~~~ text with `ticks`\n' + LONG + '~~~~  \n\n' + named()), [NOTE_NAME])
        self.assertEqual(notes('```\ncode\n    ```\n\n' + LONG), [])
        # The line after a closing fence starts as after a blank line, so a callout can start there.
        self.assertEqual(notes('```\ncode\n```\n> [!NOTE]\n> - a\n'), [NOTE_CALLOUT])
        # A paragraph that starts with inline code of three backticks is no fence, and the table after it is read.
        self.assertEqual(notes('```chart``` draws a graph.\n\n' + LONG), [NOTE_HEADER])

    def test_the_reader_stops_at_code_that_it_cannot_skip(self):
        # A fence without its closing fence, a fence in a callout or in a list item (with an indent), a tab and four
        # spaces: the reader stops and reads nothing after it. Errs toward no note.
        for code in ('```\n', '~~~md\n', '```\n~~~\n', '> ```\n> code\n> ```\n', '  ```sh\n  code\n  ```\n',
                     '    code\n', '\tcode\n'):
            with self.subTest(code=code):
                self.assertEqual(notes(LONG + '\n' + code + '\n' + named()), [NOTE_HEADER])
                self.assertEqual(notes('Lead.\n\n' + code + '\n' + LONG), [])
        # Code right under a callout ends the callout, and code inside a callout stops the reader.
        self.assertEqual(notes('> [!NOTE]\n> Short note.\n```\ncode\n```\n> - Ada said yes.\n> - Grace said no.\n'), [])
        self.assertEqual(notes('> [!NOTE]\n> Short note.\n> ```\n> - a\n'), [])
        # The fence of a list item stops the reader, as in a how-to page.
        self.assertEqual(notes('1. Run:\n\n   ```sh\n   coconut pair\n   ```\n\n' + LONG), [])

    def test_a_callout_starts_only_at_the_start_of_the_page_or_after_a_blank_line(self):
        self.assertEqual(notes('> Ada wrote this\nin the chat:\n> [!NOTE]\n> - Fix the checkout.\n> - Call Grace.\n'), [])
        self.assertEqual(notes('Text right above.\n> [!NOTE]\n> - Fix the checkout.\n'), [])
        self.assertEqual(notes('> Ada wrote:\n> [!NOTE]\n> - Fix the checkout.'), [])
        # A callout ends at the first line without a quote mark: a lazy line ends it. Errs toward no note.
        self.assertEqual(notes('> [!NOTE]\n> Short.\nA lazy line.\n- a list after it.\n'), [])
        self.assertEqual(notes('> [!NOTE]\n> ' + 'x' * 200 + '\n' + 'y' * 200 + '\n'), [])
        # A table and a quote with an indent of at most three spaces count.
        self.assertEqual(notes('  ' + LONG.replace('\n', '\n  ')), [NOTE_HEADER])
        self.assertEqual(notes('   > [!NOTE]\n   > - Fix the checkout.\n'), [NOTE_CALLOUT])

    def test_a_table_that_the_reader_cannot_be_sure_of_gives_no_note(self):
        # A line of text right under a table is a row for marked, with another first cell.
        self.assertEqual(notes(named() + 'Total: 3 issues\n'), [])
        self.assertEqual(notes(named() + '\nTotal: 3 issues\n'), [NOTE_NAME])
        # A backtick or a backslash anywhere in the table, also in an earlier row.
        self.assertEqual(notes('| Shop | Net revenue \\(\\$\\) |\n|---|---|\n| Harbor | 212 |\n'), [])
        self.assertEqual(notes('| `Orders of | the whole week` |\n|---|---|\n| x | y |\n'), [])
        rows = named().splitlines()
        rows.insert(3, '| Issue | `a | b` | Ada | Open |')
        self.assertEqual(notes('\n'.join(rows) + '\n'), [])
        rows = named(rows=4).splitlines()
        rows[2] = rows[2].replace('Task 0', 'Task \\| 0')
        self.assertEqual(notes('\n'.join(rows) + '\n'), [])
        # Only a table: a header, a line under it with a dash and as many cells, and body lines that start with "|".
        self.assertEqual(notes('| Product | Orders of the week |\n|---|\n| x |\n'), [])
        self.assertEqual(notes('| Product | Orders of the week |\n| : | : |\n| x | y |\n'), [])
        self.assertEqual(notes('| Product | Orders of the week |\n| Ink | 131 |\n| Paint | 168 |\n'), [])
        self.assertEqual(notes('Product | Orders of the week\n---|---\nInk | 131\n'), [])
        self.assertEqual(notes('| Product | Orders of the week |\n|---|---|\nInk | 131\n'), [])
        # Carriage returns of a page with Windows line ends do not count.
        self.assertEqual(notes((named() + '\n').replace('\n', '\r\n')), [NOTE_NAME])
        self.assertEqual(notes(('| Product | ' + 'x' * 16 + ' |\n|---|---|\n| a | b |\n').replace('\n', '\r\n')), [])

    def test_the_reader_reads_each_line_at_most_twice(self):
        # A bound of the work without a clock: the reader asks at most twice for each line whether it opens code.
        for page in ('> x\n' * 1999, '| a |\n|---|\n' + '| b |\n' * 1997, '- a\n' * 1999, 'text\n' * 1999,
                     '```\n' * 1999, '```\nx\n```\n' * 666):
            calls = []
            check = documents._opens_code
            with patch.object(documents, '_opens_code', lambda line: calls.append(line) or check(line)):
                notes(page)
            with self.subTest(page=page[:6]):
                self.assertLessEqual(len(calls), 2 * 2000)

    def test_page_notes_answers_for_markdown_only_and_never_raises(self):
        self.assertEqual(page_notes('html', WEAK_PAGE), [])
        for content in (None, 12, b'x', ['x'], ''):
            with self.subTest(content=content):
                self.assertEqual(page_notes('markdown', content), [])
        with patch.object(documents, '_notes', side_effect=RuntimeError):
            self.assertEqual(page_notes('markdown', WEAK_PAGE), [])
        self.assertEqual(page_notes('markdown', WEAK_PAGE), notes(WEAK_PAGE))
        self.assertEqual(notes('+ ' * 999), [])

    def test_lines_of_each_kind_never_raise(self):
        parts = ['', '>', '> [!NOTE]', '```', '~~~', '| a |', '|---|', '- x', '1. y', '# h', '  - z', '| `|` | \\| |',
                 '\t', '> > a — b — c', '****', '- [ ] task', '46. x', '|', '- ', '1.', '> [!TIP] - a', '| ![', '&;']
        for first in parts:
            for second in parts:
                for third in parts:
                    found = notes('\n'.join((first, second, third, first)))
                    self.assertTrue(set(found) <= set(NOTES))


class LimitTests(unittest.TestCase):
    """The cut: the first 2,000 lines of the first 200,000 characters, each line cut to 1,000 characters."""

    def test_the_cut_at_each_limit(self):
        def cut(content):
            return documents._cut(content)[0]
        self.assertEqual(len(cut('x\n' * 1999 + 'x')), 2000)
        self.assertEqual(len(cut('x\n' * 2000 + 'x')), 2000)
        self.assertEqual(cut('y' * 1000), ['y' * 1000])
        self.assertEqual(cut('y' * 1001), ['y' * 1000])
        full = ('z' * 499 + '\n') * 399 + 'z' * 500
        self.assertEqual(len(full), 200_000)
        self.assertEqual(cut(full)[-1], 'z' * 500)
        self.assertEqual(cut(full + 'zz\nz')[-1], 'z' * 500)

    def test_a_note_at_each_limit_and_above_it(self):
        callout = '> [!NOTE]\n> - a'
        self.assertEqual(notes('x\n\n' * 999 + callout), [NOTE_CALLOUT])
        self.assertEqual(notes('x\n\n' * 999 + 'x\n' + callout), [])
        item = '- a — b — c'
        self.assertEqual(notes('- ' + 'x' * (1000 - 2 - len(item[1:])) + item[1:]), [NOTE_DASHES])
        self.assertEqual(notes('- ' + 'x' * (1001 - 2 - len(item[1:])) + item[1:]), [])
        # A list item that the cut shortens keeps a note that its first 1,000 characters show.
        self.assertEqual(notes(item + ' ' + 'x' * 1200), [NOTE_DASHES])
        filler = ('y' * 999 + '\n') * 199
        rest = 200_000 - len(filler) - len(item)
        self.assertEqual(notes(filler + 'y' * (rest - 1) + '\n' + item), [NOTE_DASHES])
        self.assertEqual(notes(filler + 'y' * rest + '\n' + item), [])

    def test_a_table_that_the_cut_shortens_gives_no_note(self):
        # The rows after the cut could have another first cell, so the table gives no note.
        rows = '| Day | Time | Event | Owner |\n|---|---|---|---|\n' + '| 2026-11-12 | 09:00 | Check | Ada |\n' * 1997
        self.assertEqual(notes(rows), [NOTE_NAME])
        self.assertEqual(notes(rows + '| 2026-11-12 | 09:00 | Check | Ada |\n| 2026-11-13 | 10:00 | Check | Ada |\n'), [])
        # A row that the cut makes shorter is a row that the reader cannot be sure of.
        long_row = '| ' + 'a' * 1200 + ' | b | c | d |\n'
        self.assertEqual(notes('| A | B | C | D |\n|---|---|---|---|\n' + long_row * 3), [])


def worst_pages() -> dict:
    """The largest input that the cut lets through whole, of the worst kind for each rule: 190 lines of 1,000
    characters, or 1,990 lines of 99 characters. A page that the cut shortens does less work: a table that runs to the
    cut gives no note."""
    def lines(text, width=1000, count=190):
        return '\n'.join([(text * width)[:width]] * count)
    pages = {name: lines(unit) for name, unit in (('pipes', '|'), ('dashes', ' — '), ('quote marks', '>'),
                                                    ('plus marks', '+ '), ('digits', '7'), ('links', '[x]('),
                                                    ('entities', '&amp;'))}
    pages['a table of 500 columns'] = '|' + 'a|' * 499 + '\n|' + '-|' * 499 + '\n' + lines('|')
    pages['a callout of links'] = '> [!NOTE]\n' + lines('> [x](y) ')
    pages['a table with dashes'] = '| a | b | c | d |\n|---|---|---|---|\n' + lines('| a — b — ')
    pages['a table with the same first cells'] = '| a | b | c | d |\n|---|---|---|---|\n' + lines('| x ')
    pages['a header of links'] = lines('| [a](') + '\n|---|\n' + lines('|')
    pages.update({name + ', short lines': lines(unit, 99, 1990) for name, unit in (
        ('pipes', '|'), ('quote marks', '> x'), ('list items', '- a — b — '), ('table rows', '| x | a — b — c | '))})
    pages['a table of short rows'] = '| a | b | c | d |\n|---|---|---|---|\n' + lines('| x | a — b — c | ', 99, 1990)
    return pages


class SizeTests(unittest.TestCase):
    """The work of the notes has a bound by design. Each page runs in a fresh process, and the test checks the memory,
    never a time: the time is in the report of the change."""

    def test_the_largest_inputs_end_with_little_memory(self):
        child = ('import sys, tracemalloc\nfrom coconut import documents\nfrom test_page_notes import worst_pages\n'
                 'page = worst_pages()[sys.argv[1]]\ntracemalloc.start()\ndocuments._notes(page)\n'
                 'print(tracemalloc.get_traced_memory()[1] / 2 ** 20)\n')
        environment = {'PYTHONPATH': f'{ROOT / "backend"}:{Path(__file__).parent}', 'PYTHONDONTWRITEBYTECODE': '1'}
        for name in worst_pages():
            with self.subTest(page=name):
                run = subprocess.run([sys.executable, '-c', child, name], capture_output=True, text=True, timeout=60,
                                     env=environment)
                self.assertEqual(run.returncode, 0, run.stderr[-2000:])
                self.assertLess(float(run.stdout), 5)

    def test_a_page_of_20_megabytes_reads_only_its_cut(self):
        page = ('> [!NOTE]\n> - a\n' + 'x' * 1000 + '\n\n') * 20_000
        self.assertEqual(notes(page), [NOTE_CALLOUT])
        self.assertEqual(notes(page[:200_000]), notes(page))


if __name__ == '__main__':
    unittest.main()
