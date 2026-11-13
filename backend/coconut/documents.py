"""Documents: pages that a bot keeps in the Library of the phone.

A document belongs to the bot that saved it, under a key that the bot chooses. A save with the same key replaces the
content and adds a version, so a bot updates one document and never makes a copy of it. A document is Markdown, which
the phone draws natively, or HTML, which the phone shows in a closed web view with the design tokens of Coconut.

Coconut owns the style of a page and the bot owns its structure. A save of HTML fails when the code of the page sets
its own font, writes a color as a value and not as a token, or loads something from the network. The reason goes back
to the bot, which then fixes the page. A save of Markdown always works, and its answer can have notes: what the page
can do better, for the rules that Coconut can check exactly (page_notes). A document can end on a date. After that
date the Library no longer shows it. The person can pin a document, and the Library shows the pinned documents first.

This module imports only the standard library at its top, so the gateway plugin can load its schemas
(hermes_plugin/coconut/tools.py).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, time as day_time, timedelta, timezone
from html.parser import HTMLParser

SCHEMA = '''
CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS documents_key ON documents
    (json_extract(data,'$.bot_id'), json_extract(data,'$.key'));
CREATE TABLE IF NOT EXISTS document_versions (document_id TEXT NOT NULL, version INTEGER NOT NULL,
    content TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (document_id, version));
'''
FORMATS = ('markdown', 'html')
KEY = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
KEY_RULE = 'key must be 1 to 64 lowercase letters, digits and hyphens, for example signups-daily.'
MAX_CONTENT = 512 * 1024
MAX_TITLE = 120
MAX_SUMMARY = 200
KEPT_VERSIONS = 10
LONGEST_LIFE = timedelta(days=366)
# The audience of a page on a Mac that runs Coconut for a team only (team_only.py): "team" sends the page to each team
# of its bot. On a person's Mac only the person shares a page, so a save there names no audience (team_pages.py).
AUDIENCES = ('private', 'team')
BOT_SHARE = 'Only the person can share a page with a team. Save the page without audience.'
# What a save outside a Coconut chat turn tells the bot. No chat shows a card for the page, so the bot must say where
# the person finds it (hermes_plugin/coconut/tools.py adds it to the result).
OUTSIDE_SAVE_NOTE = ('Coconut keeps this page in the Library of the Coconut app. No chat shows a card for it. Tell the '
                     'person that the page is in the Library.')

# The page rules of HTML read only the code of a page, which an HTML parser finds: the CSS of its <style> elements
# and style attributes, its scripts and event attributes, its tags, and the color attributes of its tags. The text of
# the page can say anything, for example "PR #377", "Last fetch: 10:00" or 'style="color:red"'. The content security
# policy of the phone is the real guard against loads. These rules tell the bot what to fix.
_COLOR_NAMES = ('red|green|blue|black|white|gray|grey|orange|yellow|purple|pink|brown|cyan|magenta|navy|teal|maroon|'
                'olive|lime|aqua|silver|gold|indigo|violet|crimson|coral|salmon|tomato|orchid|turquoise|khaki|beige|'
                'lavender|steelblue|skyblue|darkblue|darkred|darkgreen|lightblue|lightgray|lightgrey|darkgray|darkgrey')
_HEX = r'(?<![\w.$-])#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})(?![\w-])'
_COLOR_ADVICE = ('Use a token: var(--c1) to var(--c6), var(--text), var(--text-2), var(--bg), var(--surface) or '
                 'var(--line), or coconut.palette in a script.')
_FONT = 'The page sets a font. Remove it: Coconut sets the font of each page.'
_HEX_REASON = 'The page writes a color as a hex value. ' + _COLOR_ADVICE
_NAME_REASON = 'The page writes a color by name. ' + _COLOR_ADVICE
_UNREADABLE = ('The page has HTML that Coconut cannot read. Write plain HTML: give each tag its end, and remove '
               'sections such as <![...]>.')
_FILE = 'The page loads another file. Coconut gives d3 and Plot to each page. Put everything else in the page.'
_NETWORK = 'The page loads something from the network. Pages have no network access. Put the data in the page.'
_COLOR_PROPERTY = r'(?:color|background(?:-color)?|fill|stroke|border(?:-[a-z]+)?|outline|box-shadow|text-shadow)'


class _ColorByName:
    """The rule "a color by name": a color property whose value has a color name, or a color name in quotes.

    It has the `search` of a pattern. One pattern with a lazy scan from each property to the end of its value took
    minutes on a long value without an end, for example "color:" 80,000 times. Here the value of the first property
    of each declaration is read once. A later property of the same declaration has its value inside that one.
    """
    _property = re.compile(_COLOR_PROPERTY + r'\s*:', re.I)
    _name = re.compile(r'\b(?:' + _COLOR_NAMES + r')\b', re.I)
    _quoted = re.compile(r'[\'"`]\s*(?:' + _COLOR_NAMES + r')\s*[\'"`]', re.I)
    _end = re.compile(r'[;{}]')

    def search(self, text: str) -> bool:
        if self._quoted.search(text):
            return True
        start = 0
        while found := self._property.search(text, start):
            end = self._end.search(text, found.end())
            stop = end.start() if end else len(text)
            if self._name.search(text, found.end(), stop):
                return True
            start = stop
        return False
_COLOR_ATTRIBUTES = {'fill', 'stroke', 'color', 'bgcolor', 'stop-color', 'flood-color', 'lighting-color'}
_FILE_TAGS = {'link', 'iframe', 'object', 'embed', 'base', 'frame', 'applet'}
_LOAD_ATTRIBUTES = {'src', 'srcset', 'poster', 'action', 'data', 'href', 'xlink:href', 'background'}
_REMOTE = re.compile(r'^\s*(?:[a-z][a-z0-9+.-]*:)?//', re.I)
# (the part of the page, the pattern, the reason). "css" is the text of each <style> element and style attribute
# without its selectors, and "scripts" the text of each script and event attribute without its comments. "code" is both. The rules are wide
# on purpose: a refused page costs the bot one fix, and a page with its own colors breaks the look of the Library.
HTML_RULES = (
    ('code', re.compile(r'font-family|fontFamily|\bfont\s*:', re.I), _FONT),
    ('css', re.compile(_HEX, re.I), _HEX_REASON),
    ('scripts', re.compile(_HEX, re.I),
     _HEX_REASON + ' If it is an element ID or a number, write it another way, for example "No. 377".'),
    ('code', re.compile(r'\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color-mix)\s*\(', re.I),
     'The page writes a color as a function. ' + _COLOR_ADVICE),
    ('code', _ColorByName(), _NAME_REASON),
    ('scripts', re.compile(r'\bd3\.(?:scheme|interpolate(?:Rgb|Hsl|Lab|Hcl|Cubehelix|Turbo|Viridis|Inferno|Magma|'
                           r'Plasma|Warm|Cool|Rainbow|Sinebow|Blues|Greens|Greys|Oranges|Purples|Reds|Spectral|RdBu|'
                           r'RdYlGn))'),
     'The page uses a color scheme of d3. ' + _COLOR_ADVICE),
    ('colors', re.compile(_HEX + r'|\b(?:rgba?|hsla?)\s*\(|^\s*(?:' + _COLOR_NAMES + r')\s*$', re.I),
     'A tag has a color attribute with a value. ' + _COLOR_ADVICE),
    ('code', re.compile(r'@import\b|url\(\s*[\'"]?\s*(?:[a-z][a-z0-9+.-]*:)?//', re.I), _NETWORK),
    ('scripts', re.compile(r'\bfetch\s*\(|\b(?:XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts)\b|'
                           r'\bimport\s*\('),
     'The page script uses the network. Pages have no network access. Put the data in the page.'),
)
# A string or a comment of a script. The strings stay, so a // in a string is not a comment.
_SCRIPT_TOKENS = re.compile(r'("(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\])*`)|//[^\n]*|/\*.*?(?:\*/|$)',
                            re.S)


# An expression without its "}" runs to the end of the string. With a "}" that must come, a template string with
# many "${" took more than 30 seconds to check, because each "${" read to the end of the string again.
_TEMPLATE_EXPRESSION = re.compile(r'\$\{[^}]*\}?')
_COMMENT = re.compile(r'/\*.*?\*/|//[^\n]*', re.S)


def _script_code(script: str) -> str:
    """A script without its comments, also the comments of the ${...} expressions of a template string."""
    def kept(match):
        text = match.group(1)
        if text is None:
            return ' '
        if text.startswith('`'):
            return _TEMPLATE_EXPRESSION.sub(lambda expression: _COMMENT.sub(' ', expression.group(0)), text)
        return text
    return _SCRIPT_TOKENS.sub(kept, script)


def _declarations(css: str) -> str:
    """CSS without its selectors, in one pass: the text before each { goes. An ID selector such as #add is not a
    color, and a class such as .red is not a color name."""
    parts = re.split(r'([{};])', css)
    return ''.join('' if index + 1 < len(parts) and parts[index + 1] == '{' else part
                   for index, part in enumerate(parts))

# The recipe of a page and its blocks (page.js in the app). The save tool names them, so each bot knows them. The
# coconut skill has the full recipe with an example (skills/coconut/references/pages.md).
BLOCKS = ('Write a page like a short report. First, one or two sentences that state the point. Then the key '
          'numbers: a table with one row of numbers shows them large, with a label in each header. Then sections, '
          'each with a short heading and one table or one chart. A callout is only for one decision or one warning, '
          'in one or two sentences without a list: a quote that starts with [!NOTE], [!TIP], [!IMPORTANT], '
          '[!WARNING] or [!CAUTION], with an optional title on the same line. A table that does not fit a phone shows '
          'each row as a record with the first cell as its name and a short last cell at its end, so put the name '
          'column first and the status last. Give each column one fact, not facts joined by dashes, and a header of at '
          'most 16 characters. A ```chart block draws a graph from JSON {"type": "bar", "line", "area" or "dot", "data": [...], "x": "field", "y": '
          '"field", "series": "field", "title": "..."}: give days as dates (YYYY-MM-DD). A ```mermaid block draws a '
          'diagram (flowchart, sequence, timeline, gantt, pie). Task lists and photos work too. HTML pages have the '
          'same blocks: <pre class="mermaid">, <aside class="callout tip"> and <p class="lead">. The coconut skill '
          'has the recipe with an example: references/pages.md.')
SAVE_SCHEMA = {'name': 'coconut_save_document', 'description': (
    'Save a document in the Library of Coconut, where the person reads it on the phone. A save with an existing key '
    'replaces that document and adds a version. Use a document for content that outlives the chat: something to '
    'track, to reuse or to check later. A short answer stays in the chat. ' + BLOCKS + ' '
    'HTML suits an interactive page. Coconut sets the font, the colors and the spacing of '
    'each page: in HTML use only these tokens for color: var(--c1) to var(--c6), var(--text), var(--text-2), '
    'var(--bg), var(--surface), var(--line), or coconut.palette (an array of the six colors) in a script. A page has '
    'd3 and Plot (Observable Plot) and has no network access. Coconut refuses a page that sets a font, writes a '
    'color as a value or loads something, and names the reason. The answer of a save can have notes: apply them in '
    'the next save of the page. The tool also works outside a Coconut chat, for '
    'example in another messaging app: there no chat shows a card, so tell the person that the page is in the '
    'Library of Coconut.'),
    'parameters': {'type': 'object', 'properties': {
        'key': {'type': 'string', 'description': (
            'A stable name for this document, in lowercase letters, digits and hyphens, for example signups-daily. '
            'Use the same key to update the document.')},
        'title': {'type': 'string', 'maxLength': MAX_TITLE, 'description': 'The title that the Library shows.'},
        'format': {'type': 'string', 'enum': list(FORMATS)},
        'content': {'type': 'string', 'description': (
            'The full document: Markdown, or the body of an HTML page with its scripts. At most 512 KB. The Library '
            'shows the title as the heading of the page, so do not start the content with it.')},
        'summary': {'type': 'string', 'maxLength': MAX_SUMMARY, 'description': (
            'One short line about the current content, for example "412 signups today, up 9%".')},
        'expires_at': {'type': 'string', 'description': (
            'For a temporary document: the date (YYYY-MM-DD) or the time (ISO 8601) after which the Library no '
            'longer shows it. Omit it for a document that stays. Send an empty text to remove an end date.')}},
        'required': ['key', 'title', 'format', 'content']}}
READ_SCHEMA = {'name': 'coconut_read_document', 'description': (
    'Read a document of this bot from the Library of Coconut, with its content, to update it. Without a key, list '
    'the documents of this bot.'),
    'parameters': {'type': 'object', 'properties': {
        'key': {'type': 'string', 'description': 'The key of the document. Omit it to list the documents.'}}}}
DELETE_SCHEMA = {'name': 'coconut_delete_document', 'description': (
    'Delete a document of this bot from the Library of Coconut, with all its versions.'),
    'parameters': {'type': 'object', 'properties': {
        'key': {'type': 'string', 'description': 'The key of the document.'}},
        'required': ['key']}}


class _PageCode(HTMLParser):
    """The code of a page, in the parts of HTML_RULES, and the reasons that its tags give."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = {'css': [], 'scripts': [], 'colors': []}
        self.reasons = []
        self.element = None

    def handle_starttag(self, tag, attributes):
        self.element = tag if tag in ('style', 'script') else None
        if tag == 'font':
            self.reasons.append(_FONT)
        if tag in _FILE_TAGS or (tag == 'script' and any(name == 'src' for name, _ in attributes)):
            self.reasons.append(_FILE)
        for name, value in attributes:
            value = value or ''
            if name == 'style':
                self.parts['css'].append(_declarations(value))
            elif name.startswith('on'):
                self.parts['scripts'].append(_script_code(value))
            elif name in _COLOR_ATTRIBUTES:
                self.parts['colors'].append(value)
            elif name in _LOAD_ATTRIBUTES and name != 'href' and any(
                    _REMOTE.match(part) for part in value.split(',')):
                self.reasons.append(_NETWORK)
            elif name in ('href', 'xlink:href') and tag not in ('a',) and _REMOTE.match(value):
                self.reasons.append(_NETWORK)

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        self.element = None

    def handle_data(self, data):
        if self.element == 'script':
            self.parts['scripts'].append(_script_code(data))
        elif self.element == 'style':
            self.parts['css'].append(_declarations(data))


def page_problems(content: str) -> list[str]:
    """The page rules that an HTML document breaks, each reason once.

    The HTML parser of Python can raise on some markup, for example an AssertionError for a marked section with an
    unknown keyword such as <![foo]> on some versions. That is a problem of the page, not of the service: a local save
    answers 400 with the reason, and a team page with that markup is not valid.
    """
    page = _PageCode()
    try:
        page.feed(content)
        page.close()
    except Exception:
        return [_UNREADABLE]
    parts = dict(page.parts, code=page.parts['css'] + page.parts['scripts'])
    problems = list(dict.fromkeys(page.reasons))
    for part, pattern, reason in HTML_RULES:
        if reason not in problems and any(pattern.search(text) for text in parts[part]):
            problems.append(reason)
    return problems


# The notes of a save: what a Markdown page can do better, for four rules that the lines of the page show without a
# guess. A note never blocks a save. When in doubt there is no note: each rule of the reader below can only remove a
# note, never add one. A note is one fixed sentence for each rule. It names no place and repeats no text of the page.
#
# The work has a bound by design. page_notes reads only the first MOST_LINES lines of the first MOST_CHARACTERS
# characters, cuts each line to LONGEST_LINE characters, and makes one pass over these lines with plain string
# methods. No step calls itself, and the result depends only on the page.
MOST_NOTES = 3
MOST_LINES = 2000
LONGEST_LINE = 1000
MOST_CHARACTERS = 200_000
LONGEST_HEADER = 16
LONGEST_CALLOUT = 300
FEWEST_COLUMNS = 4
FEWEST_ROWS = 3
FEWEST_DASHES = 2
NOTE_CALLOUT = 'A callout holds one or two sentences. Make it shorter, and put the rest in the page.'
NOTE_HEADER = 'A column header is longer than 16 characters. Make it shorter.'
NOTE_NAME = 'Each row of a table starts with the same text. Put the column that names each row first.'
NOTE_DASHES = 'A table cell or a list item packs several facts with dashes. Give each fact its own column.'
_CALLOUT_KINDS = ('[!NOTE]', '[!TIP]', '[!IMPORTANT]', '[!WARNING]', '[!CAUTION]')
# Only an em dash between spaces: an en dash between spaces is the sign of a range, as in "Mon – Fri".
_DASH = ' — '
# The marks of Markdown and HTML that a header cell can show fewer of than it has. _shown leaves them out.
_HIDDEN = '*_~`[]!<>'


def _cut(content: str) -> tuple[list[str], set[int]]:
    """The lines that the notes read: the first MOST_LINES lines of the first MOST_CHARACTERS characters, each cut to
    LONGEST_LINE characters, and the numbers of the lines that the cut made shorter. The last line counts as cut when
    the page goes on after the cut, because the reader cannot see what follows it."""
    head = content[:MOST_CHARACTERS]
    lines = head.split('\n', MOST_LINES)
    more = len(lines) > MOST_LINES or len(content) > MOST_CHARACTERS
    lines = lines[:MOST_LINES]
    cut = {index for index, line in enumerate(lines) if len(line) > LONGEST_LINE}  # At most MOST_LINES lines.
    if more:
        cut.add(len(lines) - 1)
    return [line[:LONGEST_LINE] for line in lines], cut


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(' '))


