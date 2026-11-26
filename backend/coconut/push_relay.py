"""Encrypt on the Mac and send through Coconut's shared push service."""
from __future__ import annotations

import base64
import http.client
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from .team_store import shown_text

# The release uses one public relay. No user supplies Apple credentials.
RELAY_URL = 'https://coconut-push.gumroad.workers.dev'
MAX_CONTENT_BYTES = 2200


def notification_content(payload):
    # Preserve routing fields while fitting the bytes that PushEnvelope encrypts.
    alert = dict(payload['aps']['alert'])
    payload = dict(payload, aps=dict(payload['aps'], alert=alert))

    def encode():
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()

    content = encode()
    for field in ('body', 'subtitle', 'title'):
        text = alert.get(field, '')
        while text and len(content) > MAX_CONTENT_BYTES:
            text = text[:-1]
            alert[field] = text + '…' if text else ''
            content = encode()
        if len(content) <= MAX_CONTENT_BYTES:
            return content
    raise ValueError('Notification routing fields exceed the content bound.')


class RelayTransport:
    def __init__(self, root):
        self.directory = Path(root) / 'push'

    @property
    def configured(self):
        return bool(RELAY_URL and self._private_file('push-encrypt') and self._private_file('sender.key'))

    def _private_file(self, name):
        path = self.directory / name
        return (not self.directory.is_symlink() and path.is_file() and not path.is_symlink()
                and not path.stat().st_mode & 0o077)

    def configuration(self):
        if not self.configured:
            return {'configured': False}
        result = subprocess.run([str(self.directory / 'push-encrypt'), str(self.directory), 'public'],
                                capture_output=True, timeout=5, check=True)
        return {'configured': True, 'relay_url': RELAY_URL, **json.loads(result.stdout)}

    def seal(self, public_key, payload, notification_id, expires):
        return self.seal_content(public_key, notification_content(payload), notification_id, expires)

    def seal_content(self, public_key, content, notification_id, expires):
        """Seal bytes for one phone, as a notification is sealed: a glance of the Home Screen (home_screen.py)."""
        request = dict(publicKey=public_key, id=notification_id, expires=int(expires),
                       content=base64.b64encode(content).decode())
        result = subprocess.run([str(self.directory / 'push-encrypt'), str(self.directory), 'seal'],
                                input=json.dumps(request).encode(), capture_output=True, timeout=5, check=True)
        return json.loads(result.stdout)

    def _post(self, path, route, body):
        """POST to the relay with the send token of the route. Returns the HTTP status and the JSON answer, or a
        status of 0 and a reason."""
        url = urlsplit(RELAY_URL)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            return 0, 'InvalidRelay'
        connection = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=15)
        try:
            connection.request('POST', f'/v1/routes/{route["route_id"]}/{path}',
                               json.dumps(body, separators=(',', ':')).encode(),
                               {'Authorization': 'Bearer ' + route['send_token'], 'Content-Type': 'application/json'})
            response = connection.getresponse()
            data = response.read(4097)
            if response.status != 200:
                return response.status, 'RelayRejected'
            return 200, json.loads(data)
        except (OSError, ValueError, http.client.HTTPException):
            return 0, 'TransportUnavailable'
        finally:
            connection.close()

    def send_glance(self, route, envelope, glance_id, expires, live=None, urgent=False, widgets=False):
        """Give the relay the newest sealed glance of the phone (relay/src/device.js, glance). `live` is the event of
        the Live Activity, `update`, `end` or None; `widgets` asks the widgets of iOS 26 to read the glance again."""
        if not self.configured:
            return 0, 'PushNotConfigured'
        status, result = self._post('glance', route, dict(id=glance_id, expires=int(expires), envelope=envelope,
                                                          live=live, urgent=bool(urgent), widgets=bool(widgets)))
        if status != 200:
            return status, result
        if not isinstance(result, dict):
            return 0, 'InvalidRelayResponse'
        status, reason = result.get('status'), result.get('reason')
        if type(status) is not int or not isinstance(reason, str) or not re.fullmatch(r'[A-Za-z]{1,80}', reason):
            return 0, 'InvalidRelayResponse'
        return status, reason

    def send(self, route, envelope, notification_id, collapse_id, expires):
        if not self.configured:
            return 0, 'PushNotConfigured'
        url = urlsplit(RELAY_URL)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            return 0, 'InvalidRelay'
        connection = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=15)
        request = dict(envelope=envelope, id=notification_id, collapse_id=collapse_id, expires=int(expires))
        try:
            connection.request('POST', f'/v1/routes/{route["route_id"]}/send',
                               json.dumps(request, separators=(',', ':')).encode(),
                               {'Authorization': 'Bearer ' + route['send_token'], 'Content-Type': 'application/json'})
            response = connection.getresponse()
            data = response.read(4097)
            if response.status != 200:
                return response.status, 'RelayRejected'
            result = json.loads(data)
            status, reason = result['status'], result['reason']
            if type(status) is not int or not isinstance(reason, str) or not re.fullmatch(r'[A-Za-z]{1,80}', reason):
                return 0, 'InvalidRelayResponse'
            return status, reason
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            return 0, 'TransportUnavailable'
        finally:
            connection.close()


