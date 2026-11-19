"""Phone OAuth with native gog token storage."""
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .profiles import _safe, _write
from .runtime_environment import profile_environment
from .google_cli import VERSION as GOG_VERSION

# Public publisher identity. Google registers this client for com.gumroad.coconut.
CLIENT_ID = '3509104299-du1m1pcfgm7meeivk712bmltf4bus8ae.apps.googleusercontent.com'
SCOPES = ['openid', 'email', 'https://www.googleapis.com/auth/gmail.readonly',
          'https://www.googleapis.com/auth/calendar.readonly']
# The support mailbox is a second connection, Coconut's own: the support page answers and closes conversations with
# it (support_threads.py). The connection above stays read-only.
SUPPORT_SCOPES = ['openid', 'email', 'https://www.googleapis.com/auth/gmail.modify']
# Each connection: its folder under connections/, its scopes, and the services that it gives.
MAILBOXES = {'': ('google', SCOPES, ('gmail', 'calendar')), 'support': ('google-support', SUPPORT_SCOPES, ('gmail',))}
TOKEN_URL = 'https://oauth2.googleapis.com/token'
USERINFO_URL = 'https://openidconnect.googleapis.com/v1/userinfo'


class GoogleError(ValueError):
    pass


class GoogleAuthorizationError(GoogleError):
    pass


def request_json(url, fields=None, access_token=None):
    headers = {'Accept': 'application/json'}
    if access_token:
        headers['Authorization'] = 'Bearer ' + access_token
    data = urlencode(fields).encode() if fields is not None else None
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=15) as response:
            result = json.loads(response.read(131073))
        if not isinstance(result, dict):
            raise GoogleError('Google returned an invalid response. Try connecting again.')
        return result
    except HTTPError as error:
        if error.code in (400, 401, 403):
            raise GoogleAuthorizationError('Google could not authorize this connection. Connect Google again.') from None
        raise GoogleError('Google is unavailable. Try again shortly.') from None
    except (URLError, TimeoutError, ValueError):
        raise GoogleError('Google did not complete this request. Try again shortly.') from None


def read_json(path):
    _safe(path, path.parent)
    return json.loads(path.read_text()) if path.exists() else {}


def write_json(path, value):
    _write(path, json.dumps(value), path.parent)
    path.chmod(0o600)


class GoogleProfile:
    def __init__(self, home, executable=None, services=('gmail', 'calendar')):
        self.home = Path(home)
        self.services = list(services)
        self.root = self.home / '.google'
        _safe(self.root, self.home)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.tools = self.home.parent.parent.parent / 'google-tools' / GOG_VERSION
        self.executable = executable or (str(self.tools / 'gog') if (self.tools / 'gog').is_file() else None)

    @contextmanager
    def locked(self):
        path = self.root / 'lock'
        _safe(path, self.home)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def environment(self):
        path = self.root / 'keyring-password'
        _safe(path, self.home)
        if not path.exists():
            _write(path, secrets.token_urlsafe(48), self.home)
        path.chmod(0o600)
        env = dict(os.environ, **profile_environment(self.home))
        env.update(GOG_KEYRING_BACKEND='file', GOG_KEYRING_PASSWORD=path.read_text(),
                   GOG_HOME=str(self.root / 'gog'))
        for key in ('GOG_ACCESS_TOKEN', 'GOG_ACCOUNT', 'GOG_CLIENT'):
            env.pop(key, None)
        return env

    def run(self, args, payload=None):
        if not self.executable:
            raise GoogleError('The Google CLI is unavailable on your Mac.')
        try:
            result = subprocess.run([self.executable, '--client', 'coconut-ios', '--no-input', *args],
                                    input=json.dumps(payload) if payload is not None else None,
                                    capture_output=True, text=True, env=self.environment(), timeout=12)
        except (OSError, subprocess.TimeoutExpired):
            raise GoogleError('The Mac could not access Google credentials. Try again.') from None
        if result.returncode:
            raise GoogleError('The Mac could not save or read the Google connection. Try again.')
        return result.stdout

    def save_tokens(self, tokens):
        self.run(['auth', 'tokens', 'import', '-'], tokens)

    def tokens(self, email):
        # gog exports only to a file. Keep that file private and remove it immediately.
        fd, name = tempfile.mkstemp(prefix='.token-', dir=self.root)
        os.close(fd)
        path = Path(name)
        try:
            self.run(['auth', 'tokens', 'export', email, '--out', name, '--overwrite'])
            return read_json(path)
        finally:
            path.unlink(missing_ok=True)

    def status(self):
        saved = read_json(self.root / 'connection.json')
        return dict(connected=bool(saved.get('email')), email=saved.get('email'),
                    needs_reconnect=bool(saved.get('needs_reconnect')), services=self.services)

    def disconnect(self):
        saved = read_json(self.root / 'connection.json')
        if saved.get('email'):
            self.run(['auth', 'remove', saved['email'], '--force'])
        write_json(self.root / 'connection.json', {})

    def access(self):
        with self.locked():
            saved = read_json(self.root / 'connection.json')
            if not saved.get('email'):
                raise GoogleError('Connect Google in Coconut Settings → Connections.')
            tokens = self.tokens(saved['email'])
            expires = tokens.get('access_token_expires_at', '')
            deadline = datetime.fromisoformat(expires.replace('Z', '+00:00')).timestamp() if expires else 0
            if deadline <= time.time() + 120:
                try:
                    result = request_json(TOKEN_URL, dict(client_id=saved['client_id'], grant_type='refresh_token', refresh_token=tokens['refresh_token']))
                except GoogleAuthorizationError:
                    saved['needs_reconnect'] = True
                    write_json(self.root / 'connection.json', saved)
                    raise
                if not result.get('access_token'):
                    raise GoogleError('Google did not return an access token. Reconnect in Coconut Settings.')
                tokens.update(access_token=result['access_token'], access_token_expires_at=expiry(result))
                if result.get('refresh_token'):
                    tokens['refresh_token'] = result['refresh_token']
                self.save_tokens(tokens)
                if saved.pop('needs_reconnect', None):
                    write_json(self.root / 'connection.json', saved)
            return tokens['access_token'], saved['email']


