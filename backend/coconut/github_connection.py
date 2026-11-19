"""One shared GitHub login for all bots, made with GitHub's device sign-in from the phone.

Other GitHub logins on the Mac stay untouched and unused. Coconut does not run gh auth commands: they read and change the
account's shared Keychain entry for github.com, even with insecure storage. Coconut keeps its own token in a private
file. Bots get it through GH_TOKEN, and git gets it through a command-scope credential helper in the environment.
Nothing writes to ~/.config/gh, ~/.gitconfig or the Keychain.
"""
from pathlib import Path
import json
import os
import shlex
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Public publisher identity of the Coconut GitHub OAuth App, with device flow on. COCONUT_GITHUB_CLIENT_ID overrides it.
CLIENT_ID = 'Ov23li1jKgyuttHBCFoW'
SCOPES = 'repo read:org workflow'
DEVICE_CODE_URL = 'https://github.com/login/device/code'
TOKEN_URL = 'https://github.com/login/oauth/access_token'
USER_URL = 'https://api.github.com/user'
TOKEN_VARIABLES = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')


class GitHubError(ValueError):
    pass


def folder(root):
    return Path(root) / 'connections' / 'github'


def token_path(root):
    return folder(root) / 'token'


def request_json(url, fields=None, token=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'Coconut'}
    if token:
        headers.update(Authorization='Bearer ' + token, Accept='application/vnd.github+json')
    data = urlencode(fields).encode() if fields is not None else None
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=15) as response:
            result = json.loads(response.read(131073))
    except HTTPError as error:
        if error.code == 401:
            raise PermissionError('GitHub no longer accepts this login.') from None
        raise GitHubError('GitHub is unavailable. Try again shortly.') from None
    except (URLError, TimeoutError, ValueError):
        raise GitHubError('GitHub did not complete this request. Try again shortly.') from None
    if not isinstance(result, dict):
        raise GitHubError('GitHub returned an invalid response. Try again.')
    return result


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name('.' + path.name + '.tmp')
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(text)
    os.replace(temporary, path)


