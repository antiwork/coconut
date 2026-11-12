"""The `coconut` platform of the Hermes gateway, with its tools and hooks. See adapter.py, tools.py and hooks.py.

Hermes imports this package in each of its processes that loads plugins, so the adapter loads only when the gateway
creates it, and the tools and hooks act only in a Coconut chat of the gateway.

`gateway.platforms.coconut.extra.mode: team` in the profile is the mode of the team chat of a Mac that runs Coconut
for a team only (team.py). In that mode the plugin registers the platform and its adapter, and nothing else. A fault
in that mode turns the channel off with one log line, and the gateway and its other channels go on.
"""
import dataclasses
import logging
import os

NAME = 'coconut'
TEAM = 'team'
LOG = logging.getLogger(__name__)
TEAM_HINT = ('You are in the team chat of Coconut. Each member of the team can write to you, and the whole team reads '
             'your reply. Coconut shows Markdown. Coconut has no cards here: when you need a choice or a fact, ask in '
             'your reply.')
HINT = ('You are in Coconut, the iPhone app of the person who runs this Hermes. Coconut shows Markdown. '
        'Write the answer for a phone screen. When the person writes to you, start with one short sentence that says '
        'what you will do, before you call a tool: the phone shows your text as you write it. Ask the person with '
        'coconut_ask when you need a choice or a fact: the card ends your turn, and Coconut continues the conversation '
        'with the answer.')
# The phone shows the replies and the interim messages. Tool progress and long-run notices would be extra messages.
DISPLAY = {'tool_progress': 'off', 'tool_preview_length': 0, 'show_reasoning': False,
           'long_running_notifications': False, 'busy_ack_detail': False}


def _adapter(config):
    from .adapter import CoconutAdapter
    return CoconutAdapter(config)


def platform_hint():
    """HINT and the worker's rules for cards, for a merge card, for bot changes and for the Library (card_tools).

    Hermes puts the hint of the platform into the system prompt of each Coconut session, with no size limit. The prompt
    sections of all plugins share a budget of 8000 characters, and the rules are longer: Hermes skipped two of their
    three parts in each session."""
    from .tools import cards
    module = cards()
    rules = (module.DECISION_INSTRUCTIONS, module.PR_MERGE_INSTRUCTIONS, module.BOT_CREATION_INSTRUCTIONS,
             module.BOT_DELETION_INSTRUCTIONS, module.LIBRARY_INSTRUCTIONS)
    return '\n\n'.join([HINT, *(text.strip() for text in rules)])


def mode() -> str | None:
    """The mode of the plugin in the config of this profile: `team`, or None for a full install."""
    from hermes_cli import config
    load = getattr(config, 'load_config_readonly', None) or config.load_config
    platforms = ((load() or {}).get('gateway') or {}).get('platforms') or {}
    block = platforms.get(NAME) if isinstance(platforms, dict) else None
    extra = block.get('extra') if isinstance(block, dict) else None
    return extra.get('mode') if isinstance(extra, dict) else None


def register_team(ctx):
    """Register the platform with the adapter of the team chat, and nothing else. Each fault, also an import of a
    Hermes that lacks a part, gives one log line and leaves the channel off. A Hermes that lacks only a field of the
    registration that the chat needs (team.unregistered) gets one log line and a channel that takes no message."""
    try:
        from gateway.platform_registry import PlatformEntry
        from . import team
        lacking = team.missing()
        if lacking:
            LOG.warning('Coconut: the team chat stays off, because this Hermes lacks %s. Update Coconut or Hermes.',
                        lacking)
            return
        options = dict(trusted_inbound=True, display_tier='high', display_overrides=DISPLAY, platform_hint=TEAM_HINT,
                       max_message_length=100_000, emoji='🥥')
        known = {field.name for field in dataclasses.fields(PlatformEntry)}
        team.TeamAdapter.lacking = team.unregistered(known)
        if team.TeamAdapter.lacking:
            LOG.warning('Coconut: the team chat takes no message, because this Hermes lacks %s. Update Hermes.',
                        team.TeamAdapter.lacking)
        ctx.register_platform(name=NAME, label='Coconut', adapter_factory=team.TeamAdapter, check_fn=lambda: True,
                              **{key: value for key, value in options.items() if key in known})
    except Exception as error:
        LOG.warning('Coconut: the team chat stays off, because the plugin cannot work with this Hermes (%s).',
                    type(error).__name__)


def register(ctx):
    try:
        team = mode() == TEAM
    except Exception:
        # A full install registers as before when the config does not read.
        team = False
    if team:
        register_team(ctx)
        return
    from gateway.platform_registry import PlatformEntry
    options = dict(
        # Only the Coconut service on this Mac sends messages, and only for the person's paired phones.
        trusted_inbound=True, display_tier='high', display_overrides=DISPLAY, platform_hint=platform_hint(),
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
