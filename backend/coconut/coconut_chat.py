"""The Coconut chat: the one place where a person changes their Coconut (docs: "Customize Coconut from its own chat").

Coconut has no settings screens. The Coconut project (bot_profiles.PROJECTS) carries the mark `coconut_chat`
(bot_profiles._ensure_projects): it is the Coconut chat. It shows as "Coconut",
it stays at the top of the sidebar and of the Agents list, and nobody archives or deletes it. It has one tool,
`coconut_settings`, that reads every setting and changes one, through the same routes that the app used before
(Service.mutate), so each rule and each check of a change stays the same.

Where Coconut runs (`mode`) is local today. Cloud, the hosted upstream Coconut, is not built yet, so the tool refuses
it with a clear line.
"""
from __future__ import annotations

from uuid import uuid4

NAME = 'Coconut'
# The name that a default profile got before the Coconut chat. The chat shows NAME in its place.
OLD_NAMES = ('Hermes', '')
MODES = ('local', 'cloud')
CLOUD_SOON = ('Cloud, the hosted upstream Coconut, is coming soon. This Coconut runs locally on this Mac, and its '
              'agents, pages and history stay here.')
# The limits of parallel work have no screen. The chat changes them through the routes of the service.
LIMIT_ROUTES = {'work_sessions_per_agent': '/v1/settings/agent-limit', 'helpers_per_task': '/v1/settings/parallel-work'}
ACCESS = ('off', 'smart', 'manual')
ACCESS_TEXT = {'off': 'runs commands without asking', 'smart': 'asks for risky commands',
               'manual': 'asks before each command'}

SETTINGS_SCHEMA = {
    'name': 'coconut_settings',
    'description': (
        'Read or change the settings of this Coconut. Only a turn of Coconut has it: the Coconut chat, and the change '
        'that another agent hands to it with coconut_message_bot. Coconut has no settings screens, '
        'so the person changes everything here, in plain words: "use Opus", "let chats run commands '
        'without asking", "mute Desk", "let each agent run 4 work sessions", "fall back to Sonnet", "archive chats after a week", "run Gumroad on mote". When the person sends "/help", call "read" '
        'and list in short lines what they can ask you to change, with the current values. First call with action "read" to see the current values and the choices. '
        'Then call action "change" with one setting. Say in one line what changed, from what to what, and that '
        'the person can ask to undo it. A change that deletes something or costs money needs a card first '
        '(coconut_ask). Mode "cloud" is not available yet: say that Cloud is coming soon.'),
    'parameters': {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['read', 'change']},
            'setting': {'type': 'string', 'enum': [
                'default_model', 'fallback_models', 'default_access', 'agent_notifications', 'agent_name', 'agent_runtime',
                'work_sessions_per_agent', 'helpers_per_task', 'auto_archive_hours', 'mode']},
            'agent': {'type': 'string', 'description': 'The name or ID of the project, for an agent_* setting.'},
            'model': {'type': 'string', 'description': 'A model ID from the catalog that "read" lists.'},
            'provider': {'type': 'string', 'description': 'The provider of that model, from the catalog.'},
            'reasoning_effort': {'type': 'string'},
            'value': {'description': 'The new value: an access mode (off, smart, manual), true or false for '
                                     'notifications, a name, hermes or mote for agent_runtime (the agent that runs '
                                     'the chats of that project), local or cloud for mode, a number from 1 to 64 (null for '
                                     'Automatic) for work_sessions_per_agent and helpers_per_task, the hours '
                                     'without activity after which a chat archives itself for auto_archive_hours '
                                     '(0 for never), or for '
                                     'fallback_models the whole list in order, each item with a model and a provider '
                                     'from the catalog (an empty list for no fallback).'},
        },
        'required': ['action'],
    },
}


def is_coconut(service, bot_id) -> bool:
    """True for the Coconut chat, the bot with the mark `coconut_chat`, and for a further chat of it."""
    try:
        bot = service._get('bots', bot_id)
        return bool(bot.get('coconut_chat') or (bot.get('chat_of') and service._get('bots', bot['chat_of']).get('coconut_chat')))
    except Exception:
        return False


def view(service, bot: dict, result: dict) -> dict:
    """The fields of the Coconut chat in a bot view: `coconut` true, and the name Coconut in place of the old name."""
    if is_coconut(service, bot['id']) and not bot.get('chat_of'):
        result['coconut'] = True
        if result.get('name') in OLD_NAMES:
            result['name'] = NAME
        result['mode'] = 'local'
    return result


