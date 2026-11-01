"""Expose Hermes's OpenAI fallback without owning provider retry logic."""
from contextlib import contextmanager
import os
from pathlib import Path

if __package__:
    from .native_settings import raw_config, save_config
    from .model_catalog import _contained
else:
    from native_settings import raw_config, save_config
    from model_catalog import _contained


def chain(config):
    if not any(key in config for key in ('fallback_providers', 'fallback_model')):
        return []
    from hermes_cli.fallback_config import get_fallback_chain
    return get_fallback_chain(config)


def is_openai(entry):
    return (entry.get('provider') == 'openai-api'
            and entry_url(entry).rstrip('/') == 'https://api.openai.com/v1')


def entry_url(entry):
    from hermes_cli.auth import PROVIDER_REGISTRY
    from agent.secret_scope import get_secret
    provider = PROVIDER_REGISTRY['openai-api']
    # Bind the native registry endpoint through the same scope as its key.
    return entry.get('base_url') or (get_secret(provider.base_url_env_var) or '').strip() or provider.inference_base_url


@contextmanager
def configuration(home):
    for name in ('config.yaml', '.env'):
        _contained(Path(home) / name, home)
    raw_config(home)
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope
    from hermes_cli.config import load_config_readonly
    from hermes_cli.env_loader import hydrate_profile_secret_sources, reset_secret_source_cache
    home_token = set_hermes_home_override(str(home))
    secret_token = None
    try:
        # Process exports can belong to another bot or an earlier turn.
        secrets = dict.fromkeys(os.environ, '')
        reset_secret_source_cache(Path(home))
        hydrate_profile_secret_sources(Path(home))
        secrets.update(build_profile_secret_scope(Path(home)))
        secret_token = set_secret_scope(secrets)
        yield load_config_readonly()
    finally:
        if secret_token is not None:
            reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def state(home):
    with configuration(home) as config:
        entry = next((item for item in chain(config) if is_openai(item)), None)
        model = config.get('model') or {}
        candidate = model.get('default') if isinstance(model, dict) and model.get('provider') in ('openai-codex', 'openai-api') else None
        available = bool(entry_key(entry or {'provider': 'openai-api'}))
        return dict(enabled=entry is not None, available=available,
                    model=entry['model'] if entry else candidate,
                    can_enable=available and bool(entry or candidate))


def save(home, enabled):
    if type(enabled) is not bool:
        raise ValueError('Choose whether to use the OpenAI API fallback.')
    current = state(home)
    if enabled and not current['can_enable']:
        raise ValueError('Save an OpenAI API key on the Mac and select an OpenAI model first.')
    config = raw_config(home)
    entries = chain(config)
    from hermes_cli.config import _expand_env_vars
    with configuration(home):
        openai_entries = [is_openai(_expand_env_vars(entry)) for entry in entries]
    if enabled and not any(openai_entries):
        entries.append(dict(provider='openai-api', model=current['model'],
                            base_url='https://api.openai.com/v1', key_env='OPENAI_API_KEY'))
    if not enabled:
        entries = [entry for entry, matches in zip(entries, openai_entries) if not matches]
    config['fallback_providers'] = entries
    config.pop('fallback_model', None)
    save_config(home, config)
    return state(home)


def entry_key(entry):
    from hermes_cli.fallback_config import resolve_entry_api_key
    from hermes_cli.auth import resolve_api_key_provider_credentials
    from hermes_cli.config import _ENV_REF_RE
    key = resolve_entry_api_key(entry)
    if key and _ENV_REF_RE.search(key):
        key = None
    if not key and entry.get('provider') == 'openai-api':
        key = resolve_api_key_provider_credentials('openai-api').get('api_key')
    return key if key and not _ENV_REF_RE.search(key) else None
