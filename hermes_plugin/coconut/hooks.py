"""Plugin hooks of the `coconut` platform: one conversation for each bot, and streamed text.

One conversation. A Coconut bot has one native conversation in its profile's state.db. The Coconut worker resumes it
from a root session ID and records the newest session as the tip in the `state_meta` table of Hermes
(backend/coconut/native_conversation.py, TIP_KEY). Before the gateway runs a Coconut turn, `join_conversation` points
the gateway session of the chat at that same tip with the session store that the `pre_gateway_dispatch` hook receives.
When the bot has no conversation for this root yet, the gateway session becomes the tip: a new session when the chat's
session holds an earlier conversation, for example after a restart of the bots. Both runtimes then write to the same
session, and each follows the compression lineage of Hermes from it, so a worker turn and a gateway turn see the same
history.

Cron runs. A cron run of the gateway in a bot's profile reports to a Coconut turn through the session hooks
(cron.py).

Work. `save_todos` (post_tool_call) reports each todo list of a Coconut turn, and Coconut keeps it as the work of the
task. An unfinished list continues in a later turn, as with the worker.

Cards. A card of coconut_ask ends its turn, and a follow-up on a pending card cannot make a card. `guard_tool`
(pre_tool_call) blocks those tool calls. The card rules of the worker (card_tools.DECISION_INSTRUCTIONS) go into the
system prompt of each Coconut session, in parts of at most 4000 characters, as Hermes allows. So do the worker's
rules for a merge card and for bot changes.

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
    # A cron run of the gateway in a bot's profile is a Coconut turn (cron.py).
    ctx.register_hook('on_session_start', cron_start)
    ctx.register_hook('on_interim_message', cron_interim)
    ctx.register_hook('post_llm_call', cron_answer)
    ctx.register_hook('on_session_end', cron_end)
    ctx.register_hook('pre_tool_call', guard_tool)
    ctx.register_hook('post_tool_call', save_todos)
    from .tools import cards
    module = cards()
    for number, part in enumerate(prompt_parts(module.DECISION_INSTRUCTIONS)):
        ctx.register_system_prompt_section(f'coconut.cards.{number}', _section(part), max_chars=SECTION_CHARS)
    # The worker's rules for a merge card and for bot changes (card_tools).
    rules = module.PR_MERGE_INSTRUCTIONS + module.BOT_CREATION_INSTRUCTIONS + module.BOT_DELETION_INSTRUCTIONS
    for number, part in enumerate(prompt_parts(rules)):
        ctx.register_system_prompt_section(f'coconut.rules.{number}', _section(part), max_chars=SECTION_CHARS)


SECTION_CHARS = 4000
# Tools that a follow-up on a pending card must not call: it answers a question and leaves the card as it is.
CARD_TOOLS = ('coconut_ask', 'coconut_suggest', 'clarify', 'coconut_track_pr', 'coconut_message_bot',
              'coconut_wait', 'coconut_correct_withdrawal', 'coconut_create_bot', 'coconut_delete_bot')
PAUSED_MESSAGE = ('Coconut shows a card and waits for the answer of the person. End this turn now. Coconut continues '
                  'this conversation with the answer.')
CONTEXT_MESSAGE = 'Answer the current question. Its pending decision stays open.'
REFRESH_TOOL = 'coconut_refresh_decision'
REFRESH_MESSAGE = 'Record this check with coconut_refresh_decision. The check does not change the card in other ways.'
NOT_A_CHECK_MESSAGE = 'Only an automatic check of a card records a check result.'
STATUS_MESSAGE = 'Answer from the saved context in the message. This answer uses no tools.'


def prompt_parts(text, size=SECTION_CHARS):
    """Split text at sentence ends into parts of at most `size` characters."""
    parts, part = [], ''
    for sentence in text.replace('. ', '.\n').split('\n'):
        piece = sentence if not part else ' ' + sentence
        if part and len(part) + len(piece) > size:
            parts.append(part)
            part, piece = '', sentence
        part += piece
    if part:
        parts.append(part)
    return parts


def _section(text):
    def render(session_info=None, **_):
        return text if (session_info or {}).get('platform') == NAME else ''
    return render


# The todo tool of Hermes and its earlier names. Its result holds the whole list.
TODO_TOOLS = ('todo_list', 'todo')


def save_todos(tool_name='', result=None, **_):
    """post_tool_call: report the todo list of a Coconut turn, so Coconut keeps the work of the task (as the worker)."""
    if tool_name not in TODO_TOOLS:
        return None
    from .adapter import MODES
    holder, turn = _turn()
    if turn is None or MODES.get(turn) in ('context', 'refresh'):
        # A follow-up on a pending card and a check of the card do not change the work of its task.
        return None
    try:
        import json
        state = json.loads(result) if isinstance(result, str) else result
        if not isinstance(state, dict) or not isinstance(state.get('todos'), list):
            return None
        from .adapter import REQUEST_SECONDS
        holder._bridge.request('/v1/gateway/tools', {'turn_id': turn, 'tool': 'todos', 'arguments': {
            'todos': state['todos'], 'revision': state.get('revision', 0)}}, REQUEST_SECONDS)
    except Exception as error:
        LOG.warning('Coconut: the Coconut service did not save the todo list of a turn (%s).', type(error).__name__)
    return None


def guard_tool(tool_name='', **_):
    """pre_tool_call: block a tool call of a turn that a card ended, and a card in a follow-up on a pending card."""
    from .adapter import MODES, PAUSED
    holder, turn = _turn()
    if turn is None:
        return None
    if turn in PAUSED:
        return {'action': 'block', 'message': PAUSED_MESSAGE}
    if MODES.get(turn) == 'status':
        # A status question is answered from its saved context, without tools, as the worker answers it.
        return {'action': 'block', 'message': STATUS_MESSAGE}
    if MODES.get(turn) == 'context' and tool_name in CARD_TOOLS:
        return {'action': 'block', 'message': CONTEXT_MESSAGE}
    if MODES.get(turn) == 'refresh':
        # An automatic check of a card reads the evidence and records its result. It makes no card and answers for
        # nobody, as in the worker.
        if tool_name in CARD_TOOLS or tool_name == 'coconut_resolve_reply':
            return {'action': 'block', 'message': REFRESH_MESSAGE}
        return None
    if tool_name == REFRESH_TOOL:
        return {'action': 'block', 'message': NOT_A_CHECK_MESSAGE}
    return None


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
                if db.get_session(entry.session_id) and db.get_resume_conversations(entry.session_id)[0]:
                    # The chat has a session of an earlier conversation, for example before a restart of the bots
                    # gave the chat a new root. The new conversation starts in a new session, as /new does.
                    entry = session_store.get_or_create_session(source, force_new=True)
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
    """The holder (an adapter or a cron run) and the Coconut turn of this context, or (None, None)."""
    from gateway.session_context import get_session_env
    if get_session_env('HERMES_SESSION_PLATFORM', '') == NAME:
        from .adapter import turn_for_chat
        holder, turn = turn_for_chat(get_session_env('HERMES_SESSION_CHAT_ID', ''))
        if turn is not None:
            return holder, turn
    from . import cron
    run = cron.current()
    return (run, run.turn) if run is not None else (None, None)


def cron_start(session_id='', **_):
    from . import cron
    cron.current(session_id)


def cron_interim(text='', already_streamed=False, **_):
    from . import cron
    run = cron.current()
    if run is not None and str(text).strip():
        run.flush_stream(run.turn)
        run.send(text, interim=True)


def cron_answer(assistant_response='', **_):
    from . import cron
    run = cron.current()
    if run is not None and str(assistant_response or '').strip():
        run.flush_stream(run.turn)
        run.send(assistant_response)


def cron_end(completed=False, failed=False, interrupted=False, **_):
    from . import cron
    cron.finish('cancelled' if interrupted else 'success' if completed and not failed else 'failure')


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
