"""Manage native Hermes MCP configuration of one profile."""
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
