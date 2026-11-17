"""Map the Coconut setting for helpers per task to native Hermes delegation settings."""
if __package__:
    from .native_settings import raw_config, save_config
    from .work_limits import validate_limit
else:
    from native_settings import raw_config, save_config
    from work_limits import validate_limit


def hermes_default():
    # Hermes runs this many helpers at once when a profile sets none.
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    return DEFAULT_CONFIG['delegation']['max_concurrent_children']


def state(home):
    limit = (raw_config(home).get('delegation') or {}).get('max_concurrent_children')
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError('Hermes has an invalid delegation.max_concurrent_children setting.')
    automatic = hermes_default()
    return dict(limit=limit, effective_limit=limit if limit is not None else automatic, automatic_limit=automatic)


def save(home, limit, *, inherit=False):
    validate_limit(limit)
    config = raw_config(home)
    delegation = config.setdefault('delegation', {})
    if limit is None and not inherit:
        delegation.pop('max_concurrent_children', None)
    else:
        delegation['max_concurrent_children'] = limit
    save_config(home, config)
