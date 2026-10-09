"""Manage native Hermes MCP configuration and inherit shared connections."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


def _settings():
    try:
        from .native_settings import raw_config, save_config
    except ImportError:
        from native_settings import raw_config, save_config
    return raw_config, save_config


def servers(home):
    raw, _ = _settings()
    value = raw(home).get('mcp_servers') or {}
    if not isinstance(value, dict):
        raise ValueError('Hermes MCP settings must contain a mapping.')
    return value


def public_servers(home):
    result = []
    for name, config in servers(home).items():
        if not isinstance(config, dict):
            continue
        # Never return headers, environment variables, arguments, or credential-bearing URLs.
        address = config.get('url', '')
        try:
            parts = urlsplit(address) if isinstance(address, str) else None
        except ValueError:
            parts = None
        safe = bool(parts and parts.scheme in ('http', 'https') and parts.hostname
                    and not parts.username and not parts.password and not parts.query and not parts.fragment)
        result.append(dict(id=name, url=address if safe else '', transport='http' if address else 'stdio',
                           has_token=bool(config.get('headers')), editable=safe and bool(re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name))))
    return sorted(result, key=lambda item: item['id'])


def manage(request, home):
    from hermes_cli.mcp_config import _save_bearer_auth_token, _probe_single_server, _bearer_auth_headers, _env_key_for_server
    from hermes_cli.config import remove_env_value
    from hermes_cli.mcp_security import validate_mcp_server_entry
    raw, save = _settings()
    action = request['action']
    name = request.get('name', '')
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name):
        raise ValueError('Use a name with lowercase letters, numbers, and underscores.')
    config = raw(home)
    entries = servers(home)
    if action == 'save':
        address = request.get('url', '')
        if not isinstance(address, str):
            raise ValueError('Enter an HTTPS server address.')
        address = address.strip()
        parts = urlsplit(address)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError('Use HTTPS without credentials, a query, or a fragment in the address.')
        token = request.get('token')
        if token is not None and (not isinstance(token, str) or len(token) > 16384 or '\n' in token.strip() or '\r' in token.strip()):
            raise ValueError('Enter a valid Bearer token.')
        current = entries.get(name, {})
        if not isinstance(current, dict) or current.get('command'):
            raise ValueError('Manage this existing server through Hermes on your Mac.')
        # Do not send an existing credential to a replacement endpoint.
        if current.get('url') != address and current.get('headers') and token is None:
            raise ValueError('Enter a new token when changing an authenticated server address.')
        entry = dict(current, url=address)
        if token is not None:
            entry.pop('headers', None)
            if token.strip():
                entry['headers'] = _bearer_auth_headers(name)
        if validate_mcp_server_entry(name, entry):
            raise ValueError('Hermes rejected this MCP configuration.')
        if token is not None and token.strip():
            _save_bearer_auth_token(name, token)
        entries[name] = entry
        config['mcp_servers'] = entries
        save(home, config)
        if token is not None and not token.strip() and current.get('headers') == _bearer_auth_headers(name):
            remove_env_value(_env_key_for_server(name))
    elif action == 'remove':
        removed = entries.pop(name, {})
        config['mcp_servers'] = entries
        save(home, config)
        if isinstance(removed, dict) and removed.get('headers') == _bearer_auth_headers(name):
            remove_env_value(_env_key_for_server(name))
    elif action == 'test':
        if name not in entries:
            raise ValueError('This MCP server no longer exists.')
        try:
            tools = _probe_single_server(name, entries[name], connect_timeout=8)
            return dict(ok=True, tools=[name for name, _ in tools], servers=public_servers(home))
        except Exception:
            # Native network errors can include request headers or credential-bearing URLs.
            return dict(ok=False, tools=[], error='Connection failed. Check the server address and authentication.', servers=public_servers(home))
    else:
        raise ValueError('Unknown MCP operation.')
    return dict(ok=True, tools=[], servers=public_servers(home))


_original_loader = None
_shared = {}
_signature = None


def reset_connections():
    from tools import mcp_tool
    from tools.registry import registry
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.mcp_tool_agent import reprobe_tool_availability
    with mcp_tool._lock:
        owned = [(name, mcp_tool._server_registry_scope(server))
                 for name, server in mcp_tool._mcp_tool_server_names.items()]
    shutdown_mcp_servers()
    for name, scope in owned:
        registry.deregister(name, scope=scope)
    # Native shutdown keeps lazy registrations. Drop them before loading new credentials.
    with mcp_tool._lock:
        for values in (mcp_tool._lazy_server_configs, mcp_tool._lazy_server_fingerprints,
                       mcp_tool._lazy_server_tool_names, mcp_tool._mcp_tool_server_names,
                       mcp_tool._server_trust_levels, mcp_tool._tool_read_only_hints,
                       mcp_tool._server_error_counts, mcp_tool._server_breaker_opened_at,
                       mcp_tool._parallel_safe_servers):
            values.clear()
    reprobe_tool_availability()


def refresh_inheritance(default_root, *, allow_reset=True):
    """Refresh native MCP connections between turns without stopping terminal processes."""
    global _original_loader, _shared, _signature
    default = Path(default_root)
    entries = servers(default)
    if not entries and _original_loader is None:
        return []
    from tools import mcp_tool_config
    from agent.secret_scope import load_env_file, set_secret_scope, reset_secret_scope
    from hermes_cli.mcp_security import validate_mcp_server_entry
    from hermes_constants import get_hermes_home
    from tools.mcp_tool_common import _env_ref_name
    values = load_env_file(default / '.env')
    for match in mcp_tool_config._ENV_VAR_PATTERN.finditer(json.dumps(entries)):
        values.setdefault(_env_ref_name(match.group(1)), '')
    scope = set_secret_scope(values)
    try:
        # Each Kanban task starts a new worker, and an eager server connects again in each one. A lazy server
        # registers its tools from the Hermes schema cache and connects on first use. An explicit setting wins.
        shared = {name: {'lazy': True, **mcp_tool_config._interpolate_env_vars(entry)} for name, entry in entries.items()
                  if isinstance(entry, dict) and not validate_mcp_server_entry(name, entry)}
    finally:
        reset_secret_scope(scope)
    if _original_loader is None:
        _original_loader = mcp_tool_config._load_mcp_config
        def inherited():
            from utils import env_var_enabled
            if env_var_enabled('HERMES_SAFE_MODE'):
                return {}
            return dict(deepcopy(_shared), **_original_loader())
        mcp_tool_config._load_mcp_config = inherited
    local = _original_loader()
    signature = hashlib.sha256(json.dumps([str(get_hermes_home()), shared, local], sort_keys=True).encode()).hexdigest()
    if _signature is not None and _signature != signature:
        if not allow_reset:
            return list(_shared)
        reset_connections()
    _shared = shared
    _signature = signature
    return list(shared)
