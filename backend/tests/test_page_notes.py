"""The notes of a save (documents.page_notes): what a Markdown page can do better, for the rules that Coconut can check
exactly. The pages have the shape of real pages, with neutral data."""
from pathlib import Path
import re
import time
import unittest

from coconut import documents
from coconut.documents import (NOTE_CALLOUT, NOTE_DASHES, NOTE_HEADER, NOTE_METRICS, NOTE_NAME, page_notes)

ROOT = Path(__file__).resolve().parents[2]
PAGE_JS = ROOT / 'ios/Coconut/Resources/Library/page.js'
# The example page of the skill (skills/coconut/references/pages.md) and a weak page with each fault of the real pages:
# a callout with a list, the key numbers in the first paragraph and in a quote, a header of 18 characters, the same
# word first in each row, a cell with three facts and dashes, and a chart of 30 days as the numbers 1 to 30.
EXAMPLE = (ROOT / 'skills/coconut/references/pages.md').read_text(encoding='utf-8') \
    .split('````markdown\n', 1)[1].split('\n````', 1)[0]
WEAK = (Path(__file__).parent / 'fixtures/weak_page.md').read_text(encoding='utf-8')

TABLE = '| Product | Orders |\n|---|---|\n| Ink sketching | 131 |\n| Watercolor basics | 168 |'


def notes(content):
    return page_notes('markdown', content)


class NumberTests(unittest.TestCase):
    """page.js and the notes have one meaning of "number"."""

    EXAMPLES = (('1,552 steps', True), ('$41,200', True), ('87%', True), ('-3', True), ('27.6 in', True),
                ('12k', True), ('−$2.1k', True), ('−\u2060$2.1k', True), ('6 days', True), ('+4.5M', True),
                ('€9', True), ('0', True),
                ('#4404', False), ('2026-11-07', False), ('v1.2', False), ('3rd', False), ('Ada', False), ('', False),
                ('12 characters of unit', False), ('1 2', False), ('1,', False), ('$', False), ('12kg', False))

    def test_the_pattern_has_the_text_of_page_js(self):
        script = PAGE_JS.read_text(encoding='utf-8')
        source = re.search(r'const number = \(text\) => text\s*\.match\(/(.*?)/s\);', script, re.S)[1]
        self.assertEqual(documents._NUMBER.pattern, source)
        self.assertEqual(documents._NUMBER.flags & re.S, re.S)

    def test_both_patterns_give_the_same_answer_for_each_example(self):
        script = PAGE_JS.read_text(encoding='utf-8')
        page_js = re.compile(re.search(r'\.match\(/(\^\(\[\+.*?)/s\);', script, re.S)[1], re.S)
        for text, number in self.EXAMPLES:
            with self.subTest(text=text):
                self.assertEqual(bool(documents._NUMBER.match(text)), number)
                self.assertEqual(bool(page_js.match(text)), number)


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

    def test_a_callout_with_a_list_or_with_long_text(self):
        self.assertEqual(notes('Lead.\n\n> [!NOTE] Open work\n> - Ada fixes the checkout.\n> - Grace reviews it.'),
                         [NOTE_CALLOUT])
        self.assertEqual(notes('Lead.\n\n> [!WARNING]\n> 1. Ada fixes the checkout.'), [NOTE_CALLOUT])
        self.assertEqual(notes('Lead.\n\n> [!TIP]\n> ' + 'The shop sells well. ' * 15), [NOTE_CALLOUT])
        # One or two sentences, a plain quote with a list, and a list after the callout get no note.
        self.assertEqual(notes('Lead.\n\n> [!IMPORTANT] Decision\n> Approve the new price before Friday.'), [])
        self.assertEqual(notes('Lead.\n\n> - Ada fixes the checkout.\n> - Grace reviews it.'), [])
        self.assertEqual(notes('Lead.\n\n> [!NOTE]\n> Approve it.\n\n- Ada fixes the checkout.'), [])

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

    def test_the_same_text_first_in_each_row(self):
        rows = ''.join(f'| Issue | {title} | Ada | Open |\n' for title in ('Checkout', 'Refund email', 'Tax rate'))
        page = 'Lead.\n\n| Kind | Title | Owner | State |\n|---|---|---|---|\n' + rows
        self.assertEqual(notes(page), [NOTE_NAME.format('Issue')])
        # Three columns, two rows, or another text in one row get no note.
        self.assertEqual(notes(page.replace(' | Open |', ' |').replace('| State |', '|').replace('|---|---|---|---|',
                                                                                                '|---|---|---|')), [])
        self.assertEqual(notes(page.rsplit('| Issue |', 1)[0]), [])
        self.assertEqual(notes(page.replace('| Issue | Tax rate', '| Pull request | Tax rate')), [])

    def test_a_cell_or_an_item_with_several_dashes(self):
        cell = 'Issue — #4404 Checkout fails — since 2026-11-07'
        self.assertEqual(notes(f'Lead.\n\n| Item | Owner |\n|---|---|\n| {cell} | Ada |'), [NOTE_DASHES])
        self.assertEqual(notes(f'Lead.\n\n- {cell}'), [NOTE_DASHES])
        self.assertEqual(notes('Lead.\n\n- Issue – #4404 – since 2026-11-07'), [NOTE_DASHES])
        # One dash, dashes without spaces, and dashes in a paragraph get no note.
        self.assertEqual(notes('Lead.\n\n- Checkout — open since 2026-11-07\n- 2026-11-07—2026-11-12'), [])
        self.assertEqual(notes(f'{cell}.'), [])


