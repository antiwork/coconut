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

Worktrees. coconut_workspace gives a turn of a work session the Git worktree of that session, and Hermes then runs the
terminal commands of the agent in it.

Board. Each Coconut turn has the board tools of a worker under a Coconut name (coconut_kanban_show for kanban_show, and
so on), on Coconut's board, where the work of the bots is. Hermes refuses a plugin tool with the name of its own tool, so
the names differ. Only a turn of a native outcome has the tools that act on the run of its card, for the Kanban claim of
the turn. The service runs each tool and records each write as a receipt of the turn. The gateway's own board tools
write to another board, so the hooks block them in each Coconut turn. The schemas of the tools that only a worker had
are the schemas of Hermes's tools, without the board.

Native outcomes. coconut_record_pause records a pause in a check of a blocked native task, and coconut_read_receipts
reads the saved receipts of the turn, as in the worker.

Card checks. coconut_refresh_decision records the result of an automatic check of a pending card. Only a check turn
(mode `refresh`) can call it, and a check turn can call no other Coconut tool (hooks.guard_tool).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

TOOLSET = 'coconut'
_CARDS = None
_WORKTREES = None
_MODULES = {}
# The board tools of a native outcome that Coconut names after Hermes's tools (claim_schemas).
CLAIM_TOOLS = ('kanban_complete', 'kanban_block', 'kanban_schedule', 'kanban_heartbeat', 'kanban_request_review',
               'kanban_link', 'kanban_unblock')
CLAIM_NOTE = ('In Coconut, this acts on the board of Coconut for the Kanban claim of this turn. ')


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
        'url': {'type': 'string', 'description': 'The exact https://github.com/owner/repo/pull/number URL.'},
        # As the worker's tool for a native outcome (hermes_worker): the PR then belongs to the outcome it delivers.
        'work_id': {'type': 'string', 'description': (
            'Native task ID or Work item ID of the outcome that this PR delivers. Supply it only when the PR delivers '
            'that outcome. Omit it for a separate PR.')}},
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


def worktrees():
    """backend/coconut/work_workspace.py of this Coconut install. It imports only the standard library."""
    global _WORKTREES
    if _WORKTREES is None:
        _WORKTREES = backend('work_workspace')
    return _WORKTREES


def backend(name):
    """A module of backend/coconut of this Coconut install that imports only the standard library at its top."""
    if name not in _MODULES:
        path = Path(__file__).resolve().parents[2] / 'backend' / 'coconut' / (name + '.py')
        spec = importlib.util.spec_from_file_location('coconut_' + name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[name] = module
    return _MODULES[name]


def claim_schemas():
    """The schemas of the board tools of a native outcome: the schemas of Hermes's tools, with a Coconut name and
    without the board, which Coconut chooses. A Hermes without these schemas gets none of these tools."""
    try:
        from tools import kanban_tools_schemas as hermes
    except Exception:
        return []
    schemas = []
    for name in CLAIM_TOOLS:
        schema = getattr(hermes, name.upper() + '_SCHEMA', None)
        if not isinstance(schema, dict):
            continue
        parameters = dict(schema.get('parameters') or {})
        parameters['properties'] = {key: value for key, value in (parameters.get('properties') or {}).items()
                                    if key != 'board'}
        parameters['required'] = [key for key in parameters.get('required') or [] if key != 'board']
        schemas.append(dict(schema, name='coconut_' + name, description=CLAIM_NOTE + schema.get('description', ''),
                            parameters=parameters))
    return schemas


def _workspace(arguments, task_id=None, **_):
    """coconut_workspace: the worktree of the work session of this turn, as the worker gets it.

    The service names the registry and the outcome of the work session, and refuses a turn without one. The plugin
    gets the worktree here, because work_workspace.acquire needs Hermes. Hermes then runs the terminal commands of this
    agent in it (tools.terminal_tool.register_task_env_overrides), as in the worker."""
    place = json.loads(_service_tool('workspace')({}))
    if place.get('error'):
        return _result(place)
    options = {key: arguments[key] for key in ('repository', 'existing_worktree')
               if isinstance((arguments or {}).get(key), str) and arguments[key]}
    if 'repository' not in options:
        return _result({'error': 'Supply the local repository path.'})
    proof = place.get('successor')
    if proof:
        # A native outcome can continue the retired worktree of an earlier outcome. The service names the claim and
        # the board, never the tool arguments (workspace_handoff.native_successor).
        options['successor'] = backend('workspace_handoff').native_successor(
            proof['database'], proof['claim'], proof['task_id'], proof['native_id'], proof['tenant'])
    try:
        binding = worktrees().acquire(place['root'], place['outcome'], **options)
    except (ValueError, OSError) as error:
        return _result({'error': str(error)})
    if task_id:
        try:
            from tools.terminal_tool import register_task_env_overrides
            register_task_env_overrides(task_id, {'cwd': binding['path']})
        except Exception as error:
            binding = dict(binding, warning=f'Use this path as cwd. Hermes did not set it ({type(error).__name__}).')
    return _result(binding)


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
                            (module.WORKSPACE_SCHEMA, _workspace),
                            (module.KANBAN_CREATE_SCHEMA, _service_tool('kanban_create')),
                            (module.KANBAN_COMMENT_SCHEMA, _service_tool('kanban_comment')),
                            (module.KANBAN_LIST_SCHEMA, _service_tool('kanban_list')),
                            (module.KANBAN_SHOW_SCHEMA, _service_tool('kanban_show')),
                            (TRACK_PR, _service_tool('track_pr')), (MESSAGE_BOT, _service_tool('message_bot')),
                            (backend('work_recovery').BLOCK_PAUSE_SCHEMA, _service_tool('record_pause')),
                            (backend('native_receipt_context').SCHEMA, _service_tool('read_receipts')),
                            *((schema, _service_tool(schema['name'][len('coconut_'):])) for schema in claim_schemas())):
        ctx.register_tool(name=schema['name'], toolset=TOOLSET, schema=schema, handler=handler, emoji='🥥')