def expiry(result):
    return datetime.fromtimestamp(time.time() + int(result.get('expires_in', 3600)), timezone.utc).isoformat()


class GoogleConnections:
    def __init__(self, root, client_id=None, profile_factory=GoogleProfile, mailbox=''):
        self.root = Path(root)
        self.client_id = CLIENT_ID if client_id is None else client_id
        self.profile_factory = profile_factory
        if mailbox not in MAILBOXES:
            raise GoogleError('Choose the Google connection or the support mailbox.')
        self.folder, self.scopes, self.services = MAILBOXES[mailbox]

    def profile(self):
        return self.profile_factory(self.root / 'connections' / self.folder / 'home', services=self.services)

    def start(self, device, key):
        if not self.client_id:
            raise GoogleError('Google sign-in is not configured for this Coconut release.')
        profile = self.profile()
        with profile.locked():
            if not profile.executable:
                try:
                    from .google_cli import install
                    profile.executable = install(profile.tools)
                except (OSError, ValueError, URLError, TimeoutError):
                    raise GoogleError('Google tools could not finish installing. Try connecting again.') from None
            old = read_json(profile.root / 'pending.json')
            if old.get('key') == key and old.get('device') == device and old.get('expires', 0) > time.time():
                return old['response']
            verifier = secrets.token_urlsafe(48)
            state = secrets.token_urlsafe(32)
            scheme = '.'.join(reversed(self.client_id.split('.')))
            redirect = scheme + ':/oauthredirect'
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(dict(
                client_id=self.client_id, redirect_uri=redirect, response_type='code', scope=' '.join(self.scopes),
                state=state, code_challenge=challenge, code_challenge_method='S256', access_type='offline', prompt='consent select_account'))
            response = dict(authorization_url=url, callback_scheme=scheme, state=state)
            write_json(profile.root / 'pending.json', dict(key=key, device=device, expires=time.time() + 900,
                verifier=verifier, state=state, redirect=redirect, client_id=self.client_id, response=response))
            return response

    def finish(self, device, state, code):
        if not isinstance(state, str) or not isinstance(code, str) or not state or not code or len(code) > 4096:
            raise GoogleError('The Google sign-in response is invalid.')
        profile = self.profile()
        with profile.locked():
            saved = read_json(profile.root / 'connection.json')
            signature = hashlib.sha256((device + '\0' + state + '\0' + code).encode()).hexdigest()
            if saved.get('receipt') == signature:
                return profile.status()
            pending = read_json(profile.root / 'pending.json')
            if pending.get('device') != device or pending.get('expires', 0) <= time.time() or not hmac.compare_digest(pending.get('state', ''), state):
                raise GoogleError('This Google sign-in expired. Connect Google again.')
            # A one-use code cannot be retried after an uncertain token exchange.
            if pending.get('exchanging'):
                raise GoogleError('Google sign-in was interrupted. Connect Google again.')
            pending['exchanging'] = True
            write_json(profile.root / 'pending.json', pending)
            result = request_json(TOKEN_URL, dict(client_id=pending['client_id'], code=code,
                code_verifier=pending['verifier'], redirect_uri=pending['redirect'], grant_type='authorization_code'))
            if not result.get('access_token') or not result.get('refresh_token'):
                raise GoogleError('Google did not grant background access. Connect Google again.')
            required = set(self.scopes[2:])
            granted = set(result.get('scope', '').split())
            if not required.issubset(granted):
                raise GoogleError('Allow both Gmail and Calendar read access to connect.' if self.scopes == SCOPES
                                  else 'Allow Coconut to read, send and label the mail of this account to connect it.')
            identity = request_json(USERINFO_URL, access_token=result['access_token'])
            if identity.get('email_verified') is not True or not isinstance(identity.get('email'), str) or not identity.get('sub'):
                raise GoogleError('Google could not verify this account.')
            email = identity['email']
            if saved.get('email') and saved['email'] != email:
                profile.disconnect()
            profile.save_tokens(dict(email=email, subject=identity['sub'], client='coconut-ios', services=self.services,
                scopes=sorted(granted), refresh_token=result['refresh_token'], access_token=result['access_token'], access_token_expires_at=expiry(result)))
            write_json(profile.root / 'connection.json', dict(email=email, client_id=pending['client_id'], receipt=signature))
            (profile.root / 'pending.json').unlink(missing_ok=True)
            return profile.status()

    def disconnect(self, device='', key=None):
        profile = self.profile()
        with profile.locked():
            receipt = None
            if key:
                folder = profile.root / 'receipts'
                _safe(folder, profile.home)
                folder.mkdir(mode=0o700, exist_ok=True)
                receipt = folder / (hashlib.sha256((device + '\0' + key).encode()).hexdigest() + '.json')
                if receipt.exists():
                    return read_json(receipt)
            profile.disconnect()
            (profile.root / 'pending.json').unlink(missing_ok=True)
            result = profile.status()
            if receipt:
                write_json(receipt, result)
            return result