def refuse_removal(service, bot_id, data: dict | None = None) -> None:
    """The Coconut chat stays: no archive and no delete."""
    from .service_base import ServiceError
    if not is_coconut(service, bot_id):
        return
    with service.lock:
        # A further chat of the Coconut agent can go. The Coconut chat itself stays.
        extra = bool(service._get('bots', bot_id).get('chat_of'))
    if not extra and (data is None or data.get('archived') is True):
        raise ServiceError(409, 'The Coconut chat stays: it is where you change Coconut.')


def _agent(service, value):
    from .service_base import ServiceError
    text = str(value or '').strip()
    # A setting belongs to the agent, not to one of its chats.
    bots = [bot for bot in service._all('bots') if not bot.get('_deleted') and not bot.get('chat_of')]
    match = next((bot for bot in bots if bot['id'] == text), None) or next(
        (bot for bot in bots if bot['name'].casefold() == text.casefold()), None)
    if match is None and text.casefold() == NAME.casefold():
        match = next((bot for bot in bots if is_coconut(service, bot['id'])), None)
    if match is None:
        names = ', '.join(sorted(view(service, bot, dict(bot))['name'] for bot in bots if not bot.get('archived')))
        raise ServiceError(404, f'No agent is called {text!r}. The agents are: {names}.')
    return match


def read(service) -> dict:
    """Every setting that the chat can change, with its current value and its choices."""
    catalog = service._model_catalog() or {}
    default = catalog.get('default') or {}
    models = [{'model': row.get('id'), 'provider': row.get('provider'), 'name': row.get('name') or row.get('id')}
              for row in catalog.get('models') or []]
    try:
        approvals = service.reconcile_approval_updates()
    except Exception:
        approvals = {}
    agents = []
    with service.lock:
        bots = [bot for bot in service._all('bots') if not bot.get('_deleted') and not bot.get('chat_of')]
    for bot in bots:
        shown = view(service, bot, dict(bot))
        agents.append({'id': bot['id'], 'name': shown['name'], 'coconut': bool(shown.get('coconut')),
                       'archived': bool(bot.get('archived')), 'notifications': bot.get('notifications', True),
                       'runtime': _runtime(service, bot)})
    return {'mode': 'local', 'modes': {'local': 'available', 'cloud': 'coming soon'},
            'default_model': {key: default.get(key) for key in ('model', 'provider', 'reasoning_effort')},
            'fallback_models': _fallback(service), 'models': models, 'default_access': approvals.get('defaults'),
            'access_modes': {mode: ACCESS_TEXT[mode] for mode in ACCESS}, 'agents': agents,
            'runtimes': _runtimes(),
            'parallel_work': {**_limits(service), 'total': 'No limit: every agent works at once.'},
            'auto_archive_hours': service.auto_archive_hours(),
            'auto_archive': 'A chat without activity for this many hours archives itself. 0 means never.',
            'not_here': ('Pairing a device, phone notifications and their work hours, and signing in to GitHub or '
                         'Google need the device itself: the header of this chat has them.')}


def _fallback(service):
    """The models that Hermes tries, in order, when the default model fails. None when Hermes does not say."""
    try:
        return service.provider.native_settings()['fallback']['models']
    except Exception:
        return None


def _limits(service) -> dict:
    """The limits of parallel work: the work sessions of each agent, and the helpers of each task."""
    result = {}
    try:
        result['work_sessions_per_agent'] = service._work_limits()['per_agent']
    except Exception:
        pass
    try:
        result['helpers_per_task'] = service.provider.native_settings()['parallel_work']
    except Exception:
        pass
    return result


