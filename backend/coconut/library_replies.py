"""Library replies: each new document shows as a card in the chat.

Coconut does not save a chat reply as a page by itself: a page exists only when an agent saves one because the person
asked. remove_unasked_reports removes the pages that an earlier build kept from replies that nobody asked for.
"""
from __future__ import annotations

import hashlib
import json
import re


def report_key(title: str) -> str:
    """The key of a kept report. Only the same title shares a document. The hash keeps apart titles with the same
    start, and titles in letters that the slug leaves out. A bot never makes this key, so it also marks a page that
    Coconut kept."""
    slug = re.sub(r'[^a-z0-9]+', '-', title.casefold()).strip('-')[:48].strip('-') or 'report'
    digest = hashlib.sha256(title.casefold().encode()).hexdigest()[:8]
    return f'reply-{slug}-{digest}'


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
