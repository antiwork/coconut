"""Plugin hooks of the `coconut` platform: one conversation for each bot.

One conversation. A Coconut bot has one native conversation in its profile's state.db. The Coconut worker resumes it
from a root session ID and records the newest session as the tip in the `state_meta` table of Hermes
(backend/coconut/native_conversation.py, TIP_KEY). Before the gateway runs a Coconut turn, `join_conversation` points
the gateway session of the chat at that same tip with the session store that the `pre_gateway_dispatch` hook receives.
When the bot has no conversation for this root yet, the gateway session becomes the tip: a new session when the chat's
session holds an earlier conversation, for example after a restart of the bots. Each turn then writes to the same
session and follows the compression lineage of Hermes from it, so the turns of a chat see the same history.

Records. The comments of a native task and the messages between bots are records that a session keeps once
(native_conversation.turn_records). After the join, `add_records` adds to the turn's own text only the records that
the session does not hold yet, and `commit_records` notes them as held when the turn ends. A new session holds none.

Cron runs. A cron run of the gateway in a bot's profile reports to a Coconut turn through the session hooks
(cron.py).

Work. `save_todos` (post_tool_call) reports each todo list of a Coconut turn, and Coconut keeps it as the work of the
task. An unfinished list continues in a later turn, as with the worker.

Cards. A card of coconut_ask ends its turn, and a follow-up on a pending card cannot make a card. `guard_tool`
(pre_tool_call) blocks those tool calls. The card rules of the worker go into the system prompt of each Coconut session
with the hint of the platform (coconut.platform_hint).

Compression. `note_prompt_size` (post_api_request) marks a Coconut chat whose model call reached the compression
threshold of Hermes, and the adapter compresses that chat after the turn. `follow_new_session` (on_session_reset) keeps
the bot's conversation in the new session after /new.

Text. The plugin sends no partial text. The phone shows a message of the bot only when it is complete: each send of
the gateway, and for a cron run each commentary and the answer (cron.py).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import NAME

LOG = logging.getLogger(__name__)
# The tip key of backend/coconut/native_conversation.py.
TIP_KEY = 'coconut.session-tip.'


def register(ctx):
    ctx.register_hook('pre_gateway_dispatch', join_conversation)
    # A cron run of the gateway in a bot's profile is a Coconut turn (cron.py).
    ctx.register_hook('on_session_start', cron_start)
    ctx.register_hook('on_interim_message', cron_interim)
    ctx.register_hook('post_llm_call', cron_answer)
    ctx.register_hook('on_session_end', cron_end)
    ctx.register_hook('pre_tool_call', guard_tool)
    ctx.register_hook('post_tool_call', save_todos)
    ctx.register_hook('post_api_request', note_prompt_size)
    ctx.register_hook('on_session_reset', follow_new_session)
    if callable(getattr(ctx, 'register_middleware', None)):
        # The context of a turn goes to its model calls only. A Hermes without request middleware gets the whole
        # text as the message, as before (adapter.CONTEXT_MIDDLEWARE).
        from .adapter import CONTEXT_MIDDLEWARE
        ctx.register_middleware('llm_request', add_turn_context)
        CONTEXT_MIDDLEWARE['registered'] = True


# Tools that a follow-up on a pending card must not call: it answers a question and leaves the card as it is.
CARD_TOOLS = ('coconut_ask', 'coconut_suggest', 'clarify', 'coconut_track_pr', 'coconut_message_bot',
              'coconut_wait', 'coconut_correct_withdrawal', 'coconut_create_bot', 'coconut_delete_bot',
              'coconut_continue_work', 'coconut_track_work', 'coconut_save_document', 'coconut_read_document',
              'coconut_delete_document')
PAUSED_MESSAGE = ('Coconut shows a card and waits for the answer of the person. End this turn now. Coconut continues '
                  'this conversation with the answer.')
WAITED_MESSAGE = ('Coconut recorded the wait of this turn. Finish this turn now with a short summary, and call no other '
                  'tool. Coconut continues the work when the wait ends.')
CONTEXT_MESSAGE = 'Answer the current question. Its pending decision stays open.'
REFRESH_TOOL = 'coconut_refresh_decision'
REFRESH_MESSAGE = 'Record this check with coconut_refresh_decision. The check does not change the card in other ways.'
NOT_A_CHECK_MESSAGE = 'Only an automatic check of a card records a check result.'
AUDIT_BOARD_MESSAGE = ('This board is not the board of Coconut. Use coconut_kanban_list, coconut_kanban_show, '
                       'coconut_kanban_create and coconut_kanban_comment, which use the board of Coconut.')
BOARD_MESSAGE = ('This board is not the board of Coconut. Call the coconut_kanban_ tool with the same ending, for '
                 'example coconut_kanban_show for kanban_show. It uses the board of Coconut, and in a native outcome '
                 'the claim of this turn.')

STATUS_MESSAGE = 'Answer from the saved context in the message. This answer uses no tools.'
# The Hermes tools that a technical lane holds, as the service holds them for the worker (technical_lane.HELD_TOOLS),
# and the gateway's message tool to other platforms, which publishes outside this Mac.
LANE_TOOLS = ('message_agent', 'cronjob_manage', 'clarify', 'send_message')
LANE_MESSAGE = ('Independent technical work holds this tool while its decision is pending. Stay within the authorized '
                'local scope, and record what needs the person with coconut_kanban_comment on your own task.')


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


def guard_tool(tool_name='', task_id=None, **_):
    """pre_tool_call: block a tool call of a turn that a card ended, a card in a follow-up on a pending card, and the
    gateway's own board tools, which write to another board than Coconut's.

    The first tool call of a native outcome turn also sets its working folder: the workspace of its claim, as the
    worker sets TERMINAL_CWD. coconut_workspace later replaces it with the worktree of the work."""
    from .adapter import FOLDER_TASKS, FOLDERS, LANE_CHATS, MODES, PAUSED, WAITED
    holder, turn = _turn()
    if turn is None:
        # A turn of Hermes itself in the chat of a technical lane, for example the completion of a background process of
        # the lane, keeps the lane holds. It has no Coconut turn, so it has no Coconut board tools either.
        from gateway.session_context import get_session_env
        if (get_session_env('HERMES_SESSION_PLATFORM', '') == NAME
                and get_session_env('HERMES_SESSION_CHAT_ID', '') in LANE_CHATS
                and (tool_name in LANE_TOOLS or tool_name.startswith('kanban_'))):
            return {'action': 'block', 'message': LANE_MESSAGE}
        return None
    if turn in FOLDERS and task_id and task_id not in FOLDER_TASKS.setdefault(turn, set()):
        FOLDER_TASKS[turn].add(task_id)
        try:
            from tools.terminal_tool import register_task_env_overrides
            register_task_env_overrides(task_id, {'cwd': FOLDERS[turn]})
        except Exception as error:
            LOG.warning('Coconut: the turn did not get the folder of its claim (%s).', type(error).__name__)
    if turn in PAUSED:
        return {'action': 'block', 'message': PAUSED_MESSAGE}
    if turn in WAITED:
        return {'action': 'block', 'message': WAITED_MESSAGE}
    if MODES.get(turn) == 'status':
        # A status question is answered from its saved context, without tools, as the worker answers it.
        return {'action': 'block', 'message': STATUS_MESSAGE}
    if MODES.get(turn) == 'technical' and tool_name in LANE_TOOLS:
        return {'action': 'block', 'message': LANE_MESSAGE}
    if tool_name.startswith('kanban_'):
        # The board tools of the gateway write to its own board, not to Coconut's, where the work of the bots is
        # (gateway_chat.gateway_kanban_tool).
        return {'action': 'block', 'message': AUDIT_BOARD_MESSAGE if MODES.get(turn) == 'audit' else BOARD_MESSAGE}
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


CONTEXT_HEADER = ('Context of this turn from Coconut. It is current for this turn only, and Coconut gives it again with each '
                  'turn:\n')


def add_turn_context(request=None, **_):
    """llm_request middleware: give a model call of a Coconut turn the whole turn text, and only the tools it can use.

    Hermes saves the user message of the turn, which is the turn's own text (the person's message, or a title). The
    notes, the rules and the live state of the turn go to the turn's own message in each request only, so the session
    does not keep them, and later calls do not send each earlier one again. That message stays the same through the
    turn, so the provider's prompt cache holds the context after the first call of the turn (turn_message).

    Only an automatic check of a card (mode `refresh`) can call coconut_refresh_decision (guard_tool), so the other
    turns do not get its schema. Hermes registers a plugin tool for every turn. Return None for any other request.
    """
    from .adapter import CONTEXTS, MODES
    holder, turn = _turn()
    if turn is None or not isinstance(request, dict):
        return None
    changes, reasons = {}, []
    tools = request.get('tools')
    if isinstance(tools, list) and MODES.get(turn) != 'refresh':
        kept = [tool for tool in tools if _tool_name(tool) != REFRESH_TOOL]
        if len(kept) != len(tools):
            changes['tools'], reasons = kept, ['tools of the turn']
    context = CONTEXTS.get(turn)
    key = next((name for name in ('messages', 'input') if isinstance(request.get(name), list)), None)
    items = list(request[key]) if context and key else []
    index = next((i for i in range(len(items) - 1, -1, -1) if turn_message(items[i])), None)
    if index is not None:
        item = dict(items[index])
        note = CONTEXT_HEADER + context
        content = item.get('content')
        if isinstance(content, str):
            item['content'] = content + '\n\n' + note
        elif isinstance(content, list):
            # The Responses API names its text parts input_text, and the chat formats name them text.
            item['content'] = content + [{'type': 'input_text' if key == 'input' else 'text', 'text': note}]
        else:
            item = None
        if item is not None:
            items[index] = item
            changes[key] = items
            reasons.insert(0, 'turn context')
    if not changes:
        return None
    return {'request': dict(request, **changes), 'source': 'coconut', 'reason': ' and '.join(reasons)}


def turn_message(item):
    """Whether a request item is a message of the person or of Coconut, not a tool result.

    The Anthropic format sends tool results as user messages of tool_result blocks. The context must not go on them: it
    would move to the newest result on each call, and the prompt cache would miss from the earlier result on."""
    if not isinstance(item, dict) or item.get('role') != 'user':
        return False
    content = item.get('content')
    return not (isinstance(content, list) and content
                and all(isinstance(part, dict) and part.get('type') == 'tool_result' for part in content))


def _tool_name(tool):
    """The name of a tool in a model request: the Anthropic and Responses formats name it, and the chat format names
    its function."""
    if not isinstance(tool, dict):
        return None
    function = tool.get('function')
    return function.get('name') if isinstance(function, dict) else tool.get('name')


def compression_threshold(context_length):
    """The prompt size in tokens at which Hermes compresses a session, from the `compression` settings of this Hermes
    home, or None when compression is off. The same rule as Hermes's compressor: the share `threshold` of the window
    (at least 75% for a window under 512K), at most `threshold_tokens`. A threshold for one model (`model_thresholds`)
    does not count here."""
    try:
        from agent.context_compressor import ContextCompressor
        from hermes_cli.config import cfg_get, load_config_readonly
        config = load_config_readonly()
        if cfg_get(config, 'compression', 'enabled', default=True) is False:
            return None
        # Hermes raises the share for a small window.
        share = ContextCompressor._effective_threshold_percent(
            int(context_length), float(cfg_get(config, 'compression', 'threshold', default=0.5)))
        threshold = ContextCompressor._compute_threshold_tokens(int(context_length), share, None)
        cap = cfg_get(config, 'compression', 'threshold_tokens', default=None)
        return min(threshold, int(cap)) if cap else threshold
    except Exception:
        LOG.exception('Coconut: the compression threshold of Hermes is not known.')
        return None


def note_prompt_size(platform='', usage=None, context_length=None, **_):
    """post_api_request: mark a Coconut chat whose model call reached the compression threshold of Hermes. The adapter
    compresses the chat's session after the turn (adapter.CoconutAdapter._compress).

    Hermes compresses a session before a model call only when it knows the size of the call from the real usage of an
    earlier call. A gateway turn of one model call starts without that size, so Hermes never compresses it
    (docs/architecture.md)."""
    if platform != NAME or not isinstance(usage, dict) or not context_length:
        return None
    from gateway.session_context import get_session_env
    from .adapter import COMPRESS, turn_for_chat
    chat = get_session_env('HERMES_SESSION_CHAT_ID', '')
    if not chat or turn_for_chat(chat)[1] is None:
        return None
    prompt = int(usage.get('prompt_tokens') or 0)
    threshold = compression_threshold(context_length)
    if threshold and prompt >= threshold:
        LOG.info('Coconut: chat %s sent %s prompt tokens, at or above %s. Its session is compressed after the turn.',
                 chat, prompt, threshold)
        COMPRESS.add(chat)
    return None


def follow_new_session(old_session_id=None, new_session_id=None, platform='', **_):
    """on_session_reset: after /new in a Coconut chat, the conversation of the bot continues in the new session.

    join_conversation joins each turn to the session that the tip of the conversation's root names (TIP_KEY). Without
    this hook, the next turn would join the old session again and undo /new."""
    if platform != NAME or not old_session_id or not new_session_id:
        return None
    from .adapter import ADAPTERS
    roots = {root for adapter in list(ADAPTERS) for root in adapter._roots.values()}
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB
        db = SessionDB(db_path=Path(get_hermes_home()) / 'state.db')
        try:
            old = db.resolve_resume_session_id(old_session_id)
            for root in sorted(roots):
                # As in join_conversation: without a recorded tip, the conversation is its root session.
                tip = db.get_meta(TIP_KEY + root) or root
                if db.resolve_resume_session_id(tip) == old:
                    db.set_meta(TIP_KEY + root, new_session_id)
        finally:
            db.close()
    except Exception:
        LOG.exception('Coconut: the conversation did not follow /new to the new session.')
    return None


def join_conversation(event=None, session_store=None, gateway=None, **_):
    source = getattr(event, 'source', None)
    if source is None or getattr(source.platform, 'value', source.platform) != NAME:
        return None
    if session_busy(event, gateway):
        # Hermes would steer the running turn with this message, or redirect it, and end the Coconut turn at once
        # without running it. The turn waits instead (adapter.BUSY).
        from .adapter import BUSY
        BUSY.add(str(event.message_id))
        return {'action': 'skip', 'reason': 'coconut: the session of this chat runs another turn'}
    from .adapter import RECORDS
    records = RECORDS.pop(str(event.message_id), None)
    session = _join_session(source, session_store)
    if records is None:
        return None
    return add_records(event, records, session)


def _join_session(source, session_store):
    """Point the gateway session of the chat at the bot's conversation. Return (the state database, the ID of the
    session that the turn runs in), or None when the turn runs in the gateway's own session of the chat."""
    if session_store is None:
        return None
    from .adapter import session_root_for_chat
    root = session_root_for_chat(source.chat_id)
    if not root:
        return None
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB
        path = Path(get_hermes_home()) / 'state.db'
        db = SessionDB(db_path=path)
        try:
            tip = db.get_meta(TIP_KEY + root)
            entry = session_store.get_or_create_session(source)
            if tip and tip == entry.session_id:
                # The chat's session is the tip. After /new, Hermes may not have saved that session yet
                # (follow_new_session). A compression continues the conversation in a newer session of its lineage.
                return path, db.resolve_resume_session_id(entry.session_id)
            if not (tip and db.get_session(tip)):
                tip = root if db.get_session(root) else None
            if tip is None:
                if db.get_session(entry.session_id) and db.get_resume_conversations(entry.session_id)[0]:
                    # The chat has a session of an earlier conversation, for example before a restart of the bots
                    # gave the chat a new root. The new conversation starts in a new session, as /new does.
                    entry = session_store.get_or_create_session(source, force_new=True)
                db.set_meta(TIP_KEY + root, entry.session_id)
                return path, entry.session_id
            target = db.resolve_resume_session_id(tip)
            if db.resolve_resume_session_id(entry.session_id) != target:
                session_store.switch_session(entry.session_key, target)
            return path, target
        finally:
            db.close()
    except Exception:
        # The turn still runs, in the gateway's own session of the chat.
        LOG.exception('Coconut: the gateway session did not join the conversation of the bot.')
    return None


