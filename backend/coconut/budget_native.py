"""The Personal budget in the Hermes profile of Personal. This runs under the Python of the person's Hermes.

Turning the budget on points only that profile's model at the relay's budget proxy (personal_budget.py,
docs/personal-budget.md): a `providers` entry with the proxy URL and Anthropic's Messages API, and the budget token in the
profile's `.env` under ENV_KEY. The model section that the profile had before is saved next to its config, and turning
the budget off puts it back. Other profiles and their keys stay as they are.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROVIDER = 'coconut-budget'
ENV_KEY = 'COCONUT_BUDGET_TOKEN'
SAVED = '.coconut-budget.json'


def _settings():
    if __package__:
        from .native_settings import raw_config, save_config
    else:
        from native_settings import raw_config, save_config
    return raw_config, save_config


def _plain(value):
    """A YAML value of Hermes's round-trip loader as plain JSON data."""
    return json.loads(json.dumps(value, default=str)) if value is not None else None


def _env(home, key, value=None):
    # Hermes writes the `.env` of the active home.
    os.environ['HERMES_HOME'] = str(home)
    from hermes_cli.config import remove_env_value, save_env_value
    if value is None:
        remove_env_value(key)
    else:
        save_env_value(key, value)


def state(home):
    raw_config, _ = _settings()
    model = raw_config(home).get('model')
    return {'on': isinstance(model, dict) and model.get('provider') == PROVIDER}


def turn_on(home, *, base_url, model, token):
    raw_config, save_config = _settings()
    home = Path(home)
    if not isinstance(token, str) or not token.startswith('cb1.') or '\n' in token:
        raise ValueError('The budget token is invalid.')
    config = raw_config(home)
    saved = home / SAVED
    if not saved.exists() and (config.get('model') or {}).get('provider') != PROVIDER:
        # The first sign-in keeps the profile's own model, so a sign-out restores it. A second sign-in keeps the first.
        temporary = saved.with_name(saved.name + '.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump({'model': _plain(config.get('model'))}, handle)
        os.replace(temporary, saved)
    # The token first: a config that names the provider without its key would fail every call.
    _env(home, ENV_KEY, token)
    providers = config.get('providers')
    if not isinstance(providers, dict):
        config['providers'] = providers = {}
    providers[PROVIDER] = {'name': 'Coconut Personal budget', 'base_url': base_url, 'api_mode': 'anthropic_messages',
                           'key_env': ENV_KEY, 'model': model}
    config['model'] = {'default': model, 'provider': PROVIDER}
    save_config(home, config)
    return state(home)


def turn_off(home):
    raw_config, save_config = _settings()
    home = Path(home)
    config = raw_config(home)
    saved = home / SAVED
    previous = json.loads(saved.read_text()).get('model') if saved.is_file() else None
    model = config.get('model')
    if isinstance(model, dict) and model.get('provider') == PROVIDER:
        if previous:
            config['model'] = previous
        else:
            config.pop('model', None)
    providers = config.get('providers')
    if isinstance(providers, dict) and PROVIDER in providers:
        providers.pop(PROVIDER)
        if not providers:
            config.pop('providers', None)
    save_config(home, config)
    _env(home, ENV_KEY, None)
    saved.unlink(missing_ok=True)
    return state(home)


def worker(op, request, homes):
    (home,) = homes.values()
    if op == 'budget_on':
        return turn_on(home, base_url=request['base_url'], model=request['model'], token=request['token'])
    if op == 'budget_off':
        return turn_off(home)
    return state(home)
