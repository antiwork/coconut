"""The text of a Hermes notice for the chat: a sign-in notice in place of its raw name, and a model switch in plain
words.

Hermes writes its notices with a lookup in its message catalog (agent/i18n.py, `t`). When the catalog does not load,
the lookup returns the bare name of the notice, for example `gateway.errors.no_credentials`, and the chat would show
that name. The lookup keeps a failed load until the process ends or its language cache is reset (agent/i18n.py,
`_load_catalog`).

Coconut carries a text for the two sign-in notices only, because a person cannot answer the raw
name of these notices. A wider rule would change real replies: a config key, a web address or a file name can have the
same form as the name of a notice.

When Hermes switches to its fallback model, it sends a status line: "Model fallback: gpt-6.1-sol via openai-codex
unavailable (provider failure); using claude-sonnet-5-5 via anthropic." (agent/chat_completion_helpers.py,
try_activate_fallback). A person reads "unavailable" as an outage of the provider. But "provider failure" is the label of
Hermes for a switch without a known cause, for example after empty replies of the model. Coconut shows: "gpt-6.1-sol
gave no answer. claude-sonnet-5-5 answers this turn." A reason that names a cause stays in brackets. Only a paragraph
that is exactly that line changes, so a reply that names the notice in a sentence stays.
"""

from __future__ import annotations

import logging
import re

LOG = logging.getLogger(__name__)

# The text for each name. The first sentence is the English text of Hermes (locales/en.yaml, the same in Hermes 0.22.3
# to 0.23.1). The rest differs from Hermes, because a Coconut chat does not accept `/login` and `/model`: the adapter
# sends chat text with `allow_gateway_control=False` (hermes_plugin/coconut/adapter.py), and gateway_bridge.COMMANDS
# does not have them.
NOTICES = {
    'gateway.errors.no_credentials': (
        "⚠️ I couldn't connect to the AI model service, so this message wasn't processed. On the Mac of this agent, "
        "run `hermes doctor` to see the cause. The agent can need a new sign-in for its model, or a different model."),
    'gateway.background.no_credentials': (
        "❌ The background task couldn't start because no AI model sign-in is configured. On the Mac of this agent, "
        "run `hermes doctor` to see the cause."),
}


# The status line of Hermes for a switch to its fallback model. A rate limit adds the retry sentence.
MODEL_SWITCH = re.compile(
    r'\u26a0\ufe0f Model fallback: (?P<old>\S+) via (?P<old_provider>\S+) unavailable \((?P<reason>[^()\n]+)\); '
    r'using (?P<new>\S+) via (?P<new_provider>\S+?)\.'
    r'(?: Primary retry eligible in ~(?P<seconds>\d+) s; recovery is not guaranteed\.)?')
# The reasons of Hermes that name no cause: its label for an unknown reason, which it also uses after empty replies of
# the model, and a reply of only reasoning.
NO_CAUSE = ('provider failure', 'incomplete response')


def _model_switch(paragraph: str) -> str:
    """A paragraph that is the model-switch line of Hermes, in plain words. Each other paragraph as it is."""
    line = paragraph.strip()
    match = MODEL_SWITCH.fullmatch(line)
    if match is None:
        return paragraph
    old, new = match['old'], match['new']
    if old == new:
        # The same model through another provider: the providers tell the two apart.
        old, new = f'{old} via {match["old_provider"]}', f'{new} via {match["new_provider"]}'
    reason = '' if match['reason'] in NO_CAUSE else f' ({match["reason"]})'
    retry = f' The agent can try {match["old"]} again in about {match["seconds"]} s.' if match['seconds'] else ''
    return paragraph.replace(line, f'\u26a0\ufe0f {old} gave no answer{reason}. {new} answers this turn.{retry}', 1)


def readable_text(text: str) -> str:
    """Return the text of a gateway message for the chat.

    When the first paragraph is only the name of a sign-in notice, the text of the notice replaces that paragraph. The
    spaces around the name go with it. Each character after the first paragraph stays. A paragraph that is the
    model-switch line of Hermes gets plain words. Each other text comes back as the same object.
    """
    if not isinstance(text, str):
        return text
    first, gap, rest = text.partition('\n\n')
    name = first.strip()
    if name in NOTICES:
        LOG.warning('Hermes sent the raw name of the notice %s in place of its text. A restart of the Hermes gateway '
                    'can load its message catalog again.', name)
        return NOTICES[name] + gap + rest
    if 'Model fallback: ' not in text:
        return text
    plain = '\n\n'.join(map(_model_switch, text.split('\n\n')))
    return text if plain == text else plain