def change(service, arguments: dict) -> dict:
    """Change one setting through the route that the app used, and answer what it was and what it is now."""
    from .service_base import ServiceError
    setting = arguments.get('setting')
    key = str(uuid4())
    device = 'coconut-chat'
    if setting == 'mode':
        if arguments.get('value') == 'cloud':
            raise ServiceError(409, CLOUD_SOON)
        if arguments.get('value') != 'local':
            raise ServiceError(400, 'Choose local or cloud.')
        return {'setting': 'mode', 'before': 'local', 'after': 'local', 'changed': False}
    if setting == 'default_model':
        before = read(service)['default_model']
        selection = {name: arguments.get(name) for name in ('model', 'provider', 'reasoning_effort')
                     if arguments.get(name) is not None}
        service.mutate('PUT', '/v1/settings/model', selection, key, device)
        return {'setting': setting, 'before': before, 'after': selection, 'changed': True}
    if setting == 'fallback_models':
        before = _fallback(service)
        after = service.mutate('PUT', '/v1/settings/fallback', {'models': arguments.get('value')}, key, device)
        return {'setting': setting, 'before': before, 'after': after['models'], 'changed': after['models'] != before}
    if setting == 'default_access':
        if arguments.get('value') not in ACCESS:
            raise ServiceError(400, 'Choose off, smart or manual.')
        before = read(service)['default_access']
        service.mutate('PUT', '/v1/settings/approvals', {'mode': arguments['value']}, key, device)
        return {'setting': setting, 'before': before, 'after': arguments['value'], 'changed': True}
    if setting == 'auto_archive_hours':
        before = service.auto_archive_hours()
        after = service.mutate('PUT', '/v1/settings/auto-archive', {'hours': arguments.get('value')}, key, device)
        return {'setting': setting, 'before': before, 'after': after['auto_archive_hours'],
                'changed': after['auto_archive_hours'] != before}
    if setting in LIMIT_ROUTES:
        value = arguments.get('value')
        before = _limits(service).get(setting)
        service.mutate('PUT', LIMIT_ROUTES[setting], {'limit': value}, key, device)
        return {'setting': setting, 'before': before, 'after': _limits(service).get(setting), 'changed': True}
    if setting in ('agent_model', 'agent_access'):
        from .bot_profiles import SHARED_SETTINGS
        raise ServiceError(409, SHARED_SETTINGS)
    if setting in ('agent_notifications', 'agent_name'):
        with service.lock:
            bot = _agent(service, arguments.get('agent'))
        if setting == 'agent_notifications':
            if not isinstance(arguments.get('value'), bool):
                raise ServiceError(400, 'Choose true or false.')
            data, before = {'notifications': arguments['value']}, bot.get('notifications', True)
        else:
            name = str(arguments.get('value') or '').strip()
            if not name or len(name) > 80:
                raise ServiceError(400, 'Choose a name of 1 to 80 characters.')
            data, before = {'name': name}, bot['name']
        service.mutate('PATCH', f'/v1/bots/{bot["id"]}', data, key, device)
        return {'setting': setting, 'agent': view(service, bot, dict(bot))['name'], 'before': before,
                'after': next(iter(data.values())) or 'default', 'changed': True}
    if setting == 'agent_runtime':
        from . import mote_runtime
        with service.lock:
            bot = _agent(service, arguments.get('agent'))
        home = service.profiles.home(bot)
        before = mote_runtime.runtime(home)
        after = mote_runtime.switch(home, arguments.get('value'))
        return {'setting': setting, 'agent': view(service, bot, dict(bot))['name'], 'before': before, 'after': after,
                'changed': after != before, 'note': mote_runtime.SWITCH_NOTE}
    raise ServiceError(400, 'Choose a setting that "read" lists.')


def _runtime(service, bot) -> str:
    """hermes or mote: the agent that runs the chats of a project (mote_runtime.py)."""
    from .mote_runtime import runtime
    try:
        return runtime(service.profiles.home(bot))
    except Exception:
        return 'hermes'


def _runtimes() -> dict:
    from . import mote_runtime
    text = dict(mote_runtime.RUNTIME_TEXT)
    if mote_runtime.command() is None:
        text['mote'] += ' ' + mote_runtime.MISSING
    return text


def settings_tool(service, task_id: str, arguments: dict) -> dict:
    """coconut_settings of a gateway turn. Only a turn of the Coconut agent changes Coconut."""
    from .service_base import ServiceError
    with service.lock:
        task = service._get('tasks', task_id)
    # The privilege belongs to the Coconut AGENT, not to the chat the turn shows in. Another agent hands a change to
    # the Coconut chat with coconut_message_bot, and that turn runs as Coconut in a work session whose conversation is
    # the requester's (`_store_bot_message` keeps conversation_bot_id at the origin). Reading only the conversation
    # refused every delegated change with 403, after the requester had promised the person to report back.
    chat = task.get('conversation_bot_id') or task['bot_id']
    if not (is_coconut(service, task['bot_id']) or is_coconut(service, chat)):
        raise ServiceError(403, 'Only the Coconut chat changes the settings of Coconut. Ask the person to open it.')
    if arguments.get('action') == 'read':
        return read(service)
    if arguments.get('action') == 'change':
        return change(service, arguments)
    raise ServiceError(400, 'Choose the action read or change.')
