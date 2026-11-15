"""The guard of the direct chats in the mode `team`: the text of a direct chat of a person with an agent stays there.

Hermes gives an agent one session store, one memory and one set of skills for the whole profile. A direct chat is a
session whose row in `sessions` of state.db has the source `coconut` and a chat ID
`<agent>:team:<room>:direct:<member>` (backend/coconut/team_turns.py, chat_id). `guard` is a `tool_execution`
middleware. It acts on each tool call of the profile, also on a turn outside Coconut (the command line, cron, another
platform), and such a turn is another chat:

- `session_search`: a read or a scroll of a session of a direct chat works only in a turn of that same chat. Discover
  and browse return no session of a direct chat of another chat. The argument `profile` and each argument that the
  guard does not know are refused.
- `memory` and `skill_manage`: refused only in a turn that the guard knows as a turn of a direct chat, because each
  chat reads the memory and the skills. The review that Hermes runs after a turn and a subagent of the turn have the
  session of that turn, so the same rule refuses their writes. Each other write runs: the agent learns from its cron
  jobs, its command line sessions and its curator, and those sessions are not chats.

Hermes runs the tool without a middleware that raises, and keeps the result that the middleware got. So the guard
catches each of its faults. A fault refuses a search. A write runs after a fault, unless the turn is a Coconut turn
whose chat the guard does not know.
"""
from contextlib import closing
import json
import logging
import sqlite3
import time

LOG = logging.getLogger(__name__)
KIND = 'tool_execution'
PLATFORM = 'coconut'
SEARCH = 'session_search'
# The arguments of session_search in its schema. Another argument can change what the tool reads.
ARGUMENTS = frozenset({'query', 'limit', 'sort', 'detail', 'after', 'before', 'exclude_session_ids', 'session_id',
                       'around_message_id', 'window', 'role_filter', 'profile'})
# The same text for each refused search, so a refusal does not tell that a session exists.
REFUSED = ('Coconut refused this search. A direct chat of a person with you is private: you can read it only in that '
           'chat. The argument profile and arguments that session_search does not have are not allowed.')
WRITES = {
    'memory': 'Coconut refused this memory write. The memory of this agent is shared with the team, so nothing of a '
              'direct chat goes into it.',
    'skill_manage': 'Coconut refused this skill change. The skills of this agent are shared with the team, so nothing '
                    'of a direct chat goes into them.'}
UNCHECKED = 'Coconut could not check the chat of this turn, so it refused this {}. Try it again later.'
WHAT = {'memory': 'memory write', 'skill_manage': 'skill change'}
# The wait before a second read of state.db for a write.
RETRY_SECONDS = 0.5
# The chat of a session that the guard cannot place: no turn can read it. A `coconut` session without a chat ID also
# writes no memory. A link `<profile>/<ID>` as `session_id` names no row, so it is such a session.
NOBODY = object()


def register(ctx) -> str | None:
    """Register the guard. Return KIND when this Hermes has no such middleware: without the guard, a turn of each
    chat can read each direct chat, so the team chat must take no message (team.TeamAdapter.lacking)."""
    try:
        from hermes_cli.middleware import VALID_MIDDLEWARE
    except ImportError:
        return KIND
    # Hermes keeps a kind that it does not know with a warning only, and never runs it.
    if KIND not in VALID_MIDDLEWARE or not callable(getattr(ctx, 'register_middleware', None)):
        return KIND
    ctx.register_middleware(KIND, guard)
    return None


def is_direct(chat) -> bool:
    """True for the chat ID of a direct chat, as team_turns.parse_chat reads it."""
    bot, team, rest = str(chat or '').partition(':team:')
    room, _, place = rest.partition(':')
    kind, _, member = place.partition(':')
    return bool(bot and team and room and kind == 'direct' and member)


def store():
    """A read-only connection to state.db of this profile."""
    from hermes_constants import get_hermes_home
    return sqlite3.connect(f'{(get_hermes_home() / "state.db").absolute().as_uri()}?mode=ro', uri=True, timeout=5)


def direct_chat(connection, session_id, unknown=NOBODY):
    """The chat ID of the direct chat of a session, None for a session of another chat, or NOBODY for a `coconut`
    session without a chat ID. A row outside Coconut without a chat ID (a subagent) has the chat of its parent. A
    session without a row, and a loop of parents, give `unknown`."""
    seen = set()
    while session_id not in seen:
        seen.add(session_id)
        row = connection.execute('SELECT source, chat_id, parent_session_id FROM sessions WHERE id = ?',
                                 (str(session_id),)).fetchone()
        if row is None:
            return unknown
        source, chat, parent = row
        if source == PLATFORM:
            return chat if is_direct(chat) else (None if chat else NOBODY)
        if chat or not parent:
            return None
        session_id = parent
    return unknown