def _opens_code(line: str) -> bool:
    """Whether a line can open code: a fence after optional spaces and quote marks, a tab or four spaces first."""
    if line.startswith('\t') or _indent(line) >= 4:
        return True
    return line.lstrip(' >').startswith(('```', '~~~'))


def _shown(cell: str) -> int:
    """The fewest characters that a header cell can show: without the marks of _HIDDEN, without the address or label
    of a link ("(...)" or "[...]" right after "]"), without the text of a photo ("![...]"), and with an entity
    ("&...;") as one character. The cell shows at least this many characters, so a note on it is true. Errs toward
    no note."""
    count = index = 0
    while index < len(cell):  # One cell of one cut line: at most LONGEST_LINE characters.
        char = cell[index]
        if index and (char in '([' and cell[index - 1] == ']' or char == '[' and cell[index - 1] == '!'):
            end = cell.find(')' if char == '(' else ']', index)
            index = len(cell) if end < 0 else end + 1
            continue
        if char == '&':
            end = cell.find(';', index, index + 33)
            if end > index:
                count, index = count + 1, end + 1
                continue
        count += char not in _HIDDEN
        index += 1
    return count


def _list_item(line: str) -> str | None:
    """The text of a list item for the dash rule: "- ", "* ", "+ ", or digits and ". " after at most three spaces."""
    if _indent(line) > 3:
        return None
    text = line.strip()
    if text[:2] in ('- ', '* ', '+ '):
        return text[2:]
    digits = len(text) - len(text.lstrip('0123456789'))
    return text[digits + 2:] if digits and text[digits:digits + 2] == '. ' else None


