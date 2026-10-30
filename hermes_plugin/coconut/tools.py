"""The `coconut` toolset of the gateway: Coconut tools that a chat turn of the gateway needs.

A tool finds its chat from the session context of the gateway (`gateway.session_context.get_session_env`), not from
gateway internals. The chat ID is the Coconut bot ID, and the adapter of this process knows the running turn of that
chat. The service then acts for the Coconut task of that turn (backend/coconut/gateway_chat.py, gateway_tool).

Hermes enables a plugin toolset on each platform by default and lists plugin tools behind its tool search. A check_fn
that reads the session context hid the tools also in Coconut chats (the native check of a real gateway showed it), so
the tools show on each platform. Each tool refuses to act outside a Coconut chat turn.

Cards. coconut_ask, coconut_suggest and coconut_resolve_reply have the schemas of the worker's tools
(backend/coconut/card_tools.py, loaded by its path). A card of coconut_ask ends the turn, as in the worker: the service
keeps the task waiting for the answer, and the pre_tool_call hook blocks each further tool call of the turn
(hooks.guard_tool). The answer continues the work in a later turn.

Card checks. coconut_refresh_decision records the result of an automatic check of a pending card. Only a check turn
(mode `refresh`) can call it, and a check turn can call no other Coconut tool (hooks.guard_tool).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

TOOLSET = 'coconut'
_CARDS = None


def cards():
    """backend/coconut/card_tools.py of this Coconut install. It imports nothing, so the gateway can load it."""
    global _CARDS
    if _CARDS is None:
        path = Path(__file__).resolve().parents[2] / 'backend' / 'coconut' / 'card_tools.py'
        spec = importlib.util.spec_from_file_location('coconut_card_tools', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CARDS = module
    return _CARDS


def reply_schema():
    """coconut_resolve_reply: a typed answer from chat names its card and message. A follow-up on one card does not."""
    module = cards()
    chat = module.CHAT_REPLY_SCHEMA['parameters']
    return dict(module.REPLY_SCHEMA, parameters=dict(chat, required=module.REPLY_SCHEMA['parameters']['required']))

TRACK_PR = {'name': 'coconut_track_pr', 'description': (
    'Track an existing GitHub pull request and its checks in Coconut Work. Call it when you create a PR or begin work '
    'on one, or when the person asks to follow one. Coconut reads GitHub and resumes the bot when results change. This '
    'does not create, edit, push or merge a PR.'),
    'parameters': {'type': 'object', 'properties': {
        'url': {'type': 'string', 'description': 'The exact https://github.com/owner/repo/pull/number URL.'}},
        'required': ['url']}}
MESSAGE_BOT = {'name': 'coconut_message_bot', 'description': (
    'Send a message to another Coconut bot. The bot works on it in its own chat and replies to this chat. Use it to '
    'hand over work or to ask another bot.'),
    'parameters': {'type': 'object', 'properties': {
        'bot': {'type': 'string', 'description': 'The name or the ID of the other bot.'},
        'text': {'type': 'string', 'maxLength': 20000, 'description': 'The message.'}},
        'required': ['bot', 'text']}}


def _turn():
    """The holder (an adapter or a cron run) and the Coconut turn of this tool call: a chat turn or a cron run."""
    from .hooks import _turn as turn
    return turn()


def _result(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _service_tool(name):
    def handler(arguments, **_):
        adapter, turn = _turn()
        if turn is None:
            return _result({'error': f'{name} works only during a Coconut chat turn.'})
        import urllib.error
        from .adapter import PAUSED, REQUEST_SECONDS
        try:
            result = adapter._bridge.request('/v1/gateway/tools', {
                'turn_id': turn, 'tool': name, 'arguments': dict(arguments or {})}, REQUEST_SECONDS * 6)
            if result.get('paused'):
                # The card ends the turn. The hook blocks each further tool call (hooks.guard_tool).
                PAUSED.add(turn)
            return _result(result)
        except urllib.error.HTTPError as error:
            # The service refused the request, for example an unknown bot. Its reason helps the model.
            try:
                return _result({'error': json.loads(error.read() or b'{}').get('error', str(error))})
            except ValueError:
                return _result({'error': str(error)})
        except Exception as error:
            return _result({'error': f'The Coconut service did not answer ({type(error).__name__}).'})
    return handler


def register(ctx):
    module = cards()
    for schema, handler in ((module.QUESTION_SCHEMA, _service_tool('ask')),
                            (module.SUGGEST_SCHEMA, _service_tool('suggest')),
                            (reply_schema(), _service_tool('resolve_reply')),
                            (module.refresh_schema(), _service_tool('refresh_decision')),
                            (module.GATEWAY_WAIT_SCHEMA, _service_tool('wait')),
                            (module.CORRECTION_SCHEMA, _service_tool('correct_withdrawal')),
                            (module.BOT_CREATION_SCHEMA, _service_tool('create_bot')),
                            (module.BOT_DELETION_SCHEMA, _service_tool('delete_bot')),
                            (TRACK_PR, _service_tool('track_pr')), (MESSAGE_BOT, _service_tool('message_bot'))):
        ctx.register_tool(name=schema['name'], toolset=TOOLSET, schema=schema, handler=handler, emoji='🥥')