class NoteTests(unittest.TestCase):
    def test_at_most_three_notes_in_the_order_of_the_rules(self):
        self.assertEqual(notes(WEAK), [NOTE_METRICS, NOTE_CALLOUT, NOTE_HEADER.format('Orders of the week')])
        for note in (NOTE_METRICS, NOTE_CALLOUT, NOTE_HEADER, NOTE_NAME, NOTE_DASHES):
            with self.subTest(note=note):
                self.assertLessEqual(len(note.format('x' * 40)), 160)
                self.assertTrue(note.endswith('.'))

    def test_the_example_of_the_skill_has_no_note(self):
        self.assertEqual(notes(EXAMPLE), [])
        self.assertIn('```chart', EXAMPLE)
        self.assertEqual(EXAMPLE, EXAMPLE.strip())

    def test_the_notes_of_the_weak_page_lead_to_a_page_without_notes(self):
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


class ReaderTests(unittest.TestCase):
    """Text that could break a reader of Markdown gives notes or none, and never an error."""

    def test_an_html_page_and_content_that_is_not_text_get_no_notes(self):
        self.assertEqual(page_notes('html', WEAK), [])
        for content in (None, 12, b'x', ['x'], ''):
            with self.subTest(content=content):
                self.assertEqual(page_notes('markdown', content), [])

    def test_a_pipe_inside_code_and_an_escaped_pipe(self):
        page = ('Lead with 412 orders and 3 refunds.\n\n| Command | Use |\n|---|---|\n| `a | b` | pipe |\n'
                '| a \\| b | escaped |')
        self.assertEqual(notes(page), [NOTE_METRICS])
        self.assertEqual(notes('Lead.\n\n| `x | y` |\n|---|\n| 1 |'), [])

    def test_a_table_inside_a_callout_counts(self):
        page = ('Lead.\n\n> [!NOTE] Orders\n> | Product | Orders of the week |\n> |---|---|\n> | Ink | 131 |')
        self.assertEqual(notes(page), [NOTE_HEADER.format('Orders of the week')])

    def test_an_unclosed_fence_hides_the_rest_of_the_page(self):
        self.assertEqual(notes('Lead.\n\n```\n| Product | Orders of the week |\n|---|---|\n| Ink | 131 |'), [])
        self.assertEqual(notes('Lead with 412 orders and 3 refunds.\n\n```chart\n{"type": "bar"'), [NOTE_METRICS])
        # A fence in a callout ends with the callout.
        self.assertEqual(notes('> [!NOTE]\n> ```\n> x\n\n| Product | Orders of the week |\n|---|---|\n| Ink | 1 |'),
                         [NOTE_HEADER.format('Orders of the week')])
        # A longer fence ends only with a fence of its length.
        self.assertEqual(notes('````\n```\n| Product | Orders of the week |\n|---|---|\n| Ink | 1 |\n````'), [])

    def test_headings_and_lines_under_a_paragraph_are_no_tables(self):
        self.assertEqual(notes('A heading | with a pipe\n---\n\n- Issue — #1 — open'), [NOTE_DASHES])
        self.assertEqual(notes('The shop had 412 orders and 3 refunds.\n===\n\n' + TABLE), [])

    def test_a_very_long_line_and_a_large_page_take_little_time(self):
        pages = ('x' * 500_000, '|' * 500_000, '> ' * 200_000 + 'x', '-' * 500_000, '[!NOTE]' * 70_000,
                 ('| a | b |\n|---|---|\n' + '| 1 | 2 |\n' * 40_000), '`' * 500_000, ' — ' * 150_000,
                 'Lead 1 2.\n\n' + '| ' + ' | '.join(['x'] * 100_000) + ' |\n|---|\n')
        for page in pages:
            with self.subTest(page=page[:20]):
                started = time.perf_counter()
                self.assertIsInstance(notes(page), list)
                self.assertLess(time.perf_counter() - started, 2)

    def test_lines_of_each_kind_never_raise(self):
        parts = ['', '>', '> [!NOTE]', '```', '~~~', '```chart', '| a |', '|---|', '---', '===', '- x', '1. y',
                 '# h', '  - z', 'text 1 2', '| `|` | \\| |', '\t', '> > nested — a — b', '****', '- [ ] task']
        for first in parts:
            for second in parts:
                for third in parts:
                    page = '\n'.join((first, second, third, first))
                    self.assertIsInstance(notes(page), list)


if __name__ == '__main__':
    unittest.main()
