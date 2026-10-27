"""GET /v1/setup: the state of the person's Hermes for the phone, for example a version that Coconut has not tested.

Coconut runs on the person's own Hermes, so it copies nothing from it. The earlier onboarding imported profiles, model
keys and MCP servers from another Hermes home; that import is gone.
"""
from __future__ import annotations


class HermesSetup:
    """Service route for the phone's Hermes status."""
    # server.main sets the checked status of the person's Hermes: {status, version, reason, tested}. See hermes_runtime.public.
    hermes_status = None

    def hermes_setup(self) -> dict:
        # A service that a test or a tool starts directly has no checked status. Fixture mode runs no Hermes.
        return {'hermes': self.hermes_status or {
            'status': 'ok' if getattr(self.provider, 'name', None) == 'hermes' else 'fixture', 'version': None, 'reason': None,
            'tested': None}}
