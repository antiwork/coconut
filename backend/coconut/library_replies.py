"""Library replies: each new document shows as a card in the chat.

Coconut does not save a chat reply as a page by itself. Those pages cost a model turn each and were rarely wanted: a
page exists only when a bot saves one because the person asked. The helpers that read the shape of a report stay for
the removal of pages that earlier builds kept (remove_unasked_reports).
"""
from __future__ import annotations

import hashlib
import json
import re

from .documents import MAX_TITLE

# A report has structure and some length. A short answer, or a long answer without structure, stays in the chat only.
MIN_REPORT = 1200
MIN_PHOTO_REPORT = 300
_PHOTO = re.compile(r'^\s*!\[[^\]]*\]\((?:<[^>]*>|[^)\s]*)\)\s*$')
_HEADING = re.compile(r'^\s{0,3}#{1,6}\s+(\S.*?)\s*#*\s*$')
_BOLD_LINE = re.compile(r'^\s*(?:\*\*|__)([^*_].*?)(?:\*\*|__):?\s*$')
_TABLE_RULE = re.compile(r'^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$')
_LIST_ITEM = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+\S')
_MARKUP = re.compile(r'[*_`~]+|\[([^\]]*)\]\([^)]*\)')


def _plain(text: str) -> str:
    return ' '.join(_MARKUP.sub(lambda match: match.group(1) or '', text).split()).strip(' :')


def _heading(line: str) -> str | None:
    match = _HEADING.match(line) or _BOLD_LINE.match(line)
    return _plain(match.group(1)) if match else None


def _in_code(lines: list[str]) -> list[bool]:
    """For each line, whether it is in fenced code, where `# Setup` is a comment and not a heading."""
    inside, fence, marks = False, '', []
    for line in lines:
        stripped = line.lstrip()
        opener = stripped[:3] if stripped.startswith(('```', '~~~')) else ''
        if opener and (not inside or opener == fence):
            marks.append(True)
            inside, fence = not inside, opener
        else:
            marks.append(inside)
    return marks


def is_report(text: str) -> bool:
    """A reply with the shape of a report: headings, a table or a long list, with enough text, or with photos."""
    lines = text.splitlines()
    photos = sum(1 for line in lines if _PHOTO.match(line))
    prose = sum(len(line.strip()) for line in lines if not _PHOTO.match(line))
    headings = sum(1 for line, code in zip(lines, _in_code(lines)) if not code and _heading(line))
    items = sum(1 for line in lines if _LIST_ITEM.match(line))
    table = any(_TABLE_RULE.match(line) for line in lines)
    if photos:
        return prose >= MIN_PHOTO_REPORT and (headings >= 1 or table or items >= 3)
    return prose >= MIN_REPORT and (headings >= 2 or table or (headings >= 1 and items >= 4) or items >= 8)


def document_of(text: str, fallback_title: str) -> dict:
    """The title, the key, the summary and the content of the document of a report."""
    lines = text.strip().splitlines()
    # The first heading names the report, also after a sentence such as "Here is today's report".
    code = _in_code(lines)
    heading = next(((index, _heading(line)) for index, line in enumerate(lines)
                    if not code[index] and _heading(line)), None)
    title = heading[1] if heading else next((_plain(line) for line in lines if _plain(line) and not _PHOTO.match(line)), '')
    title = _shorten(title or fallback_title, MAX_TITLE)
    # The Library shows the title over the page, so a heading that gave it at the top goes.
    first = heading is not None and heading[0] == 0
    content = '\n'.join(lines[1:]).strip() if first else text.strip()
    summary = next((_plain(line) for line in lines[1 if first else 0:] if _plain(line) and not _PHOTO.match(line)
                    and not _heading(line) and not _LIST_ITEM.match(line) and '|' not in line), None)
    return dict(key=report_key(title), title=title, content=content or text.strip(),
                summary=_shorten(summary, 160) if summary else None)


def report_key(title: str) -> str:
    """The key of a kept report. Only the same title shares a document. The hash keeps apart titles with the same
    start, and titles in letters that the slug leaves out. A bot never makes this key, so it also marks a page that
    Coconut kept."""
    slug = re.sub(r'[^a-z0-9]+', '-', title.casefold()).strip('-')[:48].strip('-') or 'report'
    digest = hashlib.sha256(title.casefold().encode()).hexdigest()[:8]
    return f'reply-{slug}-{digest}'


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(' ', 1)[0]
    return cut.rstrip(' ,;:.') + '…'


