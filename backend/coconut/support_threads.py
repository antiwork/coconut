"""A conversation of the Support report, read and answered in Coconut through Gmail.

The support report (the dashboards repository, support-dashboard.sh) gives each conversation of the customer queue the
ID of its Gmail thread as `slug`. Coconut reads the same thread with the Gmail API, so the apps show the conversation
in Coconut, and the person answers and closes it there instead of in Gmail. Nothing is kept: each read goes to Gmail.

    GET   /v1/support/conversations/<thread>[?all=1]
    GET   /v1/support/conversations/<thread>/messages/<message>
    POST  /v1/support/conversations/<thread>/reply   {text, message?}
    PATCH /v1/support/conversations/<thread>         {state: open|closed}

Two Google connections of this Mac can read a thread (google_connection.py). The support mailbox is Coconut's own: it
may send and label mail, and no agent shell gets its token. Without it the read-only Google connection of the agents
reads the thread, and the page offers to connect the support mailbox to answer. A conversation is open or closed by
the labels of Gumclaw's support pipeline (gumclaw/open, gumclaw/done), which the support report reads too.

The answer of a read has the subject, the connected account, the state, whether this Mac can answer, and the messages
oldest first: the sender, the address that a reply goes to, the recipients, the time, the text without the quoted
history of a reply, the names of the attachments, and `outgoing` for a message that the support mailbox sent.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import re
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .service_base import ServiceError, encoded

THREAD = re.compile(r'[0-9a-fA-F]{8,32}')
MESSAGE = re.compile(r'[0-9a-fA-F]{8,32}')
GMAIL = 'https://gmail.googleapis.com/gmail/v1/users/me/'
API = GMAIL + 'threads/'
# A thread larger than this is not read.
MAX_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 100
# The earlier messages of a long thread, when the person asks for them.
MAX_ALL = 500
MAX_TEXT = 20000
# One message that the person opened whole.
MAX_FULL = 400_000
MAX_REPLY = 50_000
NOT_CONNECTED = 'Connect Google in You, Connections, to read support conversations here.'
NO_MAILBOX = 'Connect the support mailbox to answer and close conversations here.'
OPEN_LABEL, DONE_LABEL = 'gumclaw/open', 'gumclaw/done'
# The labels that Gumclaw's pipeline clears when it closes a conversation (gmail_direct.mark_thread_done).
CLEARED = (OPEN_LABEL, 'gumclaw/claimed-ingest', 'gumclaw/awaiting-deploy')
REQUEST_HEADER = 'X-Coconut-Request'
write_lock = threading.Lock()
_labels_cache: dict = {}


def gmail_url(thread_id: str) -> str:
    return 'https://mail.google.com/mail/u/0/#all/' + thread_id


def conversation(root, thread_id: str, *, everything=False, access=None, fetch=None) -> dict:
    """The thread `thread_id` of the connected Gmail. `everything` reads up to MAX_ALL messages. `access` and `fetch`
    replace the Google connection and the request to Gmail for a check by hand."""
    if not THREAD.fullmatch(thread_id or ''):
        raise ServiceError(400, 'Choose a conversation of the support queue.')
    token, account, writable = _connected(access) if access is not None else _access(root)
    fetch = fetch or _fetch
    data = fetch(API + thread_id.lower() + '?format=full', token, account)
    items = [item for item in (data.get('messages') or []) if isinstance(item, dict)]
    messages = [message(item, account) for item in items]
    messages.sort(key=lambda item: item['date'] or '')
    subject = next((item['subject'] for item in messages if item['subject']), '')
    for item in messages:
        item.pop('subject', None)
    names = _label_names(token, account, fetch)
    labels = {names.get(label, label) for item in items for label in item.get('labelIds') or []}
    state = 'closed' if DONE_LABEL in labels else 'open' if OPEN_LABEL in labels else None
    limit = MAX_ALL if everything else MAX_MESSAGES
    return dict(id=thread_id.lower(), subject=subject, account=account, url=gmail_url(thread_id.lower()),
                messages=messages[-limit:], more=max(0, len(messages) - limit), state=state, can_reply=writable)


def full_message(root, thread_id: str, message_id: str, *, access=None, fetch=None) -> dict:
    """One message of a thread with its whole text, for a message that the conversation cut."""
    if not THREAD.fullmatch(thread_id or '') or not MESSAGE.fullmatch(message_id or ''):
        raise ServiceError(400, 'Choose a message of the support queue.')
    token, account, _ = _connected(access) if access is not None else _access(root)
    item = (fetch or _fetch)(GMAIL + 'messages/' + message_id.lower() + '?format=full', token, account)
    if str(item.get('threadId') or '').lower() != thread_id.lower():
        raise ServiceError(404, 'This message is not in that conversation.')
    result = message(item, account, limit=MAX_FULL)
    result.pop('subject', None)
    return result


def _connected(access):
    """A pair of a check by hand reads only. A triple says whether the account may also write."""
    return (*access, False) if len(access) == 2 else tuple(access)


def _access(root, write=False):
    """The token and the account that read a thread, and whether they may answer and label it: the support mailbox
    when it is connected, else the read-only Google connection of the agents, for a read."""
    from .google_connection import GoogleAuthorizationError, GoogleConnections, GoogleError
    support = GoogleConnections(root, mailbox='support').profile()
    if support.status().get('connected'):
        profile, writable = support, True
    elif write:
        raise ServiceError(409, NO_MAILBOX)
    else:
        profile, writable = GoogleConnections(root).profile(), False
        if not profile.status().get('connected'):
            raise ServiceError(409, NOT_CONNECTED)
    try:
        token, account = profile.access()
    except GoogleAuthorizationError:
        raise ServiceError(409, 'Google needs a new sign-in. Reconnect Google in You, Connections.') from None
    except GoogleError as error:
        raise ServiceError(502, str(error)) from None
    return token, account, writable


def _fetch(url: str, token: str, account: str) -> dict:
    return _call('GET', url, token, account)


def _call(method: str, url: str, token: str, account: str, payload=None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, method=method, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
                                                              **({'Content-Type': 'application/json'} if data else {})})
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read(MAX_BYTES + 1)
    except HTTPError as error:
        if error.code == 404:
            raise ServiceError(404, f'This conversation is not in the Gmail of {account}.') from None
        if error.code in (401, 403):
            if method != 'GET':
                raise ServiceError(409, f'Google refused this change. Connect the support mailbox again, and allow Coconut to send and label the mail of {account}.') from None
            raise ServiceError(409, 'Google refused this read. Reconnect Google in You, Connections.') from None
        if method != 'GET':
            raise ServiceError(502, 'Gmail did not confirm the change. Open the conversation again before you retry.') from None
        raise ServiceError(502, 'Gmail is unavailable. Try again shortly.') from None
    except (URLError, TimeoutError, OSError):
        if method != 'GET':
            raise ServiceError(502, 'Gmail did not answer. Open the conversation again before you retry.') from None
        raise ServiceError(502, 'Gmail did not answer. Try again shortly.') from None
    if len(body) > MAX_BYTES:
        raise ServiceError(413, 'This conversation is too large to show here.')
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        raise ServiceError(502, 'Gmail returned an unreadable answer.') from None
    if not isinstance(data, dict):
        raise ServiceError(502, 'Gmail returned an unreadable answer.')
    return data


def _label_names(token, account, fetch) -> dict:
    """The names of the labels of the mailbox by their IDs, kept for ten minutes per account."""
    cached = _labels_cache.get(account)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        data = fetch(GMAIL + 'labels', token, account)
    except ServiceError:
        return {}
    names = {str(label.get('id')): str(label.get('name')) for label in data.get('labels') or []
             if isinstance(label, dict) and label.get('id') and label.get('name')}
    _labels_cache[account] = (time.monotonic() + 600, names)
    return names


def _headers(item: dict) -> dict:
    payload = item.get('payload') if isinstance(item.get('payload'), dict) else {}
    headers = {}
    for header in payload.get('headers') or []:
        if isinstance(header, dict) and isinstance(header.get('name'), str):
            headers.setdefault(header['name'].lower(), str(header.get('value') or ''))
    return headers


def message(item: dict, account: str, limit: int = MAX_TEXT) -> dict:
    payload = item.get('payload')
    if not isinstance(payload, dict):
        payload = {}
    headers = _headers(item)
    name, address = parseaddr(headers.get('from', ''))
    recipients = [mail for _, mail in getaddresses([headers.get('to', ''), headers.get('cc', '')]) if mail]
    plain, rich, files = [], [], []
    _walk(payload, plain, rich, files)
    text = '\n\n'.join(part for part in plain if part.strip()) or html_text('\n'.join(rich))
    text = trim_quote(text.replace('\r\n', '\n')).strip()
    if not text:
        text = html.unescape(str(item.get('snippet') or ''))
    labels = item.get('labelIds') or []
    outgoing = 'SENT' in labels or (bool(address) and address.lower() == str(account).lower())
    return dict(id=str(item.get('id') or ''), subject=headers.get('subject', ''), from_name=name or address,
                from_email=address.lower(), to=recipients, reply_to=reply_address(headers, outgoing),
                date=_date(item.get('internalDate')), text=text[:limit], cut=len(text) > limit, attachments=files,
                outgoing=outgoing)


def reply_address(headers: dict, outgoing: bool) -> str:
    """Where an answer to this message goes: its Reply-To or its sender, or for a message of the support mailbox the
    first person that it went to."""
    if outgoing:
        to = [mail for _, mail in getaddresses([headers.get('to', '')]) if mail]
        return to[0].lower() if to else ''
    reply = [mail for _, mail in getaddresses([headers.get('reply-to', '')]) if mail]
    return (reply[0] if reply else parseaddr(headers.get('from', ''))[1]).lower()


def reply(root, thread_id: str, data: dict, request_key: str, *, access=None, fetch=None, call=None) -> dict:
    """Sends the person's answer in the thread from the support mailbox, as Reply in Gmail does: to the address that
    the answered message names, with its subject, its threading headers and its text quoted under the answer. The
    answered message is `message`, or else the newest one from a customer. A send that Gmail took before its answer was
    lost is found again by its request key, so a second Send sends nothing."""
    if not THREAD.fullmatch(thread_id or ''):
        raise ServiceError(400, 'Choose a conversation of the support queue.')
    text, target = data.get('text'), data.get('message')
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_REPLY:
        raise ServiceError(400, 'Write an answer of up to 50,000 characters.')
    if target is not None and (not isinstance(target, str) or not MESSAGE.fullmatch(target)):
        raise ServiceError(400, 'Choose a message of this conversation to answer.')
    token, account, writable = _connected(access) if access is not None else _access(root, write=True)
    if not writable:
        raise ServiceError(409, NO_MAILBOX)
    fetch, call = fetch or _fetch, call or _call
    thread = fetch(API + thread_id.lower() + '?format=full', token, account)
    items = [item for item in (thread.get('messages') or []) if isinstance(item, dict)]
    if not items:
        raise ServiceError(404, 'This conversation has no messages.')
    for item in items:
        if _headers(item).get(REQUEST_HEADER.lower()) == request_key:
            found = message(item, account)
            found.pop('subject', None)
            return found
    shown = [(item, message(item, account)) for item in items]
    if target:
        anchor = next((pair for pair in shown if pair[1]['id'] == target.lower()), None)
        if anchor is None:
            raise ServiceError(404, 'That message is not in this conversation.')
    else:
        inbound = [pair for pair in shown if not pair[1]['outgoing']]
        anchor = (inbound or shown)[-1]
    item, answered = anchor
    headers = _headers(item)
    to = answered['reply_to']
    if '@' not in to or '\n' in to or '\r' in to:
        raise ServiceError(409, 'This message has no address to answer.')
    mail = EmailMessage()
    mail['From'] = account
    mail['To'] = to
    subject = _safe(headers.get('subject', '')) or 'Gumroad support'
    mail['Subject'] = subject if subject.lower().startswith('re:') else 'Re: ' + subject
    if headers.get('message-id'):
        mail['In-Reply-To'] = _safe(headers['message-id'])
        mail['References'] = (_safe(headers.get('references', '')) + ' ' + _safe(headers['message-id'])).strip()
    mail[REQUEST_HEADER] = request_key
    mail.set_content(text.rstrip() + '\n\n' + quoted(answered))
    sent = call('POST', GMAIL + 'messages/send', token, account,
                dict(threadId=thread_id.lower(), raw=base64.urlsafe_b64encode(mail.as_bytes()).decode()))
    sent_id = str(sent.get('id') or '')
    if not MESSAGE.fullmatch(sent_id):
        raise ServiceError(502, 'Gmail did not confirm the answer. Open the conversation again before you retry.')
    result = message(fetch(GMAIL + 'messages/' + sent_id + '?format=full', token, account), account)
    result.pop('subject', None)
    return result


def _safe(value: str) -> str:
    """A header value on one line."""
    return re.sub(r'[\r\n]+', ' ', value or '').strip()[:900]


def quoted(answered: dict) -> str:
    """The answered message under an answer, as a mail program quotes it."""
    when = answered['date']
    try:
        when = datetime.fromisoformat(when.replace('Z', '+00:00')).strftime('%a, %b %-d, %Y at %-I:%M %p UTC') if when else ''
    except ValueError:
        when = ''
    who = answered['from_name'] if answered['from_name'] == answered['from_email'] else f"{answered['from_name']} <{answered['from_email']}>"
    lines = ['> ' + line if line else '>' for line in answered['text'].splitlines()]
    return (f'On {when}, {who} wrote:' if when else f'{who} wrote:') + '\n' + '\n'.join(lines)


def set_state(root, thread_id: str, data: dict, *, access=None, fetch=None, call=None) -> dict:
    """Closes or reopens a conversation with the labels of Gumclaw's support pipeline, as its own close does."""
    if not THREAD.fullmatch(thread_id or ''):
        raise ServiceError(400, 'Choose a conversation of the support queue.')
    state = data.get('state')
    if state not in ('open', 'closed'):
        raise ServiceError(400, 'Choose open or closed.')
    token, account, writable = _connected(access) if access is not None else _access(root, write=True)
    if not writable:
        raise ServiceError(409, NO_MAILBOX)
    fetch, call = fetch or _fetch, call or _call
    _labels_cache.pop(account, None)
    ids = {name: label for label, name in _label_names(token, account, fetch).items()}
    if OPEN_LABEL not in ids or DONE_LABEL not in ids:
        raise ServiceError(409, f'The mailbox {account} has no support labels, so Coconut cannot close its conversations.')
    if state == 'closed':
        change = dict(addLabelIds=[ids[DONE_LABEL]], removeLabelIds=[ids[name] for name in CLEARED if name in ids])
    else:
        change = dict(addLabelIds=[ids[OPEN_LABEL]], removeLabelIds=[ids[DONE_LABEL]])
    call('POST', API + thread_id.lower() + '/modify', token, account, change)
    return conversation(root, thread_id, access=(token, account, writable), fetch=fetch)