def _packed(text: str) -> bool:
    """Whether a text joins several facts with em dashes. A text with a link can hide its dashes in the title or the
    address of the link, so it counts as not packed. Errs toward no note."""
    return '](' not in text and text.count(_DASH) >= FEWEST_DASHES


def _cells(line: str) -> list[str]:
    """The cells of a table line that starts with "|". A pipe in code or after a backslash would split a cell wrongly,
    so a table with a backtick or a backslash gives no note."""
    cells = line.strip()[1:].split('|')  # One cut line: at most LONGEST_LINE cells.
    if not cells[-1].strip():
        cells.pop()
    return [cell.strip() for cell in cells]


def _is_delimiter(line: str) -> bool:
    text = line.strip()
    return _indent(line) <= 3 and text.startswith('|') and '-' in text and not text.strip('|-: ')


def _callout_notes(lines: list[str]) -> bool:
    """Whether a callout holds a list item or more than LONGEST_CALLOUT characters. The lines are a run of quote lines
    after the line of the mark. A list item here is "- ", "* ", "+ ", "1. " or "1) ": a wrapped line can start with
    another number, as "46. That is the first time." Errs toward no note."""
    size = 0
    for line in lines:  # At most MOST_LINES lines.
        text = line.lstrip(' >').strip()
        is_break = not text.replace(' ', '').strip('-*_')
        if (text[:2] in ('- ', '* ', '+ ') or text[:3] in ('1. ', '1) ')) and not is_break:
            return True
        size += len(text)
    return size > LONGEST_CALLOUT


