"""The Coconut chat: the one place where a person changes their Coconut (docs: "Customize Coconut from its own chat").

Coconut has no settings screens. The agent of the default Hermes profile is the Coconut chat. It shows as "Coconut",
it stays at the top of the sidebar and of the Chats list, and nobody archives or deletes it. It has one tool,
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
ACCESS = ('off', 'smart', 'manual')
ACCESS_TEXT = {'off': 'runs commands without asking', 'smart': 'asks for risky commands',
               'manual': 'asks before each command'}

SETTINGS_SCHEMA = {
    'name': 'coconut_settings',
    'description': (
        'Read or change the settings of this Coconut. Only the Coconut chat has it. Coconut has no settings screens, '
        'so the person changes everything here, in plain words: "use Opus for Scout", "let agents run commands '
        'without asking", "mute Desk". First call with action "read" to see the current values and the choices. '
        'Then call action "change" with one setting. Say in one line what changed, from what to what, and that '
        'the person can ask to undo it. A change that deletes something or costs money needs a card first '
        '(coconut_ask). Mode "cloud" is not available yet: say that Cloud is coming soon.'),
    'parameters': {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['read', 'change']},
            'setting': {'type': 'string', 'enum': [
                'default_model', 'agent_model', 'default_access', 'agent_access', 'agent_notifications',
                'agent_name', 'mode']},
            'agent': {'type': 'string', 'description': 'The name or ID of the agent, for an agent_* setting.'},
            'model': {'type': 'string', 'description': 'A model ID from the catalog that "read" lists.'},
            'provider': {'type': 'string', 'description': 'The provider of that model, from the catalog.'},
            'reasoning_effort': {'type': 'string'},
            'value': {'description': 'The new value: an access mode (off, smart, manual, or null for the default), '
                                     'true or false for notifications, a name, or local or cloud for mode.'},
        },
        'required': ['action'],
    },
}


def is_coconut(service, bot_id) -> bool:
    """True for the agent of the default Hermes profile: the Coconut chat."""
    from . import profile_registry
    row = (service.profiles.rows or {}).get(bot_id) if getattr(service, 'profiles', None) else None
    return bool(row) and row.get('name') == profile_registry.DEFAULT


def view(service, bot: dict, result: dict) -> dict:
    """The fields of the Coconut chat in a bot view: `coconut` true, and the name Coconut in place of the old name."""
    if is_coconut(service, bot['id']):
        result['coconut'] = True
        if result.get('name') in OLD_NAMES:
            result['name'] = NAME
        result['mode'] = 'local'
    return result


def refuse_removal(service, bot_id, data: dict | None = None) -> None:
    """The Coconut chat stays: no archive and no delete."""
    from .service_base import ServiceError
    if is_coconut(service, bot_id) and (data is None or data.get('archived') is True):
        raise ServiceError(409, 'The Coconut chat stays: it is where you change Coconut.')


def _agent(service, value):
    from .service_base import ServiceError
    text = str(value or '').strip()
    bots = [bot for bot in service._all('bots') if not bot.get('_deleted')]
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
        bots = [bot for bot in service._all('bots') if not bot.get('_deleted')]
    for bot in bots:
        shown = view(service, bot, dict(bot))
        agents.append({'id': bot['id'], 'name': shown['name'], 'coconut': bool(shown.get('coconut')),
                       'archived': bool(bot.get('archived')), 'model': bot.get('model_override') or 'default',
                       'access': bot.get('approval_override') or 'default',
                       'notifications': bot.get('notifications', True)})
    return {'mode': 'local', 'modes': {'local': 'available', 'cloud': 'coming soon'},
            'default_model': {key: default.get(key) for key in ('model', 'provider', 'reasoning_effort')},
            'models': models, 'default_access': approvals.get('defaults'),
            'access_modes': {mode: ACCESS_TEXT[mode] for mode in ACCESS}, 'agents': agents,
            'not_here': ('Pairing a device, phone notifications and their work hours, and signing in to GitHub or '
                         'Google need the device itself: the header of this chat has them.')}


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
    if setting == 'default_access':
        if arguments.get('value') not in ACCESS:
            raise ServiceError(400, 'Choose off, smart or manual.')
        before = read(service)['default_access']
        service.mutate('PUT', '/v1/settings/approvals', {'mode': arguments['value']}, key, device)
        return {'setting': setting, 'before': before, 'after': arguments['value'], 'changed': True}
    if setting in ('agent_model', 'agent_access', 'agent_notifications', 'agent_name'):
        with service.lock:
            bot = _agent(service, arguments.get('agent'))
        if setting == 'agent_model':
            value = None if arguments.get('model') in (None, '', 'default') else {
                name: arguments.get(name) for name in ('model', 'provider', 'reasoning_effort')
                if arguments.get(name) is not None}
            data, before = {'model_override': value}, bot.get('model_override') or 'default'
        elif setting == 'agent_access':
            value = arguments.get('value')
            if value not in (*ACCESS, None, 'default'):
                raise ServiceError(400, 'Choose off, smart, manual or default.')
            value = None if value == 'default' else value
            data, before = {'approval_override': value}, bot.get('approval_override') or 'default'
        elif setting == 'agent_notifications':
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
    raise ServiceError(400, 'Choose a setting that "read" lists.')


def settings_tool(service, task_id: str, arguments: dict) -> dict:
    """coconut_settings of a gateway turn. Only a turn of the Coconut chat changes Coconut."""
    from .service_base import ServiceError
    with service.lock:
        task = service._get('tasks', task_id)
    chat = task.get('conversation_bot_id') or task['bot_id']
    if not is_coconut(service, chat):
        raise ServiceError(403, 'Only the Coconut chat changes the settings of Coconut. Ask the person to open it.')
    if arguments.get('action') == 'read':
        return read(service)
    if arguments.get('action') == 'change':
        return change(service, arguments)
    raise ServiceError(400, 'Choose the action read or change.')
