"""Plugin hooks of the `coconut` platform: one conversation for each bot, and streamed text.

One conversation. A Coconut bot has one native conversation in its profile's state.db. The Coconut worker resumes it
from a root session ID and records the newest session as the tip in the `state_meta` table of Hermes
(backend/coconut/native_conversation.py, TIP_KEY). Before the gateway runs a Coconut turn, `join_conversation` points
the gateway session of the chat at that same tip with the session store that the `pre_gateway_dispatch` hook receives.
When the bot has no conversation yet, the gateway session becomes the tip. Both runtimes then write to the same
session, and each follows the compression lineage of Hermes from it, so a worker turn and a gateway turn see the same
history.

Streamed text. The `on_stream_*` hooks receive the text deltas of each model call, also when the gateway streams nothing
to the platform (`streaming.enabled` off). A hook runs in a Hermes dispatcher thread with the session context of the
turn, so `get_session_env` names the chat. Only a turn of a Coconut chat that an adapter of this process runs goes to
the service. The person's streaming settings stay as they are.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import NAME

LOG = logging.getLogger(__name__)
# The tip key of backend/coconut/native_conversation.py.
TIP_KEY = 'coconut.session-tip.'


def register(ctx):
    ctx.register_hook('pre_gateway_dispatch', join_conversation)
    ctx.register_hook('on_stream_start', stream_start)
    ctx.register_hook('on_stream_delta', stream_delta)
    ctx.register_hook('on_stream_end', stream_end)


def join_conversation(event=None, session_store=None, **_):
    source = getattr(event, 'source', None)
    if source is None or getattr(source.platform, 'value', source.platform) != NAME or session_store is None:
        return None
    from .adapter import session_root_for_chat
    root = session_root_for_chat(source.chat_id)
    if not root:
        return None
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB
        db = SessionDB(db_path=Path(get_hermes_home()) / 'state.db')
        try:
            tip = db.get_meta(TIP_KEY + root)
            if not (tip and db.get_session(tip)):
                tip = root if db.get_session(root) else None
            entry = session_store.get_or_create_session(source)
            if tip is None:
                db.set_meta(TIP_KEY + root, entry.session_id)
            else:
                target = db.resolve_resume_session_id(tip)
                if db.resolve_resume_session_id(entry.session_id) != target:
                    session_store.switch_session(entry.session_key, target)
        finally:
            db.close()
    except Exception:
        # The turn still runs, in the gateway's own session of the chat.
        LOG.exception('Coconut: the gateway session did not join the conversation of the bot.')
    return None


def _turn():
    from gateway.session_context import get_session_env
    if get_session_env('HERMES_SESSION_PLATFORM', '') != NAME:
        return None, None
    from .adapter import turn_for_chat
    return turn_for_chat(get_session_env('HERMES_SESSION_CHAT_ID', ''))


def stream_start(**_):
    adapter, turn = _turn()
    if turn is not None:
        adapter.flush_stream(turn)
        adapter._stream_numbers[turn] = adapter._stream_numbers.get(turn, 0) + 1


def stream_delta(delta='', kind='text', **_):
    if kind != 'text' or not delta:
        return
    adapter, turn = _turn()
    if turn is not None:
        adapter.stream(turn, adapter._stream_numbers.get(turn, 0), str(delta))


def stream_end(**_):
    adapter, turn = _turn()
    if turn is not None:
        adapter.flush_stream(turn)