def mutate_support(service, method, parts, data, request_key, fingerprint):
    """An answer or a change of state. A repeated request key answers with the saved result, which is saved only after
    Gmail confirmed the change (business.mutate_business)."""
    if not isinstance(data, dict):
        raise ServiceError(400, 'Send a JSON object for this support action.')
    with write_lock:
        with service._read_view() as view:
            receipt = view.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (request_key,)).fetchone()
        if receipt:
            if not hmac.compare_digest(receipt[0], hashlib.sha256(fingerprint.encode()).hexdigest()):
                raise ServiceError(409, 'This request key belongs to a different change.')
            return json.loads(receipt[1])
        if method == 'POST' and len(parts) == 2 and parts[1] == 'reply':
            if set(data) - {'text', 'message'}:
                raise ServiceError(400, 'Send the text of the answer and the message that it answers.')
            result = reply(service.root, parts[0], data, request_key)
        elif method == 'PATCH' and len(parts) == 1:
            if set(data) != {'state'}:
                raise ServiceError(400, 'Choose open or closed.')
            result = set_state(service.root, parts[0], data)
        else:
            raise ServiceError(404, 'This support action was not found.')
        with service.transaction():
            service.db.execute('INSERT INTO requests(key,fingerprint,response) VALUES (?,?,?)',
                               (request_key, hashlib.sha256(fingerprint.encode()).hexdigest(), encoded(result)))
        return result


