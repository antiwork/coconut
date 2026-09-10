"""Shared bounds and encoding for structured worker events."""
import json


MAX_EVENT_CHARACTERS = 2_000_000


def event_frame(kind, data):
    return json.dumps({'type': kind, 'data': data}, ensure_ascii=False) + '\n'
