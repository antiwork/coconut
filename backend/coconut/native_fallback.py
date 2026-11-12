"""Expose the fallback models of Hermes. Hermes owns the retry logic."""
from copy import deepcopy

if __package__:
    from .native_settings import raw_config, save_config
    from .model_catalog import FALLBACK_KEYS
else:
    from native_settings import raw_config, save_config
    from model_catalog import FALLBACK_KEYS

# A request holds at most this many fallback models. A chain that a person wrote in Hermes stays below it.
MAX_MODELS = 32


def chain(config):
    """The fallback chain of Hermes, in order: `fallback_providers`, then the legacy `fallback_model`."""
    if not any(key in config for key in FALLBACK_KEYS):
        return []
    from hermes_cli.fallback_config import get_fallback_chain
    return get_fallback_chain(config)


def identity(entry):
    """The route of an entry without its endpoint. The app does not see or send an endpoint."""
    from hermes_cli.providers import normalize_provider
    return normalize_provider(entry['provider']), entry['model'].strip().lower()


def public(entries):
    # Only the model and its provider leave the Mac. Other keys can hold a key, a key name or a private endpoint.
    return {'models': [{'model': entry['model'], 'provider': entry['provider']} for entry in entries]}


def state(home):
    return public(chain(raw_config(home)))


def validate(models):
    if (not isinstance(models, list) or len(models) > MAX_MODELS
            or not all(isinstance(item, dict) and set(item) == {'model', 'provider'}
                       and all(isinstance(item[key], str) and item[key].strip() for key in item) for item in models)):
        raise ValueError('Select fallback models from the Hermes list.')
    return [{'model': item['model'].strip(), 'provider': item['provider'].strip()} for item in models]


def kept_entries(current, models, identity):
    """Map `models` to entries of the chain `current`. Return the new chain.

    Each model that the chain has keeps its entry. The chain can hold one model more than once, with different
    endpoints. When the request holds fewer copies of that model, the last copies of the chain stay: the app removes or
    replaces models at the start. A further copy of a model that the request already has is dropped. A new model gets
    only its provider and model.
    """
    from collections import Counter
    wanted = Counter(identity(item) for item in models)
    unused = {}
    for entry in current:
        unused.setdefault(identity(entry), []).append(entry)
    for route, entries in unused.items():
        del entries[:max(len(entries) - wanted[route], 0)]
    result, seen = [], set()
    for item in models:
        route = identity(item)
        if unused.get(route):
            result.append(deepcopy(unused[route].pop(0)))
        elif route not in seen:
            result.append(item)
        seen.add(route)
    return result


def save(home, models):
    """Set the fallback chain of a profile to `models`, in order. Return the new state.

    Each model keeps its entry of the chain, with its endpoint and its key settings (see `kept_entries`). The kept entry
    is the copy that Hermes reads: Hermes removes spaces from `provider` and `model`, and the `/` at the end of
    `base_url`. Hermes resolves the endpoint and the key of a new model from its provider. The new chain goes into
    `fallback_providers`, so the legacy `fallback_model` moves there. An entry that Hermes cannot use, without a
    provider or a model, is not kept.
    """
    models = validate(models)
    config = raw_config(home)
    entries = kept_entries(chain(config), models, identity)
    providers, legacy = FALLBACK_KEYS
    config.pop(legacy, None)
    if entries:
        config[providers] = entries
    else:
        config.pop(providers, None)
    save_config(home, config)
    return state(home)