def validate_route(route):
    if not isinstance(route, dict) or set(route) != {'route_id', 'send_token', 'public_key'}:
        return False
    if not all(isinstance(route.get(key), str) and re.fullmatch(r'[a-f0-9]{64}', route[key]) for key in ('route_id', 'send_token')):
        return False
    try:
        return isinstance(route['public_key'], str) and len(base64.b64decode(route['public_key'], validate=True)) == 32
    except ValueError:
        return False


# The body of one notification: the message itself, as much as the sealed content holds. An expanded notification
# shows all of it, with its lines and its list items.
BODY_LIMIT, BODY_BYTES = 1200, 1800


def notification_text(value, limit=240, byte_limit=960, lines=False):
    """Plain text of Markdown for a notification. With `lines`, the text keeps its paragraphs and list items (a bullet
    becomes "•"), so a long message reads as it does in the chat. Without it, the text is one line."""
    text = str(value or '')
    text = re.sub(r'^\s*(`{3,}|~{3,})[^\n]*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'!?\[([^\]\n]+)\]\([^\n]*?\)', r'\1', text)
    if lines:
        text = re.sub(r'^[ \t]{0,3}[-*+][ \t]+', '• ', text, flags=re.MULTILINE)
        text = re.sub(r'^[ \t]{0,3}(?:#{1,6}[ \t]+|>[ \t]*)', '', text, flags=re.MULTILINE)
    else:
        text = re.sub(r'^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)', '', text, flags=re.MULTILINE)
    for marker in ('**', '__', '~~', '`'):
        text = re.sub(re.escape(marker) + r'(?=\S)(.+?)' + re.escape(marker), r'\1', text)
    if lines:
        rows = [' '.join(row.split()) for row in text.split('\n')]
        text = re.sub(r'\n{3,}', '\n\n', '\n'.join(rows)).strip()
    else:
        text = ' '.join(text.split())
    if len(text) > limit or len(text.encode()) > byte_limit:
        text = text[:limit - 1].encode()[:byte_limit - 3].decode(errors='ignore').rstrip() + '…'
    return text


def body_text(value):
    return notification_text(value, limit=BODY_LIMIT, byte_limit=BODY_BYTES, lines=True)


def subject(task, body=''):
    """The subtitle of a notification of an agent: the work that the message is about, unless the body says it."""
    title = notification_text(task.get('title'), limit=100, byte_limit=300)
    return 'Re: ' + title if title and title.casefold() not in body[:len(title) + 20].casefold() else ''


def decision_text(decision):
    """A card in full: its question, why, its risk, and the choices, so the person can decide from the lock screen."""
    parts = [decision['title']]
    for field in ('context', 'explanation'):
        if decision.get(field):
            parts.append(decision[field])
    if decision.get('risk'):
        parts.append('Risk: ' + decision['risk'])
    labels = [option.get('label') for option in decision.get('options') or [] if isinstance(option, dict)]
    if labels:
        parts.append('Choices: ' + ' · '.join(label for label in labels if label))
    return '\n\n'.join(parts)


def message_text(service, message):
    """The text of a message of an agent. A file card names the file and its preview."""
    if message.get('artifact_id'):
        artifact = service._get('artifacts', message['artifact_id'])
        if artifact.get('kind') == 'file':
            return 'File: ' + artifact['title']
        return '\n\n'.join(part for part in (artifact['title'], artifact.get('preview')) if part)
    return message['text']


def with_subject(title, task, body):
    alert = {'title': title, 'body': body}
    if subtitle := subject(task, body):
        alert['subtitle'] = subtitle
    return alert


def turn_replies(service, task, bot):
    """The replies of the agent in the newest turn of a task that a person sees, newest first."""
    rows = service.db.execute('''SELECT data FROM messages
        WHERE json_extract(data, '$.task_id')=? AND json_extract(data, '$.role')='assistant'
          AND json_extract(data, '$.kind') IN ('text','artifact')
          AND json_extract(data, '$.created_at')>=?
          AND COALESCE(json_extract(data, '$.sender_bot_id'), ?)=?
          AND COALESCE(json_array_length(data, '$.recipient_bot_ids'), 0)=0
          AND COALESCE(json_extract(data, '$._hidden'), 0)=0
          AND COALESCE(json_extract(data, '$._interim'), 0)=0
        ORDER BY rowid DESC''', (task.get('_result_task_id', task['id']), task.get('_turn_started_at', task['created_at']), bot['id'], bot['id']))
    return [json.loads(data) for (data,) in rows]


def notification_alert(service, job, bot, task, decision):
    title = notification_text(bot['name'], limit=80, byte_limit=256)
    if decision:
        return {'title': title, 'subtitle': 'Needs your decision', 'body': body_text(decision_text(decision))}
    if job['kind'] == 'failed':
        # Tool errors can contain credentials. Keep those inside the conversation.
        return {'title': title, 'subtitle': 'Could not finish', 'body': notification_text(task['title'])}
    if job.get('message_id'):
        message = service._get('messages', job['message_id'])
        return with_subject(title, task, body_text(message_text(service, message)))
    rows = turn_replies(service, task, bot)
    # The newest messages of the turn, oldest first, so the notification reads as the reply does.
    texts = []
    for message in rows:
        text = body_text(message_text(service, message))
        if text:
            texts.insert(0, text)
        if len('\n\n'.join(texts)) >= BODY_LIMIT:
            break
    if texts:
        return with_subject(title, task, body_text('\n\n'.join(texts)))
    return {'title': title, 'subtitle': 'Finished', 'body': notification_text(task['title'])}