class GitHubConnection:
    def __init__(self, root, client_id=None, request=request_json, sleep=time.sleep):
        self.data_root = Path(root)
        self.root = folder(root)
        self.client_id = client_id if client_id is not None else (os.environ.get('COCONUT_GITHUB_CLIENT_ID') or CLIENT_ID)
        self.request = request
        self.sleep = sleep
        self.lock = threading.Lock()
        self.login = None
        self.refresh_lock = threading.Lock()
        self.stopping = threading.Event()
        self.refresher = None
        if (self.root / 'refresh_token').is_file():
            self._start_refresher()

    def close(self):
        self.stopping.set()

    def _start_refresher(self):
        if self.refresher is None:
            self.refresher = threading.Thread(target=self._refresh_loop, name='github-refresh', daemon=True)
            self.refresher.start()

    def _refresh_loop(self):
        while not self.stopping.is_set():
            try:
                self.refresh()
            except OSError:
                pass
            self.stopping.wait(60)

    def _save_token(self, result, account):
        """Caller holds the connection lock. Device-flow tokens rotate without an app secret."""
        write_private(token_path(self.data_root), result['access_token'] + '\n')
        refresh = result.get('refresh_token')
        if refresh:
            write_private(self.root / 'refresh_token', refresh + '\n')
        else:
            (self.root / 'refresh_token').unlink(missing_ok=True)
        account = dict(account, needs_reconnect=False)
        for field, lifetime in (('expires_at', 'expires_in'), ('refresh_expires_at', 'refresh_token_expires_in')):
            account.pop(field, None)
            if result.get(lifetime):
                account[field] = time.time() + int(result[lifetime])
        write_private(self.root / 'connection.json', json.dumps(account))

    def refresh(self):
        """Refresh five minutes before expiry. One request runs at a time, outside the service lock."""
        if not self.refresh_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                account = self.account()
                if (not account.get('expires_at') or account['expires_at'] > time.time() + 300
                        or account.get('refresh_failed') or self.stopping.is_set()):
                    return
                token = self.token()
                try:
                    refresh = (self.root / 'refresh_token').read_text().strip()
                except OSError:
                    return
            try:
                result = self.request(TOKEN_URL, dict(client_id=self.client_id, grant_type='refresh_token', refresh_token=refresh))
            except (GitHubError, PermissionError):
                return
            with self.lock:
                if self.stopping.is_set() or self.token() != token:
                    return
                if result.get('error') in ('bad_refresh_token', 'invalid_grant'):
                    write_private(self.root / 'connection.json', json.dumps(dict(account, needs_reconnect=True, refresh_failed=True)))
                elif isinstance(result.get('access_token'), str) and result['access_token'] and result.get('refresh_token'):
                    self._save_token(result, account)
        finally:
            self.refresh_lock.release()

    def token(self):
        """Return Coconut's own token, or an empty string. Never fall back to another login on the Mac."""
        try:
            return token_path(self.data_root).read_text().strip()
        except OSError:
            return ''

    def account(self):
        try:
            return json.loads((self.root / 'connection.json').read_text())
        except (OSError, ValueError):
            return {}

    def pending(self):
        login = self.login
        if login and not login.get('finished') and login['expires'] > time.time():
            return dict(user_code=login['user_code'], verification_uri=login['verification_uri'])
        return None

    def status(self):
        account = self.account()
        connected = bool(self.token() and account.get('login'))
        return dict(connected=connected, login=account.get('login') if connected else None,
                    needs_reconnect=connected and bool(account.get('needs_reconnect')), pending=self.pending(),
                    error=(self.login or {}).get('error'))

    def check(self):
        """Ask GitHub whether the saved login still works, and record the answer for Settings."""
        self.refresh()
        if self.account().get('refresh_failed'):
            return self.status()
        token = self.token()
        if not token:
            return self.status()
        try:
            self.request(USER_URL, token=token)
            needs_reconnect = False
        except PermissionError:
            needs_reconnect = True
        except GitHubError:
            return self.status()
        with self.lock:
            # A sign-in can finish during the request. Record the answer only for the login that it checked.
            account = self.account()
            if self.token() == token and bool(account.get('needs_reconnect')) != needs_reconnect:
                write_private(self.root / 'connection.json', json.dumps(dict(account, needs_reconnect=needs_reconnect)))
        return self.status()

    def start(self):
        with self.lock:
            if self.pending():
                return self.status()
            if not self.client_id:
                raise GitHubError('GitHub sign-in is not configured for this Coconut release.')
            result = self.request(DEVICE_CODE_URL, dict(client_id=self.client_id, scope=SCOPES))
            required = ('device_code', 'user_code', 'verification_uri')
            if not all(isinstance(result.get(key), str) and result[key] for key in required):
                raise GitHubError('GitHub did not return a sign-in code. Try again.')
            login = dict(device_code=result['device_code'], user_code=result['user_code'],
                         verification_uri=result['verification_uri'], interval=max(int(result.get('interval') or 5), 1),
                         expires=time.time() + int(result.get('expires_in') or 900))
            self.login = login
            threading.Thread(target=self.wait, args=(login,), name='github-login', daemon=True).start()
            return self.status()

    def wait(self, login):
        """Ask GitHub for the token until the person approves the code, declines it, or it expires."""
        while self.login is login and time.time() < login['expires']:
            self.sleep(login['interval'])
            if self.login is not login:
                return
            try:
                result = self.request(TOKEN_URL, dict(client_id=self.client_id, device_code=login['device_code'],
                                                      grant_type='urn:ietf:params:oauth:grant-type:device_code'))
            except (GitHubError, PermissionError):
                continue
            error = result.get('error')
            if error == 'authorization_pending':
                continue
            if error == 'slow_down':
                login['interval'] = int(result.get('interval') or login['interval'] + 5)
                continue
            if error or not isinstance(result.get('access_token'), str):
                login.update(finished=True, error='GitHub sign-in did not finish. Connect GitHub again.')
                return
            try:
                user = self.request(USER_URL, token=result['access_token'])
            except (GitHubError, PermissionError):
                login.update(finished=True, error='GitHub could not confirm this account. Connect GitHub again.')
                return
            with self.lock:
                if self.login is not login:
                    return
                self._save_token(result, dict(login=user.get('login'), scopes=result.get('scope', ''), connected_at=time.time()))
                login['finished'] = True
            if result.get('refresh_token'):
                self._start_refresher()
            return
        if self.login is login:
            login.update(finished=True, error='This GitHub code expired. Connect GitHub again.')

    def disconnect(self):
        with self.lock:
            self.login = None
            for path in (token_path(self.data_root), self.root / 'refresh_token', self.root / 'connection.json'):
                path.unlink(missing_ok=True)
        return self.status()


def git_credential_helper(token_file):
    # git asks this helper for each github.com request. It reads Coconut's token file at that moment, so a new or removed
    # login takes effect at once. git runs a helper that starts with ! through the shell.
    return ('!f() { test "$1" = get && test -r ' + shlex.quote(str(token_file)) + ' && echo username=x-access-token'
            ' && echo "password=$(cat ' + shlex.quote(str(token_file)) + ')"; }; f')


def git_settings(token_file, count=0):
    """Command-scope git settings for github.com, in order. The empty helper first clears helpers from git's files,
    such as the macOS Keychain helper that may hold another account. SSH remotes would use the account's SSH keys, so
    they move to HTTPS and the same token."""
    entries = (('credential.https://github.com.helper', ''), ('credential.https://github.com.helper', git_credential_helper(token_file)),
               ('url.https://github.com/.insteadOf', 'git@github.com:'), ('url.https://github.com/.insteadOf', 'ssh://git@github.com/'))
    settings = {'GIT_CONFIG_COUNT': str(count + len(entries))}
    for index, (key, value) in enumerate(entries, count):
        settings.update({f'GIT_CONFIG_KEY_{index}': key, f'GIT_CONFIG_VALUE_{index}': value})
    return settings


def bot_environment(env, token_file):
    """Give a new bot process Coconut's login when it exists. Without it, the environment stays unchanged."""
    try:
        token = Path(token_file).read_text().strip()
    except OSError:
        token = ''
    if not token:
        return env
    env = dict(env, GH_TOKEN=token, COCONUT_GITHUB_LOGIN='1')
    for key in TOKEN_VARIABLES[1:]:
        env.pop(key, None)
    try:
        count = int(env.get('GIT_CONFIG_COUNT') or 0)
    except ValueError:
        count = 0
    env.update(git_settings(token_file, count))
    return env
