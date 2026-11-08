"""Documents: pages that a bot keeps in the Library of the phone.

A document belongs to the bot that saved it, under a key that the bot chooses. A save with the same key replaces the
content and adds a version, so a bot updates one document and never makes a copy of it. A document is Markdown, which
the phone draws natively, or HTML, which the phone shows in a closed web view with the design tokens of Coconut.

Coconut owns the style of a page and the bot owns its structure. A save of HTML fails when the code of the page sets
its own font, writes a color as a value and not as a token, or loads something from the network. The reason goes back
to the bot, which then fixes the page. A document can end on a date. After that date the Library no longer shows it.

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
MAX_CONTENT = 512 * 1024
MAX_TITLE = 120
MAX_SUMMARY = 200
KEPT_VERSIONS = 10
LONGEST_LIFE = timedelta(days=366)

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
_FILE = 'The page loads another file. Coconut gives d3 and Plot to each page. Put everything else in the page.'
_NETWORK = 'The page loads something from the network. Pages have no network access. Put the data in the page.'
_COLOR_PROPERTY = r'(?:color|background(?:-color)?|fill|stroke|border(?:-[a-z]+)?|outline|box-shadow|text-shadow)'
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
    ('code', re.compile(_COLOR_PROPERTY + r'\s*:[^;{}]*?\b(?:' + _COLOR_NAMES + r')\b|[\'"`]\s*(?:' + _COLOR_NAMES
                        + r')\s*[\'"`]', re.I), _NAME_REASON),
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


_TEMPLATE_EXPRESSION = re.compile(r'\$\{[^}]*\}')
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

# The blocks of a page (page.js in the app). The save tool and the Library rule name them, so each bot knows them.
BLOCKS = ('Write a page like a short article: its first paragraph is the lead, one or two sentences that state the '
          'point. Markdown pages have blocks: a callout is a quote that starts with [!NOTE], [!TIP], [!IMPORTANT], '
          '[!WARNING] or [!CAUTION], with an optional title on the same line; a ```mermaid block draws a diagram '
          '(flowchart, sequence, timeline, gantt, pie); a ```chart block draws a graph from JSON {"type": "bar", '
          '"line", "area" or "dot", "data": [...], "x": "field", "y": "field", "series": "field", "title": "..."}; '
          'tables, task lists and photos work too. Show a process as a diagram, numbers as a chart, a decision as a '
          'callout and a comparison as a table. HTML pages have the same blocks: <pre class="mermaid">, '
          '<aside class="callout tip"> and <p class="lead">.')
SAVE_SCHEMA = {'name': 'coconut_save_document', 'description': (
    'Save a document in the Library of Coconut, where the person reads it on the phone. A save with an existing key '
    'replaces that document and adds a version. Use a document for content that outlives the chat: something to '
    'track, to reuse or to check later. A short answer stays in the chat. ' + BLOCKS + ' '
    'HTML suits an interactive page. Coconut sets the font, the colors and the spacing of '
    'each page: in HTML use only these tokens for color: var(--c1) to var(--c6), var(--text), var(--text-2), '
    'var(--bg), var(--surface), var(--line), or coconut.palette (an array of the six colors) in a script. A page has '
    'd3 and Plot (Observable Plot) and has no network access. Coconut refuses a page that sets a font, writes a '
    'color as a value or loads something, and names the reason.'),
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
    """The page rules that an HTML document breaks, each reason once."""
    page = _PageCode()
    page.feed(content)
    page.close()
    parts = dict(page.parts, code=page.parts['css'] + page.parts['scripts'])
    problems = list(dict.fromkeys(page.reasons))
    for part, pattern, reason in HTML_RULES:
        if reason not in problems and any(pattern.search(text) for text in parts[part]):
            problems.append(reason)
    return problems


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


def page_to_save(arguments: dict) -> tuple[str, str, str, str, str | None]:
    """The key, title, format, content and summary of a page, or the reason to refuse it. The tools of a bot turn
    and the tooling of the Mac save a page through this, so both follow the page rules."""
    from .service import ServiceError, require_text
    key, document_format, content = (arguments.get(name) for name in ('key', 'format', 'content'))
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ServiceError(400, 'key must be 1 to 64 lowercase letters, digits and hyphens, for example '
                                'signups-daily.')
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
    from .service import ServiceError
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

    def save_document(self, task_id: str, arguments: dict) -> dict:
        """coconut_save_document of a bot turn: create the document of the key, or replace its content."""
        from .service import ServiceError
        key, title, document_format, content, summary = page_to_save(arguments)
        ends = page_end(arguments, datetime.now(timezone.utc))
        with self.transaction():
            task = self._running_task(task_id)
            bot = self._get('bots', task['bot_id'])
            if bot['archived'] or bot.get('_deleted'):
                raise ServiceError(409, 'The bot is archived or deleted.')
            document = self._write_document(task, bot, key, title, document_format, content, summary, expires=ends)
        return {'status': 'saved', 'key': key, 'version': document['version'],
                'expires_at': document.get('expires_at')}

    def publish_page(self, bot_id: str, arguments: dict) -> dict:
        """A page that the tooling of the Mac keeps for a bot, without a turn.

        No turn asked for the page, so there is no chat card: the person finds it in the Library. A save that
        changes nothing writes nothing, because the tooling runs on a timer and the page should keep its version
        until its content really changes.
        """
        from .service import ServiceError
        key, title, document_format, content, summary = page_to_save(arguments)
        ends = page_end(arguments, datetime.now(timezone.utc))
        with self.transaction():
            bot = self._get('bots', bot_id)
            if bot['archived'] or bot.get('_deleted'):
                raise ServiceError(409, 'The bot is archived or deleted.')
            document = self._document_of_key(bot_id, key)
            if document is not None and self._page_is_like(document, title, document_format, content, summary, ends):
                return {'status': 'unchanged', 'key': key, 'version': document['version'],
                        'expires_at': document.get('expires_at')}
            document, _ = self._store_page(bot, key, title, document_format, content, summary, ends)
        return {'status': 'saved', 'key': key, 'version': document['version'],
                'expires_at': document.get('expires_at')}

    def _page_is_like(self, document: dict, title: str, document_format: str, content: str, summary: str | None,
                      ends) -> bool:
        """Whether the page of a key is already what the tooling would write. An end date that comes with the save
        counts as a change, and an omitted one keeps the end date of the document."""
        return (ends is ... and document['title'] == title and document['format'] == document_format
                and (document.get('summary') or '') == (summary or '').strip()
                and self._document_content(document) == content)

    def read_document(self, task_id: str, arguments: dict) -> dict:
        """coconut_read_document of a bot turn: one document of the bot with its content, or the list of them."""
        from .service import ServiceError, now
        key = arguments.get('key')
        with self._read_view() as view, view._read_transaction():
            bot_id = view._get('tasks', task_id)['bot_id']
            if not key:
                return {'documents': [_summary(document) for document in view._bot_documents(bot_id)]}
            document = view._document_of_key(bot_id, key) if isinstance(key, str) else None
            if document is None or expired(document, now()):
                raise ServiceError(404, 'This bot has no document with this key.')
            return dict(_summary(document), content=view._document_content(document))

    def delete_bot_document(self, task_id: str, arguments: dict) -> dict:
        """coconut_delete_document of a bot turn."""
        from .service import ServiceError
        key = arguments.get('key')
        with self.transaction():
            bot_id = self._running_task(task_id)['bot_id']
            document = self._document_of_key(bot_id, key) if isinstance(key, str) else None
            if document is None:
                raise ServiceError(404, 'This bot has no document with this key.')
            self._remove_document(document, task_id)
        return {'status': 'deleted', 'key': key}

    def documents(self) -> dict:
        """GET /v1/documents: the documents of the bots that the phone shows, newest first, without content."""
        from .service import now
        current = now()
        with self._read_view() as view, view._read_transaction():
            bots = {bot['id'] for bot in view._all('bots') if not bot.get('_deleted')}
            rows = view._all('documents')
        shown = [document for document in rows if document['bot_id'] in bots and not expired(document, current)]
        shown.sort(key=lambda document: (document['updated_at'], document['id']), reverse=True)
        return {'documents': shown}

    def document(self, document_id: str) -> dict:
        """GET /v1/documents/<id>: one document with the content of its current version."""
        from .service import ServiceError, now
        with self._read_view() as view, view._read_transaction():
            document = view._get('documents', document_id)
            if view._get('bots', document['bot_id']).get('_deleted'):
                raise ServiceError(404, 'Document was not found.')
            if expired(document, now()):
                raise ServiceError(404, 'This document ended.')
            return dict(document, content=view._document_content(document))

    def delete_document(self, document_id: str) -> dict:
        """DELETE /v1/documents/<id> from the phone. A document that is already gone counts as deleted."""
        with self.transaction():
            row = self.db.execute('SELECT data FROM documents WHERE id=?', (document_id,)).fetchone()
            if row:
                self._remove_document(json.loads(row[0]), None)
        return {'ok': True}

    def _write_document(self, task: dict, bot: dict, key: str, title: str, document_format: str, content: str,
                        summary: str | None, *, expires) -> dict:
        """Create the document of the key, or add a version to it, and show it in the chat. `expires` is the new end
        date, None to remove it, or ... to keep it. The caller holds the transaction."""
        from .library_replies import show_card
        document, created = self._store_page(bot, key, title, document_format, content, summary, expires,
                                             task_id=task['id'])
        show_card(self, task, document, created)
        return document

    def _store_page(self, bot: dict, key: str, title: str, document_format: str, content: str, summary: str | None,
                    expires, task_id: str | None = None) -> tuple[dict, bool]:
        """Create the page of the key, or add a version to it, with its version record and its event. task_id is the
        turn that asked for the page, or None for a page that the tooling of the Mac keeps. The caller holds the
        transaction and returns the document and whether the save created it."""
        from .service import now, uid
        current = now()
        document = self._document_of_key(bot['id'], key)
        created = document is None
        if created:
            document = dict(id=uid(), bot_id=bot['id'], key=key, version=0, created_at=current)
        elif expired(document, current):
            # A save brings back an ended document. Its old end date would hide it again at once.
            document['expires_at'] = None
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

    def _document_of_key(self, bot_id: str, key: str) -> dict | None:
        row = self.db.execute("SELECT data FROM documents WHERE json_extract(data,'$.bot_id')=? "
                              "AND json_extract(data,'$.key')=?", (bot_id, key)).fetchone()
        return json.loads(row[0]) if row else None

    def _bot_documents(self, bot_id: str) -> list[dict]:
        from .service import now
        current = now()
        rows = self.db.execute("SELECT data FROM documents WHERE json_extract(data,'$.bot_id')=?", (bot_id,))
        documents = [json.loads(row[0]) for row in rows]
        return sorted((document for document in documents if not expired(document, current)),
                      key=lambda document: document['updated_at'], reverse=True)

    def _document_content(self, document: dict) -> str:
        row = self.db.execute('SELECT content FROM document_versions WHERE document_id=? AND version=?',
                              (document['id'], document['version'])).fetchone()
        return row[0] if row else ''

    def _remove_document(self, document: dict, task_id: str | None):
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
    return {name: document.get(name) for name in
            ('key', 'title', 'format', 'summary', 'version', 'updated_at', 'expires_at')}

