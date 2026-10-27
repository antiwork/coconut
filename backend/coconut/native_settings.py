"""Native Hermes settings, accessed through its installed Python runtime."""
from __future__ import annotations

from pathlib import Path


def raw_config(home):
    from hermes_cli.config import require_readable_config_before_write
    return require_readable_config_before_write(Path(home) / 'config.yaml')


def save_config(home, config):
    if __package__:
        from .model_catalog import save_yaml
    else:
        from model_catalog import save_yaml
    save_yaml(Path(home) / 'config.yaml', config)


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


def profile_state(home):
    from hermes_cli.profiles import read_profile_meta
    meta = read_profile_meta(home)
    config = raw_config(home)
    approvals = config.get('approvals') or {}
    override = mode(approvals['mode']) if 'mode' in approvals else None
    # Each profile uses its own approval mode. Without one, Hermes asks for flagged actions.
    return dict(name=meta.get('display_name', Path(home).name), role=meta.get('description', ''),
                instructions=(Path(home) / 'SOUL.md').read_text() if (Path(home) / 'SOUL.md').exists() else '',
                approval_override=override, effective_approval=override or mode('manual'),
                worker_limit=(config.get('delegation') or {}).get('max_concurrent_children'))


def validate_mode(value, nullable=False):
    from tools.approval_context import _VALID_MODES
    if value is None and nullable:
        return
    if not isinstance(value, str) or value not in _VALID_MODES:
        raise ValueError('Choose an approval mode from Hermes settings.')


def worker(request, default_home):
    from hermes_cli.profiles import write_profile_meta
    from model_catalog import profile_contained
    op = request['native_operation']
    homes = {identity: profile_contained(home, request) for identity, home in request.get('homes', {}).items()}
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
    if op == 'snapshot_read':
        from native_routines import worker as routines
        result = worker(dict(request, native_operation='read'), default_home)
        result.update(routines('routines_read', request, homes))
        return result
    if op == 'set_api_fallback':
        from native_fallback import save
        return save(default_home, request['enabled'])
    if op == 'read':
        from native_fallback import state as fallback_state
        from native_mcp import public_servers
        from parallel_work import state as parallel_state
        return {'profiles': {identity: profile_state(home) for identity, home in homes.items()},
                'api_fallback': fallback_state(default_home), 'approvals': approval_state(default_home), 'parallel_work': parallel_state(default_home), 'mcp_servers': public_servers(Path(request.get('settings_root', default_home)))}
    if op == 'parallel_work_read':
        from parallel_work import state
        return state(default_home)
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
        return profile_state(home)
    raise ValueError('Unknown native settings operation.')