def turn_chat() -> tuple[str, str]:
    """The platform and the chat ID of the gateway turn of this call, or empty texts. Hermes keeps them for the turn,
    for the review after it and for its subagents."""
    try:
        from gateway.session_context import get_session_env
        return (get_session_env('HERMES_SESSION_PLATFORM', '') or '',
                get_session_env('HERMES_SESSION_CHAT_ID', '') or '')
    except Exception:
        return '', ''


def stored_chat(session_id):
    """direct_chat for a write, with one more try after a fault of the store. A session without a row is no chat."""
    for attempt in range(2):
        try:
            with closing(store()) as connection:
                return direct_chat(connection, session_id, unknown=None)
        except Exception:
            if attempt:
                raise
            time.sleep(RETRY_SECONDS)


def allowed(chat, own) -> bool:
    return chat is None or (chat is not NOBODY and chat == own)


def refusal(text: str = REFUSED) -> str:
    return json.dumps({'success': False, 'error': text})


def blank(value) -> bool:
    return value is None or isinstance(value, str) and not value.strip()


def guard(tool_name='', args=None, next_call=None, session_id='', **_):
    """The `tool_execution` middleware. `session_id` is the session of the turn that calls the tool."""
    if tool_name in WRITES:
        return guard_write(tool_name, args, next_call, session_id)
    if tool_name != SEARCH:
        return next_call(args)
    try:
        arguments = dict(args or {})
        if set(arguments) - ARGUMENTS or not blank(arguments.get('profile')):
            return refusal()
        # A `session_id` that is not text raises here, and Hermes would browse with it.
        target = (arguments.get('session_id') or '').strip()
        with closing(store()) as connection:
            own = direct_chat(connection, session_id)
            if target and not allowed(direct_chat(connection, target), own):
                return refusal()
    except Exception:
        LOG.warning('Coconut: the guard of the direct chats failed, so it refused %s.', SEARCH, exc_info=True)
        return refusal()
    result = next_call(arguments)
    try:
        return checked(result, target, own)
    except Exception:
        LOG.warning('Coconut: the guard of the direct chats could not check a result of %s.', SEARCH, exc_info=True)
        return refusal()


def guard_write(tool_name, args, next_call, session_id):
    """Refuse a write only in a turn that the guard knows as a turn of a direct chat."""
    platform, chat = turn_chat()
    if platform == PLATFORM and is_direct(chat):
        return refusal(WRITES[tool_name])
    try:
        stored = stored_chat(session_id)
    except Exception:
        # Only a Coconut turn can be a direct chat. Each other session (cron, the command line, the curator) learns
        # also while state.db does not read.
        refused = platform == PLATFORM and not chat
        LOG.warning('Coconut: the guard of the direct chats could not read the chat of a %s call, so it %s.',
                    tool_name, 'refused the call' if refused else 'let the call run', exc_info=True)
        return refusal(UNCHECKED.format(WHAT[tool_name])) if refused else next_call(args)
    return next_call(args) if stored is None else refusal(WRITES[tool_name])


def checked(result, target: str, own) -> str:
    """The result of session_search without a session of a direct chat of another chat. A result of a form that the
    guard does not know raises."""
    answer = json.loads(result)
    if answer.get('success') is not True:
        if not isinstance(answer.get('error'), str):
            raise ValueError('An error without its text.')
        return refusal(answer['error'])
    mode = answer['mode']
    with closing(store()) as connection:
        if mode in ('read', 'scroll'):
            # Scroll can move to a child of the session in the same lineage: the result names the session it read.
            if not target or not allowed(direct_chat(connection, answer['session_id']), own):
                raise ValueError('A read of a session that the guard did not check.')
            return result
        if mode not in ('discover', 'browse') or target or not isinstance(answer['results'], list):
            raise ValueError(f'A result of the mode {mode!r}.')
        kept = [item for item in answer['results']
                if item.get('session_id') and isinstance(item['session_id'], str)
                and isinstance(item.get('parent_session_id', ''), str)
                and all(allowed(direct_chat(connection, item[key]), own)
                        for key in ('session_id', 'parent_session_id') if item.get(key))]
    hidden = len(answer['results']) - len(kept)
    # The answer again from the keys that the guard knows, so a new key of Hermes carries nothing past the guard.
    keys = ('mode', 'query', 'detail', 'message', 'link_hint', 'index_rebuild', 'sessions_searched')
    rebuilt = {'success': True, **{key: answer[key] for key in keys if key in answer}, 'results': kept,
               'count': len(kept)}
    if hidden and isinstance(answer.get('sessions_searched'), int):
        rebuilt['sessions_searched'] = max(0, answer['sessions_searched'] - hidden)
    if hidden and mode == 'browse':
        rebuilt['message'] = (f'Showing {len(kept)} most recent sessions. Pass a query= to search, or '
                              'session_id+around_message_id to scroll.')
    return json.dumps(rebuilt, ensure_ascii=False)