# The state_meta key of Hermes that holds the IDs of the records that a session holds (add_records).
RECORDS_KEY = 'coconut.records.'
# The IDs that a session keeps on record, newest last.
RECORDS_KEPT = 5000
# The records that a running turn adds to its session: (state database, session ID, record IDs).
PENDING_RECORDS: dict[str, tuple] = {}


def records_text(records):
    """backend/coconut/native_conversation.records_text of this Coconut install."""
    from .tools import backend
    return backend('native_conversation').records_text(records)


def add_records(event, records, session):
    """The rewrite of the turn's own text with the records that its session does not hold yet.

    Hermes saves the turn's own text in the session, so the session holds each record that a turn added. A session
    is new after /new, a reset of Hermes, or a compression, which summarizes the earlier turns into a new session.
    A new session holds no record, so its first turn gets every record. Without a session of the bot's conversation
    (`session` None) the turn gets every record too. The IDs count as held when the turn ends (commit_records)."""
    held = set()
    if session is not None:
        try:
            from hermes_state import SessionDB
            db = SessionDB(db_path=session[0])
            try:
                held = set(json.loads(db.get_meta(RECORDS_KEY + session[1]) or '[]'))
            finally:
                db.close()
        except Exception:
            LOG.exception('Coconut: the records of a session could not be read. The turn gets every record.')
            held = set()
    new = [record for record in records if record['id'] not in held]
    if session is not None and new:
        PENDING_RECORDS[str(event.message_id)] = (session[0], session[1], [record['id'] for record in new])
    if not new:
        return None
    return {'action': 'rewrite', 'text': (event.text or '') + records_text(new)}