def _table_notes(lines: list[str], unsure: bool) -> tuple[bool, bool, bool]:
    """The notes of one table: a long header, the same first cell in each row, a packed cell. The lines are its header,
    its delimiter and its body. A table that the reader cannot be sure of gives no note."""
    head = _cells(lines[0])
    if unsure or any('`' in line or '\\' in line for line in lines) or len(head) != len(_cells(lines[1])):
        return False, False, False
    long = any(_shown(cell) > LONGEST_HEADER for cell in head)
    rows = lines[2:]
    # The first cell of each row, and the cells of a row only when the row has two dashes. At most MOST_LINES rows.
    firsts = {row.strip()[1:].split('|', 1)[0].strip() for row in rows}
    same = len(head) >= FEWEST_COLUMNS and len(rows) >= FEWEST_ROWS and len(firsts) == 1 and '' not in firsts
    packed = any(_packed(cell) for row in rows if row.count(_DASH) >= FEWEST_DASHES for cell in _cells(row))
    return long, same, packed


def _notes(content: str) -> list[str]:
    """The notes of a Markdown page, in the order of the rules, each at most once: a callout, a header, the names of
    the rows, dashes."""
    lines, cut = _cut(content)
    found = set()
    index = 0
    while index < len(lines):  # At most MOST_LINES lines; each line is read in one step below.
        line = lines[index]
        if _opens_code(line):
            # The reader stops at the first line that can open code, and reads nothing after it. Errs toward no note.
            break
        start = index == 0 or not lines[index - 1].strip()
        if _indent(line) <= 3 and line.lstrip(' ').startswith('>'):
            # A quote: a run of lines that start with ">", which ends at the first other line. It is a callout only
            # when it starts the page or follows a blank line, and its first line starts with a mark.
            end = index
            while end < len(lines) and _indent(lines[end]) <= 3 and lines[end].lstrip(' ').startswith('>'):
                end += 1
            if any(_opens_code(text) for text in lines[index:end]):
                break
            mark = line.lstrip(' >').strip().upper()
            if start and mark.startswith(_CALLOUT_KINDS) and _callout_notes(lines[index + 1:end]):
                found.add(NOTE_CALLOUT)
            index = end
            continue
        if line.lstrip(' ').startswith('|') and index + 1 < len(lines) and _is_delimiter(lines[index + 1]):
            # A table: a header, its delimiter, then body lines that start with "|". A table that the cut shortened,
            # or with a line of text right under it (marked reads it as a row), gives no note.
            end = index + 2
            while end < len(lines) and _indent(lines[end]) <= 3 and lines[end].lstrip(' ').startswith('|'):
                end += 1
            unsure = (any(number in cut for number in range(index, end))
                      or end < len(lines) and bool(lines[end].strip()))
            long, same, packed = _table_notes(lines[index:end], unsure)
            found.update(note for note, hit in ((NOTE_HEADER, long), (NOTE_NAME, same), (NOTE_DASHES, packed)) if hit)
            index = end
            continue
        item = _list_item(line)
        if item is not None and _packed(item):
            found.add(NOTE_DASHES)
        index += 1
    return [note for note in (NOTE_CALLOUT, NOTE_HEADER, NOTE_NAME, NOTE_DASHES) if note in found][:MOST_NOTES]