def turn_reply(service, task: dict) -> str:
    """The visible text replies of the bot in the current turn of the task, in order, as one text."""
    started = task.get('_turn_started_at', task['created_at'])
    replies = [message['text'] for message in service._task_messages(task['id'])
               if message['role'] == 'assistant' and message['kind'] == 'text'
               and message.get('sender_bot_id') in (None, task['bot_id']) and message['created_at'] >= started
               and not message.get('recipient_bot_ids') and not message.get('_hidden')
               and not message.get('_interim') and not message.get('_deleted') and message['text'].strip()]
    return '\n\n'.join(text.strip() for text in replies)


def saved_in_turn(service, task: dict) -> bool:
    started = task.get('_turn_started_at', task['created_at'])
    row = service.db.execute("SELECT 1 FROM documents WHERE json_extract(data,'$.task_id')=? "
                             "AND json_extract(data,'$.updated_at')>=? LIMIT 1", (task['id'], started)).fetchone()
    return row is not None


def asked_by_person(service, task: dict) -> bool:
    """Whether a message that a person typed in a chat started or joined this turn. Bots write structured status
    replies all day: to each other, for work intake and for pull request gates. Only a person's request makes a
    reply a report to keep."""
    # A person can also join a turn that another bot or a routine started.
    for identity in task.get('_turn_message_ids', []):
        message = service.db.execute('SELECT data FROM messages WHERE id=?', (identity,)).fetchone()
        if message and json.loads(message[0]).get('_human_source_kind') == 'chat':
            return True
    return False


def show_card(service, task: dict, document: dict, created: bool):
    """A card in the chat for a new document, and for an update that a person's message asked for. A routine
    that keeps a document current updates it quietly. One turn shows one card for each document. The caller holds
    the transaction."""
    if not created and task.get('_native_job'):
        return
    started = task.get('_turn_started_at', task['created_at'])
    for message in service._task_messages(task['id']):
        if (message['kind'] == 'document' and message.get('document_id') == document['id']
                and message['created_at'] >= started):
            if message['text'] != document['title']:
                message['text'] = document['title']
                service._put('messages', message)
                service._event('message.updated', service._public(message), message['bot_id'], task['id'])
            return
    service._message(task['bot_id'], document['title'], task_id=task['id'], kind='document',
                     document_id=document['id'])


def remove_unasked_reports(service):
    """Remove once the pages that Coconut kept from turns that no person asked for, with their chat cards.

    Coconut 0.1.0 (642) kept a page for every structured reply: bot exchanges, work intake and pull request gates
    each made one. A page that the bot saved itself stays, and so does a kept report that a person asked for. The
    caller holds the transaction."""
    if service._setting('unasked_reports_removed'):
        return
    removed = set()
    for (data,) in service.db.execute("SELECT data FROM documents WHERE json_extract(data,'$.key') LIKE 'reply-%'").fetchall():
        document = json.loads(data)
        # Only a page that Coconut kept. A bot can choose a key that starts with reply-, but not the hash of its title.
        if document['key'] != report_key(document['title']):
            continue
        # The task of the latest save can be a later routine. The first card of the page names the task that made
        # it, because each new page showed a card. A page without a card stays.
        card = service.db.execute("SELECT data FROM messages WHERE json_extract(data,'$.kind')='document' AND "
                                  "json_extract(data,'$.document_id')=? ORDER BY rowid LIMIT 1",
                                  (document['id'],)).fetchone()
        creator = json.loads(card[0]).get('task_id') if card else None
        if creator is None:
            continue
        asked = any(message['role'] == 'user' and message.get('_human_source_kind') == 'chat'
                    for message in service._task_messages(creator))
        if not asked:
            service._remove_document(document, None)
            removed.add(document['id'])
    hidden = False
    for (data,) in service.db.execute("SELECT data FROM messages WHERE json_extract(data,'$.kind')='document'").fetchall():
        message = json.loads(data)
        if message.get('document_id') in removed and not message.get('_hidden'):
            message['_hidden'] = True
            service._put('messages', message)
            hidden = True
    if hidden:
        service._event('transcript.updated', {})
    service.db.execute("INSERT INTO settings VALUES ('unasked_reports_removed','true')")
