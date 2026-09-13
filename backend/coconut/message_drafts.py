"""Exact, editable message drafts use the shared decision record."""
from __future__ import annotations

DRAFT_SCHEMA = {
    'type': 'object',
    'description': 'Exact outbound text for review. Use real addresses or channel/thread URLs. No attachments or hidden recipients. Creating this card never sends it.',
    'properties': {key: dict(type='string', maxLength=limit, description=description)
        for key, limit, description in (
            ('channel', 80, 'Delivery service, such as Gmail, Slack, or GitHub.'),
            ('recipient', 500, 'Exact recipient address or destination URL. Do not use an ambiguous display name.'),
            ('sender', 320, 'Sending account. Required when more than one account could send it.'),
            ('subject', 500, 'Email subject, if applicable.'),
            ('reply_to', 2000, 'Exact message or thread URL when replying.'),
            ('body', 20000, 'The complete message as plain text. Preserve all line breaks.'))},
    'required': ['channel', 'recipient', 'body'],
    'additionalProperties': False,
}


def message_draft(value):
    from .service import ServiceError
    if not isinstance(value, dict) or set(value) - set(DRAFT_SCHEMA['properties']):
        raise ServiceError(400, 'Supply the message and its destination.')
    for key, spec in DRAFT_SCHEMA['properties'].items():
        text = value.get(key, '')
        if (not isinstance(text, str) or len(text) > spec['maxLength']
                or (key in DRAFT_SCHEMA['required'] and not text.strip())
                or any(ord(c) < 32 and (key != 'body' or c not in '\n\r\t') for c in text)):
            raise ServiceError(400, 'Check the draft text and destination.')
    return {key: value[key] for key in DRAFT_SCHEMA['properties'] if key in value}


def draft_options():
    return [dict(id='send', label='Send', answer='approve', explanation='Send this exact message.'),
            dict(id='discard', label='Discard', answer='reject', explanation='Send nothing.')]
