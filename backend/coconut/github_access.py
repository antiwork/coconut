"""The GitHub login of the app on this Mac: its GitHub notifications, its GitHub pages, Business and the to-do lists.

A team member never connects GitHub in Coconut (the person, 2026-11-28: "a team member should never have to do anything
to set up the connection"). The Mac uses the gh login that its account already has, so a comment, a move or a decision
keeps the person's name. Coconut only reads that login with `gh auth token`, which changes nothing, and never runs
gh auth login, logout or setup-git. A Mac that signed in to GitHub in Coconut in an earlier version keeps that login
(github_connection.py) while it works. A Mac with neither reads the to-do lists from Gumclaw's team page
(github_todos.py) and has no GitHub notifications.

The agents do not use this: each bot keeps the GitHub login of its terminal (providers.py, Service.pr_tracker_for).
"""
from __future__ import annotations

import hashlib
import os
import pwd
import shutil
import subprocess
import threading
import time

from .github_connection import TOKEN_VARIABLES, USER_URL, request_json
from .runtime_environment import LOGIN_FOLDERS

# Each read of the gh login starts a process, so the Mac keeps it for a short time. Without a login it asks again sooner,
# so a new gh login takes effect soon.
LOGIN_SECONDS, NO_LOGIN_SECONDS = 120, 30


def gh_token() -> str:
    """The token of the gh login of the Mac account for github.com, or an empty string. The tokens and the login folders
    of the service's environment are dropped, so gh reads the account's own login, as a terminal of the person does."""
    executable = shutil.which('gh')
    if not executable:
        return ''
    account = pwd.getpwuid(os.getuid())
    environment = {key: value for key, value in os.environ.items() if key not in (*TOKEN_VARIABLES, *LOGIN_FOLDERS)}
    environment.update(HOME=account.pw_dir, USER=account.pw_name)
    try:
        result = subprocess.run([executable, 'auth', 'token', '--hostname', 'github.com'], env=environment,
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return result.stdout.strip() if result.returncode == 0 else ''


class GitHubAccess:
    """The token and the account of the GitHub login of the app (see the top of this file). GitHubBusiness reads and
    writes with it."""

    def __init__(self, connection, read=gh_token, fetch=request_json):
        self.connection = connection
        self.read, self.fetch = read, fetch
        self.lock = threading.Lock()
        # The gh login and the time until which it is kept.
        self.kept = (0.0, '')
        # The account of the newest gh login, by the hash of its token.
        self.accounts = {}

    def _signed_in(self) -> bool:
        """Whether the login of the old sign-in works. One that needs a new sign-in gives way to the gh login, since the
        app no longer asks for one."""
        account = self.connection.account()
        return bool(self.connection.token() and account.get('login') and not account.get('needs_reconnect'))

    def refresh(self):
        if self.connection.token():
            self.connection.refresh()

    def token(self) -> str:
        return self.connection.token() if self._signed_in() else self._gh()

    def refused(self, token: str) -> None:
        """GitHub refused this token. An old sign-in is checked, which records that it needs a new one, so the gh login
        takes its place at the next request. A gh login is read again."""
        if token == self.connection.token():
            self.connection.check()
            return
        with self.lock:
            self.kept, self.accounts = (0.0, ''), {}

    def _gh(self) -> str:
        with self.lock:
            until, token = self.kept
        if until > time.monotonic():
            return token
        token = self.read()
        with self.lock:
            self.kept = (time.monotonic() + (LOGIN_SECONDS if token else NO_LOGIN_SECONDS), token)
        return token

    def account(self) -> dict:
        """The account of the login: the one of the old sign-in, or the GitHub user of the gh login. Empty without a
        login and while GitHub does not answer."""
        if self._signed_in():
            return self.connection.account()
        token = self._gh()
        if not token:
            return {}
        identity = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            kept = self.accounts.get(identity)
        if kept:
            return dict(kept)
        try:
            user = self.fetch(USER_URL, token=token)
        except (OSError, ValueError):
            return {}
        login = user.get('login')
        if not isinstance(login, str) or not login:
            return {}
        with self.lock:
            self.accounts = {identity: dict(login=login)}
        return dict(login=login)

    def status(self) -> dict:
        """GET /v1/settings/github: whether the app has a GitHub login, and whose. The GitHub screen of a build before
        this one shows it."""
        login = self.account().get('login')
        source = ('coconut' if self._signed_in() else 'gh') if login else None
        return dict(connected=bool(login), login=login or None, needs_reconnect=False, pending=None, error=None,
                    source=source)
