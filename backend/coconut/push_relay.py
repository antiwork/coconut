"""Encrypt on the Mac and send through Coconut's shared push service."""
from __future__ import annotations

import base64
import http.client
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

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
        content = notification_content(payload)
        request = dict(publicKey=public_key, id=notification_id, expires=int(expires),
                       content=base64.b64encode(content).decode())
        result = subprocess.run([str(self.directory / 'push-encrypt'), str(self.directory), 'seal'],
                                input=json.dumps(request).encode(), capture_output=True, timeout=5, check=True)
        return json.loads(result.stdout)

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


def notification_text(value, limit=240, byte_limit=960):
    text = str(value or '')
    text = re.sub(r'^\s*(`{3,}|~{3,})[^\n]*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'!?\[([^\]\n]+)\]\([^\n]*?\)', r'\1', text)
    text = re.sub(r'^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)', '', text, flags=re.MULTILINE)
    for marker in ('**', '__', '~~', '`'):
        text = re.sub(re.escape(marker) + r'(?=\S)(.+?)' + re.escape(marker), r'\1', text)
    text = ' '.join(text.split())
    if len(text) > limit or len(text.encode()) > byte_limit:
        text = text[:limit - 1].encode()[:byte_limit - 3].decode(errors='ignore').rstrip() + '…'
    return text


def notification_alert(service, job, bot, task, decision):
    title = notification_text(bot['name'], limit=80, byte_limit=256)
    if decision:
        text = decision['title'] + ('\n' + decision['explanation'] if decision.get('explanation') else '')
        return {'title': title, 'body': notification_text(text)}
    if job['kind'] == 'failed':
        # Tool errors can contain credentials. Keep those inside the conversation.
        return {'title': title, 'subtitle': 'Could not finish', 'body': notification_text(task['title'])}
    if job.get('message_id'):
        message = service._get('messages', job['message_id'])
        text = message['text']
        if message.get('artifact_id'):
            artifact = service._get('artifacts', message['artifact_id'])
            text = artifact.get('preview') or artifact['title']
        return {'title': title, 'body': notification_text(text)}
    rows = service.db.execute('''SELECT data FROM messages
        WHERE json_extract(data, '$.task_id')=? AND json_extract(data, '$.role')='assistant'
          AND json_extract(data, '$.kind') IN ('text','artifact')
          AND json_extract(data, '$.created_at')>=?
          AND COALESCE(json_extract(data, '$.sender_bot_id'), ?)=?
          AND COALESCE(json_array_length(data, '$.recipient_bot_ids'), 0)=0
          AND COALESCE(json_extract(data, '$._hidden'), 0)=0
        ORDER BY rowid DESC''', (task.get('_result_task_id', task['id']), task.get('_turn_started_at', task['created_at']), bot['id'], bot['id']))
    for (data,) in rows:
        message = json.loads(data)
        text = message['text']
        if message['kind'] == 'artifact' and message.get('artifact_id'):
            artifact = service._get('artifacts', message['artifact_id'])
            text = artifact.get('preview') or artifact['title']
        body = notification_text(text)
        if body:
            return {'title': title, 'body': body}
    return {'title': title, 'subtitle': 'Finished', 'body': notification_text(task['title'])}
