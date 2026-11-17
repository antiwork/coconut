"""A conversation of the Support report, read from Gmail.

The support report (the dashboards repository, support-dashboard.sh) gives each conversation of the customer queue the
ID of its Gmail thread as `slug`, and its web page opens that thread in Gmail. Coconut reads the same thread with the
Gmail API through the Google connection of this Mac (google_connection.py, read-only), so the apps show the
conversation in Coconut. Nothing is kept: each read goes to Gmail.

    GET /v1/support/conversations/<thread>

The answer has the subject, the connected account, the link of the thread in Gmail, and the messages oldest first:
the sender, the recipients, the time, the text without the quoted history of a reply, the names of the attachments,
and `outgoing` for a message that the support mailbox sent.
"""
from __future__ import annotations

import base64
import binascii
import html
import json
import re
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .service_base import ServiceError

THREAD = re.compile(r'[0-9a-fA-F]{8,32}')
API = 'https://gmail.googleapis.com/gmail/v1/users/me/threads/'
# A thread larger than this is not read: the apps show the link to Gmail instead.
MAX_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 100
MAX_TEXT = 20000
NOT_CONNECTED = 'Connect Google in You, Connections, to read support conversations here.'


def gmail_url(thread_id: str) -> str:
    return 'https://mail.google.com/mail/u/0/#all/' + thread_id


def conversation(root, thread_id: str, *, access=None, fetch=None) -> dict:
    """The thread `thread_id` of the connected Gmail. `access` and `fetch` replace the Google connection and the
    request to Gmail for a check by hand."""
    if not THREAD.fullmatch(thread_id or ''):
        raise ServiceError(400, 'Choose a conversation of the support queue.')
    if access is None:
        access = _access(root)
    token, account = access
    data = (fetch or _fetch)(API + thread_id.lower() + '?format=full', token, account)
    messages = [message(item, account) for item in (data.get('messages') or []) if isinstance(item, dict)]
    messages.sort(key=lambda item: item['date'] or '')
    subject = next((item['subject'] for item in messages if item['subject']), '')
    for item in messages:
        item.pop('subject', None)
    return dict(id=thread_id.lower(), subject=subject, account=account, url=gmail_url(thread_id.lower()),
                messages=messages[-MAX_MESSAGES:], more=max(0, len(messages) - MAX_MESSAGES))


def _access(root):
    from .google_connection import GoogleAuthorizationError, GoogleConnections, GoogleError
    profile = GoogleConnections(root).profile()
    if not profile.status().get('connected'):
        raise ServiceError(409, NOT_CONNECTED)
    try:
        return profile.access()
    except GoogleAuthorizationError:
        raise ServiceError(409, 'Google needs a new sign-in. Reconnect Google in You, Connections.') from None
    except GoogleError as error:
        raise ServiceError(502, str(error)) from None


def _fetch(url: str, token: str, account: str) -> dict:
    request = Request(url, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read(MAX_BYTES + 1)
    except HTTPError as error:
        if error.code == 404:
            raise ServiceError(404, f'This conversation is not in the Gmail of {account}.') from None
        if error.code in (401, 403):
            raise ServiceError(409, 'Google refused this read. Reconnect Google in You, Connections.') from None
        raise ServiceError(502, 'Gmail is unavailable. Try again shortly.') from None
    except (URLError, TimeoutError, OSError):
        raise ServiceError(502, 'Gmail did not answer. Try again shortly.') from None
    if len(body) > MAX_BYTES:
        raise ServiceError(413, 'This conversation is too large to show here. Open it in Gmail.')
    try:
        data = json.loads(body)
    except ValueError:
        raise ServiceError(502, 'Gmail returned an unreadable answer.') from None
    if not isinstance(data, dict):
        raise ServiceError(502, 'Gmail returned an unreadable answer.')
    return data


def message(item: dict, account: str) -> dict:
    payload = item.get('payload')
    if not isinstance(payload, dict):
        payload = {}
    headers = {}
    for header in payload.get('headers') or []:
        if isinstance(header, dict) and isinstance(header.get('name'), str):
            headers.setdefault(header['name'].lower(), str(header.get('value') or ''))
    name, address = parseaddr(headers.get('from', ''))
    recipients = [mail for _, mail in getaddresses([headers.get('to', ''), headers.get('cc', '')]) if mail]
    plain, rich, files = [], [], []
    _walk(payload, plain, rich, files)
    text = '\n\n'.join(part for part in plain if part.strip()) or html_text('\n'.join(rich))
    text = trim_quote(text.replace('\r\n', '\n')).strip()
    if not text:
        text = html.unescape(str(item.get('snippet') or ''))
    labels = item.get('labelIds') or []
    return dict(id=str(item.get('id') or ''), subject=headers.get('subject', ''), from_name=name or address,
                from_email=address.lower(), to=recipients, date=_date(item.get('internalDate')),
                text=text[:MAX_TEXT], cut=len(text) > MAX_TEXT, attachments=files,
                outgoing='SENT' in labels or (bool(address) and address.lower() == str(account).lower()))


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