def commit_records(turn, held=True):
    """Note the records that a turn added to its session as held, when the session saved the turn's text."""
    pending = PENDING_RECORDS.pop(str(turn), None)
    if pending is None or not held:
        return
    path, session, identities = pending
    try:
        from hermes_state import SessionDB
        db = SessionDB(db_path=path)
        try:
            kept = [identity for identity in json.loads(db.get_meta(RECORDS_KEY + session) or '[]')
                    if identity not in identities]
            db.set_meta(RECORDS_KEY + session, json.dumps((kept + identities)[-RECORDS_KEPT:]))
        finally:
            db.close()
    except Exception:
        # The next turn of the session gets these records again.
        LOG.exception('Coconut: the records of a session could not be saved.')


def session_busy(event, gateway) -> bool:
    """Whether the gateway already runs a turn in the session of this Coconut turn.

    Only a turn that the service gave counts (`CoconutAdapter._active`), not a command such as /stop or /compress.
    The runner's session check has no public signal. The Coconut contract check requires it
    (hermes_contract.verify_gateway_contract)."""
    from .adapter import ADAPTERS
    if getattr(getattr(event, 'message_type', None), 'value', None) == 'command' or gateway is None:
        return False
    if not any(str(event.message_id) in adapter._active for adapter in list(ADAPTERS)):
        return False
    try:
        return bool(gateway._is_session_running(gateway._session_key_for_source(event.source)))
    except Exception:
        LOG.exception('Coconut: the gateway did not say whether the session of a chat runs a turn.')
        return False


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


def cron_interim(text='', **_):
    from . import cron
    run = cron.current()
    if run is not None and str(text).strip():
        run.send(text, interim=True)


def cron_answer(assistant_response='', **_):
    from . import cron
    run = cron.current()
    if run is not None and str(assistant_response or '').strip():
        run.send(assistant_response)


def cron_end(completed=False, failed=False, interrupted=False, **_):
    from . import cron
    cron.finish('cancelled' if interrupted else 'success' if completed and not failed else 'failure')

