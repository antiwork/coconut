"""Exact, editable message drafts use the shared decision record."""
from __future__ import annotations

from .card_tools import DRAFT_SCHEMA


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
