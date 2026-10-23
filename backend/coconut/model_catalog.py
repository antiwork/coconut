"""Read native Hermes settings through the Python of the installed Hermes."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

# A source folder that does not exist. The catalog uses it when this Mac has no Hermes.
MISSING_SOURCE = Path('/nonexistent/hermes-agent')
MAX_REQUEST_CHARACTERS = 100_000
# A profile read that failed, for example because a busy Mac delayed the settings process, runs again after this.
PROFILE_RETRY_SECONDS = 15
NATIVE_TEXT_FIELDS = {'routine_finish': 'output', 'relay_reply': 'text'}
RUNTIME_KEYS = frozenset(('provider', 'api_mode', 'base_url', 'api_key', 'requested_provider',
                          'acp_command', 'acp_args', 'command', 'args'))
ENV_KEYS = ('PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
            'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY')
# xai-oauth is an xAI subscription through Hermes device-code sign-in. It uses the codex_responses API.
SUPPORTED_PROVIDERS = frozenset(('openai-codex', 'anthropic', 'openai', 'openai-api', 'openrouter', 'xai-oauth'))
UNSUPPORTED_RUNTIME_FIELDS = frozenset(('extra_headers', 'extra_body', 'request_overrides', 'capabilities', 'key_cmd'))
UNSUPPORTED_ROUTE_MESSAGE = 'This Hermes provider setup is not supported in Coconut yet. Use it in Hermes Terminal.'


class ModelCatalogError(ValueError):
    pass


class UnsupportedModelRoute(ModelCatalogError):
    pass


def public_endpoint(value):
    if not value:
        return None
    if not isinstance(value, str):
        raise ModelCatalogError('Hermes has an invalid model endpoint.')
    parsed = urlsplit(value)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname
            or parsed.scheme not in ('http', 'https')
            or (parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'))):
        raise ModelCatalogError('Use a separate credential field and a secure endpoint in Hermes settings.')
    return value.rstrip('/')


def _contained(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        path.relative_to(root)
    except ValueError:
        raise ModelCatalogError('The Hermes profile must remain inside Coconut storage.') from None
    for node in (path, *path.parents):
        if node.is_symlink():
            raise ModelCatalogError('Linked Hermes settings are not supported.')
        if node == root:
            break
    return path


def native_home(root):
    """Resolve the native active profile before importing profile-bound modules."""
    root = Path(root)
    active = _contained(root / 'active_profile', root)
    name = active.read_text(encoding='utf-8').strip() if active.exists() else 'default'
    if not name or name == 'default':
        return root
    if '/' in name or '\\' in name or name in ('.', '..'):
        raise ModelCatalogError('The active Hermes profile is invalid.')
    profile = _contained(root / 'profiles' / name, root)
    if not profile.is_dir():
        raise ModelCatalogError('The active Hermes profile is missing.')
    return profile


def _selection(value):
    if (not isinstance(value, dict) or set(value) - {'model', 'provider', 'reasoning_effort'}
            or not isinstance(value.get('model'), str) or not value['model'].strip()
            or not isinstance(value.get('provider'), str) or not value['provider'].strip()
            or (value.get('reasoning_effort') is not None and not isinstance(value['reasoning_effort'], str))):
        raise ModelCatalogError('Select a model and effort from the Hermes list.')
    return {key: value.get(key) for key in ('model', 'provider', 'reasoning_effort')}


def resolve_selection(selection, catalog):
    if selection is None:
        return deepcopy(catalog['default'])
    value = _selection(selection)
    row = next((item for item in catalog['models']
                if item['id'] == value['model'] and item['provider'] == value['provider']), None)
    if row is None:
        raise ModelCatalogError('This model is no longer in the Hermes list. Refresh the list.')
    effort = value['reasoning_effort']
    if effort is not None and effort not in row['reasoning_efforts']:
        raise ModelCatalogError('This effort is not available for the selected Hermes model.')
    return {'model': value['model'], 'provider': value['provider'],
            'base_url': public_endpoint(row.get('base_url')),
            'reasoning_effort': effort if effort is not None else row.get('default_reasoning_effort')}


# After a live refresh that failed, the next one may start this many seconds later.
LIVE_RETRY_SECONDS = 30


class HermesModelCatalog:
    def __init__(self, data_dir, python=None, source=None, env=None, home=None):
        """Use the person's Hermes. `home` is the Coconut folder that holds the shared Hermes home of the bots.

        Without `home`, an explicit `source` keeps that folder beside it, as the tests lay it out. Otherwise Coconut
        uses hermes_runtime.DEFAULT_HOME.
        """
        from .hermes_runtime import DEFAULT_HOME, configured
        self.data_dir = Path(data_dir).expanduser().resolve()
        if home is None:
            home = Path(source).parent / 'terminal' if source is not None else DEFAULT_HOME
        python, source = configured(python, source)
        self.python = str(python or '')
        self.source = Path(source or MISSING_SOURCE).resolve()
        self.home = Path(home).absolute()
        self.root = self.home / 'hermes'
        self.private_home = self.home / 'home'
        supplied = os.environ if env is None else env
        self.env = {key: supplied[key] for key in ENV_KEYS if key in supplied}
        self._cache = None
        self._cache_key = None
        self._cache_time = float('-inf')
        # No live refresh starts before this time (see refresh_if_old).
        self._live_blocked_until = float('-inf')
        self._live_pending = False
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._refreshing = False
        self._profile_cache = {}
        self.on_change = None

    def _signature(self):
        selected = native_home(self.root)
        paths = [self.root / 'active_profile', self.source / 'hermes_constants.py',
                 selected / 'config.yaml', selected / '.env', selected / 'auth.json',
                 self.root / 'auth.json', selected / '.anthropic_oauth.json',
                 self.root / '.anthropic_oauth.json']
        signature = []
        for path in paths:
            if path == self.source / 'hermes_constants.py':
                boundary = self.source
            elif path == self.root / 'active_profile':
                boundary = self.home
            else:
                boundary = self.root
            _contained(path, boundary)
            try:
                state = path.stat()
                signature.append((str(path), state.st_ino, state.st_mtime_ns, state.st_size))
            except FileNotFoundError:
                signature.append((str(path), None))
        return tuple(signature)

    def get(self, refresh=False, background=True):
        with self._lock:
            try:
                signature = self._signature()
            except (OSError, ValueError):
                return unavailable_catalog()
            if self._cache is None or signature != self._cache_key:
                try:
                    result = self._call('defaults')
                except ModelCatalogError:
                    result = unavailable_catalog()
                if self._cache is not None:
                    result['models'] = self._cache['models']
                self._cache = result
                self._cache_key = signature
                self._cache_time = float('-inf')
            if background and (refresh or time.monotonic() - self._cache_time >= 60):
                self._start_refresh(bool(refresh))
            return dict(deepcopy(self._cache), loading=self._refreshing)

    def refresh_if_old(self, after):
        """Ask Hermes for the live model lists, unless a live refresh started in the last `after` seconds.

        The background refresh reads the lists that Hermes has cached, and Hermes keeps its Codex list until someone asks for a
        live one. A model that a provider adds later, such as GPT 6.1 Sol, then stays out of the picker. The caller gets the
        cached catalog at once. The live lists arrive later, and Coconut sends `model_catalog.updated`.
        """
        with self._lock:
            now = time.monotonic()
            if now < self._live_blocked_until:
                return
            # Reserve the wait before the lock ends, so that two settings reads at once start one live refresh.
            self._live_blocked_until = now + after
        self.get(refresh=True)

    def _start_refresh(self, refresh):
        if self._refreshing:
            # A live request must not be lost because an ordinary refresh runs. It starts when that one ends.
            if refresh:
                self._live_pending = True
            return
        self._refreshing = True
        signature = self._cache_key

        def update():
            try:
                result = self._call('get', refresh=refresh)
                with self._lock:
                    if signature == self._signature():
                        self._cache = result
                        self._cache_key = self._signature()
                        self._cache_time = time.monotonic()
            except ModelCatalogError:
                if refresh:
                    # A failed live look may be tried again soon, but not at once. The app reads the settings again when
                    # it hears `model_catalog.updated`, and that read must not start the next look. The event still goes
                    # out, because the app shows "Loading models" until it hears one.
                    with self._lock:
                        self._live_blocked_until = min(self._live_blocked_until, time.monotonic() + LIVE_RETRY_SECONDS)
            finally:
                with self._lock:
                    if signature == self._cache_key:
                        self._cache_time = time.monotonic()
                    self._refreshing = False
                    live_again, self._live_pending = self._live_pending, False
                callback = self.on_change
                if callback:
                    try:
                        callback()
                    except Exception:
                        pass
                if live_again:
                    with self._lock:
                        self._start_refresh(True)
        threading.Thread(target=update, daemon=True, name='hermes-model-catalog').start()

    def resolve(self, selection, catalog=None):
        if catalog is None:
            if selection is None:
                return self.get_default()
            catalog = self.get_for_validation()
        return resolve_selection(selection, catalog)

    def get_default(self):
        return self._call('defaults')['default']

    def get_for_validation(self):
        """Wait for native inventory only at an explicit selection boundary."""
        catalog = self.get(background=False)
        if catalog.get('error'):
            raise ModelCatalogError(catalog['error'])
        if catalog['models']:
            return catalog
        signature = self._signature()
        result = self._call('get')
        with self._lock:
            if signature != self._signature():
                raise ModelCatalogError('Hermes settings changed. Refresh the model list.')
            self._cache = result
            self._cache_key = signature
            self._cache_time = time.monotonic()
        return deepcopy(result)

    def set_default(self, selection):
        value = _selection(selection)
        with self._write_lock:
            catalog = self.get_for_validation()
            self.resolve(value, catalog)
            result = self._call('set_default', selection=value, catalog=catalog)
            with self._lock:
                self._cache = result
                self._cache_key = self._signature()
                self._cache_time = time.monotonic()
                return deepcopy(result)

    def resolve_runtime(self, selection, bot_id=None):
        """Return private constructor arguments. Never store these in public state."""
        # Callers may pass the resolved public selection, which contains a trusted URL.
        value = {key: selection.get(key) for key in ('model', 'provider', 'reasoning_effort')}
        _selection(value)
        profile_home = str(self._profile_home(bot_id)) if bot_id else None
        return self._call('runtime', selection=value, profile_home=profile_home)

    def _profile_home(self, bot_id):
        if not isinstance(bot_id, str) or str(UUID(bot_id)) != bot_id:
            raise ModelCatalogError('The Hermes bot profile ID is invalid.')
        return _contained(self.data_dir / 'bots' / bot_id / 'home', self.data_dir)

    def profile_signature(self, bot_id):
        """Identify the files that native_settings.profile_state reads for this bot, with the default profile files."""
        home = self._profile_home(bot_id)
        files = []
        for name in ('profile.yaml', 'config.yaml', 'SOUL.md'):
            try:
                state = _contained(home / name, self.data_dir).stat()
                files.append((name, state.st_ino, state.st_mtime_ns, state.st_size))
            except FileNotFoundError:
                files.append((name, None))
        return self._signature(), tuple(files)

    def routines_signature(self, bot_id):
        """Identify the files that native_routines reads for this bot, with the default profile files.

        Hermes keeps the jobs in cron/jobs.json and their runs in cron/executions.db. Coconut keeps its claim and
        completion receipts in coconut-runs. The settings set the time zone and the routine model.
        """
        home = self._profile_home(bot_id)
        receipts = _contained(home / 'coconut-runs', self.data_dir)
        names = ['cron/jobs.json', 'cron/executions.db', 'cron/executions.db-wal', 'config.yaml', '.env']
        names += sorted('coconut-runs/' + path.name for path in receipts.glob('*.json'))
        files = []
        for name in names:
            try:
                state = _contained(home / name, self.data_dir).stat()
                files.append((name, state.st_ino, state.st_mtime_ns, state.st_size))
            except FileNotFoundError:
                files.append((name, None))
        return self._signature(), tuple(files)

    def read_profile(self, bot_id):
        return self.read_profiles([bot_id])[bot_id]

    def read_profiles(self, bot_ids):
        homes = {bot_id: self._profile_home(bot_id) for bot_id in bot_ids}
        changed = {}
        with self._lock:
            try:
                global_signature = self._signature()
            except (OSError, ValueError):
                global_signature = None
            result = {}
            for bot_id, home in homes.items():
                path = _contained(home / 'config.yaml', self.data_dir)
                stat = path.stat() if path.exists() else None
                signature = (global_signature, (stat.st_mtime_ns, stat.st_size, stat.st_ino) if stat else None)
                cached = self._profile_cache.get(bot_id)
                if cached and cached[0] == signature and (cached[2] is None or time.monotonic() < cached[2]):
                    result[bot_id] = deepcopy(cached[1])
                else:
                    changed[bot_id] = {'home': str(home), 'signature': signature}
            if changed:
                try:
                    values = self._call('read_profiles', homes={key: row['home'] for key, row in changed.items()})
                except ModelCatalogError:
                    # A busy Mac can delay the settings process, so a failed read is kept only briefly.
                    values = {key: {'model_override': None, 'effective_model': None,
                                    'error': 'Hermes profile settings are unavailable. Check this profile in Terminal.'}
                              for key in changed}
                    retry_at = time.monotonic() + PROFILE_RETRY_SECONDS
                else:
                    retry_at = None
                for bot_id, value in values.items():
                    self._profile_cache[bot_id] = (changed[bot_id]['signature'], value, retry_at)
                    result[bot_id] = deepcopy(value)
            return result

    def set_profile(self, bot_id, selection):
        value = _selection(selection) if selection is not None else None
        catalog = self.get_for_validation() if value is not None else self.get()
        self.resolve(value, catalog)
        result = self._call('set_profile', profile_home=str(self._profile_home(bot_id)),
                            selection=value, catalog=catalog)
        with self._lock:
            self._profile_cache.pop(bot_id, None)
        return result

    def migrate_legacy_default(self, model, base_url=None):
        """Import one existing Coconut selection only when Hermes has no config file."""
        with self._lock:
            if (native_home(self.root) / 'config.yaml').exists() or not model:
                return False
            catalog = self.get()
            matches = [item for item in catalog['models'] if item['id'] == model
                       and (not base_url or item.get('base_url') == public_endpoint(base_url))]
            if len(matches) != 1:
                return False
            self._call('migrate', selection={'model': model, 'provider': matches[0]['provider'],
                                           'reasoning_effort': None}, catalog=catalog)
            self._cache = None
            return True

    def _call(self, operation, **payload):
        if not self.python or not Path(self.python).is_file() or not (self.source / 'run_agent.py').is_file():
            raise ModelCatalogError('Install Hermes on this Mac before choosing a model.')
        if (self.source / '.env').exists() or (self.source / '.env').is_symlink():
            raise ModelCatalogError('Use a clean Hermes checkout without a shared .env file.')
        _contained(self.root, self.home)
        _contained(self.private_home, self.home)
        for path in (self.root, self.private_home, self.private_home / 'tmp'):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = dict(self.env)
        from .runtime_environment import prepare_profile
        environment.update(prepare_profile(self.private_home))
        from .hermes_runtime import NO_LAZY_INSTALLS
        environment.update(NO_LAZY_INSTALLS)
        environment.update(HERMES_HOME=str(self.root), PYTHONPATH=str(self.source),
                           TMPDIR=str(self.private_home / 'tmp'), HERMES_DISABLE_TELEMETRY='1',
                           PYTHONUNBUFFERED='1')
        if operation == 'native' and payload.get('native_operation', '').startswith('routine_'):
            # Coconut retains job ownership after the settings subprocess exits.
            environment['HERMES_MACHINE_ID'] = 'coconut-' + hashlib.sha256(str(self.data_dir).encode()).hexdigest()[:24]
        try:
            with ExitStack() as files:
                request = dict(operation=operation, data_root=str(self.data_dir), **payload)
                text_field = NATIVE_TEXT_FIELDS.get(payload.get('native_operation')) if operation == 'native' else None
                if text_field:
                    # Completed results can exceed the settings request bound.
                    output = files.enter_context(tempfile.NamedTemporaryFile(
                        mode='w', encoding='utf-8', dir=environment['TMPDIR'], prefix='coconut-native-'))
                    output.write(request.pop(text_field, '') or '')
                    output.flush()
                    request['native_text_file'] = Path(output.name).name
                serialized = json.dumps(request, ensure_ascii=False)
                if len(serialized) > MAX_REQUEST_CHARACTERS:
                    raise ModelCatalogError('The Hermes settings request is too large.')
                result = subprocess.run([self.python, str(Path(__file__).resolve()), '--worker'],
                                        input=serialized, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                        env=environment, cwd=self.private_home,
                                        # A turn reads its bot's profile first. Under load, this read can take seconds.
                                        timeout=30 if operation in ('get', 'runtime', 'native', 'read_profiles') else 5)
            # The snapshot combines settings and routines, each with a separate response budget.
            response_limit = 8_000_000 if operation == 'native' and payload.get('native_operation') == 'snapshot_read' else 4_000_000
            if result.returncode or len(result.stdout) > response_limit:
                raise ModelCatalogError('Hermes could not read its model settings.')
            response = json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            raise ModelCatalogError('Hermes model discovery timed out. Try refreshing the list.') from None
        except (OSError, json.JSONDecodeError):
            raise ModelCatalogError('Hermes could not read its model settings.') from None
        if not isinstance(response, dict):
            raise ModelCatalogError('Hermes returned an invalid model catalog.')
        if response.get('error'):
            messages = {
                'invalid_selection': 'Select an available model and effort from the Hermes list.',
                'unsafe_endpoint': 'Use a separate credential field and a secure endpoint in Hermes settings.',
                'invalid_config': 'Fix the native Hermes config before changing its model.',
                'auth': 'Reconnect this provider with hermes model in Terminal.',
                'unsupported_route': UNSUPPORTED_ROUTE_MESSAGE,
            }
            raise ModelCatalogError(messages.get(response['error'], 'Hermes could not read its model settings.'))
        return response['result']


def _effort(config):
    if config is None:
        return None
    return 'none' if config.get('enabled') is False else config.get('effort')


def unavailable_catalog():
    return {'default': {'model': '', 'provider': '', 'base_url': None, 'reasoning_effort': None,
                        'configured': False}, 'models': [], 'available': False,
            'default_configured': False, 'model_source': 'Hermes', 'default_source': 'Hermes config.yaml',
            'error': 'Hermes model settings are unavailable. Check Hermes in Terminal.'}


def _native_state():
    from hermes_cli.config import load_config, require_readable_config_before_write
    # The strict read prevents upstream recovery from hiding a broken user configuration.
    require_readable_config_before_write()
    config = load_config()
    model = config.get('model') or {}
    if isinstance(model, str):
        model = {'default': model}
    from hermes_cli.config import split_model_config_default
    model_id, nested_provider = split_model_config_default(model.get('default'))
    provider = str(model.get('provider') or nested_provider or '').strip()
    if not provider or provider == 'auto':
        from hermes_cli.auth import resolve_provider
        provider = resolve_provider(provider or 'auto')
    if not model_id:
        from hermes_cli.models import get_default_model_for_provider
        model_id = get_default_model_for_provider(provider)
    try:
        base_url = public_endpoint(model.get('base_url'))
    except ModelCatalogError:
        if provider in SUPPORTED_PROVIDERS:
            raise
        base_url = None
    return config, model_id, provider, base_url


def _sent_effort(effort, supported):
    """Return the effort that Hermes sends for a saved effort on a route with these levels.

    Hermes clamps an unsupported level to the nearest weaker one. A saved disable stays 'none': this value also
    reaches the model call, where 'none' omits the reasoning field and None would request Hermes' default effort.
    """
    from agent.reasoning_effort import clamp_effort
    if effort is None or effort == 'none' or effort in supported:
        return effort
    return clamp_effort(effort, supported)


def _route_efforts(provider, model_id):
    """Return the efforts a route narrows to, or None when it accepts the whole Hermes ladder."""
    if provider == 'openai-codex':
        from agent.reasoning_effort import codex_supported_efforts
        return list(codex_supported_efforts(model_id))
    if provider == 'xai-oauth':
        return _xai_efforts(model_id)
    return None


def _xai_efforts(model_id):
    """Return the efforts that Hermes sends to xAI for this model. Grok 4.6 accepts xhigh, and older Grok tops out at high."""
    from agent.model_metadata import is_grok_46_family
    from agent.reasoning_effort import XAI_GROK46_EFFORTS, XAI_LEGACY_EFFORTS
    return list(XAI_GROK46_EFFORTS if is_grok_46_family(model_id) else XAI_LEGACY_EFFORTS)


def _native_rows(config, model, provider, base_url, refresh=False):
    from hermes_cli.model_switch_providers import list_authenticated_providers
    return list_authenticated_providers(
        current_provider=provider, current_model=model, current_base_url=base_url or '',
        user_providers=config.get('providers') or {}, custom_providers=config.get('custom_providers') or [],
        excluded_providers=config.get('excluded_providers') or [], refresh=refresh,
        probe_custom_providers=False, probe_current_custom_provider=False, for_picker=True)


def _supported_route(config, provider, model, base_url=None):
    if provider not in SUPPORTED_PROVIDERS:
        return False
    candidates = []
    configured = config.get('model')
    if isinstance(configured, dict) and configured.get('provider') == provider:
        candidates.append(configured)
    providers = config.get('providers')
    if isinstance(providers, dict) and isinstance(providers.get(provider), dict):
        candidates.append(providers[provider])
    # Hermes also applies endpoint-specific headers from saved custom definitions.
    for entry in config.get('custom_providers') or []:
        if isinstance(entry, dict) and base_url and str(entry.get('base_url') or entry.get('url') or '').rstrip('/') == base_url.rstrip('/'):
            candidates.append(entry)
    for candidate in candidates:
        if any(candidate.get(key) for key in (*UNSUPPORTED_RUNTIME_FIELDS, 'headers', 'default_headers')):
            return False
        models = candidate.get('models')
        details = models.get(model) if isinstance(models, dict) else None
        if isinstance(details, dict) and any(isinstance(value, bool) for value in details.values()):
            return False
    return True


def _catalog(config, model, provider, base_url, rows):
    from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort, resolve_reasoning_config
    from hermes_cli.auth import PROVIDER_REGISTRY
    efforts = [value for value in ('none', *VALID_REASONING_EFFORTS) if parse_reasoning_effort(value) is not None]
    items, seen = [], set()
    per_model_config = {'agent': {'reasoning_overrides': (config.get('agent') or {}).get('reasoning_overrides') or {}}}
    registration = PROVIDER_REGISTRY.get(provider)
    default_url = base_url or (public_endpoint(registration.inference_base_url) if registration else None)
    if provider not in SUPPORTED_PROVIDERS:
        try:
            default_url = public_endpoint(default_url)
        except ModelCatalogError:
            default_url = None
    for row in rows:
        slug = row['slug']
        if slug not in SUPPORTED_PROVIDERS:
            continue
        registration = PROVIDER_REGISTRY.get(slug)
        url = row.get('api_url') or (base_url if slug == provider else None)
        if not url and registration:
            url = os.environ.get(registration.base_url_env_var) if registration.base_url_env_var else None
            url = url or registration.inference_base_url
        url = public_endpoint(url)
        if slug == provider and not default_url:
            default_url = url
        for model_id in row.get('models') or []:
            if not isinstance(model_id, str) or not model_id or (slug, model_id) in seen:
                continue
            if not _supported_route(config, slug, model_id, url):
                continue
            seen.add((slug, model_id))
            supported = _route_efforts(slug, model_id)
            # An empty route list means the route takes no effort. Only None widens to the whole ladder.
            supported = list(efforts) if supported is None else supported
            items.append({'id': model_id, 'name': model_id, 'provider': slug, 'base_url': url,
                          'reasoning_efforts': supported,
                          'default_reasoning_effort': _sent_effort(
                              _effort(resolve_reasoning_config(per_model_config, model_id)), supported),
                          'configured': True, 'source': row.get('source', 'hermes')})
    configured = any(item['id'] == model and item['provider'] == provider for item in items)
    supported_default = _supported_route(config, provider, model, default_url)
    default_effort = _effort(resolve_reasoning_config(config, model))
    entry = next((item for item in items if item['id'] == model and item['provider'] == provider), None)
    if entry is not None:
        # Show the effort that the default route sends, as for each listed model.
        default_effort = _sent_effort(default_effort, entry['reasoning_efforts'])
    return {'default': {'model': model, 'provider': provider, 'base_url': default_url,
                        'reasoning_effort': default_effort,
                        'configured': configured, 'supported': supported_default,
                        **({'unavailable_reason': UNSUPPORTED_ROUTE_MESSAGE} if not supported_default else {})},
            'models': items, 'model_source': 'hermes_cli.model_switch_providers.list_authenticated_providers',
            'default_source': 'Hermes config.yaml',
            'effort_source': 'Hermes native reasoning settings',
            'available': bool(model and provider),
            'default_configured': configured}


def _write_default(selection, catalog):
    from hermes_cli.config import (clear_model_endpoint_credentials,
                                   get_config_path, require_readable_config_before_write)
    from utils import atomic_roundtrip_yaml_save
    from hermes_constants import _canonical_model_variants
    selected = resolve_selection(selection, catalog)
    path = get_config_path()
    before = path.read_bytes() if path.exists() else None
    raw = require_readable_config_before_write(path)
    model = raw.get('model')
    model = dict(model) if isinstance(model, dict) else {}
    previous = catalog['default']
    if (previous['provider'] != selected['provider'] or previous.get('base_url') != selected.get('base_url')
            or not (model.get('default') or model.get('model') or model.get('name'))):
        clear_model_endpoint_credentials(model, clear_base_url=True)
        for key in ('headers', 'extra_headers'):
            model.pop(key, None)
    model['default'], model['provider'] = selected['model'], selected['provider']
    model.pop('model', None)
    model.pop('name', None)
    if selected.get('base_url'):
        model['base_url'] = selected['base_url']
    else:
        model.pop('base_url', None)
    raw['model'] = model
    for key in ('provider', 'base_url', 'api_base'):
        raw.pop(key, None)
    agent = dict(raw.get('agent') or {})
    overrides = dict(agent.get('reasoning_overrides') or {})
    for variant in _canonical_model_variants(selected['model']):
        overrides.pop(variant, None)
    if overrides:
        agent['reasoning_overrides'] = overrides
    else:
        agent.pop('reasoning_overrides', None)
    if selection.get('reasoning_effort') is None:
        agent.pop('reasoning_effort', None)
    else:
        agent['reasoning_effort'] = selection['reasoning_effort']
    raw['agent'] = agent
    if (path.read_bytes() if path.exists() else None) != before:
        raise ModelCatalogError('Hermes settings changed. Refresh before saving again.')
    atomic_roundtrip_yaml_save(path, raw)


def _read_profile(home, inherited):
    from hermes_cli.config import require_readable_config_before_write, _normalize_root_model_keys
    from hermes_constants import resolve_reasoning_config
    raw = require_readable_config_before_write(Path(home) / 'config.yaml')
    normalized = _normalize_root_model_keys(deepcopy(raw))
    model_cfg = normalized.get('model') or {}
    if isinstance(model_cfg, str):
        model_cfg = {'default': model_cfg}
    model = model_cfg.get('default')
    effort = _effort(resolve_reasoning_config(normalized, model or inherited['model']))
    if not model and effort is None:
        return {'model_override': None, 'effective_model': None}
    provider = model_cfg.get('provider') or inherited['provider']
    if provider == 'auto':
        provider = inherited['provider']
    model = model or inherited['model']
    base_url = public_endpoint(model_cfg.get('base_url'))
    if not base_url:
        from hermes_cli.auth import PROVIDER_REGISTRY
        registration = PROVIDER_REGISTRY.get(provider)
        base_url = inherited.get('base_url') if provider == inherited['provider'] else None
        base_url = base_url or (public_endpoint(registration.inference_base_url) if registration else None)
    supported = _route_efforts(provider, model)
    if supported:
        # A bot override shows the effort that its route sends, as the catalog does.
        effort = _sent_effort(effort, supported)
    selection = {'model': model, 'provider': provider, 'reasoning_effort': effort}
    return {'model_override': selection, 'effective_model': dict(selection, base_url=base_url)}


def _write_profile(home, selection, catalog):
    from hermes_cli.config import require_readable_config_before_write
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from utils import atomic_roundtrip_yaml_save
    path = Path(home) / 'config.yaml'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = set_hermes_home_override(home)
    try:
        if selection is not None:
            previous = _read_profile(home, catalog['default'])['effective_model'] or catalog['default']
            profile_catalog = dict(catalog, default=previous)
            _write_default(selection, profile_catalog)
        else:
            before = path.read_bytes() if path.exists() else None
            raw = require_readable_config_before_write(path)
            model = raw.get('model')
            if isinstance(model, dict):
                for key in ('default', 'model', 'name', 'provider', 'base_url', 'api_base'):
                    model.pop(key, None)
                if not model:
                    raw.pop('model', None)
            else:
                raw.pop('model', None)
            for key in ('provider', 'base_url', 'api_base'):
                raw.pop(key, None)
            agent = raw.get('agent')
            if isinstance(agent, dict):
                agent.pop('reasoning_effort', None)
                agent.pop('reasoning_overrides', None)
                if not agent:
                    raw.pop('agent', None)
            if (path.read_bytes() if path.exists() else None) != before:
                raise ModelCatalogError('Hermes settings changed. Refresh before saving again.')
            atomic_roundtrip_yaml_save(path, raw)
        return _read_profile(home, catalog['default'])
    finally:
        reset_hermes_home_override(token)


def _native_runtime(selection, profile_home=None, canonical_home=None, canonical_root=None):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import load_config
    config = load_config()
    current_model = config.get('model') or {}
    current_url = current_model.get('base_url') if isinstance(current_model, dict) else None
    if not _supported_route(config, selection['provider'], selection['model'], current_url):
        raise UnsupportedModelRoute(UNSUPPORTED_ROUTE_MESSAGE)
    token = None
    if profile_home:
        from hermes_cli import auth
        from dotenv import dotenv_values
        for key, value in dotenv_values(Path(profile_home) / '.env').items():
            if value is not None and key not in ('HOME', 'USER', 'HERMES_HOME', 'HERMES_REAL_HOME', 'PYTHONPATH',
                                               'CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR'):
                os.environ[key] = value
        # The native transaction writes refreshed OAuth tokens back to their source file.
        candidates = [Path(canonical_home) / 'auth.json', Path(canonical_root) / 'auth.json']
        fallback = next((path for path in candidates
                         if auth._provider_state_in(auth._load_auth_store(path), selection['provider']) is not None),
                        candidates[0])
        if selection['provider'] == 'anthropic':
            from agent import anthropic_credentials
            pkce = next((path.parent / '.anthropic_oauth.json' for path in candidates
                         if (path.parent / '.anthropic_oauth.json').is_file()), None)
            if pkce is not None:
                fallback = pkce.parent / 'auth.json'
                anthropic_credentials._root_hermes_oauth_file = lambda: pkce
        auth._global_auth_file_path = lambda: fallback
        token = set_hermes_home_override(profile_home)
    from hermes_cli.runtime_provider import resolve_runtime_provider
    # Native resolution owns credentials, OAuth refresh, aliases, and endpoint binding.
    try:
        config = load_config()
        current_model = config.get('model') or {}
        current_url = current_model.get('base_url') if isinstance(current_model, dict) else None
        if not _supported_route(config, selection['provider'], selection['model'], current_url):
            raise UnsupportedModelRoute(UNSUPPORTED_ROUTE_MESSAGE)
        runtime = resolve_runtime_provider(requested=selection['provider'], target_model=selection['model'])
    finally:
        if token is not None:
            reset_hermes_home_override(token)
    if (runtime.get('provider') not in SUPPORTED_PROVIDERS or callable(runtime.get('api_key'))
            or any(runtime.get(key) for key in UNSUPPORTED_RUNTIME_FIELDS)):
        raise UnsupportedModelRoute(UNSUPPORTED_ROUTE_MESSAGE)
    result = {key: value for key, value in runtime.items() if key in RUNTIME_KEYS}
    if not result.get('provider') or not result.get('api_mode'):
        raise ModelCatalogError('Hermes did not resolve this provider.')
    if 'command' in result:
        result['acp_command'] = result.pop('command')
    if 'args' in result:
        result['acp_args'] = result.pop('args')
    # Functions and credential pools cannot cross the private JSON transport.
    json.dumps(result)
    return result


def _worker(request):
    root = Path(os.environ['HERMES_HOME'])
    selected_home = native_home(root)
    # Native profile resolution also checks deleted profiles and validates names.
    from hermes_cli.profiles import resolve_profile_env
    expected = resolve_profile_env(selected_home.name if selected_home != root else 'default')
    if Path(expected) != selected_home:
        raise ModelCatalogError('The active Hermes profile is invalid.')
    os.environ['HERMES_HOME'] = str(selected_home)
    for name in ('config.yaml', '.env', 'auth.json', '.anthropic_oauth.json'):
        _contained(selected_home / name, root)
        _contained(root / name, root)
    # Read profile variables without the native CLI's optional file-format repairs.
    from dotenv import dotenv_values
    for key, value in dotenv_values(selected_home / '.env').items():
        if value is not None and key not in ('HOME', 'USER', 'HERMES_HOME', 'HERMES_REAL_HOME', 'PYTHONPATH',
                                           'CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR'):
            os.environ[key] = value
    if request['operation'] == 'native':
        from native_settings import worker
        return worker(dict(request, settings_root=str(root)), selected_home)
    if request['operation'] == 'runtime':
        profile = request.get('profile_home')
        if profile:
            profile = _contained(profile, request['data_root'])
            for name in ('config.yaml', '.env', 'auth.json', '.anthropic_oauth.json'):
                _contained(profile / name, request['data_root'])
            # A profile with no selection uses native global routing and credentials.
            config, model, provider, base_url = _native_state()
            inherited = _catalog(config, model, provider, base_url, [])['default']
            if _read_profile(profile, inherited)['model_override'] is None:
                profile = None
        return _native_runtime(_selection(request['selection']), profile, selected_home, root)
    config, model, provider, base_url = _native_state()
    if request['operation'] in ('read_profiles', 'set_profile', 'native'):
        inherited = _catalog(config, model, provider, base_url, [])['default']
        if request['operation'] == 'read_profiles':
            result = {}
            for bot_id, home in request['homes'].items():
                home = _contained(home, request['data_root'])
                _contained(home / 'config.yaml', request['data_root'])
                try:
                    result[bot_id] = _read_profile(home, inherited)
                except Exception:
                    result[bot_id] = {'model_override': None, 'effective_model': None,
                                      'error': 'The Hermes profile config is invalid. Fix it in Terminal.'}
            return result
        home = _contained(request['profile_home'], request['data_root'])
        _contained(home / 'config.yaml', request['data_root'])
        catalog = dict(request['catalog'], default=inherited)
        return _write_profile(home, request['selection'], catalog)
    if request['operation'] in ('set_default', 'migrate'):
        supplied = request.get('catalog')
        if not supplied:
            raise ModelCatalogError('Refresh the Hermes model list before saving.')
        catalog = dict(supplied, default=_catalog(config, model, provider, base_url, [])['default'])
        if request['operation'] == 'migrate' and (selected_home / 'config.yaml').exists():
            return catalog
        _write_default(_selection(request['selection']), catalog)
        config, model, provider, base_url = _native_state()
        result = _catalog(config, model, provider, base_url, [])
        result['models'] = supplied['models']
        result['default_configured'] = result['default']['configured'] = True
        return result
    rows = [] if request['operation'] == 'defaults' else _native_rows(config, model, provider, base_url, request.get('refresh', False))
    catalog = _catalog(config, model, provider, base_url, rows)
    return catalog


def main():
    output = sys.stdout
    request = {}
    try:
        serialized = sys.stdin.read(MAX_REQUEST_CHARACTERS + 1)
        if len(serialized) > MAX_REQUEST_CHARACTERS:
            raise ValueError('Request too large')
        request = json.loads(serialized)
        if request.get('operation') not in ('get', 'defaults', 'set_default', 'runtime', 'migrate', 'read_profiles', 'set_profile', 'native'):
            raise ValueError('Invalid operation')
        if 'native_text_file' in request:
            name = request.pop('native_text_file')
            field = NATIVE_TEXT_FIELDS.get(request.get('native_operation'))
            if (request['operation'] != 'native' or not field
                    or not isinstance(name, str) or Path(name).name != name or not name.startswith('coconut-native-')
                    or field in request):
                raise ValueError('Invalid native text file')
            directory = Path(os.environ['TMPDIR'])
            request[field] = _contained(directory / name, directory).read_text(encoding='utf-8')
        with open(os.devnull, 'w') as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            result = _worker(request)
        response = {'result': result}
    except Exception as error:
        # Native auth errors can contain tokens and private URLs. Never serialize their text.
        code = 'auth' if request.get('operation') == 'runtime' else 'invalid_config'
        if isinstance(error, UnsupportedModelRoute):
            code = 'unsupported_route'
        elif isinstance(error, ModelCatalogError):
            code = 'unsafe_endpoint' if 'endpoint' in str(error) else 'invalid_selection'
        response = {'error': code}
    output.write(json.dumps(response) + '\n')
    output.flush()


if __name__ == '__main__':
    # Python puts this folder first on the path. Coconut's providers.py would then hide the Hermes providers
    # package, which Hermes imports while it lists models. Keep this folder last, so Hermes modules win and
    # Coconut siblings such as native_settings still import.
    if sys.path and Path(sys.path[0] or '.').resolve() == Path(__file__).resolve().parent:
        sys.path.append(sys.path.pop(0))
    main()