def page_notes(document_format: str, content: str) -> list[str]:
    """What a Markdown page can do better, at most MOST_NOTES notes in the order of the rules. An HTML page gets no notes.
    The function never raises: a note never stops a save."""
    if document_format != 'markdown' or not isinstance(content, str):
        return []
    try:
        return _notes(content)
    except Exception:
        return []


def _with_notes(answer: dict, notes: list[str]) -> dict:
    """The answer of a save, with its notes when it has some."""
    return dict(answer, notes=notes) if notes else answer


def end_time(value: str, current: datetime) -> str:
    """The end of a temporary document as UTC text. A date without a time ends at the end of that day on the Mac,
    and a time without a zone is the time of the Mac. Raise ValueError with the reason for the bot."""
    text = value.strip()
    try:
        if len(text) == 10:
            moment = datetime.combine(datetime.fromisoformat(text).date(), day_time.max).astimezone()
        else:
            moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
            moment = moment.astimezone() if moment.tzinfo is None else moment
    except ValueError:
        raise ValueError('expires_at must be a date (YYYY-MM-DD) or an ISO 8601 time.') from None
    if moment <= current:
        raise ValueError('expires_at must be in the future.')
    if moment - current > LONGEST_LIFE:
        raise ValueError('expires_at must be within one year. Omit it for a document that stays.')
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def expired(document: dict, current: str) -> bool:
    return bool(document.get('expires_at')) and document['expires_at'] <= current


def page_key(arguments: dict) -> str:
    """The key of a page in the arguments of a save, or of a delete of the tooling, or the reason to refuse it."""
    from .service_base import ServiceError
    key = arguments.get('key')
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ServiceError(400, KEY_RULE)
    return key


def page_to_save(arguments: dict) -> tuple[str, str, str, str, str | None]:
    """The key, title, format, content and summary of a page, or the reason to refuse it. The tools of a bot turn
    and the tooling of the Mac save a page through this, so both follow the page rules."""
    from .service_base import ServiceError, require_text
    key = page_key(arguments)
    document_format, content = arguments.get('format'), arguments.get('content')
    title = require_text(arguments, 'title', MAX_TITLE)
    if document_format not in FORMATS:
        raise ServiceError(400, 'format must be markdown or html.')
    if not isinstance(content, str) or not content.strip():
        raise ServiceError(400, 'content must not be empty.')
    if len(content.encode()) > MAX_CONTENT:
        raise ServiceError(413, 'content must be at most 512 KB. Keep less data in the document.')
    summary = arguments.get('summary')
    if summary is not None and (not isinstance(summary, str) or len(summary) > MAX_SUMMARY):
        raise ServiceError(400, f'summary must contain at most {MAX_SUMMARY} characters.')
    if document_format == 'html':
        problems = page_problems(content)
        if problems:
            raise ServiceError(400, 'Coconut did not save the page. ' + ' '.join(problems))
    return key, title, document_format, content, summary


def page_end(arguments: dict, current: datetime):
    """The end date of a page as UTC text: the new one, None to remove it, or ... to keep the one it has, which is
    what an omitted expires_at means."""
    from .service_base import ServiceError
    if 'expires_at' not in arguments:
        return ...
    ends = arguments.get('expires_at')
    if ends is not None and not isinstance(ends, str):
        raise ServiceError(400, 'expires_at must be a date (YYYY-MM-DD) or an ISO 8601 time.')
    try:
        return end_time(ends, current) if isinstance(ends, str) and ends.strip() else None
    except ValueError as error:
        raise ServiceError(400, str(error)) from None