def _walk(part: dict, plain: list, rich: list, files: list) -> None:
    if not isinstance(part, dict):
        return
    kind = str(part.get('mimeType') or '').lower()
    body = part.get('body')
    if not isinstance(body, dict):
        body = {}
    name = str(part.get('filename') or '')
    if name:
        files.append(dict(name=name, size=int(body.get('size') or 0)))
        return
    if kind == 'text/plain' and body.get('data'):
        plain.append(_decode(body['data']))
    elif kind == 'text/html' and body.get('data'):
        rich.append(_decode(body['data']))
    for child in part.get('parts') or []:
        _walk(child, plain, rich, files)


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='replace')
    except (binascii.Error, ValueError):
        return ''


def _date(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class _Text(HTMLParser):
    BREAKS = {'br', 'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'section', 'table'}
    # Not text of the message: the style and the head, and the quoted history of a reply (a blockquote, and the block
    # of Gmail that holds the line "On <date>, <name> wrote:").
    SKIP = {'style', 'script', 'head', 'title', 'blockquote'}
    VOID = {'br', 'img', 'hr', 'meta', 'link', 'input', 'wbr', 'col', 'area', 'base', 'source', 'track', 'embed'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], []

    def handle_starttag(self, tag, attrs):
        if self.hidden:
            if tag not in self.VOID:
                self.hidden.append(tag)
            return
        classes = str(dict(attrs).get('class') or '')
        if tag in self.SKIP or 'gmail_quote' in classes:
            if tag not in self.VOID:
                self.hidden.append(tag)
        elif tag in self.BREAKS:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        if not self.hidden and tag in self.BREAKS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                while self.hidden and self.hidden.pop() != tag:
                    pass
        elif tag in self.BREAKS:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(markup: str) -> str:
    """The text of an HTML body: blocks on their own lines, no style or script, at most one empty line in a row."""
    if not markup.strip():
        return ''
    parser = _Text()
    parser.feed(markup)
    parser.close()
    lines = [re.sub(r'[ \t\u00a0]+', ' ', line).strip() for line in ''.join(parser.parts).splitlines()]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


WROTE = re.compile(r'^On .{4,240}wrote:\s*$')


def trim_quote(text: str) -> str:
    """The text without the quoted history of a reply: from the line "On <date>, <name> wrote:" when only quoted lines
    (">") or nothing follow it. A line that wraps in two counts. Text that does not end in such a quote stays whole."""
    lines = text.split('\n')
    for index, line in enumerate(lines):
        start = line.strip()
        if not start.startswith('On '):
            continue
        joined, end = start, index
        if not WROTE.match(joined) and index + 1 < len(lines):
            joined, end = start + ' ' + lines[index + 1].strip(), index + 1
        if not WROTE.match(joined):
            continue
        rest = [later.strip() for later in lines[end + 1:] if later.strip()]
        if all(later.startswith('>') for later in rest):
            return '\n'.join(lines[:index]).rstrip()
    return text
