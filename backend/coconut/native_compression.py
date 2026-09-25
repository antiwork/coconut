"""Instance-local bridge from unavailable auto summaries to the shared fallback.

Do not replace Hermes's compression facade: it owns deadlines, cancellation,
leases, archival and commit fences. The summary-call seam is inside all of those
protections and raises a typed unavailable-route exception before sending data.
"""
from functools import wraps
from contextvars import ContextVar


def _inherits(config):
    # Presence, including null/empty/disabled, is an explicit local decision.
    if any(key in config for key in ('fallback_providers', 'fallback_model')):
        return False
    auxiliary = config.get('auxiliary') or {}
    if not isinstance(auxiliary, dict):
        return False
    compression = auxiliary.get('compression') or {}
    if not isinstance(compression, dict):
        return False
    for key in ('provider', 'model'):
        if str(compression.get(key) or '').strip().lower() not in ('', 'auto'):
            return False
    # Do not reinterpret explicit auxiliary credentials/endpoints/transports or
    # a task chain (even an empty one). Timing/context budgets are not routes.
    return not any(key in compression for key in (
        'base_url', 'api_key', 'key_env', 'api_key_env', 'api_mode', 'fallback_chain'))


def _routes(fallback):
    return tuple(dict(entry) for entry in (fallback or ())
                 if entry.get('provider') == 'openai-api' and entry.get('model')
                 and entry.get('api_key') and entry.get('base_url'))


def install(agent, config, fallback):
    """Attach once to THIS compressor; reuse for_turn's resolved in-memory routes.

    Only route *unavailability* permits inheritance. Working native routes and
    native API-error recovery run unchanged. No configuration or global function
    is modified, and credentials are never emitted or persisted by this adapter.
    """
    if not _inherits(config):
        return False
    routes = _routes(fallback)
    compressor = getattr(agent, 'context_compressor', None)
    original = getattr(compressor, '_call_summary_llm', None)
    generate = getattr(compressor, '_generate_summary', None)
    if not routes or not callable(original) or not callable(generate) or getattr(original, '_coconut_inherited', False):
        return False
    if getattr(compressor, 'summary_model', '') not in ('', 'auto', None):
        return False
    from agent.auxiliary_client import AuxiliaryClientUnavailable
    from agent.context_compressor import pin_summary_route, take_pinned_summary_route
    from hermes_constants import get_hermes_home
    if __package__:
        from .native_settings import raw_config
        from .native_fallback import for_turn
    else:
        from native_settings import raw_config
        from native_fallback import for_turn
    inherited: ContextVar[bool | None] = ContextVar('coconut_inherited_summary_attempt', default=None)

    @wraps(generate)
    def generate_summary(*args, **kwargs):
        # Native main-model recursion belongs to the SAME attempt. A copied
        # worker context gets its own immutable flag; no compressor-global bit.
        if inherited.get() is not None:
            return generate(*args, **kwargs)
        token = inherited.set(False)
        try:
            return generate(*args, **kwargs)
        finally:
            inherited.reset(token)

    @wraps(original)
    def summary(*args, **kwargs):
        # A native stall-recovery pin is authoritative. Consume/rebind it only
        # around this call, so it stays single-use even in a copied context.
        pinned = take_pinned_summary_route()
        with pin_summary_route(pinned):
            try:
                return original(*args, **kwargs)
            except AuxiliaryClientUnavailable:
                # Read raw presence again: native merged defaults contain empty
                # route fields, and a user may have changed policy mid-turn.
                if (pinned is not None or inherited.get() is True
                        or not _inherits(raw_config(get_hermes_home()))):
                    raise
                # The shared owner can revoke/change its route while this turn
                # is running. Never send a summary using a stale route or key.
                if _routes(for_turn()) != routes:
                    raise
                # The real resolver has already exhausted the native routes.
                # Never route a cancellation, quota/transport failure, malformed
                # summary or explicit auxiliary override through this adapter.
                if inherited.get() is not None:
                    inherited.set(True)
                for route in routes:
                    with pin_summary_route(dict(route)):
                        try:
                            return original(*args, **kwargs)
                        except AuxiliaryClientUnavailable:
                            continue
                raise

    setattr(summary, '_coconut_inherited', True)
    setattr(compressor, '_call_summary_llm', summary)
    setattr(compressor, '_generate_summary', generate_summary)
    return True
