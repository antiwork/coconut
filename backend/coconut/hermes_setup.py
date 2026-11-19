"""GET /v1/setup: the state of the person's Hermes for the phone, for example a version that Coconut has not tested.

Coconut runs on the person's own Hermes, so it copies nothing from it. The earlier onboarding imported profiles, model
keys and MCP servers from another Hermes home; that import is gone.
"""
from __future__ import annotations

import time

from . import hermes_runtime

HERMES_CHECK_SECONDS = 60
RESTARTING = 'Coconut restarts for the new Hermes when the running turns finish.'


class HermesSetup:
    """Service route for the phone's Hermes status, and the watch for a new Hermes."""
    # server.main sets the checked status of the person's Hermes: {status, version, reason, tested}. See hermes_runtime.public.
    hermes_status = None
    # [status, identity, time of the next check]. Only the login service watches. See watch_hermes.
    _hermes_watch = None
    # The time at which a restart for a new Hermes stops waiting for the running turns. See ServiceRestart.poll_hermes_change.
    hermes_restart_deadline = None

    def hermes_setup(self) -> dict:
        # A service that a tool starts directly has no checked status. Fixture mode runs no Hermes.
        hermes = self.hermes_status or {
            'status': 'ok' if getattr(self.provider, 'name', None) == 'hermes' else 'fixture', 'version': None, 'reason': None,
            'tested': None}
        if self.hermes_restart_deadline is not None:
            hermes = dict(hermes, reason=RESTARTING, restarting=True)
        result = {'hermes': hermes}
        if hasattr(self, 'gateway_enabled') and self.gateway_enabled():
            # Which Hermes gateway runs the chat turns, or why none does. See gateway_chat.
            result['gateway'] = self.gateway_status()
        return result

    def watch_hermes(self, status: dict):
        """Note the Hermes that this process started with. See hermes_changed."""
        identity = hermes_identity(status)
        if identity is not None:
            self._hermes_watch = [status, identity, time.monotonic() + HERMES_CHECK_SECONDS]

    def hermes_changed(self) -> bool:
        """Return True when the next start would find another Hermes than this process. Check at most once a minute.

        People update Hermes without a restart of Coconut. The next process runs the contract check on the new Hermes and
        watches from there, so a new Hermes that fails the check causes no second restart.
        """
        watch = self._hermes_watch
        if watch is None or time.monotonic() < watch[2]:
            return False
        watch[2] = time.monotonic() + HERMES_CHECK_SECONDS
        identity = hermes_identity(watch[0])
        return identity is not None and identity != watch[1]


def hermes_identity(status: dict) -> str | None:
    try:
        return hermes_runtime.identity(status)
    except (OSError, RuntimeError, ValueError):
        return None
