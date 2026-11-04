"""Library replies: a report in a chat reply goes to the Library, and each new document shows as a card in the chat.

The model rule (card_tools.LIBRARY_INSTRUCTIONS) asks each bot to save a report as a document. A bot can forget the
rule, and a gateway that started before an update does not have it. So Coconut itself keeps a report: when a turn
ends with a long, structured reply and the bot saved no document in that turn, Coconut saves the reply as a Markdown
document of the bot. The reply stays in the chat. Its photos stay in the document as `coconut-file://` lines, which the
phone loads through the Mac.

A report keeps one document for each title. The same report on another day, also from a routine, adds a version to
that document.
"""
from __future__ import annotations

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


def is_report(text: str) -> bool:
    """A reply with the shape of a report: headings, a table or a long list, with enough text, or with photos."""
    lines = text.splitlines()
    photos = sum(1 for line in lines if _PHOTO.match(line))
    prose = sum(len(line.strip()) for line in lines if not _PHOTO.match(line))
    headings = sum(1 for line in lines if _heading(line))
    items = sum(1 for line in lines if _LIST_ITEM.match(line))
    table = any(_TABLE_RULE.match(line) for line in lines)
    if photos:
        return prose >= MIN_PHOTO_REPORT and (headings >= 1 or table or items >= 3)
    return prose >= MIN_REPORT and (headings >= 2 or table or (headings >= 1 and items >= 4) or items >= 8)


def document_of(text: str, fallback_title: str) -> dict:
    """The title, the key, the summary and the content of the document of a report."""
    lines = text.strip().splitlines()
    first = _heading(lines[0]) if lines else None
    title = first or next((_plain(line) for line in lines if _plain(line) and not _PHOTO.match(line)), '')
    title = _shorten(title or fallback_title, MAX_TITLE)
    # The Library shows the title over the page, so the heading that gave it goes.
    content = '\n'.join(lines[1:]).strip() if first else text.strip()
    summary = next((_plain(line) for line in lines[1 if first else 0:] if _plain(line) and not _PHOTO.match(line)
                    and not _heading(line) and not _LIST_ITEM.match(line) and '|' not in line), None)
    slug = re.sub(r'[^a-z0-9]+', '-', title.casefold()).strip('-')[:57].strip('-') or 'report'
    return dict(key='reply-' + slug, title=title, content=content or text.strip(),
                summary=_shorten(summary, 160) if summary else None)


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


def keep_report(service, task: dict):
    """Save the report of a finished turn as a document. The caller holds the transaction."""
    if saved_in_turn(service, task):
        return
    text = turn_reply(service, task)
    if not text or not is_report(text):
        return
    bot = service._get('bots', task['bot_id'])
    if bot['archived'] or bot.get('_deleted'):
        return
    document = document_of(text, task.get('title') or 'Report')
    service._write_document(task, bot, document['key'], document['title'], 'markdown', document['content'],
                            document['summary'], expires=None)


def show_card(service, task: dict, document: dict, created: bool):
    """A card in the chat for a new document, and for an update that a person's message asked for. A routine
    that keeps a document current updates it quietly. One turn shows one card for each document. The caller holds
    the transaction."""
    if not created and task.get('_native_job'):
        return
    for message in service._task_messages(task['id']):
        if message['kind'] == 'document' and message.get('document_id') == document['id']:
            if message['text'] != document['title']:
                message['text'] = document['title']
                service._put('messages', message)
                service._event('message.updated', service._public(message), message['bot_id'], task['id'])
            return
    service._message(task['bot_id'], document['title'], task_id=task['id'], kind='document',
                     document_id=document['id'])