def team_alert(store, room, entry):
    """The alert of a team message, in the pattern of a group chat: the writer in the title, the team in the
    subtitle, the text in the body. A message of a bot names the bot. A message of a named chat has the name of the chat
    and the name of the team in the subtitle, for example "Design · Example Team". When the store has no name for the
    writer, the title is that subtitle, and the alert has no subtitle."""
    message = store.message(room, entry)
    chat = store.chat(room, message['chat']) if message['chat'] else None
    team = store.room(room)['name']
    team = notification_text(f'{chat["name"]} · {team}' if chat else team, limit=140, byte_limit=512)
    writer = notification_text(message['bot_name'] if message['bot'] else message['member_name'], limit=80, byte_limit=256)
    body = body_text(shown_text(message['text'], message['files']))
    if not writer:
        return {'title': team, 'body': body}
    return {'title': writer, 'subtitle': team, 'body': body}


def direct_alert(store, room, entry):
    """The alert of a direct message, in the pattern of a chat with one person: the writer in the title and the text in
    the body, with no subtitle. The writer of a message of a chat with a bot is the bot. When the store has no name for
    the writer, the title is the name of the team."""
    message = store.direct(room, entry)
    writer = notification_text(message['bot_name'] if message['bot'] else message['member_name'], limit=80,
                               byte_limit=256)
    title = writer or notification_text(store.room(room)['name'], limit=80, byte_limit=256)
    return {'title': title, 'body': body_text(message['text'])}


NOTICE_LABELS = {'announcement': 'Announcement', 'status': 'Status', 'rule': 'Rule', 'deadline': 'Deadline'}


def notice_alert(store, room, entry):
    """The alert of a new notice: the author in the title, the kind and the team in the subtitle, for example
    "Deadline · Example Team", and the text in the body. A deadline adds its end. Without a name for the author, the
    title is that subtitle and the alert has no subtitle."""
    notice = store.notice(room, entry)
    team = store.room(room)['name']
    label = notification_text(f'{NOTICE_LABELS.get(notice["type"], "Notice")} · {team}', limit=140, byte_limit=512)
    text = notice['text']
    if notice.get('ends_at'):
        try:
            from datetime import datetime
            ends = datetime.fromisoformat(notice['ends_at'].replace('Z', '+00:00')).astimezone()
            text += '\nUntil ' + ends.strftime('%a %b %-d, %-I:%M %p')
        except ValueError:
            pass
    body = body_text(text)
    writer = notification_text(notice.get('member_name') or '', limit=80, byte_limit=256)
    if not writer:
        return {'title': label, 'body': body}
    return {'title': writer, 'subtitle': label, 'body': body}


def source_alert(data):
    """The alert of a notification of GitHub or Gmail, from what the Mac read when it came (notification_sources).
    GitHub: the repository and number in the title, why it notifies in the subtitle, the title of the issue or pull
    request in the body. Gmail: the sender in the title, the subject in the subtitle, the start of the mail in the body."""
    title = notification_text(data.get('title'), limit=80, byte_limit=256) or (
        'GitHub' if data.get('kind') == 'github' else 'Gmail')
    subtitle = notification_text(data.get('subtitle'), limit=140, byte_limit=512)
    body = body_text(data.get('body'))
    if not body:
        body, subtitle = subtitle or title, ''
    return {'title': title, **({'subtitle': subtitle} if subtitle else {}), 'body': body}


GROUP_NOUNS = (('decision', 'card', 'cards'), ('message', 'message', 'messages'), ('team', 'team message', 'team messages'),
               ('direct', 'direct message', 'direct messages'), ('notice', 'notice', 'notices'), ('failed', 'failed task', 'failed tasks'),
               ('completed', 'finished task', 'finished tasks'), ('github', 'GitHub notification', 'GitHub notifications'),
               ('mail', 'email', 'emails'))


def group_lines(lines, total):
    """The body of a group that the Mac seals: one line for each item, newest last, at most 6, and the count of the
    rest."""
    shown = lines[-6:]
    rest = total - len(shown)
    return '\n'.join(shown + ([f'and {rest} more'] if rest > 0 else []))


def group_text(items):
    """The count of a group, for example "3 cards and 1 message are waiting". It names no bot and no content."""
    counts = {kind: sum(item['kind'] == kind for item in items) for kind, _, _ in GROUP_NOUNS}
    parts = [f'{counts[kind]} {one if counts[kind] == 1 else many}' for kind, one, many in GROUP_NOUNS if counts[kind]]
    text = parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]
    return text + (' is waiting' if sum(counts.values()) == 1 else ' are waiting')
