"""Native Hermes settings, accessed through its installed Python runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path


def raw_config(home):
    from hermes_cli.config import require_readable_config_before_write
    return require_readable_config_before_write(Path(home) / 'config.yaml')


def save_config(home, config):
    from utils import atomic_roundtrip_yaml_save
    atomic_roundtrip_yaml_save(Path(home) / 'config.yaml', config)


def mode(value):
    from tools.approval_context import _normalize_approval_mode
    return _normalize_approval_mode(value)


def approval_state(home):
    from tools.approval_context import _VALID_MODES
    descriptions = {
        'off': ('Full access', 'Hermes skips approval prompts. Its built-in blocks still apply.'),
        'smart': ('Auto', 'Hermes reviews flagged actions and asks when needed.'),
        'manual': ('Ask for flagged actions', 'Hermes asks before actions that its approval checks flag.'),
    }
    config = raw_config(home)
    value = mode(config.get('approvals', {}).get('mode', 'manual'))
    return dict(default=value, modes=[dict(id=item, label=descriptions.get(item, (item, ''))[0],
                                         description=descriptions.get(item, (item, ''))[1]) for item in ('off', 'smart', 'manual') if item in _VALID_MODES])


def profile_state(home, default_home):
    from hermes_cli.profiles import read_profile_meta
    meta = read_profile_meta(home)
    config = raw_config(home)
    approvals = config.get('approvals') or {}
    override = mode(approvals['mode']) if 'mode' in approvals else None
    return dict(name=meta.get('display_name', Path(home).name), role=meta.get('description', ''),
                instructions=(Path(home) / 'SOUL.md').read_text() if (Path(home) / 'SOUL.md').exists() else '',
                approval_override=override, effective_approval=override or approval_state(default_home)['default'],
                worker_limit=(config.get('delegation') or {}).get('max_concurrent_children'))


def validate_mode(value, nullable=False):
    from tools.approval_context import _VALID_MODES
    if value is None and nullable:
        return
    if not isinstance(value, str) or value not in _VALID_MODES:
        raise ValueError('Choose an approval mode from Hermes settings.')


def worker(request, default_home):
    from hermes_cli.profiles import write_profile_meta
    from model_catalog import _contained
    op = request['native_operation']
    homes = {identity: _contained(home, request['data_root']) for identity, home in request.get('homes', {}).items()}
    if op == 'mcp':
        from native_mcp import manage
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from agent.secret_scope import load_env_file, set_secret_scope, reset_secret_scope
        root = Path(request.get('settings_root', default_home))
        token = set_hermes_home_override(str(root))
        scope = set_secret_scope(load_env_file(root / '.env'))
        try:
            return manage(request, root)
        finally:
            reset_secret_scope(scope)
            reset_hermes_home_override(token)
    if op == 'relay_recover':
        from native_relay import recover_delivery
        return recover_delivery(homes[request['bot_id']], request['call_id'])
    if op == 'relay_reply':
        from native_relay import write_native_reply
        write_native_reply(homes[request['bot_id']], request['envelope_id'], request['text'], request.get('failed', False))
        return {'status': 'recorded'}
    if op.startswith('routine'):
        from native_routines import worker as routines
        return routines(op, request, homes)
    if op == 'migrate':
        marker = Path(request.get('settings_root', default_home)) / 'coconut-native-settings.json'
        if not marker.exists():
            config = raw_config(default_home)
            config.setdefault('approvals', {})['mode'] = 'off'
            save_config(default_home, config)
            from profiles import _write
            _write(marker, '{"version":1}\n', marker.parent)
        for home in homes.values():
            config = raw_config(home)
            delegation = config.setdefault('delegation', {})
            if 'max_concurrent_children' not in delegation:
                # Null selects Hermes's environment fallback instead of its merged default.
                delegation['max_concurrent_children'] = None
                save_config(home, config)
            marker = home / '.coconut-native-config.json'
            if marker.exists():
                continue
            config = raw_config(home)
            config.setdefault('terminal', {}).setdefault('cwd', str(home.parent / 'workspace'))
            tools = config.get('tools') or {}
            if tools.get('tool_search', {}).get('enabled') == 'off':
                tools['tool_search'].pop('enabled', None)
            agent = config.get('agent') or {}
            if agent.get('environment_probe') is False:
                agent.pop('environment_probe', None)
            # Native memory limits must fit all entries imported from the former store.
            for filename, key in (('MEMORY.md', 'memory_char_limit'), ('USER.md', 'user_char_limit')):
                path = home / 'memories' / filename
                if path.exists():
                    size = len(path.read_text())
                    if size > 1000:
                        memory = config.setdefault('memory', {})
                        memory[key] = max(memory.get(key, 0), size + 1000)
            save_config(home, config)
            from profiles import _write
            _write(marker, '{"version":1}\n', home)
        return {'ok': True}
    if op == 'snapshot_read':
        from native_routines import worker as routines
        result = worker(dict(request, native_operation='read'), default_home)
        result.update(routines('routines_read', request, homes))
        return result
    if op == 'read':
        from native_mcp import public_servers
        from parallel_work import state as parallel_state
        return {'profiles': {identity: profile_state(home, default_home) for identity, home in homes.items()},
                'approvals': approval_state(default_home), 'parallel_work': parallel_state(default_home), 'mcp_servers': public_servers(Path(request.get('settings_root', default_home)))}
    if op == 'set_parallel_work':
        from parallel_work import save, state
        save(default_home, request['limit'])
        return state(default_home)
    if op == 'set_default_approval':
        value = request['mode']
        validate_mode(value)
        config = raw_config(default_home)
        config.setdefault('approvals', {})['mode'] = value
        save_config(default_home, config)
        return approval_state(default_home)
    if op == 'update_profile':
        home = homes[request['bot_id']]
        changes = request['changes']
        if 'name' in changes or 'role' in changes:
            write_profile_meta(home, **{native: changes[app] for app, native in
                                       (('name', 'display_name'), ('role', 'description')) if app in changes})
        if 'worker_limit' in changes:
            from parallel_work import save
            save(home, changes['worker_limit'], inherit=True)
        if 'approval_override' in changes:
            value = changes['approval_override']
            validate_mode(value, nullable=True)
            config = raw_config(home)
            approvals = config.setdefault('approvals', {})
            if value is None:
                approvals.pop('mode', None)
            else:
                approvals['mode'] = value
            save_config(home, config)
        return profile_state(home, default_home)
    raise ValueError('Unknown native settings operation.')


def install_approval_inheritance(default_home):
    """Resolve the default before each native approval check, including queued tools."""
    from tools import approval_context
    original = approval_context._get_approval_config
    def current():
        inherited = dict(original())
        from model_catalog import native_home
        global_values = raw_config(native_home(default_home)).get('approvals') or {}
        local_values = raw_config(os.environ['HERMES_HOME']).get('approvals') or {}
        inherited.update(global_values)
        inherited.update(local_values)
        return inherited
    approval_context._get_approval_config = current