class Documents:
    """The documents of the bots: the tools of a bot turn, the tooling of the Mac and the routes of the phone."""

    # True when a person on this Mac reads the Library. A Mac that runs Coconut for a team only has no reader, so a
    # team page of a bot that no team has yet waits on this Mac until the bot is shared (team_only.py).
    library_reader = True

    def _now(self) -> str:
        """The time of the store. Service gives the clock of service.py, which the tests of the full service set."""
        from .service_base import now
        return now()

    def save_document(self, task_id: str, arguments: dict) -> dict:
        """coconut_save_document of a bot turn: create the document of the key, or replace its content."""
        from .service_base import ServiceError
        key, title, document_format, content, summary = page_to_save(arguments)
        ends = page_end(arguments, datetime.now(timezone.utc))
        notes = page_notes(document_format, content)
        with self.transaction():
            task = self._running_task(task_id)
            bot = self._get('bots', task['bot_id'])
            if bot['archived'] or bot.get('_deleted'):
                raise ServiceError(409, 'The bot is archived or deleted.')
            audience = self._page_audience(arguments, self._document_of_key(bot['id'], key),
                                           (key, title, document_format, content, summary))
            document = self._write_document(task, bot, key, title, document_format, content, summary, expires=ends,
                                            audience=audience)
        return _with_notes({'status': 'saved', 'key': key, 'version': document['version'],
                            'expires_at': document.get('expires_at')}, notes)

    def publish_page(self, bot_id: str, arguments: dict) -> dict:
        """A page that the tooling of the Mac keeps for a bot, without a turn.

        No turn asked for the page, so there is no chat card: the person finds it in the Library. A save that
        changes nothing writes nothing, because the tooling runs on a timer and the page should keep its version
        until its content really changes.
        """
        from .service_base import ServiceError
        key, title, document_format, content, summary = page_to_save(arguments)
        ends = page_end(arguments, datetime.now(timezone.utc))
        notes = page_notes(document_format, content)
        with self.transaction():
            bot = self._get('bots', bot_id)
            if bot['archived'] or bot.get('_deleted'):
                raise ServiceError(409, 'The bot is archived or deleted.')
            document = self._document_of_key(bot_id, key)
            audience = self._page_audience(arguments, document, (key, title, document_format, content, summary))
            if (document is not None and not expired(document, self._now())
                    and self._page_is_like(document, title, document_format, content, summary, ends, audience)):
                return _with_notes({'status': 'unchanged', 'key': key, 'version': document['version'],
                                    'expires_at': document.get('expires_at')}, notes)
            document, _ = self._store_page(bot, key, title, document_format, content, summary, ends, audience)
        return _with_notes({'status': 'saved', 'key': key, 'version': document['version'],
                            'expires_at': document.get('expires_at')}, notes)

    def _page_audience(self, arguments: dict, document: dict | None, page: tuple) -> str | None:
        """The audience of a save on a Mac that runs Coconut for a team only: the one of the arguments, else the one
        of the existing document, else private. On a person's Mac a save names no audience and keeps the shares of the
        page, so the answer is None.

        A team page must fit in one entry of the log, so the save refuses a page that does not fit, and the bot can
        make it smaller.
        """
        from .service_base import ServiceError
        from .team_pages import WIDEST, WIDEST_BOT, page_entry, payload_problem
        audience = arguments.get('audience')
        if self.library_reader:
            # Only the person shares a page (team_pages.py). The bot learns that in the error, and saves again.
            if audience is not None:
                raise ServiceError(400, BOT_SHARE)
            # The limit holds while an active room has the page (TeamStore.shared_pages).
            team = document is not None and bool(self.team_store.page_teams(document['id']))
        else:
            if audience is None:
                audience = (document or {}).get('audience', 'private')
            elif audience not in AUDIENCES:
                raise ServiceError(400, 'audience must be private or team.')
            team = audience == 'team'
        if team:
            key, title, document_format, content, summary = page
            # The bot, the version and the end date of the entry are not known yet, so the check takes the longest.
            problem = payload_problem(page_entry(
                dict(WIDEST, key=key, title=title, format=document_format, summary=summary), content, WIDEST_BOT))
            if problem:
                raise ServiceError(413, f'Coconut did not save the page for the team. {problem}')
        return audience

    def _page_is_like(self, document: dict, title: str, document_format: str, content: str, summary: str | None,
                      ends, audience: str) -> bool:
        """Whether the page of a key is already what the tooling would write. An omitted end date keeps the end date
        of the document, so only an end date that differs from the stored one is a change."""
        return ((ends is ... or ends == document.get('expires_at'))
                and audience in (None, document.get('audience', 'private'))
                and document['title'] == title and document['format'] == document_format
                and (document.get('summary') or '') == (summary or '').strip()
                and self._document_content(document) == content)

    def read_document(self, task_id: str, arguments: dict) -> dict:
        """coconut_read_document of a bot turn: one document of the bot with its content, or the list of them."""
        with self._read_view() as view, view._read_transaction():
            return view._read_pages(view._get('tasks', task_id)['bot_id'], arguments)

    def read_page(self, bot_id: str, arguments: dict) -> dict:
        """coconut_read_document outside a Coconut chat turn, for the bot of the profile that runs the conversation."""
        with self._read_view() as view, view._read_transaction():
            view._get('bots', bot_id)
            return view._read_pages(bot_id, arguments)

    def _read_pages(self, bot_id: str, arguments: dict) -> dict:
        """One document of the bot with its content, or the list of them. The caller holds the read transaction."""
        from .service_base import ServiceError
        key = arguments.get('key')
        if not key:
            return {'documents': [self._page_summary(document) for document in self._bot_documents(bot_id)]}
        document = self._document_of_key(bot_id, key) if isinstance(key, str) else None
        if document is None or expired(document, self._now()):
            raise ServiceError(404, 'This bot has no document with this key.')
        return dict(self._page_summary(document), content=self._document_content(document))

    def _page_summary(self, document: dict) -> dict:
        """What a bot reads about one of its documents. On a person's Mac the audience is "team" while the person
        shares the page with a team."""
        from .team_store import TeamStore
        summary = _summary(document)
        if self.library_reader:
            # A read view has its own connection, so the store of that connection reads the shares.
            summary['audience'] = 'team' if TeamStore(self.db).page_teams(document['id']) else 'private'
        return summary

    def delete_bot_document(self, task_id: str, arguments: dict) -> dict:
        """coconut_delete_document of a bot turn."""
        with self.transaction():
            return self._delete_key(self._running_task(task_id)['bot_id'], arguments.get('key'), task_id)

    def delete_page(self, bot_id: str, arguments: dict) -> dict:
        """coconut_delete_document outside a Coconut chat turn. No turn asked for the delete, so the event names no
        task, as a delete from the phone."""
        from .service_base import ServiceError
        with self.transaction():
            bot = self._get('bots', bot_id)
            if bot['archived'] or bot.get('_deleted'):
                raise ServiceError(409, 'The bot is archived or deleted.')
            return self._delete_key(bot_id, arguments.get('key'), None)

    def unpublish_page(self, bot_id: str, arguments: dict) -> dict:
        """A page that the tooling of the Mac deletes for a bot, without a turn (DELETE /v1/gateway/document).

        The delete removes the document with all its versions, as a delete from the phone does, also when the page
        has ended. A key without a document answers "absent" and writes nothing, so the tooling can repeat a delete.
        """
        from .service_base import ServiceError
        key = page_key(arguments)
        with self.transaction():
            bot = self._get('bots', bot_id)
            if bot['archived'] or bot.get('_deleted'):
                raise ServiceError(409, 'The bot is archived or deleted.')
            document = self._document_of_key(bot_id, key)
            if document is None:
                return {'status': 'absent', 'key': key}
            self._remove_document(document, None)
        return {'status': 'deleted', 'key': key}

    def _delete_key(self, bot_id: str, key, task_id: str | None) -> dict:
        """Delete the document of a key of the bot. The caller holds the transaction."""
        from .service_base import ServiceError
        document = self._document_of_key(bot_id, key) if isinstance(key, str) else None
        if document is None:
            raise ServiceError(404, 'This bot has no document with this key.')
        self._remove_document(document, task_id)
        return {'status': 'deleted', 'key': key}

    def documents(self) -> dict:
        """GET /v1/documents: the documents of the bots that the phone shows, without content. The pinned documents
        come first. Each part has its newest document first.

        The list also has the team pages of the other Macs of each team (team_pages.py). `team_bots` names their
        bots. A phone that does not know these bots hides their pages.
        """
        from .team_pages import page_audience, team_library
        from .team_store import TeamStore
        current = self._now()
        with self._read_view() as view, view._read_transaction():
            bots = {bot['id'] for bot in view._all('bots') if not bot.get('_deleted')}
            rows = view._all('documents')
            team_pages, team_bots = team_library(view.db, current)
            teams = TeamStore(view.db).shared_pages()
        # A local document has its teams, and `audience` for an app of today.
        shown = [dict(document, **page_audience(teams.get(document['id'], []))) for document in rows
                 if document['bot_id'] in bots and not expired(document, current)]
        shown += team_pages
        shown.sort(key=lambda document: (document.get('pinned') is True, document['updated_at'], document['id']),
                   reverse=True)
        return {'documents': shown, 'team_bots': team_bots}

    def document(self, document_id: str) -> dict:
        """GET /v1/documents/<id>: one document with the content of its current version."""
        from .team_pages import team_ids, team_page
        with self._read_view() as view, view._read_transaction():
            if team_ids(document_id):
                return team_page(view.db, document_id, view._now(), content=True)
            document = view._shown_document(document_id)
            return dict(view._team_shared_document(document), content=view._document_content(document))

    def delete_document(self, document_id: str) -> dict:
        """DELETE /v1/documents/<id> from the phone. A document that is already gone counts as deleted. Only the Mac
        that shares a team page can delete it."""
        from .service_base import ServiceError
        from .team_pages import team_ids
        with self.transaction():
            ids = team_ids(document_id)
            if ids:
                if self.team_store.page(*ids):
                    raise ServiceError(403, 'This page belongs to another Mac of the team. Only that Mac can delete it.')
                return {'ok': True}
            row = self.db.execute('SELECT data FROM documents WHERE id=?', (document_id,)).fetchone()
            if row:
                self._remove_document(json.loads(row[0]), None)
        return {'ok': True}

    def pin_document(self, document_id: str, data: dict) -> dict:
        """PATCH /v1/documents/<id> from the phone: pin the document in the Library, or remove its pin.

        A pin is how this Mac shows the Library to its person. It is not content of the page: it changes no version
        and no update time, a bot does not read it, and a save of the page keeps it. A pin adds no event: the phone
        reads the pin with the Library list.
        """
        from .service_base import ServiceError
        from .team_pages import team_ids, team_page
        pinned = data.get('pinned')
        if not isinstance(pinned, bool):
            raise ServiceError(400, 'pinned must be true or false.')
        with self.transaction():
            ids = team_ids(document_id)
            if ids:
                # The pin of a team page is of this Mac only. The other Macs of the team do not see it. A page that
                # the Library does not show raises here, and the transaction takes the pin back.
                self.team_store.pin(*ids, pinned)
                return team_page(self.db, document_id, self._now())
            document = self._shown_document(document_id)
            if (document.get('pinned') is True) != pinned:
                document['pinned'] = pinned
                self._put('documents', document)
            return self._team_shared_document(document)

    def _write_document(self, task: dict, bot: dict, key: str, title: str, document_format: str, content: str,
                        summary: str | None, *, expires, audience: str | None = None) -> dict:
        """Create the document of the key, or add a version to it, and show it in the chat. `expires` is the new end
        date, None to remove it, or ... to keep it. `audience` None keeps the audience of the document. The caller
        holds the transaction."""
        from .library_replies import show_card
        document, created = self._store_page(bot, key, title, document_format, content, summary, expires, audience,
                                             task_id=task['id'])
        show_card(self, task, document, created)
        return document

    def _store_page(self, bot: dict, key: str, title: str, document_format: str, content: str, summary: str | None,
                    expires, audience: str | None = None, task_id: str | None = None) -> tuple[dict, bool]:
        """Create the page of the key, or add a version to it, with its version record and its event. task_id is the
        turn that asked for the page, or None for a page that the tooling of the Mac keeps. The caller holds the
        transaction and returns the document and whether the save created it."""
        from .service_base import uid
        current = self._now()
        document = self._document_of_key(bot['id'], key)
        created = document is None
        if created:
            document = dict(id=uid(), bot_id=bot['id'], key=key, version=0, created_at=current)
        elif expired(document, current):
            # A save brings back an ended document. Its old end date would hide it again at once.
            document['expires_at'] = None
        # The sync of the team log writes the page entries. A save does not call the relay, because it holds the
        # transaction (team_pages.py).
        if 'team' in (audience, document.get('audience')) or (not created and self.team_store.rooms_of_page(
                document['id'])):
            self.team_wake()
        # Only a team document has the field, so the rows of a private document stay as they were.
        if audience == 'private':
            document.pop('audience', None)
        elif audience == 'team':
            document['audience'] = audience
        document.update(title=title, format=document_format, summary=(summary or '').strip() or None,
                        version=document['version'] + 1, updated_at=current, task_id=task_id,
                        size=len(content.encode()))
        if expires is not ...:
            document['expires_at'] = expires
        self._put('documents', document)
        self.db.execute('INSERT INTO document_versions(document_id, version, content, created_at) '
                        'VALUES (?, ?, ?, ?)', (document['id'], document['version'], content, current))
        self.db.execute('DELETE FROM document_versions WHERE document_id=? AND version<=?',
                        (document['id'], document['version'] - KEPT_VERSIONS))
        self._event('document.created' if created else 'document.updated', document, bot['id'], task_id)
        return document, created

    def _running_task(self, task_id: str) -> dict:
        """The task of a write. The person can cancel the turn while the tool call waits for the write lock, so the
        write checks the task again inside its transaction, as a provider event does."""
        from .service import YieldForDecision
        task = self._get('tasks', task_id)
        if task['status'] != 'working' or self.stopping.is_set():
            raise YieldForDecision('This task no longer accepts provider output.')
        return task

    def _shown_document(self, document_id: str) -> dict:
        """A document that the Library shows, or the reason for the phone. The caller holds the transaction."""
        from .service_base import ServiceError
        document = self._get('documents', document_id)
        if self._get('bots', document['bot_id']).get('_deleted'):
            raise ServiceError(404, 'Document was not found.')
        if expired(document, self._now()):
            raise ServiceError(404, 'This document ended.')
        return document

    def _document_of_key(self, bot_id: str, key: str) -> dict | None:
        row = self.db.execute("SELECT data FROM documents WHERE json_extract(data,'$.bot_id')=? "
                              "AND json_extract(data,'$.key')=?", (bot_id, key)).fetchone()
        return json.loads(row[0]) if row else None

    def _bot_documents(self, bot_id: str) -> list[dict]:
        current = self._now()
        rows = self.db.execute("SELECT data FROM documents WHERE json_extract(data,'$.bot_id')=?", (bot_id,))
        documents = [json.loads(row[0]) for row in rows]
        return sorted((document for document in documents if not expired(document, current)),
                      key=lambda document: document['updated_at'], reverse=True)

    def _document_content(self, document: dict) -> str:
        row = self.db.execute('SELECT content FROM document_versions WHERE document_id=? AND version=?',
                              (document['id'], document['version'])).fetchone()
        return row[0] if row else ''

    def _remove_document(self, document: dict, task_id: str | None):
        if document.get('audience') == 'team':
            # The next pass of the team sync deletes the page entry.
            self.team_wake()
        # The shares of the page go with it, and so does the author entry of a bot without another shared page.
        self._team_forget_page(document)
        self.db.execute('DELETE FROM document_versions WHERE document_id=?', (document['id'],))
        self.db.execute('DELETE FROM documents WHERE id=?', (document['id'],))
        self._event('document.deleted', {'id': document['id']}, document['bot_id'], task_id)

    def _remove_bot_documents(self, bot_id: str):
        """The documents of a deleted bot go with it. The caller holds the transaction."""
        rows = self.db.execute("SELECT data FROM documents WHERE json_extract(data,'$.bot_id')=?", (bot_id,))
        for row in rows.fetchall():
            self._remove_document(json.loads(row[0]), None)


def _summary(document: dict) -> dict:
    """What a bot reads about one of its documents."""
    return dict({name: document.get(name) for name in
                 ('key', 'title', 'format', 'summary', 'version', 'updated_at', 'expires_at')},
                audience=document.get('audience', 'private'))

