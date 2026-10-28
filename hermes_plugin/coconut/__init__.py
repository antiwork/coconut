"""The `coconut` platform of the Hermes gateway, with its tools and hooks. See adapter.py, tools.py and hooks.py.

Hermes imports this package in each of its processes that loads plugins, so the adapter loads only when the gateway
creates it, and the tools and hooks act only in a Coconut chat of the gateway.
"""
import dataclasses
import os

NAME = 'coconut'
HINT = ('You are in Coconut, the iPhone app of the person who runs this Hermes. Coconut shows Markdown. '
        'Write the answer for a phone screen. Ask the person with coconut_ask when you need a choice or a fact.')
# The phone shows the replies and the interim messages. Tool progress and long-run notices would be extra messages.
DISPLAY = {'tool_progress': 'off', 'tool_preview_length': 0, 'show_reasoning': False,
           'long_running_notifications': False, 'busy_ack_detail': False}


def _adapter(config):
    from .adapter import CoconutAdapter
    return CoconutAdapter(config)


def register(ctx):
    from gateway.platform_registry import PlatformEntry
    options = dict(
        # Only the Coconut service on this Mac sends messages, and only for the person's paired phones.
        trusted_inbound=True, display_tier='high', display_overrides=DISPLAY, platform_hint=HINT,
        max_message_length=100_000, emoji='🥥')
    # A field that this Hermes does not know would stop the registration.
    known = {field.name for field in dataclasses.fields(PlatformEntry)}
    ctx.register_platform(name=NAME, label='Coconut', adapter_factory=_adapter, check_fn=lambda: True,
                          **{key: value for key, value in options.items() if key in known})
    # A Coconut worker has its own coconut tools and runs no gateway (backend/coconut/providers.py sets this variable).
    if os.environ.get('COCONUT_BOT_ID'):
        return
    from . import hooks, tools
    tools.register(ctx)
    hooks.register(ctx)
