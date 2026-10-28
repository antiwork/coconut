"""The `coconut` toolset of the gateway: Coconut tools that a chat turn of the gateway needs.

A tool finds its chat from the session context of the gateway (`gateway.session_context.get_session_env`), not from
gateway internals. The chat ID is the Coconut bot ID, and the adapter of this process knows the running turn of that
chat. The service then acts for the Coconut task of that turn (backend/coconut/gateway_chat.py, gateway_tool).

Hermes enables a plugin toolset on each platform by default and lists plugin tools behind its tool search. A check_fn
that reads the session context hid the tools also in Coconut chats (the native check of a real gateway showed it), so
the tools show on each platform. Each tool refuses to act outside a Coconut chat turn.
"""
from __future__ import annotations

import json

TOOLSET = 'coconut'

ASK = {'name': 'coconut_ask', 'description': (
    'Show a decision card in Coconut and wait for the answer of the person. Use it for a choice, a permission, a '
    'confirmation or a missing fact. The person can choose an option or type a reply. Returns the answer as text.'),
    'parameters': {'type': 'object', 'properties': {
        'question': {'type': 'string', 'maxLength': 300, 'description': 'The question, in one sentence.'},
        'choices': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'maxLength': 120},
                    'description': 'Up to eight short choices. Omit for a free answer.'},
        'detail': {'type': 'string', 'maxLength': 2000, 'description': 'The facts that the person needs to decide.'}},
        'required': ['question']}}
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


def ask(arguments, **_):
    adapter, turn = _turn()
    if turn is None:
        return _result({'error': 'coconut_ask works only during a Coconut chat turn.'})
    choices = [str(choice) for choice in (arguments.get('choices') or [])][:8]
    answer = adapter.ask(turn, str(arguments.get('question') or ''), choices, str(arguments.get('detail') or ''))
    if answer is None:
        return _result({'status': 'no_answer', 'detail': 'The person did not answer. Continue without the answer.'})
    return _result({'answer': answer})


def _service_tool(name):
    def handler(arguments, **_):
        adapter, turn = _turn()
        if turn is None:
            return _result({'error': f'{name} works only during a Coconut chat turn.'})
        import urllib.error
        from .adapter import REQUEST_SECONDS
        try:
            return _result(adapter._bridge.request('/v1/gateway/tools', {
                'turn_id': turn, 'tool': name, 'arguments': dict(arguments or {})}, REQUEST_SECONDS * 6))
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
    for schema, handler in ((ASK, ask), (TRACK_PR, _service_tool('track_pr')),
                            (MESSAGE_BOT, _service_tool('message_bot'))):
        ctx.register_tool(name=schema['name'], toolset=TOOLSET, schema=schema, handler=handler, emoji='🥥')
